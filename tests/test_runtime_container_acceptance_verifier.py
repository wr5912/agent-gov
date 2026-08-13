from __future__ import annotations

import hashlib
import json
import os
import re
import shutil
import signal
import subprocess
import sys
from collections import Counter
from collections.abc import Iterator
from contextlib import contextmanager
from pathlib import Path
from types import SimpleNamespace

import pytest
import scripts.container_acceptance_contract as acceptance_contract
import scripts.container_acceptance_launcher as acceptance_launcher
from scripts.container_acceptance_profiles import PROFILES


def _tool(command: str, path: str) -> dict[str, object]:
    return {
        "command": command,
        "invocation_path": path,
        "resolved_path": path,
        "sha256": "1" * 64,
        "device": 1,
        "inode": len(command),
        "mode": 0o755,
        "uid": os.geteuid(),
        "gid": os.getegid(),
        "size": 1,
        "mtime_ns": 1,
        "ctime_ns": 1,
        "invocation_device": 1,
        "invocation_inode": len(command),
        "invocation_mode": 0o755,
        "invocation_uid": os.geteuid(),
        "invocation_gid": os.getegid(),
        "invocation_mtime_ns": 1,
        "invocation_ctime_ns": 1,
        "leaf_link": None,
        "ancestor_authority_sha256": "2" * 64,
    }


@pytest.fixture(autouse=True)
def _fixed_toolchain(monkeypatch: pytest.MonkeyPatch) -> None:
    toolchain = acceptance_contract.acceptance_toolchain
    dependency = {
        "root": "/dependency/frontend/node_modules",
        "device": 1,
        "inode": 3,
        "mode": 0o755,
        "uid": os.geteuid(),
        "gid": os.getegid(),
        "mtime_ns": 1,
        "ctime_ns": 1,
        "entries": 7,
        "regular_bytes": 11,
        "sha256": "3" * 64,
        "projection_sha256": "4" * 64,
        "generation_sha256": "5" * 64,
    }
    config = {
        "path": "/dependency/.venv/pyvenv.cfg",
        "sha256": "4" * 64,
        "device": 1,
        "inode": 2,
        "mode": 0o600,
        "uid": os.geteuid(),
        "gid": os.getegid(),
        "size": 1,
        "mtime_ns": 1,
        "ctime_ns": 1,
        "ancestor_authority_sha256": "5" * 64,
    }
    payload = {
        "contract": toolchain.TOOLCHAIN_CONTRACT,
        "node_version": "22.22.0",
        "pnpm_version": "10.30.3",
        "tools": [
            _tool("python", "/dependency/.venv/bin/python"),
            _tool("bootstrap-python", "/usr/bin/python3"),
            _tool("node", "/trusted/node/node"),
            _tool("pnpm", "/trusted/node/pnpm"),
            _tool("docker-compose", "/usr/libexec/docker/cli-plugins/docker-compose"),
            _tool("chromium", "/opt/google/chrome/chrome"),
            _tool("awk", "/usr/bin/awk"),
            _tool("bash", "/usr/bin/bash"),
            _tool("curl", "/usr/bin/curl"),
            _tool("docker", "/usr/bin/docker"),
            _tool("env", "/usr/bin/env"),
            _tool("git", "/usr/bin/git"),
            _tool("make", "/usr/bin/make"),
            _tool("sh", "/usr/bin/sh"),
            _tool("sleep", "/usr/bin/sleep"),
            _tool("tr", "/usr/bin/tr"),
        ],
        "source_contract_sha256": "6" * 64,
        "python_environment": {
            "prefix": "/dependency/.venv",
            "version": "3.11.13",
            "config": config,
            "dependency_contract_sha256": "7" * 64,
            "site_packages": {**dependency, "root": "/dependency/.venv/lib/python3.11/site-packages"},
        },
        "frontend_dependencies": dependency,
        "pnpm_runtime": {**dependency, "root": "/trusted/node/lib/node_modules/pnpm"},
        "browser_runtime": {**dependency, "root": "/opt/google/chrome", "uid": 0},
        "docker_daemon": {"socket_authority_sha256": "8" * 64, "daemon_identity_sha256": "9" * 64},
        "private_state_authority_sha256": "a" * 64,
    }
    encoded = json.dumps(payload, ensure_ascii=False, sort_keys=True, separators=(",", ":")).encode()
    authority = toolchain.CapturedToolchainAuthority(
        payload, hashlib.sha256(encoded).hexdigest(), "/trusted/node:/dependency/frontend/node_modules/.bin:/usr/bin"
    )
    monkeypatch.setattr(toolchain, "_ACTIVE_AUTHORITY", authority)
    monkeypatch.setattr(toolchain, "capture_toolchain_authority", lambda **_kwargs: authority)
    monkeypatch.setattr(toolchain, "validate_execution_tool_authority", lambda *_args, **_kwargs: None)


