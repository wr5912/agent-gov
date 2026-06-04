#!/usr/bin/env python3
from __future__ import annotations

import argparse
import json
import shutil
import tarfile
from datetime import datetime, timezone
from pathlib import Path
from typing import TypedDict

from bootstrap_runtime_volume import resolve_runtime_root
from runtime_template_safety import Finding, SanitizeResult, scan_path, sanitize_path


DEFAULT_TEMPLATE_DIR = Path("docker/runtime-template")
DEFAULT_BACKUP_DIR = Path("docker/.runtime-template-backups")
DEFAULT_STAGING_DIR = Path("docker/.runtime-template-staging")
DEFAULT_ENV_FILE = Path("docker/.env")
WORKSPACE_DIR_NAMES = {
    "main-workspace",
    "attribution-analyzer-workspace",
    "proposal-generator-workspace",
    "execution-optimizer-workspace",
    "eval-case-governor-workspace",
    "regression-impact-analyzer-workspace",
}
EXCLUDED_DIR_NAMES = {
    ".git",
    "__pycache__",
    ".pytest_cache",
    ".mypy_cache",
    ".ruff_cache",
    ".claude",
    "agent-governance",
    "agent-releases",
    "agent-versions",
    "cache",
    "data",
    "langfuse",
    "logs",
    "outputs",
    "sessions",
    "telemetry",
    "transcripts",
    "uploads",
}
ALLOWED_SUFFIXES = {
    "",
    ".example",
    ".gitignore",
    ".json",
    ".md",
    ".py",
    ".sh",
    ".toml",
    ".txt",
    ".yaml",
    ".yml",
}
ALLOWED_DOT_CLAUDE_DIRS = {"agents", "commands", "output-styles", "rules", "skills"}
ALLOWED_DOT_CLAUDE_FILES = {"settings.json", "settings.local.json.example"}
ALLOWED_FILENAMES = {".mcp.json", ".worktreeinclude", "requirements.txt"}

README = """# Runtime Template

本目录保存可复用的 Agent Runtime 初始配置模板，用于从零部署时填充运行态目录。

模板只保存结构、说明和安全默认值；真实环境里的 API key、token、Authorization header、数据库凭据、MCP 地址、IP、端口、URL、邮箱、账号、本机路径和 Claude 本地状态都不能进入模板。

## 使用方式

- 初始化运行态目录：`make runtime-bootstrap`
- 从当前运行态保存模板：`make runtime-template-export`
- 查看模板备份：`make runtime-template-restore-list`
- 恢复模板备份：`make runtime-template-restore BACKUP=<backup-file>`

`runtime-bootstrap` 默认只补齐缺失文件，不覆盖已有本地配置。真实部署值应写入 `docker/.env`、部署环境变量或不提交的本地覆盖文件。

## 占位符

模板中的 `${...}` 是部署占位符，例如 `${MCP_SERVER_URL}`、`${SOC_API_URL}`、`${API_TOKEN}`、`${SERVICE_HOST}`、`${SERVICE_PORT}`。部署时按环境注入，不要把真实值提交回模板。

## 安全规则

保存模板会先进入 staging 目录，执行脱敏和校验，通过后才替换本目录。无法判断是否安全的内容会阻断导出，正式模板保持不变。
"""


class FindingResult(TypedDict):
    path: str
    line: int
    kind: str
    severity: str
    message: str
    snippet: str


class ExportResult(TypedDict, total=False):
    ok: bool
    runtime_root: str
    template_dir: str
    backup: str | None
    staging_dir: str
    copied: list[str]
    sanitize: SanitizeResult
    findings: list[FindingResult]


def _repo_root() -> Path:
    return Path(__file__).resolve().parents[1]


def _timestamp() -> str:
    return datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")


def _is_allowed_source(rel: Path) -> bool:
    parts = rel.parts
    if not parts or parts[0] not in WORKSPACE_DIR_NAMES:
        return False
    if ".git" in parts:
        return False
    if rel.name in {".env", ".mcp.local.json", "CLAUDE.local.md", "settings.local.json"}:
        return False
    if ".local." in rel.name and not rel.name.endswith(".example"):
        return False
    if ".claude" in parts:
        index = parts.index(".claude")
        if len(parts) == index + 2:
            return rel.name in ALLOWED_DOT_CLAUDE_FILES
        if len(parts) > index + 2 and parts[index + 1] not in ALLOWED_DOT_CLAUDE_DIRS:
            return False
    elif any(part in EXCLUDED_DIR_NAMES for part in parts):
        return False
    if rel.name in ALLOWED_FILENAMES:
        return True
    if rel.name.endswith(".example"):
        return True
    return rel.suffix in ALLOWED_SUFFIXES


