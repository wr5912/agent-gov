import type { ReactNode } from "react";

export function StageCard({
  letter,
  title,
  actionLabel,
  testId,
  className,
  onAction,
  children,
}: {
  letter: string;
  title: string;
  actionLabel?: string;
  testId?: string;
  className?: string;
  onAction?: () => void;
  children: ReactNode;
}) {
  return (
    <section className={`iw-stage-card${className ? ` ${className}` : ""}`} data-testid={testId}>
      <div className="iw-stage-card-head">
        <h4><span>{letter}</span>{title}</h4>
        {actionLabel && onAction ? <button className="iw-link-button" type="button" onClick={onAction}>{actionLabel}</button> : null}
      </div>
      {children}
    </section>
  );
}
