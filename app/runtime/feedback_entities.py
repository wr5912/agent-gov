"""业务对象引用的共享类型；与 AgentGov 的 feedback_case_id 生命周期独立。"""

from __future__ import annotations

from collections.abc import Iterable, Mapping
from typing import Annotated, TypeAlias

from pydantic import AfterValidator, BeforeValidator, StringConstraints, TypeAdapter
from sqlalchemy import ColumnElement, func, select, true

from .errors import BusinessRuleViolation

EntityIdentifier = Annotated[str, StringConstraints(strip_whitespace=True, min_length=1)]


def _canonicalize_entities(value: object) -> object:
    """Trim identifiers before dict materialization and merge canonical key collisions."""
    if not isinstance(value, Mapping):
        return value
    canonical: dict[object, object] = {}
    for raw_kind, raw_identifiers in value.items():
        kind = raw_kind.strip() if isinstance(raw_kind, str) else raw_kind
        if not isinstance(raw_identifiers, list):
            canonical[kind] = raw_identifiers
            continue
        identifiers = [identifier.strip() if isinstance(identifier, str) else identifier for identifier in raw_identifiers]
        if kind not in canonical:
            canonical[kind] = identifiers
            continue
        existing = canonical[kind]
        # 同名键只要任一分支不是 list，就保留非法值交给 Pydantic
        # 拒绝；不能让 JSON 键顺序决定非法输入是否被接受。
        if isinstance(existing, list):
            canonical[kind] = [*existing, *identifiers]
    return canonical


def _unique_entities(value: FeedbackEntities) -> FeedbackEntities:
    return {key: list(dict.fromkeys(ids)) for key, ids in value.items() if ids}


FeedbackEntities: TypeAlias = Annotated[
    dict[EntityIdentifier, list[EntityIdentifier]],
    BeforeValidator(_canonicalize_entities),
    AfterValidator(_unique_entities),
]
_ENTITIES = TypeAdapter(FeedbackEntities)


def parse_entities(value: object) -> FeedbackEntities:
    """仅在 JSON/持久化边界校验，不猜测旧的业务字段。"""
    return _ENTITIES.validate_python(value if value is not None else {})


def merge_entities(values: Iterable[Mapping[str, list[str]]]) -> FeedbackEntities:
    result: FeedbackEntities = {}
    for entities in values:
        for kind, identifiers in entities.items():
            result[kind] = list(dict.fromkeys([*result.get(kind, []), *identifiers]))
    return result


def entities_overlap(left: Mapping[str, list[str]], right: Mapping[str, list[str]]) -> bool:
    return any(set(ids).intersection(right.get(kind, [])) for kind, ids in left.items())


def entity_filter(column: ColumnElement, entity_type: str | None, entity_id: str | None) -> ColumnElement[bool]:
    """SQLite JSON 精确类型/值匹配，不使用 LIKE 或把业务值插入 JSON 路径。"""
    if (entity_type is None) != (entity_id is None) or (entity_type is not None and (not entity_type.strip() or not entity_id or not entity_id.strip())):
        raise BusinessRuleViolation("entity_type 与 entity_id 必须成对且非空。")
    if entity_type is None:
        return true()
    entity_type = entity_type.strip()
    entity_id = entity_id.strip()
    kinds = func.json_each(column).table_valued("key", "value", joins_implicitly=True)
    ids = func.json_each(kinds.c.value).table_valued("value", joins_implicitly=True)
    return select(1).select_from(kinds, ids).where(kinds.c.key == entity_type, ids.c.value == entity_id).exists()
