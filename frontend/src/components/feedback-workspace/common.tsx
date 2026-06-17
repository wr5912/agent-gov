import type { ReactNode } from "react";

export type PillTone = "blue" | "green" | "orange" | "red" | "gray" | "purple";

export interface DetailTabItem<T extends string> {
  key: T;
  label: string;
}

export function Metric({ label, value }: { label: string; value?: string | number | null }) {
  return (
    <span className="fw-case-status-item">
      <small>{label}</small>
      <strong title={String(value ?? "-")}>{value ?? "-"}</strong>
    </span>
  );
}

export function DetailMetric({
  label,
  count,
  active,
  onClick,
}: {
  label: string;
  count: number;
  active: boolean;
  onClick: () => void;
}) {
  return (
    <button className={`fw-case-status-item fw-case-detail-trigger ${active ? "is-active" : ""}`} onClick={onClick} type="button">
      <small>{label}</small>
      <strong>{count} 条</strong>
      <span>详情</span>
    </button>
  );
}

export function Pill({ children, tone = "blue" }: { children: ReactNode; tone?: PillTone }) {
  return <span className={`fw-pill fw-pill-${tone}`}>{children}</span>;
}

export function DetailMetricGrid({ items }: { items: Array<[string, string | number | null | undefined]> }) {
  return (
    <div className="fw-detail-metric-grid">
      {items.map(([label, value]) => (
        <Metric label={label} value={value} key={label} />
      ))}
    </div>
  );
}

export function DetailTabs<T extends string>({
  tabs,
  active,
  onChange,
  label,
}: {
  tabs: Array<DetailTabItem<T>>;
  active: T;
  onChange: (key: T) => void;
  label: string;
}) {
  return (
    <div className="fw-detail-tabs" role="tablist" aria-label={label}>
      {tabs.map((tab) => (
        <button className={active === tab.key ? "is-active" : ""} type="button" onClick={() => onChange(tab.key)} key={tab.key}>
          {tab.label}
        </button>
      ))}
    </div>
  );
}

export function DetailRecordList({
  children,
  className,
  emptyText,
  hasItems,
}: {
  children: ReactNode;
  className?: string;
  emptyText: string;
  hasItems: boolean;
}) {
  return (
    <div className={`fw-detail-record-list ${className || ""}`.trim()}>
      {hasItems ? children : <div className="fw-empty-inline">{emptyText}</div>}
    </div>
  );
}

export function ReadableJsonBody({ value }: { value: unknown }) {
  // #7 框选复制友好：以「字段: 值」纯文本为主视图，原始 JSON 收进折叠。
  // 用户拖选粘贴得到的是可直接描述 issue 的线性文本，而非带括号/引号/逗号的 JSON。
  const readable = readableKeyValue(value);
  return (
    <div className="fw-readable-wrap">
      {readable ? (
        <pre className="fw-readable-body">{readable}</pre>
      ) : (
        <div className="fw-json-empty-note">无可展示内容</div>
      )}
      <details className="fw-readable-raw">
        <summary>查看原始 JSON</summary>
        <pre>{jsonPreview(value)}</pre>
      </details>
    </div>
  );
}

export function DetailJsonPreview({ title, value }: { title: string; value: unknown }) {
  return (
    <div className="fw-json-preview fw-json-preview-standalone fw-detail-json-output">
      <div className="fw-json-preview-header">
        <strong>{title}</strong>
      </div>
      <ReadableJsonBody value={value} />
    </div>
  );
}

type FormattedTextBlock =
  | { type: "heading"; level: number; text: string }
  | { type: "paragraph"; lines: string[] }
  | { type: "ol" | "ul"; items: string[] }
  | { type: "table"; headers: string[]; rows: string[][] };

