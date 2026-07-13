from __future__ import annotations

import ast
import json
import os
import stat
from contextlib import suppress
from dataclasses import dataclass
from pathlib import Path, PurePosixPath
from uuid import uuid4

# 创建业务 Agent 时基于的模板 catalog（docker/runtime-volume-seeds/templates/business-agent/<template_id>/）。
# 默认按模块相对路径解析，容器内为 /app/docker/...（镜像 COPY），本机调试为 <repo>/docker/...；
# 可经 BUSINESS_AGENT_TEMPLATES_DIR 覆盖。
DEFAULT_TEMPLATE_ID = "general"
_TEMPLATES_DIR_DEFAULT = Path(__file__).resolve().parents[2] / "docker" / "runtime-volume-seeds" / "templates" / "business-agent"
# 渲染占位符：模板文本里的 {{AGENT_ID}} / {{AGENT_NAME}} 被替换为具体值（双花括号不与 JSON 冲突）。
_PLACEHOLDER_AGENT_ID = "{{AGENT_ID}}"
_PLACEHOLDER_AGENT_NAME = "{{AGENT_NAME}}"
_DIRECTORY_OPEN_FLAGS = os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW | getattr(os, "O_CLOEXEC", 0)

# general 模板缺失时的内联兜底（保证无种子目录的纯单测环境也能初始化）。
_STARTER_CLAUDE_MD = """# {name}

本工作区是 AgentGov 注册的业务 Agent `{agent_id}`（被治理对象）。

在此定义该 Agent 的角色、system prompt、技能与工具边界、行为约束；AgentGov 负责
其运行、反馈归因、评估和版本治理。高风险动作须经外部系统或授权用户确认。
"""

# 业务 Agent 是被治理对象：起始权限保守，默认只读自身工作区；Bash 由 sandbox、hook
# 与 deny 兜底直接执行，不走后端 HITL；写入工作区仍需确认。运行时治理根隔离由
# build_business_agent_profile 在 profile 层另行拒绝。
_STARTER_SETTINGS: dict = {
    "$schema": "https://json.schemastore.org/claude-code-settings.json",
    "permissions": {
        "defaultMode": "default",
        "disableBypassPermissionsMode": "disable",
        "allow": ["Read(./**)", "Glob", "Grep", "Skill", "Bash(*)"],
        "ask": ["Edit(./**)", "Write(./**)"],
        "deny": ["Read(./.env)", "Read(./.env.*)", "Read(./secrets/**)"],
    },
}

# 起始 MCP 配置为空：不预置任何 server，更不预置 header/凭据；由用户按需添加。
_STARTER_MCP: dict = {"mcpServers": {}}


class UnknownBusinessAgentTemplate(ValueError):
    """请求的 template_id 不在 catalog 中（外部输入越权/拼写错误）。"""


class WorkspaceSafetyError(RuntimeError):
    """Template or workspace violates the no-follow provisioning boundary."""


class WorkspaceProvisioningError(RuntimeError):
    """Workspace apply failed; ``cleanup_complete`` controls DB compensation."""

    def __init__(self, message: str, *, cleanup_complete: bool) -> None:
        super().__init__(message)
        self.cleanup_complete = cleanup_complete


@dataclass(frozen=True)
class WorkspaceTemplateEntry:
    relative_path: PurePosixPath
    content: bytes
    mode: int


@dataclass(frozen=True)
class WorkspaceTemplatePlan:
    template_id: str
    entries: tuple[WorkspaceTemplateEntry, ...]


@dataclass(frozen=True)
class _CreatedPath:
    path: Path
    device: int
    inode: int


@dataclass(frozen=True)
class WorkspaceProvisionJournal:
    created_files: tuple[_CreatedPath, ...]
    created_directories: tuple[_CreatedPath, ...]


def business_agent_templates_dir() -> Path:
    """模板 catalog 根目录（env 覆盖优先）。"""
    override = os.environ.get("BUSINESS_AGENT_TEMPLATES_DIR")
    return Path(override) if override else _TEMPLATES_DIR_DEFAULT


