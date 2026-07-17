import json
import os
import subprocess
from pathlib import Path

import yaml

REPO_ROOT = Path(__file__).resolve().parents[1]


def _workflow(name: str) -> tuple[str, dict[str, object]]:
    text = (REPO_ROOT / ".github/workflows" / name).read_text(encoding="utf-8")
    return text, yaml.safe_load(text)


def test_governance_workflow_has_parallel_blocking_lanes_and_same_run_gate() -> None:
    text, workflow = _workflow("governance.yml")
    jobs = workflow["jobs"]
    trigger = workflow.get("on", workflow.get(True))

    assert {
        "pull-request-metadata",
        "governance-static",
        "backend-main-full",
        "frontend-ui",
        "quality-gate",
    } <= set(jobs)
    assert set(jobs["quality-gate"]["needs"]) == {
        "pull-request-metadata",
        "governance-static",
        "backend-main-full",
        "frontend-ui",
    }
    assert 'python3 scripts/check_pr_aid.py --event-file "$GITHUB_EVENT_PATH"' in text
    assert 'test "$METADATA_RESULT" = success' in text
    assert "playwright install --with-deps chromium" in text
    assert "verify:playwright-runtime" in text
    assert "make main-flow-ui-test" in text
    assert "make main-flow-test" not in text
    assert "--expected-job backend-main-full" in text
    assert "--skip-collection" not in text
    assert "backend-main-full-evidence-${{ github.run_attempt }}" in text
    assert "multica issue" not in text.lower()
    assert "agent_gov_ci_status_relay" not in text
    assert trigger["push"]["branches"] == ["master"]
    for path in (REPO_ROOT / ".github/workflows").glob("*.yml"):
        workflow_text = path.read_text(encoding="utf-8")
        if "astral-sh/setup-uv@" in workflow_text:
            assert "astral-sh/setup-uv@v8.3.2" in workflow_text
            assert "astral-sh/setup-uv@v8\n" not in workflow_text


def test_shadow_workflow_keeps_serial_sentinel_and_all_declared_xdist_configs() -> None:
    text, workflow = _workflow("test-portfolio-shadow.yml")
    jobs = workflow["jobs"]
    matrix = jobs["parallel-shadow"]["strategy"]["matrix"]

    assert set(matrix["workers"]) == {2, 4}
    assert set(matrix["scheduler"]) == {"load", "worksteal"}
    assert "--lane main-full" in text
    assert "--lane pr-fast" in text
    assert "--skip-coverage-threshold" in text
    assert "compare_test_shadow_evidence.py" in text


def test_container_workflows_use_ephemeral_or_manual_acceptance_boundaries() -> None:
    container_text, _ = _workflow("container-health-e2e.yml")
    live_text, live = _workflow("container-live-acceptance.yml")

    assert 'cp docker/.env.example "$compose_env"' in container_text
    assert "AGENT_GOV_COMPOSE_ENV_FILE=%s" in container_text
    assert "make container-health-e2e" in container_text
    assert "playwright install --with-deps chromium" in container_text
    assert live["jobs"]["container-live-acceptance"]["environment"] == "container-live-acceptance"
    assert "AGENT_GOV_COMPOSE_ENV_FILE=%s" in live_text
    assert "HOST_RUNTIME_VOLUME_ROOT: ${{ runner.temp }}/agent-gov-live-runtime" in live_text
    assert "make container-live-test" in live_text
    assert "CONTAINER_LIVE_API_KEY" in live_text
    assert 'COMPOSE_ENV_PATH="$compose_env" python' in live_text
    assert '"API_KEY": os.environ["API_KEY"]' in live_text
    assert '"FRONTEND_RUNTIME_API_KEY": os.environ["FRONTEND_RUNTIME_API_KEY"]' in live_text
    assert "secrets must be single-line values" in live_text
    assert "release-live" not in live_text


def test_container_live_workflow_materializes_protected_values_in_selected_env(
    tmp_path: Path,
) -> None:
    _, workflow = _workflow("container-live-acceptance.yml")
    steps = workflow["jobs"]["container-live-acceptance"]["steps"]
    script = next(step["run"] for step in steps if step.get("name") == "Create ephemeral runtime and Compose env")
    github_env = tmp_path / "github.env"
    runtime_root = tmp_path / "runtime"
    runtime_key = r"ci-$UNSET-'quote'\backslash#fragment"
    provider_key = r"provider-$UNSET-'quote'\backslash#fragment"
    provider_url = r"https://provider.test/v1/$UNSET/'quote'\path#fragment"
    env = {
        **os.environ,
        "RUNNER_TEMP": str(tmp_path),
        "GITHUB_ENV": str(github_env),
        "HOST_RUNTIME_VOLUME_ROOT": str(runtime_root),
        "API_KEY": runtime_key,
        "FRONTEND_RUNTIME_API_KEY": runtime_key,
        "MODEL_PROVIDER_API_KEY": provider_key,
        "MODEL_PROVIDER_API_URL": provider_url,
    }

    subprocess.run(
        ["bash", "-euo", "pipefail", "-c", script],
        cwd=REPO_ROOT,
        env=env,
        check=True,
        capture_output=True,
        text=True,
    )

    compose_env = tmp_path / "agent-gov-live.env"
    values = {line.split("=", 1)[0]: line.split("=", 1)[1] for line in compose_env.read_text(encoding="utf-8").splitlines() if "=" in line}
    assert values["API_KEY"].startswith("'ci-$UNSET-")
    assert values["FRONTEND_RUNTIME_API_KEY"].startswith("'ci-$UNSET-")
    compose_process_env = {
        key: value
        for key, value in os.environ.items()
        if key
        not in {
            "API_KEY",
            "FRONTEND_RUNTIME_API_KEY",
            "MODEL_PROVIDER_API_KEY",
            "MODEL_PROVIDER_API_URL",
            "UNSET",
        }
    }
    compose_process_env.update(
        {
            "AGENT_GOV_COMPOSE_ENV_FILE": str(compose_env),
            "HOST_RUNTIME_VOLUME_ROOT": str(runtime_root),
        }
    )
    rendered = json.loads(
        subprocess.run(
            [
                "docker",
                "compose",
                "--env-file",
                str(compose_env),
                "-f",
                "docker/docker-compose.yml",
                "config",
                "--format",
                "json",
            ],
            cwd=REPO_ROOT,
            env=compose_process_env,
            check=True,
            capture_output=True,
            text=True,
        ).stdout
    )
    services = rendered["services"]

    def compose_literal(value: str) -> str:
        return value.replace("$", "$$")

    assert services["claude-agent-api"]["environment"]["API_KEY"] == compose_literal(runtime_key)
    assert services["claude-agent-ui"]["environment"]["VITE_RUNTIME_API_KEY"] == compose_literal(runtime_key)
    assert services["agent-gov-litellm-sidecar"]["environment"]["MODEL_PROVIDER_API_KEY"] == compose_literal(provider_key)
    assert services["agent-gov-litellm-sidecar"]["environment"]["MODEL_PROVIDER_API_URL"] == compose_literal(provider_url)
    assert runtime_root.is_dir()
    assert f"COMPOSE_ENV_FILE={compose_env}" in github_env.read_text(encoding="utf-8")


def test_mutation_workflow_is_scheduled_and_bounded_by_policy_runner() -> None:
    text, workflow = _workflow("test-mutation.yml")
    trigger = workflow.get("on", workflow.get(True))

    assert "schedule" in trigger
    assert "make mutation-test" in text
    assert "run_mutation_lane.py" not in text
