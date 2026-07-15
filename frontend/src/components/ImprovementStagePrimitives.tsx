import type { ReactNode } from "react";
import { operationStatusText, type ImprovementPendingOperation } from "../improvementOperationState";

export function Dl({ rows }: { rows: [string, ReactNode][] }) {
  return (
    <dl className="iw-compact-dl">
      {rows.map(([key, value]) => <div key={key}><dt>{key}</dt><dd>{value}</dd></div>)}
    </dl>
  );
}

export function Lines({ items, empty }: { items: string[]; empty: string }) {
  if (!items.length) return <div className="iw-empty">{empty}</div>;
  return <ul className="iw-check-list">{items.map((item, index) => <li key={`${index}-${item}`}>{item}</li>)}</ul>;
}

export function GenerationStatus({ operation, testId }: { operation: ImprovementPendingOperation; testId: string }) {
  return <div className="iw-operation-status" data-testid={testId}>{operationStatusText(operation)}</div>;
}

export function GenerationError({ message, testId }: { message: string; testId: string }) {
  return <div className="iw-operation-error" data-testid={testId}><strong>生成失败：</strong>{message}</div>;
}
