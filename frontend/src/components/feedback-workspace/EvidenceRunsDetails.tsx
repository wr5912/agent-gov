import { useEffect, useMemo, useState } from "react";
import { Loader2 } from "lucide-react";
import { getEvidencePackageFile } from "../../api/runtime";
import type {
  EvidencePackageFileRecord,
  EvidencePackageRecord,
  ExternalFeedbackWorkspaceProps,
  FeedbackRunRecord,
} from "../../types/feedback";
import { DetailMetricGrid, DetailRecordList, FormattedText, Pill, isEmptyJsonValue, jsonPreview } from "./common";
import {
  evidenceFileName,
  firstEvidenceFileName,
  formatDate,
  latestItem,
  shortId,
  traceRefsFromContent,
} from "./selectors";

export function EvidencePackageDetails({
  clientConfig,
  packages,
}: {
  clientConfig: ExternalFeedbackWorkspaceProps["clientConfig"];
  packages: EvidencePackageRecord[];
}) {
  const [selectedId, setSelectedId] = useState<string | null>(null);
  const [selectedFile, setSelectedFile] = useState<string | null>(null);
  const [fileRecord, setFileRecord] = useState<EvidencePackageFileRecord | null>(null);
  const [fileLoading, setFileLoading] = useState(false);

  const selectedPackage = useMemo(() => packages.find((item) => item.evidence_package_id === selectedId) || latestItem(packages) || null, [packages, selectedId]);
  const includedFiles = useMemo(() => selectedPackage?.included_files || [], [selectedPackage]);

  useEffect(() => {
    const latestPackage = latestItem(packages);
    setSelectedId((current) => current || latestPackage?.evidence_package_id || null);
  }, [packages]);

  useEffect(() => {
    const nextFile = firstEvidenceFileName(includedFiles);
    setSelectedFile((current) => current || nextFile || null);
  }, [includedFiles]);

  useEffect(() => {
    let cancelled = false;
    async function loadFile() {
      if (!selectedPackage || !selectedFile) {
        setFileRecord(null);
        return;
      }
      setFileLoading(true);
      try {
        const next = await getEvidencePackageFile(clientConfig, selectedPackage.evidence_package_id, selectedFile);
        if (!cancelled) setFileRecord(next);
      } catch {
        if (!cancelled) setFileRecord(null);
      } finally {
        if (!cancelled) setFileLoading(false);
      }
    }
    loadFile();
    return () => {
      cancelled = true;
    };
  }, [clientConfig, selectedPackage, selectedFile]);

  if (!packages.length) {
    return <div className="fw-empty-inline">暂无证据包</div>;
  }

  const hasMultiplePackages = packages.length > 1;

  return (
    <div className={`fw-detail-layout ${hasMultiplePackages ? "" : "fw-detail-layout-single"}`}>
      {hasMultiplePackages ? (
        <div className="fw-detail-list">
          {packages.map((item) => (
            <button
              className={`fw-detail-list-item ${selectedPackage?.evidence_package_id === item.evidence_package_id ? "is-active" : ""}`}
              key={item.evidence_package_id}
              onClick={() => {
                setSelectedId(item.evidence_package_id);
                setSelectedFile(firstEvidenceFileName(item.included_files) || null);
              }}
              type="button"
            >
              <strong>{shortId(item.evidence_package_id)}</strong>
              <small>{formatDate(item.created_at)}</small>
            </button>
          ))}
        </div>
      ) : null}
      <div className="fw-detail-main">
        <DetailMetricGrid
          items={[
            ["evidence_package_id", shortId(selectedPackage?.evidence_package_id)],
            ["main_agent_version_id", shortId(selectedPackage?.main_agent_version_id)],
            ["created_at", formatDate(selectedPackage?.created_at)],
            ["included_files", String(includedFiles.length)],
          ]}
        />
        <CompletenessStrip completeness={selectedPackage?.completeness || {}} />
        <div className="fw-evidence-file-layout">
          <div className="fw-evidence-file-list">
            {includedFiles.map((item) => {
              const fileName = evidenceFileName(item);
              if (!fileName) return null;
              return (
                <button
                  className={selectedFile === fileName ? "is-active" : ""}
                  key={fileName}
                  onClick={() => setSelectedFile(fileName)}
                  type="button"
                >
                  <span>{fileName}</span>
                  <small>{shortId(String(item.sha256 || ""))}</small>
                </button>
              );
            })}
          </div>
          <div className="fw-json-preview">
            <div className="fw-json-preview-header">
              <strong>{selectedFile || "未选择文件"}</strong>
              {fileLoading ? <Loader2 size={14} className="fw-spin" /> : null}
            </div>
            <TraceLinks content={fileRecord?.content} />
            {fileRecord && isEmptyJsonValue(fileRecord.content) ? <div className="fw-json-empty-note">无关联数据</div> : null}
            <pre>{fileRecord ? jsonPreview(fileRecord.content) : "暂无文件内容"}</pre>
          </div>
        </div>
      </div>
    </div>
  );
}

