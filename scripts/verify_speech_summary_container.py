#!/usr/bin/env python3
"""真实容器中的 Runtime 语义事件、Speech Summary、HITL 与 raw 契约验收。

本脚本只面向已经启动的 Compose API。它会导入两个临时业务 Agent，执行真实模型
调用，并在 finally 中恢复 OpenAI 兼容出口配置和删除临时 Agent。输出仅包含断言
结果、事件计数和 raw 字节摘要，不输出凭据、prompt 或模型正文。
"""

from __future__ import annotations

import argparse
import asyncio
import hashlib
import io
import json
import os
import sys
import tarfile
import time
import uuid
from collections.abc import Awaitable, Callable, Mapping
from dataclasses import dataclass
from pathlib import Path
from typing import Any

import httpx

REPO_ROOT = Path(__file__).resolve().parents[1]
if str(REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(REPO_ROOT))

from app.runtime.json_types import JsonObject  # noqa: E402

SseCallback = Callable[["SseEvent"], Awaitable[None]]

_SPEECH_PROMPT = "不要使用任何工具。请用一段不少于六十个汉字的中文说明：真实容器中的协议事件、终态顺序与失败隔离已经核对完成。"
_SHORT_PROMPT = "不要使用任何工具，只回复：AGENTGOV_RUNTIME_E2E_OK。"
_HITL_PROMPT = "HITL_PROBE：严格遵循 workspace 指令完成权限确认测试。"


class AcceptanceError(RuntimeError):
    """A contract assertion failed without exposing response content."""


@dataclass(frozen=True)
class SseEvent:
    name: str
    data: Any
    event_id: str | None = None


def _require(condition: bool, message: str) -> None:
    if not condition:
        raise AcceptanceError(message)


def _require_container_acceptance(environ: Mapping[str, str]) -> None:
    active = environ.get("AGENT_GOV_CONTAINER_ACCEPTANCE_ACTIVE", "").strip().lower()
    run_id = environ.get("AGENT_GOV_ACCEPTANCE_RUN_ID", "").strip()
    profile = environ.get("AGENT_GOV_CONTAINER_ACCEPTANCE_PROFILE", "").strip()
    _require(
        active in {"1", "true", "yes", "on"} and bool(run_id) and profile == "core",
        "real container verification must run through make container-speech-summary-test",
    )


def _workspace_package(agent_id: str, *, requires_hitl: bool) -> bytes:
    if requires_hitl:
        claude_md = """# AgentGov Runtime HITL E2E Agent

当用户消息包含 `HITL_PROBE` 时，必须且只能调用一次 Bash 工具，命令必须是
`printf AGENTGOV_HITL_OK`。获得工具结果后只回复 `AGENTGOV_HITL_OK`。
其他请求不得调用工具，直接简洁回答。
"""
        settings = {"permissions": {"ask": ["Bash(*)"]}}
    else:
        claude_md = """# AgentGov Runtime Protocol E2E Agent

这是协议验收 Agent。不得调用任何工具；直接根据用户要求用中文回答。
"""
        settings = {"permissions": {"ask": []}}

    files = {
        "CLAUDE.md": claude_md.encode(),
        ".mcp.json": b'{"mcpServers":{}}\n',
        ".claude/settings.json": (json.dumps(settings, ensure_ascii=False, separators=(",", ":")) + "\n").encode(),
        "agent.yaml": f"agent:\n  id: {agent_id}\n".encode(),
    }
    buffer = io.BytesIO()
    with tarfile.open(fileobj=buffer, mode="w:gz") as archive:
        root = tarfile.TarInfo("workspace/")
        root.type = tarfile.DIRTYPE
        root.mode = 0o755
        archive.addfile(root)
        for path, content in sorted(files.items()):
            member = tarfile.TarInfo(f"workspace/{path}")
            member.size = len(content)
            member.mode = 0o644
            archive.addfile(member, io.BytesIO(content))
    return buffer.getvalue()


