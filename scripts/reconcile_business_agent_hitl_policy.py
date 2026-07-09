#!/usr/bin/env python3
from __future__ import annotations

import argparse
import hashlib
import json
import os
import shutil
from datetime import datetime, timezone
from pathlib import Path
from typing import TypedDict

try:
    from scripts.bootstrap_runtime_volume import DEFAULT_ENV_FILE, DEFAULT_TEMPLATE_DIR, resolve_runtime_root
    from scripts.runtime_template_renderer import RuntimeTemplateRenderContext, build_render_context, render_template_file
except ModuleNotFoundError:  # pragma: no cover - direct script execution
    from bootstrap_runtime_volume import DEFAULT_ENV_FILE, DEFAULT_TEMPLATE_DIR, resolve_runtime_root
    from runtime_template_renderer import RuntimeTemplateRenderContext, build_render_context, render_template_file

GENERIC_MCP_MUTATION_RULES = (
    "mcp__*__*write*",
    "mcp__*__*update*",
    "mcp__*__*delete*",
    "mcp__*__*block*",
    "mcp__*__*isolate*",
    "mcp__*__*disable*",
    "mcp__*__*kill*",
    "mcp__*__*quarantine*",
)
AGENT_MCP_MUTATION_RULES = {
    "response-disposal": (
        "mcp__soc-playbook-execution__*",
        "mcp__soc-playbook-registry__*",
    )
}
BASH_ALLOW_RULE = "Bash(*)"
OLD_HOOK_MARKERS = (
    "MCP 写入/处置动作放行",
    "permissionDecision\": \"allow\"",
    "SDK 无法呈现 \"ask\"",
)


class _ReconcileChangeRequired(TypedDict):
    agent_id: str
    kind: str
    path: str
    before_sha256: str
    after_sha256: str
    after: str


class ReconcileChange(_ReconcileChangeRequired, total=False):
    backup: str


class _ReconcileChangeSummaryRequired(TypedDict):
    agent_id: str
    kind: str
    path: str
    before_sha256: str
    after_sha256: str


class ReconcileChangeSummary(_ReconcileChangeSummaryRequired, total=False):
    backup: str


class _ReconcileResultRequired(TypedDict):
    ok: bool
    dry_run: bool
    runtime_root: str
    changes: list[ReconcileChangeSummary]


class ReconcileResult(_ReconcileResultRequired, total=False):
    backup_root: str


def reconcile_business_agent_hitl_policy(
    *,
    runtime_root: Path,
    template_dir: Path,
    env_file: Path,
    runtime_volume_mode: str | None,
    apply: bool,
    backup_dir: Path | None = None,
    operator: str = "system",
) -> ReconcileResult:
    runtime_root = runtime_root.resolve()
    env = _load_env(env_file)
    mode = runtime_volume_mode or _mode_from_env_file(env_file)
    context = build_render_context(mode=mode, env=env, runtime_root=runtime_root)
    backups_root = backup_dir or runtime_root / ".runtime-volume-seeds-backups" / f"hitl-policy-{_timestamp()}"
    changes: list[ReconcileChange] = []
    agents_dir = runtime_root / "data" / "business-agents"
    if not agents_dir.exists():
        return {"ok": True, "dry_run": not apply, "runtime_root": runtime_root.as_posix(), "changes": []}

    for agent_dir in sorted(path for path in agents_dir.iterdir() if path.is_dir()):
        workspace = agent_dir / "workspace"
        if not workspace.is_dir():
            continue
        agent_id = agent_dir.name
        template_workspace = template_dir / "data" / "business-agents" / agent_id / "workspace"
        changes.extend(
            _planned_file_changes(
                agent_id=agent_id,
                workspace=workspace,
                template_workspace=template_workspace,
                context=context,
            )
        )

    if apply:
        for change in changes:
            target = Path(str(change["path"]))
            before = target.read_text(encoding="utf-8") if target.exists() else ""
            after = str(change["after"])
            if before == after:
                continue
            backup = _backup_file(target, runtime_root=runtime_root, backups_root=backups_root)
            change["backup"] = backup.as_posix()
            target.parent.mkdir(parents=True, exist_ok=True)
            target.write_text(after, encoding="utf-8")
        _write_event(runtime_root, changes, operator=operator)

    return {
        "ok": True,
        "dry_run": not apply,
        "runtime_root": runtime_root.as_posix(),
        "backup_root": backups_root.as_posix(),
        "changes": [_public_change(change) for change in changes],
    }


def _planned_file_changes(
    *,
    agent_id: str,
    workspace: Path,
    template_workspace: Path,
    context: RuntimeTemplateRenderContext,
) -> list[ReconcileChange]:
    changes: list[ReconcileChange] = []
    settings = workspace / ".claude" / "settings.json"
    if settings.exists():
        after = _reconciled_settings(settings.read_text(encoding="utf-8"), agent_id=agent_id)
        _append_change(changes, agent_id, settings, "settings_permissions", after)

    mcp = workspace / ".mcp.json"
    if mcp.exists():
        after = render_template_file(mcp.read_text(encoding="utf-8"), rel_path=Path(".mcp.json"), context=context)
        _append_change(changes, agent_id, mcp, "mcp_template_values", after)

    hook = workspace / "hooks" / "pre_tool_guard.py"
    hook_template = template_workspace / "hooks" / "pre_tool_guard.py"
    fallback_hook_template = template_workspace.parent.parent / "main-agent" / "workspace" / "hooks" / "pre_tool_guard.py"
    if hook.exists() and any(marker in hook.read_text(encoding="utf-8") for marker in OLD_HOOK_MARKERS):
        source = hook_template if hook_template.exists() else fallback_hook_template
        after = source.read_text(encoding="utf-8")
        _append_change(changes, agent_id, hook, "pre_tool_guard", after)

    claude_md = workspace / "CLAUDE.md"
    claude_template = template_workspace / "CLAUDE.md"
    if claude_md.exists() and claude_template.exists():
        after = _replace_confirmation_section(
            claude_md.read_text(encoding="utf-8"),
            render_template_file(claude_template.read_text(encoding="utf-8"), rel_path=Path("CLAUDE.md"), context=context),
        )
        _append_change(changes, agent_id, claude_md, "claude_confirmation_section", after)
    return changes


