from pathlib import Path

from scripts.export_openapi import build_openapi_schema

REPO_ROOT = Path(__file__).resolve().parents[1]


def _read_repo_text(path: str) -> str:
    return (REPO_ROOT / path).read_text(encoding="utf-8")


def test_readme_api_index_uses_current_improvement_and_agent_routes():
    readme = _read_repo_text("README.md")

    deprecated_routes = [
        "/api/feedback-cases/{feedback_case_id}/proposal-jobs",
        "/api/optimization-proposals",
        "/api/feedback-optimization-batches",
        "/api/optimization-tasks",
    ]
    for route in deprecated_routes:
        assert route not in readme

    current_routes = [
        "/api/improvements",
        "/api/improvements/{improvement_id}/attribution/generate",
        "/api/improvements/{improvement_id}/optimization-plan/generate",
        "/api/improvements/{improvement_id}/execution/apply",
        "/api/improvements/{improvement_id}/regression-test-design/generate",
        "/api/langfuse/traces/{trace_id}",
        "/api/agent-change-sets/{change_set_id}/publish",
    ]
    for route in current_routes:
        assert route in readme


def test_openapi_exposes_current_improvement_trace_routes_and_hides_legacy_optimization_chain():
    paths = set(build_openapi_schema()["paths"])

    legacy_paths = {
        "/api/feedback-cases/{feedback_case_id}/proposal-jobs",
        "/api/optimization-proposals",
        "/api/feedback-optimization-batches",
        "/api/optimization-tasks/{optimization_task_id}/execution-jobs",
    }
    assert paths.isdisjoint(legacy_paths)

    current_paths = {
        "/api/improvements/{improvement_id}/attribution/generate",
        "/api/improvements/{improvement_id}/optimization-plan/generate",
        "/api/improvements/{improvement_id}/execution/apply",
        "/api/improvements/{improvement_id}/regression-test-design/generate",
        "/api/langfuse/traces/{trace_id}",
    }
    assert current_paths <= paths


def test_readme_directory_structure_matches_actual_repo_layout():
    readme = _read_repo_text("README.md")
    structure = readme.split("## 目录结构", 1)[1].split("## 快速启动", 1)[0]

    assert "runtime-bootstrap/" in structure
    bootstrap_root = REPO_ROOT / "docker" / "runtime-bootstrap"
    assert bootstrap_root.is_dir()
    for path in (
        "governor-workspace",
        "business-agents/security-operations-expert/workspace",
    ):
        assert (bootstrap_root / path).is_dir(), f"运行卷初始化源缺少 {path}"
    assert not (bootstrap_root / "templates").exists()

    tree_block = structure.split("```text", 1)[1].split("```", 1)[0]
    assert "volume/" not in tree_block
    assert "${HOME}/volume-agent-gov" in structure


def test_deployment_docs_match_optional_ci_discovery_and_degraded_rollback_contract():
    readme = _read_repo_text("README.md")
    runbook = _read_repo_text("docs/engineering/Multica持续CI与联调环境部署.md")

    assert "缺少上述任一证据时必须失败" not in runbook
    assert "无参数调用会解析并打印当前 `origin/master` tip" in runbook
    assert "不得把空字符串当成显式 URL" in runbook
    for text in (readme, runbook):
        assert "WARN" in text
        assert "shared/docker.env" in text
        assert "${HOME}/volume-agent-gov" in text
        assert "不能只改 env 文件后重建容器" in text
        assert "无回滚目标" in text or "无 legacy 回滚目标" in text
