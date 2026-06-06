import { type FormEvent } from "react";
import { Loader2, RotateCcw, X } from "lucide-react";

export function InstructionModal({
  ariaLabel,
  busy,
  description,
  label,
  placeholder,
  title,
  value,
  onCancel,
  onChange,
  onSubmit,
}: {
  ariaLabel: string;
  busy: boolean;
  description: string;
  label: string;
  placeholder: string;
  title: string;
  value: string;
  onCancel: () => void;
  onChange: (value: string) => void;
  onSubmit: (event: FormEvent<HTMLFormElement>) => void;
}) {
  return (
    <div className="modal-backdrop" role="presentation" onClick={() => !busy && onCancel()}>
      <form
        className="modal-card fw-proposal-regenerate-modal"
        role="dialog"
        aria-modal="true"
        aria-label={ariaLabel}
        onClick={(event) => event.stopPropagation()}
        onSubmit={onSubmit}
      >
        <header className="modal-head">
          <div>
            <h3>{title}</h3>
            <p>{description}</p>
          </div>
          <button className="mini-icon-button" type="button" onClick={onCancel} aria-label="关闭" disabled={busy}>
            <X size={16} />
          </button>
        </header>
        <label className="form-field">
          <span>{label}</span>
          <textarea maxLength={2000} placeholder={placeholder} value={value} onChange={(event) => onChange(event.target.value)} />
        </label>
        <div className="fw-modal-inline-meta">
          <span>{value.length}/2000</span>
        </div>
        <div className="modal-actions">
          <button className="fw-small-secondary" type="button" onClick={onCancel} disabled={busy}>
            取消
          </button>
          <button className="fw-small-primary" type="submit" disabled={busy}>
            {busy ? <Loader2 size={16} className="fw-spin" /> : <RotateCcw size={16} />}
            重新生成
          </button>
        </div>
      </form>
    </div>
  );
}
