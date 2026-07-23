from __future__ import annotations

from pathlib import PurePosixPath

import yaml
from yaml.nodes import MappingNode, Node, ScalarNode, SequenceNode

from app.runtime.agent_paths import InvalidAgentId, validate_agent_id
from app.runtime.business_agent_workspace import WorkspaceProvisionEntry
from app.runtime.json_types import JsonObject
from app.services.agent_workspace_package_codec import WorkspacePackageError

MAX_AGENT_MANIFEST_BYTES = 256 * 1024
MAX_AGENT_MANIFEST_DEPTH = 64
MAX_AGENT_MANIFEST_NODES = 2_048


def validate_workspace_manifest_identity(
    entries: tuple[WorkspaceProvisionEntry, ...],
    *,
    expected_agent_id: str,
    import_action: str,
) -> str:
    """要求包内清单明确声明目标 Agent ID，且不得由平台改写。"""
    root = _load_manifest_root(
        entries,
        expected_agent_id=expected_agent_id,
        import_action=import_action,
    )
    _validate_manifest_node_graph(
        root,
        expected_agent_id=expected_agent_id,
        import_action=import_action,
    )
    declared_agent_id = _declared_agent_id(
        root,
        expected_agent_id=expected_agent_id,
        import_action=import_action,
    )
    if declared_agent_id != expected_agent_id:
        _raise_manifest_identity_mismatch(
            declared_agent_id=declared_agent_id,
            expected_agent_id=expected_agent_id,
            import_action=import_action,
        )
    return declared_agent_id


def _load_manifest_root(
    entries: tuple[WorkspaceProvisionEntry, ...],
    *,
    expected_agent_id: str,
    import_action: str,
) -> MappingNode:
    details = _manifest_identity_error_details(
        expected_agent_id=expected_agent_id,
        import_action=import_action,
        remediation=(f"在包根目录 agent.yaml 中设置 agent.id: {expected_agent_id}，并确保它与导入 URL 中的 agent_id 完全一致后重新打包。"),
    )
    manifest = next((entry for entry in entries if entry.relative_path == PurePosixPath("agent.yaml")), None)
    if manifest is None:
        raise WorkspacePackageError(
            422,
            "WORKSPACE_MANIFEST_AGENT_ID_REQUIRED",
            (
                f"导入被拒绝：Workspace 包根目录缺少 agent.yaml，无法确认它属于目标 Agent "
                f"“{expected_agent_id}”。请在包根目录添加 agent.yaml，并将唯一的 agent.id "
                f"设置为 {expected_agent_id} 后重新打包。"
            ),
            error_details=details,
        )
    if len(manifest.content) > MAX_AGENT_MANIFEST_BYTES:
        raise WorkspacePackageError(
            422,
            "WORKSPACE_MANIFEST_INVALID",
            (
                f"导入被拒绝：agent.yaml 超过 {MAX_AGENT_MANIFEST_BYTES} 字节，无法安全确认目标 "
                f"Agent “{expected_agent_id}”的身份。请精简清单，并将唯一的 agent.id 设置为 "
                f"{expected_agent_id} 后重新打包。"
            ),
            error_details=details,
        )
    try:
        manifest_text = manifest.content.decode("utf-8")
    except UnicodeDecodeError as exc:
        raise WorkspacePackageError(
            422,
            "WORKSPACE_MANIFEST_INVALID",
            (
                f"导入被拒绝：agent.yaml 不是有效的 UTF-8 文本，无法确认它属于目标 Agent "
                f"“{expected_agent_id}”。请将清单保存为 UTF-8，并将唯一的 agent.id 设置为 "
                f"{expected_agent_id} 后重新打包。"
            ),
            error_details=details,
        ) from exc
    try:
        root = yaml.compose(manifest_text, Loader=yaml.SafeLoader)
    except (yaml.YAMLError, RecursionError) as exc:
        raise WorkspacePackageError(
            422,
            "WORKSPACE_MANIFEST_INVALID",
            (
                f"导入被拒绝：agent.yaml 不是可解析的安全 YAML，无法确认它属于目标 Agent "
                f"“{expected_agent_id}”。请修复 YAML 语法，并将唯一的 agent.id 设置为 "
                f"{expected_agent_id} 后重新打包。"
            ),
            error_details=details,
        ) from exc
    if not isinstance(root, MappingNode):
        raise WorkspacePackageError(
            422,
            "WORKSPACE_MANIFEST_INVALID",
            (f"导入被拒绝：agent.yaml 顶层必须是对象，并包含唯一的 agent.id 字段。请将唯一的 agent.id 设置为 {expected_agent_id} 后重新打包。"),
            error_details=details,
        )
    return root


