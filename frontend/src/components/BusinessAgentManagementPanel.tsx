import { Upload } from "lucide-react";
import { useCallback, useEffect, useRef, useState } from "react";
import {
  exportBusinessAgentWorkspace,
  getCurrentAgentRef,
  importBusinessAgentWorkspace,
  inspectAgentTestSuite,
  listAgentTestRuns,
  restoreBusinessAgentWorkspace,
} from "../api/runtime";
import type {
  AgentSummary,
  RuntimeClientConfig,
  WorkspaceImportResponse,
} from "../types/runtime";
import { AgentActionMenu } from "./AgentActionMenu";
import {
  AgentWorkspaceImportDrawer,
  WorkspaceOperationNotice,
  type WorkspacePackageNotice,
  type WorkspacePackageOperation,
} from "./AgentWorkspaceImportDrawer";
import { BusinessAgentTable, type AgentTestStatus } from "./BusinessAgentTable";
import { validateAgentId } from "./agentSettingsValidation";
import "./BusinessAgentManagementPanel.css";

interface BusinessAgentManagementPanelProps {
  config: RuntimeClientConfig;
  agents: AgentSummary[];
  loading: boolean;
  externalBusy: boolean;
  pending: string | null;
  reloadAgents: () => Promise<void>;
  onAgentsChanged: () => void;
  onBusyChange: (busy: boolean) => void;
  onLifecycle: (agentId: string, status: string) => void;
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
  else if (overwrite && !window.confirm(`确认原样覆盖 ${drawer.targetAgent.name}（${targetId}）Workspace？变更将在下一 turn 生效。`)) return null;
  else return { overwrite, targetId, packageFile: file };
  return null;
}

function useWorkspaceImport(
  props: BusinessAgentManagementPanelProps,
  runner: PackageRunner,
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
      clearSelectedPackage();
      await props.reloadAgents();
      props.onAgentsChanged();
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
    setLastImport,
    changeAgentId,
    selectFile,
    reset,
    submit,
  };
}

function importSuccessMessage(result: WorkspaceImportResponse): string {
  if (result.action === "created") return `已从 Workspace 包创建 ${result.agent.name}（下一 turn 生效）`;
  if (result.action === "unchanged") return `${result.agent.name} Workspace 与导入包一致，无需变更`;
  return `已覆盖 ${result.agent.name} Workspace（下一 turn 生效）`;
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
  const restore = () => {
    if (!form.lastImport?.rollback_target_commit_sha) return;
    const receipt = form.lastImport;
    const agentId = receipt.agent.agent_id;
    runner.run(`restore:${agentId}`, async () => {
      const restored = await restoreBusinessAgentWorkspace(props.config, agentId, {
        target_commit_sha: receipt.rollback_target_commit_sha!,
        expected_current_commit_sha: receipt.current_commit_sha,
        reason: "Settings 恢复导入前 Workspace",
      });
      form.setLastImport(null);
      await props.reloadAgents();
      props.onAgentsChanged();
      return `已恢复 ${agentId} 导入前 Workspace（新 commit ${restored.current_commit_sha.slice(0, 12)}，下一 turn 生效）`;
    });
  };
  return { drawer, menuAnchor, disabled, setMenuAnchor, openCreateDrawer, openOverwriteDrawer, closeDrawer, restore };
}

export function BusinessAgentManagementPanel(props: BusinessAgentManagementPanelProps) {
  const runner = usePackageRunner(props.onBusyChange);
  const form = useWorkspaceImport(props, runner);
  const statuses = useAgentTestStatuses(props.config, props.agents);
  const surface = useManagementSurface(props, runner, form);
  const { drawer, menuAnchor } = surface;
  return (
    <section className="settings-agent-management" data-testid="settings-agent-management">
      <div className="settings-agent-management-toolbar">
        <span>{props.loading ? "正在加载…" : `${props.agents.length} 个业务 Agent`}</span>
        <button
          className="primary-button"
          type="button"
          data-testid="settings-agent-import-open"
          disabled={surface.disabled}
          onClick={surface.openCreateDrawer}
        >
          <Upload size={15} />导入 Agent
        </button>
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
          onRestore={surface.restore}
          onClose={surface.closeDrawer}
        />
      ) : null}
    </section>
  );
}
