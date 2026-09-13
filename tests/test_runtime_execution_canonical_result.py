from __future__ import annotations

import pytest
from app.runtime_gateway._execution_support import _governed_evidence_root
from app.runtime_gateway.store import RuntimeStateConflict


def test_governed_evidence_root_accepts_exact_backend_owned_agent_path() -> None:
    job_input = {
        "target_agent_context": {
            "agent_id": "soc-ops",
            "workspace_dir": "/business-agents/soc-ops/workspace",
        },
    }

    assert _governed_evidence_root(job_input) == "/business-agents/soc-ops/workspace"


@pytest.mark.parametrize(
    "job_input",
    [
        {"target_agent_context": {"workspace_dir": "/runtime-workspaces/governor"}},
        {"target_agent_context": {"workspace_dir": "/business-agents/../workspace"}},
        {
            "target_agent_context": {
                "agent_id": "other",
                "workspace_dir": "/business-agents/soc-ops/workspace",
            },
        },
        {"target_agent_context": {"workspace_dir": 7}},
    ],
)
def test_governed_evidence_root_rejects_untrusted_or_mismatched_paths(
    job_input: dict[str, object],
) -> None:
    with pytest.raises(RuntimeStateConflict):
        _governed_evidence_root(job_input)


@pytest.mark.parametrize("job_input", [{}, {"target_agent_context": {}}, {"target_agent_context": "invalid"}])
def test_governed_evidence_root_is_absent_without_a_workspace(job_input: dict[str, object]) -> None:
    assert _governed_evidence_root(job_input) is None
