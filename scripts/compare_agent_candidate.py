"""以同一私有场景集调用现有真实测试 Session，对照当前版与候选版。

本工具只产出人工参考报告，不创建 AgentTestRun、不修改候选审批/发布门。
场景文件为逐行 JSON：{"case_id": "...", "message": "..."}；报告绝不包含正文。
"""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import re
import stat
import sys
from collections.abc import Mapping
from dataclasses import asdict, dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Literal
from urllib.parse import urlsplit

import httpx

from scripts.agentscope_atomic_cutover import CutoverError, load_env_file

_SHA_RE = re.compile(r"^[0-9a-f]{40}$")
_CASE_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._-]{0,127}$")
_ID_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._-]{0,127}$")
_TRACE_RE = re.compile(r"^[0-9a-f]{32}$")
Judgment = Literal["better", "same", "worse", "inconclusive"]
ApiObject = dict[str, object]


class ComparisonError(Exception):
    """只携带无正文/无密钥的固定错误类别。"""


@dataclass(frozen=True)
class ComparisonConnection:
    api_base_url: str
    api_key: str = field(repr=False)


@dataclass(frozen=True)
class Scenario:
    case_id: str
    message: str


@dataclass
class VersionEvidence:
    commit_sha: str
    status: str = "incomplete"
    test_session_id: str | None = None
    session_id: str | None = None
    run_id: str | None = None
    trace_id: str | None = None
    answer_sha256: str | None = None
    answer_utf8_length: int | None = None
    cleanup: str = "not_created"
    error_code: str | None = None


@dataclass(frozen=True)
class CaseEvidence:
    case_id: str
    prompt_sha256: str
    prompt_utf8_length: int
    baseline: VersionEvidence
    candidate: VersionEvidence
    manual_judgment: Judgment


@dataclass(frozen=True)
class ComparisonReport:
    kind: str
    purpose: str
    created_at: str
    agent_id: str
    change_set_id: str
    baseline_commit_sha: str
    candidate_commit_sha: str
    scenario_set_sha256: str
    cases: list[CaseEvidence]
    status: str


def _digest(value: str) -> tuple[str, int]:
    raw = value.encode("utf-8")
    return hashlib.sha256(raw).hexdigest(), len(raw)


def _require_sha(value: object) -> str:
    if not isinstance(value, str) or not _SHA_RE.fullmatch(value):
        raise ComparisonError("invalid_commit_sha")
    return value


def _private_scenarios(path: Path) -> tuple[list[Scenario], str]:
    descriptor = os.open(path, os.O_RDONLY | os.O_NOFOLLOW)
    try:
        info = os.fstat(descriptor)
        if not stat.S_ISREG(info.st_mode) or info.st_uid != os.getuid() or info.st_mode & 0o077:
            raise ComparisonError("scenario_file_not_private")
        if info.st_size > 2_000_000:
            raise ComparisonError("scenario_file_too_large")
        raw = os.read(descriptor, 2_000_001)
    finally:
        os.close(descriptor)
    try:
        lines = raw.decode("utf-8").splitlines()
        values = [json.loads(line) for line in lines if line.strip()]
    except (UnicodeError, json.JSONDecodeError) as exc:
        raise ComparisonError("invalid_scenario_jsonl") from exc
    cases: list[Scenario] = []
    for value in values:
        if not isinstance(value, dict) or set(value) != {"case_id", "message"}:
            raise ComparisonError("invalid_scenario_fields")
        case_id, message = value["case_id"], value["message"]
        if not isinstance(case_id, str) or not _CASE_RE.fullmatch(case_id):
            raise ComparisonError("invalid_case_id")
        if not isinstance(message, str) or not message.strip() or len(message.encode("utf-8")) > 32_768:
            raise ComparisonError("invalid_case_message")
        cases.append(Scenario(case_id, message))
    if not cases or len(cases) > 100 or len({case.case_id for case in cases}) != len(cases):
        raise ComparisonError("invalid_scenario_set")
    return cases, hashlib.sha256(raw).hexdigest()