def _declared_agent_id(
    root: MappingNode,
    *,
    expected_agent_id: str,
    import_action: str,
) -> str:
    details = _manifest_identity_error_details(
        expected_agent_id=expected_agent_id,
        import_action=import_action,
        remediation=(f"在包根目录 agent.yaml 中设置 agent.id: {expected_agent_id}，并确保它与导入 URL 中的 agent_id 完全一致后重新打包。"),
    )
    agent_node = _require_unique_manifest_mapping_value(root, "agent", expected_agent_id, import_action)
    identity_node = _require_unique_manifest_mapping_value(agent_node, "id", expected_agent_id, import_action)
    if not isinstance(identity_node, ScalarNode) or identity_node.tag != "tag:yaml.org,2002:str":
        raise WorkspacePackageError(
            422,
            "WORKSPACE_MANIFEST_AGENT_ID_INVALID",
            (f"导入被拒绝：agent.yaml.agent.id 必须是字符串，并与目标 Agent ID “{expected_agent_id}”完全一致。请修正该字段后重新打包。"),
            error_details=details,
        )
    declared_agent_id = identity_node.value
    try:
        normalized_agent_id = validate_agent_id(declared_agent_id)
    except InvalidAgentId as exc:
        raise WorkspacePackageError(
            422,
            "WORKSPACE_MANIFEST_AGENT_ID_INVALID",
            (
                "导入被拒绝：agent.yaml.agent.id 必须是非空字符串，只能包含英文字母、数字、点、"
                f"下划线或连字符，且不能是 “.” 或 “..”。请将其设置为 {expected_agent_id} 后重新打包。"
            ),
            error_details=details,
        ) from exc
    if normalized_agent_id != declared_agent_id:
        raise WorkspacePackageError(
            422,
            "WORKSPACE_MANIFEST_AGENT_ID_INVALID",
            (f"导入被拒绝：agent.yaml.agent.id 不能包含首尾空白，并且必须与目标 Agent ID “{expected_agent_id}”逐字一致。请修正该字段后重新打包。"),
            error_details=details,
        )
    return declared_agent_id


def _raise_manifest_identity_mismatch(
    *,
    declared_agent_id: str,
    expected_agent_id: str,
    import_action: str,
) -> None:
    details = _manifest_identity_error_details(
        expected_agent_id=expected_agent_id,
        actual_agent_id=declared_agent_id,
        import_action=import_action,
        remediation="确认导入目标，使 agent.yaml.agent.id 与 URL 中的 agent_id 完全一致后重新打包。",
    )
    raise WorkspacePackageError(
        409,
        "WORKSPACE_MANIFEST_AGENT_ID_MISMATCH",
        (
            f"导入被拒绝：包内来源 Agent ID “{declared_agent_id}”与请求目标 Agent ID "
            f"“{expected_agent_id}”不一致；系统不会改写包内身份。请确认导入目标，并将 "
            "agent.yaml.agent.id 改为与 URL 中的 agent_id 完全一致后重新打包。"
        ),
        error_details=details,
    )


