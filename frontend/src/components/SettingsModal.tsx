import { useCallback, useEffect, useLayoutEffect, useMemo, useRef, useState } from "react";

import {
  getOpenAICompatAgent,
  listBusinessAgents,
  resetOpenAICompatAgent,
  setBusinessAgentLifecycle,
  setOpenAICompatAgent,
  type OpenAICompatAgentConfig,
} from "../api/runtime";
import type { AgentSummary, RuntimeClientConfig } from "../types/runtime";
import {
  DeveloperSettingsTab,
  SettingsContentHeader,
  SettingsFooter,
  SettingsHeader,
  SettingsNavigation,
  type SettingsTab,
} from "./SettingsModalSections";
import { BusinessAgentManagementPanel } from "./BusinessAgentManagementPanel";
import {
  activateSettingsRequestContext,
  beginSettingsRequest,
  deactivateSettingsRequestContext,
  settleSettingsRequest,
  type SettingsRequestAuthority,
  type SettingsRequestGeneration,
} from "./settingsRequestContext";
import {
  PendingDeletionNotice,
  usePendingDeletionOperations,
} from "./usePendingDeletionOperations";
import "./SettingsModal.css";

// 四阶段改进治理 §2 平台设置：业务 Agent 管理 / Developer·Debug（纯配置）。
// 资产 Registry 已提升为一级导航「资产复利」；旧反馈优化、API Docs、Langfuse 仍在此处。

interface SettingsModalProps {
  open: boolean;
  config: RuntimeClientConfig;
  apiDocsUrl: string;
  langfuseUrl: string;
  onClose: () => void;
  onSave: (config: RuntimeClientConfig) => void;
  onAgentsChanged: () => void;
  onOpenAgentTestAssets: (agentId: string) => void;
}

interface AgentSettingsTabProps {
  config: RuntimeClientConfig;
  agents: AgentSummary[];
  loading: boolean;
  externalBusy: boolean;
  pending: string | null;
  requestGeneration: { current: SettingsRequestGeneration };
  reloadAgents: (authority?: SettingsRequestAuthority) => Promise<void>;
  onAgentsChanged: () => void;
  onBusyChange: (busy: boolean) => void;
  onLifecycle: (agentId: string, status: string) => void;
  onOpenTestAssets: (agentId: string) => void;
  onDelete: (agentId: string) => void;
}

function SettingsAlerts({
  error,
  successMessage,
}: {
  error: string | undefined;
  successMessage: string | undefined;
}) {
  return (
    <>
      {error ? (
        <div className="error-box settings-error" data-testid="settings-error" role="alert" aria-live="assertive">
          {error}
        </div>
      ) : null}
      {successMessage ? (
        <div className="settings-success" data-testid="settings-success" role="status" aria-live="polite">
          <span>{successMessage}</span>
        </div>
      ) : null}
    </>
  );
}

function AgentSettingsTab(props: AgentSettingsTabProps) {
  return (
    <section className="settings-section settings-section-agents" data-testid="settings-section-agents" role="tabpanel">
      <BusinessAgentManagementPanel
        config={props.config}
        agents={props.agents}
        loading={props.loading}
        externalBusy={props.externalBusy}
        pending={props.pending}
        requestGeneration={props.requestGeneration}
        reloadAgents={props.reloadAgents}
        onAgentsChanged={props.onAgentsChanged}
        onBusyChange={props.onBusyChange}
        onLifecycle={props.onLifecycle}
        onOpenTestAssets={props.onOpenTestAssets}
        onDelete={props.onDelete}
      />
    </section>
  );
}

type RunSettingsAction = <T>(
  request: () => Promise<T>,
  onSuccess: (value: T) => void,
  actionKey?: string,
  lanes?: readonly string[],
) => void;

const REGISTRY_LANE = "registry";
const OPENAI_COMPAT_LANE = "openai-compat";
const FEEDBACK_LANE = "feedback";

