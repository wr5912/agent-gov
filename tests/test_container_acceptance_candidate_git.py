from __future__ import annotations

import os
import subprocess
import tempfile
from collections.abc import Sequence
from pathlib import Path

import pytest
import scripts.container_acceptance_candidate_git as candidate_git
import scripts.container_acceptance_toolchain as acceptance_toolchain


def test_fixed_candidate_git_validation_uses_only_git_execution_authority(monkeypatch: pytest.MonkeyPatch) -> None:
    calls: list[tuple[str, ...] | None] = []
    monkeypatch.setattr(
        acceptance_toolchain,
        "validate_execution_tool_authority",
        lambda _environ=None, *, commands=None: calls.append(commands),
    )
    monkeypatch.setattr(
        acceptance_toolchain,
        "validate_toolchain_authority",
        lambda *_args, **_kwargs: pytest.fail("Git validation must not recapture unrelated dependency trees"),
    )

    candidate_git.FixedCandidateGitAuthority().validate()

    assert calls == [("git",)]


def _stub_fixed_git_process(
    monkeypatch: pytest.MonkeyPatch,
    *,
    output: bytes = b"ok\n",
    returncode: int = 0,
) -> None:
    monkeypatch.setattr(candidate_git.acceptance_toolchain, "git_argv", lambda _repository, *_arguments: ("git", "status"))
    monkeypatch.setattr(candidate_git.acceptance_toolchain, "git_environment", lambda **_kwargs: {})

    def run(command: Sequence[str], **kwargs: object) -> subprocess.CompletedProcess[bytes]:
        descriptor = kwargs["stdout"]
        assert isinstance(descriptor, int)
        os.write(descriptor, output)
        return subprocess.CompletedProcess(command, returncode)

    monkeypatch.setattr(candidate_git.subprocess, "run", run)


def test_fixed_candidate_git_run_does_not_depend_on_tmpdir(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    _stub_fixed_git_process(monkeypatch)
    monkeypatch.setenv("TMPDIR", str(tmp_path / "already-cleaned"))
    monkeypatch.setattr(tempfile, "tempdir", None)
    monkeypatch.setattr(tempfile, "TemporaryFile", lambda: pytest.fail("candidate Git must not use ambient temporary paths"))

    assert candidate_git.FixedCandidateGitAuthority().run(tmp_path, ("status",), max_output_bytes=3) == b"ok\n"


@pytest.mark.parametrize("failure", ["memfd", "timeout"])
def test_fixed_candidate_git_run_fails_closed_when_execution_is_unavailable(monkeypatch: pytest.MonkeyPatch, tmp_path: Path, failure: str) -> None:
    _stub_fixed_git_process(monkeypatch)
    if failure == "memfd":
        monkeypatch.setattr(candidate_git, "_open_git_output_memfd", lambda: (_ for _ in ()).throw(OSError("denied")))
        monkeypatch.setattr(candidate_git.subprocess, "run", lambda *_args, **_kwargs: pytest.fail("Git must not run without a sink"))
    else:
        monkeypatch.setattr(candidate_git.subprocess, "run", lambda *_args, **_kwargs: (_ for _ in ()).throw(subprocess.TimeoutExpired("git", 30)))

    with pytest.raises(candidate_git.CandidateSnapshotError, match="could not be completed"):
        candidate_git.FixedCandidateGitAuthority().run(tmp_path, ("status",))


@pytest.mark.parametrize(("output", "returncode"), [(b"oversized", 0), (b"ok", 2)])
def test_fixed_candidate_git_run_rejects_overflow_and_child_failure(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
    output: bytes,
    returncode: int,
) -> None:
    _stub_fixed_git_process(monkeypatch, output=output, returncode=returncode)

    with pytest.raises(candidate_git.CandidateSnapshotError, match="bounded contract"):
        candidate_git.FixedCandidateGitAuthority().run(tmp_path, ("status",), max_output_bytes=4)
