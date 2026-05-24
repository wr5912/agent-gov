import json

from app.runtime.feedback_jobs import extract_json_object, proposal_prompt


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


def test_proposal_prompt_embeds_context_when_available():
    prompt = proposal_prompt(
        "/tmp/input.json",
        input_payload={"schema_version": "proposal-input/v1", "job_id": "fbp-test"},
        attribution_output={"schema_version": "attribution-output/v1", "recommended_next_step": "generate_proposal"},
    )

    assert "proposal_input_json" in prompt
    assert "attribution_output_json" in prompt
    assert "不要调用工具" in prompt
