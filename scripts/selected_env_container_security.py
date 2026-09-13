"""Docker inspect 对 systempaths 安全选项的语义投影。"""

from __future__ import annotations

from collections.abc import Mapping

from scripts.selected_env_operation_contract import SelectedEnvError


def actual_systempaths_unconfined(host_config: Mapping[str, object], service: str) -> bool:
    paths: list[list[str]] = []
    for key in ("MaskedPaths", "ReadonlyPaths"):
        value = host_config.get(key)
        if not isinstance(value, list) or not all(isinstance(path, str) and path.startswith("/") for path in value):
            raise SelectedEnvError(f"运行容器 {key} 结构无效: {service}")
        paths.append(value)
    if bool(paths[0]) != bool(paths[1]):
        raise SelectedEnvError(f"运行容器 systempaths 状态不一致: {service}")
    return not paths[0]