def _base_url(value: str) -> str:
    try:
        parsed = urlsplit(value)
        port = parsed.port
    except ValueError as exc:
        raise ComparisonError("invalid_api_base_url") from exc
    if any(character.isspace() or ord(character) < 32 or ord(character) == 127 for character in value) or port == 0:
        raise ComparisonError("invalid_api_base_url")
    if parsed.scheme not in {"http", "https"} or not parsed.hostname or parsed.username or parsed.password or parsed.path not in {"", "/"}:
        raise ComparisonError("invalid_api_base_url")
    if parsed.query or parsed.fragment or (parsed.scheme == "http" and parsed.hostname not in {"localhost", "127.0.0.1", "::1"}):
        raise ComparisonError("insecure_api_base_url")
    return value.rstrip("/")


def comparison_connection(*, env_file: Path | None, api_base_url: str | None, environ: Mapping[str, str]) -> ComparisonConnection:
    """所选文件与手工连接为互斥入口；文件模式不读取进程凭据。"""
    if (env_file is None) == (api_base_url is None):
        raise ComparisonError("connection_mode_required")
    if env_file is not None:
        try:
            selected = load_env_file(env_file.expanduser().resolve(strict=True))
        except OSError as exc:
            raise ComparisonError("selected_env_unreadable") from exc
        except (CutoverError, ValueError, RuntimeError) as exc:
            raise ComparisonError("selected_env_invalid") from exc
        api_key = selected.get("API_KEY")
        api_base_url = selected.get("API_BASE_URL")
        if not api_base_url:
            port = selected.get("HOST_PORT") or "50400"
            if re.fullmatch(r"504[0-9]{2}", port) is None:
                raise ComparisonError("selected_env_invalid_host_port")
            api_base_url = f"http://127.0.0.1:{port}"
        missing_key_code = "selected_env_api_key_missing"
    else:
        api_key = environ.get("API_KEY")
        missing_key_code = "api_key_env_missing"
    if not api_key or not api_key.strip():
        raise ComparisonError(missing_key_code)
    if any(ord(character) < 32 or ord(character) > 126 for character in api_key):
        raise ComparisonError("invalid_api_key")
    if api_base_url is None:
        raise ComparisonError("invalid_api_base_url")
    return ComparisonConnection(_base_url(api_base_url), api_key)


def _request(client: httpx.Client, method: str, path: str, *, payload: ApiObject | None = None, params: dict[str, str] | None = None) -> ApiObject:
    try:
        response = client.request(method, path, json=payload, params=params)
    except httpx.HTTPError as exc:
        raise ComparisonError("api_transport_error") from exc
    if not 200 <= response.status_code < 300:
        raise ComparisonError(f"api_http_{response.status_code}")
    if response.status_code == 204:
        return {}
    try:
        value = response.json()
    except ValueError as exc:
        raise ComparisonError("invalid_api_json") from exc
    if not isinstance(value, dict):
        raise ComparisonError("invalid_api_response")
    return value


def _versions(client: httpx.Client, agent_id: str, change_set_id: str) -> tuple[str, str]:
    if not _ID_RE.fullmatch(agent_id) or not _ID_RE.fullmatch(change_set_id):
        raise ComparisonError("invalid_target_identity")
    baseline = _request(client, "GET", "/api/agent-repository/current", params={"agent_id": agent_id})
    change_set = _request(client, "GET", f"/api/agent-change-sets/{change_set_id}")
    baseline_sha = _require_sha(baseline.get("commit_sha"))
    candidate_sha = _require_sha(change_set.get("candidate_commit_sha"))
    if change_set.get("agent_id") != agent_id or change_set.get("base_commit_sha") != baseline_sha:
        raise ComparisonError("candidate_base_or_agent_mismatch")
    if change_set.get("status") not in {"candidate_committed", "pending_approval", "approved"}:
        raise ComparisonError("candidate_not_testable")
    if candidate_sha == baseline_sha:
        raise ComparisonError("identical_versions")
    return baseline_sha, candidate_sha


