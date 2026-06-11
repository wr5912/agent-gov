from __future__ import annotations

import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
SCRIPTS = ROOT / "scripts"
if str(SCRIPTS) not in sys.path:
    sys.path.insert(0, str(SCRIPTS))

from check_orphan_tests import collect_orphan_issues, main  # noqa: E402


def _write(path: Path, text: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(text, encoding="utf-8")


def _scaffold(root: Path) -> None:
    _write(root / "app" / "__init__.py", "from app.runtime.settings import AppSettings\n\nCONST = 1\n\n\ndef live():\n    return 1\n")
    _write(root / "app" / "runtime" / "__init__.py", "")
    _write(root / "app" / "runtime" / "settings.py", "class AppSettings:\n    pass\n")
    _write(root / "scripts" / "util.py", "def real():\n    return 1\n")


def test_valid_imports_have_no_issues(tmp_path: Path) -> None:
    _scaffold(tmp_path)
    _write(
        tmp_path / "tests" / "test_ok.py",
        "from app import live, CONST, AppSettings\nfrom app.runtime import settings\nfrom scripts.util import real\nimport app.runtime.settings\n",
    )

    assert collect_orphan_issues(tmp_path) == []


def test_missing_symbol_and_module_are_flagged(tmp_path: Path) -> None:
    _scaffold(tmp_path)
    _write(tmp_path / "tests" / "test_bad.py", "from app import gone\nfrom app.runtime import absent_submodule\nimport scripts.removed\n")

    messages = [issue.message for issue in collect_orphan_issues(tmp_path)]

    assert "imports `gone` not defined in `app`" in messages
    assert "imports `absent_submodule` not defined in `app.runtime`" in messages
    assert "imports missing module `scripts.removed`" in messages


def test_star_import_target_is_skipped(tmp_path: Path) -> None:
    _scaffold(tmp_path)
    _write(tmp_path / "app" / "wild.py", "from os import *\n")
    _write(tmp_path / "tests" / "test_wild.py", "from app.wild import anything\n")

    assert collect_orphan_issues(tmp_path) == []


def test_third_party_and_relative_imports_ignored(tmp_path: Path) -> None:
    _scaffold(tmp_path)
    _write(tmp_path / "tests" / "test_ext.py", "import os\nfrom collections import OrderedDict\nfrom .helper import thing\n")

    assert collect_orphan_issues(tmp_path) == []


def test_tuple_and_annotated_assignments_count_as_defined(tmp_path: Path) -> None:
    _scaffold(tmp_path)
    _write(tmp_path / "app" / "shapes.py", "A, B = 1, 2\nC: int = 3\n")
    _write(tmp_path / "tests" / "test_shapes.py", "from app.shapes import A, B, C\n")

    assert collect_orphan_issues(tmp_path) == []


def test_main_reports_and_returns_codes(tmp_path: Path, capsys) -> None:
    _scaffold(tmp_path)
    _write(tmp_path / "tests" / "test_ok.py", "from app import live\n")
    assert main(["--root", str(tmp_path)]) == 0
    assert "OK: no orphan tests found." in capsys.readouterr().out

    _write(tmp_path / "tests" / "test_bad.py", "from app import gone\n")
    assert main(["--root", str(tmp_path)]) == 1
    assert "FAIL: tests/test_bad.py: imports `gone` not defined in `app`" in capsys.readouterr().out
