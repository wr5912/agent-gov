from __future__ import annotations

import copy
import dataclasses
import os
import subprocess
import sys
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parents[1]
DEPLOY_SCRIPT = REPO_ROOT / "scripts" / "deploy_agent_gov_to_host"
REMOTE_HELPER = REPO_ROOT / "scripts" / "agent_gov_release_remote"
CI_EVIDENCE_VERIFIER = REPO_ROOT / "scripts" / "verify_agent_gov_ci_evidence.py"
sys.path.insert(0, str(REPO_ROOT / "scripts"))

from verify_agent_gov_ci_evidence import (  # noqa: E402
    EvidenceConfig,
    EvidenceError,
    verify_ci_evidence,
)


def text_of(path: Path) -> str:
    return path.read_text(encoding="utf-8")


def test_release_shell_entrypoints_are_executable_and_have_valid_syntax() -> None:
    for script in (DEPLOY_SCRIPT, REMOTE_HELPER):
        assert os.access(script, os.X_OK), f"not executable: {script}"
        result = subprocess.run(
            ["bash", "-n", str(script)],
            cwd=REPO_ROOT,
            check=False,
            capture_output=True,
            text=True,
        )
        assert result.returncode == 0, f"{script}: {result.stderr}"


def test_parallel_restart_entrypoint_is_retired_in_favour_of_idempotent_deploy() -> None:
    text = text_of(DEPLOY_SCRIPT)
    remote = text_of(REMOTE_HELPER)

    assert not (REPO_ROOT / "scripts/restart_agent_gov_on_host").exists()
    assert "Reusing committed immutable release" in text
    assert "already current; reconciling Compose state" in remote


def test_deploy_reads_the_controller_source_clone_and_exact_master_sha() -> None:
    text = text_of(DEPLOY_SCRIPT)

    assert 'ROOT_DIR=$(cd "$(dirname "${BASH_SOURCE[0]}")/.."' in text
    assert "AGENT_GOV_SOURCE_REPO_DIR" in text
    assert 'SOURCE_REPO_DIR="${AGENT_GOV_SOURCE_REPO_DIR:-$ROOT_DIR}"' in text
    assert '[[ "$REF_SHA" =~ ^[0-9a-f]{40}$ ]]' in text
    assert 'git -C "$SOURCE_REPO_DIR" fetch origin master --prune' in text
    assert 'git -C "$SOURCE_REPO_DIR" merge-base --is-ancestor' in text
    assert 'git -C "$SOURCE_REPO_DIR" archive "$REF_SHA"' in text
    assert "git archive origin/master" not in text
    assert '"$TMP_DIR/$COMPOSE_FILE"' in text


def test_deploy_pins_ssh_and_installs_a_stable_remote_helper() -> None:
    text = text_of(DEPLOY_SCRIPT)

    assert "StrictHostKeyChecking=yes" in text
    assert "StrictHostKeyChecking=accept-new" not in text
    assert "UserKnownHostsFile=${SSH_KNOWN_HOSTS_FILE}" in text
    assert "AGENT_GOV_REMOTE_HELPER" in text
    assert "sync_remote_helper" in text
    assert "shared/bin/agent-gov-release-remote" in text
    assert 'invoke_remote_helper rollback "$ROLLBACK_RELEASE_ID"' in text
    assert 'invoke_remote_helper diagnose "$DIAGNOSE_RELEASE_ID"' in text


