import { X } from "lucide-react";
import { useEffect, useState } from "react";
import type { RuntimeClientConfig } from "../types/runtime";

interface SettingsModalProps {
  open: boolean;
  config: RuntimeClientConfig;
  onClose: () => void;
  onSave: (config: RuntimeClientConfig) => void;
}

export function SettingsModal({ open, config, onClose, onSave }: SettingsModalProps) {
  const [apiBase, setApiBase] = useState(config.apiBase);
  const [apiKey, setApiKey] = useState(config.apiKey);

  useEffect(() => {
    setApiBase(config.apiBase);
    setApiKey(config.apiKey);
  }, [config, open]);

  if (!open) return null;

  return (
    <div className="modal-backdrop">
      <div className="modal-card">
        <div className="modal-head">
          <div>
            <h3>Runtime API 设置</h3>
            <p>默认值来自前端环境变量；Docker 服务由 docker/.env 注入。</p>
          </div>
          <button className="icon-button" onClick={onClose}><X size={18} /></button>
        </div>

        <label className="form-field">
          <span>API Base</span>
          <input value={apiBase} onChange={(e) => setApiBase(e.target.value)} placeholder="http://localhost:58080" />
        </label>
        <label className="form-field">
          <span>API Key</span>
          <input type="password" value={apiKey} onChange={(e) => setApiKey(e.target.value)} placeholder="默认读取 docker/.env 中的 API_KEY" />
        </label>

        <div className="modal-actions">
          <button className="secondary-button" onClick={onClose}>取消</button>
          <button className="primary-button" onClick={() => onSave({ apiBase: apiBase.trim(), apiKey: apiKey.trim() })}>保存并刷新</button>
        </div>
      </div>
    </div>
  );
}
