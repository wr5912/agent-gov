#!/usr/bin/env python3
from __future__ import annotations

import json
import os
import re
import shlex
import sys
from collections.abc import Mapping
from typing import Protocol

PRIVATE_MAKE_TARGETS = (
    "_container-core-smoke",
    "_container-openapi-check",
    "_container-live-test",
    "_container-workspace-pytest-test",
    "_container-speech-summary-test",
    "_container-health-e2e",
    "_smoke",
    "_ui-smoke",
    "_ui-feedback-smoke",
    "_ui-openai-responses-smoke",
    "_ui-playground-cancel-smoke",
    "_langfuse-smoke",
)
PRIVATE_FRONTEND_SCRIPTS = (
    "verify:real-container:impl",
    "verify:openai-responses-container:impl",
    "verify:provider-health-container:impl",
)
DIRECT_ACCEPTANCE_SCRIPTS = (
    "scripts/container_acceptance_bootstrap.py",
    "scripts/container_acceptance_toolchain.py",
    "scripts/run_container_acceptance.py",
    "scripts/run_healthcheck_container_e2e.sh",
    "scripts/verify_improvement_ui_real_container.mjs",
    "scripts/verify_openai_responses_container.mjs",
    "scripts/verify_provider_health_container.mjs",
    "scripts/verify_speech_summary_container.py",
    "scripts/run_agent_test_container_e2e.py",
)
PRIVATE_PYTHON_MODULES = frozenset(script[:-3].replace("/", ".") for script in DIRECT_ACCEPTANCE_SCRIPTS if script.endswith(".py"))
MAX_COMMAND_CHARS = 32_768
MAX_TOKENS = 256
MAX_RECURSION_DEPTH = 5
_ASSIGNMENT = re.compile(r"^[A-Za-z_][A-Za-z0-9_]*=.*$", re.DOTALL)
_PYTHON = re.compile(r"^python(?:3(?:\.\d+)?)?$")
_SHELL_OPERATORS = frozenset(";&|()\n{}!`")
_SHELL_CONTROL_WORDS = frozenset(
    {
        "case",
        "coproc",
        "do",
        "done",
        "elif",
        "else",
        "esac",
        "fi",
        "for",
        "function",
        "if",
        "in",
        "select",
        "then",
        "until",
        "while",
    }
)
_XARGS_VALUE_OPTIONS = frozenset({"-a", "-d", "-E", "-I", "-L", "-n", "-P", "-s"})
_XARGS_FLAG_OPTIONS = frozenset({"-0", "-o", "-p", "-r", "-t", "-x"})
_UV_RUN_FLAG_OPTIONS = frozenset(
    {
        "--active",
        "--exact",
        "--frozen",
        "--isolated",
        "--locked",
        "--no-dev",
        "--no-editable",
        "--no-project",
        "--no-sync",
        "--offline",
    }
)
_UV_RUN_VALUE_OPTIONS = frozenset(
    {
        "--default-index",
        "--directory",
        "--env-file",
        "--index",
        "--project",
        "--python",
        "--python-platform",
        "--resolution",
        "--with",
        "--with-editable",
        "-p",
    }
)
_TIME_FLAG_OPTIONS = frozenset({"-p", "--portability", "-v", "--verbose"})
_TIME_VALUE_OPTIONS = frozenset({"-f", "--format", "-o", "--output"})
_TIMEOUT_FLAG_OPTIONS = frozenset({"--foreground", "--preserve-status", "--verbose", "-v"})
_TIMEOUT_VALUE_OPTIONS = frozenset({"--kill-after", "--signal", "-k", "-s"})


class TokenUnwrapper(Protocol):
    def __call__(self, tokens: tuple[str, ...]) -> tuple[str, ...]: ...


class _CommandParseError(ValueError):
    pass


def _read_payload() -> Mapping[str, object]:
    raw = sys.stdin.read()
    if not raw.strip():
        return {}
    try:
        payload = json.loads(raw)
    except json.JSONDecodeError:
        return {}
    return payload if isinstance(payload, dict) else {}