def test_deploy_keeps_release_metadata_and_images_immutable_without_github_secrets() -> None:
    text = text_of(DEPLOY_SCRIPT)
    verifier = text_of(CI_EVIDENCE_VERIFIER)

    assert 'RELEASE_ID="${ENVIRONMENT}-${SHORT_SHA}"' in text
    assert 'IMAGE_VERSION="${VERSION}-${SHORT_SHA}"' in text
    assert '"agent-gov-api:${IMAGE_VERSION}"' in text
    assert '"agent-gov-ui:${IMAGE_VERSION}"' in text
    assert '"agent-gov-litellm-sidecar:${IMAGE_VERSION}"' in text
    assert '"$REMOTE_PATH/releases/$RELEASE_ID"' in text
    assert '"$REMOTE_PATH/shared/docker.env"' in text
    assert '"repository": repository' in text
    assert '"aid_identifier": aid' in text
    assert '"commit_sha": sha' in text
    assert '"pr_number": int(pr_number)' in text
    assert '"workflow_url": workflow_url' in text
    assert '"ci_evidence": json.loads(ci_evidence_json)' in text
    assert '"image_digests": images' in text
    assert "project-images.tar.gz" in text
    assert "dependency-images.tar.gz" in text
    assert "sha256sum" in text
    assert "prepared_release_state" in text
    assert "PREPARED_STATE=$(prepared_release_state)" in text
    assert "Reusing committed immutable release" in text
    assert "verify_ci_evidence" in text
    assert '$(quote "$AID_IDENTIFIER") $(quote "$PR_NUMBER") $(quote "$WORKFLOW_URL")' in text
    assert '"aid_identifier": expected_aid' in text
    assert '"pr_number": int(expected_pr_number)' in text
    assert '"workflow_url": expected_workflow_url' in text
    assert 'ci_evidence = manifest.get("ci_evidence")' in text
    assert '"quality_gate": "success"' in text
    assert 'for key in ("workflow_run_id", "workflow_attempt")' in text
    assert "hasher.update(chunk)" in text
    assert "read_bytes()" not in text
    assert "GITHUB_TOKEN" not in text
    assert "GH_TOKEN" not in text
    assert "GITHUB_TOKEN" not in verifier
    assert "GH_TOKEN" not in verifier


def test_deploy_omits_empty_workflow_url_so_the_verifier_discovers_by_sha() -> None:
    text = text_of(DEPLOY_SCRIPT)

    assert 'workflow_args=(--workflow-url "$WORKFLOW_URL")' in text
    assert '"${workflow_args[@]}"' in text
    assert '      --workflow-url "$WORKFLOW_URL" \\' not in text


def test_remote_helper_restores_archived_images_and_checks_readiness() -> None:
    text = text_of(REMOTE_HELPER)

    assert "flock -n 9" in text
    assert 'RELEASES_DIR="$RELEASE_ROOT/releases"' in text
    assert 'SHARED_ENV="$SHARED_DIR/docker.env"' in text
    assert 'export AGENT_GOV_COMPOSE_ENV_FILE="$SHARED_ENV"' in text
    assert "atomic_current_link" in text
    assert 'load_release_images "$previous_path"' in text
    assert 'load_release_images "$target"' in text
    assert 'load_release_images "$current_path"' in text
    assert "legacy-images.tar.gz" in text
    assert 'docker save "${legacy_images[@]}"' in text
    assert "/health/ready" in text
    assert "Automatic rollback" in text
    assert "return 2" in text
    assert "return 3" in text
    assert "--profile langfuse up -d --wait --wait-timeout 180" in text
    assert "GITHUB_TOKEN" not in text
    assert "source_release/docker/.env.example" not in text
    assert "chmod a+rwx" not in text
    for key in (
        "LANGFUSE_POSTGRES_UID",
        "LANGFUSE_CLICKHOUSE_UID",
        "LANGFUSE_REDIS_UID",
        "LANGFUSE_MINIO_UID",
    ):
        assert key in text


def test_target_private_runtime_env_fails_closed_instead_of_using_an_example() -> None:
    deploy = text_of(DEPLOY_SCRIPT)
    remote = text_of(REMOTE_HELPER)

    assert "target private Compose env is missing" in deploy
    assert "Initializing target private Compose env" not in deploy
    assert "private Compose env is missing" in remote


