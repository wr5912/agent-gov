from __future__ import annotations

import json
from pathlib import Path

import yaml

WORKSPACE = Path(__file__).resolve().parents[1]


def test_agentscope_manifest_and_assets_are_bound() -> None:
    manifest = yaml.safe_load((WORKSPACE / "agent.yaml").read_text(encoding="utf-8"))
    assert manifest["schema_version"] == 1
    assert manifest["agent"]["runtime"] == "agentscope"
    assert manifest["agent"]["runtime_contract"] == "agentscope-app/2.0.8"
    policy = manifest["workspace_policy"]
    assert manifest["agent"]["id"] == "security-operations-expert"
    assert policy["immutable_harness"] is True
    assert manifest["paths"]["outputs"] == "/workspace/outputs"
    assert manifest["paths"]["workspace"] == "/workspace"
    assert manifest["paths"]["data_root"] == "/workspace/data"
    assert all("/runtime-data" not in rule for rule in policy["allowed_tools"])
    assert all("/runtime-data" not in path for path in policy["writable_paths"])
    for group in ("skills", "mcps", "subagents"):
        for asset in manifest["harness"][group]:
            assert (WORKSPACE / asset["path"]).exists()


def test_conversion_report_is_complete() -> None:
    report = json.loads((WORKSPACE / "conversion-report.json").read_text(encoding="utf-8"))
    assert report["source_coverage_percent"] == 100.0
    assert report["rejected_count"] == 0
    assert report["source_file_count"] == report["mapped_count"] + report["retired_count"]
