from __future__ import annotations

import subprocess
from pathlib import Path

import pytest
import scripts.container_acceptance_contract as acceptance_contract
import scripts.container_acceptance_make_gate as make_gate
import scripts.container_acceptance_toolchain as toolchain


@pytest.mark.parametrize(
    "environment",
    (
        {make_gate.MAKE_GATE_FD_ENV: "9"},
        {make_gate.MAKE_GATE_NONCE_ENV: "1" * 32},
        {make_gate.MAKE_GATE_FD_ENV: "2", make_gate.MAKE_GATE_NONCE_ENV: "1" * 32},
        {make_gate.MAKE_GATE_FD_ENV: "9", make_gate.MAKE_GATE_NONCE_ENV: "not-a-nonce"},
    ),
)
def test_partial_or_invalid_gate_environment_fails_closed(environment: dict[str, str]) -> None:
    with pytest.raises(make_gate.MakeGateError, match="environment"):
        make_gate.validate_make_gate_environment(environment)


def test_missing_gate_overlay_cli_reports_bounded_failure_without_echoing_environment_or_paths() -> None:
    secret = "private-env-value-must-not-appear"
    result = subprocess.run(
        [
            str(toolchain.REPO_ROOT / ".venv/bin/python"),
            str(toolchain.REPO_ROOT / "scripts/container_acceptance_make_gate.py"),
            "check",
            "_smoke",
        ],
        check=False,
        capture_output=True,
        text=True,
        env={"LC_ALL": "C.UTF-8", "PRIVATE_VALUE": secret},
        timeout=5,
    )

    assert result.returncode == 1
    assert result.stdout == ""
    assert result.stderr == "failure_phase=gate_open failure_code=MakeGateError.KeyError\n"
    assert secret not in result.stderr
    assert str(toolchain.REPO_ROOT) not in result.stderr


def test_late_makefile_list_guard_rejects_an_extra_parser_source(tmp_path: Path) -> None:
    extra = tmp_path / "extra.mk"
    extra.write_text("$(info $(CONTAINER_ACCEPTANCE))\n", encoding="utf-8")
    result = subprocess.run(
        [
            "/usr/bin/make",
            "-pRrq",
            "-f",
            str(toolchain.REPO_ROOT / "Makefile"),
            "-f",
            str(extra),
            "setup",
        ],
        check=False,
        capture_output=True,
        text=True,
        cwd=toolchain.REPO_ROOT,
        env={"LC_ALL": "C.UTF-8", "MAKEFLAGS": ""},
        timeout=5,
    )

    assert result.returncode != 0
    assert "rejects alternate MAKEFILE_LIST authority" in result.stderr


def test_make_guard_invocation_is_override_protected_and_snapshot_absolute() -> None:
    makefile = (toolchain.REPO_ROOT / "Makefile").read_text(encoding="utf-8")
    assignment = next(line for line in makefile.splitlines() if line.startswith("override REQUIRE_CONTAINER_ACCEPTANCE = "))
    targets = {verifier.invocation_argv[-1] for verifiers in acceptance_contract.VERIFIER_REGISTRY.values() for verifier in verifiers}
    invocations = [line.strip() for line in makefile.splitlines() if "$(CONTAINER_ACCEPTANCE_MAKE)" in line and targets.intersection(line.split())]

    assert '"$(CONTAINER_ACCEPTANCE_REPO_ROOT)/scripts/container_acceptance_make_gate.py"' in assignment
    assert assignment.endswith('check "$@"')
    assert invocations == ["+@$(CONTAINER_ACCEPTANCE_MAKE) --no-print-directory --keep-going _smoke _ui-smoke _container-openapi-check"]
    assert all("--jobs" not in line and not any(token.startswith("-j") for token in line.split()) for line in invocations)


def test_browser_targets_require_fresh_runtime_authority_before_permit() -> None:
    assert {
        "_container-health-e2e",
        "_container-openapi-check",
        "_ui-feedback-smoke",
        "_ui-openai-responses-smoke",
        "_ui-playground-cancel-smoke",
    } == make_gate._BROWSER_AUTHORITY_TARGETS