class RuntimeAcceptance:
    def __init__(self, *, base_url: str, api_key: str, timeout_seconds: float) -> None:
        headers = {"Accept": "application/json"}
        if api_key.strip():
            headers["Authorization"] = f"Bearer {api_key.strip()}"
        timeout = httpx.Timeout(timeout_seconds, connect=15.0)
        self.client = httpx.AsyncClient(
            base_url=base_url.rstrip("/"),
            headers=headers,
            timeout=timeout,
            limits=httpx.Limits(max_connections=20, max_keepalive_connections=10),
        )
        self.no_hitl_agent = ""
        self.hitl_agent = ""
        self.original_openai_config: JsonObject | None = None
        self.checks: list[str] = []
        self.event_counts: dict[str, dict[str, int]] = {}
        self.raw_summary: JsonObject = {}

    async def __aenter__(self) -> RuntimeAcceptance:
        return self

    async def __aexit__(self, _exc_type: object, _exc: object, _tb: object) -> None:
        await self.client.aclose()

    async def request_json(
        self,
        method: str,
        path: str,
        *,
        payload: JsonObject | None = None,
        expected: tuple[int, ...] = (200,),
    ) -> JsonObject:
        response = await self.client.request(method, path, json=payload)
        if response.status_code not in expected:
            raise AcceptanceError(f"{method} {path} expected HTTP {expected}, got {response.status_code}")
        if not response.content:
            return {}
        try:
            data = response.json()
        except ValueError as exc:
            raise AcceptanceError(f"{method} {path} returned non-JSON content") from exc
        _require(isinstance(data, dict), f"{method} {path} did not return a JSON object")
        return data

    async def expect_status(self, path: str, status: int, payload: JsonObject) -> None:
        response = await self.client.post(path, json=payload)
        _require(response.status_code == status, f"POST {path} expected HTTP {status}, got {response.status_code}")

    async def prepare(self) -> None:
        health = await self.request_json("GET", "/health")
        _require(health.get("status") == "ok", "API health check did not report ok")
        self.original_openai_config = await self.request_json("GET", "/api/settings/openai-compat-agent")

        suffix = f"{int(time.time())}-{uuid.uuid4().hex[:8]}"
        self.no_hitl_agent = f"e2e-speech-{suffix}"
        self.hitl_agent = f"e2e-hitl-{suffix}"
        await self._import_agent(self.no_hitl_agent, "Speech Summary E2E", requires_hitl=False)
        await self._import_agent(self.hitl_agent, "HITL E2E", requires_hitl=True)

        registry = await self.client.get("/api/agent-registry")
        _require(registry.status_code == 200, f"GET /api/agent-registry returned {registry.status_code}")
        rows = registry.json()
        _require(isinstance(rows, list), "Agent registry did not return a list")
        by_id = {row.get("agent_id"): row for row in rows if isinstance(row, dict)}
        _require(by_id.get(self.no_hitl_agent, {}).get("requires_web_hitl") is False, "no-HITL Agent profile was not derived as false")
        _require(by_id.get(self.hitl_agent, {}).get("requires_web_hitl") is True, "HITL Agent profile was not derived as true")

        await self.request_json(
            "PUT",
            "/api/settings/openai-compat-agent",
            payload={"agent_id": self.no_hitl_agent},
        )
        self.checks.append("temporary_agents_and_profile_derivation")

    async def _import_agent(self, agent_id: str, name: str, *, requires_hitl: bool) -> None:
        package = _workspace_package(agent_id, requires_hitl=requires_hitl)
        response = await self.client.post(
            f"/api/agent-registry/{agent_id}/workspace/import",
            data={"name": name},
            files={"package": (f"{agent_id}.tar.gz", package, "application/gzip")},
        )
        _require(response.status_code == 200, f"workspace import for {agent_id} returned {response.status_code}")

    async def cleanup(self) -> list[str]:
        errors: list[str] = []
        try:
            original = self.original_openai_config or {}
            if original.get("configured") is True and isinstance(original.get("agent_id"), str):
                response = await self.client.put(
                    "/api/settings/openai-compat-agent",
                    json={"agent_id": original["agent_id"]},
                )
            else:
                response = await self.client.delete("/api/settings/openai-compat-agent")
            if response.status_code != 200:
                errors.append(f"restore_openai_config:{response.status_code}")
        except Exception as exc:  # noqa: BLE001 - cleanup must continue
            errors.append(f"restore_openai_config:{exc.__class__.__name__}")

        for agent_id in (self.hitl_agent, self.no_hitl_agent):
            if not agent_id:
                continue
            try:
                response = await self.client.delete(f"/api/agent-registry/{agent_id}")
                if response.status_code not in {200, 404}:
                    errors.append(f"delete_agent:{response.status_code}")
            except Exception as exc:  # noqa: BLE001 - cleanup must continue
                errors.append(f"delete_agent:{exc.__class__.__name__}")
        return errors

    async def collect_sse(
        self,
        path: str,
        payload: JsonObject,
        *,
        callback: SseCallback | None = None,
    ) -> list[SseEvent]:
        events: list[SseEvent] = []
        async with self.client.stream(
            "POST",
            path,
            json=payload,
            headers={"Accept": "text/event-stream"},
        ) as response:
            if response.status_code != 200:
                await response.aread()
                raise AcceptanceError(f"POST {path} expected HTTP 200, got {response.status_code}")
            content_type = response.headers.get("content-type", "")
            _require(content_type.startswith("text/event-stream"), f"POST {path} did not return SSE")

            event_name = "message"
            event_id: str | None = None
            data_lines: list[str] = []
            async for line in response.aiter_lines():
                if line == "":
                    if not data_lines:
                        event_name = "message"
                        event_id = None
                        continue
                    raw_data = "\n".join(data_lines)
                    try:
                        data = json.loads(raw_data)
                    except json.JSONDecodeError:
                        data = raw_data
                    event = SseEvent(name=event_name, data=data, event_id=event_id)
                    events.append(event)
                    if callback is not None:
                        await callback(event)
                    event_name = "message"
                    event_id = None
                    data_lines = []
                    continue
                if line.startswith(":"):
                    continue
                field, separator, value = line.partition(":")
                if separator and value.startswith(" "):
                    value = value[1:]
                if field == "event":
                    event_name = value
                elif field == "id":
                    event_id = value
                elif field == "data":
                    data_lines.append(value)

            _require(not data_lines, f"POST {path} ended with an incomplete SSE frame")
        _require(events, f"POST {path} returned no data events")
        return events

    @staticmethod
    def _validate_speech(events: list[SseEvent], *, terminal_name: str) -> int:
        names = [event.name for event in events]
        _require(names[-1] == terminal_name, f"{terminal_name} was not the final SSE event")
        summaries = [event.data for event in events if event.name == "agentgov.speech_summary"]
        _require(summaries, "opted-in stream emitted no agentgov.speech_summary")
        for envelope in summaries:
            _require(isinstance(envelope, dict), "Speech Summary envelope was not an object")
            _require(envelope.get("v") == 1, "Speech Summary envelope v was not 1")
            _require(envelope.get("type") == "agentgov.speech_summary", "Speech Summary type was invalid")
            _require(isinstance(envelope.get("run_id"), str) and envelope["run_id"], "Speech Summary run_id was absent")
            _require(isinstance(envelope.get("ts"), (int, float)), "Speech Summary ts was not numeric")
            _require(isinstance(envelope.get("seq"), int) and envelope["seq"] > 0, "Speech Summary seq was invalid")
            payload = envelope.get("payload")
            _require(isinstance(payload, dict), "Speech Summary payload was not an object")
            text = payload.get("text")
            _require(isinstance(text, str) and 10 <= len(text) <= 50, "Speech Summary text violated the 10–50 character contract")
            _require(payload.get("char_count") == len(text), "Speech Summary char_count did not match text")
            _require(payload.get("scope") == "main", "Speech Summary scope was not main")
            _require(payload.get("source_kind") in {"thinking", "assistant_response"}, "Speech Summary source_kind was invalid")
            _require(isinstance(payload.get("message_id"), str) and payload["message_id"], "Speech Summary message_id was absent")
            _require(isinstance(payload.get("summary_id"), str) and payload["summary_id"], "Speech Summary summary_id was absent")
            if payload["source_kind"] == "thinking":
                _require(isinstance(payload.get("block_index"), int), "thinking summary omitted block_index")
            else:
                _require("block_index" not in payload, "assistant response summary exposed block_index")
        return len(summaries)

    def _record_events(self, label: str, events: list[SseEvent]) -> None:
        counts: dict[str, int] = {}
        for event in events:
            counts[event.name] = counts.get(event.name, 0) + 1
        self.event_counts[label] = counts

    @staticmethod
    def _require_no_sdk_mirror_failure(events: list[SseEvent]) -> None:
        _require(
            not any(event.name == "claude.sdk.MirrorErrorMessage" for event in events),
            "SDK stream emitted MirrorErrorMessage after ResultMessage",
        )
        results = [event.data for event in events if event.name == "agentgov.result"]
        for result in results:
            _require(isinstance(result, dict), "agentgov.result data was not an object")
            _require(not result.get("errors"), "agentgov.result reported runtime errors")

    async def verify_speech_surfaces(self) -> None:
        base = {
            "message": _SPEECH_PROMPT,
            "agent_id": self.no_hitl_agent,
            "max_turns": 2,
            "with_speech_summary": True,
        }
        sdk = await self.collect_sse("/api/agent-runtime/sdk-events", base)
        self._require_no_sdk_mirror_failure(sdk)
        self._validate_speech(sdk, terminal_name="agentgov.done")
        self._record_events("sdk", sdk)

        for mode in ("raw", "semantic"):
            events = await self.collect_sse(f"/api/chat/stream?event_mode={mode}", base)
            self._validate_speech(events, terminal_name="done")
            self._record_events(f"chat_{mode}", events)

        responses = await self.collect_sse(
            "/v1/responses",
            {
                "input": _SPEECH_PROMPT,
                "stream": True,
                "agentgov": {
                    "agent_id": self.no_hitl_agent,
                    "with_speech_summary": True,
                },
            },
        )
        names = [event.name for event in responses]
        _require(names.count("response.completed") == 1, "Responses stream did not emit response.completed exactly once")
        _require("response.failed" not in names, "Responses stream emitted response.failed")
        self._validate_speech(responses, terminal_name="response.completed")
        _require(names[-2] == "agentgov.done", "agentgov.done was not immediately before response.completed")
        self._record_events("responses_control", responses)
        self.checks.append("speech_summary_all_semantic_surfaces")

    async def verify_strict_and_compatibility_surfaces(self) -> None:
        strict = await self.collect_sse(
            "/v1/responses",
            {"input": _SHORT_PROMPT, "stream": True},
        )
        strict_names = [event.name for event in strict]
        _require(strict_names[-1] == "response.completed", "strict Responses terminal was not last")
        _require(not any(name.startswith("agentgov.") for name in strict_names), "strict Responses leaked AgentGov events")
        self._record_events("responses_strict", strict)

        chat = await self.request_json(
            "POST",
            "/api/chat",
            payload={
                "message": _SHORT_PROMPT,
                "agent_id": self.no_hitl_agent,
                "max_turns": 2,
            },
        )
        _require(not chat.get("errors"), "/api/chat returned runtime errors")
        _require(isinstance(chat.get("answer"), str) and chat["answer"], "/api/chat returned an empty answer")

        completion = await self.request_json(
            "POST",
            "/v1/chat/completions",
            payload={
                "messages": [{"role": "user", "content": _SHORT_PROMPT}],
                "max_turns": 2,
            },
        )
        _require(completion.get("object") == "chat.completion", "Chat Completions object was invalid")
        choices = completion.get("choices")
        _require(isinstance(choices, list) and choices, "Chat Completions returned no choices")

        await self.expect_status(
            "/v1/responses",
            422,
            {
                "input": _SHORT_PROMPT,
                "agentgov": {
                    "agent_id": self.no_hitl_agent,
                    "with_speech_summary": True,
                },
            },
        )
        for path, payload in (
            (
                "/api/chat",
                {
                    "message": _SHORT_PROMPT,
                    "agent_id": self.no_hitl_agent,
                    "with_speech_summary": True,
                },
            ),
            (
                "/v1/chat/completions",
                {
                    "messages": [{"role": "user", "content": _SHORT_PROMPT}],
                    "with_speech_summary": True,
                },
            ),
        ):
            await self.expect_status(path, 422, payload)
        self.checks.append("strict_and_legacy_compatibility_contracts")

    async def verify_raw_surface(self) -> None:
        await self.expect_status(
            "/api/debug/agent-runtime/raw-events",
            422,
            {
                "message": _SHORT_PROMPT,
                "agent_id": self.no_hitl_agent,
                "with_speech_summary": True,
            },
        )
        response = await self.client.post(
            "/api/debug/agent-runtime/raw-events",
            json={
                "message": _SHORT_PROMPT,
                "agent_id": self.no_hitl_agent,
                "max_turns": 2,
                "stream": False,
            },
            headers={"Accept": "application/octet-stream"},
        )
        _require(response.status_code == 200, f"raw endpoint returned HTTP {response.status_code}")
        _require(
            response.headers.get("content-type", "").startswith("application/octet-stream"),
            "raw endpoint content type was not application/octet-stream",
        )
        _require(response.headers.get("x-agentgov-raw-fidelity") == "byte-exact", "raw fidelity header was not byte-exact")
        _require(
            response.headers.get("x-agentgov-native-protocol") == "cli-stream-json-stdout",
            "raw native protocol header was invalid",
        )
        body = response.content
        _require(body, "raw endpoint returned an empty body")
        _require(b"agentgov.speech_summary" not in body, "raw Runtime stdout was contaminated by Speech Summary")

        native_types: set[str] = set()
        native_events = 0
        for line in body.splitlines():
            _require(bool(line), "raw Runtime stdout contained an unexpected blank JSONL record")
            try:
                value = json.loads(line)
            except (UnicodeDecodeError, json.JSONDecodeError) as exc:
                raise AcceptanceError("raw Runtime stdout was not untouched UTF-8 JSONL") from exc
            _require(isinstance(value, dict), "raw Runtime stdout contained a non-object JSONL record")
            native_events += 1
            event_type = value.get("type")
            if isinstance(event_type, str):
                native_types.add(event_type)
        _require({"system", "assistant", "result"} <= native_types, "raw Runtime stdout omitted recognizable native lifecycle records")
        self.raw_summary = {
            "byte_count": len(body),
            "sha256_prefix": hashlib.sha256(body).hexdigest()[:16],
            "native_event_count": native_events,
            "native_types": sorted(native_types),
            "fidelity": "byte-exact",
        }
        self.checks.append("raw_byte_exact_native_stdout_contract")

    async def verify_hitl(self) -> None:
        requested = 0
        resolved = 0
        decision_sent = False

        async def decide(event: SseEvent) -> None:
            nonlocal requested, resolved, decision_sent
            if event.name == "agentgov.confirmation.requested":
                requested += 1
                _require(not decision_sent, "HITL Agent requested more than one decision")
                data = event.data
                _require(isinstance(data, dict), "HITL requested event data was not an object")
                request_id = data.get("request_id")
                token = data.get("decision_token")
                _require(isinstance(request_id, str) and request_id, "HITL requested event omitted request_id")
                _require(isinstance(token, str) and token, "HITL requested event omitted decision_token")
                decision = await self.client.post(
                    f"/v1/agentgov/confirmation-requests/{request_id}/decision",
                    json={"action": "allow_once", "decision_token": token},
                )
                _require(decision.status_code == 200, f"HITL decision returned HTTP {decision.status_code}")
                decision_sent = True
            elif event.name == "agentgov.confirmation.resolved":
                resolved += 1
                _require(isinstance(event.data, dict), "HITL resolved event data was not an object")
                _require("decision_token" not in event.data, "HITL resolved event leaked decision_token")

        events = await self.collect_sse(
            "/api/agent-runtime/sdk-events",
            {
                "message": _HITL_PROMPT,
                "agent_id": self.hitl_agent,
                "max_turns": 3,
            },
            callback=decide,
        )
        names = [event.name for event in events]
        self._require_no_sdk_mirror_failure(events)
        _require(names[-1] == "agentgov.done", "HITL SDK stream terminal was not last")
        _require(requested == 1 and resolved == 1 and decision_sent, "HITL requested→decision→resolved flow was incomplete")
        self._record_events("sdk_hitl", events)
        await self._verify_hitl_failfast()
        self.checks.append("hitl_requested_decision_resolved_and_failfast")

    async def _verify_hitl_failfast(self) -> None:
        await self.expect_status(
            "/api/chat",
            422,
            {
                "message": _SHORT_PROMPT,
                "agent_id": self.hitl_agent,
            },
        )
        await self.expect_status(
            "/api/debug/agent-runtime/raw-events",
            422,
            {
                "message": _SHORT_PROMPT,
                "agent_id": self.hitl_agent,
                "stream": False,
            },
        )

        await self.request_json(
            "PUT",
            "/api/settings/openai-compat-agent",
            payload={"agent_id": self.hitl_agent},
        )
        try:
            await self.expect_status(
                "/v1/chat/completions",
                422,
                {"messages": [{"role": "user", "content": _SHORT_PROMPT}]},
            )
            await self.expect_status(
                "/v1/responses",
                422,
                {"input": _SHORT_PROMPT, "stream": True},
            )
        finally:
            await self.request_json(
                "PUT",
                "/api/settings/openai-compat-agent",
                payload={"agent_id": self.no_hitl_agent},
            )

    async def verify_openapi(self) -> None:
        response = await self.client.get("/openapi.json")
        _require(response.status_code == 200, f"GET /openapi.json returned {response.status_code}")
        schema = response.json()
        paths = schema.get("paths", {})
        for path in ("/api/chat", "/api/chat/stream", "/v1/chat/completions"):
            _require(paths.get(path, {}).get("post", {}).get("deprecated") is True, f"{path} was not marked deprecated")
        raw_request = paths["/api/debug/agent-runtime/raw-events"]["post"]["requestBody"]
        raw_schema = raw_request["content"]["application/json"]["schema"]
        raw_ref = raw_schema.get("$ref", "")
        _require(raw_ref.endswith("/RuntimeRawEventsRequest"), "raw endpoint did not use its dedicated request schema")
        raw_properties = schema["components"]["schemas"]["RuntimeRawEventsRequest"]["properties"]
        _require("with_speech_summary" not in raw_properties, "OpenAPI exposed Speech Summary on raw request")
        self.checks.append("openapi_deprecation_and_field_ownership")