function useSettingsRequestContext(config: RuntimeClientConfig, open: boolean) {
  const generation = useRef<SettingsRequestGeneration>({ active: false, context: 0, lanes: {} });
  useLayoutEffect(() => {
    if (!open) {
      deactivateSettingsRequestContext(generation.current);
      return;
    }
    const context = activateSettingsRequestContext(generation.current);
    return () => deactivateSettingsRequestContext(generation.current, context);
  }, [config.apiBase, config.apiKey, open]);
  return generation;
}

function useActionFeedback(
  generation: { current: SettingsRequestGeneration },
  config: RuntimeClientConfig,
  open: boolean,
) {
  const [pending, setPending] = useState<string | null>(null);
  const [error, setError] = useState<string | undefined>();
  const [successMessage, setSuccessMessage] = useState<string | undefined>();
  useLayoutEffect(() => {
    setPending(null);
    setError(undefined);
    setSuccessMessage(undefined);
  }, [config.apiBase, config.apiKey, open]);
  const run = useCallback<RunSettingsAction>(
    (request, onSuccess, actionKey = "busy", lanes = []) => {
      const token = beginSettingsRequest(generation.current, [FEEDBACK_LANE, ...lanes]);
      setPending(actionKey);
      setError(undefined);
      setSuccessMessage(undefined);
      void settleSettingsRequest(
        generation.current,
        token,
        Promise.resolve().then(request),
        {
          onSuccess,
          onError: (cause) => setError(cause instanceof Error ? cause.message : String(cause)),
          onFinally: () => setPending(null),
          errorLanes: [FEEDBACK_LANE],
          finallyLanes: [FEEDBACK_LANE],
        },
      );
    },
    [generation],
  );
  return {
    error,
    pending,
    run,
    setError,
    setPending,
    setSuccessMessage,
    successMessage,
  };
}

function useRuntimeFields(config: RuntimeClientConfig, open: boolean) {
  const [apiBase, setApiBase] = useState(config.apiBase);
  const [apiKey, setApiKey] = useState(config.apiKey);
  useEffect(() => {
    setApiBase(config.apiBase);
    setApiKey(config.apiKey);
  }, [config.apiBase, config.apiKey, open]);
  return { apiBase, apiKey, setApiBase, setApiKey };
}

interface RegistryReloadEffects {
  onStart: () => void;
  onSuccess: (agents: AgentSummary[]) => void;
  onError: (cause: unknown) => void;
  onFinally: () => void;
}

export async function runSettingsRegistryReload(
  generation: SettingsRequestGeneration,
  origin: SettingsRequestAuthority | undefined,
  request: () => Promise<AgentSummary[]>,
  effects: RegistryReloadEffects,
): Promise<void> {
  if (origin && !origin.isCurrent()) return;
  const originLanes = origin ? Object.keys(origin.token.lanes) : [];
  const token = beginSettingsRequest(
    generation,
    [REGISTRY_LANE],
    [FEEDBACK_LANE, ...originLanes],
  );
  effects.onStart();
  await settleSettingsRequest(generation, token, request(), {
    onSuccess: effects.onSuccess,
    onError: effects.onError,
    onFinally: effects.onFinally,
    successLanes: [REGISTRY_LANE, ...originLanes],
    errorLanes: [REGISTRY_LANE, FEEDBACK_LANE, ...originLanes],
    finallyLanes: [REGISTRY_LANE, ...originLanes],
  });
}

