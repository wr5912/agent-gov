from __future__ import annotations

import asyncio
import json
import shlex
import shutil
import subprocess
from pathlib import Path
from types import SimpleNamespace

import pytest
from agentscope.permission import PermissionBehavior, PermissionContext, PermissionDecision, PermissionMode
from agentscope.tool import Bash
from agentscope_runtime.policy_middleware import AgentGovPolicyMiddleware


@pytest.fixture
def policy(tmp_path: Path) -> AgentGovPolicyMiddleware:
    manifest = {
        "session": {"permission_mode": "default"},
        "runtime_middlewares": [{"type": "policy_guard"}],
        "workspace_policy": {
            "fail_closed": True,
            "immutable_harness": True,
            "allowed_tools": ["Bash(*)"],
            "denied_tools": [],
            "denied_read_paths": [".env", "**/.env", "**/*credential*"],
            "immutable_paths": ["AGENT.md", "agent.yaml", "skills/**", "mcp/**", "subagents/**"],
            "writable_paths": ["outputs/**"],
            "allowed_network_domains": [],
            "sandbox": {"enabled": True, "fail_if_unavailable": True, "allow_unsandboxed_commands": False},
        },
    }
    (tmp_path / "agent.yaml").write_text(json.dumps(manifest), encoding="utf-8")
    return AgentGovPolicyMiddleware(tmp_path)


def decision(policy: AgentGovPolicyMiddleware, command: str) -> PermissionDecision:
    async def next_handler(**_: object) -> PermissionDecision:
        raise AssertionError("策略必须在执行 Bash 前完成判定")

    agent = SimpleNamespace(state=SimpleNamespace(permission_context=PermissionContext(mode=PermissionMode.DEFAULT)))
    return asyncio.run(policy.on_check_permission(agent, {"tool": Bash(), "tool_input": {"command": command}}, next_handler))


@pytest.mark.parametrize("arguments", [("jq", "-n", "-R", "inputs", ".env"), ("date", "-f", ".env")])
def test_bash_file_read_canary_is_denied_before_execution(policy: AgentGovPolicyMiddleware, tmp_path: Path, arguments: tuple[str, ...]) -> None:
    executable = shutil.which(arguments[0])
    if executable is None:
        pytest.skip(f"需要本机 {arguments[0]} 验证合成文件读取")
    canary = "AGENTGOV_SYNTHETIC_DENIED_FILE_CANARY"
    (tmp_path / ".env").write_text(canary + "\n", encoding="utf-8")
    completed = subprocess.run([executable, *arguments[1:]], cwd=tmp_path, capture_output=True, text=True, check=False, timeout=5)
    assert canary in completed.stdout + completed.stderr
    assert decision(policy, " ".join(arguments)).behavior is PermissionBehavior.DENY


@pytest.mark.parametrize(
    "command",
    [
        "jq -n -R inputs .env",
        "jq -n -R inputs -",
        "jq -n -R inputs /proc/self/environ",
        "jq -n -f .env",
        "jq -nf.env",
        "jq -nRf.env",
        "jq -n --from-file=.env",
        "jq -n --from=.env",
        "jq -n --rawfile x .env null",
        "jq -n --rawfile=x .env null",
        "jq -n --slurpfile x .env null",
        "jq -n --argfile x .env null",
        "jq -n -L. 'include \"payload\";'",
        "jq -n 'include \"payload\";'",
        "jq -n 'import \"payload\" as x; x'",
        "jq -n env",
        "jq -n '$ENV'",
        "jq -n input",
        "jq -n inputs",
        "jq -n '.[input]'",
        "jq -n -- 'inputs' .env",
        "jq -n -- --rawfile",
        "jq -n null -- .env",
        "jq --null-input null .env",
        "jq -n 'null' --args .env",
        "jq -n --jsonargs . .env",
        "jq -n --run-tests .env",
        "jq -n --debug-dump-disasm env",
        "jq -n --unknown null",
        "jq -nR null",
        "jq -n",
        "jq null",
        "jq --slurp null",
        "jq -n NaN",
        "jq -n Infinity",
        "jq -n '1+2'",
        "jq -n '\"\\(env)\"'",
        "date -f .env",
        "date -f.env",
        "date -uf.env",
        "date --file=.env",
        "date --file .env",
        "date --fi=.env",
        "date -r .env",
        "date -r.env",
        "date --reference=.env",
        "date --reference .env",
        "date --ref=.env",
        "date --set now",
        "date -usnow",
        "date --date yesterday",
        "date --unknown",
        "date -- -f .env",
        "date +%s .env",
        "date +%s --file=.env",
        "date --iso-8601=invalid",
        "mkdir -p --mode=.env outputs/result",
        "mkdir -pm700 outputs/result",
        "mkdir --reference=.env outputs/result",
        "mkdir --parents=.env outputs/result",
        "mkdir -p",
        "mkdir -p --",
        "mkdir -p ../outside",
        "mkdir -p outputs/result /tmp/outside",
        "pwd --file=.env",
        "pwd --",
        "pwd .env",
        "date +%s; pwd",
        "date +%s\npwd",
        "date +%s && pwd",
        "date +%s | jq -R .",
        "date +%s > outputs/time",
        "jq -n null < .env",
        "jq -n 'input' <<< secret",
        "jq -n 'input' <(pwd)",
        "jq -n null 2>&1",
        "jq -n null &",
        "jq -n null # comment",
        "PATH=/tmp jq -n null",
        "command jq -n null",
        "env jq -n null",
        "$(pwd)",
        "`pwd`",
        'jq -n "$ENV"',
        'jq -n "$(pwd)"',
        "mkdir -p outputs/*",
        "mkdir -p outputs/?",
        "mkdir -p outputs/[ab]",
        "mkdir -p outputs/{a,b}",
        "mkdir -p ~/outputs/result",
        "mkdir -p outputs/$name",
        "mkdir -p outputs/$((1+2))",
        "jq -n $'null'",
        "jq -n 'nu'\"ll\"",
        "jq -n 'null",
        "jq -n null\0",
        "jq -n '\ud800'",
    ],
)
def test_bash_implicit_inputs_unknown_options_and_shell_programs_fail_closed(policy: AgentGovPolicyMiddleware, command: str) -> None:
    assert decision(policy, command).behavior is PermissionBehavior.DENY


