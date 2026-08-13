import type { AgentRelease, AgentTestRun, AgentTestSuite } from "../types/runtime";
import type {
  ReleaseGateState,
  ReleaseWorkbenchController,
} from "./releaseWorkbenchController";

const GATE_TEXT: Record<ReleaseGateState, string> = {
  pass: "通过",
  fail: "未通过",
  pending: "未完成",
  not_applicable: "不适用",
};

const TEST_STATUS_TEXT: Record<string, string> = {
  queued: "排队中",
  running: "运行中",
  passed: "通过",
  failed: "未通过",
  error: "执行错误",
  cancelled: "已取消",
  interrupted: "服务重启中断",
};

export function ReleaseGatePanel({ controller }: { controller: ReleaseWorkbenchController }) {
  const { actions, activeTestRun, busyAction, gateLabel, gates, pendingChangeSets } = controller;
  const selected = controller.selectedChangeSet;
  return (
    <div className="release-stage-band" data-testid="release-gate-workbench">
      <div className="release-stage-heading">
        <h4>发布条件</h4>
        <span className={`iw-stage-pill ${gateLabel === "可发布" ? "is-done" : ""}`} data-testid="release-gate">
          {gateLabel}
        </span>
        <span>{String(selected?.publication_blocker || "待发布版本与平台测试记录将按 commit 精确绑定。")}</span>
      </div>
      {pendingChangeSets.length > 1 ? (
        <select
          className="iw-select select-inline"
          data-testid="release-changeset-select"
          value={selected?.change_set_id || ""}
          onChange={(event) => actions.selectChangeSet(event.target.value)}
        >
          {pendingChangeSets.map((changeSet) => (
            <option key={changeSet.change_set_id} value={changeSet.change_set_id}>
              {changeSet.title || changeSet.change_set_id} · {changeSet.status}
            </option>
          ))}
        </select>
      ) : null}
      <div className="iw-source-refs">
        {gates.map((gate) => (
          <span key={gate.id} className="iw-stage-pill" data-testid={`release-gate-${gate.id}`} data-state={gate.state}>
            {gate.label}：{GATE_TEXT[gate.state]}
          </span>
        ))}
      </div>
      {!controller.readOnly ? (
        <ReleaseGateActions
          controller={controller}
          activeTestRun={activeTestRun}
          busyAction={busyAction}
        />
      ) : null}
    </div>
  );
}

function ReleaseGateActions({
  controller,
  activeTestRun,
  busyAction,
}: {
  controller: ReleaseWorkbenchController;
  activeTestRun: AgentTestRun | null;
  busyAction?: string;
}) {
  const { actions, currentTestRun, readyToPublish, retryPublishAvailable, selectedChangeSet, showChanges, suite } = controller;
  return (
    <div className="iw-action-row">
      <button
        className="iw-primary-button"
        type="button"
        data-testid="release-action-run-tests"
        disabled={!selectedChangeSet?.candidate_commit_sha || !suite?.test_file_count || Boolean(activeTestRun) || Boolean(busyAction)}
        onClick={actions.runTests}
      >
        {busyAction === "tests" ? "提交中..." : currentTestRun ? "重新运行测试" : "运行测试"}
      </button>
      <button className="iw-secondary-button" type="button" data-testid="release-action-cancel-tests" disabled={!activeTestRun || Boolean(busyAction)} onClick={actions.cancelTests}>
        取消测试
      </button>
      <button className="iw-secondary-button" type="button" data-testid="release-action-view-changes" onClick={actions.toggleChanges}>
        {showChanges ? "收起变更" : "展开变更"}
      </button>
      <button className={readyToPublish ? "iw-primary-button" : "iw-secondary-button"} type="button" data-testid="release-action-publish" disabled={!readyToPublish || Boolean(busyAction)} onClick={actions.publish}>
        {busyAction === "publish" ? "发布中..." : "发布"}
      </button>
      <button className="iw-secondary-button" type="button" data-testid="release-action-retry" disabled={!retryPublishAvailable || Boolean(busyAction)} onClick={actions.retryPublish}>
        {busyAction === "retry-publish" ? "重试中..." : "重试发布"}
      </button>
    </div>
  );
}