def _command_from_payload(payload: Mapping[str, object]) -> str:
    tool_input = payload.get("tool_input")
    if not isinstance(tool_input, dict):
        return ""
    for key in ("command", "cmd"):
        value = tool_input.get(key)
        if isinstance(value, str):
            return value
    return ""


def _program_name(token: str) -> str:
    return os.path.basename(token)


def _tokenize(command: str) -> tuple[str, ...]:
    if len(command) > MAX_COMMAND_CHARS:
        raise _CommandParseError("command is too long")
    lexer = shlex.shlex(command, posix=True, punctuation_chars=";&|()\n{}!`<>")
    lexer.whitespace = " \t\r"
    lexer.whitespace_split = True
    lexer.commenters = ""
    try:
        tokens = tuple(lexer)
    except ValueError as exc:
        raise _CommandParseError("shell tokenization failed") from exc
    if len(tokens) > MAX_TOKENS:
        raise _CommandParseError("command has too many tokens")
    return tokens


def _segments(tokens: tuple[str, ...]) -> tuple[tuple[str, ...], ...]:
    result: list[tuple[str, ...]] = []
    current: list[str] = []
    for token in tokens:
        if token in _SHELL_CONTROL_WORDS or token and set(token) <= _SHELL_OPERATORS:
            if current:
                result.append(tuple(current))
                current = []
            continue
        current.append(token)
    if current:
        result.append(tuple(current))
    return tuple(result)


def _strip_assignments(tokens: tuple[str, ...]) -> tuple[str, ...]:
    index = 0
    while index < len(tokens) and _ASSIGNMENT.fullmatch(tokens[index]) is not None:
        index += 1
    return tokens[index:]


def _unwrap_env_split(tokens: tuple[str, ...], index: int) -> tuple[str, ...] | None:
    token = tokens[index]
    if token in {"-S", "--split-string"}:
        if index + 1 >= len(tokens):
            raise _CommandParseError("env split-string value is missing")
        return (*_tokenize(tokens[index + 1]), *tokens[index + 2 :])
    if not (token.startswith("--split-string=") or token.startswith("-S") and token != "-S"):
        return None
    split_value = token.split("=", 1)[1] if token.startswith("--split-string=") else token[2:]
    if not split_value:
        raise _CommandParseError("env split-string value is missing")
    return (*_tokenize(split_value), *tokens[index + 1 :])


def _unwrap_env(tokens: tuple[str, ...]) -> tuple[str, ...]:
    index = 1
    while index < len(tokens):
        token = tokens[index]
        if token == "--":
            return tokens[index + 1 :]
        if _ASSIGNMENT.fullmatch(token) is not None:
            index += 1
            continue
        split_tokens = _unwrap_env_split(tokens, index)
        if split_tokens is not None:
            return split_tokens
        if token in {"-u", "--unset", "-C", "--chdir"}:
            index += 2
            continue
        if token.startswith(("--unset=", "--chdir=")) or token in {"-i", "--ignore-environment", "-0", "--null"}:
            index += 1
            continue
        if token.startswith("-"):
            raise _CommandParseError("unsupported env option")
        return tokens[index:]
    return ()


def _unwrap_xargs(tokens: tuple[str, ...]) -> tuple[str, ...]:
    index = 1
    while index < len(tokens):
        token = tokens[index]
        if token == "--":
            return tokens[index + 1 :]
        if token in _XARGS_FLAG_OPTIONS or token.startswith("--") and "=" in token:
            index += 1
            continue
        if token in _XARGS_VALUE_OPTIONS:
            index += 2
            continue
        if any(token.startswith(option) and token != option for option in _XARGS_VALUE_OPTIONS):
            index += 1
            continue
        if token.startswith("-"):
            raise _CommandParseError("unsupported xargs option")
        return tokens[index:]
    return ()


