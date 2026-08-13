from __future__ import annotations

import json
import shutil
import subprocess
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parents[1]
AUTHORITY_MODULE = (REPO_ROOT / "scripts/playwright_browser_authority.mjs").as_uri()
MANAGED_SCRIPTS = (
    "verify_openapi_docs.mjs",
    "verify_improvement_ui_real_container.mjs",
    "verify_openai_responses_container.mjs",
    "verify_provider_health_container.mjs",
    "verify_playground_cancel.mjs",
)
FORBIDDEN_REAL_MODE_SCRIPTS = (
    "verify_asset_registry.mjs",
    "verify_improvement_decision_ui.mjs",
    "verify_message_actions_browser.mjs",
)
DIRECT_MANAGED_SCRIPTS = MANAGED_SCRIPTS[:4]
HYBRID_MANAGED_MARKERS = {
    "verify_playground_cancel.mjs": "PLAYGROUND_CANCEL_BROWSER",
}
NODE = shutil.which("node")


def _private_directory(path: Path) -> Path:
    path.mkdir(parents=True)
    path.chmod(0o700)
    return path


def _runtime(tmp_path: Path) -> Path:
    root = _private_directory(tmp_path / ("managed-runtime-" + "x" * 120))
    _private_directory(root / "tmp")
    return root


def _node_env(runtime: Path) -> dict[str, str]:
    return {
        "AGENT_GOV_ACCEPTANCE_RUNTIME_ROOT": str(runtime),
        "LC_ALL": "C.UTF-8",
        "PLAYWRIGHT_CHROMIUM_EXECUTABLE_PATH": "/opt/google/chrome/chrome",
        "TMPDIR": str(runtime / "tmp"),
    }


def _run_node(source: str, runtime: Path, cwd: Path) -> subprocess.CompletedProcess[str]:
    if NODE is None:
        pytest.skip("Node.js is unavailable")
    return subprocess.run(
        (NODE, "--input-type=module", "--eval", source),
        cwd=cwd,
        env=_node_env(runtime),
        check=False,
        capture_output=True,
        text=True,
        timeout=10,
    )


def _preamble(runtime: Path) -> str:
    return f"""
import {{ strict as assert }} from "node:assert";
import {{ withManagedChromium }} from {json.dumps(AUTHORITY_MODULE)};
const runtimeRoot = {json.dumps(str(runtime))};
const originalCwd = process.cwd();
const originalTmpdir = process.env.TMPDIR;
"""


def test_managed_browser_keeps_relative_tmpdir_through_close_and_restores(tmp_path: Path) -> None:
    runtime = _runtime(tmp_path)
    original = _private_directory(tmp_path / "original")
    source = (
        _preamble(runtime)
        + """
const events = [];
const chromium = { async launch(options) {
  events.push(["launch", process.cwd(), process.env.TMPDIR, options]);
  return { async close() { events.push(["close", process.cwd(), process.env.TMPDIR]); } };
} };
const result = await withManagedChromium(chromium, { headless: true }, async () => {
  events.push(["callback", process.cwd(), process.env.TMPDIR]);
  process.chdir(originalCwd);
  process.env.TMPDIR = "drifted";
  return 37;
});
assert.equal(result, 37);
assert.deepEqual(events[0], ["launch", runtimeRoot, "tmp", {
  headless: true, executablePath: "/opt/google/chrome/chrome",
}]);
assert.deepEqual(events[1], ["callback", runtimeRoot, "tmp"]);
assert.deepEqual(events[2], ["close", runtimeRoot, "tmp"]);
assert.equal(process.cwd(), originalCwd);
assert.equal(process.env.TMPDIR, originalTmpdir);
console.log("MANAGED_BROWSER_FAKE_OK");
"""
    )

    result = _run_node(source, runtime, original)

    assert result.returncode == 0, result.stderr
    assert result.stdout.strip() == "MANAGED_BROWSER_FAKE_OK"


@pytest.mark.parametrize("failure_stage", ("launch", "callback", "close"))
def test_managed_browser_restores_after_failure(tmp_path: Path, failure_stage: str) -> None:
    runtime = _runtime(tmp_path)
    original = _private_directory(tmp_path / "original")
    source = (
        _preamble(runtime)
        + f"""
const failureStage = {json.dumps(failure_stage)};
const chromium = {{ async launch() {{
  if (failureStage === "launch") throw new Error("launch failure");
  return {{ async close() {{
    assert.equal(process.cwd(), runtimeRoot);
    assert.equal(process.env.TMPDIR, "tmp");
    if (failureStage === "close") throw new Error("close failure");
  }} }};
}} }};
await assert.rejects(() => withManagedChromium(chromium, {{ headless: true }}, async () => {{
  process.chdir(originalCwd);
  process.env.TMPDIR = "drifted";
  if (failureStage === "callback") throw new Error("callback failure");
}}));
assert.equal(process.cwd(), originalCwd);
assert.equal(process.env.TMPDIR, originalTmpdir);
await withManagedChromium({{
  async launch() {{ return {{ async close() {{}} }}; }},
}}, {{ headless: true }}, async () => undefined);
assert.equal(process.cwd(), originalCwd);
assert.equal(process.env.TMPDIR, originalTmpdir);
console.log("MANAGED_BROWSER_FAILURE_RESTORED");
"""
    )

    result = _run_node(source, runtime, original)

    assert result.returncode == 0, result.stderr
    assert result.stdout.strip() == "MANAGED_BROWSER_FAILURE_RESTORED"