def _validate_run(chat: ApiObject, run: ApiObject, *, agent_id: str, commit_sha: str) -> str:
    run_id = chat.get("run_id")
    session_id = chat.get("session_id")
    if not isinstance(run_id, str) or not _ID_RE.fullmatch(run_id) or not isinstance(session_id, str) or not _ID_RE.fullmatch(session_id):
        raise ComparisonError("missing_chat_identity")
    if (
        chat.get("agent_version_id") != commit_sha
        or run.get("run_id") != run_id
        or run.get("session_id") != session_id
        or run.get("agent_id") != agent_id
        or run.get("agent_version_id") != commit_sha
    ):
        raise ComparisonError("run_identity_mismatch")
    if run.get("status") != "succeeded" or run.get("error") or chat.get("errors") != [] or not run.get("completed_at"):
        raise ComparisonError("run_not_succeeded")
    answer = chat.get("answer")
    if not isinstance(answer, str):
        raise ComparisonError("missing_answer")
    return answer


def _run_version(
    client: httpx.Client,
    *,
    scenario: Scenario,
    agent_id: str,
    commit_sha: str,
    change_set_id: str | None,
) -> tuple[VersionEvidence, str | None]:
    evidence = VersionEvidence(commit_sha=commit_sha)
    answer: str | None = None
    try:
        session = _request(
            client,
            "POST",
            "/api/agent-test-sessions",
            payload={"agent_id": agent_id, "commit_sha": commit_sha, "change_set_id": change_set_id},
        )
        session_id = session.get("test_session_id")
        if not isinstance(session_id, str) or not _ID_RE.fullmatch(session_id):
            raise ComparisonError("missing_test_session_id")
        if session.get("agent_id") != agent_id or session.get("commit_sha") != commit_sha or session.get("change_set_id") != change_set_id:
            evidence.test_session_id = session_id
            evidence.cleanup = "ownership_unconfirmed"
            raise ComparisonError("test_session_identity_mismatch")
        evidence.test_session_id = session_id
        evidence.cleanup = "pending"
        chat = _request(client, "POST", f"/api/agent-test-sessions/{session_id}/messages", payload={"message": scenario.message, "metadata": {}})
        run_id = chat.get("run_id")
        chat_session_id = chat.get("session_id")
        evidence.run_id = run_id if isinstance(run_id, str) and _ID_RE.fullmatch(run_id) else None
        evidence.session_id = chat_session_id if isinstance(chat_session_id, str) and _ID_RE.fullmatch(chat_session_id) else None
        trace_id = chat.get("trace_id")
        evidence.trace_id = trace_id if isinstance(trace_id, str) and _TRACE_RE.fullmatch(trace_id) else None
        if not evidence.run_id or not evidence.session_id:
            raise ComparisonError("missing_chat_identity")
        run = _request(client, "GET", f"/api/agent-runs/{evidence.run_id}")
        answer = _validate_run(chat, run, agent_id=agent_id, commit_sha=commit_sha)
        evidence.answer_sha256, evidence.answer_utf8_length = _digest(answer)
        evidence.status = "complete"
    except ComparisonError as exc:
        evidence.error_code = str(exc)
    finally:
        if evidence.test_session_id is not None and evidence.cleanup == "pending":
            try:
                _request(client, "DELETE", f"/api/agent-test-sessions/{evidence.test_session_id}")
                evidence.cleanup = "complete"
            except ComparisonError:
                evidence.cleanup = "failed"
                evidence.status = "incomplete"
                evidence.error_code = evidence.error_code or "cleanup_failed"
    return evidence, answer


def _judgment(case_id: str, baseline_answer: str | None, candidate_answer: str | None, *, interactive: bool) -> Judgment:
    if not interactive or baseline_answer is None or candidate_answer is None:
        return "inconclusive"
    print(f"\nCase {case_id} — baseline:\n{baseline_answer}\n\nCandidate:\n{candidate_answer}\n", file=sys.stdout)
    choice = input("人工判断 [better/same/worse/inconclusive]: ").strip()
    return choice if choice in {"better", "same", "worse", "inconclusive"} else "inconclusive"  # type: ignore[return-value]


def _validate_private_report_path(path: Path) -> None:
    parent = path.parent.resolve(strict=True)
    parent_info = parent.stat()
    if parent_info.st_uid != os.getuid() or parent_info.st_mode & 0o077:
        raise ComparisonError("report_directory_not_private")
    if path.exists() or path.is_symlink():
        raise ComparisonError("report_already_exists")