def _unwrap_uv_run(tokens: tuple[str, ...]) -> tuple[str, ...]:
    index = 2
    while index < len(tokens):
        token = tokens[index]
        if token == "--":
            return tokens[index + 1 :]
        if token == "-m":
            if index + 1 >= len(tokens):
                raise _CommandParseError("uv run module is missing")
            return ("python", "-m", tokens[index + 1], *tokens[index + 2 :])
        if token in _UV_RUN_FLAG_OPTIONS:
            index += 1
            continue
        if token in _UV_RUN_VALUE_OPTIONS:
            if index + 1 >= len(tokens):
                raise _CommandParseError("uv run option value is missing")
            index += 2
            continue
        if any(token.startswith(f"{option}=") for option in _UV_RUN_VALUE_OPTIONS if option.startswith("--")):
            index += 1
            continue
        if token.startswith("-"):
            raise _CommandParseError("unsupported uv run option")
        return tokens[index:]
    return ()


def _unwrap_exec(tokens: tuple[str, ...]) -> tuple[str, ...]:
    index = 1
    while index < len(tokens):
        token = tokens[index]
        if token == "--":
            return tokens[index + 1 :]
        if token == "-a":
            if index + 1 >= len(tokens):
                raise _CommandParseError("exec argv0 value is missing")
            index += 2
            continue
        if token in {"-c", "-l", "-cl", "-lc"}:
            index += 1
            continue
        if token.startswith("-"):
            raise _CommandParseError("unsupported exec option")
        return tokens[index:]
    return ()


def _unwrap_nohup(tokens: tuple[str, ...]) -> tuple[str, ...]:
    if len(tokens) > 1 and tokens[1] == "--":
        return tokens[2:]
    if len(tokens) > 1 and tokens[1].startswith("-"):
        raise _CommandParseError("unsupported nohup option")
    return tokens[1:]


def _unwrap_timeout(tokens: tuple[str, ...]) -> tuple[str, ...]:
    index = 1
    while index < len(tokens):
        token = tokens[index]
        if token == "--":
            index += 1
            break
        if token in _TIMEOUT_FLAG_OPTIONS:
            index += 1
            continue
        if token in _TIMEOUT_VALUE_OPTIONS:
            if index + 1 >= len(tokens):
                raise _CommandParseError("timeout option value is missing")
            index += 2
            continue
        if any(token.startswith(f"{option}=") for option in _TIMEOUT_VALUE_OPTIONS if option.startswith("--")):
            index += 1
            continue
        if token.startswith(("-k", "-s")) and token not in {"-k", "-s"}:
            index += 1
            continue
        if token.startswith("-"):
            raise _CommandParseError("unsupported timeout option")
        break
    if index >= len(tokens):
        return ()
    index += 1  # duration
    return tokens[index:]


def _unwrap_time(tokens: tuple[str, ...]) -> tuple[str, ...]:
    index = 1
    while index < len(tokens):
        token = tokens[index]
        if token == "--":
            return tokens[index + 1 :]
        if token in _TIME_FLAG_OPTIONS:
            index += 1
            continue
        if token in _TIME_VALUE_OPTIONS:
            if index + 1 >= len(tokens):
                raise _CommandParseError("time option value is missing")
            index += 2
            continue
        if any(token.startswith(f"{option}=") for option in _TIME_VALUE_OPTIONS if option.startswith("--")):
            index += 1
            continue
        if token.startswith("-"):
            raise _CommandParseError("unsupported time option")
        return tokens[index:]
    return ()


def _unwrap_nice(tokens: tuple[str, ...]) -> tuple[str, ...]:
    index = 1
    while index < len(tokens):
        token = tokens[index]
        if token == "--":
            return tokens[index + 1 :]
        if token in {"-n", "--adjustment"}:
            if index + 1 >= len(tokens):
                raise _CommandParseError("nice adjustment is missing")
            index += 2
            continue
        if token.startswith("--adjustment=") or re.fullmatch(r"-\d+", token):
            index += 1
            continue
        if token.startswith("-"):
            raise _CommandParseError("unsupported nice option")
        return tokens[index:]
    return ()


def _script_token_matches(token: str, script: str) -> bool:
    normalized = token[2:] if token.startswith("./") else token
    return normalized == script or normalized.endswith(f"/{script}")