def list_business_agent_templates() -> list[str]:
    """列出可用 template_id（按名排序）；catalog 目录缺失时回退到内置 general。"""
    root = business_agent_templates_dir()
    root_stat = _lstat(root)
    if root_stat is None:
        return [DEFAULT_TEMPLATE_ID]
    if stat.S_ISLNK(root_stat.st_mode) or not stat.S_ISDIR(root_stat.st_mode):
        raise WorkspaceSafetyError("Business Agent template catalog must be a real directory")
    ids: list[str] = []
    with os.scandir(root) as entries:
        for entry in entries:
            relative_path = PurePosixPath(entry.name)
            _validate_relative_path(relative_path)
            if _is_cache_artifact(relative_path):
                raise WorkspaceSafetyError(f"Business Agent template catalog contains a cache artifact: {entry.name}")
            if entry.is_symlink():
                raise WorkspaceSafetyError(f"Business Agent template catalog contains a symlink: {entry.name}")
            if entry.is_dir(follow_symlinks=False):
                ids.append(entry.name)
    ids.sort()
    return ids or [DEFAULT_TEMPLATE_ID]


def _render(text: str, *, agent_id: str, name: str) -> str:
    return text.replace(_PLACEHOLDER_AGENT_ID, agent_id).replace(_PLACEHOLDER_AGENT_NAME, name)


def prepare_business_agent_workspace(
    *,
    agent_id: str,
    name: str,
    template_id: str = DEFAULT_TEMPLATE_ID,
) -> WorkspaceTemplatePlan:
    """Read, render and validate the whole template before any DB/FS mutation."""
    normalized = (template_id or DEFAULT_TEMPLATE_ID).strip() or DEFAULT_TEMPLATE_ID
    _validate_template_id(normalized)
    root = business_agent_templates_dir()
    root_stat = _lstat(root)
    if root_stat is not None and (stat.S_ISLNK(root_stat.st_mode) or not stat.S_ISDIR(root_stat.st_mode)):
        raise WorkspaceSafetyError("Business Agent template catalog must be a real directory")
    template_path = root / normalized
    template_stat = _lstat(template_path)
    if template_stat is None:
        if normalized == DEFAULT_TEMPLATE_ID and _lstat(root) is None:
            return _inline_plan(agent_id=agent_id, name=name)
        raise UnknownBusinessAgentTemplate(f"Unknown business agent template: {normalized!r}; available: {list_business_agent_templates()}")
    if stat.S_ISLNK(template_stat.st_mode) or not stat.S_ISDIR(template_stat.st_mode):
        raise WorkspaceSafetyError(f"Business Agent template must be a real directory: {normalized}")
    entries = tuple(_read_template_tree(template_path, agent_id=agent_id, name=name))
    if not entries:
        raise WorkspaceSafetyError(f"Business Agent template is empty: {normalized}")
    return WorkspaceTemplatePlan(template_id=normalized, entries=entries)


def apply_business_agent_workspace_plan(
    workspace_dir: Path,
    plan: WorkspaceTemplatePlan,
    *,
    require_workspace_absent: bool = False,
) -> WorkspaceProvisionJournal:
    """Publish atomically from a no-follow workspace root; recovered roots must be absent."""
    created_files: list[_CreatedPath] = []
    created_directories: list[_CreatedPath] = []
    workspace_descriptor: int | None = None
    try:
        workspace_descriptor, workspace_path = _open_workspace_root(
            workspace_dir,
            created_directories,
            require_new=require_workspace_absent,
        )
        for entry in plan.entries:
            created = _publish_entry(
                workspace_descriptor,
                workspace_path,
                entry,
                created_directories,
                reject_existing=require_workspace_absent,
            )
            if created is not None:
                created_files.append(created)
    except Exception as exc:
        if workspace_descriptor is not None:
            os.close(workspace_descriptor)
            workspace_descriptor = None
        journal = WorkspaceProvisionJournal(tuple(created_files), tuple(created_directories))
        local_cleanup_complete = not isinstance(exc, WorkspaceProvisioningError) or exc.cleanup_complete
        cleanup_complete = rollback_business_agent_workspace(journal) and local_cleanup_complete
        raise WorkspaceProvisioningError(
            f"Business Agent workspace provisioning failed: {exc.__class__.__name__}",
            cleanup_complete=cleanup_complete,
        ) from exc
    finally:
        if workspace_descriptor is not None:
            os.close(workspace_descriptor)
    return WorkspaceProvisionJournal(tuple(created_files), tuple(created_directories))


