"""以目标机为准的「当前版本」对账器。

存在理由：控制器手里那个 `active:<环境>` 指针，此前**从来没跟机器上的真实情况核对过**。
远端脚本早就实现了 `status` 子命令（能读机器上的 current 符号链接、报告真实在跑的版本），
但全仓库无人调用——是死代码。于是治理面说 B、机器上跑 A 时，没有任何东西会发现。

这跟 #32「durable intent 建好了但没有对账器」是同一个毛病：**写了意图，没写收敛**。

对账原则：
- **以机器上的 current 为准**。控制器的记录是投影，机器上跑的才是事实。
- **fail-soft**：目标机连不上、输出解析不了，都只记事件、不打断 poll。发布链的存活
  不能取决于一次 ssh 抖动（这正是批 2 里 TransportError 要分流的同一个教训）。
"""

from __future__ import annotations

import json
import subprocess

from agent_gov_release_state import (
    ControllerConfig,
    ControllerError,
    StateStore,
    sanitized_environment,
)

_REMOTE_STATUS_TIMEOUT_SECONDS = 60
_STDERR_EXCERPT_CHARS = 400


class ProbeFailed(ControllerError):
    """没问到机器。**与「问到了，机器上什么都没跑」是两回事**：

    前者不足以推翻本地记录（一次 ssh 抖动不能让治理面失忆），后者是一条真实的告警。
    把两者混成一个"返回 None"，就等于让网络抖动和"有人删了 current"长得一模一样。
    """


def remote_active_release(config: ControllerConfig) -> str | None:
    """问目标机：你现在实际跑的是哪个 release？

    返回 release id；机器**明确回答**"没有 current"时返回 None。
    问不到（不可达 / 非零退出 / 输出不可解析）抛 ProbeFailed，并带上 stderr 摘要——
    否则运维只会看到一条"没问到"，无从下手。
    """
    command = [
        str(config.deploy_script),
        "--remote-status",
        "--host",
        config.deploy_host,
        "--environment",
        config.environment,
    ]
    completed = subprocess.run(
        command,
        check=False,
        capture_output=True,
        text=True,
        timeout=_REMOTE_STATUS_TIMEOUT_SECONDS,
        env=sanitized_environment(config),
    )
    if completed.returncode != 0:
        excerpt = completed.stderr.strip()[-_STDERR_EXCERPT_CHARS:]
        raise ProbeFailed(f"remote status exited {completed.returncode}: {excerpt}")
    try:
        payload = json.loads(completed.stdout)
    except json.JSONDecodeError as exc:
        raise ProbeFailed(f"remote status stdout is not JSON: {exc}") from exc
    if not isinstance(payload, dict):
        raise ProbeFailed("remote status payload has an unexpected shape")
    active = payload.get("active_release")
    if active is None:
        return None
    if not isinstance(active, str) or not active:
        raise ProbeFailed(f"remote status reported an invalid active_release: {active!r}")
    return active


def _note_probe_failure(config: ControllerConfig, store: StateStore, detail: str) -> None:
    """同一个故障只报一次。

    poll 每 30 秒一轮，而 events 表没有清理、`releasectl status` 只显示最近 50 条。
    若每轮都记一条，目标机宕机一小时就会把 120 条同样的失败刷进去，把部署、回滚、
    漂移等真正要看的事件**全部挤出运维视野**——恰恰在最需要 status 的时候。
    """
    signature_key = f"probe_failure:{config.environment}"
    if store.get_metadata(signature_key) == detail:
        return
    store.set_metadata(signature_key, detail)
    store.add_event("active_probe_failed", detail)


def _note_probe_recovered(config: ControllerConfig, store: StateStore) -> None:
    signature_key = f"probe_failure:{config.environment}"
    if store.get_metadata(signature_key):
        store.set_metadata(signature_key, "")
        store.add_event("active_probe_recovered", "target host is reachable again")


def reconcile_active_release(config: ControllerConfig, store: StateStore) -> None:
    """把本地记的「当前版本」跟目标机对一下；不一致就告警并以机器为准回填。"""
    active_key = f"active:{config.environment}"
    recorded = store.get_metadata(active_key)
    if not recorded:
        # 还没有任何受管发布（首次安装、或第一次部署之前）：没有可对账的对象。
        # 此时探测只会每 30 秒失败一次并刷屏——机器上本来就还没有 helper。
        # active 会由首次成功部署或 reconcile_head 建立起来。
        return
    try:
        actual = remote_active_release(config)
    except (ProbeFailed, OSError, subprocess.SubprocessError) as exc:
        _note_probe_failure(config, store, f"{type(exc).__name__}: {exc}")
        return
    _note_probe_recovered(config, store)
    if actual is None:
        # 机器上没有 current，本地却记着一个在线版本：这是真漂移，必须告警。
        # 但**不清空记录**——机器可能正处在部署中途，清空只会把一次瞬时状态
        # 固化成失忆。留给人工裁决。
        store.add_event(
            "active_drift",
            f"recorded={recorded} but the target host reports no current release; "
            "keeping the record for manual review",
        )
        return
    if actual == recorded:
        return
    store.add_event(
        "active_drift",
        f"recorded={recorded} actual={actual}; reconciled to the target host",
    )
    store.set_metadata(active_key, actual)