def _reconciled_settings(text: str, *, agent_id: str) -> str:
    data = json.loads(text)
    permissions = data.setdefault("permissions", {})
    allow = [str(item) for item in permissions.get("allow") or []]
    ask = [str(item) for item in permissions.get("ask") or []]
    mutation_rules = AGENT_MCP_MUTATION_RULES.get(agent_id, GENERIC_MCP_MUTATION_RULES)
    ask = [rule for rule in ask if rule != BASH_ALLOW_RULE]
    if BASH_ALLOW_RULE not in allow:
        allow.append(BASH_ALLOW_RULE)
    allow = [rule for rule in allow if rule not in mutation_rules]
    for rule in mutation_rules:
        if rule not in ask:
            ask.append(rule)
    permissions["allow"] = allow
    permissions["ask"] = ask
    return json.dumps(data, ensure_ascii=False, indent=2) + "\n"


def _replace_confirmation_section(current: str, template: str) -> str:
    marker = "确认与执行规则（避免重复确认死循环）："
    start = current.find(marker)
    template_start = template.find(marker)
    if start < 0 or template_start < 0:
        return current
    end = _next_heading(current, start + len(marker))
    template_end = _next_heading(template, template_start + len(marker))
    if end < 0 or template_end < 0:
        return current
    return current[:start] + template[template_start:template_end].rstrip() + "\n\n" + current[end:]


def _next_heading(text: str, offset: int) -> int:
    index = text.find("\n## ", offset)
    return index + 1 if index >= 0 else -1


def _append_change(changes: list[ReconcileChange], agent_id: str, path: Path, kind: str, after: str) -> None:
    before = path.read_text(encoding="utf-8") if path.exists() else ""
    if before == after:
        return
    changes.append(
        {
            "agent_id": agent_id,
            "kind": kind,
            "path": path.as_posix(),
            "before_sha256": _sha256(before),
            "after_sha256": _sha256(after),
            "after": after,
        }
    )


def _backup_file(path: Path, *, runtime_root: Path, backups_root: Path) -> Path:
    rel = path.relative_to(runtime_root)
    digest = hashlib.sha256(rel.as_posix().encode("utf-8")).hexdigest()[:12]
    backup = backups_root / digest / rel
    backup.parent.mkdir(parents=True, exist_ok=True)
    shutil.copy2(path, backup)
    return backup


def _write_event(runtime_root: Path, changes: list[ReconcileChange], *, operator: str) -> None:
    event_path = runtime_root / "data" / "transcripts" / "business-agent-hitl-reconcile.jsonl"
    event_path.parent.mkdir(parents=True, exist_ok=True)
    record = {
        "created_at": datetime.now(timezone.utc).isoformat(),
        "operator": operator,
        "change_count": len(changes),
        "changes": [_public_change(change) for change in changes],
    }
    with event_path.open("a", encoding="utf-8") as fh:
        fh.write(json.dumps(record, ensure_ascii=False, sort_keys=True) + "\n")


def _public_change(change: ReconcileChange) -> ReconcileChangeSummary:
    summary: ReconcileChangeSummary = {
        "agent_id": change["agent_id"],
        "kind": change["kind"],
        "path": change["path"],
        "before_sha256": change["before_sha256"],
        "after_sha256": change["after_sha256"],
    }
    if "backup" in change:
        summary["backup"] = change["backup"]
    return summary


def _sha256(text: str) -> str:
    return hashlib.sha256(text.encode("utf-8")).hexdigest()


def _timestamp() -> str:
    return datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")


def _load_env(path: Path) -> dict[str, str]:
    env = dict(os.environ)
    if not path.exists():
        return env
    for line in path.read_text(encoding="utf-8").splitlines():
        stripped = line.strip()
        if not stripped or stripped.startswith("#") or "=" not in stripped:
            continue
        key, value = stripped.split("=", 1)
        env[key.strip()] = os.path.expandvars(os.path.expanduser(value.strip().strip("'\"")))
    return env


def _mode_from_env_file(path: Path) -> str:
    return "local-debug" if "local-debug" in path.name else "container"


def main() -> int:
    parser = argparse.ArgumentParser(description="Reconcile existing business-agent workspaces for Claude Web HITL policy.")
    parser.add_argument("--runtime-root", default=None)
    parser.add_argument("--template-dir", type=Path, default=DEFAULT_TEMPLATE_DIR)
    parser.add_argument("--env-file", type=Path, default=DEFAULT_ENV_FILE)
    parser.add_argument("--runtime-volume-mode", choices=["container", "local-debug"], default=None)
    parser.add_argument("--apply", action="store_true", help="Write changes. Without this flag the command is dry-run.")
    parser.add_argument("--operator", default="system")
    args = parser.parse_args()
    runtime_root = resolve_runtime_root(args.runtime_root, args.env_file, args.runtime_volume_mode)
    result = reconcile_business_agent_hitl_policy(
        runtime_root=runtime_root,
        template_dir=args.template_dir,
        env_file=args.env_file,
        runtime_volume_mode=args.runtime_volume_mode,
        apply=args.apply,
        operator=args.operator,
    )
    print(json.dumps(result, ensure_ascii=False, indent=2, sort_keys=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