export function ReleaseTestSuitePanel({ suite }: { suite: AgentTestSuite | null }) {
  return (
    <div className="release-stage-band" data-testid="release-test-suite">
      <div className="release-stage-heading">
        <h4>Workspace 测试资产</h4>
        <span className="iw-stage-pill" data-state={suite?.test_file_count ? "pass" : "pending"}>
          {suite ? `${suite.test_file_count} 个测试文件` : "未加载"}
        </span>
        <span>{suite?.suite_digest ? `suite ${suite.suite_digest.slice(0, 12)}` : "tests/ 是唯一测试资产来源"}</span>
      </div>
      {suite?.test_files?.length ? (
        <div className="iw-source-refs">
          {suite.test_files.map((path) => <code key={path}>{path}</code>)}
        </div>
      ) : <div className="iw-empty">待发布版本未提供可运行的 tests/test_*.py。</div>}
      {(suite?.diagnostics ?? []).map((diagnostic) => (
        <div className={diagnostic.level === "error" ? "iw-error" : "iw-next-step"} key={`${diagnostic.code}-${diagnostic.path || ""}`}>
          {diagnostic.code} · {diagnostic.message}
        </div>
      ))}
    </div>
  );
}

export function ReleaseTestRunPanel({ controller }: { controller: ReleaseWorkbenchController }) {
  const run = controller.currentTestRun;
  const eligible = controller.currentRunIsReleaseEligible;
  return (
    <div className="release-stage-band" data-testid="release-test-run-details">
      <div className="release-stage-heading">
        <h4>平台测试运行</h4>
        <span className="iw-stage-pill" data-state={eligible ? "pass" : run ? "pending" : "not_applicable"}>
          {testRunStatusText(run, eligible)}
        </span>
        <span>{run?.test_run_id || "只认可当前待发布 commit 的运行记录"}</span>
      </div>
      {run ? (
        <TestRunDetails
          run={run}
          eligible={eligible}
          showOutput={controller.showTestOutput}
          onToggleOutput={controller.actions.toggleTestOutput}
        />
      ) : null}
    </div>
  );
}

function testRunStatusText(run: AgentTestRun | null, eligible: boolean): string {
  if (!run) return "尚未运行";
  if (run.status === "passed" && !eligible) return "执行通过，回执未获发布资格";
  return TEST_STATUS_TEXT[run.status] || run.status;
}

function TestRunDetails({
  run,
  eligible,
  showOutput,
  onToggleOutput,
}: {
  run: AgentTestRun;
  eligible: boolean;
  showOutput: boolean;
  onToggleOutput: () => void;
}) {
  return (
    <div className="release-candidate-detail">
      <span>commit：{run.commit_sha}</span>
      <span>命令：{(run.command ?? []).join(" ")}</span>
      <span data-testid="release-test-eligibility">
        发布资格：{eligible ? "后端已确认" : "未获后端门禁确认"}
      </span>
      <span data-testid="release-test-receipt">回执可信状态：{testReceiptText(run, eligible)}</span>
      <span>开始：{run.started_at || "排队中"}</span>
      <span>完成：{run.completed_at || "-"}</span>
      <button className="iw-secondary-button" type="button" data-testid="release-action-view-test-output" onClick={onToggleOutput}>
        {showOutput ? "收起输出" : "查看输出"}
      </button>
      {showOutput ? <TestRunOutput run={run} /> : null}
    </div>
  );
}

function testReceiptText(run: AgentTestRun, eligible: boolean): string {
  const digest = run.receipt?.receipt_digest;
  if (eligible) return `后端已确认 · ${digest?.slice(0, 12) || "digest 未返回"}`;
  return digest ? `已记录但未获发布资格 · ${digest.slice(0, 12)}` : "无可信回执";
}

