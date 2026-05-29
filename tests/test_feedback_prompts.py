import json

import pytest

from app.runtime.errors import AgentOutputParseError
from app.runtime.prompts.feedback_prompts import attribution_prompt, extract_json_object, proposal_prompt, read_json


def test_extract_json_object_prefers_expected_schema_version():
    proposal = {
        "schema_version": "proposal-output/v1",
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

    parsed = extract_json_object(text, expected_schema_version="proposal-output/v1")

    assert parsed["schema_version"] == "proposal-output/v1"
    assert parsed["proposal_job_id"] == "fbp-test"


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
        attribution_output={"schema_version": "attribution-output/v1", "recommended_next_step": "generate_proposal"},
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