def _copy_allowed_config(runtime_root: Path, staging_dir: Path) -> list[str]:
    copied: list[str] = []
    for path in sorted(runtime_root.rglob("*")):
        if not path.is_file():
            continue
        rel = path.relative_to(runtime_root)
        if not _is_allowed_source(rel):
            continue
        dest = staging_dir / rel
        dest.parent.mkdir(parents=True, exist_ok=True)
        shutil.copy2(path, dest)
        copied.append(rel.as_posix())
    return copied


def _write_template_docs(staging_dir: Path, *, runtime_root: Path, copied: list[str]) -> None:
    staging_dir.mkdir(parents=True, exist_ok=True)
    (staging_dir / "README.md").write_text(README, encoding="utf-8")
    manifest = {
        "generated_at": datetime.now(timezone.utc).isoformat(),
        "source": "runtime-template-export",
        "source_runtime_root": "${HOST_RUNTIME_VOLUME_ROOT}",
        "source_file_count": len(copied),
        "safety": {
            "private_network_values": "placeholder_or_documentation_range_only",
            "secrets": "placeholder_only",
            "local_overrides": "excluded",
            "runtime_state": "excluded",
        },
    }
    (staging_dir / ".template-sanitization.json").write_text(json.dumps(manifest, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")


def _create_backup(template_dir: Path, backup_dir: Path, prefix: str = "runtime-template") -> Path | None:
    if not template_dir.exists():
        return None
    backup_dir.mkdir(parents=True, exist_ok=True)
    backup_path = backup_dir / f"{prefix}-{_timestamp()}.tar.gz"
    with tarfile.open(backup_path, "w:gz") as archive:
        archive.add(template_dir, arcname=template_dir.name)
    return backup_path


def _finding_result(finding: Finding) -> FindingResult:
    return {
        "path": finding.path,
        "line": finding.line,
        "kind": finding.kind,
        "severity": finding.severity,
        "message": finding.message,
        "snippet": finding.snippet,
    }


def _replace_template(staging_dir: Path, template_dir: Path) -> None:
    old_dir = template_dir.with_name(f".{template_dir.name}.old-{_timestamp()}")
    if old_dir.exists():
        shutil.rmtree(old_dir)
    if template_dir.exists():
        template_dir.rename(old_dir)
    try:
        staging_dir.rename(template_dir)
    except Exception:
        if template_dir.exists():
            shutil.rmtree(template_dir)
        if old_dir.exists():
            old_dir.rename(template_dir)
        raise
    if old_dir.exists():
        shutil.rmtree(old_dir)


def export_runtime_template(
    *,
    runtime_root: Path,
    template_dir: Path,
    backup_dir: Path,
    staging_root: Path,
) -> ExportResult:
    timestamp = _timestamp()
    staging_dir = staging_root / f"runtime-template-{timestamp}"
    if staging_dir.exists():
        shutil.rmtree(staging_dir)
    staging_dir.mkdir(parents=True, exist_ok=True)

    copied = _copy_allowed_config(runtime_root, staging_dir)
    _write_template_docs(staging_dir, runtime_root=runtime_root, copied=copied)
    sanitize_result = sanitize_path(staging_dir)
    findings = scan_path(staging_dir)
    high_findings = [finding for finding in findings if finding.severity == "high"]
    if high_findings:
        return {
            "ok": False,
            "staging_dir": staging_dir.as_posix(),
            "copied": copied,
            "sanitize": sanitize_result,
            "findings": [_finding_result(finding) for finding in findings],
        }

    backup_path = _create_backup(template_dir, backup_dir)
    _replace_template(staging_dir, template_dir)
    return {
        "ok": True,
        "runtime_root": runtime_root.as_posix(),
        "template_dir": template_dir.as_posix(),
        "backup": backup_path.as_posix() if backup_path else None,
        "copied": copied,
        "sanitize": sanitize_result,
    }


def main() -> int:
    repo_root = _repo_root()
    parser = argparse.ArgumentParser(description="Export runtime config files into docker/runtime-template with sanitization.")
    parser.add_argument("--runtime-root")
    parser.add_argument("--template-dir", type=Path, default=repo_root / DEFAULT_TEMPLATE_DIR)
    parser.add_argument("--backup-dir", type=Path, default=repo_root / DEFAULT_BACKUP_DIR)
    parser.add_argument("--staging-dir", type=Path, default=repo_root / DEFAULT_STAGING_DIR)
    parser.add_argument("--env-file", type=Path, default=repo_root / DEFAULT_ENV_FILE)
    args = parser.parse_args()

    runtime_root = resolve_runtime_root(args.runtime_root, args.env_file)
    result = export_runtime_template(
        runtime_root=runtime_root,
        template_dir=args.template_dir.resolve(),
        backup_dir=args.backup_dir.resolve(),
        staging_root=args.staging_dir.resolve(),
    )
    print(json.dumps(result, ensure_ascii=False, indent=2))
    return 0 if result["ok"] else 1


if __name__ == "__main__":
    raise SystemExit(main())
