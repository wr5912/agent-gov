from __future__ import annotations

import hashlib
import json
import os
import subprocess
from pathlib import Path

import pytest
from app.agent_testing import materializer as materializer_module
from app.agent_testing.execution_contracts import (
    FIXED_PYTEST_COMMAND,
    P0_EXACT_COMMIT_LANE,
    RECEIPT_CONTRACT,
    AgentTestCleanupReceipt,
    AgentTestExecutionReceipt,
    AgentTestInvocationReceipt,
    AgentTestIsolationReceipt,
    AgentTestResultReceipt,
    AgentTestSandboxMountReceipt,
    AgentTestTargetReceipt,
    SourceObservation,
    canonical_json_digest,
    sandbox_environment,
    sandbox_environment_digest,
    verify_receipt_integrity,
)
from app.agent_testing.materializer import MaterializationError, materialize_git_commit
from app.agent_testing.suite import inspect_agent_test_suite
from pydantic import ValidationError

_GIT = "/usr/bin/git"
_EMPTY_DIGEST = hashlib.sha256(b"").hexdigest()


def _git(repository: Path, *args: str, input_bytes: bytes | None = None) -> str:
    result = subprocess.run(
        [_GIT, *args],
        cwd=repository,
        input=input_bytes,
        capture_output=True,
        check=False,
    )
    assert result.returncode == 0, result.stderr.decode("utf-8", errors="replace")
    return result.stdout.decode("ascii").strip()


def _repository(tmp_path: Path) -> Path:
    repository = tmp_path / "repository"
    repository.mkdir(parents=True)
    _git(repository, "init", "-q")
    _git(repository, "config", "user.name", "AgentGov Tests")
    _git(repository, "config", "user.email", "agentgov@example.invalid")
    return repository


def _commit(repository: Path, message: str = "test source", *, stage: bool = True) -> str:
    if stage:
        _git(repository, "add", "-A", "-f")
    _git(repository, "commit", "-q", "-m", message)
    return _git(repository, "rev-parse", "HEAD")


def _write_static_suite(repository: Path, *, assertion: str = "True") -> None:
    tests_dir = repository / "tests"
    tests_dir.mkdir(exist_ok=True)
    tests_dir.joinpath("README.md").write_text("# tests\n", encoding="utf-8")
    tests_dir.joinpath("test_static.py").write_text(f"def test_static():\n    assert {assertion}\n", encoding="utf-8")


def test_materializer_reads_exact_raw_commit_and_ignores_dirty_worktree_and_replace_refs(tmp_path: Path) -> None:
    repository = _repository(tmp_path)
    _write_static_suite(repository, assertion="1 == 1")
    baseline_commit = _commit(repository, "baseline")

    _write_static_suite(repository, assertion="2 == 2")
    replacement_commit = _commit(repository, "replacement")
    _git(repository, "replace", baseline_commit, replacement_commit)
    repository.joinpath("tests", "test_static.py").write_text("raise AssertionError('dirty')\n", encoding="utf-8")
    repository.joinpath("untracked.txt").write_text("dirty\n", encoding="utf-8")

    first_destination = tmp_path / "first"
    second_destination = tmp_path / "second"
    first = materialize_git_commit(repository, baseline_commit, first_destination)
    second = materialize_git_commit(repository, baseline_commit, second_destination)

    expected = "def test_static():\n    assert 1 == 1\n"
    assert first_destination.joinpath("tests", "test_static.py").read_text(encoding="utf-8") == expected
    assert first == second
    assert first.commit_sha == baseline_commit
    assert first.file_count == 2
    assert first.total_bytes > 0
    assert len(first.tree_sha) == 40
    assert len(first.source_digest) == 64
    assert "untracked.txt" not in {path.relative_to(first_destination).as_posix() for path in first_destination.rglob("*")}


