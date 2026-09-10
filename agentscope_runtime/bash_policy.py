"""Bash 的无隐式输入命令子集；不解释任意 shell 或 jq 程序。"""

from __future__ import annotations

import json
import re
import shlex
from dataclasses import dataclass

import tree_sitter_bash
from tree_sitter import Language, Node, Parser

_BASH_LANGUAGE = Language(tree_sitter_bash.language())
_BARE_ARGUMENT = re.compile(r"[A-Za-z0-9_./%+=:@,-]+")
_JQ_SHORT_OPTIONS = frozenset("ncrajSMe")
_JQ_LONG_OPTIONS = frozenset(
    {
        "--null-input",
        "--compact-output",
        "--raw-output",
        "--ascii-output",
        "--join-output",
        "--sort-keys",
        "--monochrome-output",
        "--exit-status",
        "--tab",
    }
)
_DATE_OPTIONS = frozenset({"--utc", "--universal", "--rfc-email", "-I", "--iso-8601"})
_DATE_RESOLUTIONS = frozenset({"date", "hours", "minutes", "seconds", "ns"})


@dataclass(frozen=True)
class SafeBashCommand:
    """已证明没有文件/stdin/环境输入的命令及其显式写目录。"""

    arguments: tuple[str, ...]
    directory_paths: tuple[str, ...] = ()


def parse_safe_bash(command: object) -> SafeBashCommand | None:
    if not isinstance(command, str) or not command.strip() or len(command) > 65_536 or "\0" in command:
        return None
    try:
        encoded = command.encode("utf-8")
    except UnicodeEncodeError:
        return None
    tree = Parser(_BASH_LANGUAGE).parse(encoded)
    root = tree.root_node
    if root.has_error or len(root.children) != 1 or root.children[0].type != "command":
        return None
    nodes = root.children[0].named_children
    if not nodes or nodes[0].type != "command_name":
        return None
    name = _literal_argument(nodes[0])
    values = [_literal_argument(node) for node in nodes[1:]]
    if name is None or any(value is None for value in values):
        return None
    arguments = tuple(value for value in values if value is not None)
    if name == "pwd" and not arguments:
        return SafeBashCommand((name,))
    if name == "date" and _safe_date(arguments):
        return SafeBashCommand((name, *arguments))
    if name == "jq" and _safe_jq(arguments):
        return SafeBashCommand((name, *arguments))
    if name == "mkdir":
        paths = _mkdir_paths(arguments)
        if paths:
            return SafeBashCommand((name, *arguments), paths)
    return None


def _literal_argument(node: Node) -> str | None:
    if node.type == "command_name":
        return _literal_argument(node.named_children[0]) if len(node.named_children) == 1 else None
    raw = node.text.decode("utf-8") if node.text is not None else ""
    if node.type == "word":
        # 未引用的 glob、brace/tilde/参数展开、转义和 shell 元字符不属于字面量。
        return raw if _BARE_ARGUMENT.fullmatch(raw) else None
    if node.type not in {"raw_string", "string"}:
        return None
    if node.type == "string" and any(child.type != "string_content" for child in node.named_children):
        return None
    try:
        values = shlex.split(raw)
    except ValueError:
        return None
    return values[0] if len(values) == 1 else None


def _safe_date(arguments: tuple[str, ...]) -> bool:
    for index, argument in enumerate(arguments):
        if argument == "--":
            remaining = arguments[index + 1 :]
            return not remaining or (len(remaining) == 1 and remaining[0].startswith("+"))
        if argument.startswith("+"):
            return index == len(arguments) - 1
        if argument in _DATE_OPTIONS or re.fullmatch(r"-[uR]+", argument):
            continue
        if argument.startswith("-I") and argument[2:] in _DATE_RESOLUTIONS:
            continue
        if argument.startswith("--iso-8601=") and argument.partition("=")[2] in _DATE_RESOLUTIONS:
            continue
        if argument.startswith("--rfc-3339=") and argument.partition("=")[2] in {"date", "seconds", "ns"}:
            continue
        return False
    return True


def _safe_jq(arguments: tuple[str, ...]) -> bool:
    null_input = False
    for index, argument in enumerate(arguments):
        if argument == "--":
            return null_input and len(arguments) == index + 2 and _json_literal(arguments[index + 1])
        if argument in _JQ_LONG_OPTIONS:
            null_input = null_input or argument == "--null-input"
            continue
        if argument.startswith("-"):
            if argument.startswith("--") or len(argument) == 1 or not set(argument[1:]).issubset(_JQ_SHORT_OPTIONS):
                return False
            null_input = null_input or "n" in argument[1:]
            continue
        return null_input and index == len(arguments) - 1 and _json_literal(argument)
    return False


def _json_literal(value: str) -> bool:
    if value == ".":
        return True
    try:
        json.loads(value, parse_constant=_reject_non_json_constant)
    except (ValueError, RecursionError):
        return False
    return True


def _reject_non_json_constant(_: str) -> None:
    raise ValueError("jq accepts only strict JSON literals")


def _mkdir_paths(arguments: tuple[str, ...]) -> tuple[str, ...]:
    paths: list[str] = []
    options = True
    for argument in arguments:
        if options and argument == "--":
            options = False
        elif options and argument in {"-p", "--parents"}:
            continue
        elif not argument or (options and argument.startswith("-")):
            return ()
        else:
            paths.append(argument)
    return tuple(paths)
