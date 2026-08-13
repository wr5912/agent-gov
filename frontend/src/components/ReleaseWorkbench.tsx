import "../improvement-workbench.css";
import {
  ReleaseGatePanel,
  ReleaseTestRunPanel,
  ReleaseTestSuitePanel,
  ReleaseVersionPanel,
} from "./ReleaseWorkbenchPanels";
import {
  type ReleaseWorkbenchProps,
  useReleaseWorkbenchController,
} from "./releaseWorkbenchController";

export function ReleaseWorkbench(props: ReleaseWorkbenchProps) {
  const controller = useReleaseWorkbenchController(props);
  return (
    <section className="release-stage-workbench" data-testid="release-workbench">
      <header className="iw-stage-toolbar">
        <span>测试与发布 · {controller.scopeAgentId}</span>
        <button className="iw-secondary-button" type="button" onClick={controller.actions.refresh}>刷新</button>
      </header>
      {controller.actionError ? (
        <div className="iw-error" data-testid="release-action-error">{controller.actionError}</div>
      ) : null}
      {controller.actionMessage ? (
        <div className="iw-next-step" data-testid="release-action-message">{controller.actionMessage}</div>
      ) : null}
      <ReleaseGatePanel controller={controller} />
      <ReleaseTestSuitePanel suite={controller.suite} />
      <ReleaseTestRunPanel controller={controller} />
      <ReleaseVersionPanel controller={controller} />
    </section>
  );
}