function TestRunOutput({ run }: { run: AgentTestRun }) {
  return (
    <>
      <pre className="iw-context-body release-diff-summary" data-testid="release-test-output">
        {[run.stdout, run.stderr, run.error?.message].filter(Boolean).join("\n") || "暂无输出。"}
      </pre>
      {(run.invocations ?? []).map((invocation, index) => {
        const traceUrl = typeof invocation.langfuse_trace_url === "string" ? invocation.langfuse_trace_url : "";
        const runId = typeof invocation.run_id === "string" ? invocation.run_id : `调用 ${index + 1}`;
        return (
          <span className="iw-next-step" data-testid="release-test-trace" key={`${runId}-${index}`}>
            Trace：{traceUrl ? <a href={traceUrl} target="_blank" rel="noreferrer">{runId}</a> : runId}
          </span>
        );
      })}
    </>
  );
}

export function ReleaseVersionPanel({ controller }: { controller: ReleaseWorkbenchController }) {
  const selected = controller.selectedChangeSet;
  return (
    <div className="release-stage-band" data-testid="release-changeset-details">
      <h4>待发布版本与发布记录</h4>
      {selected ? (
        <div className="release-candidate-detail">
          <strong>{selected.title || selected.change_set_id}</strong>
          <span>状态：{selected.status}</span>
          <span>修复前版本：{selected.base_commit_sha}</span>
          <span>待发布版本：{selected.candidate_commit_sha || "-"}</span>
          <span>阻塞项：{String(selected.publication_blocker || "无")}</span>
          <span>发布错误：{selected.publication_error?.detail || "无"}</span>
          {controller.showChanges ? (
            <pre className="iw-context-body release-diff-summary" data-testid="release-diff-summary">
              {JSON.stringify(selected.diff_summary || {}, null, 2)}
            </pre>
          ) : null}
        </div>
      ) : <div className="iw-empty">当前事项尚无待发布版本。</div>}
      {controller.scopedReleases.map((release) => <ReleaseRecord key={release.release_id} release={release} />)}
      {controller.cleanupTargets.map((changeSet) => (
        <div className="iw-list-item" data-testid="release-cleanup-pending" key={changeSet.change_set_id}>
          <span className="iw-list-item-title">工作目录清理待恢复 · {changeSet.title || changeSet.change_set_id}</span>
          {!controller.readOnly ? (
            <button className="iw-secondary-button" type="button" data-testid="release-action-retry-cleanup" disabled={Boolean(controller.busyAction)} onClick={() => controller.actions.retryCleanup(changeSet.change_set_id)}>
              {controller.busyAction === `cleanup-${changeSet.change_set_id}` ? "清理中..." : "重试清理"}
            </button>
          ) : null}
        </div>
      ))}
    </div>
  );
}

function ReleaseRecord({ release }: { release: AgentRelease }) {
  return (
    <div className="iw-list-item" data-testid="release-item" data-status={release.status}>
      <span className="iw-list-item-title">{release.tag_name || release.release_id}</span>
      <span className="iw-list-item-meta">{release.status} · {release.commit_sha.slice(0, 12)} · {release.created_at}</span>
      <span data-testid="release-test-evidence">
        发布门证：{release.test_run_id && release.test_receipt_digest
          ? `${release.test_run_id} · receipt ${release.test_receipt_digest.slice(0, 12)}`
          : "历史记录未固化"}
      </span>
      {release.force_published ? <ForcePublicationNotice release={release} /> : null}
    </div>
  );
}

function ForcePublicationNotice({ release }: { release: AgentRelease }) {
  return release.force_publication_blocker ? (
    <span className="iw-error" data-testid="release-force-warning">
      历史旧策略记录：该发布曾绕过当时阻断项 · 阻断项：{release.force_publication_blocker}
      {" · "}当前策略不可绕过发布条件
      {" · "}原因：{release.force_publish_reason || "未记录"}
      {" · "}操作人：{typeof release.operator === "string" ? release.operator : "未记录"}
    </span>
  ) : (
    <span className="iw-next-step" data-testid="release-force-warning">
      管理员加急发布 · 发布条件已满足；force 仅记录加急与审批审计，不可绕过发布条件
      {" · "}原因：{release.force_publish_reason || "未记录"}
      {" · "}操作人：{typeof release.operator === "string" ? release.operator : "未记录"}
    </span>
  );
}
