import { useEffect, useMemo, useState } from "react";
import CodeMirror from "@uiw/react-codemirror";
import { json, jsonParseLinter } from "@codemirror/lang-json";
import { linter, lintGutter } from "@codemirror/lint";
import { EditorView } from "@codemirror/view";
import { Braces, RefreshCw, Save, WandSparkles } from "lucide-react";
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

interface JsonValidation {
  error?: string;
  formatted?: string;
  sizeBytes: number;
}

function validateJsonContent(content: string, loading: boolean): JsonValidation {
  if (loading && !content.trim()) return { sizeBytes: 0 };
  try {
    const parsed = JSON.parse(content);
    if (!parsed || typeof parsed !== "object" || Array.isArray(parsed)) {
      return { error: ".mcp.json 必须是 JSON object", sizeBytes: new Blob([content]).size };
    }
    return {
      formatted: `${JSON.stringify(parsed, null, 2)}\n`,
      sizeBytes: new Blob([content]).size,
    };
  } catch (nextError) {
    return {
      error: nextError instanceof Error ? nextError.message : "JSON 解析失败",
      sizeBytes: new Blob([content]).size,
    };
  }
}

function formatBytes(size: number): string {
  if (size < 1024) return `${size} B`;
  return `${(size / 1024).toFixed(1)} KB`;
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

  const editorExtensions = useMemo(() => [
    json(),
    lintGutter(),
    linter(jsonParseLinter(), { delay: 250 }),
    EditorView.lineWrapping,
    EditorView.theme({
      "&": {
        height: "100%",
        backgroundColor: "#fffdf8",
        color: "#33281f",
        fontSize: "13px",
      },
      ".cm-scroller": {
        fontFamily: "ui-monospace, SFMono-Regular, Menlo, Monaco, Consolas, monospace",
        lineHeight: "1.55",
      },
      ".cm-gutters": {
        backgroundColor: "#f8f0e8",
        color: "#8b7a6a",
        borderRight: "1px solid rgba(104, 78, 56, 0.16)",
      },
      ".cm-activeLine, .cm-activeLineGutter": {
        backgroundColor: "rgba(143, 75, 50, 0.08)",
      },
      ".cm-tooltip": {
        borderRadius: "8px",
        border: "1px solid rgba(181, 72, 72, 0.25)",
      },
      ".cm-diagnostic": {
        fontFamily: "inherit",
        fontSize: "12px",
      },
    }),
  ], []);

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

  const validation = useMemo(() => validateJsonContent(draft, loading), [draft, loading]);
  const dirty = file ? draft !== file.content : draft.trim() !== "";
  const blocked = loading || applying || streaming;
  const validationLabel = loading ? "加载中" : validation.error ? "JSON 错误" : "JSON 有效";
  const validationTone = loading ? "" : validation.error ? "warn" : "good";

  function resetDraft() {
    setDraft(file?.exists ? file.content : "{}\n");
    setError(undefined);
    setStatus(undefined);
  }

  function formatDraft() {
    if (validation.error || !validation.formatted) return;
    setDraft(validation.formatted);
    setError(undefined);
    setStatus(undefined);
  }

  async function applyConfig() {
    if (validation.error || blocked) return;
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
        <header className="config-file-editor-head">
          <div className="config-file-editor-title">
            <Braces size={20} />
            <div>
              <h3>{path}</h3>
              <p>{file?.container_path || "加载中"}</p>
            </div>
          </div>
          <div className="config-file-editor-toolbar">
            <span className={`config-file-editor-chip ${validationTone}`}>
              {validationLabel}
            </span>
            <span className="config-file-editor-chip">{loading ? "待加载" : formatBytes(validation.sizeBytes)}</span>
            <button
              className="secondary-button"
              type="button"
              data-testid="agent-config-file-editor-format"
              disabled={blocked || Boolean(validation.error)}
              onClick={formatDraft}
            >
              <WandSparkles size={15} />格式化
            </button>
            <button
              className="secondary-button"
              type="button"
              disabled={blocked || !dirty}
              onClick={resetDraft}
            >
              <RefreshCw size={15} />还原
            </button>
          </div>
        </header>

        <div className="config-file-editor-body" data-testid="agent-config-file-editor-content">
          <CodeMirror
            value={draft}
            height="100%"
            basicSetup={{
              foldGutter: true,
              highlightActiveLine: true,
              highlightSelectionMatches: true,
              lineNumbers: true,
            }}
            extensions={editorExtensions}
            editable={!blocked}
            readOnly={blocked}
            onChange={(value) => {
              setDraft(value);
              setStatus(undefined);
              setError(undefined);
            }}
          />
        </div>

        <div className="config-file-editor-feedback">
          {validation.error ? <div className="error-box">{validation.error}</div> : null}
          {error ? <div className="error-box" data-testid="agent-config-file-editor-error">{error}</div> : null}
          {status ? <div className="success-box" data-testid="agent-config-file-editor-status">{status}</div> : null}
        </div>

        <div className="config-file-editor-actions">
          <button className="secondary-button" type="button" onClick={onClose}>关闭</button>
          <button
            className="primary-button"
            type="button"
            data-testid="agent-config-file-editor-apply"
            disabled={blocked || Boolean(validation.error) || !dirty}
            onClick={applyConfig}
          >
            <Save size={15} />{applying ? "应用中" : "应用"}
          </button>
        </div>
      </section>
    </div>
  );
}
