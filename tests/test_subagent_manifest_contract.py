from __future__ import annotations

import copy
import sys
from pathlib import Path
from typing import Any

import pytest
import yaml
from agentgov_subagent_manifest_policy import validate_subagent_manifest
from agentscope_runtime.subagent_templates import load_subagent_templates
from app.runtime.managed_agent_policy import plan_workspace_policy
from scripts.agentscope_atomic_cutover_bootstrap import _source_candidates

SCRIPTS = Path(__file__).resolve().parents[1] / "scripts"
if str(SCRIPTS) not in sys.path:
    sys.path.insert(0, str(SCRIPTS))

from scripts.check_agentscope_cutover import _check_subagents  # noqa: E402


def test_shared_validator_is_packaged_in_both_images_and_source_snapshot() -> None:
    repo_root = Path(__file__).resolve().parents[1]
    shared_file = repo_root / "agentgov_subagent_manifest_policy.py"

    assert shared_file.is_file()
    assert shared_file in _source_candidates(repo_root)
    for dockerfile_name in ("Dockerfile", "agentscope-runtime.Dockerfile"):
        dockerfile = (repo_root / "docker" / dockerfile_name).read_text(encoding="utf-8")
        dockerignore = (repo_root / "docker" / f"{dockerfile_name}.dockerignore").read_text(encoding="utf-8")
        assert "COPY agentgov_subagent_manifest_policy.py /app/agentgov_subagent_manifest_policy.py" in dockerfile
        assert "agentgov_subagent_manifest_policy.py" not in dockerignore
        assert "**/*.py" not in dockerignore.splitlines()


def _main_manifest() -> dict[str, Any]:
    return {
        "schema_version": 1,
        "agent": {
            "id": "policy-agent",
            "runtime": "agentscope",
            "runtime_contract": "agentscope-app/2.0.8",
            "system_prompt": "AGENT.md",
        },
        "session": {"permission_mode": "default"},
        "workspace_policy": {
            "fail_closed": True,
            "immutable_harness": True,
            "allow_for_run": False,
            "allowed_tools": [],
            "ask_tools": [],
            "denied_tools": [],
        },
    }


def _subagent_manifest() -> dict[str, Any]:
    return {
        "schema_version": 1,
        "agent": {
            "id": "reviewer",
            "description": "Review findings",
            "runtime": "agentscope",
            "runtime_contract": "agentscope-app/2.0.8",
            "system_prompt": "AGENT.md",
        },
        "context_config": {},
        "react_config": {},
        "invite_config": {"invitable": False},
        "session": {"permission_mode": "dont_ask"},
        "workspace_policy": {
            "fail_closed": True,
            "allowed_tools": ["Read(outputs/**)", "TeamSay"],
            "ask_tools": [],
            "denied_tools": [],
        },
    }


def _write_workspace(workspace: Path, subagent_manifest: object) -> None:
    workspace.mkdir(parents=True, exist_ok=True)
    (workspace / "AGENT.md").write_text("# Policy agent\n", encoding="utf-8")
    (workspace / "agent.yaml").write_text(
        yaml.safe_dump(_main_manifest(), sort_keys=False),
        encoding="utf-8",
    )
    subagent = workspace / "subagents" / "reviewer"
    subagent.mkdir(parents=True)
    (subagent / "AGENT.md").write_text("# Reviewer\n", encoding="utf-8")
    (subagent / "agent.yaml").write_text(
        yaml.safe_dump(subagent_manifest, sort_keys=False),
        encoding="utf-8",
    )


def _set_nested(manifest: dict[str, Any], path: tuple[str, ...], value: object) -> None:
    target = manifest
    for key in path[:-1]:
        nested = target[key]
        assert isinstance(nested, dict)
        target = nested
    target[path[-1]] = value


@pytest.mark.parametrize(
    ("path", "value", "managed_code", "cutover_code"),
    [
        (("agent", "id"), "other", "subagent_agent_id", "subagent_contract"),
        (
            ("session", "permission_mode"),
            "default",
            "subagent_permission_mode",
            "subagent_contract",
        ),
        (
            ("workspace_policy", "fail_closed"),
            False,
            "subagent_policy",
            "subagent_policy",
        ),
        (
            ("agent", "system_prompt"),
            "OTHER.md",
            "subagent_system_prompt",
            "subagent_contract",
        ),
        (
            ("react_config", "max_iters"),
            "bad",
            "subagent_contract",
            "subagent_contract",
        ),
    ],
)
def test_managed_cutover_and_runtime_reject_the_same_unsafe_subagent_contract(
    tmp_path: Path,
    path: tuple[str, ...],
    value: object,
    managed_code: str,
    cutover_code: str,
) -> None:
    manifest = copy.deepcopy(_subagent_manifest())
    _set_nested(manifest, path, value)
    _write_workspace(tmp_path, manifest)

    managed = plan_workspace_policy(
        workspace=tmp_path,
        agent_id="policy-agent",
    ).violations
    cutover = _check_subagents(tmp_path)

    assert managed_code in {item.rule_id for item in managed if item.path == "subagents/reviewer/agent.yaml"}
    assert cutover_code in {item.code for item in cutover if item.path == "subagents/reviewer/agent.yaml"}
    with pytest.raises(ValueError, match=managed_code):
        load_subagent_templates(tmp_path, "a" * 64)


