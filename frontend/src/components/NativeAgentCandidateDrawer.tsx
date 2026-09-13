import { Loader2, Save } from "lucide-react";
import { useEffect, useMemo, useState, type ChangeEvent } from "react";
import {
  createNativeAgentCandidate,
  getNativeAgentCandidateSource,
  getRuntimeNativeAgentSchema,
} from "../api/runtime";
import {
  deriveNativeAgentForm,
  nativeAgentFormValueAt,
  updateNativeAgentFormValue,
  type NativeAgentFieldValue,
  type NativeAgentFormDefinition,
  type NativeAgentFormField,
} from "../nativeAgentSchemaForm";
import type {
  AgentSummary,
  NativeAgentCandidateRequest,
  NativeAgentCandidateResponse,
  NativeAgentDataInput,
  RuntimeClientConfig,
} from "../types/runtime";
import { validateAgentId } from "./agentSettingsValidation";
import { CandidateReceiptDetails } from "./AgentWorkspaceImportDrawer";
import { DrawerShell } from "./DrawerShell";

interface NativeAgentCandidateDrawerProps {
  config: RuntimeClientConfig;
  existingAgents: AgentSummary[];
  targetAgent?: AgentSummary;
  onSaved: (receipt: NativeAgentCandidateResponse) => void;
  onOpenGovernance: (receipt: NativeAgentCandidateResponse) => void;
  onClose: () => void;
}

export function NativeAgentCandidateDrawer(props: NativeAgentCandidateDrawerProps) {
  const [agentId, setAgentId] = useState(props.targetAgent?.agent_id || "");
  const [definition, setDefinition] = useState<NativeAgentFormDefinition | null>(null);
  const [agentData, setAgentData] = useState<Record<string, unknown> | null>(null);
  const [sourceCommitSha, setSourceCommitSha] = useState<string | undefined>();
  const [sourceChangeSetId, setSourceChangeSetId] = useState<string | undefined>();
  const [receipt, setReceipt] = useState<NativeAgentCandidateResponse | null>(null);
  const [loadingSchema, setLoadingSchema] = useState(true);
  const [submitting, setSubmitting] = useState(false);
  const [error, setError] = useState<string | undefined>();
  const editing = Boolean(props.targetAgent);

  useEffect(() => {
    const controller = new AbortController();
    setLoadingSchema(true);
    setError(undefined);
    setDefinition(null);
    setAgentData(null);
    setSourceCommitSha(undefined);
    setSourceChangeSetId(undefined);
    setReceipt(null);
    const sourceRequest = props.targetAgent
      ? getNativeAgentCandidateSource(props.config, props.targetAgent.agent_id, controller.signal)
      : Promise.resolve(null);
    void Promise.all([
      getRuntimeNativeAgentSchema(props.config, controller.signal),
      sourceRequest,
    ])
      .then(([response, source]) => {
        if (controller.signal.aborted) return;
        const nextDefinition = deriveNativeAgentForm(response.schema);
        setDefinition(nextDefinition);
        if (source) {
          setAgentId(props.targetAgent?.agent_id || "");
          setAgentData(source.agent_data as unknown as Record<string, unknown>);
          setSourceCommitSha(source.current_commit_sha);
          setSourceChangeSetId(source.change_set_id || undefined);
        } else {
          setAgentData(nextDefinition.initialValue);
          setSourceCommitSha(undefined);
          setSourceChangeSetId(undefined);
        }
      })
      .catch((caught) => {
        if (!controller.signal.aborted) setError(errorMessage(caught));
      })
      .finally(() => {
        if (!controller.signal.aborted) setLoadingSchema(false);
      });
    return () => controller.abort();
  }, [props.config, props.targetAgent]);

  const validationError = useMemo(() => {
    const cleanId = agentId.trim();
    const idError = validateAgentId(cleanId);
    if (!cleanId) return "请输入 Agent ID。";
    if (idError) return idError;
    if (!editing && props.existingAgents.some((agent) => agent.agent_id === cleanId)) {
      return `Agent ID ${cleanId} 已存在，请从该 Agent 的操作菜单创建配置候选。`;
    }
    const name = agentData && typeof agentData.name === "string" ? agentData.name.trim() : "";
    if (!name) return "请输入 Agent 名称。";
    return undefined;
  }, [agentData, agentId, editing, props.existingAgents]);

  const updateField = (path: string[], value: NativeAgentFieldValue) => {
    setReceipt(null);
    setAgentData((current) => updateNativeAgentFormValue(current || {}, path, value));
  };

  const submit = async () => {
    if (!agentData || validationError || submitting) return;
    setSubmitting(true);
    setError(undefined);
    setReceipt(null);
    try {
      const payload: NativeAgentCandidateRequest = {
        agent_data: agentData as NativeAgentDataInput,
        expected_current_commit_sha: sourceChangeSetId ? undefined : sourceCommitSha,
        change_set_id: sourceChangeSetId,
        expected_candidate_commit_sha: sourceChangeSetId ? sourceCommitSha : undefined,
        reason: editing ? "Settings 原生表单配置候选" : "Settings 原生表单创建业务 Agent 候选",
      };
      const result = await createNativeAgentCandidate(props.config, agentId.trim(), payload);
      setSourceChangeSetId(result.change_set_id);
      setSourceCommitSha(result.candidate_commit_sha);
      setReceipt(result);
      props.onSaved(result);
    } catch (caught) {
      setError(errorMessage(caught));
    } finally {
      setSubmitting(false);
    }
  };

  return (
    <DrawerShell
      title={editing ? "配置业务 Agent 候选" : "表单创建业务 Agent"}
      description="字段与校验来自固定版本 AgentScope AgentData schema；保存只形成隔离 Git 候选。"
      size="wide"
      testId="settings-native-agent-drawer"
      dataState={editing ? "edit" : "create"}
      bodyClassName="settings-native-agent-drawer-body"
      closeDisabled={submitting}
      onClose={props.onClose}
    >
      {loadingSchema ? <div className="empty-state" data-testid="settings-native-agent-loading">正在读取 AgentScope schema…</div> : null}
      {error ? <div className="error-box" data-testid="settings-native-agent-error" role="alert">{error}</div> : null}
      {!loadingSchema && definition && agentData ? (
        <form
          className="settings-native-agent-form"
          onSubmit={(event) => {
            event.preventDefault();
            void submit();
          }}
        >
          <section className="settings-native-agent-section">
            <h4>治理身份</h4>
            <label className="form-field">
              <span>Agent ID</span>
              <input
                data-testid="settings-native-agent-id"
                value={agentId}
                readOnly={editing}
                disabled={editing || submitting}
                placeholder="例如 incident-response-agent"
                onChange={(event) => { setAgentId(event.target.value); setReceipt(null); }}
              />
              <small>Agent ID 属于 AgentGov 路由与 Git 身份，不写入原生 AgentData。</small>
            </label>
          </section>

          <section className="settings-native-agent-section" data-testid="settings-native-agent-primary-fields">
            <h4>AgentScope Agent</h4>
            <div className="settings-native-agent-fields">
              {definition.fields.map((field) => (
                <SchemaField
                  key={field.path.join(".")}
                  field={field}
                  value={nativeAgentFormValueAt(agentData, field.path)}
                  disabled={submitting}
                  onChange={(value) => updateField(field.path, value)}
                />
              ))}
            </div>
          </section>

          {definition.sections.map((section) => (
            <fieldset className="settings-native-agent-section" key={section.key}>
              <legend>{section.title}</legend>
              {section.description ? <p>{section.description}</p> : null}
              <div className="settings-native-agent-fields">
                {section.fields.map((field) => (
                  <SchemaField
                    key={field.path.join(".")}
                    field={field}
                    value={nativeAgentFormValueAt(agentData, field.path)}
                    disabled={submitting}
                    onChange={(value) => updateField(field.path, value)}
                  />
                ))}
              </div>
            </fieldset>
          ))}

          {validationError ? <div className="settings-native-agent-validation" role="status">{validationError}</div> : null}
          {receipt ? (
            <CandidateReceiptDetails receipt={receipt} onOpenGovernance={() => props.onOpenGovernance(receipt)} />
          ) : null}
          <div className="settings-native-agent-actions">
            <button className="secondary-button" type="button" disabled={submitting} onClick={props.onClose}>关闭</button>
            <button
              className="primary-button"
              type="submit"
              data-testid="settings-native-agent-submit"
              disabled={Boolean(validationError) || submitting}
              aria-busy={submitting}
            >
              {submitting
                ? <><Loader2 size={14} className="settings-spin" />保存中…</>
                : <><Save size={14} />保存候选（不发布）</>}
            </button>
          </div>
        </form>
      ) : null}
    </DrawerShell>
  );
}