async def _run(args: argparse.Namespace) -> JsonObject:
    _require_container_acceptance(os.environ)
    api_key = args.api_key or os.environ.get("API_KEY", "")
    acceptance = RuntimeAcceptance(
        base_url=args.base_url,
        api_key=api_key,
        timeout_seconds=args.timeout_seconds,
    )
    cleanup_errors: list[str] = []
    started = time.monotonic()
    async with acceptance:
        try:
            await acceptance.prepare()
            await acceptance.verify_openapi()
            await acceptance.verify_speech_surfaces()
            await acceptance.verify_strict_and_compatibility_surfaces()
            await acceptance.verify_raw_surface()
            await acceptance.verify_hitl()
        finally:
            cleanup_errors = await acceptance.cleanup()
    _require(not cleanup_errors, f"container acceptance cleanup failed: {','.join(cleanup_errors)}")
    return {
        "status": "passed",
        "elapsed_seconds": round(time.monotonic() - started, 2),
        "checks": acceptance.checks,
        "event_counts": acceptance.event_counts,
        "raw": acceptance.raw_summary,
        "cleanup": "complete",
    }


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--base-url", default="http://127.0.0.1:8080")
    parser.add_argument("--api-key", default="")
    parser.add_argument("--timeout-seconds", type=float, default=360.0)
    args = parser.parse_args()
    try:
        result = asyncio.run(_run(args))
    except Exception as exc:  # noqa: BLE001 - CLI needs a concise, content-free failure
        print(
            json.dumps(
                {
                    "status": "failed",
                    "error_type": exc.__class__.__name__,
                    "error": str(exc),
                },
                ensure_ascii=False,
                indent=2,
            )
        )
        return 1
    print(json.dumps(result, ensure_ascii=False, indent=2, sort_keys=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
