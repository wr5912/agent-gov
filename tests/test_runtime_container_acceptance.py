from __future__ import annotations

import importlib.util
import json
import os
import shutil
import subprocess
import sys
import threading
from datetime import datetime, timezone
from pathlib import Path
from types import ModuleType

import pytest
import yaml

REPO_ROOT = Path(__file__).resolve().parents[1]
RUNNER_PATH = REPO_ROOT / "scripts/run_container_acceptance.py"
HOOK_PATH = REPO_ROOT / ".codex/hooks/container_acceptance_guard.py"
MAIN_FLOW_PATH = REPO_ROOT / "scripts/run_main_flow_tests.py"
SPEECH_VERIFIER_PATH = REPO_ROOT / "scripts/verify_speech_summary_container.py"


def _load_module(name: str, path: Path) -> ModuleType:
    spec = importlib.util.spec_from_file_location(name, path)
    assert spec and spec.loader
    module = importlib.util.module_from_spec(spec)
    sys.modules[name] = module
    spec.loader.exec_module(module)
    return module


RUNNER = _load_module("agentgov_container_acceptance", RUNNER_PATH)
HOOK = _load_module("agentgov_container_acceptance_hook", HOOK_PATH)
sys.path.insert(0, str(REPO_ROOT / "scripts"))
MAIN_FLOW = _load_module("agentgov_run_main_flow_tests", MAIN_FLOW_PATH)
SPEECH_VERIFIER = _load_module("agentgov_speech_summary_container", SPEECH_VERIFIER_PATH)


def test_acceptance_profiles_are_exact_and_compose_parallelism_is_bounded(tmp_path: Path) -> None:
    core = RUNNER.PROFILES["core"]
    langfuse = RUNNER.PROFILES["langfuse"]
    health = RUNNER.PROFILES["isolated-health"]
    command = RUNNER.compose_command(langfuse, tmp_path / "selected.env")

    assert core.expected_services == RUNNER.CORE_SERVICES
    assert core.build_services == RUNNER.CORE_SERVICES
    assert set(langfuse.expected_services) == set((*RUNNER.CORE_SERVICES, *RUNNER.LANGFUSE_SERVICES))
    assert langfuse.build_services == RUNNER.CORE_SERVICES
    assert health.delegated_refresh is True
    assert command[:4] == ["docker", "compose", "--parallel", "3"]
    assert command[-2:] == ["--profile", "langfuse"]