def test_materialized_suite_classifies_agent_live_fixture_without_making_suite_unrunnable(tmp_path: Path) -> None:
    repository = _repository(tmp_path)
    _write_static_suite(repository)
    repository.joinpath("tests", "test_static.py").write_text(
        "def test_live(agent):\n    assert agent is not None\n",
        encoding="utf-8",
    )
    commit_sha = _commit(repository)
    destination = tmp_path / "materialized"
    materialize_git_commit(repository, commit_sha, destination)

    suite = inspect_agent_test_suite(destination, agent_id="agent-a", commit_sha=commit_sha)

    assert suite.runnable is True
    assert suite.requires_live_agent is True
    assert suite.live_test_files == ["tests/test_static.py"]
    assert {item.code for item in suite.diagnostics} == {"AGENT_TEST_LIVE_FIXTURE_REQUIRES_P1"}


@pytest.mark.parametrize(
    "path",
    [
        ".env",
        ".aws/credentials",
        ".claude/settings.local.json",
        ".docker/config.json",
        ".git-credentials",
        "credentials.json",
        "secret/token.txt",
        "secrets/token.txt",
        "ignored.secret",
        "private.pem.txt",
    ],
)
def test_materializer_omits_sensitive_local_paths_from_the_sandbox_projection(tmp_path: Path, path: str) -> None:
    repository = _repository(tmp_path)
    _write_static_suite(repository)
    target = repository / path
    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_text("must-not-enter-sandbox\n", encoding="utf-8")
    commit_sha = _commit(repository)
    destination = tmp_path / "materialized"

    fingerprint = materialize_git_commit(repository, commit_sha, destination)

    assert fingerprint.commit_sha == commit_sha
    assert destination.joinpath("tests", "test_static.py").is_file()
    assert not destination.joinpath(*Path(path).parts).exists()


def test_materializer_rejects_ambiguous_trailing_space_path_before_writing(tmp_path: Path) -> None:
    repository = _repository(tmp_path)
    _write_static_suite(repository)
    repository.joinpath(".env ").write_text("must-not-enter-sandbox\n", encoding="utf-8")
    commit_sha = _commit(repository)
    destination = tmp_path / "materialized"

    with pytest.raises(MaterializationError) as raised:
        materialize_git_commit(repository, commit_sha, destination)

    assert raised.value.code == "AGENT_SOURCE_PATH_INVALID"
    assert not destination.exists()


def test_sensitive_only_change_updates_full_tree_but_not_sandbox_source_digest(tmp_path: Path) -> None:
    repository = _repository(tmp_path)
    _write_static_suite(repository)
    repository.joinpath(".env").write_text("TOKEN=first\n", encoding="utf-8")
    first_commit = _commit(repository, "first secret")
    first = materialize_git_commit(repository, first_commit, tmp_path / "first")

    repository.joinpath(".env").write_text("TOKEN=second\n", encoding="utf-8")
    second_commit = _commit(repository, "second secret")
    second = materialize_git_commit(repository, second_commit, tmp_path / "second")

    assert first.tree_sha != second.tree_sha
    assert first.source_digest == second.source_digest
    assert first.file_count == second.file_count == 2


def test_materializer_rejects_symlink_and_gitlink_entries(tmp_path: Path) -> None:
    symlink_repository = _repository(tmp_path / "symlink-case")
    symlink_repository.joinpath("target.txt").write_text("target\n", encoding="utf-8")
    os.symlink("target.txt", symlink_repository / "link.txt")
    symlink_commit = _commit(symlink_repository)

    with pytest.raises(MaterializationError) as symlinked:
        materialize_git_commit(symlink_repository, symlink_commit, tmp_path / "symlink-output")
    assert symlinked.value.code == "AGENT_SOURCE_SYMLINK_FORBIDDEN"

    gitlink_repository = _repository(tmp_path / "gitlink-case")
    gitlink_repository.joinpath("base.txt").write_text("base\n", encoding="utf-8")
    base_commit = _commit(gitlink_repository, "base")
    _git(gitlink_repository, "update-index", "--add", "--cacheinfo", f"160000,{base_commit},vendor")
    gitlink_commit = _commit(gitlink_repository, "gitlink", stage=False)

    with pytest.raises(MaterializationError) as gitlinked:
        materialize_git_commit(gitlink_repository, gitlink_commit, tmp_path / "gitlink-output")
    assert gitlinked.value.code == "AGENT_SOURCE_GITLINK_FORBIDDEN"


