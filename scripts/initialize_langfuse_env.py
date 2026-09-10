#!/usr/bin/env python3
"""首次生成私有 Langfuse 配置，或无损精简已有 env；不会轮换既有凭据。"""

from __future__ import annotations

import argparse
import base64
import io
import os
import re
import secrets
import tempfile
from collections.abc import Mapping
from pathlib import Path

from dotenv.parser import Binding, parse_stream

SECRET_KEYS = (
    "LANGFUSE_PUBLIC_KEY",
    "LANGFUSE_SECRET_KEY",
    "LANGFUSE_INIT_USER_PASSWORD",
    "LANGFUSE_SALT",
    "LANGFUSE_ENCRYPTION_KEY",
    "LANGFUSE_NEXTAUTH_SECRET",
    "LANGFUSE_POSTGRES_PASSWORD",
    "LANGFUSE_CLICKHOUSE_PASSWORD",
    "LANGFUSE_REDIS_AUTH",
    "LANGFUSE_MINIO_ROOT_PASSWORD",
)
DEFAULTS = {
    "LANGFUSE_BASE_URL": "http://langfuse-web:3000",
    "LANGFUSE_BIND_IP": "127.0.0.1",
    "LANGFUSE_ALLOW_PUBLIC_BIND": "0",
    "LANGFUSE_HOST_PORT": "50402",
    "LANGFUSE_INIT_ORG_ID": "agent-gov",
    "LANGFUSE_INIT_ORG_NAME": "AgentGov",
    "LANGFUSE_INIT_PROJECT_ID": "agent-gov",
    "LANGFUSE_INIT_PROJECT_NAME": "AgentGov",
    "LANGFUSE_INIT_USER_NAME": "admin",
    "LANGFUSE_MINIO_ROOT_USER": "minio",
    "OTEL_SERVICE_NAME": "agent-gov-agentscope-runtime",
    "OTEL_RESOURCE_ATTRIBUTES": "deployment.environment.name=local",
}
DATA_MOUNTS = {
    "LANGFUSE_POSTGRES_DATA_MOUNT": "postgres",
    "LANGFUSE_CLICKHOUSE_DATA_MOUNT": "clickhouse/data",
    "LANGFUSE_REDIS_DATA_MOUNT": "redis",
    "LANGFUSE_MINIO_DATA_MOUNT": "minio",
}


def parse_env(content: str) -> tuple[list[Binding], dict[str, str]]:
    bindings = list(parse_stream(io.StringIO(content)))
    values: dict[str, str] = {}
    for binding in bindings:
        if binding.error:
            raise ValueError("env 格式无法安全解析，未修改文件")
        if binding.key is not None:
            if binding.key in values:
                raise ValueError(f"重复配置 {binding.key}，请先消除歧义")
            values[binding.key] = binding.value or ""
    return bindings, values


def _is_missing(value: str) -> bool:
    return not value or value.startswith("replace-with-")


def _existing_data(values: dict[str, str]) -> bool:
    for key in ("HOST_RUNTIME_VOLUME_ROOT", *DATA_MOUNTS):
        if key in os.environ and os.environ[key] != values.get(key):
            raise ValueError("宿主环境覆盖了存储位置，请先移除覆盖并使用所选私有 env")

    def expand(value: str) -> Path:
        def replace(match: re.Match[str]) -> str:
            key = match.group(1)
            if key in values and key in os.environ and values[key] != os.environ[key]:
                raise ValueError("宿主环境覆盖了存储路径引用，请先移除覆盖")
            resolved = values.get(key, os.environ.get(key))
            if resolved is None or "$" in resolved:
                raise ValueError("存储路径无法安全解析，未生成凭据")
            return resolved

        expanded = re.sub(r"\$\{([A-Za-z_][A-Za-z0-9_]*)\}", replace, value)
        if "$" in expanded:
            raise ValueError("存储路径无法安全解析，未生成凭据")
        path = Path(expanded).expanduser()
        # Compose 的相对 bind mount 以首个 -f 文件目录为基准，而不是 --env-file 所在目录。
        return path if path.is_absolute() else Path(__file__).resolve().parents[1] / "docker" / path

    root = expand(values.get("HOST_RUNTIME_VOLUME_ROOT") or str(Path.home() / "volume-agent-gov"))
    for key, suffix in DATA_MOUNTS.items():
        path = expand(values[key]) if values.get(key) else root / "langfuse" / suffix
        if path.exists() and (not path.is_dir() or next(path.iterdir(), None) is not None):
            return True
    return False


def _initial_env_values(values: dict[str, str]) -> Mapping[str, str]:
    missing = [key for key in SECRET_KEYS if _is_missing(values.get(key, ""))]
    if missing and _existing_data(values):
        raise ValueError("检测到已有 Langfuse 数据；请恢复原有凭据，禁止自动生成替代值")
    updates = {}
    for key in missing:
        prefix = "pk-lf-" if key == "LANGFUSE_PUBLIC_KEY" else "sk-lf-" if key == "LANGFUSE_SECRET_KEY" else ""
        updates[key] = prefix + secrets.token_hex(32)
    return updates


