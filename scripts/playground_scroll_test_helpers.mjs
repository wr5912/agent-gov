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
    const repeatCount = count <= 4 ? 18 : 2;
    const messages = Array.from({ length: count }, (_, index) => {
      const n = index + 1;
      const createdAt = new Date(Date.parse("2026-06-18T00:00:00Z") + index * 2000).toISOString();
      const completedAt = new Date(Date.parse("2026-06-18T00:00:00Z") + index * 2000 + 1000).toISOString();
      const answer = `我是 AgentGov 治理测试助手。第 ${n} 段回复用于构造可滚动的 Playground 长会话，验证刻度密度。`.repeat(repeatCount);
      return [
        { id: `density-user-${count}-${n}`, role: "user", content: `请用一句话说明你的治理职责，序号 ${n}。`, createdAt },
        { id: `density-assistant-${count}-${n}`, role: "assistant", content: answer, createdAt: completedAt, runId: `density-run-${count}-${n}`, sessionId, agentVersionId: "v-density-check", events: [] },
      ];
    }).flat();
    window.sessionStorage.setItem("parity-preserve-playground-session", "1");
    window.localStorage.setItem("playground-active-session", JSON.stringify(sessionId));
    window.localStorage.setItem("playground-session-messages", JSON.stringify({ [sessionId]: messages }));
  }, turnCount);
  await page.reload({ waitUntil: "domcontentloaded" });
  await page.getByTestId("playground-scroll-navigator").waitFor({ timeout: 8000 });
}
