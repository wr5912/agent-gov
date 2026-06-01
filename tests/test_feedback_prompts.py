import json

import pytest

from app.runtime.agent_job_runner import AgentJobRunner
from app.runtime.errors import AgentOutputParseError
from app.runtime.prompts.feedback_prompts import attribution_prompt, extract_json_object, proposal_prompt, read_json
from app.runtime.schema_versions import (
    ATTRIBUTION_OUTPUT_SCHEMA_VERSION,
    FEEDBACK_OPTIMIZATION_PLAN_OUTPUT_SCHEMA_VERSION,
    PROPOSAL_OUTPUT_SCHEMA_VERSION,
)


def test_extract_json_object_prefers_expected_schema_version():
    proposal = {
        "schema_version": PROPOSAL_OUTPUT_SCHEMA_VERSION,
        "feedback_case_id": "fbc-test",
        "proposal_job_id": "fbp-test",
        "status": "completed",
        "proposals": [],
        "external_guidance": [],
        "no_action_reason": "没有可执行建议。",
    }
    text = (
        "先看到一个配置片段：\n"
        '```json\n{"permissions":{"allow":["Bash(npm *)"]}}\n```\n'
        "最终输出：\n"
        f"```json\n{json.dumps(proposal, ensure_ascii=False)}\n```"
    )

    parsed = extract_json_object(text, expected_schema_version=PROPOSAL_OUTPUT_SCHEMA_VERSION)

    assert parsed["schema_version"] == PROPOSAL_OUTPUT_SCHEMA_VERSION
    assert parsed["proposal_job_id"] == "fbp-test"


def test_direct_schema_candidate_requires_exact_schema_version():
    schema_like_payload = {
        "feedback_case_id": "fbc-test",
        "proposal_job_id": "fbp-test",
        "status": "completed",
        "proposals": [],
        "external_guidance": [],
        "no_action_reason": "没有可执行建议。",
    }
    text = f"候选对象：\n```json\n{json.dumps(schema_like_payload, ensure_ascii=False)}\n```"

    assert AgentJobRunner.direct_schema_candidate(text, PROPOSAL_OUTPUT_SCHEMA_VERSION) is None


def test_extract_json_object_repairs_markdown_json_candidate():
    text = f"""
以下是完整 JSON：
```json
{{
  "schema_version": "{FEEDBACK_OPTIMIZATION_PLAN_OUTPUT_SCHEMA_VERSION}",
  "batch_id": "fob-test",
  "status": "pending_approval",
  "title": "sec-ops-data 漏洞数据未覆盖2026年",
  "recommendation": "通知 sec-ops-data 工具提供方。",
  "expected_effect": "修复后包含2026年数据。",
  "validation": "复测通过。",
  "risk": "外部系统需要变更。",
  "rationale": "反馈内容"缺少2026年的漏情况"明确指向数据不完整问题。",
  "tasks": [],
  "blocked_items": [
    {{
      "title": "确认并上报漏洞数据源 2026 年数据缺失问题",
      "reason": "需要通知 sec-ops-data 数据维护团队。"
    }}
  ]
}}
```
"""

    parsed = extract_json_object(text, expected_schema_version=FEEDBACK_OPTIMIZATION_PLAN_OUTPUT_SCHEMA_VERSION)

    assert parsed["schema_version"] == FEEDBACK_OPTIMIZATION_PLAN_OUTPUT_SCHEMA_VERSION
    assert parsed["batch_id"] == "fob-test"
    assert "缺少2026年" in parsed["rationale"]


def test_extract_json_object_rejects_empty_agent_output():
    with pytest.raises(AgentOutputParseError, match="empty agent output") as exc_info:
        extract_json_object("  ")

    assert exc_info.value.status_code == 409
    assert exc_info.value.error_code == "AGENT_OUTPUT_PARSE_ERROR"


def test_extract_json_object_rejects_output_without_json_object():
    with pytest.raises(AgentOutputParseError, match="did not contain a JSON object"):
        extract_json_object("这里只有自然语言，没有 JSON 对象。")


def test_read_json_rejects_non_object_payload(tmp_path):
    path = tmp_path / "payload.json"
    path.write_text("[1, 2, 3]", encoding="utf-8")

    with pytest.raises(AgentOutputParseError, match="Expected JSON object"):
        read_json(path)


def test_proposal_prompt_embeds_context_when_available():
    prompt = proposal_prompt(
        "/tmp/input.json",
        input_payload={
            "schema_version": "proposal-input/v1",
            "job_id": "fbp-test",
            "regeneration_instruction": "优先修改 triage-alert skill。",
        },
        attribution_output={
            "schema_version": ATTRIBUTION_OUTPUT_SCHEMA_VERSION,
            "recommended_next_step": "generate_proposal",
        },
    )

    assert "proposal_input_json" in prompt
    assert "attribution_output_json" in prompt
    assert "不要调用工具" in prompt
    assert "regeneration_instruction" in prompt
    assert "不能覆盖 schema、中文输出、证据约束、target_policy 和安全边界" in prompt


def test_attribution_and_proposal_prompts_require_chinese_user_facing_text():
    attribution = attribution_prompt("/tmp/attribution.json")
    proposal = proposal_prompt("/tmp/proposal.json")

    assert "所有面向人的说明文本必须使用简体中文" in attribution
    assert "evidence_refs[].reason" in attribution
    assert "responsibility_boundary.reason" in attribution
    assert "rationale 必须使用简体中文" in attribution
    assert "所有面向人的说明文本必须使用简体中文" in proposal
    assert "proposals[].title/recommendation/expected_effect/validation/risk" in proposal
    assert "external_guidance[].recommendation/reason" in proposal
    assert "no_action_reason 必须使用简体中文" in proposal
