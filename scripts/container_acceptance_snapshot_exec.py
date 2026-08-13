"""把候选 bootstrap 的 Prepared 摘要验证与同一 fd 执行绑定。"""

from __future__ import annotations

import json
import os
from collections.abc import Mapping
from pathlib import Path
from typing import NoReturn

from scripts import container_acceptance_tool_authority as tool_authority


class SnapshotExecAuthorityError(RuntimeError):
    """候选 bootstrap 不是 Prepared 冻结的实际字节。"""


def _snapshot(environment: Mapping[str, str]) -> list[object]:
    raw = environment.get("AGENT_GOV_PREPARED_CANDIDATE_AUTHORITY", "")
    if not raw or len(raw.encode()) > 64 * 1024:
        raise SnapshotExecAuthorityError("prepared candidate authority is invalid")
    try:
        payload = json.loads(raw)
    except (UnicodeError, ValueError) as exc:
        raise SnapshotExecAuthorityError("prepared candidate authority is invalid") from exc
    if (
        not isinstance(payload, dict)
        or set(payload) != {"contract", "source", "snapshot"}
        or payload.get("contract") != "agentgov.container-acceptance-candidate.v5"
        or not isinstance(payload.get("snapshot"), list)
        or len(payload["snapshot"]) != 32
    ):
        raise SnapshotExecAuthorityError("prepared candidate authority is invalid")
    return payload["snapshot"]


def _source_digest(snapshot: list[object], relative: str) -> str:
    loaded = snapshot[24]
    if not isinstance(loaded, list) or not 0 < len(loaded) <= 64:
        raise SnapshotExecAuthorityError("prepared source authority is invalid")
    matches = [item[1] for item in loaded if isinstance(item, list) and len(item) == 2 and item[0] == relative]
    if len(matches) != 1 or not isinstance(matches[0], str) or len(matches[0]) != 64:
        raise SnapshotExecAuthorityError("prepared source authority is invalid")
    return matches[0]


def exec_snapshot_python(
    entrypoint: Path,
    arguments: tuple[str, ...],
    environment: Mapping[str, str],
    *,
    python_record: Mapping[str, object],
) -> NoReturn:
    expected_entrypoint = Path(os.path.abspath(entrypoint))
    snapshot = _snapshot(environment)
    snapshot_root = Path(str(snapshot[5]))
    repository = Path(str(snapshot[7]))
    expected_runner = repository / "scripts/run_container_acceptance.py"
    relative = "scripts/container_acceptance_bootstrap.py"
    if (
        snapshot_root != repository.parent
        or expected_entrypoint != repository / relative
        or len(arguments) < 2
        or arguments[:2] != ("resume", str(expected_runner))
        or environment.get("AGENT_GOV_ACCEPTANCE_SNAPSHOT_ROOT") != str(snapshot_root)
    ):
        raise SnapshotExecAuthorityError("candidate acceptance entrypoint is invalid")
    expected_sha256 = _source_digest(snapshot, relative)
    script_record: tool_authority.ToolAuthority
    try:
        script_record, _encoded = tool_authority.capture_tool_file(
            expected_entrypoint,
            "candidate-acceptance-bootstrap",
            capture=True,
            allow_sticky_ancestor=True,
        )
        if script_record["sha256"] != expected_sha256:
            raise SnapshotExecAuthorityError("candidate bootstrap bytes do not match Prepared authority")
        python_descriptor = tool_authority.open_verified_executable(python_record)
        script_descriptor = tool_authority.open_verified_file(
            script_record,
            require_executable=False,
            allow_sticky_ancestor=True,
        )
    except tool_authority.ToolFileAuthorityError as exc:
        raise SnapshotExecAuthorityError("fixed Python execution authority drifted") from exc
    try:
        os.set_inheritable(python_descriptor, True)
        os.set_inheritable(script_descriptor, True)
        invocation = str(python_record["invocation_path"])
        os.execve(
            python_descriptor,
            (
                invocation,
                "-I",
                "-P",
                "-S",
                "-X",
                "pycache_prefix=/dev/null",
                f"/proc/self/fd/{script_descriptor}",
                *arguments,
            ),
            dict(environment),
        )
    finally:
        os.close(script_descriptor)
        os.close(python_descriptor)
