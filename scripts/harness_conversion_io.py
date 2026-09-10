"""Deterministic filesystem and serialization helpers for Harness conversion."""

from __future__ import annotations

import hashlib
import json
import os
import shutil
import tempfile
from collections.abc import Iterable, Mapping, Sequence
from pathlib import Path
from typing import TypeAlias, cast

import yaml

HarnessObject: TypeAlias = dict[str, object]
AssetRecord: TypeAlias = dict[str, str]

LEGACY_TEXT_REPLACEMENTS: tuple[tuple[str, str], ...] = (
    ("../data/business-agents/", "/business-agents/"),
    ("/data/business-agents/", "/business-agents/"),
    ("/data/outputs", "/workspace/outputs"),
    ("/data/uploads", "/workspace/data"),
    ("/data/sessions", "/workspace/data/sessions"),
    ("/data/transcripts", "/workspace/data/transcripts"),
    ("/data/agent-memory", "/workspace/data/agent-memory"),
    ("../data/", "/workspace/data/"),
    ("/data/", "/workspace/data/"),
    (".claude/skills/", "skills/"),
    (".claude/agents/", "subagents/"),
    (".claude/commands/", "skills/"),
    (".claude/rules/", "AGENT.md"),
    (".claude/settings.local.json", "agent.local.yaml"),
    (".claude/settings.json", "agent.yaml 中的 workspace_policy"),
    (".claude/**", "skills/**"),
    (".claude/", "skills/"),
    (".mcp.json", "mcp/*.json"),
    ("mcp_servers/", "mcp/"),
    ("subagents/<name>.md", "subagents/<name>/{agent.yaml,AGENT.md}"),
    ("CLAUDE.md", "AGENT.md"),
    ("AGENT.md/settings/mcp/*.json/skills/.env", "AGENT.md/agent.yaml/mcp/*.json/skills/.env"),
    ("`hooks/`", "`agent.yaml` 的 `runtime_middlewares`"),
    ("权限（allow/ask/deny）、hooks、defaultMode", "权限策略、runtime_middlewares、permission_mode"),
    ("权限、hooks 和 sandbox", "权限、runtime_middlewares 和 sandbox"),
    ("Claude Code", "AgentScope Runtime"),
    ("Claude 原生", "AgentScope 原生"),
    ("Claude 内部", "AgentScope Runtime 内部"),
)

_LEGACY_SELF_READ_INSTRUCTION = (
    "当用户询问 workspace 配置结构、配置项含义或配置对比时，先用 Read 工具读取当前 workspace 下的 "
    "`AGENT.md`、`agent.yaml`、`mcp/*.json` 和 `agent.yaml 中的 workspace_policy`，"
    "基于实际文件内容回答，不得仅凭训练知识或泛化格式回答。"
)
_AGENTSCOPE_INJECTED_HARNESS_INSTRUCTION = (
    "当前 AgentScope Session 的 `/workspace` 只保存会话数据与执行产物；已发布 Harness 的 `AGENT.md` "
    "由 Runtime 注入为 system prompt，Skill 与 MCP 由 Runtime 绑定。用户询问配置结构、配置项含义或配置对比时，"
    "只依据本次会话已注入的内容回答；未注入的 `agent.yaml` 字段或源文件逐字内容应由 AgentGov 配置接口查询，"
    "不得尝试读取 Runtime 外层路径或臆测。"
)