def test_deploy_selects_one_complete_env_for_compose_interpolation_and_services() -> None:
    deploy = text_of(DEPLOY_SCRIPT)
    remote = text_of(REMOTE_HELPER)

    assert 'AGENT_GOV_COMPOSE_ENV_FILE="$LOCAL_COMPOSE_ENV_PATH"' in deploy
    assert '--env-file "$LOCAL_COMPOSE_ENV_PATH"' in deploy
    assert "os.path.abspath(sys.argv[1])" in deploy
    assert 'export COMPOSE_ENV_FILE="$SHARED_ENV"' in remote
    assert 'export AGENT_GOV_COMPOSE_ENV_FILE="$SHARED_ENV"' in remote


def test_invalid_ref_fails_locally_without_transport_or_network(tmp_path: Path) -> None:
    source = tmp_path / "source"
    subprocess.run(["git", "init", str(source)], check=True, capture_output=True)
    subprocess.run(
        [
            "git",
            "-C",
            str(source),
            "remote",
            "add",
            "origin",
            "https://github.test/wr5912/agent-gov.git",
        ],
        check=True,
        capture_output=True,
    )
    environment = os.environ.copy()
    environment["AGENT_GOV_SOURCE_REPO_DIR"] = str(source)

    result = subprocess.run(
        [
            "bash",
            str(DEPLOY_SCRIPT),
            "--ref",
            "not-a-sha",
            "--preflight-only",
        ],
        cwd=REPO_ROOT,
        env=environment,
        check=False,
        capture_output=True,
        text=True,
    )

    assert result.returncode == 1
    assert "lowercase full 40-character commit SHA" in result.stderr
    assert "--aid is required" not in result.stderr


def test_deployment_requires_complete_ci_trace_before_network_or_transport(
    tmp_path: Path,
) -> None:
    source = tmp_path / "source"
    subprocess.run(["git", "init", str(source)], check=True, capture_output=True)
    subprocess.run(
        [
            "git",
            "-C",
            str(source),
            "remote",
            "add",
            "origin",
            "https://github.test/wr5912/agent-gov.git",
        ],
        check=True,
        capture_output=True,
    )
    environment = {**os.environ, "AGENT_GOV_SOURCE_REPO_DIR": str(source)}
    base = ["bash", str(DEPLOY_SCRIPT), "--ref", "0" * 40]

    # --aid/--pr-number 可选（master 允许直推、提交没有 PR），但必须成对出现。
    aid_without_pr = subprocess.run(
        [*base, "--aid", "AID-16"],
        cwd=REPO_ROOT,
        env=environment,
        check=False,
        capture_output=True,
        text=True,
    )
    pr_without_aid = subprocess.run(
        [*base, "--pr-number", "42"],
        cwd=REPO_ROOT,
        env=environment,
        check=False,
        capture_output=True,
        text=True,
    )
    bad_workflow_url = subprocess.run(
        [*base, "--workflow-url", "https://evil.test/wr5912/agent-gov/actions/runs/1"],
        cwd=REPO_ROOT,
        env=environment,
        check=False,
        capture_output=True,
        text=True,
    )

    assert aid_without_pr.returncode == 1
    assert "--pr-number is required when --aid is supplied" in aid_without_pr.stderr
    assert pr_without_aid.returncode == 1
    assert "--aid is required when --pr-number is supplied" in pr_without_aid.stderr
    # workflow-url 可省略（按 SHA 反查），但**给了就必须属于本仓库**——
    # 否则等于允许拿别的仓库的绿 run 冒充本次部署的证据。
    assert bad_workflow_url.returncode == 1
    assert "workflow URL must belong to wr5912/agent-gov" in bad_workflow_url.stderr


def test_deployment_needs_no_trace_arguments_at_all(tmp_path: Path) -> None:
    """裸跑不该被任何「缺参数」挡下：ref/workflow-url 反查得到，PR/AID 已非必需。

    此处 origin 指向不存在的主机，因此跑到 fetch 就会以 git 的 128 退出——那正好证明它
    越过了全部参数校验、进到了需要网络的阶段，而不是在参数关就被拒。
    """
    source = tmp_path / "source"
    subprocess.run(["git", "init", str(source)], check=True, capture_output=True)
    subprocess.run(
        ["git", "-C", str(source), "remote", "add", "origin", "https://github.test/wr5912/agent-gov.git"],
        check=True,
        capture_output=True,
    )
    environment = {**os.environ, "AGENT_GOV_SOURCE_REPO_DIR": str(source)}

    result = subprocess.run(
        ["bash", str(DEPLOY_SCRIPT)],
        cwd=REPO_ROOT,
        env=environment,
        check=False,
        capture_output=True,
        text=True,
    )

    assert result.returncode == 128
    for gone in (
        "--ref is required",
        "--aid is required for deployment",
        "--pr-number is required for deployment",
        "--workflow-url is required for deployment",
    ):
        assert gone not in result.stderr