def rollback_business_agent_workspace(journal: WorkspaceProvisionJournal) -> bool:
    """Remove only paths whose inode is still owned by this provisioning attempt."""
    complete = True
    for created in reversed(journal.created_files):
        complete = _unlink_owned_file(created) and complete
    for created in reversed(journal.created_directories):
        complete = _remove_owned_directory(created) and complete
    return complete


def seed_business_agent_workspace(
    workspace_dir: Path,
    *,
    agent_id: str,
    name: str,
    template_id: str = DEFAULT_TEMPLATE_ID,
) -> str:
    """从 catalog 模板幂等播种业务 Agent workspace，渲染 {{AGENT_ID}}/{{AGENT_NAME}} 占位。

    - 未知 template_id 抛 UnknownBusinessAgentTemplate（由路由投影为 422）。
    - 已存在的文件不覆盖（保留用户编辑），FS 副作用幂等。
    - 模板内不含任何 api_key / MCP header / 本机私有路径。
    返回实际使用的 template_id。
    """
    plan = prepare_business_agent_workspace(agent_id=agent_id, name=name, template_id=template_id)
    apply_business_agent_workspace_plan(workspace_dir, plan)
    return plan.template_id


def initialize_business_agent_workspace(workspace_dir: Path, *, agent_id: str, name: str) -> None:
    """向后兼容入口：以默认 general 模板幂等初始化业务 Agent 工作区配置容器。"""
    seed_business_agent_workspace(workspace_dir, agent_id=agent_id, name=name, template_id=DEFAULT_TEMPLATE_ID)


def _inline_plan(*, agent_id: str, name: str) -> WorkspaceTemplatePlan:
    values = {
        PurePosixPath("CLAUDE.md"): _STARTER_CLAUDE_MD.format(name=name, agent_id=agent_id),
        PurePosixPath(".claude/settings.json"): json.dumps(_STARTER_SETTINGS, ensure_ascii=False, indent=2) + "\n",
        PurePosixPath(".mcp.json"): json.dumps(_STARTER_MCP, ensure_ascii=False, indent=2) + "\n",
    }
    entries = tuple(_validated_entry(relative_path, content, 0o644) for relative_path, content in values.items())
    return WorkspaceTemplatePlan(template_id=DEFAULT_TEMPLATE_ID, entries=entries)


def _read_template_tree(root: Path, *, agent_id: str, name: str) -> list[WorkspaceTemplateEntry]:
    rendered: list[WorkspaceTemplateEntry] = []
    for current, directories, files in os.walk(root, topdown=True, followlinks=False):
        current_path = Path(current)
        current_stat = os.lstat(current_path)
        if stat.S_ISLNK(current_stat.st_mode) or not stat.S_ISDIR(current_stat.st_mode):
            raise WorkspaceSafetyError("Business Agent template directory changed during validation")
        directories.sort()
        files.sort()
        for name_on_disk in (*directories, *files):
            candidate = current_path / name_on_disk
            candidate_stat = os.lstat(candidate)
            relative_path = PurePosixPath(candidate.relative_to(root).as_posix())
            _validate_relative_path(relative_path)
            if _is_cache_artifact(relative_path):
                raise WorkspaceSafetyError(f"Business Agent template contains a cache artifact: {relative_path}")
            if stat.S_ISLNK(candidate_stat.st_mode):
                raise WorkspaceSafetyError(f"Business Agent template contains a symlink: {relative_path}")
        for filename in files:
            relative_path = PurePosixPath((current_path / filename).relative_to(root).as_posix())
            if relative_path.name == "README.md":
                continue
            source = current_path / filename
            source_stat, text = _read_regular_text_no_follow(source)
            content = _render(text, agent_id=agent_id, name=name)
            rendered.append(_validated_entry(relative_path, content, stat.S_IMODE(source_stat.st_mode)))
    rendered.sort(key=lambda entry: entry.relative_path.as_posix())
    return rendered


