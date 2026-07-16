"""血缘固化与人工出口的契约测试。

针对三类此前无覆盖的失败：

1. **合并后编辑 PR 即可永久 wedge 整条发布链**。GitHub 允许 PR 合并后继续编辑
   标题/正文，而控制器在部署时重新查 live PR。任何人往自己已合并的 PR 正文加一句
   "参考 AID-99" → 该提交被永久隔离；又因 validate_lineage 每轮回放 cursor..head 的
   每一个提交，此后每个新 head 也验不过。
2. **一次 GitHub 5xx / 超时即永久隔离一个合法发布**。传输故障与业务违规此前共用
   ControllerError，无法分流；poll 每 30 秒一轮、等 CI 窗口默认 2 小时。
3. **隔离后无解封出口**，只能手改 sqlite。
"""

from __future__ import annotations

import sys
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parents[1]
SCRIPTS_DIR = REPO_ROOT / "scripts"
sys.path.insert(0, str(SCRIPTS_DIR))

from agent_gov_release_controller import (  # noqa: E402
    link_head_release,
    resolve_pull_request,
)
from agent_gov_release_state import (  # noqa: E402
    ControllerConfig,
    ControllerError,
    ReleaseStatus,
    StateStore,
    TransportError,
)

HEAD = "a" * 40
CURSOR = "b" * 40


class FakeGitHub:
    """按路径前缀返回预设响应；可注入传输故障，并记录调用次数。"""

    def __init__(self, responses: dict[str, object]) -> None:
        self._responses = responses
        self.calls: list[str] = []

    def get(self, path: str) -> object:
        self.calls.append(path)
        for prefix, value in self._responses.items():
            if prefix in path:
                if isinstance(value, Exception):
                    raise value
                return value
        raise AssertionError(f"unexpected GitHub path: {path}")


def _pull(number: int, sha: str, *, title: str, body: str = "") -> dict:
    return {
        "number": number,
        "merged_at": "2026-07-15T00:00:00Z",
        "base": {"ref": "master"},
        "merge_commit_sha": sha,
        "merged_by": {"login": "wr5912"},
        "head": {"ref": "feature/x"},
        "title": title,
        "body": body,
    }


@pytest.fixture()
def config(tmp_path: Path) -> ControllerConfig:
    # 用真实的 ControllerConfig 而非手搓 stub——手搓的会漏字段（如 owner_repo），
    # 且一旦真实配置演进，stub 会给出虚假的绿。
    return ControllerConfig(
        repository="wr5912/agent-gov",
        branch="master",
        environment="staging-232",
        deploy_host="172.16.112.232",
        deploy_user="root",
        remote_dir="~/work/agent-gov",
        state_dir=tmp_path / "state",
        deploy_script=SCRIPTS_DIR / "deploy_agent_gov_to_host",
        github_api_url="https://api.github.test",
        multica_profile="release-controller",
        quality_check="quality-gate",
        workflow_file=".github/workflows/governance.yml",
        allowed_mergers=("wr5912",),
        release_sre_agent="release-sre",
        release_sre_metadata_key="release_sre_issue_id",
        require_branch_protection=False,
        ci_timeout_seconds=7200,
    )


@pytest.fixture()
def store(tmp_path: Path) -> StateStore:
    s = StateStore(tmp_path / "state.db")
    yield s
    s.close()


def test_resolved_lineage_is_frozen_and_survives_a_post_merge_pr_edit(
    config: ControllerConfig, store: StateStore
) -> None:
    """PR 合并后被编辑成两个 AID，已固化的提交必须不受影响。"""
    github = FakeGitHub({f"/commits/{HEAD}/pulls": [_pull(64, HEAD, title="feat: AID-16 x")]})

    first = resolve_pull_request(config, github, store, HEAD)
    assert first.aid_identifier == "AID-16"
    assert len(github.calls) == 1

    # 有人在 PR 合并之后往正文里加了第二个 AID——放在以前这会让该提交被永久隔离。
    github._responses[f"/commits/{HEAD}/pulls"] = [
        _pull(64, HEAD, title="feat: AID-16 x", body="参考 AID-99")
    ]

    second = resolve_pull_request(config, github, store, HEAD)

    assert second == first, "已固化的血缘必须不随 PR 事后编辑而改变"
    assert len(github.calls) == 1, "固化之后不得再查 live PR"


def test_business_violation_is_not_frozen_so_it_stays_rejected(
    config: ControllerConfig, store: StateStore
) -> None:
    """业务违规不得被固化——否则一个非法提交会被缓存成"合法"。"""
    github = FakeGitHub(
        {f"/commits/{HEAD}/pulls": [_pull(64, HEAD, title="feat: AID-16 and AID-99")]}
    )

    with pytest.raises(ControllerError, match="exactly one AID"):
        resolve_pull_request(config, github, store, HEAD)

    assert store.get_commit_link(HEAD) is None, "校验失败的结果绝不能进快照"


def test_transport_failure_is_not_frozen_and_not_quarantined(
    config: ControllerConfig, store: StateStore
) -> None:
    """GitHub 5xx/超时只说明"这一次没问到"，不得固化、更不得隔离。"""
    github = FakeGitHub(
        {f"/commits/{HEAD}/pulls": TransportError("GitHub API GET failed with HTTP 502")}
    )

    with pytest.raises(TransportError):
        resolve_pull_request(config, github, store, HEAD)

    assert store.get_commit_link(HEAD) is None, "传输故障绝不能进快照"


def test_link_head_release_quarantines_business_violation_but_not_transport_failure(
    config: ControllerConfig, store: StateStore
) -> None:
    """link_head_release 的分流：业务违规隔离，传输故障放行重试。"""
    compare = {
        "status": "ahead",
        "total_commits": 1,
        "commits": [{"sha": HEAD}],
    }

    # 传输故障：不得隔离，让下一轮 poll 重试
    transport_github = FakeGitHub(
        {
            f"/compare/{CURSOR}...{HEAD}": compare,
            f"/commits/{HEAD}/pulls": TransportError("HTTP 503"),
        }
    )
    with pytest.raises(TransportError):
        link_head_release(config, transport_github, store, CURSOR, HEAD)
    row = store.get_release(HEAD)
    assert row is None or ReleaseStatus(row["status"]) != ReleaseStatus.QUARANTINED, (
        "一次网络抖动不得永久废掉一个合法发布"
    )

    # 业务违规：必须隔离（fail-closed 方向不变）
    business_github = FakeGitHub(
        {
            f"/compare/{CURSOR}...{HEAD}": compare,
            f"/commits/{HEAD}/pulls": [_pull(64, HEAD, title="no identifier here")],
        }
    )
    with pytest.raises(ControllerError):
        link_head_release(config, business_github, store, CURSOR, HEAD)
    row = store.get_release(HEAD)
    assert row is not None
    assert ReleaseStatus(row["status"]) == ReleaseStatus.QUARANTINED


def test_quarantined_commit_can_be_manually_released_back_to_the_ci_gate(
    store: StateStore,
) -> None:
    """隔离必须有人工出口——此前解封只能手改 sqlite。"""
    store.discover(HEAD)
    store.transition(HEAD, ReleaseStatus.QUARANTINED, reason="lineage rejected")

    store.transition(HEAD, ReleaseStatus.WAITING_CI, reason="manually unquarantined by ops")

    row = store.get_release(HEAD)
    assert row is not None
    assert ReleaseStatus(row["status"]) == ReleaseStatus.WAITING_CI