def test_deployment_rejects_malformed_trace_before_fetch(tmp_path: Path) -> None:
    source = tmp_path / "source"
    subprocess.run(["git", "init", str(source)], check=True, capture_output=True)
    subprocess.run(
        [
            "git",
            "-C",
            str(source),
            "remote",
            "add",
            "origin",
            "https://github.test/wr5912/agent-gov.git",
        ],
        check=True,
        capture_output=True,
    )
    environment = {**os.environ, "AGENT_GOV_SOURCE_REPO_DIR": str(source)}

    result = subprocess.run(
        [
            "bash",
            str(DEPLOY_SCRIPT),
            "--ref",
            "0" * 40,
            "--aid",
            "not-an-aid",
            "--pr-number",
            "42",
            "--workflow-url",
            "https://github.test/actions/runs/1",
        ],
        cwd=REPO_ROOT,
        env=environment,
        check=False,
        capture_output=True,
        text=True,
    )

    assert result.returncode == 1
    assert "invalid AID identifier" in result.stderr


def test_deployment_rejects_workflow_url_from_another_repository_before_fetch(
    tmp_path: Path,
) -> None:
    source = tmp_path / "source"
    subprocess.run(["git", "init", str(source)], check=True, capture_output=True)
    subprocess.run(
        [
            "git",
            "-C",
            str(source),
            "remote",
            "add",
            "origin",
            "https://github.test/wr5912/agent-gov.git",
        ],
        check=True,
        capture_output=True,
    )
    environment = {**os.environ, "AGENT_GOV_SOURCE_REPO_DIR": str(source)}

    result = subprocess.run(
        [
            "bash",
            str(DEPLOY_SCRIPT),
            "--ref",
            "0" * 40,
            "--aid",
            "AID-16",
            "--pr-number",
            "42",
            "--workflow-url",
            "https://github.com/another/project/actions/runs/1",
        ],
        cwd=REPO_ROOT,
        env=environment,
        check=False,
        capture_output=True,
        text=True,
    )

    assert result.returncode == 1
    assert "workflow URL must belong to wr5912/agent-gov" in result.stderr


def test_deployment_rejects_origin_repository_with_same_project_name(
    tmp_path: Path,
) -> None:
    source = tmp_path / "source"
    subprocess.run(["git", "init", str(source)], check=True, capture_output=True)
    subprocess.run(
        [
            "git",
            "-C",
            str(source),
            "remote",
            "add",
            "origin",
            "https://github.test/another/agent-gov.git",
        ],
        check=True,
        capture_output=True,
    )
    environment = {**os.environ, "AGENT_GOV_SOURCE_REPO_DIR": str(source)}

    result = subprocess.run(
        [
            "bash",
            str(DEPLOY_SCRIPT),
            "--ref",
            "0" * 40,
            "--preflight-only",
        ],
        cwd=REPO_ROOT,
        env=environment,
        check=False,
        capture_output=True,
        text=True,
    )

    assert result.returncode == 1
    assert "git origin repository must be wr5912/agent-gov" in result.stderr


class FakeGitHub:
    def __init__(self, responses: dict[str, object]) -> None:
        self.responses = responses
        self.calls: list[str] = []

    def get(self, path: str) -> object:
        self.calls.append(path)
        if path not in self.responses:
            raise AssertionError(f"unexpected GitHub path: {path}")
        return self.responses[path]


