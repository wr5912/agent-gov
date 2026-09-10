from __future__ import annotations

from pathlib import Path

import pytest
from scripts.check_agentscope_cutover import check_static_cutover


@pytest.mark.parametrize(
    "relative_path",
    (
        "agentgov_harness_digest.py",
        "Makefile",
        "requirements-api.txt",
    ),
)
def test_static_cutover_checks_build_dependency_and_shared_digest_roots(
    tmp_path: Path,
    relative_path: str,
) -> None:
    client = tmp_path / "app" / "runtime_gateway" / "client.py"
    client.parent.mkdir(parents=True)
    client.write_text("class AgentScopeRuntimeClient:\n    pass\n", encoding="utf-8")

    target = tmp_path / relative_path
    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_text("CLAUDE_API_KEY\n", encoding="utf-8")

    findings = check_static_cutover(tmp_path)

    assert any(finding.path == relative_path and finding.code == "legacy_runtime_reference" and "CLAUDE_" in finding.message for finding in findings)
