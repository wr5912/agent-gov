from __future__ import annotations

import json
import subprocess
import sys
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parents[1]
SCRIPTS_DIR = REPO_ROOT / "scripts"
sys.path.insert(0, str(SCRIPTS_DIR))

from check_pr_aid import (  # noqa: E402
    extract_aid_identifiers,
    validate_pull_request_metadata,
)


def test_extract_aid_normalizes_case_and_deduplicates_occurrences() -> None:
    assert extract_aid_identifiers(
        "aid-16-release-controller",
        "AID-016: PAT-only staging",
        "Implements aid-16",
    ) == ["AID-16"]


def test_validate_pull_request_metadata_uses_head_branch_as_stable_authority() -> None:
    assert (
        validate_pull_request_metadata(
            "aid-16-work",
            "AID-016: keep CI visible",
            "Implements aid-16",
        )
        == "AID-16"
    )
    assert validate_pull_request_metadata("aid-16-work", "No identifier here", "") == "AID-16"


@pytest.mark.parametrize(
    ("head_ref", "title", "body"),
    [
        ("feature/no-trace", "AID-16 exists only in title", ""),
        ("aid-16-work", "AID-17 wrong task", ""),
        ("aid-16-aid-17-work", "conflicting branch", ""),
        ("AID-16x", "not a boundary", ""),
    ],
)
def test_validate_pull_request_metadata_rejects_missing_or_ambiguous_ids(
    head_ref: str,
    title: str,
    body: str,
) -> None:
    with pytest.raises(ValueError, match="head branch|must match"):
        validate_pull_request_metadata(head_ref, title, body)


def test_cli_skips_push_event_and_validates_pull_request_event(tmp_path: Path) -> None:
    event_file = tmp_path / "event.json"
    event_file.write_text(json.dumps({"ref": "refs/heads/master"}), encoding="utf-8")
    skipped = subprocess.run(
        [sys.executable, str(SCRIPTS_DIR / "check_pr_aid.py"), "--event-file", str(event_file)],
        check=False,
        capture_output=True,
        text=True,
    )
    assert skipped.returncode == 0
    assert "not a pull request" in skipped.stdout

    event_file.write_text(
        json.dumps(
            {
                "pull_request": {
                    "head": {"ref": "aid-16-controller"},
                    "title": "PAT-only staging",
                    "body": "Tracks AID-16",
                }
            }
        ),
        encoding="utf-8",
    )
    validated = subprocess.run(
        [sys.executable, str(SCRIPTS_DIR / "check_pr_aid.py"), "--event-file", str(event_file)],
        check=False,
        capture_output=True,
        text=True,
    )
    assert validated.returncode == 0
    assert "AID-16" in validated.stdout
