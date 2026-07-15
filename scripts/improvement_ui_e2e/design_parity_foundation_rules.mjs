let fillJsonEditor;
let has;
let observedApiRequests;
let openAuditImprovement;
let openImprovementById;
let removeReleaseWorkbenchHarness;
let renderReleaseWorkbenchHarness;
let scrollDistance;
let scrollNavigationMetrics;
let seedPlaygroundMessages;
let stageGridHeightMetrics;
let stageTarget;
let ts;
let textIncludes;
let visible;
let waitForObservedRequest;
let waitNearBottom;
let waitPreviewOpen;

export function createFoundationRules(context) {
  ({
    fillJsonEditor,
    has,
    observedApiRequests,
    openAuditImprovement,
    openImprovementById,
    removeReleaseWorkbenchHarness,
    renderReleaseWorkbenchHarness,
    scrollDistance,
    scrollNavigationMetrics,
    seedPlaygroundMessages,
    stageGridHeightMetrics,
    stageTarget,
    ts,
    textIncludes,
    visible,
    waitForObservedRequest,
    waitNearBottom,
    waitPreviewOpen,
  } = context);
  return RULES;
}

const RULES = [
  { id: "nav-converged", phase: "P0", desc: "一级导航三支柱 Playground/改进事项/资产复利；测试发布归入改进治理第四阶段，旧发布不作为顶级主导航", async fn(page) {
    const nav = await page.locator(".topbar-nav .topbar-nav-button").count();
    const asset = await has(page, "nav-asset");
    const release = await has(page, "nav-release");
    const feedbackTopNav = await page.getByRole("button", { name: "反馈优化", exact: true }).count();
    return { ok: nav === 3 && asset && !release && feedbackTopNav === 0, detail: `topbar-nav=${nav} nav-asset=${asset} nav-release=${release} 反馈优化顶级=${feedbackTopNav}（期望 3/true/false/0）` };
  } },
  { id: "settings-ia", phase: "P0", desc: "Settings 使用宽幅工作台弹窗，含业务 Agent 与 Developer 分组，旧自动推进策略入口不存在", async fn(page) {
    await (page.getByTestId("open-settings").click().catch(() => page.getByRole("button", { name: "设置" }).first().click()));
    await page.getByTestId("settings-panel").waitFor({ timeout: 8000 });
    const box = await page.getByTestId("settings-panel").boundingBox();
    const wide = (box?.width || 0) >= 1000;
    const tall = (box?.height || 0) >= 760;
    const navigation = await visible(page, "settings-navigation");
    const content = await visible(page, "settings-content");
    const oldHorizontalTabs = await page.locator(".settings-tabs").count();
    const tabs = ["agents", "developer"];
    const found = [];
    for (const tab of tabs) {
      await page.getByTestId(`settings-tab-${tab}`).click();
      const section = `settings-section-${tab}`;
      await page.getByTestId(section).waitFor({ timeout: 5000 }).catch(() => {});
      if (await visible(page, section)) found.push(section);
    }
    await page.getByTestId("settings-tab-agents").click();
    const agentTable = await visible(page, "settings-agent-table");
    const obsoleteAutomation = await page.getByTestId("settings-tab-automation").count();
    await page.getByTestId("settings-tab-developer").click();
    const runtimeInput = await visible(page, "settings-api-base");
    // 关闭设置弹窗，避免 modal-backdrop 拦截后续规则的点击。
    await page.locator(".settings-footer").getByRole("button", { name: "关闭" }).click().catch(() => {});
    await page.getByTestId("settings-panel").waitFor({ state: "detached", timeout: 5000 }).catch(() => {});
    const ok = wide && tall && navigation && content && oldHorizontalTabs === 0 && found.length === tabs.length && agentTable && obsoleteAutomation === 0 && runtimeInput;
    return { ok, detail: `size=${Math.round(box?.width || 0)}x${Math.round(box?.height || 0)} nav=${navigation} content=${content} oldTabs=${oldHorizontalTabs} sections=${found.length}/${tabs.length} table=${agentTable} obsoleteAutomation=${obsoleteAutomation} runtime=${runtimeInput}` };
  } },
  { id: "playground-clean", phase: "P1", desc: "Playground 主区无旧 Subagent/Sessions/Skills 侧栏、无 Inspector、无常显 control-strip", async fn(page) {
    await page.getByTestId("nav-playground").click(); await page.waitForTimeout(400);
    const legacySidebar = await page.locator(".sidebar .panel-section").count();
    const inspector = await page.locator(".inspector").count();
    const controlStrip = await page.locator(".control-strip").count();
    return { ok: legacySidebar === 0 && inspector === 0 && controlStrip === 0, detail: `legacy-sidebar=${legacySidebar} inspector=${inspector} control-strip=${controlStrip}（期望全 0）` };
  } },
  { id: "playground-action-semantics", phase: "P1", desc: "Playground 动作语义分离：无旧配置入口，会话与运行设置分开", async fn(page) {
    await page.getByTestId("nav-playground").click(); await page.waitForTimeout(400);
    const oldConfig = await page.getByTestId("playground-config-trigger").count();
    const sessionTrigger = page.getByTestId("playground-session-trigger");
    const runtimeTrigger = page.getByTestId("playground-runtime-settings-trigger");
    const sessionCount = await sessionTrigger.count();
    const runtimeCount = await runtimeTrigger.count();
    const sessionText = sessionCount ? (await sessionTrigger.first().innerText().catch(() => "")).trim() : "";
    const sessionAria = sessionCount ? await sessionTrigger.first().getAttribute("aria-label").catch(() => "") : "";
    const sessionTitle = sessionCount ? await sessionTrigger.first().getAttribute("title").catch(() => "") : "";
    const sessionExpanded = sessionCount ? await sessionTrigger.first().getAttribute("aria-expanded").catch(() => "") : "";
    const sessionBox = sessionCount ? await sessionTrigger.first().boundingBox().catch(() => null) : null;
    const titleBox = await page.locator(".chat-header h2").first().boundingBox().catch(() => null);
    const runtimeBox = runtimeCount ? await runtimeTrigger.first().boundingBox().catch(() => null) : null;
    const runtimeText = runtimeCount ? await runtimeTrigger.first().innerText().catch(() => "") : "";
    const sessionIsIconOnly = sessionText === "";
    const sessionIsLeft = !!sessionBox && !!titleBox && !!runtimeBox && sessionBox.x < titleBox.x && sessionBox.x < runtimeBox.x;
    const sessionOk = sessionCount === 1
      && sessionIsIconOnly
      && sessionAria === "展开会话栏"
      && sessionTitle === "展开会话栏"
      && sessionExpanded === "false"
      && sessionIsLeft;
    const runtimeOk = runtimeCount === 1 && runtimeText.includes("运行设置") && !runtimeText.includes("会话");
    return { ok: oldConfig === 0 && sessionOk && runtimeOk, detail: `oldConfig=${oldConfig} session=${sessionCount}/iconOnly=${sessionIsIconOnly}/left=${sessionIsLeft}/aria=${sessionAria}/title=${sessionTitle}/expanded=${sessionExpanded} runtime=${runtimeCount}/${runtimeText}` };
  } },
  { id: "playground-session-sidebar", phase: "P1", desc: "Playground 会话管理进入左侧可折叠导航栏，且不混入运行设置", async fn(page) {
    await page.getByTestId("nav-playground").click();
    const trigger = await has(page, "playground-session-trigger");
    const closedBefore = await page.getByTestId("playground-session-sidebar").count() === 0;
    if (trigger) await page.getByTestId("playground-session-trigger").click();
    const sidebar = page.getByTestId("playground-session-sidebar");
    await sidebar.waitFor({ timeout: 8000 }).catch(() => {});
    const open = await visible(page, "playground-session-sidebar");
    const width = open ? ((await sidebar.boundingBox())?.width || 0) : 0;
    const text = open ? await sidebar.innerText().catch(() => "") : "";
    const expandedAria = await page.getByTestId("playground-session-trigger").getAttribute("aria-expanded").catch(() => "");
    const expandedLabel = await page.getByTestId("playground-session-trigger").getAttribute("aria-label").catch(() => "");
    const duplicatedCloseInSidebar = open ? await sidebar.getByLabel("折叠会话栏").count() : 0;
    const hasSessionControls = text.includes("新会话") && text.includes("会话");
    const noRuntimeSettings = !text.includes("Subagent") && !text.includes("Skills Mode") && !text.includes("Allowed Tools");
    if (open) {
      await page.getByTestId("playground-session-trigger").click();
      await sidebar.waitFor({ state: "detached", timeout: 5000 }).catch(() => {});
    }
    const closedAfterToggle = await page.getByTestId("playground-session-sidebar").count() === 0;
    const collapsedAria = await page.getByTestId("playground-session-trigger").getAttribute("aria-expanded").catch(() => "");
    return { ok: trigger && closedBefore && open && width >= 260 && width <= 340 && expandedAria === "true" && expandedLabel === "折叠会话栏" && duplicatedCloseInSidebar === 0 && closedAfterToggle && collapsedAria === "false" && hasSessionControls && noRuntimeSettings, detail: `trigger=${trigger} defaultCollapsed=${closedBefore} open=${open} width=${Math.round(width)} expanded=${expandedAria}/${expandedLabel} sidebarCloseButtons=${duplicatedCloseInSidebar} closedAfterToggle=${closedAfterToggle}/${collapsedAria} sessionControls=${hasSessionControls} noRuntimeSettings=${noRuntimeSettings}` };
  } },
  { id: "playground-runtime-settings-drawer", phase: "P1", desc: "Playground 运行设置进入独立抽屉，且不混入会话历史", async fn(page) {
    await page.getByTestId("nav-playground").click();
    const trigger = await has(page, "playground-runtime-settings-trigger");
    if (trigger) await page.getByTestId("playground-runtime-settings-trigger").click();
    const drawer = page.getByTestId("playground-runtime-settings-drawer");
    await drawer.waitFor({ timeout: 8000 }).catch(() => {});
    const open = await visible(page, "playground-runtime-settings-drawer");
    const size = open ? await drawer.getAttribute("data-size") : null;
    const agentSettingsSection = open && await has(page, "runtime-agent-settings");
    const parameterSettingsSection = open && await has(page, "runtime-parameter-settings");
    const maxTurnsControl = open && await drawer.locator('input[type="number"]').count() === 1;
    const hasRuntimeSettings = agentSettingsSection && parameterSettingsSection && maxTurnsControl;
    const noMisleadingControls = open
      && !(await textIncludes(drawer, "Skills Mode"))
      && !(await textIncludes(drawer, "Allowed Tools"))
      && !(await textIncludes(drawer, "Disallowed Tools"));
    const noSessionHistory = open
      && await drawer.getByText("新会话").count() === 0
      && await drawer.getByText("删除会话映射").count() === 0
      && await drawer.getByText("Sessions").count() === 0
      && await drawer.getByTestId("playground-session-list").count() === 0
      && await page.getByTestId("playground-session-sidebar").count() === 0;
    const debug = open ? page.getByTestId("runtime-debug-section") : null;
    const debugClosed = debug ? await debug.evaluate((el) => !el.open).catch(() => false) : false;
    if (debug) await debug.locator("summary").click().catch(() => {});
    const debugVisible = open ? await textIncludes(drawer, "Runtime") && !(await textIncludes(drawer, "Events")) && !(await textIncludes(drawer, "Subagents / Skills")) : false;
    const agentConfigVisible = open ? await textIncludes(drawer, "Agent 配置") && await textIncludes(drawer, "版本治理运行态") : false;
    const mcpEditButton = open ? await has(page, "runtime-config-edit-mcp") : false;
    let mcpEditorOpened = false;
    let mcpEditorApplied = false;
    if (mcpEditButton) {
      await page.getByTestId("runtime-config-edit-mcp").click();
      await page.getByTestId("agent-config-file-editor").waitFor({ timeout: 8000 }).catch(() => {});
      mcpEditorOpened = await visible(page, "agent-config-file-editor");
      if (mcpEditorOpened) {
        await fillJsonEditor(page, '{"mcpServers":{"parity":{"command":"node","args":["server.js"]}}}\n');
        await page.getByTestId("agent-config-file-editor-format").click();
        await page.getByTestId("agent-config-file-editor-apply").click();
        await page.getByTestId("agent-config-file-editor-status").waitFor({ timeout: 8000 }).catch(() => {});
        mcpEditorApplied = await visible(page, "agent-config-file-editor-status");
      }
      if (mcpEditorOpened) await page.getByTestId("agent-config-file-editor").getByRole("button", { name: "关闭" }).click().catch(() => {});
      await page.getByTestId("agent-config-file-editor").waitFor({ state: "detached", timeout: 5000 }).catch(() => {});
    }
    const noLegacyGovernancePath = open ? !(await textIncludes(drawer, "/data/agent-governance")) : false;
    if (open) {
      await drawer.getByLabel("关闭").click();
      await drawer.waitFor({ state: "detached", timeout: 5000 }).catch(() => {});
    }
    return {
      ok: trigger && open && size === "wide" && hasRuntimeSettings && noMisleadingControls && noSessionHistory && debugClosed && debugVisible && agentConfigVisible && mcpEditorOpened && mcpEditorApplied && noLegacyGovernancePath,
      detail: `trigger=${trigger} open=${open} size=${size} runtimeSettings=${hasRuntimeSettings} sections=${agentSettingsSection}/${parameterSettingsSection} maxTurns=${maxTurnsControl} noMisleadingControls=${noMisleadingControls} noSessionHistory=${noSessionHistory} debugClosed=${debugClosed} debugVisible=${debugVisible} agentConfig=${agentConfigVisible} mcpEditor=${mcpEditorOpened}/${mcpEditorApplied} legacyPath=${!noLegacyGovernancePath}`,
    };
  } },
  { id: "message-actions", phase: "P1", desc: "助手回复动作含 创建反馈/查看Trace/获取上下文（领域级 data-testid）", async fn(page) {
    await page.getByTestId("nav-playground").click();
    const create = await has(page, "message-action-create-feedback");
    const trace = await has(page, "message-action-view-trace");
    const ctx = await has(page, "message-action-get-context");
    return { ok: create && trace && ctx, detail: `create=${create} trace=${trace} get-context=${ctx}` };
  } },
  { id: "playground-scroll-navigation", phase: "P1", desc: "Playground 长对话自动置底、上滚暂停、一键置底与滚动预览导航", async fn(page) {
    await page.getByTestId("nav-playground").click();
    await page.getByTestId("playground-scroll-navigator").waitFor({ timeout: 8000 });
    await waitNearBottom(page);
    const initialDistance = await scrollDistance(page);
    await page.getByTestId("playground-messages").evaluate((el) => {
      el.scrollTop = 0;
      el.dispatchEvent(new Event("scroll", { bubbles: true }));
    });
    await page.getByTestId("playground-jump-to-bottom").waitFor({ timeout: 5000 });
    const jump = await visible(page, "playground-jump-to-bottom");
    await page.getByTestId("playground-scroll-rail").hover();
    await waitPreviewOpen(page);
    const previewItems = await page.getByTestId("playground-scroll-preview-item").count();
    const previewRoles = await page.getByTestId("playground-scroll-preview-item").evaluateAll((items) => items.map((item) => item.getAttribute("data-message-role")));
    const markCount = await page.getByTestId("playground-scroll-mark").count();
    const markRoles = await page.getByTestId("playground-scroll-mark").evaluateAll((items) => items.map((item) => item.getAttribute("data-message-role")));
    const largeMetrics = await scrollNavigationMetrics(page);
    await page.getByTestId("playground-scroll-preview-item").first().click();
    await page.waitForFunction(() => {
      const el = document.querySelector('[data-testid="playground-messages"]');
      return !!el && el.scrollTop <= 80;
    }, null, { timeout: 5000 });
    const nearTop = await page.getByTestId("playground-messages").evaluate((el) => el.scrollTop <= 80);
    await page.getByTestId("playground-jump-to-bottom").click();
    await waitNearBottom(page);
    const finalDistance = await scrollDistance(page);
    const noPanelMix = await page.getByTestId("playground-evidence-panel").count() === 0
      && await page.getByTestId("feedback-drawer").count() === 0
      && await page.getByTestId("playground-runtime-settings-drawer").count() === 0;
    const anchorRolesOk = previewRoles.every((role) => role === "user") && markRoles.every((role) => role === "user");
    await seedPlaygroundMessages(page, 4);
    await page.getByTestId("playground-messages").evaluate((el) => {
      el.scrollTop = 0;
      el.dispatchEvent(new Event("scroll", { bubbles: true }));
    });
    await page.getByTestId("playground-scroll-rail").hover();
    await waitPreviewOpen(page);
    const fewPreviewItems = await page.getByTestId("playground-scroll-preview-item").count();
    const fewMarkCount = await page.getByTestId("playground-scroll-mark").count();
    const fewPreviewRoles = await page.getByTestId("playground-scroll-preview-item").evaluateAll((items) => items.map((item) => item.getAttribute("data-message-role")));
    const fewMarkRoles = await page.getByTestId("playground-scroll-mark").evaluateAll((items) => items.map((item) => item.getAttribute("data-message-role")));
    const fewMetrics = await scrollNavigationMetrics(page);
    const fewRolesOk = fewPreviewRoles.every((role) => role === "user") && fewMarkRoles.every((role) => role === "user");
    await page.evaluate(() => window.sessionStorage.removeItem("parity-preserve-playground-session"));
    await page.reload({ waitUntil: "domcontentloaded" });
    await page.getByTestId("playground").waitFor({ timeout: 8000 });

    const ok = initialDistance <= 24
      && jump
      && previewItems === 36
      && markCount === 24
      && anchorRolesOk
      && largeMetrics.railHeight >= 340
      && largeMetrics.maxGap <= 24
      && largeMetrics.centerDelta <= 24
      && fewPreviewItems === 4
      && fewMarkCount === 4
      && fewRolesOk
      && fewMetrics.railHeight >= 90
      && fewMetrics.railHeight <= 130
      && fewMetrics.avgGap >= 24
      && fewMetrics.avgGap <= 40
      && fewMetrics.centerDelta <= 24
      && nearTop
      && finalDistance <= 24
      && noPanelMix;
    return { ok, detail: `initial=${initialDistance} jump=${jump} large=${previewItems}/${markCount}/${largeMetrics.railHeight}px gap=${largeMetrics.minGap}-${largeMetrics.maxGap} userOnly=${anchorRolesOk} few=${fewPreviewItems}/${fewMarkCount}/${fewMetrics.railHeight}px avgGap=${fewMetrics.avgGap} fewUserOnly=${fewRolesOk} center=${largeMetrics.centerDelta}/${fewMetrics.centerDelta} nearTop=${nearTop} final=${finalDistance} noPanelMix=${noPanelMix}` };
  } },
  { id: "trace-evidence-panel", phase: "P0", desc: "SDK transcript 历史的查看 Trace 打开右侧 block 证据面板，不伪造 Langfuse run 元数据", async fn(page) {
    await page.getByTestId("nav-playground").click();
    if (!(await has(page, "message-action-view-trace"))) return { ok: false, detail: "无 Trace 入口" };
    await page.getByTestId("message-action-view-trace").first().click();
    await page.getByTestId("playground-evidence-panel").waitFor({ timeout: 8000 });
    const panel = await visible(page, "playground-evidence-panel");
    const traceTab = await visible(page, "evidence-tab-trace");
    const tabCount = await page.locator(".evidence-tab").count();
    const legacy = await page.locator(".detail-modal-card").isVisible().catch(() => false);
    const traceDrawer = await page.getByTestId("trace-drawer").count();
    const panelText = await page.getByTestId("playground-evidence-panel").innerText().catch(() => "");
    const langfuse = await has(page, "trace-open-langfuse");
    const langfuseHref = await page.getByTestId("trace-open-langfuse").first().getAttribute("href").catch(() => "");
    const concreteTrace = (langfuseHref || "").includes("/project/agent-gov/traces/") && !(langfuseHref || "").includes("langfuse-web:3000");
    await page.getByTestId("playground-evidence-panel").getByLabel("折叠运行证据栏").click();
    await page.getByTestId("playground-evidence-panel").waitFor({ state: "detached", timeout: 5000 }).catch(() => {});
    const sdkBlockEvidence = panelText.includes("tool_use") || panelText.includes("Read");
    return { ok: panel && traceTab && tabCount === 1 && !legacy && traceDrawer === 0 && sdkBlockEvidence && (!langfuse || concreteTrace), detail: `panel=${panel} traceTab=${traceTab} tabCount=${tabCount} legacyModal=${legacy} traceDrawer=${traceDrawer} sdkBlocks=${sdkBlockEvidence} langfuse=${langfuse} href=${langfuseHref}` };
  } },
  { id: "panel-size-policy", phase: "P0", desc: "侧栏、tab 面板与抽屉按职责分档且打开后稳定", async fn(page) {
    await page.getByTestId("nav-playground").click();
    await page.getByTestId("message-action-view-trace").first().click();
    await page.getByTestId("playground-evidence-panel").waitFor({ timeout: 8000 });
    const traceWidth = (await page.getByTestId("playground-evidence-panel").boundingBox())?.width || 0;
    const resizeHandle = page.getByTestId("evidence-panel-resize-handle");
    const resizeBox = await resizeHandle.boundingBox();
    if (resizeBox) {
      await page.mouse.move(resizeBox.x + resizeBox.width / 2, resizeBox.y + 36);
      await page.mouse.down();
      await page.mouse.move(resizeBox.x - 110, resizeBox.y + 36, { steps: 8 });
      await page.mouse.up();
    }
    const resizedTraceWidth = (await page.getByTestId("playground-evidence-panel").boundingBox())?.width || 0;
    const resizeAria = Number(await resizeHandle.getAttribute("aria-valuenow") || 0);
    await page.getByTestId("playground-evidence-panel").getByLabel("折叠运行证据栏").click();
    await page.getByTestId("playground-evidence-panel").waitFor({ state: "detached", timeout: 5000 }).catch(() => {});
    await page.getByTestId("message-action-create-feedback").first().click();
    await page.getByTestId("feedback-drawer").waitFor({ timeout: 8000 });
    const feedbackSize = await page.getByTestId("feedback-drawer").getAttribute("data-size");
    const feedbackWidth = (await page.getByTestId("feedback-drawer").boundingBox())?.width || 0;
    await page.getByTestId("feedback-drawer").getByLabel("关闭").click();
    await page.getByTestId("feedback-drawer").waitFor({ state: "detached", timeout: 5000 }).catch(() => {});
    await page.getByTestId("playground-session-trigger").click();
    await page.getByTestId("playground-session-sidebar").waitFor({ timeout: 8000 });
    const sessionWidth = (await page.getByTestId("playground-session-sidebar").boundingBox())?.width || 0;
    await page.getByTestId("playground-session-trigger").click();
    await page.getByTestId("playground-session-sidebar").waitFor({ state: "detached", timeout: 5000 }).catch(() => {});
    await page.getByTestId("playground-runtime-settings-trigger").click();
    await page.getByTestId("playground-runtime-settings-drawer").waitFor({ timeout: 8000 });
    const settingsSize = await page.getByTestId("playground-runtime-settings-drawer").getAttribute("data-size");
    const settingsWidth = (await page.getByTestId("playground-runtime-settings-drawer").boundingBox())?.width || 0;
    await page.getByTestId("playground-runtime-settings-drawer").getByLabel("关闭").click();
    await page.getByTestId("playground-runtime-settings-drawer").waitFor({ state: "detached", timeout: 5000 }).catch(() => {});
    const ok = traceWidth >= 520
      && traceWidth <= 590
      && resizedTraceWidth >= traceWidth + 80
      && resizedTraceWidth <= 680
      && resizeAria === Math.round(resizedTraceWidth)
      && feedbackSize === "narrow"
      && feedbackWidth >= 430
      && sessionWidth >= 260
      && sessionWidth <= 340
      && settingsSize === "wide"
      && settingsWidth >= 860;
    return { ok, detail: `trace-panel=${Math.round(traceWidth)} resized=${Math.round(resizedTraceWidth)} aria=${resizeAria} feedback=${feedbackSize}/${Math.round(feedbackWidth)} session-sidebar=${Math.round(sessionWidth)} settings=${settingsSize}/${Math.round(settingsWidth)}` };
  } },
  { id: "feedback-drawer-2phase", phase: "P1", desc: "创建反馈 Drawer 两阶段：输入态 → 系统理解确认态", async fn(page) {
    await page.getByTestId("nav-playground").click();
    if (!(await has(page, "feedback-drawer-open"))) return { ok: false, detail: "无 feedback-drawer-open 入口" };
    await page.getByTestId("feedback-drawer-open").first().click();
    const open = await visible(page, "feedback-drawer");
    const state = open ? await page.getByTestId("feedback-drawer").getAttribute("data-state") : null;
    if (open) {
      await page.getByTestId("feedback-drawer").getByLabel("关闭").click();
      await page.getByTestId("feedback-drawer").waitFor({ state: "detached", timeout: 5000 }).catch(() => {});
    }
    return { ok: open && state === "input", detail: `drawer 可见=${open} data-state=${state}` };
  } },
  { id: "context-4types", phase: "P2", desc: "获取上下文四类型 + 下载", async fn(page) {
    if (!(await openAuditImprovement(page))) return { ok: false, detail: "无改进事项可打开上下文（需种子数据）" };
    await page.getByTestId("open-context-drawer").click().catch(() => {});
    await page.getByTestId("context-drawer").waitFor({ timeout: 8000 }).catch(() => {});
    const drawerSize = await page.getByTestId("context-drawer").getAttribute("data-size").catch(() => null);
    const types = ["context-type-problem", "context-type-ai", "context-type-playwright", "context-type-json"];
    const found = []; for (const t of types) if (await has(page, t)) found.push(t);
    await page.getByTestId("context-type-json").click().catch(() => {});
    const preview = await page.getByTestId("context-preview").innerText().catch(() => "");
    const rich = preview.includes('"attribution_id"')
      && preview.includes('"agent_version_id"')
      && preview.includes('"optimization_plan_id"')
      && preview.includes('"asset_id"')
      && preview.includes('"test_dataset_ref"')
      && !preview.includes('"attribution": null')
      && !preview.includes('"evidence": []');
    const download = await has(page, "context-download");
    await page.getByTestId("context-drawer").getByLabel("关闭").click().catch(() => {});
    await page.getByTestId("context-drawer").waitFor({ state: "detached", timeout: 5000 }).catch(() => {});
    return { ok: drawerSize === "medium" && found.length === 4 && download && rich, detail: `size=${drawerSize} 类型 ${found.length}/4，下载=${download}，证据链JSON=${rich}` };
  } },
  { id: "release-workbench-target-binding", phase: "P2", desc: "发布工作台两候选选择、普通发布/回归/强制发布目标绑定、确认目标冻结、来源门禁与发布错误均按选中候选呈现", async fn(page) {
    const base = {
      agent_id: "soc-ops", created_at: ts, updated_at: ts, base_commit_sha: "base-demo",
      branch_name: "agent-change/test", worktree_path: "/tmp/test", diff_summary: {}, source_improvement_id: "imp-demo04",
    };
    const ready = [
      { ...base, change_set_id: "agc-target-a", title: "候选 A", status: "candidate_committed", candidate_commit_sha: "candidate-a", execution_job_id: "exec-1" },
      {
        ...base, change_set_id: "agc-target-b", title: "候选 B", status: "candidate_committed", candidate_commit_sha: "candidate-b",
        execution_job_id: "exec-1",
        source_attribution_id: "attr-b", source_attribution_status: "confirmed",
        publication_error: { detail: "release metadata pending reconciliation", updated_at: ts },
      },
    ];
    try {
      await renderReleaseWorkbenchHarness(page, [{ ...ready[1], change_set_id: "agc-execution-mismatch", execution_job_id: "exec-other" }]);
      await page.waitForFunction(() => document.querySelectorAll('[data-testid="release-regression-dataset"] option').length === 1);
      const mismatchedExecutionDatasetHidden = await page.getByTestId("release-regression-dataset").inputValue() === "";
      await renderReleaseWorkbenchHarness(page, ready);
      await page.waitForFunction(() => document.querySelectorAll('[data-testid="release-regression-dataset"] option').length === 1);
      const mismatchedCandidateDatasetHidden = await page.getByTestId("release-regression-dataset").inputValue() === "";
      await page.getByTestId("release-changeset-select").selectOption("agc-target-b");
      await page.waitForFunction(() => document.querySelector('[data-testid="release-changeset-details"]')?.textContent?.includes("候选 B"));
      await page.waitForFunction(() => document.querySelectorAll('[data-testid="release-regression-dataset"] option').length === 2);
      const boundGate = await page.getByTestId("release-gate-attribution").getAttribute("data-state");
      const errorVisible = await page.getByText("release metadata pending reconciliation", { exact: false }).first().isVisible().catch(() => false);
      await page.getByTestId("release-regression-dataset").selectOption("tds-demo04");

      observedApiRequests.length = 0;
      await page.getByTestId("release-action-run-regression").click();
      const regressionBound = await waitForObservedRequest((r) => r.path === "/api/agent-change-sets/agc-target-b/regression-runs");
      const regressionRequest = observedApiRequests.find((r) => r.path === "/api/agent-change-sets/agc-target-b/regression-runs");
      const regressionBody = JSON.parse(regressionRequest?.postData || "{}");

      observedApiRequests.length = 0;
      await page.getByTestId("release-action-publish").click();
      const publishBound = await waitForObservedRequest((r) => r.path === "/api/agent-change-sets/agc-target-b/publish");
      const normalPublish = observedApiRequests.find((r) => r.path === "/api/agent-change-sets/agc-target-b/publish");
      const normalBody = JSON.parse(normalPublish?.postData || "{}");

      await renderReleaseWorkbenchHarness(page, ready.map((item) => ({ ...item, status: "regression_failed" })));
      await page.getByTestId("release-action-force").click();
      await page.getByTestId("release-force-confirm").waitFor({ timeout: 5000 });
      const frozenLabel = await page.getByTestId("release-force-confirm").innerText();
      await page.getByTestId("release-changeset-select").evaluate((element) => {
        element.value = "agc-target-a";
        element.dispatchEvent(new Event("change", { bubbles: true }));
      });
      await page.waitForFunction(() => document.querySelector('[data-testid="release-changeset-details"]')?.textContent?.includes("候选 A"));
      const manualGate = await page.getByTestId("release-gate-attribution").getAttribute("data-state");

      observedApiRequests.length = 0;
      await page.getByTestId("release-force-confirm-submit").click();
      const forceBound = await waitForObservedRequest((r) => r.path === "/api/agent-change-sets/agc-target-b/publish");
      const forcePublish = observedApiRequests.find((r) => r.path === "/api/agent-change-sets/agc-target-b/publish");
      const forceBody = JSON.parse(forcePublish?.postData || "{}");
      await page.getByTestId("release-force-confirm").waitFor({ state: "detached", timeout: 5000 });
      await renderReleaseWorkbenchHarness(page, [{
        ...ready[1],
        publication_provenance_blocker: "改进执行尚未确认或执行来源不完整，请先确认执行结果",
        publication_blocker: "改进执行尚未确认或执行来源不完整，请先确认执行结果",
      }]);
      const provenancePublishDisabled = await page.getByTestId("release-action-publish").isDisabled();
      const provenanceForceDisabled = await page.getByTestId("release-action-force").isDisabled();
      const provenanceOptimizationGate = await page.getByTestId("release-gate-optimization").getAttribute("data-state");
      await renderReleaseWorkbenchHarness(page, [
        { ...ready[0], status: "regression_running" },
        { ...ready[1], change_set_id: "agc-rejected", title: "已拒绝候选", status: "rejected" },
        { ...ready[1], change_set_id: "agc-failed", title: "失败候选", status: "failed" },
      ]);
      const runningRegressionDisabled = await page.getByTestId("release-action-run-regression").isDisabled();
      const terminalCandidatesHidden = await page.getByText("已拒绝候选", { exact: true }).count() === 0
        && await page.getByText("失败候选", { exact: true }).count() === 0;
      await renderReleaseWorkbenchHarness(page, [{
        ...ready[0],
        status: "failed",
        worktree_cleanup_pending: true,
      }]);
      observedApiRequests.length = 0;
      const cleanupVisible = await has(page, "release-cleanup-pending");
      await page.getByTestId("release-action-retry-cleanup").click();
      const cleanupBound = await waitForObservedRequest((request) => request.path === "/api/agent-change-sets/agc-target-a/worktree-cleanup/retry");
      await renderReleaseWorkbenchHarness(page, [{
        ...ready[0],
        change_set_id: "agc-other-improvement",
        source_improvement_id: "imp-other",
      }]);
      await page.waitForFunction(() => document.querySelectorAll('[data-testid="release-regression-dataset"] option').length === 1);
      const crossImprovementDatasetCleared = await page.getByTestId("release-regression-dataset").inputValue() === "";
      const regressionPayloadIsTyped = Object.keys(regressionBody).sort().join(",") === "dataset_id"
        && regressionBody.dataset_id === "tds-demo04";
      const ok = mismatchedExecutionDatasetHidden && mismatchedCandidateDatasetHidden && boundGate === "pass" && manualGate === "unknown" && errorVisible
        && regressionBound && regressionPayloadIsTyped && publishBound && normalBody.force === false
        && frozenLabel.includes("agc-target-b") && forceBound && forceBody.force === true
        && provenancePublishDisabled && provenanceForceDisabled && provenanceOptimizationGate === "pending"
        && runningRegressionDisabled && terminalCandidatesHidden && cleanupVisible && cleanupBound && crossImprovementDatasetCleared;
      return {
        ok,
        detail: `executionMismatchHidden=${mismatchedExecutionDatasetHidden} candidateMismatchHidden=${mismatchedCandidateDatasetHidden} attribution=${boundGate}/manual=${manualGate} error=${errorVisible} regressionB=${regressionBound}/${regressionPayloadIsTyped} publishB=${publishBound}/${normalBody.force} frozenB=${frozenLabel.includes("agc-target-b")} forceB=${forceBound}/${forceBody.force} provenanceDisabled=${provenancePublishDisabled}/${provenanceForceDisabled}/${provenanceOptimizationGate} runningDisabled=${runningRegressionDisabled} terminalHidden=${terminalCandidatesHidden} cleanup=${cleanupVisible}/${cleanupBound} crossImprovementDatasetCleared=${crossImprovementDatasetCleared}`,
      };
    } finally {
      await removeReleaseWorkbenchHarness(page);
    }
  } },
  { id: "release-regression-review", phase: "P2", desc: "待人工复核回归逐条决策完整后只提交专用 review 路由，且不能以强制发布替代", async fn(page) {
    const datasetCases = [
      { case_id: "tdc-demo-1", position: 1, prompt: "核验事件时间", expected_behavior: "先核验时间一致性。", checkpoints: ["核验时间"] },
      { case_id: "tdc-demo-2", position: 2, prompt: "核验数据源", expected_behavior: "先确认数据源真实性。", checkpoints: ["核验数据源"] },
    ];
    const latestEvalRun = {
      eval_run_id: "evr-review-demo",
      dataset_id: "tds-demo04",
      dataset_snapshot: { dataset_id: "tds-demo04" },
      change_set_id: "agc-review-demo",
      agent_id: "soc-ops",
      source: "typed_test_dataset",
      status: "completed",
      result_status: "needs_human_review",
      items: datasetCases.map((item, index) => ({
        eval_run_id: "evr-review-demo",
        eval_run_item_id: `evri-review-${index + 1}`,
        dataset_case_id: item.case_id,
        dataset_case_snapshot: item,
        status: "needs_human_review",
        answer_summary: `自动判定无法确认用例 ${index + 1}`,
      })),
      gate_result: {
        status: "review_required",
        blocked_dataset_case_ids: [],
        review_dataset_case_ids: datasetCases.map((item) => item.case_id),
        note_dataset_case_ids: [],
      },
      summary: { total: 2, passed: 0, failed: 0, blocked: 0, needs_human_review: 2, note: 0 },
      created_at: ts,
    };
    const changeSet = {
      change_set_id: "agc-review-demo",
      agent_id: "soc-ops",
      created_at: ts,
      updated_at: ts,
      status: "regression_review_required",
      execution_job_id: "exec-1",
      base_commit_sha: "base-demo",
      candidate_commit_sha: "candidate-b",
      branch_name: "agent-change/agc-review-demo",
      worktree_path: "/tmp/agc-review-demo",
      title: "待人工复核候选",
      diff_summary: {},
      source_improvement_id: "imp-demo04",
      latest_eval_run: latestEvalRun,
    };
    try {
      await renderReleaseWorkbenchHarness(page, [changeSet]);
      await page.getByTestId("release-regression-review").waitFor({ timeout: 5000 });
      const forceDisabled = await page.getByTestId("release-action-force").isDisabled();
      const submitDisabledBefore = await page.getByTestId("release-review-submit").isDisabled();
      await page.getByTestId("release-review-operator").fill("reviewer@example.com");
      await page.getByTestId("release-review-reason").fill("人工核验关键证据后给出逐条结论");
      await page.getByTestId("release-review-approve-tdc-demo-1").click();
      const submitDisabledIncomplete = await page.getByTestId("release-review-submit").isDisabled();
      await page.getByTestId("release-review-reject-tdc-demo-2").click();
      const submitEnabledComplete = !(await page.getByTestId("release-review-submit").isDisabled());

      observedApiRequests.length = 0;
      await page.getByTestId("release-review-submit").click();
      const reviewBound = await waitForObservedRequest((request) => request.path === "/api/agent-change-sets/agc-review-demo/regression-runs/evr-review-demo/review");
      await page.getByTestId("release-action-message").waitFor({ timeout: 5000 });
      await page.waitForFunction(() => window.__releaseWorkbenchRefreshCount === 1);
      const refreshCount = await page.evaluate(() => window.__releaseWorkbenchRefreshCount);
      const reviewRequest = observedApiRequests.find((request) => request.path === "/api/agent-change-sets/agc-review-demo/regression-runs/evr-review-demo/review");
      const reviewBody = JSON.parse(reviewRequest?.postData || "{}");
      const decisions = Array.isArray(reviewBody.decisions) ? reviewBody.decisions : [];
      const payloadExact = reviewRequest?.method === "POST"
        && Object.keys(reviewBody).sort().join(",") === "decisions,operator,reason,review_id,scope"
        && reviewBody.review_id === "review-evr-review-demo"
        && reviewBody.operator === "reviewer@example.com"
        && reviewBody.reason === "人工核验关键证据后给出逐条结论"
        && reviewBody.scope === "current_eval_run"
        && decisions.length === 2
        && decisions.every((item) => Object.keys(item).sort().join(",") === "dataset_case_id,decision")
        && decisions[0]?.dataset_case_id === "tdc-demo-1"
        && decisions[0]?.decision === "approve"
        && decisions[1]?.dataset_case_id === "tdc-demo-2"
        && decisions[1]?.decision === "reject";
      const pendingReviewForceRouteUnused = !observedApiRequests.some((request) => request.path.endsWith("/publish"));

      await renderReleaseWorkbenchHarness(page, [{
        ...changeSet,
        status: "regression_failed",
        publication_blocker: "人工复核拒绝了关键用例",
        latest_eval_run: {
          ...latestEvalRun,
          result_status: "failed",
          gate_result: {
            ...latestEvalRun.gate_result,
            status: "blocked",
            review_dataset_case_ids: [],
            blocked_dataset_case_ids: ["tdc-demo-2"],
          },
        },
      }]);
      const forceEnabledAfterRejection = !(await page.getByTestId("release-action-force").isDisabled());
      await page.getByTestId("release-action-force").click();
      await page.getByTestId("release-force-confirm").waitFor({ timeout: 5000 });
      const forceConfirmEnabledAfterRejection = !(await page.getByTestId("release-force-confirm-submit").isDisabled());
      observedApiRequests.length = 0;
      await page.getByTestId("release-force-confirm-submit").click();
      const rejectedForceBound = await waitForObservedRequest((request) => request.path === "/api/agent-change-sets/agc-review-demo/publish");
      const rejectedForceRequest = observedApiRequests.find((request) => request.path === "/api/agent-change-sets/agc-review-demo/publish");
      const rejectedForceBody = JSON.parse(rejectedForceRequest?.postData || "{}");
      const ok = forceDisabled && submitDisabledBefore && submitDisabledIncomplete && submitEnabledComplete
        && reviewBound && payloadExact && refreshCount === 1 && pendingReviewForceRouteUnused
        && forceEnabledAfterRejection && forceConfirmEnabledAfterRejection
        && rejectedForceBound && rejectedForceBody.force === true;
      return {
        ok,
        detail: `forceDisabled=${forceDisabled} submit=${submitDisabledBefore}/${submitDisabledIncomplete}/${submitEnabledComplete} review=${reviewBound}/${payloadExact} refresh=${refreshCount} pendingForceUnused=${pendingReviewForceRouteUnused} rejectedForce=${forceEnabledAfterRejection}/${forceConfirmEnabledAfterRejection}/${rejectedForceBound}/${rejectedForceBody.force}`,
      };
    } finally {
      await removeReleaseWorkbenchHarness(page);
    }
  } },
];
