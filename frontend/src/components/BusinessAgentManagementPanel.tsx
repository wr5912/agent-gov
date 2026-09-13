import { FilePlus2, Upload } from "lucide-react";
import { useCallback, useEffect, useMemo, useRef, useState } from "react";
import {
  exportBusinessAgentWorkspace,
  getCurrentAgentRef,
  importBusinessAgentWorkspace,
  inspectAgentTestSuite,
  listAgentTestRuns,
} from "../api/runtime";
import type {
  AgentSummary,
  AgentChangeSet,
  AgentRelease,
  NativeAgentCandidateResponse,
  RuntimeClientConfig,
  WorkspaceImportResponse,
} from "../types/runtime";
import { AgentActionMenu } from "./AgentActionMenu";
import {
  AgentWorkspaceImportDrawer,
  type CandidateReceipt,
  WorkspaceOperationNotice,
  type WorkspacePackageNotice,
  type WorkspacePackageOperation,
} from "./AgentWorkspaceImportDrawer";
import { BusinessAgentTable, type AgentTestStatus } from "./BusinessAgentTable";
import { NativeAgentCandidateDrawer } from "./NativeAgentCandidateDrawer";
import { ReleaseWorkbench } from "./ReleaseWorkbench";
import { validateAgentId } from "./agentSettingsValidation";
import "./BusinessAgentManagementPanel.css";

interface BusinessAgentManagementPanelProps {
  config: RuntimeClientConfig;
  agents: AgentSummary[];
  changeSets: AgentChangeSet[];
  releases: AgentRelease[];
  loading: boolean;
  externalBusy: boolean;
  pending: string | null;
  reloadAgents: () => Promise<void>;
  onGovernanceRefresh: () => void | Promise<void>;
  onBusyChange: (busy: boolean) => void;
  onLifecycle: (agentId: string, status: string) => void;
  onOpenTestAssets: (agentId: string) => void;
  onDelete: (agentId: string) => void;
}

interface PackageRunner {
  pending: string | null;
  notice: WorkspacePackageNotice | null;
  clearFeedback: () => void;
  fail: (operation: WorkspacePackageOperation, message: string) => void;
  run: (key: string, action: () => Promise<string | undefined>) => void;
}

type ImportDrawerState =
  | { mode: "create" }
  | { mode: "overwrite"; targetAgent: AgentSummary };

type NativeDrawerState =
  | { mode: "create" }
  | { mode: "configure"; targetAgent: AgentSummary };

interface MenuAnchor {
  agent: AgentSummary;
  element: HTMLButtonElement;
}

interface PreparedWorkspaceImport {
  overwrite: boolean;
  targetId: string;
  packageFile: File;
}

function usePackageRunner(onBusyChange: (busy: boolean) => void): PackageRunner {
  const [pending, setPending] = useState<string | null>(null);
  const [notice, setNotice] = useState<WorkspacePackageNotice | null>(null);

  useEffect(() => {
    onBusyChange(pending !== null);
    return () => onBusyChange(false);
  }, [onBusyChange, pending]);

  const clearFeedback = useCallback(() => setNotice(null), []);
  const fail = useCallback((operation: WorkspacePackageOperation, message: string) => {
    setNotice({ operation, kind: "error", message });
  }, []);
  const run = useCallback((key: string, action: () => Promise<string | undefined>) => {
    const operation = key.split(":", 1)[0] as WorkspacePackageOperation;
    setPending(key);
    setNotice(null);
    void action()
      .then((message) => {
        if (message) setNotice({ operation, kind: "success", message });
      })
      .catch((error) => {
        setNotice({
          operation,
          kind: "error",
          message: error instanceof Error ? error.message : String(error),
        });
      })
      .finally(() => setPending(null));
  }, []);

  return { pending, notice, clearFeedback, fail, run };
}

function useAgentTestStatuses(config: RuntimeClientConfig, agents: AgentSummary[]) {
  const [statuses, setStatuses] = useState<Record<string, AgentTestStatus>>({});
  const { apiBase, apiKey } = config;

  useEffect(() => {
    let cancelled = false;
    setStatuses({});
    const requestConfig = { apiBase, apiKey };
    void Promise.all(agents.map(async (agent) => {
      try {
        const [suite, runs] = await Promise.all([
          inspectAgentTestSuite(requestConfig, agent.agent_id),
          listAgentTestRuns(requestConfig, { agentId: agent.agent_id, limit: 1 }),
        ]);
        return [agent.agent_id, { suite, latestRun: runs[0] }] as const;
      } catch (error) {
        return [agent.agent_id, { error: error instanceof Error ? error.message : String(error) }] as const;
      }
    })).then((entries) => {
      if (!cancelled) setStatuses(Object.fromEntries(entries));
    });
    return () => { cancelled = true; };
  }, [agents, apiBase, apiKey]);

  return statuses;
}