def _snapshot_dependency_values(root: Path) -> dict[str, str]:
    frontend = root / "repository/frontend/node_modules"
    python = root / "dependencies/python-site-packages"
    pnpm = root / "dependencies/pnpm"
    toolchain_path = root / "repository/scripts/container_acceptance_toolchain.py"
    toolchain_path.parent.mkdir(parents=True, exist_ok=True)
    toolchain_path.write_text("# frozen test toolchain\n", encoding="utf-8")
    return {
        "AGENT_GOV_ACCEPTANCE_FRONTEND_DEPENDENCY_ROOT": str(frontend),
        "AGENT_GOV_ACCEPTANCE_FRONTEND_DEPENDENCIES_SHA256": "b" * 64,
        "AGENT_GOV_ACCEPTANCE_FRONTEND_DEPENDENCIES_ENTRIES": "7",
        "AGENT_GOV_ACCEPTANCE_FRONTEND_DEPENDENCIES_BYTES": "11",
        "AGENT_GOV_ACCEPTANCE_PYTHON_SITE_PACKAGES": str(python),
        "AGENT_GOV_ACCEPTANCE_PYTHON_DEPENDENCIES_SHA256": "c" * 64,
        "AGENT_GOV_ACCEPTANCE_PYTHON_DEPENDENCIES_ENTRIES": "7",
        "AGENT_GOV_ACCEPTANCE_PYTHON_DEPENDENCIES_BYTES": "11",
        "AGENT_GOV_ACCEPTANCE_PNPM_DEPENDENCY_ROOT": str(pnpm),
        "AGENT_GOV_ACCEPTANCE_PNPM_DEPENDENCIES_SHA256": "d" * 64,
        "AGENT_GOV_ACCEPTANCE_PNPM_DEPENDENCIES_ENTRIES": "7",
        "AGENT_GOV_ACCEPTANCE_PNPM_DEPENDENCIES_BYTES": "11",
    }


def test_profile_registry_fixes_every_public_verifier_identity_and_exact_argv() -> None:
    expected = {
        "core": (
            ("ui-smoke", "_ui-smoke"),
            ("ui-feedback-smoke", "_ui-feedback-smoke"),
            ("ui-openai-responses-smoke", "_ui-openai-responses-smoke"),
            ("ui-playground-cancel-smoke", "_ui-playground-cancel-smoke"),
            ("smoke", "_smoke"),
            ("container-core-smoke", "_container-core-smoke"),
            ("container-openapi-check", "_container-openapi-check"),
            ("container-live-test", "_container-live-test"),
            ("container-speech-summary-test", "_container-speech-summary-test"),
        ),
        "langfuse": (("langfuse-smoke", "_langfuse-smoke"),),
        "agent-test": (("container-workspace-pytest-test", "_container-workspace-pytest-test"),),
        "isolated-health": (("container-health-e2e", "_container-health-e2e"),),
    }

    assert set(acceptance_contract.VERIFIER_REGISTRY) == set(PROFILES) == set(expected)
    for profile, identities in expected.items():
        declared = (*PROFILES[profile].build_services, *PROFILES[profile].external_image_services)
        assert acceptance_contract.PROFILE_IMAGE_SERVICES[profile] == declared
        assert tuple((verifier.identity, verifier.invocation_argv) for verifier in acceptance_contract.VERIFIER_REGISTRY[profile]) == tuple(
            (
                identity,
                ("make", "--no-print-directory", target),
            )
            for identity, target in identities
        )
        assert acceptance_contract.verifier_evidence.verifier_v1_identities(profile) == tuple(identity for identity, _target in identities)
    assert acceptance_contract.PROFILE_IMAGE_KINDS == acceptance_contract.RECEIPT_V2_PROFILE_IMAGE_KINDS


def test_verifier_receipt_is_portable_and_execution_argv_is_snapshot_derived(tmp_path: Path) -> None:
    verifier = acceptance_contract.VERIFIER_REGISTRY["core"][0]
    receipt = acceptance_contract.verifier_receipt_payload("core", verifier)
    snapshot = tmp_path / "candidate/repository"

    assert acceptance_contract.MAKEFILE_PATH not in json.dumps(receipt, sort_keys=True)
    assert receipt["execution_template"] == {
        "executable": "/usr/bin/make",
        "arguments": ["--no-print-directory", "-f", "Makefile", "_ui-smoke"],
    }
    assert acceptance_contract.parse_verifier_receipt_payload("core", receipt) == verifier
    assert acceptance_contract.verifier_execution_argv("core", verifier, snapshot) == (
        "/usr/bin/make",
        "--no-print-directory",
        "-f",
        str(snapshot / "Makefile"),
        "_ui-smoke",
    )