def _python_module_invocation(tokens: tuple[str, ...]) -> tuple[str, tuple[str, ...]] | None:
    index = 1
    while index < len(tokens):
        token = tokens[index]
        if token == "-c":
            if index + 1 >= len(tokens):
                raise _CommandParseError("python command string is missing")
            return "", tokens[index + 2 :]
        if token == "-m":
            if index + 1 >= len(tokens):
                raise _CommandParseError("python module is missing")
            return tokens[index + 1], tokens[index + 2 :]
        if token == "--" or not token.startswith("-"):
            return None
        if token in {"-W", "-X", "--check-hash-based-pycs"}:
            if index + 1 >= len(tokens):
                raise _CommandParseError("python option value is missing")
            index += 2
            continue
        index += 1
    return None


def _uses_private_frontend_script(tokens: tuple[str, ...], *, program: str) -> bool:
    pnpm_tokens = tokens[1:] if program == "corepack" and len(tokens) > 1 and _program_name(tokens[1]) == "pnpm" else tokens
    if not pnpm_tokens or _program_name(pnpm_tokens[0]) != "pnpm":
        return False
    return any(token == "run" and pnpm_tokens[index + 1] in PRIVATE_FRONTEND_SCRIPTS for index, token in enumerate(pnpm_tokens[:-1]))


def _uses_direct_acceptance_script(
    tokens: tuple[str, ...],
    *,
    program: str,
    python_invocation: tuple[str, tuple[str, ...]] | None,
) -> bool:
    if any(_script_token_matches(tokens[0], script) for script in DIRECT_ACCEPTANCE_SCRIPTS):
        return True
    if program in {"bash", "sh"} and any(token == "--noexec" or re.fullmatch(r"-[A-Za-z]*n[A-Za-z]*", token) for token in tokens[1:]):
        return False
    interpreted = program in {"node", "bash", "sh"} or _PYTHON.fullmatch(program) is not None and python_invocation is None
    return interpreted and any(any(_script_token_matches(token, script) for script in DIRECT_ACCEPTANCE_SCRIPTS) for token in tokens[1:])


def _pytest_arguments(
    tokens: tuple[str, ...],
    *,
    program: str,
    python_invocation: tuple[str, tuple[str, ...]] | None,
) -> tuple[str, ...]:
    if program == "pytest":
        return tokens[1:]
    if python_invocation is not None and python_invocation[0] == "pytest":
        return python_invocation[1]
    return ()


def _direct_reason(tokens: tuple[str, ...]) -> str | None:
    program = _program_name(tokens[0])
    if program in {"make", "gmake"} and any(token in PRIVATE_MAKE_TARGETS for token in tokens[1:]):
        return "私有容器验收 Make 目标不能直接调用"
    if _uses_private_frontend_script(tokens, program=program):
        return "真实容器前端 :impl 脚本不能直接调用"
    python_invocation = _python_module_invocation(tokens) if _PYTHON.fullmatch(program) is not None else None
    if python_invocation is not None and python_invocation[0] in PRIVATE_PYTHON_MODULES:
        return "真实容器验收 Python 模块不能绕过公共 Make 入口"
    if _uses_direct_acceptance_script(tokens, program=program, python_invocation=python_invocation):
        return "真实容器验收脚本不能绕过公共 Make 入口"
    pytest_args = _pytest_arguments(tokens, program=program, python_invocation=python_invocation)
    if pytest_args and any(
        token == "tests/test_live_runtime_acceptance.py" or token.startswith("tests/test_live_runtime_acceptance.py::") for token in pytest_args
    ):
        return "live pytest 必须通过 make container-live-test"
    return None


_WRAPPER_UNWRAPPERS: Mapping[str, TokenUnwrapper] = {
    "env": _unwrap_env,
    "exec": _unwrap_exec,
    "nice": _unwrap_nice,
    "nohup": _unwrap_nohup,
    "time": _unwrap_time,
    "timeout": _unwrap_timeout,
    "xargs": _unwrap_xargs,
}


