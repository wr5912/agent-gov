from __future__ import annotations

import subprocess
from collections.abc import Callable
from pathlib import Path
from types import SimpleNamespace

import pytest
import scripts.container_acceptance_contract as acceptance_contract
import scripts.container_acceptance_lock as acceptance_lock
import scripts.container_acceptance_make_gate as make_gate
import scripts.container_acceptance_toolchain as toolchain

from test_container_acceptance_make_gate import _prepared_gate_authority


def _write_nested_fake_makefile(repository: Path, marker: Path) -> None:
    makefile = (toolchain.REPO_ROOT / "Makefile").read_text(encoding="utf-8").splitlines()
    names = (
        "CONTAINER_ACCEPTANCE_REPO_ROOT",
        "CONTAINER_ACCEPTANCE_BOOTSTRAP_PYTHON",
        "CONTAINER_ACCEPTANCE_BOOTSTRAP",
        "CONTAINER_ACCEPTANCE_TOOLCHAIN",
        "CONTAINER_ACCEPTANCE_BOOTSTRAP_LOADER",
        "REQUIRE_CONTAINER_ACCEPTANCE",
    )
    assignments = [next(line for line in makefile if line.startswith(f"override {name} ")) for name in names]
    repository.chmod(0o700)
    (repository / "Makefile").write_text(
        "\n".join(
            (
                "override SHELL := /bin/sh",
                "override .SHELLFLAGS := -c",
                *assignments,
                'override PYTHON_RUN := "$(CONTAINER_ACCEPTANCE_BOOTSTRAP_PYTHON)" -I -S -X pycache_prefix=/dev/null -c "$(CONTAINER_ACCEPTANCE_BOOTSTRAP_LOADER)" "$(CONTAINER_ACCEPTANCE_BOOTSTRAP)" python',
                'override CONTAINER_ACCEPTANCE_MAKE := "$${AGENT_GOV_ACCEPTANCE_MAKE}"',
                "_container-core-smoke:",
                "\t@$(REQUIRE_CONTAINER_ACCEPTANCE)",
                f"\t@printf '%s\\n' _container-core-smoke >> '{marker}'",
                "\t+@$(CONTAINER_ACCEPTANCE_MAKE) --no-print-directory --keep-going _smoke _ui-smoke _container-openapi-check",
                "_smoke:",
                "\t@$(REQUIRE_CONTAINER_ACCEPTANCE)",
                f"\t@printf '%s\\n' _smoke >> '{marker}'",
                "_ui-smoke:",
                "\t@$(REQUIRE_CONTAINER_ACCEPTANCE)",
                f"\t@printf '%s\\n' _ui-smoke >> '{marker}'",
                "_container-openapi-check:",
                "\t@$(REQUIRE_CONTAINER_ACCEPTANCE)",
                f"\t@printf '%s\\n' _container-openapi-check >> '{marker}'",
                "",
            )
        ),
        encoding="utf-8",
    )
    (repository / "Makefile").chmod(0o400)
    repository.chmod(0o500)


def test_gate_allows_four_consecutive_nested_make_permits_without_closing_parent_descriptor(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    observed: dict[str, str] = {}
    browser_validations: list[str] = []
    calls = {"helper": 0, "make": 0, "authority": 0, "response": 0}
    marker = tmp_path / "nested-bodies"
    originals: dict[str, Callable[..., object]] = {
        "helper": make_gate._verify_helper_process,
        "make": make_gate._make_process,
        "authority": make_gate._validate_authority,
        "response": make_gate._response,
    }

    def counted(name: str) -> Callable[..., object]:
        def wrapper(*args: object) -> object:
            calls[name] += 1
            return originals[name](*args)

        return wrapper

    monkeypatch.setattr(acceptance_contract, "validate_managed_environment", lambda _managed: None)
    monkeypatch.setattr(toolchain, "validate_execution_tool_authority", lambda *_args, **_kwargs: None)
    monkeypatch.setattr(toolchain, "validate_browser_runtime_authority", lambda: browser_validations.append("browser"))
    monkeypatch.setattr(make_gate, "_verify_helper_process", counted("helper"))
    monkeypatch.setattr(make_gate, "_make_process", counted("make"))
    monkeypatch.setattr(make_gate, "_validate_authority", counted("authority"))
    monkeypatch.setattr(make_gate, "_response", counted("response"))
    with (
        acceptance_lock.lifecycle_lock({}) as lock,
        _prepared_gate_authority(
            monkeypatch,
            tmp_path,
            lock,
            target="_container-core-smoke",
            makefile_writer=lambda repository: _write_nested_fake_makefile(repository, marker),
        ) as (verifier, receipt, managed),
    ):
        repository = receipt.identity.candidate_snapshot.snapshot.repository_root
        command = list(acceptance_contract.verifier_execution_argv("core", verifier, repository))
        candidate = SimpleNamespace(
            recovery=receipt.identity.candidate_snapshot,
            snapshot_repository_root=repository,
        )

        def process_runner(
            argv: list[str],
            environment: dict[str, str],
            pass_fds: tuple[int, ...],
        ) -> int:
            completed = subprocess.run(
                argv,
                check=False,
                cwd=repository,
                env=environment,
                pass_fds=pass_fds,
                start_new_session=True,
                capture_output=True,
                text=True,
                timeout=20,
            )
            observed["stderr"] = completed.stderr
            return completed.returncode

        result = make_gate.run_make_verifier(
            command,
            managed,
            profile="core",
            verifier=verifier,
            candidate=candidate,
            receipt=receipt,
            lock=lock,
            process_runner=process_runner,
        )

    assert result == 0, observed.get("stderr", "")
    assert marker.read_text(encoding="utf-8").splitlines() == [
        "_container-core-smoke",
        "_smoke",
        "_ui-smoke",
        "_container-openapi-check",
    ]
    assert calls == {"helper": 4, "make": 4, "authority": 5, "response": 4}
    assert browser_validations == ["browser"]