export function RunsDetails({ runs }: { runs: FeedbackRunRecord[] }) {
  return (
    <DetailRecordList hasItems={runs.length > 0} emptyText="暂无关联运行">
      {runs.map((run) => (
        <article className="fw-run-card" key={run.run_id}>
          <div className="fw-detail-record-head">
            <h4>{shortId(run.run_id)} · {shortId(run.agent_version_id)}</h4>
            <Pill tone="blue">run</Pill>
          </div>
          <DetailMetricGrid
            items={[
              ["session", shortId(run.session_id)],
              ["agent_version", shortId(run.agent_version_id)],
              ["created", formatDate(run.created_at)],
              ["completed", formatDate(run.completed_at)],
              ["stop_reason", run.stop_reason || "-"],
              ["cost", run.total_cost_usd != null ? `$${run.total_cost_usd.toFixed(6)}` : "-"],
            ]}
          />
          <section className="fw-run-section">
            <h4>回答摘要</h4>
            <FormattedText className="fw-record-long-text" value={run.answer_summary || run.message || "-"} />
          </section>
          <section className="fw-run-section">
            <h4>工具调用</h4>
            <RunToolList tools={run.agent_activity?.tool_names || []} />
          </section>
          {run.errors?.length ? (
            <section className="fw-run-section">
              <h4>错误</h4>
              <FormattedText className="fw-warning-text" value={run.errors.join("\n")} />
            </section>
          ) : null}
        </article>
      ))}
    </DetailRecordList>
  );
}

function CompletenessStrip({ completeness }: { completeness: Record<string, unknown> }) {
  const entries = Object.entries(completeness);
  if (!entries.length) return null;
  return (
    <div className="fw-completeness-strip">
      {entries.map(([key, value]) => (
        <span className={value ? "is-complete" : "is-empty"} key={key}>
          {key.replace(/^has_/, "")}
        </span>
      ))}
    </div>
  );
}

function TraceLinks({ content }: { content?: unknown }) {
  const refs = traceRefsFromContent(content);
  if (!refs.length) return null;
  return (
    <div className="fw-trace-links">
      {refs.map((ref) => (
        <a href={ref.url} key={`${ref.traceId}:${ref.url}`} target="_blank" rel="noreferrer">
          Langfuse trace {shortId(ref.traceId)}
        </a>
      ))}
    </div>
  );
}

function RunToolList({ tools }: { tools: string[] }) {
  if (!tools.length) return <div className="fw-empty-inline fw-run-empty-inline">暂无工具调用记录</div>;
  return (
    <div className="fw-run-tool-list">
      {tools.map((tool, index) => (
        <span className="fw-run-tool-pill" title={tool} key={`${tool}:${index}`}>
          {tool}
        </span>
      ))}
    </div>
  );
}
