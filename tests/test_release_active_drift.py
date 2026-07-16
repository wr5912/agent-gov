"""「当前版本」漂移的契约测试。

针对三类此前无覆盖的失败：

1. **人工回滚 30 秒后被 poll 悄悄抹掉**。rollback 只裸写 active metadata、不动被换下
   release 的状态，于是它仍是 SUCCEEDED；下一轮 poll 的 `cursor == head_sha` 分支看到
   "head 仍成功"就无条件把 active 覆写回去 —— 机器上跑 A、治理面说 B，且永不自愈。
2. **控制器的 active 指针从未跟机器核对过**。远端 `status` 子命令早就能报告机器上真实
   在跑的版本，但全仓库无人调用（死代码），于是漂移无人发现。
3. **对账本身不得成为新的单点**：目标机连不上时只能记事件，不能打断 poll。

两层"机器"，各有分工：

- 多数用例用一个**假部署脚本**（真 bash、真 current 符号链接）当机器，用来编排各种
  漂移场景。rollback 与对账器都以真实子进程跑过去 —— 把 run_logged 打桩掉就看不见
  bug 了：active 漂移恰恰发生在"脚本成功了、而状态没跟上"这个缝里。
- `..._real_deploy_script_...` 两条则**真正执行仓库里的 deploy 脚本**（`--host localhost`
  走 LOCAL_TARGET）。remote-status 是本批新增的 shell 路径，而这一层此前只有 `bash -n`
  和 grep 源码文本 —— 往脚本里加一句 `|| true` 都不会红，不能再靠那种"测试"上线。
"""

from __future__ import annotations

import json
import os
import subprocess
import sys
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parents[1]
SCRIPTS_DIR = REPO_ROOT / "scripts"
sys.path.insert(0, str(SCRIPTS_DIR))

import agent_gov_release_controller as controller  # noqa: E402
from agent_gov_release_reconcile import reconcile_active_release  # noqa: E402
from agent_gov_release_state import (  # noqa: E402
    ControllerConfig,
    ReleaseStatus,
    StateStore,
)

HEAD = "c" * 40
RELEASE_OLD = "staging-232-cccccccc"
RELEASE_PREVIOUS = "staging-232-bbbbbbbb"
PREVIOUS_SHA = "d" * 40

# 假部署脚本：既是被回滚的"机器"，也是被对账问话的"机器"。
# --rollback <id> 把 current 指向该 release；--remote-status 把 current 的真名打成 JSON。
FAKE_DEPLOY_SCRIPT = """#!/usr/bin/env bash
set -euo pipefail
root="$FAKE_RELEASE_ROOT"
mode=""
release_id=""
while (( $# )); do
  case "$1" in
    --rollback) mode="rollback"; release_id="$2"; shift 2 ;;
    --remote-status) mode="remote-status"; shift ;;
    *) shift ;;
  esac
done
if [[ "$mode" == "rollback" ]]; then
  [[ "${FAKE_ROLLBACK_EXIT:-0}" == "0" ]] || exit "${FAKE_ROLLBACK_EXIT}"
  mkdir -p "$root/releases/$release_id"
  ln -sfn "$root/releases/$release_id" "$root/current"
  exit 0
fi
if [[ "$mode" == "remote-status" ]]; then
  [[ "${FAKE_STATUS_EXIT:-0}" == "0" ]] || exit "${FAKE_STATUS_EXIT}"
  echo "[deploy] Checking target prerequisites" >&2
  active=""
  [[ -e "$root/current" ]] && active=$(basename "$(readlink -f "$root/current")")
  printf '{"active_release": %s, "releases": []}\\n' \
    "$([[ -n "$active" ]] && printf '"%s"' "$active" || printf 'null')"
  exit 0
fi
exit 9
"""


