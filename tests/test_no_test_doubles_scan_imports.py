from __future__ import annotations

from pathlib import Path

import pytest
from scripts.no_test_doubles_scan import expand_local_import_closure, scan_paths


def test_dotted_typescript_basename_is_included_in_formal_import_closure(tmp_path: Path) -> None:
    source = tmp_path / "frontend" / "src"
    source.mkdir(parents=True)
    entrypoint = source / "ImprovementWorkbench.tsx"
    helper = source / "improvementWorkbench.helpers.ts"
    unrelated = source / "unrelated.ts"
    entrypoint.write_text('import { value } from "./improvementWorkbench.helpers";\n', encoding="utf-8")
    helper.write_text("globalThis.fetch = replacement;\nexport const value = 1;\n", encoding="utf-8")
    unrelated.write_text("globalThis.fetch = unrelatedReplacement;\n", encoding="utf-8")

    closure = expand_local_import_closure((entrypoint,), repo_root=tmp_path)

    assert closure == (entrypoint.resolve(), helper.resolve())
    assert [(finding.path, finding.rule) for finding in scan_paths(closure, repo_root=tmp_path)] == [
        ("frontend/src/improvementWorkbench.helpers.ts", "replaced browser fetch transport"),
    ]


def test_missing_dotted_typescript_import_still_fails_closed(tmp_path: Path) -> None:
    source = tmp_path / "frontend" / "src"
    source.mkdir(parents=True)
    entrypoint = source / "ImprovementWorkbench.tsx"
    entrypoint.write_text('import { value } from "./improvementWorkbench.helpers";\n', encoding="utf-8")

    with pytest.raises(ValueError, match="formal live JavaScript import cannot be resolved"):
        expand_local_import_closure((entrypoint,), repo_root=tmp_path)
