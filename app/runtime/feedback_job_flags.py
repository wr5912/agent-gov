from __future__ import annotations

from .json_types import JsonObject


REUSED_EXISTING_FLAG = "_reused_existing"
NO_ACTIONABLE_ATTRIBUTIONS_FLAG = "_no_actionable_attributions"


def with_reused_existing(record: JsonObject) -> JsonObject:
    return {**record, REUSED_EXISTING_FLAG: True}


def no_actionable_attributions(batch_id: str) -> JsonObject:
    return {NO_ACTIONABLE_ATTRIBUTIONS_FLAG: True, "batch_id": batch_id}


def reused_existing(record: JsonObject | None) -> bool:
    return bool(record and record.get(REUSED_EXISTING_FLAG))


def has_no_actionable_attributions(record: JsonObject | None) -> bool:
    return bool(record and record.get(NO_ACTIONABLE_ATTRIBUTIONS_FLAG))
