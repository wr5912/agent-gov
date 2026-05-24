from __future__ import annotations

import argparse
import asyncio
import json
import sys
import uuid
from pathlib import Path
from typing import Any

from app.runtime.agent_version_store import AgentVersionStore
from app.runtime.claude_runtime import ClaudeRuntime
from app.runtime.feedback_jobs import attribution_prompt
from app.runtime.feedback_schemas import validate_attribution_output
from app.runtime.feedback_store import FeedbackStore
from app.runtime.session_store import LocalSessionStore
from app.runtime.settings import AppSettings


PROJECT_ROOT = Path(__file__).resolve().parent
DEFAULT_CASE_ID = "fbc-9b69d469-77ad-461a-aced-a8bd6c4b0120"
DEFAULT_CASE_TITLE = "数据不全BBB"
ATTRIBUTION_EVIDENCE_FILES = (
    "feedback.json",
    "tool_calls.json",
    "trace_summary.json",
    "soc_events.json",
    "main_agent_version.json",
    "messages.json",
    "agent_activity.json",
    "langfuse_trace_refs.json",
)


def _map_container_path(path: Path, container_prefix: str, host_prefix: Path) -> Path:
    prefix = Path(container_prefix)
    try:
        relative = path.relative_to(prefix)
    except ValueError:
        return path
    return host_prefix / relative


def _load_debug_settings() -> AppSettings:
    settings = AppSettings(_env_file=PROJECT_ROOT / "docker" / ".env")
    volume_root = PROJECT_ROOT / "docker" / "volume"
    if not settings.runtime_db_path.exists() and (volume_root / "data" / "runtime.sqlite3").exists():
        settings.data_dir = volume_root / "data"
        settings.main_workspace_dir = _map_container_path(settings.main_workspace_dir, "/main-workspace", volume_root / "main-workspace")
        settings.workspace_dir = settings.main_workspace_dir
        settings.attribution_workspace_dir = _map_container_path(settings.attribution_workspace_dir, "/attribution-workspace", volume_root / "attribution-workspace")
        settings.proposal_workspace_dir = _map_container_path(settings.proposal_workspace_dir, "/proposal-workspace", volume_root / "proposal-workspace")
        settings.main_claude_root = _map_container_path(settings.main_claude_root, "/claude-roots/main", volume_root / "claude-roots" / "main")
        settings.claude_root = settings.main_claude_root
        settings.attribution_claude_root = _map_container_path(settings.attribution_claude_root, "/claude-roots/attribution", volume_root / "claude-roots" / "attribution")
        settings.proposal_claude_root = _map_container_path(settings.proposal_claude_root, "/claude-roots/proposal", volume_root / "claude-roots" / "proposal")
        settings.claude_home = settings.main_claude_root / ".claude"

    for path in (
        settings.data_dir,
        settings.main_workspace_dir,
        settings.attribution_workspace_dir,
        settings.proposal_workspace_dir,
        settings.main_claude_root,
        settings.attribution_claude_root,
        settings.proposal_claude_root,
        settings.claude_home,
        settings.agent_versions_dir,
    ):
        path.mkdir(parents=True, exist_ok=True)
    return settings


def _json_dump(value: Any) -> str:
    return json.dumps(value, ensure_ascii=False, indent=2, sort_keys=True)


def _print_section(title: str, value: Any) -> None:
    print(f"\n===== {title} =====")
    if isinstance(value, str):
        print(value)
    else:
        print(_json_dump(value))


def _latest(values: list[str] | None) -> str | None:
    return values[-1] if values else None


def _bootstrap_runtime() -> tuple[AppSettings, FeedbackStore, ClaudeRuntime]:
    settings = _load_debug_settings()
    agent_version_store = AgentVersionStore(
        versions_dir=settings.agent_versions_dir,
        workspace_dir=settings.main_workspace_dir,
        claude_root=settings.main_claude_root,
    )
    feedback_store = FeedbackStore(
        data_dir=settings.data_dir,
        agent_version_provider=agent_version_store.current_version_id,
        runtime_version="0.1.0",
        enable_debug_evidence=settings.enable_feedback_debug_evidence,
    )
    runtime = ClaudeRuntime(settings, LocalSessionStore(settings.session_dir), feedback_store, agent_version_store)
    feedback_store.set_langfuse_trace_fetcher(runtime.fetch_langfuse_trace)
    return settings, feedback_store, runtime


def _resolve_case(feedback_store: FeedbackStore, case_id: str) -> dict[str, Any]:
    feedback_case = feedback_store.find_case(case_id)
    if feedback_case:
        return feedback_case
    candidates = feedback_store.list_cases(q=case_id, limit=20)
    if not candidates and DEFAULT_CASE_TITLE in case_id:
        candidates = feedback_store.list_cases(q=DEFAULT_CASE_TITLE, limit=20)
    if len(candidates) == 1:
        return candidates[0]
    if not candidates:
        raise RuntimeError(f"未找到反馈处置单: {case_id}")
    raise RuntimeError(f"匹配到多个反馈处置单，请传入完整 case_id: {[item.get('feedback_case_id') for item in candidates]}")