function prepareWorkspaceImport(
  drawer: ImportDrawerState,
  agents: AgentSummary[],
  agentId: string,
  name: string,
  file: File | null,
  fail: (operation: WorkspacePackageOperation, message: string) => void,
): PreparedWorkspaceImport | null {
  const overwrite = drawer.mode === "overwrite";
  const targetId = overwrite ? drawer.targetAgent.agent_id : agentId.trim();
  const idError = validateAgentId(targetId);
  const existing = agents.find((agent) => agent.agent_id === targetId);
  if (!targetId || idError) fail("import", idError || "请输入 Agent ID。");
  else if (!file) fail("import", "请选择 .tar.gz Workspace 包。");
  else if (!overwrite && existing) fail("import", `Agent ID ${targetId} 已存在，请从该 Agent 的操作菜单选择“覆盖导入”。`);
  else if (!overwrite && !name.trim()) fail("import", "创建业务 Agent 时必须填写名称。");
  else if (overwrite && !existing) fail("import", `业务 Agent ${targetId} 已不存在，请刷新后重试。`);
  else if (overwrite && !window.confirm(`确认使用导入包为 ${drawer.targetAgent.name}（${targetId}）创建覆盖候选？活动版本和已有 Session 不会改变。`)) return null;
  else return { overwrite, targetId, packageFile: file };
  return null;
}

function useWorkspaceImport(
  props: BusinessAgentManagementPanelProps,
  runner: PackageRunner,
  onCandidateSaved: (receipt: WorkspaceImportResponse) => void,
) {
  const [agentId, setAgentId] = useState("");
  const [name, setName] = useState("");
  const [file, setFile] = useState<File | null>(null);
  const [lastImport, setLastImport] = useState<WorkspaceImportResponse | null>(null);
  const fileInput = useRef<HTMLInputElement | null>(null);

  const clearSelectedPackage = useCallback(() => {
    setFile(null);
    if (fileInput.current) fileInput.current.value = "";
  }, []);

  const reset = useCallback((target?: AgentSummary) => {
    setAgentId(target?.agent_id ?? "");
    setName(target?.name ?? "");
    setLastImport(null);
    clearSelectedPackage();
    runner.clearFeedback();
  }, [clearSelectedPackage, runner.clearFeedback]);

  const changeAgentId = (value: string) => {
    if (value !== agentId) {
      clearSelectedPackage();
      setLastImport(null);
      runner.clearFeedback();
    }
    setAgentId(value);
  };

  const selectFile = (nextFile: File | null) => {
    setFile(nextFile);
    setLastImport(null);
    runner.clearFeedback();
  };

  const submit = (drawer: ImportDrawerState) => {
    const prepared = prepareWorkspaceImport(drawer, props.agents, agentId, name, file, runner.fail);
    if (!prepared) return;
    runner.run(`import:${prepared.targetId}`, async () => {
      const current = prepared.overwrite ? await getCurrentAgentRef(props.config, prepared.targetId) : null;
      const result = await importBusinessAgentWorkspace(props.config, prepared.targetId, {
        package: prepared.packageFile,
        name: prepared.overwrite ? undefined : name.trim(),
        expectedCurrentCommitSha: current?.commit_sha || current?.agent_version_id || undefined,
        reason: prepared.overwrite ? "Settings 覆盖导入 Workspace 包" : "Settings 导入 Workspace 包创建业务 Agent",
      });
      setLastImport(result);
      onCandidateSaved(result);
      clearSelectedPackage();
      await Promise.all([props.reloadAgents(), props.onGovernanceRefresh()]);
      return importSuccessMessage(result);
    });
  };

  return {
    agentId,
    name,
    file,
    lastImport,
    fileInput,
    setName,
    changeAgentId,
    selectFile,
    reset,
    submit,
  };
}

function importSuccessMessage(result: WorkspaceImportResponse): string {
  if (result.action === "unchanged") return `${result.agent.name} 候选与导入包一致，仍未发布`;
  return `已保存 ${result.agent.name} 的 Workspace 候选，尚未发布`;
}

function exportWorkspace(
  props: BusinessAgentManagementPanelProps,
  runner: PackageRunner,
  agentId: string,
) {
  runner.run(`export:${agentId}`, async () => {
    const exported = await exportBusinessAgentWorkspace(props.config, agentId);
    const url = URL.createObjectURL(exported.blob);
    try {
      const anchor = document.createElement("a");
      anchor.href = url;
      anchor.download = exported.filename;
      anchor.click();
    } finally {
      URL.revokeObjectURL(url);
    }
    return `已导出 ${agentId} Workspace（commit ${exported.commitSha.slice(0, 12)}）`;
  });
}

