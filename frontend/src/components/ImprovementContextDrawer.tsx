import { CONTEXT_TYPE_LABEL, type ContextType } from "../contextPackage";
import { DrawerShell } from "./DrawerShell";

const CONTEXT_TYPES: ContextType[] = ["problem", "ai", "playwright", "json"];

export function ImprovementContextDrawer({
  text,
  contextType,
  onContextTypeChange,
  onCopy,
  onDownload,
  onClose,
}: {
  text: string;
  contextType: ContextType;
  onContextTypeChange: (value: ContextType) => void;
  onCopy: () => void;
  onDownload: () => void;
  onClose: () => void;
}) {
  return (
    <DrawerShell
      title="上下文包"
      description="按不同用途生成可复制、可下载的改进事项上下文。"
      size="medium"
      testId="context-drawer"
      dataState="open"
      className="context-drawer"
      bodyClassName="context-drawer-body"
      onClose={onClose}
      headerActions={(
        <>
          <button className="secondary-button drawer-header-link" type="button" data-testid="context-copy" onClick={onCopy}>复制</button>
          <button className="secondary-button drawer-header-link" type="button" data-testid="context-download" onClick={onDownload}>下载</button>
        </>
      )}
    >
      <div className="iw-context-types" role="radiogroup" aria-label="上下文类型">
        {CONTEXT_TYPES.map((type) => (
          <label key={type} className={`iw-context-type ${contextType === type ? "active" : ""}`} data-testid={`context-type-${type}`}>
            <input type="radio" name="iw-context-type" checked={contextType === type} onChange={() => onContextTypeChange(type)} />
            {CONTEXT_TYPE_LABEL[type]}
          </label>
        ))}
      </div>
      <pre className="iw-context-body" data-testid="context-preview">{text}</pre>
    </DrawerShell>
  );
}
