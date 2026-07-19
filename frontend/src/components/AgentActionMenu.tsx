import { Download, Trash2, Upload } from "lucide-react";
import {
  useCallback,
  useEffect,
  useLayoutEffect,
  useRef,
  useState,
  type KeyboardEvent as ReactKeyboardEvent,
} from "react";
import { createPortal } from "react-dom";
import type { AgentSummary } from "../types/runtime";

interface AgentActionMenuProps {
  anchor: HTMLButtonElement;
  agent: AgentSummary;
  disabled: boolean;
  onClose: () => void;
  onExport: () => void;
  onOverwrite: () => void;
  onDelete: () => void;
}

function useAnchoredMenu(anchor: HTMLButtonElement, onClose: () => void) {
  const menuRef = useRef<HTMLDivElement | null>(null);
  const [position, setPosition] = useState({ top: 0, left: 0, ready: false });
  const updatePosition = useCallback(() => {
    if (!anchor.isConnected) return onClose();
    const rect = anchor.getBoundingClientRect();
    const menuWidth = menuRef.current?.offsetWidth || 216;
    const menuHeight = menuRef.current?.offsetHeight || 132;
    const gap = 6;
    const inset = 8;
    const roomBelow = window.innerHeight - rect.bottom - inset;
    const top = roomBelow >= menuHeight + gap ? rect.bottom + gap : Math.max(inset, rect.top - menuHeight - gap);
    const left = Math.min(window.innerWidth - menuWidth - inset, Math.max(inset, rect.right - menuWidth));
    setPosition({ top, left, ready: true });
  }, [anchor, onClose]);

  useLayoutEffect(() => {
    updatePosition();
  }, [updatePosition]);
  useEffect(() => {
    const focusFrame = window.requestAnimationFrame(() => {
      menuRef.current?.querySelector<HTMLButtonElement>('[role="menuitem"]:not(:disabled)')?.focus();
    });
    const handlePointerDown = (event: PointerEvent) => {
      const target = event.target as Node;
      if (menuRef.current?.contains(target) || anchor.contains(target)) return;
      onClose();
    };
    const handleEscape = (event: KeyboardEvent) => {
      if (event.key !== "Escape") return;
      event.preventDefault();
      onClose();
      anchor.focus();
    };
    const handleViewportChange = () => onClose();
    document.addEventListener("pointerdown", handlePointerDown);
    document.addEventListener("keydown", handleEscape);
    document.addEventListener("scroll", handleViewportChange, true);
    window.addEventListener("resize", handleViewportChange);
    return () => {
      window.cancelAnimationFrame(focusFrame);
      document.removeEventListener("pointerdown", handlePointerDown);
      document.removeEventListener("keydown", handleEscape);
      document.removeEventListener("scroll", handleViewportChange, true);
      window.removeEventListener("resize", handleViewportChange);
    };
  }, [anchor, onClose]);
  return { menuRef, position };
}

function moveMenuFocus(event: ReactKeyboardEvent<HTMLDivElement>, menu: HTMLDivElement | null) {
  if (!["ArrowDown", "ArrowUp", "Home", "End"].includes(event.key)) return;
  event.preventDefault();
  const items = Array.from(menu?.querySelectorAll<HTMLButtonElement>('[role="menuitem"]:not(:disabled)') ?? []);
  if (!items.length) return;
  const current = items.indexOf(document.activeElement as HTMLButtonElement);
  if (event.key === "Home") return items[0].focus();
  if (event.key === "End") return items[items.length - 1].focus();
  const delta = event.key === "ArrowDown" ? 1 : -1;
  items[(current + delta + items.length) % items.length].focus();
}

export function AgentActionMenu(props: AgentActionMenuProps) {
  const { menuRef, position } = useAnchoredMenu(props.anchor, props.onClose);
  return createPortal(
    <div
      ref={menuRef}
      id={`settings-agent-actions-menu-${props.agent.agent_id}`}
      className="settings-agent-actions-menu"
      data-testid="settings-agent-actions-menu"
      role="menu"
      aria-label={`${props.agent.name} 操作`}
      style={{ top: position.top, left: position.left, visibility: position.ready ? "visible" : "hidden" }}
      onKeyDown={(event) => moveMenuFocus(event, menuRef.current)}
    >
      <button type="button" role="menuitem" data-testid="settings-agent-export" disabled={props.disabled} onClick={props.onExport}>
        <Download size={15} /><span>导出 Workspace</span>
      </button>
      <button type="button" role="menuitem" data-testid="settings-agent-overwrite" disabled={props.disabled} onClick={props.onOverwrite}>
        <Upload size={15} /><span>覆盖导入…</span>
      </button>
      <div className="settings-agent-actions-separator" role="separator" />
      <button
        className="is-danger"
        type="button"
        role="menuitem"
        data-testid="settings-agent-delete"
        disabled={props.disabled || props.agent.protected}
        title={props.agent.protected ? "该业务 Agent 受保护，只能经仓库变更移除" : undefined}
        onClick={props.onDelete}
      >
        <Trash2 size={15} />
        <span>删除 Agent</span>
        {props.agent.protected ? <small>受保护</small> : null}
      </button>
    </div>,
    document.body,
  );
}