def _validated_entry(relative_path: PurePosixPath, content: str, mode: int) -> WorkspaceTemplateEntry:
    _validate_relative_path(relative_path)
    if relative_path.suffix == ".json":
        try:
            value = json.loads(content)
        except json.JSONDecodeError as exc:
            raise WorkspaceSafetyError(f"Rendered template JSON is invalid: {relative_path}") from exc
        if not isinstance(value, dict):
            raise WorkspaceSafetyError(f"Rendered template JSON must be an object: {relative_path}")
    if relative_path.suffix == ".py":
        try:
            ast.parse(content, filename=relative_path.as_posix())
        except SyntaxError as exc:
            raise WorkspaceSafetyError(f"Rendered template Python is invalid: {relative_path}") from exc
    return WorkspaceTemplateEntry(
        relative_path=relative_path,
        content=content.encode("utf-8"),
        mode=0o755 if mode & 0o111 else 0o644,
    )


def _read_regular_text_no_follow(path: Path) -> tuple[os.stat_result, str]:
    flags = os.O_RDONLY | os.O_NOFOLLOW
    descriptor = os.open(path, flags)
    try:
        source_stat = os.fstat(descriptor)
        if not stat.S_ISREG(source_stat.st_mode):
            raise WorkspaceSafetyError(f"Business Agent template entry is not a regular file: {path.name}")
        with os.fdopen(descriptor, "rb", closefd=False) as source:
            raw = source.read()
    finally:
        os.close(descriptor)
    try:
        return source_stat, raw.decode("utf-8")
    except UnicodeDecodeError as exc:
        raise WorkspaceSafetyError(f"Business Agent template entry is not UTF-8 text: {path.name}") from exc


def _publish_entry(
    workspace_descriptor: int,
    workspace_path: Path,
    entry: WorkspaceTemplateEntry,
    created_directories: list[_CreatedPath],
    *,
    reject_existing: bool = False,
) -> _CreatedPath | None:
    destination = workspace_path.joinpath(*entry.relative_path.parts)
    parent_descriptor = _open_workspace_relative_directory(
        workspace_descriptor,
        workspace_path,
        entry.relative_path.parts[:-1],
        created_directories,
    )
    temporary_name = f".agentgov-provision-{uuid4().hex}.tmp"
    temporary_stat: os.stat_result | None = None
    published: _CreatedPath | None = None
    published_stat: os.stat_result | None = None
    try:
        destination_name = entry.relative_path.name
        existing = _stat_at(parent_descriptor, destination_name)
        if existing is not None:
            _require_regular_destination(existing, entry.relative_path)
            if reject_existing:
                raise WorkspaceSafetyError(f"Recovered workspace changed during provisioning: {entry.relative_path}")
            return None
        temporary_stat = _write_temporary(parent_descriptor, temporary_name, entry.content, entry.mode)
        try:
            os.link(
                temporary_name,
                destination_name,
                src_dir_fd=parent_descriptor,
                dst_dir_fd=parent_descriptor,
                follow_symlinks=False,
            )
        except FileExistsError as race_error:
            raced = _stat_at(parent_descriptor, destination_name)
            if raced is None:
                raise WorkspaceSafetyError(f"Workspace destination disappeared during publish: {entry.relative_path}") from race_error
            _require_regular_destination(raced, entry.relative_path)
            if not _unlink_owned_at(parent_descriptor, temporary_name, temporary_stat):
                raise WorkspaceSafetyError("Workspace temporary file cleanup failed") from race_error
            temporary_stat = None
            if reject_existing:
                raise WorkspaceSafetyError(f"Recovered workspace changed during provisioning: {entry.relative_path}") from race_error
            return None
        published = _CreatedPath(destination, temporary_stat.st_dev, temporary_stat.st_ino)
        published_stat = temporary_stat
        if not _unlink_owned_at(parent_descriptor, temporary_name, temporary_stat):
            raise WorkspaceSafetyError("Workspace temporary file cleanup failed")
        temporary_stat = None
        os.fsync(parent_descriptor)
        return published
    except Exception as exc:
        cleanup_complete = not isinstance(exc, WorkspaceProvisioningError) or exc.cleanup_complete
        if temporary_stat is not None:
            cleanup_complete = _unlink_owned_at(parent_descriptor, temporary_name, temporary_stat) and cleanup_complete
        if published is not None and published_stat is not None:
            cleanup_complete = _unlink_owned_at(parent_descriptor, entry.relative_path.name, published_stat) and cleanup_complete
        raise WorkspaceProvisioningError(
            f"Workspace file publish failed: {entry.relative_path}",
            cleanup_complete=cleanup_complete,
        ) from exc
    finally:
        with suppress(OSError):
            os.close(parent_descriptor)


