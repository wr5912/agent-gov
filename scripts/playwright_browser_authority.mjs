import { lstatSync, realpathSync } from "node:fs";
import { isAbsolute, join } from "node:path";
import process from "node:process";

const FIXED_CHROMIUM = "/opt/google/chrome/chrome";
const RUNTIME_ROOT_ENV = "AGENT_GOV_ACCEPTANCE_RUNTIME_ROOT";
let managedBrowserActive = false;

function requireDirectory(path) {
  const identity = lstatSync(path, { bigint: true });
  const expectedUid = BigInt(process.geteuid());
  if (
    !identity.isDirectory()
    || identity.uid !== expectedUid
    || (identity.mode & 0o777n) !== 0o700n
    || realpathSync(path) !== path
  ) {
    throw new Error("Managed Playwright runtime authority is invalid.");
  }
}

function managedBrowserAuthority() {
  const executablePath = process.env.PLAYWRIGHT_CHROMIUM_EXECUTABLE_PATH;
  if (executablePath !== FIXED_CHROMIUM || realpathSync(FIXED_CHROMIUM) !== FIXED_CHROMIUM) {
    throw new Error("Managed Playwright browser authority is invalid.");
  }
  const executable = lstatSync(FIXED_CHROMIUM, { bigint: true });
  if (!executable.isFile() || executable.uid !== 0n || (executable.mode & 0o022n) !== 0n) {
    throw new Error("Managed Playwright browser authority is invalid.");
  }

  const runtimeRoot = process.env[RUNTIME_ROOT_ENV];
  if (!runtimeRoot || !isAbsolute(runtimeRoot)) {
    throw new Error("Managed Playwright runtime authority is invalid.");
  }
  const temporary = join(runtimeRoot, "tmp");
  if (process.env.TMPDIR !== temporary) {
    throw new Error("Managed Playwright runtime authority is invalid.");
  }
  requireDirectory(runtimeRoot);
  requireDirectory(temporary);
  return { executablePath, runtimeRoot };
}

function enterManagedRuntime(authority) {
  requireDirectory(authority.runtimeRoot);
  requireDirectory(join(authority.runtimeRoot, "tmp"));
  process.chdir(authority.runtimeRoot);
  process.env.TMPDIR = "tmp";
}

export async function withManagedChromium(chromium, options, callback) {
  if (managedBrowserActive || typeof chromium?.launch !== "function" || typeof callback !== "function") {
    throw new Error("Managed Playwright browser lifecycle is invalid.");
  }
  if (
    options === null
    || typeof options !== "object"
    || Array.isArray(options)
    || ![Object.prototype, null].includes(Object.getPrototypeOf(options))
    || Object.keys(options).some((name) => name !== "headless")
    || (Object.hasOwn(options, "headless") && typeof options.headless !== "boolean")
  ) {
    throw new Error("Managed Playwright browser launch options are invalid.");
  }
  const authority = managedBrowserAuthority();
  const originalCwd = process.cwd();
  const hadTmpdir = Object.hasOwn(process.env, "TMPDIR");
  const originalTmpdir = process.env.TMPDIR;
  let browser;
  managedBrowserActive = true;
  try {
    enterManagedRuntime(authority);
    browser = await chromium.launch({ ...options, executablePath: authority.executablePath });
    return await callback(browser);
  } finally {
    try {
      enterManagedRuntime(authority);
      await browser?.close();
    } finally {
      process.chdir(originalCwd);
      if (hadTmpdir) process.env.TMPDIR = originalTmpdir;
      else delete process.env.TMPDIR;
      managedBrowserActive = false;
    }
  }
}
