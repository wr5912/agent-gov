from __future__ import annotations

import json
from collections.abc import Callable
from typing import Any

from .a2ui_bridge import (
    A2UI_CARD_TOOL_NAME,
    A2UI_RENDER_TOOL_NAME,
    A2uiToolPayload,
    a2ui_payload_surface_ids,
    asset_risk_result_payload,
    asset_risk_skeleton_payload,
    asset_risk_status_payload,
    asset_risk_surface_id,
    generic_progressive_skeleton_payload,
    generic_progressive_status_payload,
    generic_progressive_surface_id,
    retarget_a2ui_payload,
)


class A2UIProgressiveOrchestrator:
    """Owns A2UI progressive surface state for a single AG-UI run."""

    def __init__(
        self,
        *,
        run_id: str,
        walk_records: Callable[[Any], list[dict[str, Any]]],
        tool_result_from_record: Callable[[dict[str, Any]], dict[str, Any] | None],
        string_value: Callable[[Any], str | None],
        is_tool_result_error: Callable[[dict[str, Any]], bool],
    ) -> None:
        self.run_id = run_id
        self._walk_records = walk_records
        self._tool_result_from_record = tool_result_from_record
        self._string_value = string_value
        self._is_tool_result_error = is_tool_result_error

        self.asset_surface_id: str | None = None
        self.asset_started = False
        self.asset_completed = False
        self.asset_records: list[dict[str, Any]] = []
        self.asset_final_ui_emitted = False

        self.generic_surface_id: str | None = None
        self.generic_started = False
        self.generic_completed = False
        self.generic_final_ui_emitted = False

    def handle_activity(self, activity: dict[str, Any]) -> list[dict[str, Any]]:
        tool_name = self._string_value(activity.get("toolName")) or ""
        if is_asset_tool_name(tool_name):
            return self._handle_asset_activity(activity)
        if _is_generic_tool_name(tool_name):
            return self._handle_generic_activity(activity)
        return []

    def handle_tool_results(
        self,
        raw_message: Any,
        tool_context: dict[str, dict[str, str]],
    ) -> list[dict[str, Any]]:
        payloads: list[dict[str, Any]] = []
        for result in self._tool_results_from_raw(raw_message, tool_context):
            tool_name = self._tool_name_for_result(result, tool_context)
            if is_asset_tool_name(tool_name):
                payloads.extend(self._handle_asset_result(result))
            elif _is_generic_tool_name(tool_name):
                payloads.extend(self._handle_generic_result(result))
        return payloads

    def retarget_tool_payload(self, tool_payload: A2uiToolPayload) -> dict[str, Any]:
        payload = tool_payload.payload
        surface_id = self._active_surface_id()
        if not surface_id:
            return payload

        surface_ids = a2ui_payload_surface_ids(payload)
        if surface_ids == [surface_id]:
            self._mark_final_ui_emitted()
            return payload

        if not _is_retargetable_tool_payload(tool_payload) or not surface_ids:
            return payload

        self._mark_final_ui_emitted()
        return retarget_a2ui_payload(payload, surface_id)

    def final_payloads(self) -> list[dict[str, Any]]:
        payloads: list[dict[str, Any]] = []
        if self.asset_started and not self.asset_completed and not self.asset_final_ui_emitted and self.asset_surface_id:
            self.asset_completed = True
            if self.asset_records:
                payloads.append(
                    asset_risk_result_payload(
                        self.asset_surface_id,
                        self.asset_records,
                        completed=True,
                    )
                )
            else:
                payloads.append(
                    asset_risk_status_payload(
                        self.asset_surface_id,
                        "资产风险视图已完成，未获得可展示的资产明细。",
                    )
                )

        if (
            self.generic_started
            and not self.generic_completed
            and not self.generic_final_ui_emitted
            and self.generic_surface_id
        ):
            self.generic_completed = True
            payloads.append(
                generic_progressive_status_payload(
                    self.generic_surface_id,
                    "Agent 工具调用已完成，正在等待最终回复。",
                )
            )
        return payloads

    def _handle_asset_activity(self, activity: dict[str, Any]) -> list[dict[str, Any]]:
        self._ensure_asset_surface()
        if activity.get("status") == "running" and not self.asset_started:
            self.asset_started = True
            return [
                asset_risk_skeleton_payload(self.asset_surface_id or ""),
                asset_risk_status_payload(self.asset_surface_id or "", "正在查询资产数据"),
            ]
        if activity.get("status") == "error" and self.asset_started:
            return [
                asset_risk_status_payload(
                    self.asset_surface_id or "",
                    "资产数据查询失败，已保留当前文本回复。",
                )
            ]
        return []

    def _handle_asset_result(self, result: dict[str, Any]) -> list[dict[str, Any]]:
        self._ensure_asset_surface()
        payloads: list[dict[str, Any]] = []
        if not self.asset_started:
            self.asset_started = True
            payloads.append(asset_risk_skeleton_payload(self.asset_surface_id or ""))

        if self._is_tool_result_error(result):
            payloads.append(
                asset_risk_status_payload(
                    self.asset_surface_id or "",
                    "资产数据查询失败，已保留当前文本回复。",
                )
            )
            return payloads

        assets = asset_records_from_value(result.get("content"))
        if assets:
            self.asset_records = assets
            payloads.append(
                asset_risk_result_payload(
                    self.asset_surface_id or "",
                    self.asset_records,
                    completed=False,
                )
            )
        else:
            payloads.append(
                asset_risk_status_payload(
                    self.asset_surface_id or "",
                    "已完成资产查询，但未返回可结构化展示的资产记录。",
                )
            )
        return payloads

    def _handle_generic_activity(self, activity: dict[str, Any]) -> list[dict[str, Any]]:
        status = activity.get("status")
        if status == "running" and not self.generic_started and not self.asset_started:
            self.generic_surface_id = generic_progressive_surface_id(self.run_id)
            self.generic_started = True
            label = self._string_value(activity.get("label")) or "Agent 工具调用"
            return [
                generic_progressive_skeleton_payload(
                    self.generic_surface_id,
                    "Agent 工作进度",
                    f"{label}中",
                )
            ]
        if status == "error" and self.generic_started and not self.generic_completed and self.generic_surface_id:
            self.generic_completed = True
            return [
                generic_progressive_status_payload(
                    self.generic_surface_id,
                    "工具调用失败，已保留当前文本回复。",
                )
            ]
        return []

    def _handle_generic_result(self, result: dict[str, Any]) -> list[dict[str, Any]]:
        if not self.generic_started or self.generic_completed or not self.generic_surface_id:
            return []
        if self._is_tool_result_error(result):
            self.generic_completed = True
            return [
                generic_progressive_status_payload(
                    self.generic_surface_id,
                    "工具调用失败，已保留当前文本回复。",
                )
            ]
        return [
            generic_progressive_status_payload(
                self.generic_surface_id,
                "工具调用已返回结果，正在生成最终展示。",
            )
        ]

    def _tool_results_from_raw(
        self,
        raw_message: Any,
        tool_context: dict[str, dict[str, str]],
    ) -> list[dict[str, Any]]:
        results: list[dict[str, Any]] = []
        for record in self._walk_records(raw_message):
            tool_result = self._tool_result_from_record(record)
            if not tool_result:
                continue
            tool_name = self._tool_name_for_result(tool_result, tool_context)
            if (is_asset_tool_name(tool_name) or _is_generic_tool_name(tool_name)) and "content" in tool_result:
                results.append(tool_result)
        return results

    def _tool_name_for_result(
        self,
        result: dict[str, Any],
        tool_context: dict[str, dict[str, str]],
    ) -> str:
        tool_use_id = self._string_value(result.get("tool_use_id"))
        context = tool_context.get(tool_use_id or "", {})
        return self._string_value(result.get("name")) or context.get("toolName") or ""

    def _ensure_asset_surface(self) -> None:
        if self.asset_surface_id is None:
            self.asset_surface_id = asset_risk_surface_id(self.run_id)

    def _active_surface_id(self) -> str | None:
        if self.asset_started and self.asset_surface_id:
            return self.asset_surface_id
        if self.generic_started and self.generic_surface_id:
            return self.generic_surface_id
        return None

    def _mark_final_ui_emitted(self) -> None:
        if self.asset_started:
            self.asset_final_ui_emitted = True
            self.asset_completed = True
        elif self.generic_started:
            self.generic_final_ui_emitted = True
            self.generic_completed = True