def _validate_manifest_node_graph(
    root: Node,
    *,
    expected_agent_id: str,
    import_action: str,
) -> None:
    details = _manifest_identity_error_details(
        expected_agent_id=expected_agent_id,
        import_action=import_action,
        remediation=f"简化 agent.yaml，并将唯一的 agent.id 设置为 {expected_agent_id} 后重新打包。",
    )
    stack: list[tuple[Node, int]] = [(root, 1)]
    visited: set[int] = set()
    while stack:
        node, depth = stack.pop()
        if depth > MAX_AGENT_MANIFEST_DEPTH:
            raise WorkspacePackageError(
                422,
                "WORKSPACE_MANIFEST_INVALID",
                (
                    f"导入被拒绝：agent.yaml 嵌套超过 {MAX_AGENT_MANIFEST_DEPTH} 层，无法安全确认包身份。"
                    f"请简化清单，并将唯一的 agent.id 设置为 {expected_agent_id} 后重新打包。"
                ),
                error_details=details,
            )
        node_identity = id(node)
        if node_identity in visited:
            raise WorkspacePackageError(
                422,
                "WORKSPACE_MANIFEST_INVALID",
                (f"导入被拒绝：agent.yaml 使用了重复引用或递归别名，身份声明不够明确。请移除别名，并将唯一的 agent.id 设置为 {expected_agent_id} 后重新打包。"),
                error_details=details,
            )
        visited.add(node_identity)
        if len(visited) > MAX_AGENT_MANIFEST_NODES:
            raise WorkspacePackageError(
                422,
                "WORKSPACE_MANIFEST_INVALID",
                (
                    f"导入被拒绝：agent.yaml 超过 {MAX_AGENT_MANIFEST_NODES} 个结构节点，无法安全确认包身份。"
                    f"请简化清单，并将唯一的 agent.id 设置为 {expected_agent_id} 后重新打包。"
                ),
                error_details=details,
            )
        if isinstance(node, MappingNode):
            stack.extend((child, depth + 1) for pair in node.value for child in pair)
        elif isinstance(node, SequenceNode):
            stack.extend((child, depth + 1) for child in node.value)


def _require_unique_manifest_mapping_value(
    node: MappingNode,
    key_name: str,
    expected_agent_id: str,
    import_action: str,
) -> Node:
    matching = [value for key, value in node.value if isinstance(key, ScalarNode) and key.value == key_name]
    field = "agent.yaml.agent" if key_name == "agent" else "agent.yaml.agent.id"
    details = _manifest_identity_error_details(
        expected_agent_id=expected_agent_id,
        import_action=import_action,
        field=field,
        remediation=(f"在 agent.yaml 中只保留一个 {key_name} 字段，并将 agent.id 设置为 {expected_agent_id} 后重新打包。"),
    )
    if not matching:
        raise WorkspacePackageError(
            422,
            "WORKSPACE_MANIFEST_AGENT_ID_REQUIRED",
            (
                f"导入被拒绝：agent.yaml 缺少必填字段 {field}，无法确认目标 Agent "
                f"“{expected_agent_id}”的身份。请补充唯一的 agent.id: {expected_agent_id} 后重新打包。"
            ),
            error_details=details,
        )
    if len(matching) != 1:
        raise WorkspacePackageError(
            422,
            "WORKSPACE_MANIFEST_INVALID",
            (f"导入被拒绝：agent.yaml 包含重复的 {field} 字段，身份声明不唯一。请只保留一个 agent.id: {expected_agent_id} 后重新打包。"),
            error_details=details,
        )
    value = matching[0]
    if key_name == "agent" and not isinstance(value, MappingNode):
        raise WorkspacePackageError(
            422,
            "WORKSPACE_MANIFEST_INVALID",
            (f"导入被拒绝：agent.yaml.agent 必须是对象，并包含唯一的字符串 id 字段。请将唯一的 agent.id 设置为 {expected_agent_id} 后重新打包。"),
            error_details=details,
        )
    return value


def _manifest_identity_error_details(
    *,
    expected_agent_id: str,
    import_action: str,
    remediation: str,
    actual_agent_id: str | None = None,
    field: str = "agent.yaml.agent.id",
) -> JsonObject:
    details: JsonObject = {
        "field": field,
        "import_action": import_action,
        "expected_agent_id": expected_agent_id,
        "remediation": remediation,
    }
    if actual_agent_id is not None:
        details["actual_agent_id"] = actual_agent_id
    return details
