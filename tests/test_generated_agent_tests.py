from __future__ import annotations

import pytest
from app.services.generated_agent_tests import (
    GeneratedAgentTestError,
    build_generated_agent_test,
    validate_generated_test_code,
)

VALID_TEST = '''
def test_conflicting_evidence_is_reported(agent):
    result = agent.run("两个权威来源结论冲突时如何处置？")
    assert not result.errors
    normalized_text = "".join(result.text.split())
    assert "冲突" in normalized_text
    assert "复核" in normalized_text
'''


def test_build_generated_agent_test_owns_path_and_normalizes_code() -> None:
    candidate = build_generated_agent_test(
        improvement_id="imp-中文/a",
        index=1,
        test_code=VALID_TEST,
        test_intent="验证冲突证据降级",
        assertion_rationale="回答必须显式说明冲突并请求复核",
    )

    assert candidate.target_path.startswith("tests/test_feedback_imp_a_01_")
    assert candidate.target_path.endswith(".py")
    assert candidate.test_code.endswith("\n")
    assert candidate.test_intent == "验证冲突证据降级"


def test_validate_generated_test_code_tracks_a_business_output_alias() -> None:
    validate_generated_test_code(
        "def test_case(agent):\n"
        "    result = agent.run('x')\n"
        "    assert not result.errors\n"
        "    normalized_text = ''.join(result.text.split())\n"
        "    assert '未消解' in normalized_text\n"
        "    assert '低' in normalized_text\n"
    )


def test_validate_generated_test_code_allows_structured_raw_assertions() -> None:
    validate_generated_test_code(
        "def test_case(agent):\n"
        "    result = agent.run('x')\n"
        "    assert not result.errors\n"
        "    assert result.raw['status'] == 'blocked'\n"
    )


def test_validate_generated_test_code_allows_explicit_no_tool_activity_assertion() -> None:
    validate_generated_test_code(
        "def test_case(agent):\n"
        "    result = agent.run('x')\n"
        "    assert not result.errors\n"
        "    assert result.raw['agent_activity']['tool_calls'] == []\n"
        "    normalized_text = ''.join(result.text.split())\n"
        "    assert '未消解' in normalized_text\n"
    )


@pytest.mark.parametrize(
    ("source", "message"),
    [
        ("def helper():\n    return True\n", "top-level test"),
        ("def test_case(agent):\n    result = agent.invoke('x')\n    assert 'x' in result.text\n", "agent.invoke"),
        ("def test_case(agent):\n    result = agent.run('x')\n    assert not result.errors\n", "concrete business outcome"),
        (
            "def test_case(agent):\n    result = agent.run('x')\n    assert not result.errors\n"
            "    assert result.text.strip()\n",
            "concrete business outcome",
        ),
        (
            "def test_case(agent):\n    result = agent.run('x')\n    assert not result.errors\n"
            "    assert True\n",
            "concrete business outcome",
        ),
        ("def test_case():\n    result = agent.run('x')\n    assert 'x' in result.text\n", "agent fixture"),
        ("def test_case(agent):\n    result = agent.run('x')\n    assert 'x' in result.text\n", "result.errors is empty"),
        (
            "def test_case(agent):\n    result = agent.run('x')\n    assert result.errors == []\n"
            "    assert 'x' in result.text\n",
            "result.errors is empty",
        ),
        (
            "def test_case(agent):\n    result = agent.run('x')\n    assert result.errors == ()\n"
            "    assert 'x' in result.text\n",
            "result.errors is empty",
        ),
        (
            "def test_case(agent):\n    result = agent.run('x')\n    assert not result.errors\n"
            "    assert any(word in result.text for word in ('x', 'y'))\n",
            "concrete business outcome",
        ),
        (
            "def test_case(agent):\n    result = agent.run('x')\n    assert not result.errors\n"
            "    assert 'x' in result.text or 'y' in result.text\n",
            "concrete business outcome",
        ),
        ("import requests\ndef test_case(agent):\n    result = agent.run('x')\n    assert 'x' in result.text\n", "unsupported module"),
        (
            "from agentgov_testkit import agent\n"
            "def test_case(agent):\n    result = agent.run('x')\n    assert not result.errors\n"
            "    assert 'x' in result.text\n",
            "injected agent fixture",
        ),
        ("import pytest\n@pytest.mark.skip\ndef test_case(agent):\n    result = agent.run('x')\n    assert 'x' in result.text\n", "skip or xfail"),
        (
            "import pytest\n@pytest.fixture\ndef agent():\n    return object()\n"
            "def test_case(agent):\n    result = agent.run('x')\n    assert 'x' in result.text\n",
            "agent fixture",
        ),
        (
            "def test_one(agent):\n    result = agent.run('x')\n    assert 'x' in result.text\n"
            "def test_two(agent):\n    result = agent.run('y')\n    assert 'y' in result.text\n",
            "exactly one",
        ),
        (
            "def helper(value):\n    return value\n"
            "def test_case(agent):\n    result = agent.run('x')\n    assert not result.errors\n"
            "    assert helper(result.text) == 'x'\n",
            "helper functions",
        ),
        (
            "def test_case(agent):\n    result = agent.run('x')\n    assert not result.errors\n"
            "    if False:\n        assert 'x' in result.text\n",
            "directly in the test body",
        ),
        (
            "def test_case(agent):\n    result = agent.run('x')\n    assert not result.errors\n"
            "    assert result.text == result.text\n",
            "concrete business outcome",
        ),
        (
            "def test_case(agent):\n    result = agent.run('x')\n    assert not result.errors\n"
            "    assert len(result.text) > 0\n",
            "concrete business outcome",
        ),
        (
            "def test_case(agent):\n    result = agent.run('x')\n    assert not result.errors\n"
            "    assert result.text.startswith('')\n",
            "concrete business outcome",
        ),
        (
            "def test_case(agent):\n    result = agent.run('x')\n    assert not result.errors\n"
            "    assert '来源A' in result.text\n",
            "concrete business outcome",
        ),
        (
            "def test_case(agent):\n    result = agent.run('x')\n    text = ''.join(result.text.split())\n"
            "    assert '来源A' in text\n"
            "    assert not result.errors\n",
            "result.errors is empty",
        ),
    ],
)
def test_validate_generated_test_code_rejects_non_executable_or_weak_tests(source: str, message: str) -> None:
    with pytest.raises(GeneratedAgentTestError, match=message):
        validate_generated_test_code(source)


def test_generated_test_rejects_markdown_and_backend_owned_empty_metadata() -> None:
    with pytest.raises(GeneratedAgentTestError, match="Markdown fences"):
        build_generated_agent_test(
            improvement_id="imp-1",
            index=1,
            test_code=f"```python\n{VALID_TEST}\n```",
            test_intent="intent",
            assertion_rationale="reason",
        )


def test_generated_test_rejects_overlong_module() -> None:
    source = VALID_TEST + "\n".join(f"# line {index}" for index in range(61))
    with pytest.raises(GeneratedAgentTestError, match="60 lines"):
        validate_generated_test_code(source)
    with pytest.raises(GeneratedAgentTestError, match="test_intent"):
        build_generated_agent_test(
            improvement_id="imp-1",
            index=1,
            test_code=VALID_TEST,
            test_intent=" ",
            assertion_rationale="reason",
        )