def test_managed_environment_rejects_caller_redirects_and_binds_preserved_transport(tmp_path: Path) -> None:
    hostile_bin = tmp_path / "hostile-bin"
    hostile_bin.mkdir()
    execution_marker = tmp_path / "executed"
    for command in ("node", "pnpm"):
        executable = hostile_bin / command
        executable.write_text(f"#!/bin/sh\ntouch '{execution_marker}'\n", encoding="utf-8")
        executable.chmod(0o755)
    poisoned = {
        **os.environ,
        "PATH": f"{hostile_bin}:{os.environ['PATH']}",
        "SAFE": "removed",
        "API_BASE": "http://redirect.invalid",
        "FRONTEND_URL": "http://redirect.invalid",
        "HOST_PORT": "1",
        "API_KEY": "private",
        "MAKEFLAGS": "-n",
        "MAKEFILES": "/untrusted.mk",
        "MAKE": "/untrusted/make",
        "PYTHONPATH": "/untrusted/python",
        "PYTHON_RUN": "true",
        "COMPOSE": "true",
        "GIT_DIR": "/untrusted/git",
        "LD_PRELOAD": "/untrusted/loader.so",
        "BASH_ENV": "/untrusted/shell",
        "NODE_OPTIONS": "--require=/untrusted/module.js",
        "DOCKER_HOST": "unix:///run/user/1000/docker.sock",
        "DOCKER_CONFIG": "/untrusted/docker",
        "DOCKER_CLI_PLUGIN_EXTRA_DIRS": "/untrusted/plugins",
        "DOCKER_CREDENTIAL_HELPERS": "untrusted-helper",
        "BUILDX_CONFIG": "/untrusted/buildx",
    }
    runtime_values = acceptance_contract.candidate_runtime_environment_paths(tmp_path / "runtime").managed_values()
    dependency_values = _snapshot_dependency_values(tmp_path / "snapshot")
    managed = acceptance_contract.build_managed_environment(
        poisoned,
        managed_values={
            **runtime_values,
            **dependency_values,
            "AGENT_GOV_CONTAINER_ACCEPTANCE_ACTIVE": "1",
            "COMPOSE_ENV_FILE": "/selected.env",
            "API_BASE": "http://127.0.0.1:39017",
            "HOST_PORT": "39017",
            "LITELLM_LOCAL_MODEL_COST_MAP": "True",
        },
    )

    actual = acceptance_contract.verifier_environment(managed)

    assert actual["API_BASE"] == "http://127.0.0.1:39017"
    assert actual["HOST_PORT"] == "39017"
    assert actual["DOCKER_HOST"] == "unix:///run/docker.sock"
    assert actual["DOCKER_CONFIG"].endswith("/.agentgov-container-acceptance/docker-config")
    assert actual["DOCKER_CLI_PLUGIN_EXTRA_DIRS"] == "/usr/libexec/docker/cli-plugins"
    assert actual["HOME"] == str(tmp_path / "runtime/home")
    assert actual["XDG_CONFIG_HOME"] == str(tmp_path / "runtime/xdg-config")
    assert actual["BUILDX_CONFIG"] == str(tmp_path / "runtime/buildx")
    assert actual["TMPDIR"] == str(tmp_path / "runtime/tmp")
    assert actual["VERIFY_SCREENSHOT_DIR"] == str(tmp_path / "runtime/screenshots")
    assert actual["AGENT_GOV_ACCEPTANCE_NODE"] == str(tmp_path / "snapshot/dependencies/node/bin/node")
    assert actual["AGENT_GOV_ACCEPTANCE_PNPM"] == str(tmp_path / "snapshot/dependencies/pnpm/bin/pnpm.cjs")
    assert str(hostile_bin) not in actual["PATH"].split(os.pathsep)
    assert actual["PATH"].split(os.pathsep)[0] == str(tmp_path / "snapshot/dependencies/node/bin")
    assert not execution_marker.exists()
    assert all(
        key not in actual
        for key in (
            "SAFE",
            "FRONTEND_URL",
            "API_KEY",
            "MAKEFLAGS",
            "MAKEFILES",
            "MAKE",
            "PYTHONPATH",
            "PYTHON_RUN",
            "COMPOSE",
            "GIT_DIR",
            "LD_PRELOAD",
            "BASH_ENV",
            "NODE_OPTIONS",
            "DOCKER_CREDENTIAL_HELPERS",
        )
    )
    digest = acceptance_contract.validate_managed_environment(actual)
    assert actual[acceptance_contract.MANAGED_ENVIRONMENT_SHA256_ENV] == digest
    actual["DOCKER_HOST"] = "unix:///replaced.sock"
    with pytest.raises(acceptance_contract.AcceptanceContractError, match="digest"):
        acceptance_contract.verifier_environment(actual)

    for redirected in ({"HOME": str(tmp_path)}, {"NVM_BIN": str(hostile_bin)}):
        redirected_env = acceptance_contract.build_managed_environment(
            {**poisoned, **redirected},
            managed_values={
                **runtime_values,
                **dependency_values,
                "AGENT_GOV_CONTAINER_ACCEPTANCE_ACTIVE": "1",
                "LITELLM_LOCAL_MODEL_COST_MAP": "True",
            },
        )
        assert redirected_env["HOME"] == actual["HOME"]
        assert "NVM_BIN" not in redirected_env

    toolchain = acceptance_contract.verifier_receipt_payload("core", acceptance_contract.VERIFIER_REGISTRY["core"][0])["toolchain"]
    assert isinstance(toolchain, dict) and toolchain["pnpm_version"] == "10.30.3"
    assert all(set(tool) == {"command", "authority_sha256"} and len(tool["authority_sha256"]) == 64 for tool in toolchain["tools"])
    encoded = json.dumps(toolchain, sort_keys=True)
    assert "/dependency" not in encoded and str(Path.home()) not in encoded