def _write_temporary(parent_descriptor: int, name: str, content: bytes, mode: int) -> os.stat_result:
    flags = os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW
    descriptor = os.open(name, flags, mode or 0o600, dir_fd=parent_descriptor)
    temporary_stat = os.fstat(descriptor)
    try:
        view = memoryview(content)
        while view:
            written = os.write(descriptor, view)
            view = view[written:]
        os.fchmod(descriptor, mode or 0o600)
        os.fsync(descriptor)
        return temporary_stat
    except Exception as exc:
        cleanup_complete = _unlink_owned_at(parent_descriptor, name, temporary_stat)
        raise WorkspaceProvisioningError(
            "Workspace temporary file write failed",
            cleanup_complete=cleanup_complete,
        ) from exc
    finally:
        with suppress(OSError):
            os.close(descriptor)


def _open_workspace_root(
    workspace_dir: Path,
    created_directories: list[_CreatedPath],
    *,
    require_new: bool = False,
) -> tuple[int, Path]:
    workspace_path = _absolute_path(workspace_dir)
    descriptor = os.open(workspace_path.anchor, _DIRECTORY_OPEN_FLAGS)
    current_path = Path(workspace_path.anchor)
    try:
        relative_parts = workspace_path.parts[1:]
        if not relative_parts and require_new:
            raise WorkspaceSafetyError("Recovered Business Agent workspace must be absent before retry")
        for position, component in enumerate(relative_parts):
            current_path /= component
            child_descriptor, created = _open_or_create_directory_at(
                descriptor,
                component,
                current_path,
                created_directories,
            )
            if position == len(relative_parts) - 1 and require_new and not created:
                os.close(child_descriptor)
                raise WorkspaceSafetyError("Recovered Business Agent workspace must be absent before retry")
            os.close(descriptor)
            descriptor = child_descriptor
        return descriptor, workspace_path
    except Exception:
        os.close(descriptor)
        raise


def _open_workspace_relative_directory(
    workspace_descriptor: int,
    workspace_path: Path,
    relative_parts: tuple[str, ...],
    created_directories: list[_CreatedPath],
) -> int:
    descriptor = os.dup(workspace_descriptor)
    current_path = workspace_path
    try:
        for component in relative_parts:
            current_path /= component
            child_descriptor, _ = _open_or_create_directory_at(
                descriptor,
                component,
                current_path,
                created_directories,
            )
            os.close(descriptor)
            descriptor = child_descriptor
        return descriptor
    except Exception:
        os.close(descriptor)
        raise


def _open_or_create_directory_at(
    parent_descriptor: int,
    name: str,
    path: Path,
    created_directories: list[_CreatedPath],
) -> tuple[int, bool]:
    try:
        return os.open(name, _DIRECTORY_OPEN_FLAGS, dir_fd=parent_descriptor), False
    except FileNotFoundError:
        pass
    except OSError as exc:
        raise WorkspaceSafetyError(f"Workspace path component is not a real directory: {name}") from exc

    try:
        os.mkdir(name, 0o750, dir_fd=parent_descriptor)
    except FileExistsError:
        try:
            return os.open(name, _DIRECTORY_OPEN_FLAGS, dir_fd=parent_descriptor), False
        except OSError as exc:
            raise WorkspaceSafetyError(f"Workspace path component changed during create: {name}") from exc

    created = _stat_at(parent_descriptor, name)
    if created is None or not stat.S_ISDIR(created.st_mode):
        raise WorkspaceSafetyError(f"Workspace path component changed during create: {name}")
    owned = _CreatedPath(path, created.st_dev, created.st_ino)
    created_directories.append(owned)
    os.fsync(parent_descriptor)
    try:
        descriptor = os.open(name, _DIRECTORY_OPEN_FLAGS, dir_fd=parent_descriptor)
    except OSError as exc:
        raise WorkspaceSafetyError(f"Workspace path component changed during create: {name}") from exc
    opened = os.fstat(descriptor)
    if (opened.st_dev, opened.st_ino) != (owned.device, owned.inode):
        os.close(descriptor)
        raise WorkspaceSafetyError(f"Workspace path component changed during create: {name}")
    return descriptor, True