async def _run_live_debug(feedback_store: FeedbackStore, runtime: ClaudeRuntime, feedback_case: dict[str, Any], *, keep_tmp: bool) -> tuple[dict[str, Any], dict[str, Any], str, str]:
    evidence_id = _latest(feedback_case.get("evidence_package_ids"))
    if not evidence_id:
        manifest = feedback_store.create_evidence_package(feedback_case["feedback_case_id"])
        evidence_id = str(manifest["evidence_package_id"])

    debug_job_id = f"fba-debug-{uuid.uuid4()}"
    allowed_evidence_paths = feedback_store._materialize_evidence_files(debug_job_id, "attribution", evidence_id, ATTRIBUTION_EVIDENCE_FILES)
    input_payload = {
        "schema_version": "attribution-input/v1",
        "job_id": debug_job_id,
        "feedback_case_id": feedback_case["feedback_case_id"],
        "evidence_package_id": evidence_id,
        "main_agent_version_id": feedback_store._current_agent_version_id(),
        "evidence_manifest_path": feedback_store._materialize_manifest(debug_job_id, "attribution", evidence_id),
        "allowed_evidence_paths": allowed_evidence_paths,
        "task": "analyze_feedback_attribution",
        "debug_case_title": feedback_case.get("title"),
    }
    input_path = feedback_store._write_job_input(debug_job_id, "attribution", input_payload)
    try:
        raw_output = await runtime._run_profile_json(
            profile_name="feedback-attribution",
            prompt=attribution_prompt(input_path),
            expected_schema_version="attribution-output/v1",
        )
    finally:
        if not keep_tmp:
            feedback_store._cleanup_job_tmp(debug_job_id)
    return input_payload, raw_output, debug_job_id, input_path


async def _run_persisted_job(runtime: ClaudeRuntime, feedback_case_id: str) -> dict[str, Any] | None:
    return await runtime.run_attribution_job(feedback_case_id)


async def main() -> int:
    parser = argparse.ArgumentParser(description="调用真实 feedback-attribution Agent 调试“数据不全BBB”归因输出。")
    parser.add_argument("--case-id", default=DEFAULT_CASE_ID, help=f"反馈处置单 ID，默认 {DEFAULT_CASE_ID}")
    parser.add_argument("--persist", action="store_true", help="使用正式 run_attribution_job 路径，结果写入 feedback_jobs；默认只做实时调试调用并打印。")
    parser.add_argument("--keep-tmp", action="store_true", help="保留 /data/.runtime-tmp/jobs 下的本次调试输入和证据文件。")
    parser.add_argument("--allow-offline", action="store_true", help="允许未配置 provider 时继续运行；默认要求真实模型 provider。")
    args = parser.parse_args()

    settings, feedback_store, runtime = _bootstrap_runtime()
    feedback_case = _resolve_case(feedback_store, args.case_id)
    evidence_id = _latest(feedback_case.get("evidence_package_ids"))
    evidence = feedback_store.get_evidence_package(evidence_id) if evidence_id else None

    _print_section(
        "debug_context",
        {
            "case_id": feedback_case.get("feedback_case_id"),
            "case_title": feedback_case.get("title"),
            "case_status": feedback_case.get("status"),
            "evidence_package_id": evidence_id,
            "data_dir": str(settings.data_dir),
            "attribution_workspace_dir": str(settings.attribution_workspace_dir),
            "provider_configured": runtime._provider_configured(),
            "max_turns": settings.max_turns,
            "persist": args.persist,
        },
    )
    if evidence:
        _print_section("evidence_completeness", evidence.get("completeness", {}))

    if not runtime._provider_configured() and not args.allow_offline:
        print("\n未配置 MODEL_PROVIDER_API_KEY/ANTHROPIC_API_KEY。为确保调用真实归因 Agent，本脚本已停止。", file=sys.stderr)
        return 2

    if args.persist:
        job = await _run_persisted_job(runtime, str(feedback_case["feedback_case_id"]))
        _print_section("persisted_job", job)
        if not job:
            return 1
        output = feedback_store.get_job_output(str(job["job_id"]), "attribution")
        if output:
            _print_section("validated_attribution_output", output)
            return 0
        _print_section("job_error", job.get("error_json") or "未生成 validated attribution output")
        return 1

    input_payload, raw_output, debug_job_id, input_path = await _run_live_debug(feedback_store, runtime, feedback_case, keep_tmp=args.keep_tmp)
    validated, validation_error = validate_attribution_output(raw_output)
    _print_section("debug_job", {"job_id": debug_job_id, "input_path": input_path, "tmp_kept": args.keep_tmp})
    _print_section("attribution_input", input_payload)
    _print_section("raw_agent_output", raw_output)
    if validated:
        _print_section("validated_attribution_output", validated)
        return 0
    _print_section("validation_error", validation_error)
    return 1


if __name__ == "__main__":
    raise SystemExit(asyncio.run(main()))
