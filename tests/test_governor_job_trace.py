import asyncio
from contextlib import nullcontext

from app.runtime.governor_job_trace import governor_trace_attributes, run_governor_profile_json


def test_governor_trace_attributes_case_scope_tags_and_name():
    attrs = governor_trace_attributes(job_type="attribution", scope_kind="feedback_case", scope_id="fc-1", job_id="job-1")
    assert attrs["session_id"] == "case:fc-1"
    assert attrs["trace_name"] == "runtime.governor.attribution"
    assert attrs["user_id"] == "system:governor"
    assert "role:governance" in attrs["tags"]
    assert "agent:governor" in attrs["tags"]
    assert "job_type:attribution" in attrs["tags"]
    assert attrs["metadata"]["job_id"] == "job-1"
    assert attrs["metadata"]["scope_kind"] == "feedback_case"


def test_governor_trace_attributes_improvement_scope_uses_improvement_prefix():
    attrs = governor_trace_attributes(
        job_type="optimization_plan",
        scope_kind="improvement",
        scope_id="imp-1",
        job_id="j",
    )
    assert attrs["session_id"] == "improvement:imp-1"


def test_governor_trace_attributes_falls_back_to_job_id_session():
    attrs = governor_trace_attributes(job_type="eval_case_generation", scope_kind="", scope_id="", job_id="job-9")
    assert attrs["session_id"] == "job:job-9"


class _FakeLangfuse:
    def __init__(self, enabled: bool):
        self.settings = type("S", (), {"langfuse_enabled": enabled})()
        self.propagations: list[dict] = []
        self.trace_attrs: list[dict] = []

    def propagate_attributes(self, **kwargs):
        self.propagations.append(kwargs)
        return nullcontext()

    def start_observation(self, **kwargs):
        return nullcontext(None)

    def set_trace_attributes(self, observation, **kwargs):
        self.trace_attrs.append(kwargs)


def test_run_governor_profile_json_enriches_when_enabled():
    lf = _FakeLangfuse(enabled=True)
    ran = {"value": False}

    async def run():
        ran["value"] = True
        return "output"

    governor = {"job_type": "attribution", "scope_kind": "feedback_case", "scope_id": "fc", "job_id": "j"}
    out = asyncio.run(run_governor_profile_json(lf, run, governor))

    assert out == "output"
    assert ran["value"] is True
    assert lf.propagations[0]["trace_name"] == "runtime.governor.attribution"
    assert lf.propagations[0]["session_id"] == "case:fc"
    assert "role:governance" in lf.propagations[0]["tags"]
    # set_trace_attributes 也写入了 tags（otel 边界），便于按 role/agent 过滤。
    assert "role:governance" in lf.trace_attrs[0]["tags"]


def test_run_governor_profile_json_skips_when_disabled_or_no_governor():
    disabled = _FakeLangfuse(enabled=False)
    enabled = _FakeLangfuse(enabled=True)

    async def run():
        return "output"

    # Langfuse 关闭：直接执行、不富化。
    assert asyncio.run(run_governor_profile_json(disabled, run, {"job_type": "attribution"})) == "output"
    assert disabled.propagations == []
    # 无 governor 上下文（业务路径）：不富化。
    assert asyncio.run(run_governor_profile_json(enabled, run, None)) == "output"
    assert enabled.propagations == []
