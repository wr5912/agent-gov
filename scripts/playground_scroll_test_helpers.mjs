export async function scrollNavigationMetrics(page) {
  return page.evaluate(() => {
    const region = document.querySelector('[data-testid="playground-message-scroll-region"]');
    const rail = document.querySelector('[data-testid="playground-scroll-rail"]');
    const marks = [...document.querySelectorAll('[data-testid="playground-scroll-mark"]')];
    if (!region || !rail) return { railHeight: 0, centerDelta: 999, avgGap: 0, minGap: 0, maxGap: 0 };
    const regionRect = region.getBoundingClientRect();
    const railRect = rail.getBoundingClientRect();
    const centers = marks.map((mark) => {
      const rect = mark.getBoundingClientRect();
      return Math.round((rect.top + rect.height / 2) - railRect.top);
    });
    const gaps = centers.slice(1).map((center, index) => center - centers[index]);
    return {
      railHeight: Math.round(railRect.height),
      centerDelta: Math.round(Math.abs((railRect.top + railRect.height / 2) - (regionRect.top + regionRect.height / 2))),
      avgGap: gaps.length ? Math.round(gaps.reduce((sum, gap) => sum + gap, 0) / gaps.length) : 0,
      minGap: gaps.length ? Math.min(...gaps) : 0,
      maxGap: gaps.length ? Math.max(...gaps) : 0,
    };
  });
}

export async function seedPlaygroundMessages(page, turnCount) {
  await page.evaluate((count) => {
    const sessionId = `density-check-${count}`;
    window.sessionStorage.setItem("parity-preserve-playground-session", "1");
    window.localStorage.setItem("playground-active-session", JSON.stringify(sessionId));
  }, turnCount);
  await page.reload({ waitUntil: "domcontentloaded" });
  try {
    await page.getByTestId("playground-scroll-navigator").waitFor({ timeout: 8000 });
  } catch (error) {
    const metrics = await page.getByTestId("playground-messages").evaluate((element) => ({
      messages: element.querySelectorAll("[data-message-id]").length,
      clientHeight: element.clientHeight,
      scrollHeight: element.scrollHeight,
    })).catch(() => ({ messages: -1, clientHeight: -1, scrollHeight: -1 }));
    throw new Error(`playground scroll navigator missing for ${turnCount} turns: ${JSON.stringify(metrics)}; ${error instanceof Error ? error.message : String(error)}`);
  }
}
