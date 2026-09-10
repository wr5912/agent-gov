from __future__ import annotations

import hashlib
import json
import uuid
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any

from app.runtime.agent_job_types import FormatterOutputModel, agent_job_spec
from app.runtime.agent_paths import InvalidAgentId, validate_agent_id
from app.runtime.json_types import JsonObject

from .client import RuntimeUpstreamError
from .contracts import GOVERNED_EVIDENCE_ROOT_METADATA_KEY, AgentRunResponse
from .store import RuntimeStateConflict


@dataclass(frozen=True)
class _CanonicalAssistantResult:
    text: str
    usage: JsonObject | None
    finished_reason: str | None


@dataclass(frozen=True)
class _ObservedExecution:
    events: list[JsonObject]
    terminal: AgentRunResponse


@dataclass(frozen=True)
class _ExecutionResourcePlan:
    cache_key: str
    business_agent_id: str
    version_id: str
    harness_digest: str
    source_id: str
    source_root: Path
    version_owner_id: str
    source_kind: str
    workspace_id: str
    display_name: str


def _awaiting_restart_cutoff(include_ready: bool, ttl_seconds: int) -> str | None:
    if include_ready:
        return None
    return (datetime.now(timezone.utc) - timedelta(seconds=ttl_seconds)).isoformat()


async def _iter_sse_events(response: Any):
    buffer = b""
    async for chunk in response.aiter_raw():
        buffer += chunk.replace(b"\r\n", b"\n")
        while b"\n\n" in buffer:
            frame, buffer = buffer.split(b"\n\n", 1)
            data = b"\n".join(line[5:].lstrip() for line in frame.split(b"\n") if line.startswith(b"data:"))
            if not data:
                continue
            try:
                event = json.loads(data)
            except (UnicodeDecodeError, json.JSONDecodeError):
                continue
            if isinstance(event, dict):
                yield event


def _requires_interactive_continuation(event: JsonObject) -> bool:
    if event.get("type") in {"REQUIRE_USER_CONFIRM", "REQUIRE_EXTERNAL_EXECUTION"}:
        return True
    if event.get("type") != "CUSTOM" or event.get("name") != "subagent_require_user_confirm":
        return False
    value = event.get("value")
    projected = value.get("event") if isinstance(value, dict) else None
    return isinstance(projected, dict) and projected.get("type") in {
        "REQUIRE_USER_CONFIRM",
        "REQUIRE_EXTERNAL_EXECUTION",
    }


def _user_message(message: str, *, governed_evidence_root: str | None = None) -> JsonObject:
    result: JsonObject = {
        "name": "user",
        "role": "user",
        "content": [{"type": "text", "text": message}],
    }
    if governed_evidence_root is not None:
        result["metadata"] = {
            GOVERNED_EVIDENCE_ROOT_METADATA_KEY: governed_evidence_root,
        }
    return result


def _messages_from_body(value: object) -> list[JsonObject]:
    if not isinstance(value, dict) or not isinstance(value.get("messages"), list):
        return []
    return [dict(item) for item in value["messages"] if isinstance(item, dict)]


def _canonical_assistant_result(
    messages: list[JsonObject],
    reply_ids: list[str],
) -> _CanonicalAssistantResult | None:
    """只从当前 run 最后一条 canonical assistant Message 取结果。"""

    for reply_id in reversed(reply_ids):
        for message in reversed(messages):
            if message.get("id") != reply_id or message.get("role") != "assistant":
                continue
            content = message.get("content")
            parts = (
                [str(block["text"]) for block in content if isinstance(block, dict) and block.get("type") == "text" and isinstance(block.get("text"), str)]
                if isinstance(content, list)
                else []
            )
            usage = message.get("usage")
            reason = message.get("finished_reason")
            return _CanonicalAssistantResult(
                text="\n".join(parts),
                usage=dict(usage) if isinstance(usage, dict) else None,
                finished_reason=reason if isinstance(reason, str) else None,
            )
    return None