def valid_evidence_fixture() -> tuple[EvidenceConfig, dict[str, object]]:
    repository = "wr5912/agent-gov"
    commit_sha = "a" * 40
    run_id = 901
    attempt = 2
    pr_number = 42
    config = EvidenceConfig(
        repository=repository,
        commit_sha=commit_sha,
        aid_identifier="AID-16",
        pr_number=pr_number,
        workflow_url=f"https://github.com/{repository}/actions/runs/{run_id}/attempts/{attempt}",
    )
    responses: dict[str, object] = {
        f"/repos/{repository}/actions/runs/{run_id}": {
            "id": run_id,
            "run_attempt": attempt,
            "event": "push",
            "head_branch": "master",
            "head_sha": commit_sha,
            "status": "completed",
            "conclusion": "success",
            "path": ".github/workflows/governance.yml@refs/heads/master",
            "repository": {"full_name": repository},
        },
        f"/repos/{repository}/actions/runs/{run_id}/attempts/{attempt}/jobs?per_page=100": {
            "jobs": [
                {
                    "name": "quality-gate",
                    "status": "completed",
                    "conclusion": "success",
                }
            ]
        },
        f"/repos/{repository}/commits/{commit_sha}/pulls?per_page=100": [
            {
                "number": pr_number,
                "merged_at": "2026-07-16T00:00:00Z",
                "merge_commit_sha": commit_sha,
                "base": {"ref": "master"},
            }
        ],
        f"/repos/{repository}/pulls/{pr_number}": {
            "number": pr_number,
            "state": "closed",
            "merged_at": "2026-07-16T00:00:00Z",
            "merge_commit_sha": commit_sha,
            "base": {"ref": "master"},
            "head": {"ref": "feature/AID-16-ci-evidence"},
            "title": "Verify staging deployment evidence",
            "body": "",
        },
    }
    return config, responses


def test_deployment_ci_evidence_binds_successful_master_run_pr_and_aid() -> None:
    config, responses = valid_evidence_fixture()
    github = FakeGitHub(responses)

    evidence = verify_ci_evidence(config, github)

    assert evidence.commit_sha == config.commit_sha
    assert evidence.workflow_run_id == 901
    assert evidence.workflow_attempt == 2
    assert evidence.quality_gate == "success"
    assert evidence.pr_number == config.pr_number
    assert evidence.aid_identifier == config.aid_identifier
    assert len(github.calls) == 4


def _direct_push_config(config: EvidenceConfig) -> EvidenceConfig:
    """同一份 fixture 去掉 PR/AID —— 模拟 master 直推（无分支保护）的提交。"""
    return dataclasses.replace(config, aid_identifier=None, pr_number=None)


def _discovery_fixture(
    extra_runs: list[dict[str, object]] | None = None,
) -> tuple[EvidenceConfig, dict[str, object]]:
    """省略 workflow_url ⇒ 走 SHA 反查。extra_runs 排在成功 run 之前，模拟同 SHA 多次 run。"""
    base, responses = valid_evidence_fixture()
    direct = dataclasses.replace(base, workflow_url=None, aid_identifier=None, pr_number=None)
    successful_run = responses["/repos/wr5912/agent-gov/actions/runs/901"]
    assert isinstance(successful_run, dict)
    listing = f"/repos/wr5912/agent-gov/actions/runs?head_sha={direct.commit_sha}&event=push&branch=master&per_page=100"
    responses[listing] = {"workflow_runs": [*(extra_runs or []), successful_run]}
    return direct, responses


def test_ci_evidence_discovers_master_push_run_from_commit_sha() -> None:
    config, responses = _discovery_fixture()
    github = FakeGitHub(responses)

    evidence = verify_ci_evidence(config, github)

    assert evidence.workflow_run_id == 901
    assert evidence.quality_gate == "success"
    # 反查到的 run 必须原样留痕给 release.json —— 不能写空串或调用者转述的值。
    assert evidence.workflow_url == "https://github.com/wr5912/agent-gov/actions/runs/901/attempts/2"


