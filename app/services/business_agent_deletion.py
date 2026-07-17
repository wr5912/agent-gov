"""业务 Agent 删除：注册表 tombstone + 磁盘清理 + 运行态 seed 移除。

删除的三条边界，各有其理由：

- **受保护 Agent 拒删**：`PROTECTED_BUSINESS_AGENT_IDS` 里的 Agent 真相源在仓库，删除必须经
  受保护 PR。保护不看 `origin`——origin 是随 catalog 内容漂移的派生投影，把权限挂在它上面
  会让保护也跟着漂。
- **删除前必须无活跃 turn / 无未终结 change set**：与导入/恢复共用同一把维护租约，因此二者
  天然互斥；否则会删掉正在被使用的 workspace。
- **rmtree 不在事务块内**：事务内只 tombstone 并标记清理待完成，提交后才动磁盘。磁盘删除
  不可回滚，放进事务意味着事务回滚后磁盘已经回不去了。

崩溃安全：tombstone 先落库，磁盘清理若中断，注册表仍是 tombstone（不可见、重启不复活），
且 `create_business_agent` 会拒绝复用带未完成清理标记的 id，直到恢复器收口。
"""

from __future__ import annotations

import shutil
from dataclasses import dataclass
from pathlib import Path

from app.runtime.agent_paths import InvalidAgentId, business_agent_layout, validate_agent_id
from app.runtime.business_agent_seed_catalog import (
    declared_business_agent_workspace,
    runtime_seed_catalog_dir,
    seed_deletion_marker_path,
)


class BusinessAgentDeletionError(RuntimeError):
    """带 HTTP 状态与错误码的删除失败。"""

    def __init__(self, status_code: int, code: str, detail: str) -> None:
        super().__init__(detail)
        self.status_code = status_code
        self.code = code
        self.detail = detail


def _remove_tree(path: Path) -> bool:
    """删除一个真实目录，返回是否已确认删除。

    symlink 一律不跟随、不删除——删除的目标是这个 Agent 自己的目录，跟随 symlink 会把删除
    放大到目录之外。
    """

    if path.is_symlink():
        return False
    if not path.exists():
        return True
    if not path.is_dir():
        return False
    shutil.rmtree(path, ignore_errors=True)
    return not path.exists()


@dataclass(frozen=True)
class BusinessAgentPurgeResult:
    """删除清理的结果证据。

    部分失败必须可见：注册表已 tombstone 但磁盘有残留时，调用方要能如实回报，而不是把它
    当成删干净了。
    """

    workspace_removed: bool
    seed_removed: bool

    @property
    def cleanup_complete(self) -> bool:
        return self.workspace_removed and self.seed_removed


def purge_business_agent_storage(*, data_dir: Path, agent_id: str) -> BusinessAgentPurgeResult:
    """删除该 Agent 的全部运行态存储与运行态 seed 条目。"""

    try:
        safe_id = validate_agent_id(agent_id)
    except InvalidAgentId as exc:
        raise BusinessAgentDeletionError(422, "INVALID_AGENT_ID", str(exc)) from exc

    layout = business_agent_layout(data_dir, safe_id)
    workspace_removed = _remove_tree(layout.root)

    catalog_root = runtime_seed_catalog_dir(data_dir)
    catalog_entry = declared_business_agent_workspace(safe_id, seed_root=catalog_root).parent
    seed_removed = _remove_tree(catalog_entry)
    if seed_removed:
        # 标记必须在条目移除后写：仓库出生配置仍声明着这个 seed，没有标记时下次 bootstrap
        # 会照仓库把它填回 catalog，删除就不粘。
        marker = seed_deletion_marker_path(safe_id, seed_root=catalog_root)
        marker.parent.mkdir(parents=True, exist_ok=True)
        marker.write_text("", encoding="utf-8")

    return BusinessAgentPurgeResult(workspace_removed=workspace_removed, seed_removed=seed_removed)
