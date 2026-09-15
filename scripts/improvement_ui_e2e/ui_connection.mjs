// 每个 Playwright Browser.newPage() 都有独立 Context；通过公开设置 UI 配置连接。
export async function configureUiApiConnection(page, config) {
  await page.goto(config.uiBase, { waitUntil: "domcontentloaded" });
  await page.getByTestId("playground").waitFor({ timeout: config.actionTimeoutMs });
  await page.getByTestId("open-settings").click();
  await page.getByTestId("settings-panel").waitFor({ timeout: config.actionTimeoutMs });
  await page.getByTestId("settings-tab-developer").click();
  await page.getByTestId("settings-api-base").fill(config.apiBase);
  await page.getByTestId("settings-api-key").fill(config.apiKey);
  await page.getByTestId("settings-save").click();
  await page.getByTestId("settings-panel").waitFor({ state: "detached", timeout: config.actionTimeoutMs });
}
