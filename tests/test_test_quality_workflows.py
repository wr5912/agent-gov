from pathlib import Path

import yaml

REPO_ROOT = Path(__file__).resolve().parents[1]


def _workflow(name: str) -> tuple[str, dict[str, object]]:
    text = (REPO_ROOT / ".github/workflows" / name).read_text(encoding="utf-8")
    return text, yaml.safe_load(text)


def test_governance_workflow_has_parallel_blocking_lanes_and_same_run_gate() -> None:
    text, workflow = _workflow("governance.yml")
    jobs = workflow["jobs"]

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


def test_container_and_release_workflows_use_ephemeral_or_protected_boundaries() -> None:
    container_text, _ = _workflow("container-health-e2e.yml")
    release_text, release = _workflow("release-live.yml")

    assert 'cp docker/.env.example "$compose_env"' in container_text
    assert "make container-health-e2e" in container_text
    assert "playwright install --with-deps chromium" in container_text
    assert release["jobs"]["live-acceptance"]["environment"] == "release-live"
    assert "HOST_RUNTIME_VOLUME_ROOT: ${{ runner.temp }}/agent-gov-live-runtime" in release_text
    assert "make container-live-test" in release_text


def test_mutation_workflow_is_scheduled_and_bounded_by_policy_runner() -> None:
    text, workflow = _workflow("test-mutation.yml")
    trigger = workflow.get("on", workflow.get(True))

    assert "schedule" in trigger
    assert "make mutation-test" in text
    assert "run_mutation_lane.py" not in text
