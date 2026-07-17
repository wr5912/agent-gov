from __future__ import annotations

import sys
from pathlib import Path
from typing import Any

REPO_ROOT = Path(__file__).resolve().parents[1]
SCRIPTS_DIR = REPO_ROOT / "scripts"
sys.path.insert(0, str(SCRIPTS_DIR))

import agent_gov_ci_status_relay as relay  # noqa: E402
import agent_gov_multica as multica  # noqa: E402

__all__ = [
    "PR_SHA",
    "PUSH_SHA",
    "SCRIPTS_DIR",
    "FakeGitHub",
    "_config",
    "_pull",
    "_responses",
    "_run",
    "multica",
    "relay",
]

PR_SHA = "1" * 40
PUSH_SHA = "2" * 40


class FakeGitHub:
    def __init__(self, responses: dict[str, object]) -> None:
        self.responses = responses
        self.requests: list[str] = []

    def get(self, path: str) -> Any:
        self.requests.append(path)
        if path not in self.responses:
            raise AssertionError(f"unexpected GitHub request: {path}")
        return self.responses[path]


def _config(tmp_path: Path) -> relay.RelayConfig:
    return relay.RelayConfig(
        repository="wr5912/agent-gov",
        branch="master",
        workflow_file=".github/workflows/governance.yml",
        github_api_url="https://api.github.test",
        state_dir=tmp_path / "relay",
        multica_profile="ci-status-relay",
        run_limit=50,
    )


def _pull(
    number: int,
    *,
    sha: str | None = None,
    title: str = "AID-16: keep CI visible",
    body: str = "",
) -> dict[str, object]:
    return {
        "number": number,
        "title": title,
        "body": body,
        "head": {"ref": "aid-16-ci-relay"},
        "base": {"ref": "master"},
        "merged_at": "2026-07-16T00:00:00Z" if sha else None,
        "merge_commit_sha": sha,
    }


def _run(
    run_id: int,
    *,
    event: str,
    sha: str,
    conclusion: str,
    attempt: int = 1,
    pull_number: int | None = None,
) -> dict[str, object]:
    pulls = [{"number": pull_number}] if pull_number else []
    return {
        "id": run_id,
        "run_attempt": attempt,
        "event": event,
        "status": "completed",
        "conclusion": conclusion,
        "head_sha": sha,
        "head_branch": "master" if event == "push" else "aid-16-ci-relay",
        "path": ".github/workflows/governance.yml",
        "html_url": f"https://github.test/actions/runs/{run_id}",
        "pull_requests": pulls,
        "updated_at": "2026-07-16T08:00:00Z",
    }


def _responses(
    config: relay.RelayConfig,
    *,
    pull_runs: list[object],
    push_runs: list[object],
) -> dict[str, object]:
    return {
        relay.workflow_runs_path(config, "pull_request", page=1): {"workflow_runs": pull_runs},
        relay.workflow_runs_path(config, "push", page=1): {"workflow_runs": push_runs},
    }