def _strip_shell_builtin_wrappers(tokens: tuple[str, ...]) -> tuple[str, ...]:
    while tokens and _program_name(tokens[0]) in {"builtin", "command"}:
        program = _program_name(tokens[0])
        tokens = tokens[1:]
        allowed = {"--"} if program == "builtin" else {"-p", "--"}
        while tokens and tokens[0] in allowed:
            tokens = tokens[1:]
        tokens = _strip_assignments(tokens)
    return tokens


def _unwrap_known_command(tokens: tuple[str, ...]) -> tuple[str, ...] | None:
    program = _program_name(tokens[0])
    if program == "uv" and len(tokens) > 1 and tokens[1] == "run":
        return _unwrap_uv_run(tokens)
    unwrapper = _WRAPPER_UNWRAPPERS.get(program)
    return unwrapper(tokens) if unwrapper is not None else None


def _nested_shell_command(tokens: tuple[str, ...]) -> str | None:
    if _program_name(tokens[0]) not in {"bash", "sh"}:
        return None
    for index, token in enumerate(tokens[1:], start=1):
        if re.fullmatch(r"-[A-Za-z]*c[A-Za-z]*", token):
            if index + 1 >= len(tokens):
                raise _CommandParseError("shell command string is missing")
            return tokens[index + 1]
    return None


def _has_unsafe_leading_redirection(tokens: tuple[str, ...]) -> bool:
    index = 1 if len(tokens) > 1 and tokens[0].isdigit() else 0
    if index >= len(tokens):
        return False
    operator = tokens[index]
    return bool(operator) and set(operator) <= {"<", ">"}


def _reason_for_tokens(tokens: tuple[str, ...], *, depth: int) -> str | None:
    if depth > MAX_RECURSION_DEPTH:
        raise _CommandParseError("wrapper recursion is too deep")
    tokens = _strip_shell_builtin_wrappers(_strip_assignments(tokens))
    if not tokens:
        return None
    if _has_unsafe_leading_redirection(tokens) and _mentions_acceptance_keyword(" ".join(tokens)):
        raise _CommandParseError("leading redirection cannot be proven safe")
    if any(("$(" in token or "`" in token) and _mentions_acceptance_keyword(token) for token in tokens):
        raise _CommandParseError("embedded shell command cannot be proven safe")
    unwrapped = _unwrap_known_command(tokens)
    if unwrapped is not None:
        return _reason_for_tokens(_strip_assignments(unwrapped), depth=depth + 1)
    nested = _nested_shell_command(tokens)
    if nested is not None:
        return _reason_for_command(nested, depth=depth + 1)
    return _direct_reason(tokens)


def _reason_for_command(command: str, *, depth: int) -> str | None:
    for segment in _segments(_tokenize(command)):
        reason = _reason_for_tokens(segment, depth=depth)
        if reason is not None:
            return reason
    return None


def _mentions_acceptance_keyword(command: str) -> bool:
    keywords = (
        *PRIVATE_MAKE_TARGETS,
        *PRIVATE_FRONTEND_SCRIPTS,
        *DIRECT_ACCEPTANCE_SCRIPTS,
        *PRIVATE_PYTHON_MODULES,
        "tests/test_live_runtime_acceptance.py",
    )
    return any(keyword in command for keyword in keywords)


def bypass_reason(command: str) -> str | None:
    try:
        return _reason_for_command(command, depth=0)
    except _CommandParseError:
        if _mentions_acceptance_keyword(command):
            return "容器验收命令无法安全解析，必须通过公共 Make 入口"
        return None


def main() -> int:
    payload = _read_payload()
    tool_name = payload.get("tool_name")
    if tool_name not in {None, "Bash"}:
        return 0
    reason = bypass_reason(_command_from_payload(payload))
    if reason is None:
        return 0
    print(f"{reason}；公共入口会先重建镜像、force-recreate 服务并验证最新配置。", file=sys.stderr)
    return 2


if __name__ == "__main__":
    raise SystemExit(main())