def test_materializer_rejects_non_utf8_git_path(tmp_path: Path) -> None:
    repository = _repository(tmp_path)
    blob_sha = _git(repository, "hash-object", "-w", "--stdin", input_bytes=b"content\n")
    tree_sha = _git(
        repository,
        "mktree",
        "-z",
        input_bytes=f"100644 blob {blob_sha}\t".encode("ascii") + b"invalid-\xff\0",
    )
    commit_sha = _git(repository, "commit-tree", tree_sha, input_bytes=b"invalid path\n")

    with pytest.raises(MaterializationError) as raised:
        materialize_git_commit(repository, commit_sha, tmp_path / "output")

    assert raised.value.code == "AGENT_SOURCE_PATH_INVALID"


def test_materializer_enforces_file_count_and_destination_boundaries(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    repository = _repository(tmp_path)
    repository.joinpath("one.txt").write_text("one\n", encoding="utf-8")
    repository.joinpath("two.txt").write_text("two\n", encoding="utf-8")
    commit_sha = _commit(repository)
    monkeypatch.setattr(materializer_module, "MAX_SOURCE_FILES", 1)

    with pytest.raises(MaterializationError) as oversized:
        materialize_git_commit(repository, commit_sha, tmp_path / "oversized")
    assert oversized.value.code == "AGENT_SOURCE_TOO_LARGE"

    monkeypatch.setattr(materializer_module, "MAX_SOURCE_FILES", 10_000)
    existing = tmp_path / "existing"
    existing.mkdir()
    with pytest.raises(MaterializationError) as destination:
        materialize_git_commit(repository, commit_sha, existing)
    assert destination.value.code == "AGENT_SOURCE_DESTINATION_INVALID"


def test_materializer_accepts_placeholder_only_mcp_credentials(tmp_path: Path) -> None:
    repository = _repository(tmp_path)
    repository.joinpath(".mcp.json").write_text(
        json.dumps(
            {
                "mcpServers": {
                    "safe": {
                        "type": "http",
                        "url": "${SAFE_MCP_URL}",
                        "headers": {
                            "Authorization": "Bearer ${SAFE_MCP_TOKEN}",
                            "Accept": "application/json",
                        },
                    }
                }
            }
        ),
        encoding="utf-8",
    )
    commit_sha = _commit(repository)

    fingerprint = materialize_git_commit(repository, commit_sha, tmp_path / "output")

    assert fingerprint.file_count == 1


@pytest.mark.parametrize(
    "server_config",
    [
        {"type": "http", "url": "https://mcp.example.invalid", "headers": {"Authorization": "Bearer literal-token"}},
        {"type": "http", "url": "https://mcp.example.invalid", "headers": {"X-Api-Key": "literal-api-key"}},
        {"type": "http", "url": "https://user:password@mcp.example.invalid"},
        {"type": "http", "url": "https://mcp.example.invalid?access_token=literal-token"},
        {"type": "http", "url": "https://mcp.example.invalid?accessToken=literal-token"},
        {"command": "safe-command", "env": {"MCP_TOKEN": "literal-token"}},
        {"command": "safe-command", "args": ["--header", "Authorization: Bearer literal-token"]},
        {"command": "safe-command", "args": ["--token", "literal-token"]},
        {"command": "safe-command", "args": ["--api-key", "literal-api-key"]},
        {"command": "safe-command", "clientSecret": "literal-secret"},
        {"command": "safe-command", "privateKey": "literal-private-key"},
        {"command": "safe-command", "proxyAuthorization": "literal-authorization"},
    ],
)
def test_materializer_omits_literal_mcp_credentials_from_projection(tmp_path: Path, server_config: dict[str, object]) -> None:
    repository = _repository(tmp_path)
    repository.joinpath(".mcp.json").write_text(
        json.dumps({"mcpServers": {"hostile": server_config}}),
        encoding="utf-8",
    )
    commit_sha = _commit(repository)
    destination = tmp_path / "output"

    fingerprint = materialize_git_commit(repository, commit_sha, destination)

    assert fingerprint.commit_sha == commit_sha
    assert fingerprint.file_count == 0
    assert fingerprint.total_bytes == 0
    assert not destination.joinpath(".mcp.json").exists()


def test_literal_mcp_secret_change_updates_full_tree_but_not_sandbox_source_digest(tmp_path: Path) -> None:
    repository = _repository(tmp_path)
    repository.joinpath(".mcp.json").write_text(
        json.dumps({"mcpServers": {"private": {"command": "tool", "env": {"TOKEN": "first"}}}}),
        encoding="utf-8",
    )
    first_commit = _commit(repository, "first mcp secret")
    first = materialize_git_commit(repository, first_commit, tmp_path / "first")

    repository.joinpath(".mcp.json").write_text(
        json.dumps({"mcpServers": {"private": {"command": "tool", "env": {"TOKEN": "second"}}}}),
        encoding="utf-8",
    )
    second_commit = _commit(repository, "second mcp secret")
    second = materialize_git_commit(repository, second_commit, tmp_path / "second")

    assert first.tree_sha != second.tree_sha
    assert first.source_digest == second.source_digest
    assert first.file_count == second.file_count == 0


def test_materializer_keeps_placeholder_only_settings_and_omits_literal_settings_env(tmp_path: Path) -> None:
    repository = _repository(tmp_path)
    _write_static_suite(repository)
    settings_path = repository / ".claude" / "settings.json"
    settings_path.parent.mkdir(parents=True)
    settings_path.write_text(
        json.dumps({"env": {"SAFE_TOKEN": "${SAFE_TOKEN}"}, "permissions": {"ask": []}}),
        encoding="utf-8",
    )
    safe_commit = _commit(repository, "placeholder settings")
    safe_destination = tmp_path / "safe"
    materialize_git_commit(repository, safe_commit, safe_destination)
    assert safe_destination.joinpath(".claude", "settings.json").is_file()

    settings_path.write_text(
        json.dumps({"env": {"SAFE_TOKEN": "literal-token"}, "permissions": {"ask": []}}),
        encoding="utf-8",
    )
    literal_commit = _commit(repository, "literal settings")
    literal_destination = tmp_path / "literal"
    materialize_git_commit(repository, literal_commit, literal_destination)
    assert not literal_destination.joinpath(".claude", "settings.json").exists()


def _invocation_receipt() -> AgentTestInvocationReceipt:
    return AgentTestInvocationReceipt(
        image_id=f"sha256:{'b' * 64}",
        argv=FIXED_PYTEST_COMMAND,
        environment_keys=tuple(sorted(sandbox_environment())),
        environment_digest=sandbox_environment_digest(),
        working_directory="/workspace",
    )


def _isolation_receipt() -> AgentTestIsolationReceipt:
    return AgentTestIsolationReceipt(
        user="65532:65532",
        network_mode="none",
        network_disabled=True,
        pid_mode="private",
        ipc_mode="private",
        uts_mode="private",
        readonly_rootfs=True,
        cap_drop=("ALL",),
        security_opt=("no-new-privileges",),
        privileged=False,
        devices=(),
        mounts=(
            AgentTestSandboxMountReceipt(
                target="/workspace",
                read_only=True,
                mount_type="volume",
                source_scope="run_workspace_subpath",
            ),
        ),
        pids_limit=256,
        memory_bytes=536870912,
        memory_swap_bytes=536870912,
        nano_cpus=1000000000,
        tmpfs_targets=("/output", "/tmp"),
        tmpfs_size_bytes=67108864,
        tmpfs_noexec=True,
        tmpfs_nosuid=True,
        tmpfs_nodev=True,
        shm_size_bytes=16777216,
        ports_published=False,
        auto_remove=False,
        restart_policy="no",
        log_driver="local",
        log_max_bytes=1048576,
        log_max_files=1,
        log_compression=False,
        docker_socket_mounted=False,
    )


def _result_receipt(status: str) -> AgentTestResultReceipt:
    return AgentTestResultReceipt(
        status=status,
        exit_code=0 if status == "passed" else 1 if status == "failed" else None,
        duration_ms=12,
        workspace_report_authority="agent_owned_unverified",
        workspace_report_digest=canonical_json_digest({}),
        stdout_digest=_EMPTY_DIGEST,
        stderr_digest=_EMPTY_DIGEST,
    )


def _cleanup_receipt(cleanup_complete: bool) -> AgentTestCleanupReceipt:
    return AgentTestCleanupReceipt(
        container_removed=cleanup_complete,
        label_residue_absent=cleanup_complete,
        temporary_paths_removed=cleanup_complete,
        error_codes=() if cleanup_complete else ("AGENT_TEST_CLEANUP_FAILED",),
    )


def _receipt(
    *,
    status: str = "passed",
    cleanup_complete: bool = True,
    source_observation: SourceObservation | None = None,
) -> AgentTestExecutionReceipt:
    digest = "a" * 64
    observation = source_observation or ("stable" if status in {"passed", "failed"} else "not_observed")
    pre_digest = digest if observation != "not_observed" else None
    post_digest = "f" * 64 if observation == "changed" else digest if observation == "stable" else None
    executed = status in {"passed", "failed"}
    return AgentTestExecutionReceipt(
        contract=RECEIPT_CONTRACT,
        lane=P0_EXACT_COMMIT_LANE,
        assurance_level="execution_provenance",
        test_run_id="atr-test",
        worker_id="worker-test",
        container_id="c" * 64,
        target=AgentTestTargetReceipt(
            agent_id="agent-a",
            commit_sha="1" * 40,
            tree_sha="2" * 40,
            source_digest=digest,
            pre_source_digest=pre_digest,
            post_source_digest=post_digest,
            source_observation=observation,
            suite_digest="3" * 64,
        ),
        invocation=_invocation_receipt() if executed else None,
        isolation=_isolation_receipt() if executed else None,
        result=_result_receipt(status),
        cleanup=_cleanup_receipt(cleanup_complete),
    )


def test_receipt_is_strict_canonical_and_detects_tampering() -> None:
    signed = _receipt().with_digest()
    assert verify_receipt_integrity(signed) is True
    assert signed.receipt_digest == signed.with_digest().receipt_digest

    payload = signed.model_dump(mode="json")
    payload["target"]["source_digest"] = "f" * 64
    payload["target"]["pre_source_digest"] = "f" * 64
    payload["target"]["post_source_digest"] = "f" * 64
    tampered = AgentTestExecutionReceipt.model_validate(payload)
    assert verify_receipt_integrity(tampered) is False

    payload["client_command"] = ["true"]
    with pytest.raises(ValidationError):
        AgentTestExecutionReceipt.model_validate(payload)


def test_receipt_allows_typed_pre_container_error_but_cleanup_failure_forces_error() -> None:
    interrupted = _receipt(status="interrupted").with_digest()
    assert interrupted.invocation is None
    assert interrupted.isolation is None
    assert verify_receipt_integrity(interrupted) is True

    with pytest.raises(ValidationError, match="cleanup failure"):
        _receipt(status="passed", cleanup_complete=False)


@pytest.mark.parametrize(
    "target",
    [
        {
            "source_observation": "not_observed",
            "pre_source_digest": "a" * 64,
            "post_source_digest": None,
        },
        {
            "source_observation": "pre_only",
            "pre_source_digest": None,
            "post_source_digest": None,
        },
        {
            "source_observation": "stable",
            "pre_source_digest": "a" * 64,
            "post_source_digest": "f" * 64,
        },
        {
            "source_observation": "changed",
            "pre_source_digest": "a" * 64,
            "post_source_digest": "a" * 64,
        },
    ],
)
def test_receipt_rejects_hostile_source_observation_combinations(target: dict[str, object]) -> None:
    with pytest.raises(ValidationError):
        AgentTestTargetReceipt(
            agent_id="agent-a",
            commit_sha="1" * 40,
            tree_sha="2" * 40,
            source_digest="a" * 64,
            suite_digest="3" * 64,
            **target,
        )

    with pytest.raises(ValidationError, match="stable pre/post source evidence"):
        _receipt(status="passed", source_observation="pre_only")