def is_asset_tool_name(tool_name: str) -> bool:
    lowered = tool_name.lower()
    return "list_assets" in lowered or "_assets_" in lowered


def asset_records_from_value(value: Any) -> list[dict[str, Any]]:
    if isinstance(value, str):
        try:
            return asset_records_from_value(json.loads(value))
        except json.JSONDecodeError:
            return [{"asset": value}] if value.strip() else []

    if isinstance(value, list):
        records: list[dict[str, Any]] = []
        for item in value:
            records.extend(asset_records_from_value(item))
        return records

    if not isinstance(value, dict):
        return []

    if str(value.get("type") or "") == "text" and isinstance(value.get("text"), str):
        return asset_records_from_value(value["text"])

    for key in ("items", "assets", "data", "results"):
        nested = value.get(key)
        if isinstance(nested, list):
            return asset_records_from_value(nested)

    asset = value.get("asset")
    if isinstance(asset, str):
        return [{"asset": asset, **{key: item for key, item in value.items() if key != "asset"}}]
    if isinstance(asset, dict):
        return [asset]

    if any(key in value for key in ("assetId", "asset_id", "hostname", "name", "id", "host")):
        return [value]
    return []


def _is_generic_tool_name(tool_name: str) -> bool:
    if not tool_name:
        return False
    if is_asset_tool_name(tool_name):
        return False
    if tool_name.startswith("mcp__ai-soc-ui__"):
        return False
    if tool_name == "Skill" or tool_name.startswith("Skill("):
        return False
    return True


def _is_retargetable_tool_payload(tool_payload: A2uiToolPayload) -> bool:
    return tool_payload.tool_name == A2UI_CARD_TOOL_NAME or (
        tool_payload.tool_name == A2UI_RENDER_TOOL_NAME and tool_payload.mode in {"card", "cards", "catalog"}
    )