@pytest.mark.parametrize(
    ("mutated_policy", "expected_code"),
    [
        ({"allowed_tools": ["Read"]}, "subagent_team_say_required"),
        ({"ask_tools": ["ReviewAction"]}, "subagent_ask_unsupported"),
        (
            {"allowed_tools": ["TeamSay", "mcp__sec_ops__create*"]},
            "wildcard_mcp_permission_forbidden",
        ),
        ({"allowed_tools": ["TeamSay", "TeamSay"]}, "invalid_tool_policy"),
        ({"denied_tools": ["TeamSay"]}, "conflicting_tool_policy"),
    ],
)
def test_shared_validator_rejects_unsafe_worker_tool_policy(
    mutated_policy: dict[str, object],
    expected_code: str,
) -> None:
    manifest = _subagent_manifest()
    policy = manifest["workspace_policy"]
    assert isinstance(policy, dict)
    policy.update(mutated_policy)

    codes = {
        issue.code
        for issue in validate_subagent_manifest(
            manifest,
            directory_name="reviewer",
        )
    }

    assert expected_code in codes


def test_shared_validator_rejects_non_object_manifest() -> None:
    issues = validate_subagent_manifest([], directory_name="reviewer")
    assert [(issue.code, issue.detail) for issue in issues] == [
        ("subagent_contract", "manifest root must be an object"),
    ]


@pytest.mark.parametrize(
    ("path", "value"),
    [
        pytest.param(("context_config",), [], id="context-object"),
        pytest.param(("context_config", "trigger_ratio"), 1.0, id="trigger-ratio-upper-bound"),
        pytest.param(("context_config", "reserve_ratio"), 0.0, id="reserve-ratio-lower-bound"),
        pytest.param(("context_config", "context_buffer_ratio"), 1.1, id="context-buffer-upper-bound"),
        pytest.param(("context_config", "max_image_num"), -1, id="max-image-num-lower-bound"),
        pytest.param(("context_config", "tool_result_limit"), "invalid", id="tool-result-limit-type"),
        pytest.param(("context_config", "compression_prompt"), 1, id="compression-prompt-type"),
        pytest.param(("context_config", "summary_template"), 1, id="summary-template-type"),
        pytest.param(("context_config", "summary_schema"), [], id="summary-schema-type"),
        pytest.param(
            ("context_config", "compression_fallback_to_truncation"),
            [],
            id="compression-fallback-type",
        ),
        pytest.param(("context_config", "compression_tool_enabled"), [], id="compression-tool-type"),
        pytest.param(("react_config",), None, id="react-object"),
        pytest.param(("react_config", "max_iters"), "invalid", id="max-iters-type"),
        pytest.param(
            ("react_config", "structured_output_grace_iters"),
            0,
            id="structured-output-grace-lower-bound",
        ),
        pytest.param(("react_config", "stop_on_reject"), [], id="stop-on-reject-type"),
        pytest.param(("react_config", "interruption_message"), 1, id="interruption-message-type"),
        pytest.param(
            ("react_config", "interruption_raise_cancelled_error"),
            [],
            id="interruption-cancel-type",
        ),
        pytest.param(("invite_config",), {"invitable": True}, id="invitable-true"),
        pytest.param(
            ("invite_config",),
            {"invitable": False, "extra": True},
            id="invite-extra-field",
        ),
    ],
)
def test_managed_cutover_and_runtime_reject_the_same_unsafe_template_config(
    tmp_path: Path,
    path: tuple[str, ...],
    value: object,
) -> None:
    manifest = copy.deepcopy(_subagent_manifest())
    _set_nested(manifest, path, value)
    _write_workspace(tmp_path, manifest)

    managed = plan_workspace_policy(
        workspace=tmp_path,
        agent_id="policy-agent",
    ).violations
    cutover = _check_subagents(tmp_path)

    assert "subagent_contract" in {item.rule_id for item in managed if item.path == "subagents/reviewer/agent.yaml"}
    assert "subagent_contract" in {item.code for item in cutover if item.path == "subagents/reviewer/agent.yaml"}
    with pytest.raises(ValueError, match="subagent_contract"):
        load_subagent_templates(tmp_path, "a" * 64)


def test_shared_validator_accepts_complete_unattended_worker_contract() -> None:
    assert (
        validate_subagent_manifest(
            _subagent_manifest(),
            directory_name="reviewer",
        )
        == ()
    )


def test_managed_and_cutover_require_regular_subagent_prompt(tmp_path: Path) -> None:
    _write_workspace(tmp_path, _subagent_manifest())
    (tmp_path / "subagents" / "reviewer" / "AGENT.md").unlink()

    managed = plan_workspace_policy(
        workspace=tmp_path,
        agent_id="policy-agent",
    ).violations
    cutover = _check_subagents(tmp_path)

    assert any(item.path == "subagents/reviewer/AGENT.md" and item.rule_id == "required_asset_missing" for item in managed)
    assert any(item.path == "subagents/reviewer" and item.code == "subagent_contract" for item in cutover)
