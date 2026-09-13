from __future__ import annotations


def test_greeting_uses_real_agentscope_runtime(agent) -> None:
    greeting = agent.run("你好")
    followup = agent.run("请用一句话说明你能提供哪些安全运营帮助，不需要查询工具。")
    for result in (greeting, followup):
        assert not result.errors
        assert result.text.strip()
        assert result.run_id
        assert result.session_id
        assert result.agent_version_id == agent.resolved_commit_sha
    assert followup.session_id == greeting.session_id
    assert followup.run_id != greeting.run_id
