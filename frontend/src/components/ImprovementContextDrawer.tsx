import { CONTEXT_TYPE_LABEL, type ContextType } from "../contextPackage";

const CONTEXT_TYPES: ContextType[] = ["problem", "ai", "playwright", "json"];

export function ImprovementContextDrawer({
  text,
  contextType,
  onContextTypeChange,
  onCopy,
  onDownload,
}: {
  text: string;
  contextType: ContextType;
  onContextTypeChange: (value: ContextType) => void;
  onCopy: () => void;
  onDownload: () => void;
}) {
  return (
    <div className="iw-context-drawer" data-testid="context-drawer" data-state="open">
      <div className="iw-context-head">
        <span>上下文包</span>
        <div className="iw-context-head-actions">
          <button className="iw-secondary-button" type="button" data-testid="context-copy" onClick={onCopy}>复制</button>
          <button className="iw-secondary-button" type="button" data-testid="context-download" onClick={onDownload}>下载</button>
        </div>
      </div>
      <div className="iw-context-types" role="radiogroup" aria-label="上下文类型">
        {CONTEXT_TYPES.map((type) => (
          <label key={type} className={`iw-context-type ${contextType === type ? "active" : ""}`} data-testid={`context-type-${type}`}>
            <input type="radio" name="iw-context-type" checked={contextType === type} onChange={() => onContextTypeChange(type)} />
            {CONTEXT_TYPE_LABEL[type]}
          </label>
        ))}
      </div>
      <pre className="iw-context-body" data-testid="context-preview">{text}</pre>
    </div>
  );
}