function useManagementSurface(
  props: BusinessAgentManagementPanelProps,
  runner: PackageRunner,
  form: ReturnType<typeof useWorkspaceImport>,
) {
  const [drawer, setDrawer] = useState<ImportDrawerState | null>(null);
  const [nativeDrawer, setNativeDrawer] = useState<NativeDrawerState | null>(null);
  const [menuAnchor, setMenuAnchor] = useState<MenuAnchor | null>(null);
  const disabled = props.externalBusy || runner.pending !== null;
  useEffect(() => {
    if (disabled) setMenuAnchor(null);
  }, [disabled]);
  const openCreateDrawer = () => {
    form.reset();
    setMenuAnchor(null);
    setDrawer({ mode: "create" });
  };
  const openNativeCreateDrawer = () => {
    setMenuAnchor(null);
    setDrawer(null);
    setNativeDrawer({ mode: "create" });
  };
  const openNativeConfigureDrawer = (agent: AgentSummary) => {
    setMenuAnchor(null);
    setDrawer(null);
    setNativeDrawer({ mode: "configure", targetAgent: agent });
  };
  const openOverwriteDrawer = (agent: AgentSummary) => {
    form.reset(agent);
    setMenuAnchor(null);
    setDrawer({ mode: "overwrite", targetAgent: agent });
  };
  const closeDrawer = () => {
    if (runner.pending) return;
    setDrawer(null);
    form.reset();
  };
  const closeNativeDrawer = () => setNativeDrawer(null);
  return {
    drawer,
    nativeDrawer,
    menuAnchor,
    disabled,
    setMenuAnchor,
    openCreateDrawer,
    openNativeCreateDrawer,
    openNativeConfigureDrawer,
    openOverwriteDrawer,
    closeDrawer,
    closeNativeDrawer,
  };
}