export function FormattedText({
  value,
  className = "",
}: {
  value?: string | number | null;
  className?: string;
}) {
  const text = String(value ?? "").trim();
  const blocks = parseFormattedText(text || "-");
  return (
    <div className={`fw-formatted-text ${className}`.trim()}>
      {blocks.map((block, index) => {
        if (block.type === "heading") {
          const HeadingTag = block.level <= 2 ? "h4" : "h5";
          return <HeadingTag key={`heading:${index}`}>{block.text}</HeadingTag>;
        }
        if (block.type === "paragraph") {
          return <p key={`paragraph:${index}`}>{block.lines.join("\n")}</p>;
        }
        if (block.type === "table") {
          return (
            <div className="fw-formatted-table-wrap" key={`table:${index}`}>
              <table>
                <thead>
                  <tr>
                    {block.headers.map((cell, cellIndex) => (
                      <th key={`table:${index}:head:${cellIndex}`}>{cell || "-"}</th>
                    ))}
                  </tr>
                </thead>
                <tbody>
                  {block.rows.map((row, rowIndex) => (
                    <tr key={`table:${index}:row:${rowIndex}`}>
                      {block.headers.map((_, cellIndex) => (
                        <td key={`table:${index}:row:${rowIndex}:cell:${cellIndex}`}>{row[cellIndex] || "-"}</td>
                      ))}
                    </tr>
                  ))}
                </tbody>
              </table>
            </div>
          );
        }
        const ListTag = block.type === "ol" ? "ol" : "ul";
        return (
          <ListTag key={`${block.type}:${index}`}>
            {block.items.map((item, itemIndex) => (
              <li key={`${block.type}:${index}:${itemIndex}`}>{item}</li>
            ))}
          </ListTag>
        );
      })}
    </div>
  );
}

export function FormattedTextSection({
  title,
  value,
  compact = false,
}: {
  title: string;
  value?: string | number | null;
  compact?: boolean;
}) {
  return (
    <section className={`fw-text-section ${compact ? "fw-text-section-compact" : ""}`.trim()}>
      <h4>{title}</h4>
      <FormattedText value={value} />
    </section>
  );
}

export function FormattedTextFields({
  fields,
}: {
  fields: Array<[string, string | number | null | undefined]>;
}) {
  return (
    <div className="fw-text-field-grid">
      {fields.map(([title, value]) => (
        <FormattedTextSection title={title} value={value ?? "-"} compact key={title} />
      ))}
    </div>
  );
}

