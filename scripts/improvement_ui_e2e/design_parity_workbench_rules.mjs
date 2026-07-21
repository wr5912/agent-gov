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

export function createWorkbenchRules(context) {
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
  { id: "release-merged-into-test-stage", phase: "P2", desc: "旧发布顶级入口消失，发布条件预览合入测试发布阶段", async fn(page) {
    const releaseNav = await has(page, "nav-release");
    const opened = await openImprovementById(page, stageTarget("testRelease", "imp-demo04"));
    if (!opened) return { ok: false, detail: "无法打开测试发布阶段改进事项" };
    const stage = await page.getByTestId("stage-work-area").getAttribute("data-visible-stage").catch(() => "");
    const gate = await has(page, "stage-panel-release-gate");
    const primary = await page.getByTestId("primary-action").innerText().catch(() => "");
    const actionMatches = primary.includes("生成回归测试") && !primary.includes("运行测试");
    return { ok: !releaseNav && stage === "test_release" && gate && actionMatches, detail: `nav-release=${releaseNav} stage=${stage} gate=${gate} primary=${primary}` };
  } },
  { id: "test-release-stage-panels", phase: "P1", desc: "测试发布阶段展示 pytest 代码、Workspace 测试文件、版本范围和确定性发布门", async fn(page) {
    const opened = await openImprovementById(page, stageTarget("testRelease", "imp-demo04"));
    if (!opened) return { ok: false, detail: "无法打开测试发布阶段改进事项" };
    const panels = ["regression-test-design", "workspace-test-files", "stage-panel-coverage", "stage-panel-execution-baseline", "stage-panel-release-gate"];
    const found = [];
    for (const panel of panels) if (await has(page, panel)) found.push(panel);
    await page.getByTestId("workspace-test-files").getByRole("button", { name: "查看详情" }).click();
    await page.getByTestId("stage-detail-drawer").waitFor({ timeout: 6000 }).catch(() => {});
    const filesText = await page.getByTestId("stage-detail-drawer").innerText().catch(() => "");
    await page.locator(".drawer-shell-actions").getByRole("button", { name: "关闭" }).first().click().catch(() => {});
    await page.getByTestId("stage-detail-drawer").waitFor({ state: "detached", timeout: 4000 }).catch(() => {});
    const confirmButton = page.getByTestId("confirm-regression-tests");
    const materialized = await confirmButton.isDisabled().catch(() => false)
      && (await confirmButton.innerText().catch(() => "")).includes("待发布变更已确认");
    const legacyPanels = await page.locator('[data-testid="test-dataset-asset"], [data-testid="regression-guarantee"], [data-testid="test-dataset-lifecycle-management"]').count();
    const coverage = await has(page, "regression-test-code-coverage");
    const summaryItems = await page.getByTestId("regression-test-code-summary-item").count();
    await page.getByTestId("stage-panel-coverage").getByRole("button", { name: "查看详情" }).click();
    await page.getByTestId("stage-detail-drawer").waitFor({ timeout: 6000 }).catch(() => {});
    const detailItems = await page.getByTestId("regression-test-code-detail-item").count();
    const paths = await page.getByTestId("regression-test-target-path").count();
    const intents = await page.getByTestId("regression-test-intent").count();
    const rationales = await page.getByTestId("regression-test-rationale").count();
    const codeText = await page.getByTestId("regression-test-code").first().innerText().catch(() => "");
    const codeSemantics = codeText.includes("agent.run(") && codeText.includes("result.text") && codeText.includes("assert");
    await page.locator(".drawer-shell-actions").getByRole("button", { name: "关闭" }).first().click().catch(() => {});
    await page.getByTestId("stage-detail-drawer").waitFor({ state: "detached", timeout: 4000 }).catch(() => {});
    return {
      ok: found.length === panels.length
        && filesText.includes("tests/test_feedback_imp_demo04_01_time.py")
        && materialized
        && legacyPanels === 0
        && coverage
        && summaryItems >= 3
        && detailItems >= 3
        && paths >= 3
        && intents >= 3
        && rationales >= 3
        && codeSemantics,
      detail: [
        "panels=" + found.length + "/" + panels.length,
        "files=" + filesText.includes("tests/test_feedback_imp_demo04_01_time.py"),
        "materialized=" + materialized,
        "legacy=" + legacyPanels,
        "coverage=" + coverage,
        "summary=" + summaryItems,
        "detail=" + detailItems,
        "codeSemantics=" + codeSemantics,
      ].join(" "),
    };
  } },
  { id: "improvement-default-detail", phase: "P1", desc: "改进列表有数据时默认展示首个详情，不留空白首屏", async fn(page) {
    await page.getByTestId("nav-improvement").click();
    await page.getByTestId("improvement-workbench").waitFor({ timeout: 8000 });
    await page.getByTestId("improvement-detail").waitFor({ timeout: 8000 }).catch(() => {});
    const detail = await has(page, "improvement-detail");
    const emptyVisible = await page.getByTestId("improvement-workbench").first()
      .locator(":scope > .iw-detail-panel > .iw-panel-body > .iw-empty").isVisible().catch(() => false);
    return { ok: detail && !emptyVisible, detail: `detail=${detail} emptyVisible=${emptyVisible}` };
  } },
  { id: "improvement-create-drawer", phase: "P1", desc: "改进事项从标题栏新建，使用独立窄抽屉并在成功后对齐归属范围", async fn(page) {
    await page.getByTestId("nav-improvement").click();
    await page.getByTestId("improvement-workbench").waitFor({ timeout: 8000 });
    const legacyInlineForm = await page.locator(".iw-list-panel .iw-create").count();
    const duplicateScopeText = await page.getByTestId("improvement-scope-label").locator("strong").count();
    const refreshIconOnly = await page.getByTestId("improvement-refresh").getAttribute("aria-label") === "刷新改进事项"
      && (await page.getByTestId("improvement-refresh").innerText()).trim() === "";

    await page.getByTestId("status-filter-done").click();
    await page.getByTestId("improvement-scope-filter").selectOption("shop-bot");
    await page.getByTestId("improvement-create-open").click();
    const drawer = page.getByTestId("improvement-create-drawer");
    await drawer.waitFor({ timeout: 6000 });
    const size = await drawer.getAttribute("data-size");
    const defaultAgent = await page.getByTestId("improvement-create-agent").inputValue();
    await page.getByTestId("improvement-create-cancel").click();
    await drawer.waitFor({ state: "detached", timeout: 4000 });

    await page.getByTestId("improvement-create-open").click();
    await page.getByTestId("improvement-create-agent").selectOption("soc-ops");
    await page.getByTestId("improvement-create-title").fill("触发受控创建失败");
    await page.getByTestId("improvement-create-title").press("Enter");
    await page.getByTestId("improvement-create-error").waitFor({ timeout: 6000 });
    const errorVisible = (await page.getByTestId("improvement-create-error").innerText()).includes("受控创建失败");
    const stayedOpen = await drawer.isVisible();

    const createdTitle = "抽屉创建的改进事项";
    await page.getByTestId("improvement-create-title").fill(createdTitle);
    await page.getByTestId("improvement-create-title").press("Enter");
    await drawer.waitFor({ state: "detached", timeout: 6000 });
    await page.getByTestId("improvement-list-item").filter({ hasText: createdTitle }).waitFor({ timeout: 6000 });
    const selectedScope = await page.getByTestId("improvement-scope-filter").inputValue();
    const createdSelected = await page.getByTestId("improvement-list-item").filter({ hasText: createdTitle }).evaluate((node) => node.classList.contains("is-active"));
    const statusReset = await page.getByTestId("status-filter-all").evaluate((node) => node.classList.contains("active"));

    const ok = legacyInlineForm === 0
      && duplicateScopeText === 0
      && refreshIconOnly
      && size === "narrow"
      && defaultAgent === "shop-bot"
      && errorVisible
      && stayedOpen
      && selectedScope === "soc-ops"
      && createdSelected
      && statusReset;
    return {
      ok,
      detail: `legacy=${legacyInlineForm} duplicateScope=${duplicateScopeText} refreshIcon=${refreshIconOnly} size=${size} default=${defaultAgent} error=${errorVisible}/${stayedOpen} scope=${selectedScope} selected=${createdSelected} statusReset=${statusReset}`,
    };
  } },
  { id: "decision-card-slim", phase: "P1", desc: "决策卡只承载主决策/返回/事实变更动作，不混入查看 Trace/Diff/日志/上下文", async fn(page) {
    if (!(await openAuditImprovement(page))) return { ok: false, detail: "无改进事项" };
    const card = page.getByTestId("current-decision-card");
    const text = await card.innerText().catch(() => "");
    const forbidden = ["查看 Trace", "查看完整 Trace", "查看 Diff", "查看完整 Diff", "查看日志", "查看测试计划", "获取上下文", "查看处理链路"].filter((word) => text.includes(word));
    const primaryCount = await card.getByTestId("primary-action").count();
    const contextOutside = await has(page, "open-context-drawer");
    const kicker = await card.locator(".iw-section-kicker").innerText().catch(() => "");
    const box = await card.boundingBox();
    const compactHeight = !!box && box.height <= 180;
    return {
      ok: forbidden.length === 0 && primaryCount === 1 && contextOutside && kicker.includes("请确认") && compactHeight,
      detail: `forbidden=${forbidden.join(",") || "none"} primary=${primaryCount} contextOutside=${contextOutside} kicker=${kicker} height=${box?.height ?? "na"}`,
    };
  } },
  { id: "invalid-back-actions-hidden", phase: "P1", desc: "execution/release 不展示状态机拒绝的返回动作，regression 合法返工入口保留", async fn(page) {
    if (!(await openImprovementById(page, "imp-demo07"))) return { ok: false, detail: "无法打开 execution 事项" };
    const executionBack = await page.getByTestId("decision-back-action").count();
    const executionText = await page.getByTestId("current-decision-card").innerText().catch(() => "");
    if (!(await openImprovementById(page, "imp-demo08"))) return { ok: false, detail: "无法打开 release 事项" };
    const releaseBack = await page.getByTestId("decision-back-action").count();
    const releaseText = await page.getByTestId("current-decision-card").innerText().catch(() => "");
    if (!(await openImprovementById(page, stageTarget("testRelease", "imp-demo04")))) return { ok: false, detail: "无法打开 regression 事项" };
    const regressionBack = page.getByTestId("decision-back-action");
    const regressionBackCount = await regressionBack.count();
    const regressionAction = await regressionBack.getAttribute("data-action").catch(() => "");
    const obsoleteAutoAdvance = await page.getByTestId("auto-advance").count();
    const ok = executionBack === 0
      && releaseBack === 0
      && !executionText.includes("返回归因分析")
      && !releaseText.includes("返回优化执行")
      && regressionBackCount === 1
      && regressionAction === "execution"
      && obsoleteAutoAdvance === 0;
    return { ok, detail: `execution=${executionBack} release=${releaseBack} regression=${regressionBackCount}:${regressionAction} autoAdvance=${obsoleteAutoAdvance}` };
  } },
  { id: "four-stage-panels", phase: "P1", desc: "四个内部阶段样例分别映射到四阶段工作面板", async fn(page) {
    const expectations = [
      ["feedback", "imp-demo01", "feedback_sorting", "stage-panel-sorting-result"],
      ["attribution", "imp-demo02", "attribution_analysis", "attribution"],
      ["optimization", "imp-demo03", "optimization_execution", "optimization-plan"],
      ["testRelease", "imp-demo04", "test_release", "regression-test-design"],
    ];
    const seen = [];
    for (const [key, mockId, stage, panel] of expectations) {
      const id = stageTarget(key, mockId);
      if (!(await openImprovementById(page, id))) return { ok: false, detail: `无法打开 ${id}` };
      const current = await page.getByTestId("stage-work-area").getAttribute("data-visible-stage").catch(() => "");
      const panelVisible = await has(page, panel);
      const heightMetrics = await stageGridHeightMetrics(page);
      const rowDetail = heightMetrics.rowSpreads.map((row) => `${row.testids.join("+")}=${row.heights.join("/")}`).join(";");
      seen.push(`${id}:${current}:${panelVisible}:rows=${rowDetail}:maxRowSpread=${heightMetrics.maxRowSpread}:overflow=${heightMetrics.overflowing.join(",") || "none"}`);
      if (current !== stage || !panelVisible || heightMetrics.maxRowSpread > 2 || heightMetrics.overflowing.length) return { ok: false, detail: seen.join(" | ") };
    }
    return { ok: true, detail: seen.join(" | ") };
  } },
  { id: "closed-loop-spine", phase: "P1", desc: "改进详情始终显示四阶段 spine，并支持已完成阶段只读回看、未来阶段禁用", async fn(page) {
    if (!(await openImprovementById(page, stageTarget("optimization", "imp-demo03")))) return { ok: false, detail: "无改进事项" };
    await page.getByTestId("closed-loop-spine").waitFor({ timeout: 6000 }).catch(() => {});
    const spine = await has(page, "closed-loop-spine");
    const steps = await page.getByTestId("closed-loop-step").count();
    const labels = await page.getByTestId("closed-loop-step").evaluateAll((nodes) => nodes.map((node) => node.textContent || "").join("|")).catch(() => "");
    const okLabels = ["反馈整理", "归因分析", "优化执行", "测试发布"].every((label) => labels.includes(label));
    const currentBefore = await page.getByTestId("current-decision-card").getAttribute("data-visible-stage").catch(() => "");
    const feedbackStep = page.getByTestId("closed-loop-step").filter({ hasText: "反馈整理" }).first();
    const futureStep = page.getByTestId("closed-loop-step").filter({ hasText: "测试发布" }).first();
    const futureDisabled = await futureStep.isDisabled().catch(() => false);
    await feedbackStep.click();
    await page.getByTestId("stage-review-banner").waitFor({ timeout: 6000 }).catch(() => {});
    const reviewedStage = await page.getByTestId("stage-work-area").getAttribute("data-visible-stage").catch(() => "");
    const decisionAfterReview = await page.getByTestId("current-decision-card").getAttribute("data-visible-stage").catch(() => "");
    const factActions = await page.locator('[data-testid="stage-work-area"] [data-testid="confirm-attribution"], [data-testid="stage-work-area"] [data-testid="generate-attribution"], [data-testid="stage-work-area"] [data-testid="confirm-optimization-plan"], [data-testid="stage-work-area"] [data-testid="adopt-regression"]').count();
    await page.getByTestId("return-current-stage").click();
    const returnedStage = await page.getByTestId("stage-work-area").getAttribute("data-visible-stage").catch(() => "");
    return {
      ok: spine && steps === 4 && okLabels && currentBefore === "optimization_execution" && futureDisabled && reviewedStage === "feedback_sorting" && decisionAfterReview === currentBefore && factActions === 0 && returnedStage === "optimization_execution",
      detail: `spine=${spine} steps=${steps} futureDisabled=${futureDisabled} reviewed=${reviewedStage} decision=${currentBefore}->${decisionAfterReview} factActions=${factActions} returned=${returnedStage}`,
    };
  } },
  { id: "improvement-content", phase: "P3", desc: "改进详情含系统理解、归因结论和证据链，阶段卡内容归属不交叉", async fn(page) {
    if (!(await openImprovementById(page, stageTarget("feedback", "imp-demo01")))) return { ok: false, detail: "无反馈整理事项" };
    const sortingText = await page.getByTestId("stage-panel-sorting-result").innerText().catch(() => "");
    const evidenceText = await page.getByTestId("stage-panel-evidence").innerText().catch(() => "");
    const feedbackOwnershipOk = !sortingText.includes("建议下一步") && !evidenceText.includes("版本影响");

    if (!(await openImprovementById(page, stageTarget("attribution", "imp-demo02")))) return { ok: false, detail: "无改进事项" };
    await page.getByTestId("attribution").waitFor({ timeout: 6000 }).catch(() => {});
    const attr = await has(page, "attribution");
    const ev = await has(page, "attribution-evidence");
    await page.getByTestId("attribution").getByRole("button", { name: "查看详情" }).click().catch(() => {});
    await page.getByTestId("stage-detail-drawer").waitFor({ timeout: 6000 }).catch(() => {});
    const attrDetailText = await page.getByTestId("stage-detail-content").innerText().catch(() => "");
    const attrDetailKey = await page.getByTestId("stage-detail-content").getAttribute("data-detail-key").catch(() => "");
    const attributionOwnershipOk = attrDetailKey === "attribution" && !attrDetailText.includes("证据");
    await page.locator(".drawer-shell-actions").getByRole("button", { name: "关闭" }).first().click().catch(() => {});
    await page.getByTestId("stage-detail-drawer").waitFor({ state: "detached", timeout: 4000 }).catch(() => {});
    return {
      ok: feedbackOwnershipOk && attr && ev && attributionOwnershipOk,
      detail: `整理归属=${feedbackOwnershipOk} 归因=${attr} 证据链=${ev} 归因详情归属=${attributionOwnershipOk}`,
    };
  } },
  { id: "stage-detail-drawers", phase: "P1", desc: "四阶段面板头部「查看详情/管理」统一打开对应详情抽屉（无死按钮，内容与卡片对应）", async fn(page) {
    if (!(await openImprovementById(page, stageTarget("attribution", "imp-demo02")))) return { ok: false, detail: "无改进事项" };
    await page.getByTestId("stage-panel-impact-scope").waitFor({ timeout: 6000 }).catch(() => {});
    const btn = page.getByTestId("stage-panel-impact-scope").getByRole("button", { name: "查看详情" });
    if (!(await btn.count())) return { ok: false, detail: "影响范围卡缺查看详情按钮（疑似死按钮）" };
    await btn.first().click();
    await page.getByTestId("stage-detail-drawer").waitFor({ timeout: 6000 }).catch(() => {});
    const drawer = await visible(page, "stage-detail-drawer");
    const key = await page.getByTestId("stage-detail-content").getAttribute("data-detail-key").catch(() => "");
    await page.locator(".drawer-shell-actions").getByRole("button", { name: "关闭" }).first().click().catch(() => {});
    await page.getByTestId("stage-detail-drawer").waitFor({ state: "detached", timeout: 4000 }).catch(() => {});
    return { ok: drawer && key === "impact-scope", detail: `drawer=${drawer} detailKey=${key}（期望 impact-scope）` };
  } },
  { id: "trace-summary", phase: "P3", desc: "Trace 摘要(§9)：关联运行 + 打开 Langfuse（深色调试区）", async fn(page) {
    if (!(await openImprovementById(page, stageTarget("attribution", "imp-demo02")))) return { ok: false, detail: "无改进事项" };
    await page.getByTestId("trace-summary").waitFor({ state: "attached", timeout: 6000 }).catch(() => {});
    const ts = await has(page, "trace-summary");
    const lf = await has(page, "trace-open-langfuse");
    const href = await page.getByTestId("trace-open-langfuse").first().getAttribute("href").catch(() => "");
    const concreteTrace = (href || "").includes("/project/agent-gov/traces/") && !(href || "").includes("langfuse-web:3000");
    return { ok: ts && lf && concreteTrace, detail: `Trace摘要=${ts} 打开Langfuse=${lf} href=${href}` };
  } },
  { id: "merge-basis", phase: "P3", desc: "相似归并 §8.5：置信度 + 合并依据 + 标记合并不准", async fn(page) {
    if (!(await openAuditImprovement(page))) return { ok: false, detail: "无改进事项" };
    await page.getByTestId("merge-basis").first().waitFor({ state: "attached", timeout: 6000 }).catch(() => {});
    const basis = await has(page, "merge-basis");
    const mark = await has(page, "mark-merge-inaccurate");
    return { ok: basis && mark, detail: `合并依据=${basis} 标记不准=${mark}` };
  } },
  { id: "status-filter", phase: "P3", desc: "改进列表状态过滤 pills(§5 待确认/处理中/待回归/已完成 + 全部)", async fn(page) {
    await page.getByTestId("nav-improvement").click();
    await page.getByTestId("improvement-workbench").waitFor({ timeout: 8000 }).catch(() => {});
    const sf = await has(page, "status-filter");
    let pills = 0;
    for (const k of ["status-filter-all", "status-filter-pending-confirm", "status-filter-in-progress", "status-filter-pending-regression", "status-filter-done"]) if (await has(page, k)) pills += 1;
    return { ok: sf && pills === 5, detail: `过滤区=${sf} pills=${pills}/5` };
  } },
  { id: "full-chain", phase: "P3", desc: "查看完整链路：4 阶段时间线 + 状态", async fn(page) {
    if (!(await openAuditImprovement(page))) return { ok: false, detail: "无改进事项" };
    await page.getByTestId("full-chain").waitFor({ timeout: 6000 }).catch(() => {});
    const fc = await has(page, "full-chain");
    const steps = await page.getByTestId("full-chain-step").count();
    return { ok: fc && steps === 4, detail: `完整链路=${fc} 阶段数=${steps}` };
  } },
  { id: "detail-collapsed", phase: "P2", desc: "改进详情收纳：相似归并/关联对象进「高级」折叠，旧自动化策略入口不存在", async fn(page) {
    if (!(await openAuditImprovement(page))) return { ok: false, detail: "无改进事项" };
    await page.getByTestId("improvement-advanced").waitFor({ timeout: 6000 }).catch(() => {});
    const advanced = await has(page, "improvement-advanced");
    const obsoleteAutomation = await page.getByTestId("automation-mode").count();
    const obsoleteCopy = await page.getByText("自动化策略", { exact: false }).count();
    return { ok: advanced && obsoleteAutomation === 0 && obsoleteCopy === 0, detail: `高级折叠=${advanced} 旧控件=${obsoleteAutomation} 旧文案=${obsoleteCopy}` };
  } },
  { id: "source-feedback-table", phase: "P3", desc: "来源反馈表(§8.4 #/反馈摘要/来源/状态) 进入来源管理抽屉，支持行级详情与 ref-only 缺记录提示", async fn(page) {
    if (!(await openImprovementById(page, stageTarget("duplicate", "imp-demo05")))) return { ok: false, detail: "无重复反馈事项" };
    const hiddenInline = await page.getByTestId("source-feedback-table").isVisible().catch(() => false);
    await page.getByTestId("view-all-feedbacks").click().catch(() => {});
    await page.getByTestId("source-management-drawer").waitFor({ timeout: 6000 }).catch(() => {});
    await page.getByTestId("source-feedback-table").waitFor({ timeout: 6000 }).catch(() => {});
    const drawer = await has(page, "source-management-drawer");
    const basis = await has(page, "source-merge-basis");
    const tbl = await has(page, "source-feedback-table");
    const rows = await page.getByTestId("source-feedback-row").count();
    const detailButtons = await page.getByTestId("source-feedback-detail-open").count();
    await page.getByTestId("source-feedback-detail-open").first().click();
    await page.getByTestId("source-feedback-detail").waitFor({ timeout: 6000 }).catch(() => {});
    const firstDetail = await page.getByTestId("source-feedback-detail").innerText().catch(() => "");
    await page.getByTestId("source-feedback-detail").getByLabel("关闭").click().catch(() => {});
    await page.getByTestId("source-feedback-detail-open").nth(1).click();
    await page.getByTestId("source-feedback-detail").waitFor({ timeout: 6000 }).catch(() => {});
    const secondDetail = await page.getByTestId("source-feedback-detail").innerText().catch(() => "");
    await page.getByTestId("source-feedback-detail").getByLabel("关闭").click().catch(() => {});
    await page.getByTestId("source-management-drawer").getByLabel("关闭").click().catch(() => {});

    let refOnly = true;
    if (await openImprovementById(page, "imp-demo01")) {
      await page.getByTestId("view-all-feedbacks").click().catch(() => {});
      await page.getByTestId("source-management-drawer").waitFor({ timeout: 6000 }).catch(() => {});
      await page.getByTestId("source-feedback-ref-detail-open").first().click();
      await page.getByTestId("source-feedback-ref-missing-detail").waitFor({ timeout: 6000 }).catch(() => {});
      const missing = await page.getByTestId("source-feedback-ref-missing-detail").innerText().catch(() => "");
      refOnly = missing.includes("仅有引用 ID，无反馈记录");
      await page.getByTestId("source-feedback-ref-missing-detail").getByLabel("关闭").click().catch(() => {});
      await page.getByTestId("source-management-drawer").getByLabel("关闭").click().catch(() => {});
    }

    const detailsOk = firstDetail.includes("fb-2") && firstDetail.includes("第一条") && secondDetail.includes("fb-3") && secondDetail.includes("第二条");
    return {
      ok: !hiddenInline && drawer && basis && tbl && rows >= 2 && detailButtons >= 2 && detailsOk && refOnly,
      detail: `inlineHidden=${!hiddenInline} drawer=${drawer} basis=${basis} 表=${tbl} 行=${rows} 详情=${detailButtons}/2 detailOk=${detailsOk} refOnly=${refOnly}`,
    };
  } },
  { id: "optimization-execution", phase: "P3", desc: "优化执行阶段 A=方案正文、B=Diff/变更预览、E=执行记录，内容归属不交叉", async fn(page) {
    if (!(await openImprovementById(page, stageTarget("optimization", "imp-demo03")))) return { ok: false, detail: "无改进事项" };
    await page.getByTestId("optimization-plan").waitFor({ timeout: 6000 }).catch(() => {});
    const optCard = page.getByTestId("optimization-plan").first();
    const opt = await has(page, "optimization-plan");
    const optText = await optCard.innerText().catch(() => "");
    const optMisplacedChanges = optText.includes("变更项") || await optCard.getByTestId("diff-preview-changes").count() > 0;
    const diffPreview = await has(page, "diff-preview-changes");
    const legacyPlanChanges = await has(page, "optimization-plan-changes");
    const exec = await has(page, "execution-record");
    await page.getByTestId("stage-panel-diff-preview").getByRole("button", { name: "查看详情" }).click().catch(() => {});
    await page.getByTestId("stage-detail-drawer").waitFor({ timeout: 6000 }).catch(() => {});
    const fileDiffs = await has(page, "diff-preview-file-diffs");
    await page.getByTestId("diff-preview-file-unified-diff").waitFor({ timeout: 6000 }).catch(() => {});
    const unifiedDiffText = await page.getByTestId("diff-preview-file-unified-diff").innerText().catch(() => "");
    const unifiedDiffOk = unifiedDiffText.includes("+新增事件时间与告警时间一致性校验");
    await page.locator(".drawer-shell-actions").getByRole("button", { name: "关闭" }).first().click().catch(() => {});
    await page.getByTestId("stage-detail-drawer").waitFor({ state: "detached", timeout: 4000 }).catch(() => {});
    return {
      ok: opt && !optMisplacedChanges && diffPreview && fileDiffs && unifiedDiffOk && !legacyPlanChanges && exec,
      detail: `方案=${opt} A混入变更=${optMisplacedChanges} B变更预览=${diffPreview} 文件diff=${fileDiffs}/${unifiedDiffOk} legacyPlanChanges=${legacyPlanChanges} 执行记录=${exec}`,
    };
  } },
  { id: "optimization-action-semantics", phase: "P3", desc: "优化执行主动作=执行优化；重新生成优化方案只在决策卡出现，旧自动执行文案不回归", async fn(page) {
    if (!(await openImprovementById(page, stageTarget("optimizationPending", "imp-demo06")))) return { ok: false, detail: "无待执行优化事项" };
    await page.getByTestId("primary-action").waitFor({ timeout: 6000 }).catch(() => {});
    await page.waitForFunction(() => {
      const action = document.querySelector('[data-testid="primary-action"]');
      return !!action && action.textContent?.includes("执行优化");
    }, null, { timeout: 6000 }).catch(() => {});
    const primaryLabel = await page.getByTestId("primary-action").innerText().catch(() => "");
    const primaryAction = await page.getByTestId("primary-action").getAttribute("data-action").catch(() => "");
    const decisionText = await page.getByTestId("current-decision-card").innerText().catch(() => "");
    const stageText = await page.getByTestId("stage-work-area").innerText().catch(() => "");
    const decisionRegen = await page.getByTestId("decision-regenerate-optimization-plan").count();
    const stageRegen = await page.getByTestId("stage-work-area").getByTestId("regenerate-optimization-plan").count();
    const legacyAutoExecute = `${decisionText}\n${stageText}`.includes("自动执行优化");
    observedApiRequests.length = 0;
    await page.getByTestId("primary-action").click();
    const sawApply = await waitForObservedRequest((r) => r.path.endsWith("/execution/apply"));
    const reqs = observedApiRequests.map((r) => `${r.method} ${r.path}`);
    const confirmedPlan = reqs.some((r) => r.includes("/optimization-plan/confirm"));
    const planAlreadyConfirmed = decisionText.includes("方案已确认");
    const lifecycle = reqs.some((r) => r.includes("/lifecycle"));
    return {
      ok: primaryLabel.includes("执行优化") && primaryAction === "apply-execution" && decisionRegen === 1 && stageRegen === 0 && !legacyAutoExecute && sawApply && (confirmedPlan || planAlreadyConfirmed) && !lifecycle,
      detail: `primary=${primaryLabel}/${primaryAction} decisionRegen=${decisionRegen} stageRegen=${stageRegen} legacyAuto=${legacyAutoExecute} apply=${sawApply} confirmPlan=${confirmedPlan} planAlreadyConfirmed=${planAlreadyConfirmed} lifecycle=${lifecycle}`,
    };
  } },
  { id: "regression-governor", phase: "P3", desc: "治理 Agent 只产出 pytest 代码、测试意图和断言依据；确认后写入待发布版本且不自动运行", async fn(page) {
    if (!(await openImprovementById(page, stageTarget("testRelease", "imp-demo04")))) return { ok: false, detail: "无改进事项" };
    await page.getByTestId("regression-test-design").waitFor({ timeout: 6000 }).catch(() => {});
    const primary = await page.getByTestId("primary-action").innerText().catch(() => "");
    const duplicateGenerate = await page.getByTestId("stage-work-area").getByTestId("generate-regression").count();
    const design = await has(page, "regression-test-design");
    const files = await page.getByTestId("workspace-test-files").innerText().catch(() => "");
    const coverage = await has(page, "regression-test-code-coverage");
    const primaryOk = primary.includes("重新生成回归测试") && !primary.includes("运行测试");
    return {
      ok: primaryOk && duplicateGenerate === 0 && design && files.includes("3 个文件") && coverage,
      detail: "决策卡=" + primary + " duplicate_generate=" + duplicateGenerate + " design=" + design + " files=" + files.includes("3 个文件") + " coverage=" + coverage,
    };
  } },
  { id: "execution-version-binding", phase: "P3", desc: "执行记录标治理 Agent 应用来源；governor 成功时绑定待发布 Agent 版本/待发布变更", async fn(page) {
    // 执行来源徽标始终在；版本绑定仅在 governor 成功 apply 时出现（取决于 governor 判断/环境），不强制。
    if (!(await openImprovementById(page, stageTarget("optimization", "imp-demo03")))) return { ok: false, detail: "无改进事项" };
    await page.getByTestId("execution-record").waitFor({ timeout: 6000 }).catch(() => {});
    const src = await has(page, "execution-source");
    const srcVal = await page.getByTestId("execution-source").first().getAttribute("data-source").catch(() => "");
    const validSrc = srcVal === "governor" || srcVal === "heuristic";
    const binding = await has(page, "execution-version-binding");
    const bindingOk = srcVal === "governor" ? binding : true;
    return { ok: src && validSrc && bindingOk, detail: `执行来源徽标=${src}(${srcVal}) 版本绑定=${binding}` };
  } },
  { id: "governance-generation-source", phase: "P3", desc: "§17.5 归因/方案标注来源（治理 Agent 生成 vs 启发式初步）", async fn(page) {
    // 断言来源徽标存在且取值合法；governor/heuristic 取决于环境 LLM 可用性（代码两态都正确），不强制 governor。
    if (!(await openImprovementById(page, stageTarget("attribution", "imp-demo02")))) return { ok: false, detail: "无改进事项" };
    await page.getByTestId("attribution-source").waitFor({ timeout: 6000 }).catch(() => {});
    const attrSrc = await has(page, "attribution-source");
    const src = await page.getByTestId("attribution-source").first().getAttribute("data-source").catch(() => "");
    const validSrc = src === "governor" || src === "heuristic";
    if (!(await openImprovementById(page, stageTarget("optimization", "imp-demo03")))) return { ok: false, detail: "无优化事项" };
    const optSrc = await has(page, "optimization-plan-source");
    return { ok: attrSrc && optSrc && validSrc, detail: `归因来源徽标=${attrSrc}(${src}) 方案来源徽标=${optSrc}` };
  } },
  { id: "attribution-actions", phase: "P3", desc: "归因支持 修改/重新整理(§6 [确认][修改][重新整理])", async fn(page) {
    if (!(await openImprovementById(page, stageTarget("attribution", "imp-demo02")))) return { ok: false, detail: "无改进事项" };
    await page.getByTestId("attribution").waitFor({ timeout: 6000 }).catch(() => {});
    const edit = await has(page, "edit-attribution");
    const regen = await has(page, "regenerate-attribution");
    return { ok: edit && regen, detail: `修改=${edit} 重新整理=${regen}` };
  } },
  { id: "decision-card-product-action", phase: "P1", desc: "决策卡主按钮调用业务产物 API，前端不得用 /lifecycle 前推", async fn(page) {
    if (!(await openImprovementById(page, stageTarget("feedback", "imp-demo01")))) return { ok: false, detail: "无反馈整理事项" };
    await page.getByTestId("primary-action").waitFor({ timeout: 6000 }).catch(() => {});
    const feedbackLabel = await page.getByTestId("primary-action").innerText().catch(() => "");
    const feedbackAction = await page.getByTestId("primary-action").getAttribute("data-action").catch(() => "");
    observedApiRequests.length = 0;
    await page.getByTestId("primary-action").click();
    await waitForObservedRequest((r) => r.path.endsWith("/attribution/generate"));
    const feedbackReqs = observedApiRequests.map((r) => `${r.method} ${r.path}`);
    const feedbackGenerate = feedbackReqs.some((r) => r.includes("/attribution/generate"));
    const feedbackLifecycle = feedbackReqs.some((r) => r.includes("/lifecycle"));
    const feedbackConfirm = feedbackReqs.some((r) => r.includes("/normalized-feedback/confirm"));

    if (!(await openImprovementById(page, stageTarget("attribution", "imp-demo02")))) return { ok: false, detail: "无归因分析事项" };
    await page.getByTestId("primary-action").waitFor({ timeout: 6000 }).catch(() => {});
    const attributionLabel = await page.getByTestId("primary-action").innerText().catch(() => "");
    const attributionAction = await page.getByTestId("primary-action").getAttribute("data-action").catch(() => "");
    observedApiRequests.length = 0;
    await page.getByTestId("primary-action").click();
    await waitForObservedRequest((r) => r.path.endsWith("/optimization-plan/generate"));
    const attributionReqs = observedApiRequests.map((r) => `${r.method} ${r.path}`);
    const attributionGenerate = attributionReqs.some((r) => r.includes("/optimization-plan/generate"));
    const attributionLifecycle = attributionReqs.some((r) => r.includes("/lifecycle"));
    const attributionConfirm = attributionReqs.some((r) => r.includes("/attribution/confirm"));

    const ok = feedbackLabel.includes("生成归因分析")
      && feedbackAction === "generate-attribution"
      && feedbackGenerate
      && !feedbackLifecycle
      && feedbackConfirm
      && attributionLabel.includes("生成优化方案")
      && attributionAction === "generate-optimization-plan"
      && attributionGenerate
      && !attributionLifecycle
      && attributionConfirm;
    return {
      ok,
      detail: `feedback=${feedbackLabel}/${feedbackAction} gen=${feedbackGenerate} lifecycle=${feedbackLifecycle} confirm=${feedbackConfirm}; attribution=${attributionLabel}/${attributionAction} gen=${attributionGenerate} lifecycle=${attributionLifecycle} confirm=${attributionConfirm}`,
    };
  } },
  { id: "improvement-assets", phase: "P3", desc: "Workspace pytest 与通用治理资产分开呈现，测试文件不进入通用资产生命周期", async fn(page) {
    if (!(await openImprovementById(page, stageTarget("testRelease", "imp-demo04")))) return { ok: false, detail: "无改进事项" };
    await page.getByTestId("improvement-detail").waitFor({ timeout: 6000 }).catch(() => {});
    const design = await has(page, "regression-test-design");
    const filesText = await page.getByTestId("workspace-test-files").innerText().catch(() => "");
    const sediment = await has(page, "sediment-assets");
    const sedimentTypes = await page.getByTestId("sediment-asset-item").evaluateAll((items) => items.map((item) => item.getAttribute("data-asset-type") || ""));
    const legacyLifecycle = await page.locator('[data-testid="test-dataset-lifecycle-management"], [data-testid="adopt-regression"]').count();
    const oldRequests = observedApiRequests.filter((request) => request.path.includes("/api/test-datasets") || request.path.includes("/test-dataset/")).length;
    const ok = design
      && filesText.includes("3 个文件")
      && sediment
      && sedimentTypes.includes("methodology")
      && !sedimentTypes.includes("test_dataset")
      && legacyLifecycle === 0
      && oldRequests === 0;
    return {
      ok,
      detail: "design=" + design + " files=" + filesText.includes("3 个文件") + " sediment=" + sediment + " types=" + sedimentTypes.join(",") + " legacy=" + legacyLifecycle + " oldRequests=" + oldRequests,
    };
  } },
  { id: "asset-browse-first", phase: "P1", desc: "资产中心默认展示测试资产，治理资产保留浏览追溯与抽屉创建", async fn(page) {
    await page.getByTestId("nav-asset").click();
    await page.getByTestId("asset-registry").waitFor({ timeout: 8000 });
    const testAssets = await visible(page, "agent-test-assets");
    const testTabSelected = await page.getByTestId("asset-center-tab-tests").getAttribute("aria-selected");
    const governanceVisibleBefore = await visible(page, "governance-asset-registry");
    await page.getByTestId("asset-center-tab-governance").click();
    await page.getByTestId("governance-asset-registry").waitFor({ timeout: 8000 });
    const toolbar = await has(page, "asset-browser-toolbar");
    const typeFilter = await has(page, "asset-type-filter");
    const sourceFilter = await has(page, "asset-source-filter");
    const createButton = await has(page, "asset-create-open");
    const titleVisibleBefore = await visible(page, "asset-create-title");
    await page.getByTestId("asset-create-open").click();
    const drawer = await visible(page, "asset-create-drawer");
    const drawerSize = await page.getByTestId("asset-create-drawer").getAttribute("data-size").catch(() => "");
    await page.getByTestId("asset-create-drawer").getByLabel("关闭").click().catch(() => {});
    const ok = testAssets
      && testTabSelected === "true"
      && !governanceVisibleBefore
      && toolbar
      && typeFilter
      && sourceFilter
      && createButton
      && !titleVisibleBefore
      && drawer
      && drawerSize === "narrow";
    return { ok, detail: `tests=${testAssets}/${testTabSelected} governanceBefore=${governanceVisibleBefore} toolbar=${toolbar} type=${typeFilter} source=${sourceFilter} createBtn=${createButton} titleBefore=${titleVisibleBefore} drawer=${drawer}/${drawerSize}` };
  } },
  { id: "theme-governance-light", phase: "P4", desc: "主工作台统一 Governance Light（主区背景非旧暖色，含背景渐变）", async fn(page) {
    await page.getByTestId("nav-playground").click();
    // 旧暖色：body 用暖色渐变(#fbf7f0/#f4eee5)+ topbar 等用 rgb(255,250,243)；需检查 backgroundImage（渐变在 image 而非 color）。
    const bg = await page.evaluate(() => {
      const s = getComputedStyle(document.body);
      const t = document.querySelector(".topbar");
      const ts = t ? getComputedStyle(t).backgroundColor : "";
      return `${s.backgroundImage} | ${s.backgroundColor} | topbar:${ts}`;
    });
    const warm = /251,\s*247,\s*240|244,\s*238,\s*229|255,\s*250,\s*243|246,\s*240,\s*230/.test(bg);
    return { ok: !warm, detail: `${bg.slice(0, 90)}（不应含暖色调）` };
  } },
];