export function BusinessAgentManagementPanel(props: BusinessAgentManagementPanelProps) {
  const runner = usePackageRunner(props.onBusyChange);
  const [governanceAgentId, setGovernanceAgentId] = useState("");
  const [preferredChangeSetId, setPreferredChangeSetId] = useState<string>();
  const [lastCandidateReceipt, setLastCandidateReceipt] = useState<CandidateReceipt | null>(null);
  const selectCandidate = useCallback((receipt: CandidateReceipt) => {
    setLastCandidateReceipt(receipt);
    setGovernanceAgentId(receipt.agent.agent_id);
    setPreferredChangeSetId(receipt.change_set_id);
  }, []);
  const form = useWorkspaceImport(props, runner, selectCandidate);
  const statuses = useAgentTestStatuses(props.config, props.agents);
  const surface = useManagementSurface(props, runner, form);
  const { drawer, menuAnchor } = surface;
  const candidateSaved = async (receipt: NativeAgentCandidateResponse) => {
    selectCandidate(receipt);
    await Promise.all([props.reloadAgents(), props.onGovernanceRefresh()]);
  };
  const openChangeSets = useMemo(
    () => props.changeSets.filter((changeSet) => !["published", "abandoned", "rejected", "failed"].includes(changeSet.status)),
    [props.changeSets],
  );
  const governanceAgents = useMemo(() => {
    const eligibleIds = new Set(openChangeSets.map((changeSet) => changeSet.agent_id));
    const available = props.agents.filter((agent) => eligibleIds.has(agent.agent_id) || agent.status === "draft");
    if (lastCandidateReceipt && !available.some((agent) => agent.agent_id === lastCandidateReceipt.agent.agent_id)) {
      return [...available, lastCandidateReceipt.agent];
    }
    return available;
  }, [lastCandidateReceipt, openChangeSets, props.agents]);

  useEffect(() => {
    setGovernanceAgentId((current) => {
      if (current && governanceAgents.some((agent) => agent.agent_id === current)) return current;
      return governanceAgents[0]?.agent_id || "";
    });
  }, [governanceAgents]);

  const refreshGovernance = async () => {
    await Promise.all([props.reloadAgents(), props.onGovernanceRefresh()]);
  };
  return (
    <section className="settings-agent-management" data-testid="settings-agent-management">
      <div className="settings-agent-management-toolbar">
        <span>{props.loading ? "正在加载…" : `${props.agents.length} 个业务 Agent`}</span>
        <div className="settings-agent-management-toolbar-actions">
          <button
            className="primary-button"
            type="button"
            data-testid="settings-native-agent-open"
            disabled={surface.disabled}
            onClick={surface.openNativeCreateDrawer}
          >
            <FilePlus2 size={15} />表单创建
          </button>
          <button
            className="secondary-button"
            type="button"
            data-testid="settings-agent-import-open"
            disabled={surface.disabled}
            onClick={surface.openCreateDrawer}
          >
            <Upload size={15} />导入 Workspace 包
          </button>
        </div>
      </div>

      {runner.notice?.operation === "export" ? <WorkspaceOperationNotice notice={runner.notice} /> : null}

      <BusinessAgentTable
        agents={props.agents}
        loading={props.loading}
        statuses={statuses}
        disabled={surface.disabled}
        pending={props.pending}
        packagePending={runner.pending}
        openMenuAgentId={menuAnchor?.agent.agent_id}
        onLifecycle={props.onLifecycle}
        onOpenCandidateGovernance={(agentId) => {
          setGovernanceAgentId(agentId);
          setPreferredChangeSetId(
            openChangeSets.find((changeSet) => changeSet.agent_id === agentId)?.change_set_id,
          );
          setLastCandidateReceipt(null);
          window.requestAnimationFrame(() => {
            document.querySelector('[data-testid="settings-candidate-governance"]')?.scrollIntoView({ block: "start" });
          });
        }}
        onOpenTestAssets={props.onOpenTestAssets}
        onToggleMenu={(agent, element) => {
          surface.setMenuAnchor(menuAnchor?.agent.agent_id === agent.agent_id ? null : { agent, element });
        }}
      />

      {menuAnchor ? (
        <AgentActionMenu
          anchor={menuAnchor.element}
          agent={menuAnchor.agent}
          disabled={surface.disabled}
          onClose={() => surface.setMenuAnchor(null)}
          onExport={() => {
            surface.setMenuAnchor(null);
            exportWorkspace(props, runner, menuAnchor.agent.agent_id);
          }}
          onOverwrite={() => surface.openOverwriteDrawer(menuAnchor.agent)}
          onConfigure={() => surface.openNativeConfigureDrawer(menuAnchor.agent)}
          onDelete={() => {
            const agentId = menuAnchor.agent.agent_id;
            surface.setMenuAnchor(null);
            props.onDelete(agentId);
          }}
        />
      ) : null}

      {drawer ? (
        <AgentWorkspaceImportDrawer
          mode={drawer.mode}
          targetAgent={drawer.mode === "overwrite" ? drawer.targetAgent : undefined}
          agentId={form.agentId}
          name={form.name}
          file={form.file}
          fileInputRef={form.fileInput}
          receipt={form.lastImport}
          notice={runner.notice?.operation === "export" ? null : runner.notice}
          pending={runner.pending}
          onAgentIdChange={form.changeAgentId}
          onNameChange={form.setName}
          onFileChange={form.selectFile}
          onSubmit={() => form.submit(drawer)}
          onOpenGovernance={(receipt) => {
            selectCandidate(receipt);
            surface.closeDrawer();
          }}
          onClose={surface.closeDrawer}
        />
      ) : null}
      {surface.nativeDrawer ? (
        <NativeAgentCandidateDrawer
          config={props.config}
          existingAgents={props.agents}
          targetAgent={surface.nativeDrawer.mode === "configure" ? surface.nativeDrawer.targetAgent : undefined}
          onSaved={(receipt) => { void candidateSaved(receipt); }}
          onOpenGovernance={(receipt) => {
            selectCandidate(receipt);
            surface.closeNativeDrawer();
          }}
          onClose={surface.closeNativeDrawer}
        />
      ) : null}

      <section className="settings-candidate-governance" data-testid="settings-candidate-governance">
        <div className="settings-candidate-governance-head">
          <div>
            <strong>候选治理</strong>
            <span>查看真实 Diff，运行候选 commit 的 Workspace suite，按需审批后再发布。</span>
          </div>
          {governanceAgents.length ? (
            <label>
              <span>业务 Agent</span>
              <select
                data-testid="settings-candidate-agent-select"
                value={governanceAgentId}
                onChange={(event) => {
                  setGovernanceAgentId(event.target.value);
                  setPreferredChangeSetId(undefined);
                  setLastCandidateReceipt(null);
                }}
              >
                {governanceAgents.map((agent) => (
                  <option key={agent.agent_id} value={agent.agent_id}>{agent.name} · {agent.agent_id}</option>
                ))}
              </select>
            </label>
          ) : null}
        </div>
        {lastCandidateReceipt ? (
          <div className="settings-candidate-selected" data-testid="settings-candidate-selected" role="status">
            已选中候选 <code>{lastCandidateReceipt.change_set_id}</code>，尚未发布。
          </div>
        ) : null}
        {governanceAgentId ? (
          <ReleaseWorkbench
            clientConfig={props.config}
            scopeAgentId={governanceAgentId}
            preferredChangeSetId={preferredChangeSetId}
            releases={props.releases}
            changeSets={props.changeSets}
            onRefresh={refreshGovernance}
          />
        ) : (
          <div className="empty-state" data-testid="settings-candidate-governance-empty">
            当前没有未完成候选。通过原生表单或 Workspace 包保存候选后，可在此测试、审批与发布。
          </div>
        )}
      </section>
    </section>
  );
}