def test_managed_browser_rejects_nested_and_concurrent_use(tmp_path: Path) -> None:
    runtime = _runtime(tmp_path)
    original = _private_directory(tmp_path / "original")
    source = (
        _preamble(runtime)
        + """
let release;
let entered;
const held = new Promise((resolve) => { release = resolve; });
const active = new Promise((resolve) => { entered = resolve; });
const chromium = { async launch() { return { async close() {} }; } };
const first = withManagedChromium(chromium, { headless: true }, async () => {
  await assert.rejects(() => withManagedChromium(chromium, { headless: true }, async () => undefined));
  entered();
  await held;
});
await active;
await assert.rejects(() => withManagedChromium(chromium, { headless: true }, async () => undefined));
release();
await first;
assert.equal(process.cwd(), originalCwd);
assert.equal(process.env.TMPDIR, originalTmpdir);
console.log("MANAGED_BROWSER_SERIALIZED");
"""
    )

    result = _run_node(source, runtime, original)

    assert result.returncode == 0, result.stderr
    assert result.stdout.strip() == "MANAGED_BROWSER_SERIALIZED"


def test_managed_browser_rejects_invalid_runtime_and_options(tmp_path: Path) -> None:
    runtime = _runtime(tmp_path)
    original = _private_directory(tmp_path / "original")
    source = (
        _preamble(runtime)
        + """
import { chmodSync } from "node:fs";
const chromium = { async launch() { return { async close() {} }; } };
const callback = async () => undefined;
async function rejected() {
  await assert.rejects(() => withManagedChromium(chromium, { headless: true }, callback));
  assert.equal(process.cwd(), originalCwd);
}
process.env.AGENT_GOV_ACCEPTANCE_RUNTIME_ROOT = "relative";
await rejected();
process.env.AGENT_GOV_ACCEPTANCE_RUNTIME_ROOT = runtimeRoot;
process.env.TMPDIR = runtimeRoot + "/other";
await rejected();
process.env.TMPDIR = runtimeRoot + "/tmp";
chmodSync(runtimeRoot, 0o755);
await rejected();
chmodSync(runtimeRoot, 0o700);
chmodSync(runtimeRoot + "/tmp", 0o755);
await rejected();
chmodSync(runtimeRoot + "/tmp", 0o700);
for (const options of [
  { executablePath: "/tmp/browser" },
  { downloadsPath: "/tmp/downloads" },
  { headless: "true" },
]) {
  await assert.rejects(() => withManagedChromium(chromium, options, callback));
}
console.log("MANAGED_BROWSER_INVALID_REJECTED");
"""
    )

    result = _run_node(source, runtime, original)

    assert result.returncode == 0, result.stderr
    assert result.stdout.strip() == "MANAGED_BROWSER_INVALID_REJECTED"


def test_real_managed_browser_scripts_have_bounded_output_contract() -> None:
    sources = {name: (REPO_ROOT / "scripts" / name).read_text(encoding="utf-8") for name in MANAGED_SCRIPTS}
    for name, source in sources.items():
        assert "withManagedChromium" in source, name
        assert "managedChromiumLaunchOptions" not in source, name

    for name in DIRECT_MANAGED_SCRIPTS:
        source = sources[name]
        assert "chromium.launch" not in source, name
        console_lines = [line.strip() for line in source.splitlines() if "console." in line]
        assert all("JSON.stringify" not in line and "stack" not in line and "message" not in line for line in console_lines), name
        assert all("screenshot" not in line.lower() and "http" not in line.lower() for line in console_lines), name

    for name, marker in HYBRID_MANAGED_MARKERS.items():
        source = sources[name]
        assert "chromium.launch" in source, name
        assert f'console.log("{marker}_OK")' in source, name
        assert f'console.error("{marker}_FAIL")' in source, name
        assert "if (real) {" in source and "outcome = await withManagedChromium(" in source

    playground = sources["verify_playground_cancel.mjs"]
    assert "const browser = await chromium.launch(options)" in playground

    forbidden_sources = {name: (REPO_ROOT / "scripts" / name).read_text(encoding="utf-8") for name in FORBIDDEN_REAL_MODE_SCRIPTS}
    forbidden_tokens = (
        "RUNTIME_UI_BASE",
        "RUNTIME_API_BASE",
        "RUNTIME_API_KEY",
        "requireContainerAcceptance",
        "withManagedChromium",
    )
    for name, source in forbidden_sources.items():
        assert all(token not in source for token in forbidden_tokens), name
        assert "chromium.launch" in source, name
        assert 'mode: "mock"' in source, name