@pytest.fixture()
def release_root(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    root = tmp_path / "machine"
    (root / "releases").mkdir(parents=True)
    monkeypatch.setenv("FAKE_RELEASE_ROOT", str(root))
    return root


@pytest.fixture()
def deploy_script(tmp_path: Path) -> Path:
    path = tmp_path / "fake_deploy"
    path.write_text(FAKE_DEPLOY_SCRIPT, encoding="utf-8")
    path.chmod(0o755)
    return path


@pytest.fixture()
def config(tmp_path: Path, deploy_script: Path) -> ControllerConfig:
    return ControllerConfig(
        repository="wr5912/agent-gov",
        branch="master",
        environment="staging-232",
        deploy_host="172.16.112.232",
        deploy_user="root",
        remote_dir="~/work/agent-gov",
        state_dir=tmp_path / "state",
        deploy_script=deploy_script,
        github_api_url="https://api.github.test",
        multica_profile="release-controller",
        quality_check="quality-gate",
        workflow_file=".github/workflows/governance.yml",
        allowed_mergers=("trusted-merger",),
        release_sre_agent="release-sre",
        release_sre_metadata_key="release_sre_issue_id",
        require_branch_protection=False,
        ci_timeout_seconds=7200,
    )


@pytest.fixture()
def store(tmp_path: Path) -> StateStore:
    (tmp_path / "state").mkdir(exist_ok=True)
    s = StateStore(tmp_path / "state" / "state.db")
    yield s
    s.close()


def _succeeded_release(store: StateStore, sha: str, release_id: str, *, aid: str) -> None:
    """把一个提交推到 SUCCEEDED 并带上 release_id（走真实的状态机转移，不直接写库）。"""
    store.discover(sha)
    store.set_linkage(sha, pr_number=64, aid_identifiers=[aid], release_id=release_id)
    store.transition(sha, ReleaseStatus.WAITING_CI, reason="ci passed")
    store.transition(sha, ReleaseStatus.DEPLOYING, reason="deploying")
    store.transition(sha, ReleaseStatus.SUCCEEDED, reason="deployed")


def test_manual_rollback_marks_the_replaced_release_and_is_not_undone_by_poll(
    config: ControllerConfig,
    store: StateStore,
    release_root: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """回滚 → 下一轮 poll → active 必须还是回滚的目标。

    这是整个批次的招牌用例：修复前，poll 会在 30 秒内把 active 覆写回 head。
    """
    _succeeded_release(store, HEAD, RELEASE_OLD, aid="AID-16")
    _succeeded_release(store, PREVIOUS_SHA, RELEASE_PREVIOUS, aid="AID-15")
    store.set_metadata("active:staging-232", RELEASE_OLD)
    store.set_metadata("cursor:master", HEAD)
    store.close()

    # 1) 人工回滚到上一版（真实跑假脚本，它会翻转真实的 current 符号链接）
    exit_code = controller.rollback(config, RELEASE_PREVIOUS, "ops-operator")
    assert exit_code == 0

    reopened = StateStore(config.state_dir / "state.db")
    try:
        assert reopened.get_metadata("active:staging-232") == RELEASE_PREVIOUS
        replaced = reopened.get_release(HEAD)
        assert replaced is not None
        assert ReleaseStatus(replaced["status"]) == ReleaseStatus.ROLLED_BACK, (
            "被换下的 release 必须落到 ROLLED_BACK，否则 poll 会把它当成仍在线的 head"
        )

        # 2) 下一轮 poll：head 仍是 HEAD、cursor 仍是 HEAD —— 正是覆写 bug 的触发条件
        github = _fake_github_at_head(monkeypatch)
        controller.reconcile_head(config, github, reopened)

        # 3) 断言 active 没被抹回去
        assert reopened.get_metadata("active:staging-232") == RELEASE_PREVIOUS, (
            "一轮 poll 就把人工回滚抹掉了：治理面会开始对运维撒谎"
        )
        assert basename_of_current(release_root) == RELEASE_PREVIOUS, (
            "机器上的 current 与治理面记录必须一致"
        )
    finally:
        reopened.close()


def test_rollback_then_a_real_poll_then_status_agree_with_the_machine(
    config: ControllerConfig,
    store: StateStore,
    release_root: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    """端到端三步：rollback → 真实 poll（含对账器）→ status。

    与上面的用例不同，这里跑的是真正的 poll() 入口，因此对账器、reconcile_head、
    outbox flush 全在链路上；status 打印的 JSON 必须与机器上的 current 一致。
    这是计划要求的验收路径：治理面、控制器状态、机器事实三者不得互相撒谎。
    """
    _succeeded_release(store, HEAD, RELEASE_OLD, aid="AID-16")
    _succeeded_release(store, PREVIOUS_SHA, RELEASE_PREVIOUS, aid="AID-15")
    store.set_metadata("active:staging-232", RELEASE_OLD)
    store.set_metadata("cursor:master", HEAD)
    store.close()

    (release_root / "releases" / RELEASE_OLD).mkdir(parents=True)
    (release_root / "current").symlink_to(release_root / "releases" / RELEASE_OLD)

    monkeypatch.setattr(controller, "load_github_token", lambda: "fake-token")
    monkeypatch.setattr(controller, "GitHubClient", lambda **_kwargs: _RepoPolicyGitHub())
    monkeypatch.setattr(controller, "flush_outbox", lambda *_args, **_kwargs: None)

    # 1) 人工回滚：真实子进程翻转机器上的 current
    assert controller.rollback(config, RELEASE_PREVIOUS, "ops-operator") == 0
    assert basename_of_current(release_root) == RELEASE_PREVIOUS

    # 2) 真实 poll（对账器会去问那台"机器"）
    controller.poll(config)

    # 3) status 必须与机器一致
    capsys.readouterr()
    controller.show_status(config)
    snapshot = json.loads(capsys.readouterr().out)

    assert snapshot["metadata"]["active:staging-232"] == RELEASE_PREVIOUS
    assert basename_of_current(release_root) == RELEASE_PREVIOUS
    assert snapshot["metadata"]["active:staging-232"] == basename_of_current(release_root), (
        "治理面报的版本必须就是机器上跑的版本"
    )
    assert "active_drift" not in [event["event_type"] for event in snapshot["events"]], (
        "回滚已如实记账，对账器不该再报漂移"
    )


def test_poll_reconciles_a_drifted_record_against_the_machine(
    config: ControllerConfig,
    store: StateStore,
    release_root: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """poll 必须真的调用对账器。

    这条用例守的是**接线本身**：远端 status 早就实现了，却因为没有调用者而成为死代码，
    于是漂移无人发现。把对账器从 poll 里摘掉时，其余用例全都照绿——只有这条会红。
    """
    _succeeded_release(store, HEAD, RELEASE_OLD, aid="AID-16")
    store.set_metadata("cursor:master", HEAD)
    store.set_metadata("active:staging-232", RELEASE_OLD)  # 治理面以为跑的是 OLD
    store.close()

    # 机器上实际跑的是上一版（例如有人直接在机器上动了 current）
    (release_root / "releases" / RELEASE_PREVIOUS).mkdir(parents=True)
    (release_root / "current").symlink_to(release_root / "releases" / RELEASE_PREVIOUS)

    monkeypatch.setattr(controller, "load_github_token", lambda: "fake-token")
    monkeypatch.setattr(controller, "GitHubClient", lambda **_kwargs: _RepoPolicyGitHub())
    monkeypatch.setattr(controller, "flush_outbox", lambda *_args, **_kwargs: None)

    controller.poll(config)

    reopened = StateStore(config.state_dir / "state.db")
    try:
        assert reopened.get_metadata("active:staging-232") == RELEASE_PREVIOUS, (
            "poll 没有对账：治理面继续报着一个机器上根本没在跑的版本"
        )
        events = [event["event_type"] for event in reopened.snapshot()["events"]]
        assert "active_drift" in events
    finally:
        reopened.close()


class _RepoPolicyGitHub:
    """只回答 poll 必需的两个问题：仓库合并策略、master 的 head。"""

    def get(self, path: str) -> object:
        if path == "/repos/wr5912/agent-gov":
            return {
                "allow_squash_merge": True,
                "allow_merge_commit": False,
                "allow_rebase_merge": False,
            }
        if path == "/repos/wr5912/agent-gov/branches/master":
            return {"commit": {"sha": HEAD}}
        raise AssertionError(f"unexpected GitHub GET: {path}")


def _fake_github_at_head(monkeypatch: pytest.MonkeyPatch) -> object:
    class _GitHub:
        def get(self, path: str) -> object:
            raise AssertionError(f"unexpected GitHub GET: {path}")

    monkeypatch.setattr(controller, "current_branch_head", lambda *_args: HEAD)
    return _GitHub()


def basename_of_current(release_root: Path) -> str:
    return os.path.basename(os.path.realpath(release_root / "current"))


def test_poll_does_not_overwrite_an_active_pointer_that_disagrees_with_a_successful_head(
    config: ControllerConfig,
    store: StateStore,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """head 仍是 SUCCEEDED、但 active 指向别的 release 时，poll 不得覆写。

    这条边独立于 rollback 的修复：对账器刚把 active 纠正成机器上的真相（上一版），
    而 head 的状态仍然是 SUCCEEDED —— 若 poll 无条件覆写，下一轮就把刚纠正的结果
    又抹回 head，与机器再次背离，且每 30 秒重复一次。
    """
    _succeeded_release(store, HEAD, RELEASE_OLD, aid="AID-16")
    store.set_metadata("cursor:master", HEAD)
    # 机器上实际跑的是上一版（例如对账器刚回填过，或历史遗留的裸 metadata 回滚）
    store.set_metadata("active:staging-232", RELEASE_PREVIOUS)

    controller.reconcile_head(config, _fake_github_at_head(monkeypatch), store)

    assert store.get_metadata("active:staging-232") == RELEASE_PREVIOUS, (
        "poll 把已确立的 active 覆写回 head 了：对账刚纠正完就被抹掉，且每轮重复"
    )


def test_poll_still_initializes_active_when_it_has_never_been_set(
    config: ControllerConfig,
    store: StateStore,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """修 bug 不能把功能一起修没：active 从未建立时，poll 仍要初始化它。"""
    _succeeded_release(store, HEAD, RELEASE_OLD, aid="AID-16")
    store.set_metadata("cursor:master", HEAD)
    assert store.get_metadata("active:staging-232") is None

    controller.reconcile_head(config, _fake_github_at_head(monkeypatch), store)

    assert store.get_metadata("active:staging-232") == RELEASE_OLD


def test_rollback_does_not_force_an_illegal_transition_on_the_replaced_release(
    config: ControllerConfig,
    store: StateStore,
    release_root: Path,
) -> None:
    """被换下的 release 已经是 ROLLED_BACK 时，不得强推一次非法转移。

    可达路径：对账器把 active 回填成机器上的真相，而那个 release 早已被标记 ROLLED_BACK；
    此时再回滚一次，replaced 就处在不可再回滚的状态。宁可只落指针并如实记账，
    也不能让状态机为了"记账好看"接受一条它本不允许的边。
    """
    _succeeded_release(store, HEAD, RELEASE_OLD, aid="AID-16")
    _succeeded_release(store, PREVIOUS_SHA, RELEASE_PREVIOUS, aid="AID-15")
    store.transition(HEAD, ReleaseStatus.ROLLED_BACK, reason="rolled back earlier")
    store.set_metadata("active:staging-232", RELEASE_OLD)  # active 仍指着它
    store.close()

    assert controller.rollback(config, RELEASE_PREVIOUS, "ops-operator") == 0

    reopened = StateStore(config.state_dir / "state.db")
    try:
        assert reopened.get_metadata("active:staging-232") == RELEASE_PREVIOUS
        events = [event["event_type"] for event in reopened.snapshot()["events"]]
        assert "manual_rollback_replaced_non_successful" in events, "这种情况必须留痕"
        replaced = reopened.get_release(HEAD)
        assert replaced is not None
        assert ReleaseStatus(replaced["status"]) == ReleaseStatus.ROLLED_BACK
    finally:
        reopened.close()


def test_reconciler_takes_the_target_host_as_the_source_of_truth(
    config: ControllerConfig,
    store: StateStore,
    release_root: Path,
) -> None:
    """本地记录与机器不一致时，以机器为准回填并告警。"""
    (release_root / "releases" / RELEASE_PREVIOUS).mkdir(parents=True)
    (release_root / "current").symlink_to(release_root / "releases" / RELEASE_PREVIOUS)
    store.set_metadata("active:staging-232", RELEASE_OLD)  # 本地记的是另一个

    reconcile_active_release(config, store)

    assert store.get_metadata("active:staging-232") == RELEASE_PREVIOUS
    events = [event["event_type"] for event in store.snapshot()["events"]]
    assert "active_drift" in events, "漂移必须留痕，否则没人知道治理面曾经撒过谎"


def test_reconciler_is_silent_when_the_record_already_matches(
    config: ControllerConfig,
    store: StateStore,
    release_root: Path,
) -> None:
    """一致时不得刷告警——否则 active_drift 会因为天天响而被忽略。"""
    (release_root / "releases" / RELEASE_OLD).mkdir(parents=True)
    (release_root / "current").symlink_to(release_root / "releases" / RELEASE_OLD)
    store.set_metadata("active:staging-232", RELEASE_OLD)

    reconcile_active_release(config, store)

    events = [event["event_type"] for event in store.snapshot()["events"]]
    assert "active_drift" not in events


def test_unreachable_target_host_does_not_overwrite_the_local_record(
    config: ControllerConfig,
    store: StateStore,
    release_root: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """机器问不到时必须 fail-soft：不清空 active、不抛异常打断 poll。

    "没问到"绝不能被当成"机器上什么都没跑"——那会把一次 ssh 抖动升级成治理面失忆。
    """
    monkeypatch.setenv("FAKE_STATUS_EXIT", "255")  # 模拟 ssh 不可达
    store.set_metadata("active:staging-232", RELEASE_OLD)

    reconcile_active_release(config, store)  # 不得抛

    assert store.get_metadata("active:staging-232") == RELEASE_OLD
    events = [event["event_type"] for event in store.snapshot()["events"]]
    assert "active_probe_failed" in events
    assert "active_drift" not in events


def test_machine_reporting_no_current_is_drift_not_a_probe_failure(
    config: ControllerConfig,
    store: StateStore,
    release_root: Path,
) -> None:
    """机器明确回答"没有 current"，本地却记着在线版本：这是真漂移，必须告警。

    但**不清空本地记录**——机器可能正在部署中途，清空会把一次瞬时状态固化成失忆。
    这与"没问到"（ssh 抖动）是两码事：把两者混成一个 None，网络抖动就和
    "有人删了 current"长得一模一样了。
    """
    # release_root 存在但没有 current 符号链接
    store.set_metadata("active:staging-232", RELEASE_OLD)

    reconcile_active_release(config, store)

    events = [event["event_type"] for event in store.snapshot()["events"]]
    assert "active_drift" in events, "机器上没东西在跑，治理面却报着 B —— 这必须告警"
    assert "active_probe_failed" not in events, "这不是没问到，是问到了一个坏消息"
    assert store.get_metadata("active:staging-232") == RELEASE_OLD, (
        "不得清空：机器可能正在部署中途，留给人工裁决"
    )


def test_a_persistent_outage_does_not_flood_the_event_log(
    config: ControllerConfig,
    store: StateStore,
    release_root: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """目标机持续宕机时，同一个故障只报一次，之后静默重试。

    poll 每 30 秒一轮、events 无清理、status 只显示最近 50 条：若每轮记一条，
    宕机一小时就把 120 条同样的失败刷进去，把部署/回滚/漂移全挤出运维视野——
    恰恰在最需要 status 的时候。恢复时再报一条 active_probe_recovered。
    """
    monkeypatch.setenv("FAKE_STATUS_EXIT", "255")
    store.set_metadata("active:staging-232", RELEASE_OLD)

    for _ in range(5):  # 模拟连续 5 轮 poll
        reconcile_active_release(config, store)

    failures = [
        event
        for event in store.snapshot()["events"]
        if event["event_type"] == "active_probe_failed"
    ]
    assert len(failures) == 1, f"同一个故障被刷了 {len(failures)} 次，status 会被淹没"

    # 机器恢复：必须留一条恢复事件，且重新开始对账
    monkeypatch.delenv("FAKE_STATUS_EXIT")
    (release_root / "releases" / RELEASE_PREVIOUS).mkdir(parents=True)
    (release_root / "current").symlink_to(release_root / "releases" / RELEASE_PREVIOUS)

    reconcile_active_release(config, store)

    events = [event["event_type"] for event in store.snapshot()["events"]]
    assert "active_probe_recovered" in events
    assert store.get_metadata("active:staging-232") == RELEASE_PREVIOUS, "恢复后必须继续对账"


def test_no_probe_before_the_first_managed_release(
    config: ControllerConfig,
    store: StateStore,
    tmp_path: Path,
) -> None:
    """还没有任何受管发布时不探测——没有可对账的对象，探测只会每 30 秒刷一次屏。

    这正是控制器刚装好、机器上还没有 helper 的状态：不能一上来就把事件表写满。
    """
    probed = tmp_path / "probed"
    config.deploy_script.write_text(
        f"#!/usr/bin/env bash\ntouch {probed}\nexit 1\n", encoding="utf-8"
    )
    assert store.get_metadata("active:staging-232") is None

    reconcile_active_release(config, store)

    assert not probed.exists(), "没有 active 记录时不该去打扰目标机"
    assert store.snapshot()["events"] == []


def test_probe_failure_event_carries_the_diagnostic_detail(
    config: ControllerConfig,
    store: StateStore,
    release_root: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """探测失败的事件必须带上退出码/stderr，否则运维只看到一句"没问到"，无从下手。"""
    monkeypatch.setenv("FAKE_STATUS_EXIT", "255")
    store.set_metadata("active:staging-232", RELEASE_OLD)

    reconcile_active_release(config, store)

    detail = next(
        event["details"]
        for event in store.snapshot()["events"]
        if event["event_type"] == "active_probe_failed"
    )
    assert "255" in detail, f"事件里没有退出码，无法定位是哪种失败：{detail}"


def test_garbled_status_output_is_treated_as_unknown_not_as_empty(
    config: ControllerConfig,
    store: StateStore,
    tmp_path: Path,
) -> None:
    """脚本返回 0 但输出不是 JSON：同样按"没问到"处理，不得覆写本地记录。"""
    config.deploy_script.write_text(
        "#!/usr/bin/env bash\necho 'not json at all'\nexit 0\n", encoding="utf-8"
    )
    store.set_metadata("active:staging-232", RELEASE_OLD)

    reconcile_active_release(config, store)

    assert store.get_metadata("active:staging-232") == RELEASE_OLD
    events = [event["event_type"] for event in store.snapshot()["events"]]
    assert "active_probe_failed" in events


def test_remote_status_stdout_is_parseable_json_despite_deploy_logging(
    config: ControllerConfig,
    release_root: Path,
) -> None:
    """--remote-status 的 stdout 是契约：日志必须走 stderr，不得混进 JSON。"""
    (release_root / "releases" / RELEASE_OLD).mkdir(parents=True)
    (release_root / "current").symlink_to(release_root / "releases" / RELEASE_OLD)

    from agent_gov_release_reconcile import remote_active_release

    assert remote_active_release(config) == RELEASE_OLD

    # 直接证明 stdout 可被 json.loads 整体解析（而不是"最后一行碰巧是 JSON"）
    completed = subprocess.run(
        [str(config.deploy_script), "--remote-status"],
        check=True,
        capture_output=True,
        text=True,
    )
    assert json.loads(completed.stdout)["active_release"] == RELEASE_OLD


def test_the_probe_does_not_hand_the_github_credential_to_the_deploy_script(
    config: ControllerConfig,
    store: StateStore,
    release_root: Path,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """对账器每 30 秒 fork 一次部署脚本 —— 它绝不能顺手把 PAT 带进子进程。

    控制器手里的 GitHub PAT 由 systemd LoadCredential 注入；剥离逻辑必须复用
    sanitized_environment，而不是各处自己拼环境变量（新增一个 fork 点就是新增一个
    漏掉剥离的机会）。
    """
    dump = tmp_path / "child_env.json"
    config.deploy_script.write_text(
        "#!/usr/bin/env bash\n"
        f'python3 -c "import os,json;json.dump(dict(os.environ),open(\'{dump}\',\'w\'))"\n'
        'echo \'{"active_release": null, "releases": []}\'\n',
        encoding="utf-8",
    )
    monkeypatch.setenv("GITHUB_TOKEN", "ghp_must_not_reach_the_deploy_script")
    monkeypatch.setenv("GH_TOKEN", "ghp_must_not_reach_either")
    monkeypatch.setenv("CREDENTIALS_DIRECTORY", "/run/credentials/agent-gov")
    store.set_metadata("active:staging-232", RELEASE_OLD)  # 有可对账的对象，才会探测

    reconcile_active_release(config, store)

    child_env = json.loads(dump.read_text(encoding="utf-8"))
    for leaked in ("GITHUB_TOKEN", "GH_TOKEN", "CREDENTIALS_DIRECTORY"):
        assert leaked not in child_env, f"{leaked} 被带进了部署脚本子进程"


def test_the_real_deploy_script_reports_the_real_current_symlink(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """**真正执行**仓库里那个 deploy 脚本的 --remote-status（不是假脚本、不是 grep 源码）。

    上面的用例用假脚本验的是控制器侧的解析契约；这条验的是**真脚本自己**：
    --host localhost 走 LOCAL_TARGET 分支，真的解析 REMOTE_DIR、真的调用真 helper、
    真的读一个真的 current 符号链接。

    这一层此前只有 `bash -n`（查语法）和 grep 源码文本 —— 往脚本里加一句 `|| true`
    都不会红。remote-status 是本批新增的路径，不能再靠那种"测试"上线。
    """
    remote_dir = tmp_path / "machine"
    (remote_dir / "releases" / RELEASE_OLD).mkdir(parents=True)
    (remote_dir / "current").symlink_to(remote_dir / "releases" / RELEASE_OLD)

    # helper 由部署安装到 shared/bin；这里照实摆放真 helper，而不是同步一份
    helper_dir = remote_dir / "shared" / "bin"
    helper_dir.mkdir(parents=True)
    helper = helper_dir / "agent-gov-release-remote"
    helper.write_bytes((SCRIPTS_DIR / "agent_gov_release_remote").read_bytes())
    helper.chmod(0o755)

    monkeypatch.setenv("REMOTE_DIR", str(remote_dir))
    monkeypatch.setenv("AGENT_GOV_SOURCE_REPO_DIR", str(tmp_path / "not-the-repo"))

    completed = subprocess.run(
        [
            str(SCRIPTS_DIR / "deploy_agent_gov_to_host"),
            "--remote-status",
            "--host",
            "localhost",
            "--environment",
            "staging-232",
        ],
        check=False,
        capture_output=True,
        text=True,
    )

    assert completed.returncode == 0, f"stderr={completed.stderr}"
    payload = json.loads(completed.stdout)
    assert payload["active_release"] == RELEASE_OLD


def test_the_real_deploy_script_reports_null_when_the_machine_has_no_current(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """真脚本 + 没有 current：必须明确回答 null，而不是报错或输出空。

    这是"机器上什么都没跑"与"没问到"的分界线，对账器靠它区分告警与静默重试。
    """
    remote_dir = tmp_path / "machine"
    (remote_dir / "releases").mkdir(parents=True)
    helper_dir = remote_dir / "shared" / "bin"
    helper_dir.mkdir(parents=True)
    helper = helper_dir / "agent-gov-release-remote"
    helper.write_bytes((SCRIPTS_DIR / "agent_gov_release_remote").read_bytes())
    helper.chmod(0o755)

    monkeypatch.setenv("REMOTE_DIR", str(remote_dir))
    monkeypatch.setenv("AGENT_GOV_SOURCE_REPO_DIR", str(tmp_path / "not-the-repo"))

    completed = subprocess.run(
        [
            str(SCRIPTS_DIR / "deploy_agent_gov_to_host"),
            "--remote-status",
            "--host",
            "localhost",
        ],
        check=False,
        capture_output=True,
        text=True,
    )

    assert completed.returncode == 0, f"stderr={completed.stderr}"
    assert json.loads(completed.stdout)["active_release"] is None