def _absolute_path(path: Path) -> Path:
    return Path(os.path.abspath(path))


def _validate_template_id(template_id: str) -> None:
    relative = PurePosixPath(template_id)
    if relative.is_absolute() or len(relative.parts) != 1 or template_id in {".", ".."}:
        raise UnknownBusinessAgentTemplate(f"Invalid business agent template id: {template_id!r}")


def _validate_relative_path(relative_path: PurePosixPath) -> None:
    if relative_path.is_absolute() or not relative_path.parts or any(part in {"", ".", ".."} for part in relative_path.parts):
        raise WorkspaceSafetyError(f"Unsafe Business Agent template path: {relative_path}")


def _is_cache_artifact(relative_path: PurePosixPath) -> bool:
    return "__pycache__" in relative_path.parts or relative_path.suffix.lower() in {".pyc", ".pyo"}


def _lstat(path: Path) -> os.stat_result | None:
    try:
        return os.lstat(path)
    except FileNotFoundError:
        return None


def _stat_at(parent_descriptor: int, name: str) -> os.stat_result | None:
    try:
        return os.stat(name, dir_fd=parent_descriptor, follow_symlinks=False)
    except FileNotFoundError:
        return None


def _require_regular_destination(destination_stat: os.stat_result, relative_path: PurePosixPath) -> None:
    if not stat.S_ISREG(destination_stat.st_mode):
        raise WorkspaceSafetyError(f"Workspace destination is not a regular file: {relative_path}")


def _unlink_owned_at(parent_descriptor: int, name: str, owned: os.stat_result) -> bool:
    current = _stat_at(parent_descriptor, name)
    if current is None:
        return True
    if not stat.S_ISREG(current.st_mode) or (current.st_dev, current.st_ino) != (owned.st_dev, owned.st_ino):
        return False
    try:
        os.unlink(name, dir_fd=parent_descriptor)
    except OSError:
        return False
    return True


def _unlink_owned_file(created: _CreatedPath) -> bool:
    try:
        parent_descriptor = _open_existing_directory(created.path.parent)
    except FileNotFoundError:
        return True
    except OSError:
        return False
    try:
        current = _stat_at(parent_descriptor, created.path.name)
        if current is None:
            return True
        if not stat.S_ISREG(current.st_mode) or (current.st_dev, current.st_ino) != (created.device, created.inode):
            return False
        os.unlink(created.path.name, dir_fd=parent_descriptor)
        os.fsync(parent_descriptor)
        return True
    except OSError:
        return False
    finally:
        os.close(parent_descriptor)


def _remove_owned_directory(created: _CreatedPath) -> bool:
    try:
        parent_descriptor = _open_existing_directory(created.path.parent)
    except FileNotFoundError:
        return True
    except OSError:
        return False
    try:
        current = _stat_at(parent_descriptor, created.path.name)
        if current is None:
            return True
        if not stat.S_ISDIR(current.st_mode) or (current.st_dev, current.st_ino) != (created.device, created.inode):
            return False
        os.rmdir(created.path.name, dir_fd=parent_descriptor)
        os.fsync(parent_descriptor)
        return True
    except OSError:
        return False
    finally:
        os.close(parent_descriptor)


def _open_existing_directory(path: Path) -> int:
    absolute = _absolute_path(path)
    descriptor = os.open(absolute.anchor, _DIRECTORY_OPEN_FLAGS)
    try:
        for component in absolute.parts[1:]:
            child_descriptor = os.open(component, _DIRECTORY_OPEN_FLAGS, dir_fd=descriptor)
            os.close(descriptor)
            descriptor = child_descriptor
        return descriptor
    except Exception:
        os.close(descriptor)
        raise
