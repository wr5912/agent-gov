from __future__ import annotations

from dataclasses import dataclass

from app.runtime.state_machines import IMPROVEMENT_STAGE_ORDER
from app.runtime.stores.improvement_store import ImprovementItemRecord, ImprovementStore

# 自动推进边分类（确定性，沿真实 improvement_stage 状态机）：
# AUTO：系统可自动完成的过渡（规范化、归因执行、回归执行）。
# GATE：关键人工判断点（确认归因→生成方案、确认方案→执行）；semi 停下等确认，full 自动通过。
# release 入口（regression→release）属发布门禁，任何模式都不自动，保持人工。
_AUTO_EDGES = {
    ("feedback_intake", "triage"),
    ("triage", "attribution"),
    ("execution", "regression"),
}
_GATE_EDGES = {
    ("attribution", "optimization"),
    ("optimization", "execution"),
}


def _next_stage(stage: str) -> str | None:
    try:
        idx = IMPROVEMENT_STAGE_ORDER.index(stage)
    except ValueError:
        return None
    if idx + 1 >= len(IMPROVEMENT_STAGE_ORDER):
        return None
    return IMPROVEMENT_STAGE_ORDER[idx + 1]


@dataclass(frozen=True)
class AutoAdvanceResult:
    item: ImprovementItemRecord
    applied_stages: list[str]
    stopped_reason: str  # policy_off / archived / gate_confirmation / release_gate / terminal


def auto_advance(store: ImprovementStore, *, mode: str, item: ImprovementItemRecord) -> AutoAdvanceResult:
    """按策略 mode 自动推进一个改进事项；推进交由 store.transition_stage（状态机校验）。

    幂等且确定性：off 不动；semi 推进 AUTO 段、遇 GATE 停；full 额外通过 GATE，遇 release 门禁停。
    """
    if mode == "off":
        return AutoAdvanceResult(item=item, applied_stages=[], stopped_reason="policy_off")
    if item.improvement_status == "archived":
        return AutoAdvanceResult(item=item, applied_stages=[], stopped_reason="archived")

    applied: list[str] = []
    current = item
    stage = current.improvement_stage
    while True:
        nxt = _next_stage(stage)
        if nxt is None:
            return AutoAdvanceResult(item=current, applied_stages=applied, stopped_reason="terminal")
        edge = (stage, nxt)
        if edge in _AUTO_EDGES or (edge in _GATE_EDGES and mode == "full"):
            current = store.transition_stage(current.improvement_id, stage=nxt)
            applied.append(nxt)
            stage = nxt
            continue
        stopped = "gate_confirmation" if edge in _GATE_EDGES else "release_gate"
        return AutoAdvanceResult(item=current, applied_stages=applied, stopped_reason=stopped)