def sha256_file(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def regular_files(root: Path) -> tuple[Path, ...]:
    return tuple(sorted(path for path in root.rglob("*") if path.is_file()))


def tree_digest(root: Path, *, excluded: Iterable[str] = ()) -> str:
    excluded_paths = set(excluded)
    digest = hashlib.sha256()
    for path in regular_files(root):
        relative = path.relative_to(root).as_posix()
        if relative in excluded_paths:
            continue
        digest.update(relative.encode("utf-8"))
        digest.update(b"\0")
        digest.update(path.read_bytes())
        digest.update(b"\0")
    return digest.hexdigest()


def publish_directory(staging: Path, destination: Path, *, replace: bool) -> None:
    if destination.exists() and not replace:
        raise FileExistsError(f"destination already exists: {destination}")
    backup: Path | None = None
    if destination.exists():
        backup = Path(tempfile.mkdtemp(prefix=f".{destination.name}.backup-", dir=destination.parent))
        backup.rmdir()
        os.replace(destination, backup)
    try:
        os.replace(staging, destination)
    except Exception:
        if backup is not None and not destination.exists():
            os.replace(backup, destination)
        raise
    if backup is not None:
        shutil.rmtree(backup)


def read_yaml_object(path: Path) -> HarnessObject:
    value = yaml.safe_load(path.read_text(encoding="utf-8"))
    if not isinstance(value, dict):
        raise ValueError(f"YAML root must be an object: {path}")
    return cast(HarnessObject, value)


def read_json_object(path: Path) -> HarnessObject:
    value = json.loads(path.read_text(encoding="utf-8"))
    if not isinstance(value, dict):
        raise ValueError(f"JSON root must be an object: {path}")
    return cast(HarnessObject, value)


def split_frontmatter(text: str) -> tuple[HarnessObject, str]:
    if not text.startswith("---\n"):
        return {}, text
    end = text.find("\n---\n", 4)
    if end < 0:
        raise ValueError("unterminated YAML frontmatter")
    metadata = yaml.safe_load(text[4:end]) or {}
    if not isinstance(metadata, dict):
        raise ValueError("YAML frontmatter must be an object")
    return cast(HarnessObject, metadata), text[end + 5 :]


def render_skill(*, name: str, description: str, body: str) -> str:
    metadata = yaml.safe_dump(
        {"name": name, "description": description},
        allow_unicode=True,
        sort_keys=False,
        width=120,
    ).rstrip()
    return f"---\n{metadata}\n---\n\n{body.strip()}\n"


def asset_record(root: Path, path: Path, *, name: str) -> AssetRecord:
    return {
        "name": name,
        "path": path.relative_to(root).as_posix(),
        "sha256": sha256_file(path) if path.is_file() else tree_digest(path),
    }


def string_list(value: object) -> list[str]:
    if not isinstance(value, list):
        return []
    return [str(item) for item in value]


def rewrite_text(value: str, replacements: Sequence[tuple[str, str]] = LEGACY_TEXT_REPLACEMENTS) -> str:
    rewritten = value
    # Path replacements must be atomic.  A sequential replacement would turn
    # ``/data/uploads`` into ``/workspace/workspace/data`` when the later
    # generic ``/data/`` rule sees the replacement text again.
    protected_paths: list[tuple[str, str]] = []
    for index, (old, new) in enumerate(replacements):
        if old.startswith(("/data", "../data")):
            marker = f"@@AGENTGOV_CONVERTED_PATH_{index}@@"
            rewritten = rewritten.replace(old, marker)
            protected_paths.append((marker, new))
        else:
            rewritten = rewritten.replace(old, new)
    for marker, path in protected_paths:
        rewritten = rewritten.replace(marker, path)
    rewritten = rewritten.replace("CLAUDE.local.md", "AGENT.local.md")
    rewritten = rewritten.replace("claude-hook-audit.jsonl", "agentscope-tool-audit.jsonl")
    return rewritten.replace(
        _LEGACY_SELF_READ_INSTRUCTION,
        _AGENTSCOPE_INJECTED_HARNESS_INSTRUCTION,
    )


def rewrite_kind_text(value: str, kind: str) -> str:
    """Rewrite Runtime-specific instructions that are not a path rename."""

    rewritten = rewrite_text(value)
    if kind == "business":
        rewritten = rewritten.replace(
            "- 将安全运营分析、处置提案和只读校验结论写入",
            "- 子 Agent 委派只能使用 AgentScope 公共团队流程：`TeamCreate` → `AgentCreate` → `TeamSay` → `TeamDelete`。"
            "`AgentCreate.subagent_type` 必须从 Runtime 追加的当前 Harness 精确类型清单选择，禁止 `default`、其他 Agent "
            "或其他版本的模板。\n- 将安全运营分析、处置提案和只读校验结论写入",
            1,
        )
        rewritten = rewritten.replace(
            "5. 委派 `response-playbook-planning` 形成目标、成功标准、风险和影响范围。\n"
            "6. 委派 `response-playbook-builder` 选择已有剧本，或在内存中构建完整临时剧本；临时剧本此时不得保存。"
            "若当前模型不能稳定收敛子 Agent 输出，可由主 Agent 在相同只读边界内直接完成，但不得降低下列校验要求。",
            "5. 需要委派时先调用 AgentScope `TeamCreate`，再调用 `AgentCreate`；`subagent_type` 必须使用系统提示中当前 "
            "Harness 版本列出的 `response-playbook-planning` 精确类型。通过 `TeamSay` 发送任务并等待结果，形成目标、"
            "成功标准、风险和影响范围。\n"
            "6. 以同一公共团队流程调用当前版本的 `response-playbook-builder`，选择已有剧本或在内存中构建完整临时剧本；"
            "完成后调用 `TeamDelete`。临时剧本此时不得保存。若当前模型不能稳定收敛子 Agent 输出，可由主 Agent 在相同"
            "只读边界内直接完成，但不得降低下列校验要求。",
        )
        return rewritten
    if kind != "governor":
        return rewritten

    replacements = (
        (
            "你对所有业务 Agent 有完全读取权限，可用 Read/Glob/Grep 按需读取其 workspace 全部配置（含 .env/secrets）"
            "以支撑归因/优化；不直接落地结果。需要原样保留代码的回归测试任务使用 AgentScope 原生结构化输出并由 "
            "Pydantic 校验，其他治理任务由后端结构化投影后校验。",
            "你只能通过 Runtime 按本次 run 绑定的 `HarnessList`/`HarnessRead` 读取单一目标业务 Agent 的非敏感 "
            "Harness 文件以支撑归因/优化；不直接落地结果。每次最终回复必须是符合本次 JSON Schema 的单个 JSON "
            "object，并由 AgentGov 使用 Pydantic 校验。",
        ),
        (
            "- 可用 Read/Glob/Grep 按需读取本次 job 涉及业务 Agent 的 workspace 原始配置（`AGENT.md`、"
            "`agent.yaml 中的 workspace_policy`、`mcp/*.json`、`skills/**`、`.env` 等），也可读 job input 列出的 "
            "evidence；优先读与本次归因/优化直接相关的配置，不必全量读。",
            "- 读取目标业务 Agent 的 Harness 时，先调用 `HarnessList()` 获取本次 run 已绑定 root 的非敏感文件清单，"
            "再把返回的完整逻辑路径交给 `HarnessRead(path)`；`Read`/`Glob`/`Grep` 只用于 `/workspace/data` 下本次 job "
            "提供的非敏感 evidence。禁止读取 `.env`、credential、token、secret 和 Runtime/AgentGov 数据库。",
        ),
        (
            "- 可以输出自然语言分析或 JSON；重点是明确本次治理任务要求的业务结论、责任边界、证据引用、置信度和下一步。",
            "- 最终只输出本次请求末尾 JSON Schema 对应的 JSON object，不输出 Markdown 围栏或自然语言前后缀。",
        ),
        (
            "description: 归因/优化时按需读取目标业务 Agent 的 workspace 原始配置（AGENT.md/agent.yaml/mcp/*.json/skills/.env），"
            "核对当前配置真相，避免脱离实际臆断。",
            "description: 归因/优化时通过受控工具读取目标业务 Agent 的非敏感 Harness（AGENT.md/agent.yaml/mcp/*.json/skills/subagents），核对当前配置真相。",
        ),
        (
            "用 Read/Glob/Grep 直接读该业务 Agent 的 workspace 原始配置，而不是仅凭 job input 的摘要推断。",
            "先调用 `HarnessList()` 获取本次 run 绑定的文件清单，再用 `HarnessRead(path)` 读取相关 Harness 文件，而不是"
            "仅凭 job input 的摘要推断。目标 Agent 由 Runtime 的可信 metadata 绑定，不由模型选择。",
        ),
        (
            "├── agent.yaml 中的 workspace_policy          # 权限策略、runtime_middlewares、permission_mode",
            "├── agent.yaml                    # 权限策略、runtime_middlewares、permission_mode",
        ),
        (
            "└── .env                           # 运行环境变量（可读；含密钥时按证据引用，勿在结论里逐字回填）",
            "└── tests/                         # 受治理回归资产（仅在任务需要时读取）",
        ),
        (
            "先用 `Glob` 列 `skills/*/SKILL.md`、`subagents/*.md` 摸清资产清单，再对与本次归因/优化直接相关的文件用 `Read` 读正文。",
            "先调用 `HarnessList()` 摸清实际资产清单，再把返回的完整逻辑路径交给 `HarnessRead(path)`；不要猜测路径，"
            "也不要用 `Read`/`Glob`/`Grep` 访问 `/business-agents`。",
        ),
        (
            "- **密钥**：`.env` 可读用于判断（如占位符未解析、凭据缺失），但结论/建议里引用「存在/缺失/未解析」即可，不必逐字复制密钥值。",
            "- **密钥**：禁止读取 `.env`、credential、token、secret 或任何 Runtime/AgentGov 数据库；凭据状态只能使用后端已脱敏的证据摘要。",
        ),
    )
    for old, new in replacements:
        rewritten = rewritten.replace(old, new)
    return rewritten


def write_bytes_or_rewritten_text(source: Path, target: Path) -> None:
    raw = source.read_bytes()
    try:
        text = raw.decode("utf-8")
    except UnicodeDecodeError:
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_bytes(raw)
        return
    write_text(target, rewrite_text(text))


def write_yaml(path: Path, value: Mapping[str, object]) -> None:
    write_text(path, yaml.safe_dump(dict(value), allow_unicode=True, sort_keys=False, width=120))


def write_json(path: Path, value: object) -> None:
    write_text(path, json.dumps(value, ensure_ascii=False, indent=2, sort_keys=True) + "\n")


def write_text(path: Path, value: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(value, encoding="utf-8")