def _private_report(path: Path, report: ComparisonReport) -> None:
    _validate_private_report_path(path)
    raw = json.dumps(asdict(report), ensure_ascii=False, sort_keys=True, indent=2).encode("utf-8") + b"\n"
    descriptor = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW, 0o600)
    with os.fdopen(descriptor, "wb") as stream:
        stream.write(raw)


def compare(args: argparse.Namespace) -> ComparisonReport:
    if args.interactive and (not sys.stdin.isatty() or not sys.stdout.isatty()):
        raise ComparisonError("interactive_tty_required")
    connection = comparison_connection(
        env_file=Path(args.env_file) if getattr(args, "env_file", None) else None,
        api_base_url=args.api_base_url,
        environ=os.environ,
    )
    cases, suite_sha256 = _private_scenarios(Path(args.scenarios))
    with httpx.Client(
        base_url=connection.api_base_url, headers={"Authorization": f"Bearer {connection.api_key}"}, timeout=args.timeout_seconds, trust_env=False
    ) as client:
        baseline_sha, candidate_sha = _versions(client, args.agent_id, args.change_set_id)
        case_evidence: list[CaseEvidence] = []
        for scenario in cases:
            baseline, baseline_answer = _run_version(client, scenario=scenario, agent_id=args.agent_id, commit_sha=baseline_sha, change_set_id=None)
            candidate, candidate_answer = _run_version(
                client, scenario=scenario, agent_id=args.agent_id, commit_sha=candidate_sha, change_set_id=args.change_set_id
            )
            prompt_sha256, prompt_utf8_length = _digest(scenario.message)
            case_evidence.append(
                CaseEvidence(
                    case_id=scenario.case_id,
                    prompt_sha256=prompt_sha256,
                    prompt_utf8_length=prompt_utf8_length,
                    baseline=baseline,
                    candidate=candidate,
                    manual_judgment=_judgment(
                        scenario.case_id,
                        baseline_answer,
                        candidate_answer,
                        interactive=args.interactive
                        and baseline.status == candidate.status == "complete"
                        and baseline.cleanup == candidate.cleanup == "complete",
                    ),
                )
            )
        status = (
            "complete"
            if all(evidence.status == "complete" and evidence.cleanup == "complete" for case in case_evidence for evidence in (case.baseline, case.candidate))
            else "incomplete"
        )
        return ComparisonReport(
            kind="agent_candidate_manual_comparison",
            purpose="human_reference_only_not_release_evidence",
            created_at=datetime.now(timezone.utc).isoformat(),
            agent_id=args.agent_id,
            change_set_id=args.change_set_id,
            baseline_commit_sha=baseline_sha,
            candidate_commit_sha=candidate_sha,
            scenario_set_sha256=suite_sha256,
            cases=case_evidence,
            status=status,
        )


def main() -> int:
    parser = argparse.ArgumentParser(description="用真实测试 Session 生成当前版/候选版人工对照报告；不影响发布门。")
    connection = parser.add_mutually_exclusive_group(required=True)
    connection.add_argument("--api-base-url", help="手动连接地址，API_KEY 从进程环境读取")
    connection.add_argument("--env-file", help="公共 Make 入口所选完整 env；不叠加进程里的 API_KEY")
    parser.add_argument("--agent-id", required=True)
    parser.add_argument("--change-set-id", required=True)
    parser.add_argument("--scenarios", required=True, help="仅属主可读的 JSONL 场景集")
    parser.add_argument("--report", required=True, help="现有私有目录中的新报告路径，不能覆盖")
    parser.add_argument("--timeout-seconds", type=float, default=180.0)
    parser.add_argument("--interactive", action="store_true", help="仅 TTY 当场显示回答并收集人工参考判断")
    args = parser.parse_args()
    try:
        if not 1 <= args.timeout_seconds <= 900:
            raise ComparisonError("invalid_timeout")
        _validate_private_report_path(Path(args.report))
        report = compare(args)
        _private_report(Path(args.report), report)
    except (ComparisonError, OSError) as exc:
        code = str(exc) if isinstance(exc, ComparisonError) else "private_file_io_error"
        print(f"comparison failed: {code}", file=sys.stderr)
        return 2
    print(f"comparison {report.status}: {len(report.cases)} cases; human reference only")
    return 0 if report.status == "complete" else 1


if __name__ == "__main__":
    raise SystemExit(main())