def test_terminal_environment_keeps_fixed_authority_after_candidate_cleanup(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    runtime_values = acceptance_contract.candidate_runtime_environment_paths(tmp_path / "runtime").managed_values()
    dependency_values = _snapshot_dependency_values(tmp_path / "snapshot")
    managed = acceptance_contract.build_managed_environment(
        {},
        managed_values={
            **runtime_values,
            **dependency_values,
            "AGENT_GOV_CONTAINER_ACCEPTANCE_ACTIVE": "1",
            "LITELLM_LOCAL_MODEL_COST_MAP": "True",
        },
    )
    observed: list[object] = []
    monkeypatch.setattr(
        acceptance_contract.acceptance_toolchain,
        "validate_execution_tool_authority",
        lambda environ=None, **_kwargs: observed.append(environ),
    )
    shutil.rmtree(tmp_path / "snapshot")

    terminal = acceptance_contract.terminal_process_environment(managed)
    assert terminal["DOCKER_HOST"] == managed["DOCKER_HOST"]
    assert terminal["DOCKER_CONFIG"] == managed["DOCKER_CONFIG"]
    assert terminal["PATH"] == "/usr/bin"
    assert all(key in {"DOCKER_HOST", "DOCKER_CONFIG", "PATH", "LANG", "LANGUAGE", "TZ"} or key.startswith("LC_") for key in terminal)
    assert not any("snapshot" in value or "runtime" in value for value in terminal.values())
    assert observed == [None]
    acceptance_contract.validate_managed_environment(managed)
    assert observed[-1] is managed

    tampered = dict(managed)
    tampered["DOCKER_HOST"] = "unix:///replaced.sock"
    with pytest.raises(acceptance_contract.AcceptanceContractError, match="digest"):
        acceptance_contract.validate_terminal_managed_environment(tampered)


def test_every_public_target_maps_once_to_exact_profile_and_private_verifier() -> None:
    makefile = (Path(acceptance_contract.MAKEFILE_PATH)).read_text(encoding="utf-8")
    expected = {
        (verifier.identity, profile, verifier.invocation_argv[-1])
        for profile, verifiers in acceptance_contract.VERIFIER_REGISTRY.items()
        for verifier in verifiers
    }
    assert len(expected) == sum(len(verifiers) for verifiers in acceptance_contract.VERIFIER_REGISTRY.values())
    stanzas = re.findall(r"(?m)^([A-Za-z0-9_.-]+):[^\n]*\n((?:\t[^\n]*\n)*)", makefile)
    route = re.compile(r"\$\(CONTAINER_ACCEPTANCE\) --profile ([a-z0-9-]+) -- make --no-print-directory (_[a-z0-9-]+)")
    actual: list[tuple[str, str, str]] = []
    for public_target, body in stanzas:
        if "$(CONTAINER_ACCEPTANCE) --profile" not in body:
            continue
        matches = route.findall(body)
        assert len(matches) == 1
        actual.append((public_target, *matches[0]))
    assert Counter(actual) == Counter({item: 1 for item in expected})
    guarded = Counter(target for target, body in stanzas if "$(REQUIRE_CONTAINER_ACCEPTANCE)" in body)
    assert guarded == Counter({private: 1 for _public, _profile, private in expected})
    assert "langfuse-smoke: langfuse-prepare" not in makefile
    assert PROFILES["langfuse"].volume_init_service == "langfuse-volume-init"


def test_live_runtime_verifier_cannot_write_the_candidate_snapshot() -> None:
    makefile = Path(acceptance_contract.MAKEFILE_PATH).read_text(encoding="utf-8")
    body = makefile.split("_container-live-test:", 1)[1].split("\ncontainer-workspace-pytest-test:", 1)[0]

    assert '-v "$(CURDIR):/app:ro"' in body
    assert "-e PYTHONDONTWRITEBYTECODE=1" in body
    assert "-p no:cacheprovider" in body
    assert '-v "$(CURDIR):/app"' not in body


def test_all_container_acceptance_modules_are_in_typecheck_authority() -> None:
    makefile_path = Path(acceptance_contract.MAKEFILE_PATH)
    typecheck_block = makefile_path.read_text(encoding="utf-8").split("PYTHON_TYPECHECK_TARGETS :=", 1)[1].split("\n\n", 1)[0]
    modules = tuple(sorted(makefile_path.parent.glob("scripts/container_acceptance_*.py")))

    assert modules
    assert all(str(module.relative_to(makefile_path.parent)) in typecheck_block for module in modules)


def test_command_line_cannot_override_container_acceptance_runner() -> None:
    overrides = (
        "CONTAINER_ACCEPTANCE=printf BYPASS",
        "CONTAINER_ACCEPTANCE_BOOTSTRAP_PYTHON=/tmp/python",
        "CONTAINER_ACCEPTANCE_BOOTSTRAP=/tmp/bootstrap.py",
        "CONTAINER_ACCEPTANCE_BOOTSTRAP_LOADER=raise SystemExit(0)",
        "CONTAINER_ACCEPTANCE_TOOLCHAIN=/tmp/toolchain.py",
        "CONTAINER_ACCEPTANCE_REPO_ROOT=/tmp/repository",
        "SHELL=/tmp/shell",
    )
    result = subprocess.run(
        [
            "/usr/bin/make",
            "-pRrq",
            "-f",
            acceptance_contract.MAKEFILE_PATH,
            *overrides,
            "setup",
        ],
        cwd=Path(acceptance_contract.MAKEFILE_PATH).parent,
        check=False,
        capture_output=True,
        text=True,
        env={**os.environ, "MAKEFLAGS": ""},
    )

    assignments = tuple(line for line in result.stdout.splitlines() if line.startswith(("CONTAINER_ACCEPTANCE =", "CONTAINER_ACCEPTANCE :=")))
    assert len(assignments) == 1
    assert '"/usr/bin/env" -i' in assignments[0]
    assert '"$(CONTAINER_ACCEPTANCE_BOOTSTRAP_PYTHON)"' in assignments[0]
    assert '"$(CONTAINER_ACCEPTANCE_BOOTSTRAP)"' in assignments[0]
    assert "printf BYPASS" not in assignments[0]
    fixed = {
        line.split(" :=", 1)[0]: line
        for line in result.stdout.splitlines()
        if line.startswith(
            (
                "CONTAINER_ACCEPTANCE_BOOTSTRAP :=",
                "CONTAINER_ACCEPTANCE_BOOTSTRAP_LOADER :=",
                "CONTAINER_ACCEPTANCE_BOOTSTRAP_PYTHON :=",
                "CONTAINER_ACCEPTANCE_TOOLCHAIN :=",
                "CONTAINER_ACCEPTANCE_REPO_ROOT :=",
                "SHELL :=",
            )
        )
    }
    assert fixed["CONTAINER_ACCEPTANCE_BOOTSTRAP_PYTHON"].endswith("/usr/bin/python3.10")
    assert fixed["CONTAINER_ACCEPTANCE_BOOTSTRAP"].endswith("/scripts/container_acceptance_bootstrap.py")
    assert "O_NOFOLLOW" in fixed["CONTAINER_ACCEPTANCE_BOOTSTRAP_LOADER"]
    assert "raise SystemExit(0)" not in fixed["CONTAINER_ACCEPTANCE_BOOTSTRAP_LOADER"]
    assert fixed["CONTAINER_ACCEPTANCE_TOOLCHAIN"].endswith("/scripts/container_acceptance_toolchain.py")
    assert fixed["CONTAINER_ACCEPTANCE_REPO_ROOT"].endswith("/agent-gov")
    assert fixed["SHELL"].endswith("/bin/sh")


def test_public_make_rejects_outer_parser_injection_before_launcher_dispatch() -> None:
    makefile = Path(acceptance_contract.MAKEFILE_PATH).read_text(encoding="utf-8")
    public = next(line for line in makefile.splitlines() if line.startswith("_CONTAINER_ACCEPTANCE_PUBLIC_TARGETS := "))
    declared = set(public.split(" := ", 1)[1].split())
    expected = {verifier.identity for verifiers in acceptance_contract.VERIFIER_REGISTRY.values() for verifier in verifiers}
    guard = makefile.split("_CONTAINER_ACCEPTANCE_PUBLIC_TARGETS :=", 1)[1].split("VENV ?=", 1)[0]

    assert declared == expected
    assert "ifneq ($(AGENT_GOV_CONTAINER_ACCEPTANCE_ACTIVE),1)" in guard
    assert "$(MAKEFILES)" in guard and "$(MAKEOVERRIDES)" in guard
    assert "$(words $(MAKEFILE_LIST))" in guard and "$(realpath $(MAKEFILE_LIST))" in guard
    assert "--eval=%" in guard and "--environment-overrides" in guard
    assert all(f"$(findstring {flag},$(_CONTAINER_ACCEPTANCE_SHORT_MAKEFLAGS))" in guard for flag in "einqt")


def test_public_launcher_treats_compose_env_file_as_bootstrap_data() -> None:
    makefile = Path(acceptance_contract.MAKEFILE_PATH).read_text(encoding="utf-8")
    assignment = next(line for line in makefile.splitlines() if line.startswith("override CONTAINER_ACCEPTANCE = "))

    assert 'COMPOSE_ENV_FILE="$${COMPOSE_ENV_FILE}"' in assignment
    assert '"/usr/bin/env" -i' in assignment
    assert "$(COMPOSE_ENV_FILE)" not in assignment
    assert '"$(CONTAINER_ACCEPTANCE_BOOTSTRAP)" launch' in assignment


def test_openapi_container_verifier_reports_only_bounded_child_phases() -> None:
    makefile = Path(acceptance_contract.MAKEFILE_PATH).read_text(encoding="utf-8")
    body = makefile.split("_container-openapi-check:", 1)[1].split("container-health-e2e:", 1)[0]

    assert "failure_phase=openapi_contract failure_code=ChildExit" in body
    assert "failure_phase=openapi_docs_browser failure_code=ChildExit" in body
    assert "if ! $(PYTHON_RUN) scripts/audit_openapi_contract.py" in body
    assert body.count(">/dev/null 2>&1") == 2
    assert "$(CONTAINER_ACCEPTANCE_PNPM) --silent --dir frontend run verify:openapi-docs" in body
    assert 'echo "OPENAPI_CONTAINER_CHECK_OK"' in body
    assert 'if ! RUNTIME_API_BASE="$$api_base" $(CONTAINER_ACCEPTANCE_PNPM) --silent' in body


def test_public_managed_browser_verifiers_expose_only_bounded_results() -> None:
    makefile = Path(acceptance_contract.MAKEFILE_PATH).read_text(encoding="utf-8")
    expected = {
        "_ui-feedback-smoke:": ("ui-openai-responses-smoke:", "ui_feedback_browser", "UI_FEEDBACK_CONTAINER_OK"),
        "_ui-playground-cancel-smoke:": ("_ui-openai-responses-smoke:", "playground_cancel_browser", "PLAYGROUND_CANCEL_CONTAINER_OK"),
        "_ui-openai-responses-smoke:": ("langfuse-prepare:", "openai_responses_browser", "OPENAI_RESPONSES_CONTAINER_OK"),
    }
    for target, (next_target, phase, success) in expected.items():
        body = makefile.split(target, 1)[1].split(next_target, 1)[0]
        assert "$(CONTAINER_ACCEPTANCE_PNPM) --silent --dir frontend run" in body
        assert ">/dev/null 2>&1" in body
        assert f"failure_phase={phase} failure_code=ChildExit" in body
        assert f'echo "{success}"' in body

    health = (Path(__file__).resolve().parents[1] / "scripts/run_healthcheck_container_e2e.sh").read_text(encoding="utf-8")
    assert '"$AGENT_GOV_ACCEPTANCE_PNPM" --silent --dir frontend run' in health
    assert ">/dev/null 2>&1" in health
    assert "failure_phase=provider_health_browser failure_code=ChildExit" in health
    assert "failure_phase=provider_health_diagnose failure_code=ChildExit" in health
    assert "failure_phase=provider_health_diagnose failure_code=ContractMismatch" in health
    assert "_container-health-diagnose 2>/dev/null" in health
    assert "printf '%s\\n' \"$diagnosis\"" not in health
    assert 'echo "PROVIDER_HEALTH_CONTAINER_E2E_OK"' in health
    assert "passed run_id=" not in health


def test_public_make_rejects_an_additional_makefile_during_static_parse(tmp_path: Path) -> None:
    root = Path(acceptance_contract.MAKEFILE_PATH)
    extra = tmp_path / "extra.mk"
    extra.write_text("PROBE := $(CONTAINER_ACCEPTANCE)\n", encoding="utf-8")
    result = subprocess.run(
        ["/usr/bin/make", "-pRrq", "-f", str(root), "-f", str(extra), "setup"],
        cwd=root.parent,
        check=False,
        capture_output=True,
        text=True,
        env={**os.environ, "MAKEFLAGS": ""},
    )

    assert result.returncode != 0
    assert "alternate MAKEFILE_LIST authority" in result.stderr


def test_stdlib_loader_binds_actual_bootstrap_bytes_before_later_capture(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    makefile = Path(acceptance_contract.MAKEFILE_PATH).read_text(encoding="utf-8")
    assignment = next(line for line in makefile.splitlines() if line.startswith("override CONTAINER_ACCEPTANCE_BOOTSTRAP_LOADER := "))
    loader = assignment.split(" := ", 1)[1]
    repository = tmp_path / "repository"
    scripts = repository / "scripts"
    scripts.mkdir(parents=True)
    source = scripts / "container_acceptance_bootstrap.py"
    replacement = scripts / "replacement.py"
    source.write_text(
        "import hashlib,os\n"
        "actual=globals()['__agentgov_loaded_sha256__']\n"
        "actual_python=globals()['__agentgov_system_python_sha256__']\n"
        "python_digest=hashlib.sha256(open('/proc/self/exe','rb').read()).hexdigest()\n"
        "os.replace(os.environ['REPLACEMENT'],__file__)\n"
        "current=hashlib.sha256(open(__file__,'rb').read()).hexdigest()\n"
        "raise SystemExit(29 if actual != current and actual_python == python_digest else 7)\n",
        encoding="utf-8",
    )
    loaded_sha256 = hashlib.sha256(source.read_bytes()).hexdigest()
    replacement.write_text("raise SystemExit(7)\n", encoding="utf-8")

    completed = subprocess.run(
        ("/usr/bin/python3", "-I", "-S", "-c", loader, str(source)),
        check=False,
        env={"LC_ALL": "C.UTF-8", "REPLACEMENT": str(replacement)},
        timeout=5,
    )
    monkeypatch.setattr(acceptance_launcher.acceptance_toolchain, "REPO_ROOT", repository)

    assert completed.returncode == 29
    with pytest.raises(acceptance_launcher.AcceptanceLauncherError, match="actual-loaded authority drifted"):
        acceptance_launcher._source_file(source, loaded_sha256)


def test_launcher_bootstrap_authority_uses_actual_bytes_not_proc_alias_path(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    toolchain = acceptance_launcher.acceptance_toolchain
    python_digest = "1" * 64
    system_digest = "2" * 64
    source_digest = "3" * 64
    authority = SimpleNamespace(
        payload={
            "tools": [
                {**_tool("python", "/venv/python"), "sha256": python_digest},
                {**_tool("bootstrap-python", "/usr/bin/python3.10"), "sha256": system_digest},
            ]
        }
    )
    monkeypatch.setattr(toolchain, "__file__", "/proc/self/fd/29/scripts/container_acceptance_toolchain.py")
    monkeypatch.setattr(toolchain, "active_toolchain_authority", lambda: authority)
    monkeypatch.setattr(toolchain, "actual_loaded_source_sha256", lambda relative: source_digest)

    acceptance_launcher._bootstrap_authority(
        {
            toolchain.BOOTSTRAP_PYTHON_SHA256_ENV: python_digest,
            toolchain.BOOTSTRAP_TOOLCHAIN_SHA256_ENV: source_digest,
            toolchain.BOOTSTRAP_STAGE_ENV: system_digest,
        }
    )


def test_health_verifier_routes_python_through_the_snapshot_make_loader() -> None:
    root = Path(acceptance_contract.MAKEFILE_PATH).parent
    health = (root / "scripts/run_healthcheck_container_e2e.sh").read_text(encoding="utf-8")
    makefile = (root / "Makefile").read_text(encoding="utf-8")

    assert '"$AGENT_GOV_ACCEPTANCE_MAKE" --no-print-directory' in health
    assert "_container-health-diagnose" in health
    assert "/usr/bin/python3" not in health
    diagnosis = makefile.split("_container-health-diagnose:", 1)[1].split("\n\n", 1)[0]
    assert "$(PYTHON_RUN) scripts/diagnose_runtime_health.py" in diagnosis
    assert '-c "$(CONTAINER_ACCEPTANCE_BOOTSTRAP_LOADER)"' in makefile


@pytest.mark.parametrize(
    "command",
    (
        ["true"],
        ["make", "--no-print-directory", "_smoke", "extra"],
        ["/usr/bin/make", "--no-print-directory", "_smoke"],
        list(acceptance_contract.VERIFIER_REGISTRY["agent-test"][0].invocation_argv),
    ),
)
def test_unregistered_verifier_is_rejected_by_the_portable_registry(command: list[str]) -> None:
    with pytest.raises(acceptance_contract.AcceptanceContractError, match="fixed verifier"):
        acceptance_contract.resolve_verifier("core", command)


def test_launcher_signal_exit_code_is_preserved_in_a_real_subprocess() -> None:
    source = (
        "import os,signal,sys\n"
        f"sys.path.insert(0,{str(Path(acceptance_contract.MAKEFILE_PATH).parent)!r})\n"
        "from scripts.container_acceptance_launcher import controlled_launcher_signals\n"
        "with controlled_launcher_signals():\n"
        " os.kill(os.getpid(),signal.SIGTERM)\n"
    )
    completed = subprocess.run(
        (sys.executable, "-I", "-P", "-c", source),
        check=False,
        capture_output=True,
        text=True,
        timeout=5,
    )

    assert completed.returncode == 128 + signal.SIGTERM


@pytest.mark.parametrize("interrupt_during", ("reserved", "prepare"))
def test_launcher_signal_closes_the_exact_reserved_lineage_once(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
    interrupt_during: str,
) -> None:
    profile = SimpleNamespace(name="core")
    verifier = acceptance_contract.VERIFIER_REGISTRY["core"][0]
    reserved = SimpleNamespace(identity=SimpleNamespace(candidate_reservation=object()), reserved_sha256="1" * 64)
    lock = SimpleNamespace(assert_current=lambda: None, descriptor=3)
    closed: list[tuple[object, object, object]] = []

    @contextmanager
    def lifecycle_lock(_environ: object) -> Iterator[object]:
        yield lock

    def reserve(*_args: object) -> object:
        if interrupt_during == "reserved":
            os.kill(os.getpid(), signal.SIGTERM)
        return reserved

    def prepare(*_args: object, **_kwargs: object) -> object:
        os.kill(os.getpid(), signal.SIGTERM)
        raise AssertionError("SIGTERM must interrupt candidate preparation")

    monkeypatch.setattr(acceptance_launcher, "_request", lambda _args: (profile, verifier))
    monkeypatch.setattr(acceptance_launcher, "_bootstrap_authority", lambda _env: None)
    monkeypatch.setattr(acceptance_launcher, "_selected_env", lambda *_args: (tmp_path / "selected.env", False))
    monkeypatch.setattr(acceptance_launcher, "_loaded_sources", lambda _environ: ())
    monkeypatch.setattr(acceptance_launcher, "_recover_stale", lambda _lock: None)
    monkeypatch.setattr(acceptance_launcher, "_reserve_candidate", reserve)
    monkeypatch.setattr(acceptance_launcher.acceptance_lock, "lifecycle_lock", lifecycle_lock)
    monkeypatch.setattr(acceptance_launcher.acceptance_lock, "lifecycle_descriptor_sha256", lambda _descriptor: "9" * 64)
    monkeypatch.setattr(acceptance_launcher.acceptance_candidate, "prepare_candidate_snapshot", prepare)
    monkeypatch.setattr(acceptance_launcher.acceptance_toolchain, "frontend_dependency_projection_requirement", object)
    monkeypatch.setattr(acceptance_launcher.acceptance_toolchain, "python_dependency_snapshot_requirement", object)
    monkeypatch.setattr(acceptance_launcher.acceptance_toolchain, "pnpm_dependency_snapshot_requirement", object)
    monkeypatch.setattr(acceptance_launcher.acceptance_toolchain, "node_executable_snapshot_requirement", object)
    monkeypatch.setattr(
        acceptance_launcher,
        "_close_candidate_failure",
        lambda item, candidate, runtime, _primary: closed.append((item, candidate, runtime)),
    )

    with acceptance_launcher.controlled_launcher_signals(), pytest.raises(acceptance_launcher.AcceptanceLauncherInterrupted) as interrupted:
        acceptance_launcher.launch([], {})

    assert interrupted.value.code == 128 + signal.SIGTERM
    assert closed == [(reserved, None, None)]


def test_launcher_signal_during_exec_handoff_closes_prepared_in_the_same_run(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    profile = SimpleNamespace(name="core")
    verifier = acceptance_contract.VERIFIER_REGISTRY["core"][0]
    reserved = SimpleNamespace(identity=SimpleNamespace(candidate_reservation=object()), reserved_sha256="1" * 64)
    candidate = SimpleNamespace(
        snapshot=SimpleNamespace(root=tmp_path / "snapshot"),
        snapshot_repository_root=tmp_path / "snapshot/repository",
        snapshot_runner_path=tmp_path / "snapshot/repository/scripts/run_container_acceptance.py",
        env_file=tmp_path / "snapshot/selected.env",
        to_json=lambda: "candidate",
    )
    runtime = object()
    prepared = SimpleNamespace(to_json=lambda: "prepared", verify_current=lambda: None)
    lock = SimpleNamespace(assert_current=lambda: None, inheritance_env={}, descriptor=3)
    closed: list[tuple[object, object, object]] = []

    def interrupt_exec(*_args: object) -> None:
        raise acceptance_launcher.AcceptanceLauncherInterrupted(signal.SIGINT)

    @contextmanager
    def lifecycle_lock(_environ: object) -> Iterator[object]:
        yield lock

    monkeypatch.setattr(acceptance_launcher, "_request", lambda _args: (profile, verifier))
    monkeypatch.setattr(acceptance_launcher, "_bootstrap_authority", lambda _env: None)
    monkeypatch.setattr(acceptance_launcher, "_selected_env", lambda *_args: (tmp_path / "selected.env", False))
    monkeypatch.setattr(acceptance_launcher, "_loaded_sources", lambda _environ: ())
    monkeypatch.setattr(acceptance_launcher, "_recover_stale", lambda _lock: None)
    monkeypatch.setattr(acceptance_launcher, "_reserve_candidate", lambda *_args: reserved)
    monkeypatch.setattr(acceptance_launcher.acceptance_lock, "lifecycle_lock", lifecycle_lock)
    monkeypatch.setattr(acceptance_launcher.acceptance_lock, "lifecycle_descriptor_sha256", lambda _descriptor: "9" * 64)
    monkeypatch.setattr(acceptance_launcher.acceptance_candidate, "prepare_candidate_snapshot", lambda *_args, **_kwargs: candidate)
    monkeypatch.setattr(acceptance_launcher.acceptance_environment, "prepare_isolated_runtime", lambda *_args: runtime)
    monkeypatch.setattr(
        acceptance_launcher.acceptance_environment,
        "build_acceptance_env",
        lambda *_args, **_kwargs: {acceptance_contract.MANAGED_ENVIRONMENT_SHA256_ENV: "2" * 64},
    )
    monkeypatch.setattr(acceptance_launcher.acceptance_receipt, "transition_reserved_to_prepared", lambda *_args, **_kwargs: prepared)
    monkeypatch.setattr(acceptance_launcher.acceptance_candidate, "verify_candidate_snapshot", lambda *_args, **_kwargs: None)
    monkeypatch.setattr(acceptance_launcher.acceptance_toolchain, "validate_toolchain_authority", lambda *_args: None)
    monkeypatch.setattr(acceptance_launcher.acceptance_contract, "build_prepared_reexec_environment", lambda *_args, **_kwargs: {})
    for name in (
        "frontend_dependency_projection_requirement",
        "python_dependency_snapshot_requirement",
        "pnpm_dependency_snapshot_requirement",
        "node_executable_snapshot_requirement",
    ):
        monkeypatch.setattr(acceptance_launcher.acceptance_toolchain, name, object)
    monkeypatch.setattr(
        acceptance_launcher.acceptance_toolchain,
        "exec_authoritative_python",
        interrupt_exec,
    )
    monkeypatch.setattr(
        acceptance_launcher,
        "_close_prepared_failure",
        lambda item, frozen, live, _primary: closed.append((item, frozen, live)),
    )

    with pytest.raises(acceptance_launcher.AcceptanceLauncherInterrupted) as interrupted:
        with acceptance_launcher.controlled_launcher_signals():
            acceptance_launcher.launch([], {})

    assert interrupted.value.code == 128 + signal.SIGINT
    assert closed == [(prepared, candidate, runtime)]