def _redundant_keys(values: dict[str, str]) -> set[str]:
    defaults = dict(DEFAULTS)
    port = values.get("LANGFUSE_HOST_PORT") or defaults["LANGFUSE_HOST_PORT"]
    defaults["LANGFUSE_NEXTAUTH_URL"] = f"http://localhost:{port}"
    defaults["FRONTEND_LANGFUSE_URL"] = values.get("LANGFUSE_NEXTAUTH_URL") or defaults["LANGFUSE_NEXTAUTH_URL"]
    base = values.get("LANGFUSE_BASE_URL") or defaults["LANGFUSE_BASE_URL"]
    defaults["OTEL_EXPORTER_OTLP_ENDPOINT"] = f"{base.rstrip('/')}/api/public/otel"
    redundant = {key for key, value in defaults.items() if values.get(key) == value}
    for suffix in ("PUBLIC_KEY", "SECRET_KEY"):
        old_key = f"LANGFUSE_INIT_PROJECT_{suffix}"
        if old_key not in values:
            continue
        if values[old_key] and values[old_key] != values.get(f"LANGFUSE_{suffix}"):
            raise ValueError(f"{old_key} 与主凭据不一致，请确认项目身份后再精简")
        redundant.add(old_key)
    public, secret = values.get("LANGFUSE_PUBLIC_KEY"), values.get("LANGFUSE_SECRET_KEY")
    if public and secret:
        auth = base64.b64encode(f"{public}:{secret}".encode()).decode()
        if values.get("OTEL_EXPORTER_OTLP_HEADERS") == f"Authorization=Basic {auth},x-langfuse-ingestion-version=4":
            redundant.add("OTEL_EXPORTER_OTLP_HEADERS")
    return redundant


def render_env(content: str, *, compact: bool) -> tuple[str, list[str]]:
    bindings, values = parse_env(content)
    # 初始化也检查旧身份差异，避免生成一对与既有项目无关的新 key。
    redundant = _redundant_keys(values)
    updates = {} if compact else _initial_env_values(values)
    removals = redundant if compact else set()
    output: list[str] = []
    for binding in bindings:
        if binding.key in removals:
            continue
        if binding.key in updates:
            output.append(f"{binding.key}={updates[binding.key]}\n")
        else:
            output.append(binding.original.string)
    added = [key for key in updates if key not in values]
    if added:
        if output and not output[-1].endswith("\n"):
            output.append("\n")
        output.extend(f"{key}={updates[key]}\n" for key in added)
    return "".join(output), sorted(set(updates) | removals)


def update_env(env_file: Path, *, compact: bool = False, dry_run: bool = False) -> list[str]:
    if env_file.is_symlink() or env_file.name.endswith(".example"):
        raise ValueError("只允许修改普通私有 env 文件，不修改符号链接或示例")
    original = env_file.read_bytes()
    updated, changed = render_env(original.decode("utf-8"), compact=compact)
    if not changed or dry_run:
        return changed
    # 临时文件默认 0600；备份保留在私有 env 旁，项目 .gitignore 排除 .env.bak*。
    with tempfile.NamedTemporaryFile(prefix=".env.bak-", dir=env_file.parent, delete=False) as backup:
        backup.write(original)
    temporary: Path | None = None
    try:
        with tempfile.NamedTemporaryFile(prefix=".env.bak-write-", dir=env_file.parent, delete=False) as destination:
            temporary = Path(destination.name)
            destination.write(updated.encode("utf-8"))
            destination.flush()
            os.fsync(destination.fileno())
        if env_file.read_bytes() != original:
            raise ValueError("env 在处理中发生变化，未覆盖文件")
        os.replace(temporary, env_file)
    finally:
        if temporary is not None:
            temporary.unlink(missing_ok=True)
    return changed


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--env-file", type=Path, default=Path("docker/.env"))
    parser.add_argument("--compact", action="store_true", help="仅移除等于默认/派生值的配置，不生成或轮换凭据")
    parser.add_argument("--dry-run", action="store_true", help="只报告将变更的键名")
    args = parser.parse_args()
    try:
        changed = update_env(args.env_file, compact=args.compact, dry_run=args.dry_run)
    except (OSError, ValueError):
        # 不回显异常中的路径/业务内容；详细规则由测试和文档说明。
        parser.exit(1, "无法安全更新 Langfuse env：请检查格式、权限、重复项目身份或已有数据的凭据完整性。文件未覆盖。\n")
    state = "预检" if args.dry_run else "已更新并保留 0600 备份" if changed else "无需修改"
    print(f"Langfuse env {state}：{len(changed)} 项" + (f" ({', '.join(changed)})" if changed else ""))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