function SchemaField({ field, value, disabled, onChange }: {
  field: NativeAgentFormField;
  value: NativeAgentFieldValue;
  disabled: boolean;
  onChange: (value: NativeAgentFieldValue) => void;
}) {
  if (field.kind === "boolean") {
    return (
      <label className="settings-native-agent-toggle">
        <input type="checkbox" checked={value === true} disabled={disabled} onChange={(event) => onChange(event.target.checked)} />
        <span><strong>{field.title}</strong>{field.description ? <small>{field.description}</small> : null}</span>
      </label>
    );
  }
  const common = {
    value: value === null ? "" : String(value),
    disabled,
    onChange: (event: ChangeEvent<HTMLInputElement | HTMLTextAreaElement>) => {
      const raw = event.target.value;
      onChange(field.kind === "string" || raw === "" ? raw : Number(raw));
    },
  };
  return (
    <label className={`form-field ${field.textarea ? "wide" : ""}`.trim()}>
      <span>{field.title}</span>
      {field.textarea ? (
        <textarea {...common} rows={field.path.length === 1 ? 7 : 4} />
      ) : (
        <input
          {...common}
          type={field.kind === "number" || field.kind === "integer" ? "number" : "text"}
          step={field.kind === "integer" ? 1 : "any"}
          min={field.minimum}
          max={field.maximum}
        />
      )}
      {field.description ? <small>{field.description}</small> : null}
    </label>
  );
}

function errorMessage(error: unknown) {
  return error instanceof Error ? error.message : String(error);
}