function useAgentRegistry(
  config: RuntimeClientConfig,
  open: boolean,
  generation: { current: SettingsRequestGeneration },
  run: RunSettingsAction,
  setError: (error: string | undefined) => void,
  onAgentsChanged: () => void,
) {
  const requestConfig = useMemo(
    () => ({ apiBase: config.apiBase, apiKey: config.apiKey }),
    [config.apiBase, config.apiKey],
  );
  const [agents, setAgents] = useState<AgentSummary[]>([]);
  const [loading, setLoading] = useState(false);
  const reloadAgents = useCallback(async (origin?: SettingsRequestAuthority) => {
    await runSettingsRegistryReload(
      generation.current,
      origin,
      () => listBusinessAgents(requestConfig),
      {
        onStart: () => {
          setError(undefined);
          setLoading(true);
        },
        onSuccess: setAgents,
        onError: (cause) => setError(cause instanceof Error ? cause.message : String(cause)),
        onFinally: () => setLoading(false),
      },
    );
  }, [generation, requestConfig, setError]);
  useEffect(() => {
    if (open) void reloadAgents();
  }, [open, reloadAgents]);
  const handleLifecycle = useCallback(
    (agentId: string, status: string) => {
      run(
        async () => {
          await setBusinessAgentLifecycle(requestConfig, agentId, status);
          return listBusinessAgents(requestConfig);
        },
        (value) => {
          setAgents(value);
          onAgentsChanged();
        },
        `lifecycle:${agentId}`,
        [REGISTRY_LANE],
      );
    },
    [onAgentsChanged, requestConfig, run],
  );
  return { agents, handleLifecycle, loading, reloadAgents, setAgents };
}

function useOpenAICompatSettings(
  config: RuntimeClientConfig,
  open: boolean,
  generation: { current: SettingsRequestGeneration },
  agents: AgentSummary[],
  run: RunSettingsAction,
) {
  const requestConfig = useMemo(
    () => ({ apiBase: config.apiBase, apiKey: config.apiKey }),
    [config.apiBase, config.apiKey],
  );
  const [value, setValue] = useState<OpenAICompatAgentConfig | null>(null);
  const [selection, setSelection] = useState("");
  const options = useMemo(() => agents.map((agent) => agent.agent_id), [agents]);
  useEffect(() => {
    if (!open) return;
    const token = beginSettingsRequest(generation.current, [OPENAI_COMPAT_LANE]);
    void settleSettingsRequest(
      generation.current,
      token,
      getOpenAICompatAgent(requestConfig),
      {
        onSuccess: (response) => {
          setValue(response);
          setSelection(response.effective_agent_id);
        },
        onError: () => {
          setValue(null);
          setSelection("");
        },
      },
    );
  }, [generation, open, requestConfig]);
  useEffect(() => {
    if (selection && !options.includes(selection)) {
      setSelection(agents.find((agent) => agent.default)?.agent_id ?? options[0] ?? "");
    }
  }, [agents, options, selection]);
  const applyResponse = useCallback((response: OpenAICompatAgentConfig) => {
    setValue(response);
    setSelection(response.effective_agent_id);
  }, []);
  const save = useCallback(() => {
    run(
      () => setOpenAICompatAgent(requestConfig, selection),
      applyResponse,
      "busy",
      [OPENAI_COMPAT_LANE],
    );
  }, [applyResponse, requestConfig, run, selection]);
  const reset = useCallback(() => {
    run(
      () => resetOpenAICompatAgent(requestConfig),
      applyResponse,
      "busy",
      [OPENAI_COMPAT_LANE],
    );
  }, [applyResponse, requestConfig, run]);
  return { options, reset, save, selection, setSelection, value };
}

