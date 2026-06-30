import { useEffect, useMemo, useState } from "react";
import { getAgentConfigFile, updateAgentConfigFile } from "../api/runtime";
import type { AgentConfigFileResponse, RuntimeClientConfig } from "../types/runtime";

interface AgentConfigFileEditorProps {
  clientConfig: RuntimeClientConfig;
  agentId: string;
  path: string;
  sessionId?: string;
  streaming: boolean;
  onApplied?: () => void;
  onClose: () => void;
}

export function AgentConfigFileEditor({
  clientConfig,
  agentId,
  path,
  sessionId,
  streaming,
  onApplied,
  onClose,
}: AgentConfigFileEditorProps) {
  const [file, setFile] = useState<AgentConfigFileResponse | null>(null);
  const [draft, setDraft] = useState("");
  const [loading, setLoading] = useState(true);
  const [applying, setApplying] = useState(false);
  const [error, setError] = useState<string | undefined>();
  const [status, setStatus] = useState<string | undefined>();

  useEffect(() => {
    let cancelled = false;
    setLoading(true);
    setError(undefined);
    setStatus(undefined);
    getAgentConfigFile(clientConfig, agentId, path)
      .then((nextFile) => {
        if (cancelled) return;
        setFile(nextFile);
        setDraft(nextFile.exists ? nextFile.content : "{}\n");
      })
      .catch((nextError) => {
        if (!cancelled) setError(nextError instanceof Error ? nextError.message : String(nextError));
      })
      .finally(() => {
        if (!cancelled) setLoading(false);
      });
    return () => {
      cancelled = true;
    };
  }, [agentId, clientConfig, path]);

  const jsonError = useMemo(() => {
    try {
      const parsed = JSON.parse(draft);
      if (!parsed || typeof parsed !== "object" || Array.isArray(parsed)) return ".mcp.json 必须是 JSON object";
      return undefined;
    } catch (nextError) {
      return nextError instanceof Error ? nextError.message : "JSON 解析失败";
    }
  }, [draft]);
  const dirty = file ? draft !== file.content : draft.trim() !== "";

  async function applyConfig() {
    if (jsonError || applying || loading || streaming) return;
    setApplying(true);
    setError(undefined);
    setStatus(undefined);
    try {
      const updated = await updateAgentConfigFile(clientConfig, agentId, path, {
        content: draft,
        expected_sha256: file?.sha256 || undefined,
        session_id: sessionId,
      });
      setFile(updated);
      setDraft(updated.content);
      setStatus(updated.sdk_session_invalidated ? "已应用，当前会话将在下次运行重新加载配置。" : "已应用。");
      onApplied?.();
    } catch (nextError) {
      setError(nextError instanceof Error ? nextError.message : String(nextError));
    } finally {
      setApplying(false);
    }
  }

  return (
    <div className="modal-backdrop config-file-editor-backdrop" role="presentation" onClick={onClose}>
      <section
        className="modal-card config-file-editor-modal"
        role="dialog"
        aria-modal="true"
        aria-label="编辑 Agent 配置"
        data-testid="agent-config-file-editor"
        onClick={(event) => event.stopPropagation()}
      >
        <header className="modal-head">
          <div>
            <h3>{path}</h3>
            <p>{file?.container_path || "加载中"}</p>
          </div>
        </header>
        <label className="form-field">
          <span>内容</span>
          <textarea
            className="config-file-editor-textarea"
            data-testid="agent-config-file-editor-content"
            value={draft}
            disabled={loading || applying || streaming}
            spellCheck={false}
            onChange={(event) => {
              setDraft(event.target.value);
              setStatus(undefined);
            }}
          />
        </label>
        {jsonError ? <div className="error-box">{jsonError}</div> : null}
        {error ? <div className="error-box" data-testid="agent-config-file-editor-error">{error}</div> : null}
        {status ? <div className="success-box" data-testid="agent-config-file-editor-status">{status}</div> : null}
        <div className="modal-actions">
          <button className="secondary-button" type="button" onClick={onClose}>关闭</button>
          <button
            className="primary-button"
            type="button"
            data-testid="agent-config-file-editor-apply"
            disabled={loading || applying || streaming || Boolean(jsonError) || !dirty}
            onClick={applyConfig}
          >
            {applying ? "应用中" : "应用"}
          </button>
        </div>
      </section>
    </div>
  );
}