def test_refresh_builds_then_force_recreates_before_verifying(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    commands: list[list[str]] = []
    verified: list[tuple[str, bool]] = []
    profile = RUNNER.PROFILES["core"]
    env = {RUNNER.RUN_ID_ENV: "run-current"}

    monkeypatch.setattr(RUNNER, "_validate_service_model", lambda *_args: None)

    def fake_run(command: list[str], **_kwargs: object) -> str:
        commands.append(command)
        return ""

    def fake_verify(
        _base: list[str],
        service: str,
        _run_id: str,
        _started_at: datetime,
        _env: dict[str, str],
        *,
        check_image: bool,
    ) -> None:
        verified.append((service, check_image))

    monkeypatch.setattr(RUNNER, "_run_checked", fake_run)
    monkeypatch.setattr(RUNNER, "_verify_container", fake_verify)

    RUNNER.refresh_profile(profile, tmp_path / "selected.env", env)

    assert commands[0][-4:] == ["build", *RUNNER.CORE_SERVICES]
    up = commands[1]
    assert up[up.index("up") :] == [
        "up",
        "-d",
        "--force-recreate",
        "--wait",
        "--wait-timeout",
        "180",
        "--remove-orphans",
        *RUNNER.CORE_SERVICES,
    ]
    assert verified == [(service, True) for service in RUNNER.CORE_SERVICES]


def test_acceptance_injects_current_run_and_refreshes_before_child(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    env_file = tmp_path / "selected.env"
    env_file.write_text("API_KEY=private\n", encoding="utf-8")
    events: list[str] = []
    observed_env: dict[str, str] = {}
    monkeypatch.setattr(RUNNER, "LOCK_FILE", tmp_path / "acceptance.lock")
    monkeypatch.setattr(RUNNER, "source_fingerprint", lambda _path: "stable")
    monkeypatch.setattr(RUNNER.secrets, "token_hex", lambda _size: "fixed")
    monkeypatch.setattr(RUNNER, "refresh_profile", lambda *_args: events.append("refresh"))

    def fake_child(command: list[str], env: dict[str, str]) -> int:
        events.append("child")
        observed_env.update(env)
        assert command == ["verify-command"]
        return 0

    monkeypatch.setattr(RUNNER, "_run_child", fake_child)

    result = RUNNER.run_acceptance(
        RUNNER.PROFILES["core"],
        env_file,
        ["verify-command"],
        {"APP_VERSION": "stale", "UNRELATED": "kept"},
    )

    assert result == 0
    assert events == ["refresh", "child"]
    assert observed_env[RUNNER.ACTIVE_ENV] == "1"
    assert observed_env[RUNNER.PROFILE_ENV] == "core"
    assert observed_env[RUNNER.RUN_ID_ENV].endswith("-fixed")
    assert observed_env["COMPOSE_ENV_FILE"] == str(env_file)
    assert observed_env["AGENT_GOV_COMPOSE_ENV_FILE"] == str(env_file)
    assert observed_env["APP_VERSION"] == (REPO_ROOT / "VERSION").read_text(encoding="utf-8").strip()
    assert observed_env["UNRELATED"] == "kept"


def test_refresh_failure_prevents_acceptance_command(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    env_file = tmp_path / "selected.env"
    env_file.write_text("SAFE=1\n", encoding="utf-8")
    child_called = False
    monkeypatch.setattr(RUNNER, "LOCK_FILE", tmp_path / "acceptance.lock")
    monkeypatch.setattr(RUNNER, "source_fingerprint", lambda _path: "stable")

    def fail_refresh(*_args: object) -> None:
        raise RUNNER.AcceptanceError("refresh failed")

    def child(*_args: object) -> int:
        nonlocal child_called
        child_called = True
        return 0

    monkeypatch.setattr(RUNNER, "refresh_profile", fail_refresh)
    monkeypatch.setattr(RUNNER, "_run_child", child)

    with pytest.raises(RUNNER.AcceptanceError, match="refresh failed"):
        RUNNER.run_acceptance(RUNNER.PROFILES["core"], env_file, ["verify"], {})

    assert child_called is False


def test_worktree_change_after_refresh_invalidates_result(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    env_file = tmp_path / "selected.env"
    env_file.write_text("SAFE=1\n", encoding="utf-8")
    fingerprints = iter(("before", "after"))
    child_called = False
    monkeypatch.setattr(RUNNER, "LOCK_FILE", tmp_path / "acceptance.lock")
    monkeypatch.setattr(RUNNER, "source_fingerprint", lambda _path: next(fingerprints))
    monkeypatch.setattr(RUNNER, "refresh_profile", lambda *_args: None)

    def child(*_args: object) -> int:
        nonlocal child_called
        child_called = True
        return 0

    monkeypatch.setattr(RUNNER, "_run_child", child)

    with pytest.raises(RUNNER.AcceptanceError, match="工作树/env 已变化"):
        RUNNER.run_acceptance(RUNNER.PROFILES["core"], env_file, ["verify"], {})

    assert child_called is False


def test_container_and_image_must_belong_to_current_acceptance(monkeypatch: pytest.MonkeyPatch) -> None:
    started_at = datetime.now(timezone.utc)

    def fake_run(command: list[str], **_kwargs: object) -> str:
        if command[0] == "compose":
            return "container-id"
        template, target = command[3], command[4]
        if ".State.Running" in template:
            return "true"
        if ".Created" in template:
            return started_at.isoformat()
        if ".Image" in template:
            return "image-id"
        if target == "container-id":
            return "stale-run"
        return "current-run"

    monkeypatch.setattr(RUNNER, "_run_checked", fake_run)

    with pytest.raises(RUNNER.AcceptanceError, match="未加载本轮容器配置"):
        RUNNER._verify_container(
            ["compose"],
            "claude-agent-api",
            "current-run",
            started_at,
            {},
            check_image=True,
        )


def test_acceptance_lock_serializes_refresh_and_child(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    env_file = tmp_path / "selected.env"
    env_file.write_text("SAFE=1\n", encoding="utf-8")
    first_in_child = threading.Event()
    release_first = threading.Event()
    second_refreshed = threading.Event()
    results: list[int] = []
    monkeypatch.setattr(RUNNER, "LOCK_FILE", tmp_path / "acceptance.lock")
    monkeypatch.setattr(RUNNER, "source_fingerprint", lambda _path: "stable")

    def refresh(_profile: object, _path: Path, env: dict[str, str]) -> None:
        if env["WORKER"] == "second":
            second_refreshed.set()

    def child(_command: list[str], env: dict[str, str]) -> int:
        if env["WORKER"] == "first":
            first_in_child.set()
            assert release_first.wait(timeout=2)
        return 0

    monkeypatch.setattr(RUNNER, "refresh_profile", refresh)
    monkeypatch.setattr(RUNNER, "_run_child", child)

    def run(worker: str) -> None:
        results.append(
            RUNNER.run_acceptance(
                RUNNER.PROFILES["core"],
                env_file,
                ["verify"],
                {"WORKER": worker},
            )
        )

    first = threading.Thread(target=run, args=("first",))
    second = threading.Thread(target=run, args=("second",))
    first.start()
    assert first_in_child.wait(timeout=2)
    second.start()
    assert second_refreshed.wait(timeout=0.2) is False
    release_first.set()
    first.join(timeout=2)
    second.join(timeout=2)

    assert not first.is_alive()
    assert not second.is_alive()
    assert sorted(results) == [0, 0]
    assert second_refreshed.is_set()


def test_runner_failure_does_not_echo_captured_secrets(monkeypatch: pytest.MonkeyPatch) -> None:
    sentinel = "must-not-leak-private-value"

    class Result:
        returncode = 1
        stdout = sentinel
        stderr = sentinel

    monkeypatch.setattr(RUNNER.subprocess, "run", lambda *_args, **_kwargs: Result())

    with pytest.raises(RUNNER.AcceptanceError) as error:
        RUNNER._run_checked(["failing-command"], env={}, label="刷新", capture=True)

    assert sentinel not in str(error.value)


@pytest.mark.parametrize(
    "command",
    (
        "make _smoke",
        "cd /tmp && make _container-openapi-check",
        "pnpm --dir frontend run verify:real-container:impl",
        "bash scripts/run_healthcheck_container_e2e.sh",
        "./scripts/run_healthcheck_container_e2e.sh",
        "node scripts/verify_openai_responses_container.mjs",
        "python scripts/verify_speech_summary_container.py",
        "python -m pytest tests/test_live_runtime_acceptance.py",
    ),
)
def test_pretool_guard_blocks_container_acceptance_bypasses(command: str) -> None:
    assert HOOK.bypass_reason(command)


@pytest.mark.parametrize(
    "command",
    (
        "make container-core-smoke",
        "make container-speech-summary-test",
        "make test",
        "pytest tests/test_runtime.py",
        "pnpm --dir frontend run verify:asset-registry",
        "rg --files",
    ),
)
def test_pretool_guard_keeps_host_and_public_commands_available(command: str) -> None:
    assert HOOK.bypass_reason(command) is None


def test_codex_and_claude_pretool_hooks_use_the_shared_guard() -> None:
    codex = json.loads((REPO_ROOT / ".codex/hooks.json").read_text(encoding="utf-8"))
    claude = json.loads((REPO_ROOT / ".claude/settings.json").read_text(encoding="utf-8"))

    for payload in (codex, claude):
        pre_tool_use = payload["hooks"]["PreToolUse"]
        assert any(entry.get("matcher") == "Bash" and "container_acceptance_guard.py" in json.dumps(entry) for entry in pre_tool_use)


def test_make_routes_real_acceptance_through_refresh_and_keeps_main_full_serial() -> None:
    makefile = (REPO_ROOT / "Makefile").read_text(encoding="utf-8")

    for target, profile in (
        ("ui-smoke", "core"),
        ("ui-feedback-smoke", "core"),
        ("ui-openai-responses-smoke", "core"),
        ("smoke", "core"),
        ("container-core-smoke", "core"),
        ("container-openapi-check", "core"),
        ("container-live-test", "core"),
        ("container-speech-summary-test", "core"),
        ("container-health-e2e", "isolated-health"),
        ("langfuse-smoke", "langfuse"),
    ):
        body = makefile.split(f"{target}:", 1)[1].split("\n\n", 1)[0]
        assert f"$(CONTAINER_ACCEPTANCE) --profile {profile}" in body
    assert "langfuse-smoke: langfuse-dirs" in makefile
    assert "--keep-going --jobs=3 _smoke _ui-smoke _container-openapi-check" in makefile
    live_body = makefile.split("_container-live-test:", 1)[1].split("\n\n", 1)[0]
    assert "-e AGENT_GOV_CONTAINER_ACCEPTANCE_ACTIVE" in live_body
    assert "-e AGENT_GOV_ACCEPTANCE_RUN_ID" in live_body
    speech_body = makefile.split("_container-speech-summary-test:", 1)[1].split("\n\n", 1)[0]
    assert "ENABLE_AGENT_RUNTIME_RAW_EVENTS" in speech_body
    assert "scripts/verify_speech_summary_container.py" in speech_body
    assert "test: codex-guard test-backend" not in makefile
    test_body = makefile.split("test:\n", 1)[1].split("\n\n", 1)[0]
    assert test_body.index("codex-guard") < test_body.index("test-backend")


def test_real_ui_acceptance_prefers_compose_port_environment_over_env_file() -> None:
    makefile = (REPO_ROOT / "Makefile").read_text(encoding="utf-8")

    for target in ("_ui-feedback-smoke", "_ui-openai-responses-smoke"):
        body = makefile.split(f"{target}:", 1)[1].split("\n\n", 1)[0]
        assert "frontend_port=$${FRONTEND_HOST_PORT:-$$(awk" in body
        assert "host_port=$${HOST_PORT:-$$(awk" in body


def test_compose_and_local_images_carry_acceptance_run_labels() -> None:
    compose = yaml.safe_load((REPO_ROOT / "docker/docker-compose.yml").read_text(encoding="utf-8"))
    services = compose["services"]

    for service in (*RUNNER.CORE_SERVICES, *RUNNER.LANGFUSE_SERVICES):
        assert services[service]["labels"]["io.agentgov.acceptance-run-id"] == "${AGENT_GOV_ACCEPTANCE_RUN_ID:-unmanaged}"
    for service in RUNNER.CORE_SERVICES:
        assert services[service]["build"]["args"]["AGENT_GOV_ACCEPTANCE_RUN_ID"] == "${AGENT_GOV_ACCEPTANCE_RUN_ID:-unmanaged}"
    for dockerfile in (
        "docker/Dockerfile",
        "docker/frontend.Dockerfile",
        "docker/litellm-sidecar.Dockerfile",
        "docker/e2e/slow-vllm.Dockerfile",
    ):
        text = (REPO_ROOT / dockerfile).read_text(encoding="utf-8")
        assert "ARG AGENT_GOV_ACCEPTANCE_RUN_ID=unmanaged" in text
        assert 'LABEL io.agentgov.acceptance-run-id="${AGENT_GOV_ACCEPTANCE_RUN_ID}"' in text


def test_frontend_real_container_scripts_expose_only_guarded_impls() -> None:
    package = json.loads((REPO_ROOT / "frontend/package.json").read_text(encoding="utf-8"))
    scripts = package["scripts"]

    assert scripts["verify:real-container"] == "cd .. && make ui-feedback-smoke"
    assert scripts["verify:openai-responses-container"] == "cd .. && make ui-openai-responses-smoke"
    assert scripts["verify:provider-health-container"] == "cd .. && make container-health-e2e"
    for script_name in (
        "scripts/verify_improvement_ui_real_container.mjs",
        "scripts/verify_openai_responses_container.mjs",
        "scripts/verify_provider_health_container.mjs",
        "scripts/verify_asset_registry.mjs",
        "scripts/verify_improvement_decision_ui.mjs",
        "scripts/verify_message_actions_browser.mjs",
    ):
        assert "requireContainerAcceptance" in (REPO_ROOT / script_name).read_text(encoding="utf-8")


def test_provider_health_browser_acceptance_uses_sdk_native_playground_route() -> None:
    script = (REPO_ROOT / "scripts/verify_provider_health_container.mjs").read_text(encoding="utf-8")

    assert 'pathname === "/api/agent-runtime/sdk-events"' in script
    assert 'pathname === "/v1/responses"' not in script


def test_javascript_guard_allows_mock_mode_and_rejects_unguarded_real_mode() -> None:
    node = shutil.which("node")
    if node is None:
        pytest.skip("node is unavailable")
    module_uri = (REPO_ROOT / "scripts/container_acceptance_guard.mjs").as_uri()
    script = (
        f'import {{ requireContainerAcceptance }} from "{module_uri}";'
        "requireContainerAcceptance(false);"
        "try { requireContainerAcceptance(true); process.exit(9); } "
        "catch (error) { if (!String(error).includes('public Make target')) process.exit(8); }"
    )
    env = os.environ.copy()
    env.pop("AGENT_GOV_CONTAINER_ACCEPTANCE_ACTIVE", None)
    env.pop("AGENT_GOV_ACCEPTANCE_RUN_ID", None)

    result = subprocess.run(
        [node, "--input-type=module", "--eval", script],
        cwd=REPO_ROOT,
        env=env,
        check=False,
        capture_output=True,
        text=True,
    )

    assert result.returncode == 0, result.stdout + result.stderr


def test_host_main_flow_scrubs_real_container_routing(monkeypatch: pytest.MonkeyPatch) -> None:
    for key in MAIN_FLOW.CONTAINER_ACCEPTANCE_ENV_KEYS:
        monkeypatch.setenv(key, "must-be-removed")
    monkeypatch.setenv("HOST_TEST_SENTINEL", "kept")

    env = MAIN_FLOW._host_test_env()

    assert all(key not in env for key in MAIN_FLOW.CONTAINER_ACCEPTANCE_ENV_KEYS)
    assert env["HOST_TEST_SENTINEL"] == "kept"
    assert env["PYTHONDONTWRITEBYTECODE"] == "1"


def test_live_pytest_requires_the_runner_freshness_marker() -> None:
    live_test = (REPO_ROOT / "tests/test_live_runtime_acceptance.py").read_text(encoding="utf-8")

    assert "AGENT_GOV_CONTAINER_ACCEPTANCE_ACTIVE" in live_test
    assert "AGENT_GOV_ACCEPTANCE_RUN_ID" in live_test
    assert "make container-live-test 完成镜像重建和服务 recreate" in live_test


def test_speech_summary_verifier_requires_the_public_container_target() -> None:
    with pytest.raises(SPEECH_VERIFIER.AcceptanceError, match="container-speech-summary-test"):
        SPEECH_VERIFIER._require_container_acceptance({})

    SPEECH_VERIFIER._require_container_acceptance(
        {
            "AGENT_GOV_CONTAINER_ACCEPTANCE_ACTIVE": "1",
            "AGENT_GOV_ACCEPTANCE_RUN_ID": "run-current",
            "AGENT_GOV_CONTAINER_ACCEPTANCE_PROFILE": "core",
        }
    )