function useSettingsModalController(props: SettingsModalProps) {
  const requestContext = useSettingsRequestContext(props.config, props.open);
  const runtime = useRuntimeFields(props.config, props.open);
  const feedback = useActionFeedback(requestContext, props.config, props.open);
  const registry = useAgentRegistry(
    props.config,
    props.open,
    requestContext,
    feedback.run,
    feedback.setError,
    props.onAgentsChanged,
  );
  const [workspaceBusy, setWorkspaceBusy] = useState(false);
  const [activeTab, setActiveTab] = useState<SettingsTab>("agents");
  const deletions = usePendingDeletionOperations({
    open: props.open,
    config: props.config,
    agents: registry.agents,
    setAgents: registry.setAgents,
    setPending: feedback.setPending,
    setError: feedback.setError,
    setSuccessMessage: feedback.setSuccessMessage,
    onAgentsChanged: props.onAgentsChanged,
  });
  const openaiCompat = useOpenAICompatSettings(
    props.config,
    props.open,
    requestContext,
    registry.agents,
    feedback.run,
  );
  const selectTab = useCallback(
    (tab: SettingsTab) => {
      setActiveTab(tab);
      feedback.setError(undefined);
      feedback.setSuccessMessage(undefined);
    },
    [feedback.setError, feedback.setSuccessMessage],
  );
  const saveRuntime = useCallback(
    () => props.onSave({ apiBase: runtime.apiBase.trim(), apiKey: runtime.apiKey.trim() }),
    [props.onSave, runtime.apiBase, runtime.apiKey],
  );
  return {
    activeTab,
    busy: feedback.pending !== null || workspaceBusy,
    deletions,
    feedback,
    openaiCompat,
    registry,
    requestContext,
    runtime,
    saveRuntime,
    selectTab,
    setWorkspaceBusy,
  };
}

type SettingsModalController = ReturnType<typeof useSettingsModalController>;

function SettingsModalView({
  props,
  controller,
}: {
  props: SettingsModalProps;
  controller: SettingsModalController;
}) {
  const { deletions, feedback, openaiCompat, registry, runtime } = controller;
  return (
    <div className="settings-backdrop" role="presentation">
      <section className="settings-panel" data-testid="settings-panel" role="dialog" aria-modal="true" aria-labelledby="settings-panel-title">
        <SettingsHeader agentCount={registry.agents.length} onClose={props.onClose} />
        <SettingsAlerts error={feedback.error} successMessage={feedback.successMessage} />
        <PendingDeletionNotice
          busy={controller.busy}
          pendingDeletions={deletions.pendingDeletions}
          onRefresh={deletions.handleRefreshDeletion}
        />
        <div className="settings-layout">
          <SettingsNavigation activeTab={controller.activeTab} onSelect={controller.selectTab} />
          <main className="settings-content" data-testid="settings-content">
            <SettingsContentHeader activeTab={controller.activeTab} />
            {controller.activeTab === "agents" ? (
              <AgentSettingsTab
                config={props.config}
                agents={registry.agents}
                loading={registry.loading}
                externalBusy={feedback.pending !== null}
                pending={feedback.pending}
                requestGeneration={controller.requestContext}
                reloadAgents={registry.reloadAgents}
                onAgentsChanged={props.onAgentsChanged}
                onBusyChange={controller.setWorkspaceBusy}
                onLifecycle={registry.handleLifecycle}
                onOpenTestAssets={props.onOpenAgentTestAssets}
                onDelete={deletions.handleDelete}
              />
            ) : null}
            {controller.activeTab === "developer" ? (
              <DeveloperSettingsTab
                apiBase={runtime.apiBase}
                apiKey={runtime.apiKey}
                apiDocsUrl={props.apiDocsUrl}
                langfuseUrl={props.langfuseUrl}
                busy={controller.busy}
                openaiCompat={openaiCompat.value}
                openaiCompatSelection={openaiCompat.selection}
                openaiCompatOptions={openaiCompat.options}
                onApiBaseChange={runtime.setApiBase}
                onApiKeyChange={runtime.setApiKey}
                onOpenAICompatChange={openaiCompat.setSelection}
                onSaveOpenAICompat={openaiCompat.save}
                onResetOpenAICompat={openaiCompat.reset}
              />
            ) : null}
          </main>
        </div>
        <SettingsFooter onClose={props.onClose} onSave={controller.saveRuntime} />
      </section>
    </div>
  );
}

export function SettingsModal(props: SettingsModalProps) {
  const controller = useSettingsModalController(props);
  if (!props.open) return null;
  return <SettingsModalView props={props} controller={controller} />;
}