@pytest.mark.parametrize(
    "command",
    [
        "pwd",
        "date",
        "date +%s",
        "date -u '+%Y-%m-%d %H:%M:%S'",
        "date -uR",
        "date --utc --rfc-email",
        "date -Iseconds",
        "date --iso-8601=ns",
        "date --rfc-3339=seconds",
        "date -- +%s",
        "jq -n null",
        "jq --null-input .",
        "jq -nc '{\"value\":true}'",
        "jq -n --sort-keys --compact-output '{\"value\":[1,null,false]}'",
        "jq -ncr '42'",
        "jq --null-input --ascii-output --tab -- '[1,2]'",
        "jq -n -- '-7'",
        "jq -n '\"env input inputs import include --rawfile -f .env\"'",
        'jq -n \'{"env":"$ENV", "input":"$(cat .env)", "include":"file", "import":"*?[a]"}\'',
        "mkdir -p outputs/result",
        "mkdir --parents outputs/first outputs/second",
        "mkdir -p -- outputs/result",
        "mkdir -p 'outputs/folder with spaces'",
    ],
)
def test_bash_documented_literal_subset_remains_allowed(policy: AgentGovPolicyMiddleware, command: str) -> None:
    assert decision(policy, command).behavior is PermissionBehavior.ALLOW


def test_mkdir_resolved_directory_cannot_escape_writable_paths(policy: AgentGovPolicyMiddleware, tmp_path: Path) -> None:
    outputs = tmp_path / "outputs"
    outputs.mkdir()
    (outputs / "redirect").symlink_to(tmp_path, target_is_directory=True)
    assert decision(policy, "mkdir -p outputs/redirect/not-writable").behavior is PermissionBehavior.DENY


def test_jq_literal_keywords_do_not_execute_programs_or_consume_environment_and_stdin(policy: AgentGovPolicyMiddleware, tmp_path: Path) -> None:
    if shutil.which("jq") is None:
        pytest.skip("需要本机 jq 验证字面量执行语义")
    payload = {"env": "$ENV", "input": "inputs", "import": "include", "shell": "$(cat .env)", "escape": "\\(env)"}
    command = shlex.join(("jq", "-n", json.dumps(payload)))
    assert decision(policy, command).behavior is PermissionBehavior.ALLOW
    canary = "AGENTGOV_SYNTHETIC_IMPLICIT_INPUT_CANARY"
    (tmp_path / ".env").write_text(canary, encoding="utf-8")
    completed = subprocess.run(
        ["/bin/bash", "--noprofile", "--norc", "-c", command],
        cwd=tmp_path,
        input=canary,
        env={"PATH": "/usr/bin:/bin", "LANG": "C", "AGENTGOV_TEST_CANARY": canary},
        capture_output=True,
        text=True,
        check=True,
        timeout=5,
    )
    assert json.loads(completed.stdout) == payload
    assert canary not in completed.stdout + completed.stderr