def test_ci_evidence_discovery_skips_failed_run_and_picks_successful_rerun() -> None:
    """同 SHA 既有失败 run 又有成功重跑时，必须挑成功那个——挑错会把「有绿」误判成「不可部署」。"""
    config, responses = _discovery_fixture(
        extra_runs=[
            {
                "id": 999,  # 比成功 run 更大：若只按 id 取最新而不过滤结论，就会挑中它
                "run_attempt": 1,
                "event": "push",
                "head_branch": "master",
                "head_sha": "a" * 40,
                "status": "completed",
                "conclusion": "failure",
                "path": ".github/workflows/governance.yml@refs/heads/master",
                "repository": {"full_name": "wr5912/agent-gov"},
            }
        ]
    )

    evidence = verify_ci_evidence(config, FakeGitHub(responses))

    assert evidence.workflow_run_id == 901


def test_ci_evidence_discovery_ignores_other_workflow_files() -> None:
    """container-live-acceptance 之类的其它 workflow 长期红，绝不能被当成 quality-gate 证据。"""
    config, responses = _discovery_fixture(
        extra_runs=[
            {
                "id": 998,
                "run_attempt": 1,
                "event": "push",
                "head_branch": "master",
                "head_sha": "a" * 40,
                "status": "completed",
                "conclusion": "success",
                "path": ".github/workflows/container-live-acceptance.yml@refs/heads/master",
                "repository": {"full_name": "wr5912/agent-gov"},
            }
        ]
    )

    evidence = verify_ci_evidence(config, FakeGitHub(responses))

    assert evidence.workflow_run_id == 901


def test_ci_evidence_discovery_rejects_commit_without_successful_run() -> None:
    config, responses = _discovery_fixture()
    listing = f"/repos/wr5912/agent-gov/actions/runs?head_sha={config.commit_sha}&event=push&branch=master&per_page=100"
    responses[listing] = {"workflow_runs": []}

    with pytest.raises(EvidenceError, match="no successful .* run found"):
        verify_ci_evidence(config, FakeGitHub(responses))


def test_deployment_ci_evidence_accepts_direct_push_without_pr_trace() -> None:
    config, responses = valid_evidence_fixture()
    github = FakeGitHub(responses)

    evidence = verify_ci_evidence(_direct_push_config(config), github)

    assert evidence.commit_sha == config.commit_sha
    assert evidence.quality_gate == "success"
    assert evidence.pr_number is None
    assert evidence.aid_identifier is None
    # 不查 PR，就不该打 PR 的两个接口：直推提交上那两个接口本来就返回空。
    assert len(github.calls) == 2
    assert not [call for call in github.calls if "pulls" in call]


@pytest.mark.parametrize(
    ("field", "value", "message"),
    [
        ("conclusion", "failure", "workflow run conclusion mismatch"),
        ("head_sha", "b" * 40, "workflow run head_sha mismatch"),
        ("event", "pull_request", "workflow run event mismatch"),
        ("head_branch", "feature/x", "workflow run head_branch mismatch"),
        ("path", ".github/workflows/other.yml", "workflow file mismatch"),
    ],
)
def test_direct_push_evidence_still_rejects_wrong_workflow_run(
    field: str,
    value: str,
    message: str,
) -> None:
    """放宽 PR/AID 后，剩下这条证据链是唯一防线，必须一条都不松。"""
    config, original = valid_evidence_fixture()
    responses = copy.deepcopy(original)
    run = responses["/repos/wr5912/agent-gov/actions/runs/901"]
    assert isinstance(run, dict)
    run[field] = value

    with pytest.raises(EvidenceError, match=message):
        verify_ci_evidence(_direct_push_config(config), FakeGitHub(responses))


def test_direct_push_evidence_still_rejects_failed_quality_gate() -> None:
    config, original = valid_evidence_fixture()
    responses = copy.deepcopy(original)
    jobs = responses["/repos/wr5912/agent-gov/actions/runs/901/attempts/2/jobs?per_page=100"]
    assert isinstance(jobs, dict)
    job_list = jobs["jobs"]
    assert isinstance(job_list, list)
    job = job_list[0]
    assert isinstance(job, dict)
    job["conclusion"] = "failure"

    with pytest.raises(EvidenceError, match="quality-gate job is not successful"):
        verify_ci_evidence(_direct_push_config(config), FakeGitHub(responses))


