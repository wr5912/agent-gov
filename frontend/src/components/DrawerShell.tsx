import { X } from "lucide-react";
import type { ReactNode } from "react";

export type DrawerSize = "narrow" | "medium" | "wide";

interface DrawerShellProps {
  title: string;
  description?: string;
  size: DrawerSize;
  testId: string;
  dataState?: string;
  className?: string;
  bodyClassName?: string;
  headerMeta?: ReactNode;
  headerActions?: ReactNode;
  closeDisabled?: boolean;
  children: ReactNode;
  onClose: () => void;
}

export function DrawerShell({
  title,
  description,
  size,
  testId,
  dataState,
  className = "",
  bodyClassName = "",
  headerMeta,
  headerActions,
  closeDisabled = false,
  children,
  onClose,
}: DrawerShellProps) {
  const titleId = `${testId}-title`;
  return (
    <div className="drawer-backdrop" role="presentation" onClick={closeDisabled ? undefined : onClose}>
      <aside
        className={`drawer-shell drawer-shell-${size} ${className}`.trim()}
        data-testid={testId}
        data-size={size}
        data-state={dataState}
        role="dialog"
        aria-modal="true"
        aria-labelledby={titleId}
        onClick={(event) => event.stopPropagation()}
      >
        <header className="drawer-shell-head">
          <div className="drawer-shell-title">
            <h3 id={titleId}>{title}</h3>
            {description ? <p>{description}</p> : null}
            {headerMeta ? <div className="drawer-shell-meta">{headerMeta}</div> : null}
          </div>
          <div className="drawer-shell-actions">
            {headerActions}
            <button className="icon-button" type="button" disabled={closeDisabled} onClick={onClose} aria-label="关闭">
              <X size={18} />
            </button>
          </div>
        </header>
        <div className={`drawer-shell-body ${bodyClassName}`.trim()}>{children}</div>
      </aside>
    </div>
  );
}