def _governed_evidence_root(job_input: JsonObject) -> str | None:
    """从后端 job_input 提取唯一允许注入 Governor Message 的只读根。"""

    target = job_input.get("target_agent_context")
    if not isinstance(target, dict):
        return None
    workspace_dir = target.get("workspace_dir")
    if workspace_dir is None:
        return None
    if not isinstance(workspace_dir, str):
        raise RuntimeStateConflict("Governor evidence workspace must be a string")
    prefix = "/business-agents/"
    suffix = "/workspace"
    if not workspace_dir.startswith(prefix) or not workspace_dir.endswith(suffix):
        raise RuntimeStateConflict("Governor evidence workspace is outside the governed business Agent root")
    agent_id = workspace_dir[len(prefix) : -len(suffix)]
    try:
        safe_agent_id = validate_agent_id(agent_id)
    except InvalidAgentId as exc:
        raise RuntimeStateConflict("Governor evidence workspace contains an unsafe Agent id") from exc
    target_agent_id = target.get("agent_id")
    if workspace_dir != f"{prefix}{safe_agent_id}{suffix}" or (target_agent_id is not None and target_agent_id != safe_agent_id):
        raise RuntimeStateConflict("Governor evidence workspace does not match its governed Agent identity")
    return workspace_dir


def _candidate_source_id(cache_key: str, agent_id: str, version_id: str, digest: str) -> str:
    value = hashlib.sha256(f"{cache_key}\n{agent_id}\n{version_id}\n{digest}".encode()).hexdigest()[:32]
    return f"candidate-{value}"


def _stable_session_uuid(cache_key: str, source_id: str) -> str:
    return str(uuid.uuid5(uuid.NAMESPACE_URL, f"agentgov:{cache_key}:{source_id}"))


def _ephemeral_runtime_name(source_id: str) -> str:
    return f"agentgov-ephemeral-{source_id}"


def _requires_runtime_restart(exc: Exception, source_root: Path) -> bool:
    """只识别 AgentScope 明确给出的启动期 template 缺失，不吞其他失败。"""

    subagents = source_root / "workspace" / "subagents"
    return isinstance(exc, RuntimeUpstreamError) and subagents.is_dir() and b"published after Runtime startup; restart Runtime" in exc.body


def _json_digest(value: JsonObject) -> str:
    raw = json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":")).encode()
    return hashlib.sha256(raw).hexdigest()


def _with_json_contract(prompt: str, job_type: str) -> str:
    """Attach the exact backend-owned output contract to one governed run."""

    output_model = agent_job_spec(job_type).formatter_output_model
    schema = json.dumps(
        output_model.model_json_schema(),
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
    )
    return (
        f"{prompt.rstrip()}\n\n"
        "## 输出契约\n"
        "最终回复必须且只能是一个 UTF-8 JSON object：不要 Markdown 围栏、前后说明或额外字段。"
        "只能使用输入证据中已有的事实；无法满足时使用 schema 中的人工复核/无操作字段。\n"
        f"JSON Schema：{schema}"
    )


def _parse_structured_output(job_type: str, raw_text: str) -> FormatterOutputModel:
    """Fail closed when a governor reply is not exactly one schema-valid object."""

    text = raw_text.strip()
    if not text.startswith("{") or not text.endswith("}"):
        raise RuntimeStateConflict("AgentScope governor reply is not a bare JSON object")
    try:
        payload = json.loads(text)
    except json.JSONDecodeError as exc:
        raise RuntimeStateConflict("AgentScope governor reply is not valid JSON") from exc
    if not isinstance(payload, dict):
        raise RuntimeStateConflict("AgentScope governor reply must be a JSON object")
    output_model = agent_job_spec(job_type).formatter_output_model
    try:
        return output_model.model_validate(payload)
    except Exception as exc:
        raise RuntimeStateConflict("AgentScope governor reply violates the governed output schema") from exc


def _reject_symlinks(root: Path) -> None:
    if root.is_symlink() or not root.is_dir():
        raise RuntimeStateConflict("Harness source must be a real directory")
    for path in root.rglob("*"):
        if path.is_symlink():
            raise RuntimeStateConflict("Harness source must not contain symlinks")
