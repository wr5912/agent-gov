import importlib.util
import sys
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[1]
SCRIPT_PATH = REPO_ROOT / "scripts" / "check_stage_language.py"
SPEC = importlib.util.spec_from_file_location("check_stage_language", SCRIPT_PATH)
assert SPEC is not None
assert SPEC.loader is not None
check_stage_language = importlib.util.module_from_spec(SPEC)
sys.modules[SPEC.name] = check_stage_language
SPEC.loader.exec_module(check_stage_language)


def _term(*parts: str) -> str:
    return "".join(parts)


def test_active_stage_version_content_fails(tmp_path: Path) -> None:
    doc = tmp_path / "docs" / "plan.md"
    doc.parent.mkdir(parents=True)
    doc.write_text("Do not call this " + _term("v", "2.7") + ".\n", encoding="utf-8")

    issues = check_stage_language.collect_issues(tmp_path)

    assert len(issues) == 1
    assert issues[0].path == "docs/plan.md"
    assert issues[0].line == 1
    assert issues[0].term == _term("v", "2.7")


def test_archive_stage_version_content_is_allowed(tmp_path: Path) -> None:
    doc = tmp_path / "docs" / "archive" / "old.md"
    doc.parent.mkdir(parents=True)
    doc.write_text("Historical " + _term("pre-", "v", "2.7") + " wording.\n", encoding="utf-8")

    assert check_stage_language.collect_issues(tmp_path) == []


def test_active_stage_version_path_fails(tmp_path: Path) -> None:
    doc = tmp_path / "docs" / ("plan_" + _term("v", "27") + ".md")
    doc.parent.mkdir(parents=True)
    doc.write_text("No forbidden content here.\n", encoding="utf-8")

    issues = check_stage_language.collect_issues(tmp_path)

    assert len(issues) == 1
    assert issues[0].path == "docs/plan_" + _term("v", "27") + ".md"
    assert issues[0].line is None
    assert issues[0].term == _term("v", "27")


def test_clean_active_language_passes(tmp_path: Path) -> None:
    doc = tmp_path / "docs" / "AgentGov_四阶段改进治理工作台UI整改方案.md"
    doc.parent.mkdir(parents=True)
    doc.write_text("# 四阶段改进治理方案\n", encoding="utf-8")

    assert check_stage_language.collect_issues(tmp_path) == []
