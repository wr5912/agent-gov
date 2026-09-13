export const FORMAL_BROWSER_REPETITIONS = 3;
export const FORMAL_BROWSER_ENGINES = Object.freeze(["chromium", "firefox"]);

export function browserExecutionPlan(selection, { formal }) {
  if (!new Set(["both", ...FORMAL_BROWSER_ENGINES]).has(selection)) {
    throw new Error("BROWSER must be chromium, firefox, or both");
  }
  if (formal && selection !== "both") {
    throw new Error("Formal browser acceptance requires BROWSER=both");
  }
  return selection === "both" ? [...FORMAL_BROWSER_ENGINES] : [selection];
}

export function requireFormalBrowserResultMatrix(results, { formal }) {
  if (!formal) return;
  if (!Array.isArray(results) || results.length !== FORMAL_BROWSER_ENGINES.length * FORMAL_BROWSER_REPETITIONS) {
    throw new Error("Formal browser acceptance must contain exactly six results");
  }
  const actual = results.map((item) => `${item?.engine}:${item?.attempt}`).sort();
  const expected = FORMAL_BROWSER_ENGINES.flatMap((engine) => (
    Array.from({ length: FORMAL_BROWSER_REPETITIONS }, (_, index) => `${engine}:${index + 1}`)
  )).sort();
  if (actual.some((value, index) => value !== expected[index])) {
    throw new Error("Formal browser acceptance requires Chromium and Firefox exactly three times each");
  }
}