@pytest.mark.parametrize(
    ("aid", "pr_number"),
    [("AID-16", None), (None, 42)],
)
def test_deployment_ci_evidence_requires_pr_and_aid_together(
    aid: str | None,
    pr_number: int | None,
) -> None:
    """只给一个会让 AID 校验静默失效：AID 是从 PR 元数据里读出来比对的。"""
    config, responses = valid_evidence_fixture()
    partial = dataclasses.replace(config, aid_identifier=aid, pr_number=pr_number)

    with pytest.raises(EvidenceError, match="must be supplied together"):
        verify_ci_evidence(partial, FakeGitHub(responses))


@pytest.mark.parametrize(
    ("field", "value", "message"),
    [
        ("conclusion", "failure", "workflow run conclusion mismatch"),
        ("head_sha", "b" * 40, "workflow run head_sha mismatch"),
        ("event", "pull_request", "workflow run event mismatch"),
        ("path", ".github/workflows/other.yml", "workflow file mismatch"),
    ],
)
def test_deployment_ci_evidence_rejects_wrong_workflow_run(
    field: str,
    value: str,
    message: str,
) -> None:
    config, original = valid_evidence_fixture()
    responses = copy.deepcopy(original)
    run = responses["/repos/wr5912/agent-gov/actions/runs/901"]
    assert isinstance(run, dict)
    run[field] = value

    with pytest.raises(EvidenceError, match=message):
        verify_ci_evidence(config, FakeGitHub(responses))


def test_deployment_ci_evidence_rejects_failed_quality_gate() -> None:
    config, original = valid_evidence_fixture()
    responses = copy.deepcopy(original)
    jobs = responses["/repos/wr5912/agent-gov/actions/runs/901/attempts/2/jobs?per_page=100"]
    assert isinstance(jobs, dict)
    job_list = jobs["jobs"]
    assert isinstance(job_list, list)
    job = job_list[0]
    assert isinstance(job, dict)
    job["conclusion"] = "failure"

    with pytest.raises(EvidenceError, match="quality-gate job is not successful"):
        verify_ci_evidence(config, FakeGitHub(responses))


def test_deployment_ci_evidence_rejects_unrelated_pr_or_aid() -> None:
    config, original = valid_evidence_fixture()
    unrelated_pr = copy.deepcopy(original)
    pulls = unrelated_pr[f"/repos/wr5912/agent-gov/commits/{config.commit_sha}/pulls?per_page=100"]
    assert isinstance(pulls, list)
    pull = pulls[0]
    assert isinstance(pull, dict)
    pull["number"] = 43
    with pytest.raises(EvidenceError, match="exactly the supplied pull request"):
        verify_ci_evidence(config, FakeGitHub(unrelated_pr))

    wrong_aid = copy.deepcopy(original)
    pull_detail = wrong_aid["/repos/wr5912/agent-gov/pulls/42"]
    assert isinstance(pull_detail, dict)
    pull_detail["head"] = {"ref": "feature/AID-17-ci-evidence"}
    with pytest.raises(EvidenceError, match="pull request AID mismatch"):
        verify_ci_evidence(config, FakeGitHub(wrong_aid))


def test_deployment_ci_evidence_rejects_workflow_attempt_substitution() -> None:
    config, responses = valid_evidence_fixture()
    substituted = EvidenceConfig(
        repository=config.repository,
        commit_sha=config.commit_sha,
        aid_identifier=config.aid_identifier,
        pr_number=config.pr_number,
        workflow_url="https://github.com/wr5912/agent-gov/actions/runs/901/attempts/1",
    )

    with pytest.raises(EvidenceError, match="workflow run attempt mismatch"):
        verify_ci_evidence(substituted, FakeGitHub(responses))
