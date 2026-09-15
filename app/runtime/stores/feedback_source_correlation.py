"""反馈与运行的确定性关联；多轮会话或多对象命中时不按时间猜测。"""

from sqlalchemy import select
from sqlalchemy.orm import Session

from ..errors import BusinessRuleViolation
from ..feedback_entities import FeedbackEntities, entities_overlap, parse_entities
from ..json_types import JsonObject
from ..runtime_db import AgentRunModel, FeedbackEventModel, FeedbackSignalModel


def find_source_run(db: Session, source: JsonObject) -> AgentRunModel | None:
    run_id = str(source.get("run_id") or "").strip()
    session_id = str(source.get("session_id") or "").strip()
    if run_id:
        row = db.get(AgentRunModel, run_id)
        if row is None or (session_id and row.session_id != session_id):
            raise BusinessRuleViolation("run_id 必须存在且属于所指定的 Session。")
        return row
    entities = parse_entities(source.get("entities"))
    stmt = select(AgentRunModel)
    if session_id:
        stmt = stmt.where(AgentRunModel.session_id == session_id)
    elif not entities:
        return None
    runs = list(db.scalars(stmt).all())
    if session_id and len(runs) == 1:
        return runs[0]
    if not entities:
        return None
    referenced = _source_entity_run_ids(db, entities)
    matches = [row for row in runs if row.run_id in referenced or entities_overlap(entities, row.entities_json or {})]
    return matches[0] if len(matches) == 1 else None


def _source_entity_run_ids(db: Session, entities: FeedbackEntities) -> set[str]:
    run_ids: set[str] = set()
    for model in (FeedbackSignalModel, FeedbackEventModel):
        # 人工纠正来源归属后仍保留历史 Run 引用；它不能再替原 Agent 推断新反馈归属。
        stmt = select(model).join(AgentRunModel, model.matched_run_id == AgentRunModel.run_id).where(model.agent_id == AgentRunModel.agent_id)
        for row in db.scalars(stmt):
            if entities_overlap(entities, parse_entities((row.payload_json or {}).get("entities"))):
                run_ids.add(str(row.matched_run_id))
    return run_ids