function parseFormattedText(text: string): FormattedTextBlock[] {
  const blocks: FormattedTextBlock[] = [];
  const paragraph: string[] = [];
  let listBlock: Extract<FormattedTextBlock, { type: "ol" | "ul" }> | null = null;
  let tableLines: string[] = [];

  function flushParagraph() {
    if (!paragraph.length) return;
    blocks.push({ type: "paragraph", lines: [...paragraph] });
    paragraph.length = 0;
  }

  function flushList() {
    if (!listBlock) return;
    blocks.push(listBlock);
    listBlock = null;
  }

  function flushTable() {
    if (!tableLines.length) return;
    const parsed = parseMarkdownTable(tableLines);
    if (parsed) {
      blocks.push(parsed);
    } else {
      blocks.push({ type: "paragraph", lines: [...tableLines] });
    }
    tableLines = [];
  }

  for (const rawLine of normalizeFormattedText(text).split("\n")) {
    const line = rawLine.trimEnd();
    const trimmed = line.trim();
    if (!trimmed) {
      flushParagraph();
      flushTable();
      continue;
    }

    if (trimmed === "---") {
      flushParagraph();
      flushList();
      flushTable();
      continue;
    }

    if (isMarkdownTableLine(trimmed)) {
      flushParagraph();
      flushList();
      tableLines.push(trimmed);
      continue;
    }

    flushTable();

    const heading = trimmed.match(/^(#{1,4})\s+(.+)$/);
    if (heading) {
      flushParagraph();
      flushList();
      blocks.push({ type: "heading", level: heading[1].length, text: cleanFormattedText(heading[2]) });
      continue;
    }

    const ordered = trimmed.match(/^(\d+)[.)]\s+(.+)$/);
    if (ordered) {
      flushParagraph();
      if (!listBlock || listBlock.type !== "ol") {
        flushList();
        listBlock = { type: "ol", items: [] };
      }
      listBlock.items.push(cleanFormattedText(ordered[2]));
      continue;
    }

    const unordered = trimmed.match(/^[-*]\s+(.+)$/);
    if (unordered) {
      flushParagraph();
      if (!listBlock || listBlock.type !== "ul") {
        flushList();
        listBlock = { type: "ul", items: [] };
      }
      listBlock.items.push(cleanFormattedText(unordered[1]));
      continue;
    }

    flushList();
    paragraph.push(cleanFormattedText(line));
  }

  flushParagraph();
  flushList();
  flushTable();
  return blocks.length ? blocks : [{ type: "paragraph", lines: ["-"] }];
}

function normalizeFormattedText(text: string): string {
  return text
    .replace(/\r\n/g, "\n")
    .replace(/\s+---\s+/g, "\n\n---\n\n")
    .replace(/\s+(#{1,4}\s+)/g, "\n\n$1")
    .replace(/(#{1,4}\s+[^\n|]+?)\s{2,}(\|)/g, "$1\n$2")
    .replace(/([^\n|])\s{2,}(\|)/g, "$1\n$2")
    .replace(/\|\s+(?=\|)/g, "|\n")
    .replace(/\|\s+(?=(?:---|#{1,4}\s))/g, "|\n\n");
}

function isMarkdownTableLine(line: string): boolean {
  return line.startsWith("|") && line.endsWith("|") && line.split("|").length >= 4;
}

function isMarkdownTableDivider(line: string): boolean {
  const cells = line
    .replace(/^\|/, "")
    .replace(/\|$/, "")
    .split("|")
    .map((cell) => cell.trim());
  return Boolean(cells.length) && cells.every((cell) => /^:?-{3,}:?$/.test(cell));
}

function splitMarkdownTableRow(line: string): string[] {
  return line
    .replace(/^\|/, "")
    .replace(/\|$/, "")
    .split("|")
    .map((cell) => cleanFormattedText(cell.trim()));
}

function parseMarkdownTable(lines: string[]): Extract<FormattedTextBlock, { type: "table" }> | null {
  const rows = lines.filter(isMarkdownTableLine);
  if (rows.length < 2) return null;
  const header = splitMarkdownTableRow(rows[0]);
  const dividerOffset = isMarkdownTableDivider(rows[1]) ? 1 : -1;
  const bodyRows = rows
    .slice(dividerOffset === 1 ? 2 : 1)
    .map(splitMarkdownTableRow)
    .filter((row) => row.some(Boolean));
  if (!header.length || !bodyRows.length) return null;
  return { type: "table", headers: header, rows: bodyRows };
}

function cleanFormattedText(text: string): string {
  return text
    .replace(/\*\*([^*]+)\*\*/g, "$1")
    .replace(/__([^_]+)__/g, "$1")
    .trim();
}

export function jsonPreview(value: unknown): string {
  return JSON.stringify(value, null, 2);
}

// 把任意 JSON 值扁平为「字段: 值」纯文本行（标量数组顿号合并、嵌套缩进、跳过空字段），
// 供 ReadableJsonBody 渲染人/AI 双友好的可框选文本。原始 JSON 仍由 jsonPreview 提供。
export function readableKeyValue(value: unknown): string {
  return renderReadableLines(value, 0).join("\n");
}

function isReadableEmpty(value: unknown): boolean {
  if (value === null || value === undefined) return true;
  if (typeof value === "string") return value.trim() === "";
  if (Array.isArray(value)) return value.every(isReadableEmpty);
  if (typeof value === "object") return Object.values(value as Record<string, unknown>).every(isReadableEmpty);
  return false;
}

function renderReadableLines(value: unknown, depth: number): string[] {
  const pad = "  ".repeat(depth);
  if (isReadableEmpty(value)) return [];
  if (value === null || value === undefined) return [];
  if (typeof value !== "object") return [`${pad}${String(value)}`];
  if (Array.isArray(value)) {
    const lines: string[] = [];
    for (const item of value) {
      if (isReadableEmpty(item)) continue;
      if (item !== null && typeof item === "object") {
        lines.push(`${pad}-`);
        lines.push(...renderReadableLines(item, depth + 1));
      } else {
        lines.push(`${pad}- ${String(item)}`);
      }
    }
    return lines;
  }
  const lines: string[] = [];
  for (const [key, item] of Object.entries(value as Record<string, unknown>)) {
    if (isReadableEmpty(item)) continue;
    if (item !== null && typeof item === "object") {
      const scalarArray = Array.isArray(item) && item.every((entry) => entry === null || typeof entry !== "object");
      if (scalarArray) {
        const joined = (item as unknown[]).filter((entry) => !isReadableEmpty(entry)).map((entry) => String(entry)).join("、");
        lines.push(`${pad}${key}: ${joined}`);
      } else {
        lines.push(`${pad}${key}:`);
        lines.push(...renderReadableLines(item, depth + 1));
      }
    } else {
      lines.push(`${pad}${key}: ${String(item)}`);
    }
  }
  return lines;
}

export function isEmptyJsonValue(value: unknown): boolean {
  if (Array.isArray(value)) return value.length === 0;
  if (value && typeof value === "object") return Object.keys(value as Record<string, unknown>).length === 0;
  return false;
}
