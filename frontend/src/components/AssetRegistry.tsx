import { useCallback, useEffect, useState } from "react";
import { createAsset, inheritAsset, listAssets, type Asset } from "../api/assets";
import type { AgentSummary, RuntimeClientConfig } from "../types/runtime";
import "../improvement-workbench.css";

// 治理资产 Registry 复利中心（v2.7 W3）：沉淀方法论/回归/执行/审计资产，并跨业务 Agent 继承复用。
const ASSET_TYPE_LABEL: Record<string, string> = {
  methodology: "方法论",
  regression: "回归",
  execution: "执行",
  audit: "审计",
};

export function AssetRegistry({
  clientConfig,
  scopeAgentId,
  businessAgents,
}: {
  clientConfig: RuntimeClientConfig;
  scopeAgentId: string;
  businessAgents: AgentSummary[];
}) {
  const [assets, setAssets] = useState<Asset[]>([]);
  const [error, setError] = useState<string | undefined>();
  const [busy, setBusy] = useState(false);
  const [newType, setNewType] = useState("methodology");
  const [newTitle, setNewTitle] = useState("");
  const [newBody, setNewBody] = useState("");
  const [inheritTarget, setInheritTarget] = useState<Record<string, string>>({});

  const refresh = useCallback(async () => {
    setError(undefined);
    try {
      setAssets(await listAssets(clientConfig, { agentId: scopeAgentId || undefined }));
    } catch (e) {
      setError(e instanceof Error ? e.message : String(e));
    }
  }, [clientConfig, scopeAgentId]);

  useEffect(() => {
    void refresh();
  }, [refresh]);

  const run = async (action: () => Promise<void>) => {
    setBusy(true);
    setError(undefined);
    try {
      await action();
    } catch (e) {
      setError(e instanceof Error ? e.message : String(e));
    } finally {
      setBusy(false);
    }
  };

  const handleCreate = () => {
    const title = newTitle.trim();
    const agentId = scopeAgentId || businessAgents[0]?.agent_id || "";
    if (!title || !agentId || busy) return;
    void run(async () => {
      await createAsset(clientConfig, { agent_id: agentId, asset_type: newType, title, body: newBody, source_improvement_id: "" });
      setNewTitle("");
      setNewBody("");
      await refresh();
    });
  };

  const handleInherit = (asset: Asset) => {
    const target = inheritTarget[asset.asset_id];
    if (!target || busy) return;
    void run(async () => {
      await inheritAsset(clientConfig, asset.asset_id, target);
      await refresh();
    });
  };

  const agentName = (agentId: string) => businessAgents.find((a) => a.agent_id === agentId)?.name || agentId;

  return (
    <div className="improvement-workbench" data-testid="asset-registry" style={{ gridTemplateColumns: "minmax(0, 1fr)" }}>
      <section className="iw-detail-panel">
        <div className="iw-panel-head">
          <h3>资产 Registry 复利中心{scopeAgentId ? ` · ${agentName(scopeAgentId)}` : "（全部业务 Agent）"}</h3>
          <button className="iw-secondary-button" type="button" disabled={busy} onClick={() => void refresh()}>刷新</button>
        </div>
        <div className="iw-panel-body">
          {error ? <div className="iw-error" data-testid="asset-error">{error}</div> : null}

          <div className="iw-detail-section">
            <h4>沉淀新资产</h4>
            <div className="iw-automation-row">
              <select className="iw-select select-inline" data-testid="asset-create-type" value={newType} disabled={busy} onChange={(e) => setNewType(e.target.value)}>
                <option value="methodology">方法论</option>
                <option value="regression">回归</option>
                <option value="execution">执行</option>
                <option value="audit">审计</option>
              </select>
              <input className="iw-input" data-testid="asset-create-title" style={{ width: "auto", flex: 1, minWidth: 160 }} placeholder="资产标题" value={newTitle} disabled={busy} onChange={(e) => setNewTitle(e.target.value)} />
              <button className="iw-primary-button" type="button" data-testid="asset-create-submit" disabled={busy || !newTitle.trim()} onClick={handleCreate}>沉淀</button>
            </div>
            <textarea className="iw-input" data-testid="asset-create-body" style={{ marginTop: 8, minHeight: 60 }} placeholder="资产正文（方法论步骤 / 回归用例 / 执行脚本 / 审计说明）" value={newBody} disabled={busy} onChange={(e) => setNewBody(e.target.value)} />
          </div>

          <div className="iw-detail-section">
            <h4>资产清单（{assets.length}）</h4>
            {assets.length === 0 ? (
              <div className="iw-empty">当前范围还没有沉淀资产。</div>
            ) : (
              assets.map((asset) => {
                const targets = businessAgents.filter((a) => a.agent_id !== asset.agent_id);
                return (
                  <div className="iw-list-item" data-testid="asset-item" data-asset-type={asset.asset_type} key={asset.asset_id}>
                    <span className="iw-list-item-title">
                      {asset.title}
                      {asset.inherited_from ? <span className="iw-ref" data-testid="asset-inherited" style={{ marginLeft: 8 }}>继承</span> : null}
                    </span>
                    <span className="iw-list-item-meta">{ASSET_TYPE_LABEL[asset.asset_type] ?? asset.asset_type} · {agentName(asset.agent_id)}</span>
                    <div className="iw-automation-row" style={{ marginTop: 6 }}>
                      <select
                        className="iw-select select-inline"
                        data-testid="asset-inherit-target"
                        value={inheritTarget[asset.asset_id] ?? ""}
                        disabled={busy || targets.length === 0}
                        onChange={(e) => setInheritTarget((prev) => ({ ...prev, [asset.asset_id]: e.target.value }))}
                      >
                        <option value="">继承到…业务 Agent</option>
                        {targets.map((a) => (
                          <option key={a.agent_id} value={a.agent_id}>{a.name}</option>
                        ))}
                      </select>
                      <button
                        className="iw-secondary-button"
                        type="button"
                        data-testid="asset-inherit-submit"
                        disabled={busy || !inheritTarget[asset.asset_id]}
                        onClick={() => handleInherit(asset)}
                      >
                        继承复用
                      </button>
                    </div>
                  </div>
                );
              })
            )}
          </div>
        </div>
      </section>
    </div>
  );
}
