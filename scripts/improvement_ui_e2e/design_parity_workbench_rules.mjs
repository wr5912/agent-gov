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
  { id: "release-merged-into-test-stage", phase: "P2", desc: "旧发布顶级入口消失，发布门禁预览合入测试发布阶段", async fn(page) {
    const releaseNav = await has(page, "nav-release");
    const opened = await openImprovementById(page, stageTarget("testRelease", "imp-demo04"));
    if (!opened) return { ok: false, detail: "无法打开测试发布阶段改进事项" };
    const stage = await page.getByTestId("stage-work-area").getAttribute("data-visible-stage").catch(() => "");
    const gate = await has(page, "stage-panel-release-gate");
    const primary = await page.getByTestId("primary-action").innerText().catch(() => "");
    const actionMatches = primary.includes("生成回归方案") && !primary.includes("执行回归测试");
    return { ok: !releaseNav && stage === "test_release" && gate && actionMatches, detail: `nav-release=${releaseNav} stage=${stage} gate=${gate} primary=${primary}` };
  } },
  { id: "test-release-stage-panels", phase: "P1", desc: "测试发布阶段含测试数据集、回归执行、测试用例详情、执行环境和门禁预览五个面板", async fn(page) {
    const opened = await openImprovementById(page, stageTarget("testRelease", "imp-demo04"));
    if (!opened) return { ok: false, detail: "无法打开测试发布阶段改进事项" };
    const panels = ["test-dataset-asset", "regression-guarantee", "stage-panel-coverage", "stage-panel-execution-baseline", "stage-panel-release-gate"];
    const found = [];
    for (const panel of panels) if (await has(page, panel)) found.push(panel);
    const datasetId = await page.getByTestId("test-dataset-id").innerText().catch(() => "");
    const runRef = await page.getByTestId("regression-run-dataset-ref").innerText().catch(() => "");
    const duplicateGenerate = await page.getByTestId("test-dataset-asset").getByTestId("generate-regression").count();
    const coverage = await has(page, "regression-case-coverage");
    const summaryItems = await page.getByTestId("regression-case-summary-item").count();
    await page.getByTestId("stage-panel-coverage").getByRole("button", { name: "查看详情" }).click();
    await page.getByTestId("stage-detail-drawer").waitFor({ timeout: 6000 }).catch(() => {});
    const detailItems = await page.getByTestId("regression-case-detail-item").count();
    const inputs = await page.getByTestId("regression-case-input").count();
    const expected = await page.getByTestId("regression-case-expected").count();
    const checkpoints = await page.getByTestId("regression-case-checkpoint").count();
    const inputText = await page.getByTestId("regression-case-input").first().innerText().catch(() => ""), toggleButtons = await page.getByTestId("regression-case-input-toggle").count(), previewText = await page.getByTestId("regression-case-input-text").first().innerText().catch(() => "");
    if (toggleButtons > 0) await page.getByTestId("regression-case-input-toggle").first().click();
    const expandedText = await page.getByTestId("regression-case-input-text").first().innerText().catch(() => ""), inputSemantics = inputText.includes("数据转换前原始数据") && !inputText.includes("复现场景：") && toggleButtons > 0 && expandedText.length > previewText.length;
    await page.locator(".drawer-shell-actions").getByRole("button", { name: "关闭" }).first().click().catch(() => {});
    await page.getByTestId("stage-detail-drawer").waitFor({ state: "detached", timeout: 4000 }).catch(() => {});
    return {
      ok: found.length === panels.length && datasetId && runRef === datasetId && duplicateGenerate === 0 && coverage && summaryItems >= 3 && detailItems >= 3 && inputs >= 3 && expected >= 3 && checkpoints >= 3 && inputSemantics,
      detail: `panels=${found.length}/${panels.length} dataset=${datasetId} regression_ref=${runRef} duplicate_generate=${duplicateGenerate} coverage=${coverage} summary=${summaryItems} detail=${detailItems} inputs=${inputs} expected=${expected} checkpoints=${checkpoints} inputSemantics=${inputSemantics}`,
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
      ["testRelease", "imp-demo04", "test_release", "test-dataset-asset"],
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
  { id: "regression-governor", phase: "P3", desc: "§11/§17.5 回归保障：生成/重新生成方案入口由决策卡承载，候选用例归属测试用例详情卡", async fn(page) {
    if (!(await openImprovementById(page, stageTarget("testRelease", "imp-demo04")))) return { ok: false, detail: "无改进事项" };
    await page.getByTestId("regression-guarantee").waitFor({ timeout: 6000 }).catch(() => {});
    const primary = await page.getByTestId("primary-action").innerText().catch(() => "");
    const duplicateGenerate = await page.getByTestId("test-dataset-asset").getByTestId("generate-regression").count();
    const sediment = await has(page, "sediment-assets");
    const coverage = await has(page, "regression-case-coverage");
    const primaryOk = primary.includes("重新生成回归方案") && !primary.includes("执行回归测试");
    return {
      ok: primaryOk && duplicateGenerate === 0 && (coverage || sediment),
      detail: `决策卡=${primary} duplicate_generate=${duplicateGenerate} 覆盖场景=${coverage} 沉淀=${sediment}`,
    };
  } },
  { id: "execution-version-binding", phase: "P3", desc: "§17.5 执行记录标治理 Agent 应用来源；governor 成功时绑定候选 Agent 版本/变更集", async fn(page) {
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
  { id: "improvement-assets", phase: "P3", desc: "改进详情含 typed TestDataset 快照、生命周期修订管理和本事项沉淀资产区", async fn(page) {
    if (!(await openImprovementById(page, stageTarget("testRelease", "imp-demo04")))) return { ok: false, detail: "无改进事项" };
    await page.getByTestId("improvement-detail").waitFor({ timeout: 6000 }).catch(() => {});
    // §11 能力存在的两种合法态：未采纳→候选卡(regression-guarantee+adopt)；已采纳→沉淀资产区(sediment-assets)。
    const rg = await has(page, "regression-guarantee");
    const adopt = await has(page, "adopt-regression");
    const sediment = await has(page, "sediment-assets");
    const lifecycle = await has(page, "test-dataset-lifecycle-management");
    observedApiRequests.length = 0;
    if (lifecycle) {
      await page.getByTestId("test-dataset-lifecycle-target").selectOption("deprecated");
      await page.getByTestId("test-dataset-lifecycle-reason").fill("设计基线生命周期验证");
      await page.getByTestId("test-dataset-lifecycle-submit").click();
      await waitForObservedRequest((request) => request.path === "/api/test-datasets/tds-demo04/lifecycle");
    }
    const lifecycleRequest = observedApiRequests.find((request) => request.path === "/api/test-datasets/tds-demo04/lifecycle");
    const lifecycleBody = JSON.parse(lifecycleRequest?.postData || "{}");
    const lifecycleBound = lifecycleRequest?.method === "POST"
      && lifecycleBody.target_state === "deprecated"
      && lifecycleBody.expected_revision === 1
      && lifecycleBody.operator === "ui"
      && lifecycleBody.reason === "设计基线生命周期验证";
    return { ok: ((rg && adopt) || sediment) && lifecycle && lifecycleBound, detail: `回归保障候选=${rg} 采纳=${adopt} 沉淀资产=${sediment} 生命周期=${lifecycle}/${lifecycleBound}` };
  } },
  { id: "asset-browse-first", phase: "P1", desc: "资产 Registry 默认浏览/追溯优先，创建资产进入抽屉", async fn(page) {
    await page.getByTestId("nav-asset").click();
    await page.getByTestId("asset-registry").waitFor({ timeout: 8000 });
    const toolbar = await has(page, "asset-browser-toolbar");
    const typeFilter = await has(page, "asset-type-filter");
    const sourceFilter = await has(page, "asset-source-filter");
    const createButton = await has(page, "asset-create-open");
    const titleVisibleBefore = await visible(page, "asset-create-title");
    await page.getByTestId("asset-create-open").click();
    const drawer = await visible(page, "asset-create-drawer");
    const drawerSize = await page.getByTestId("asset-create-drawer").getAttribute("data-size").catch(() => "");
    await page.getByTestId("asset-create-drawer").getByLabel("关闭").click().catch(() => {});
    return { ok: toolbar && typeFilter && sourceFilter && createButton && !titleVisibleBefore && drawer && drawerSize === "narrow", detail: `toolbar=${toolbar} type=${typeFilter} source=${sourceFilter} createBtn=${createButton} titleBefore=${titleVisibleBefore} drawer=${drawer}/${drawerSize}` };
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
