from __future__ import annotations

from pathlib import Path


SOURCE_SUFFIXES = {".py", ".js", ".jsx", ".ts", ".tsx"}
ACTIVE_PREFIXES = ("app/", "frontend/src/")
ACTIVE_PATTERNS = (
    "/optimization-proposals",
    "optimization-proposals/",
    "/proposal-jobs",
    "proposal-jobs/",
    "def proposal_prompt",
    "proposal_prompt(",
    "def batch_optimization_plan_prompt",
    "batch_optimization_plan_prompt(",
    "ProposalFormattingSignature",
    "FeedbackProposalRegenerateRequest",
    "PROPOSAL_OUTPUT_SCHEMA_VERSION",
    "create_proposal_job(",
    "complete_proposal_job(",
    "run_proposal_job(",
    "queue_proposal_job(",
    'job_type="proposal"',
    "job_type='proposal'",
    'job_type = "proposal"',
    "job_type = 'proposal'",
    'job_type == "proposal"',
    "job_type == 'proposal'",
    '"job_type": "proposal"',
    '"job_type": \'proposal\'',
    "'job_type': \"proposal\"",
    "'job_type': 'proposal'",
    'job_type: "proposal"',
    "job_type: 'proposal'",
)


def legacy_feedback_active_refs(rel_path: str, text: str) -> set[str]:
    if not _is_active_scan_path(rel_path):
        return set()
    refs: set[str] = set()
    for line in text.splitlines():
        stripped = line.strip()
        if not stripped:
            continue
        for pattern in ACTIVE_PATTERNS:
            if pattern in stripped:
                refs.add(f"{rel_path}:{pattern}:{stripped}")
    return refs


def legacy_feedback_active_ref_issue_specs(
    current_refs: set[str],
    base_refs: set[str],
) -> list[tuple[str, str, bool]]:
    issues: list[tuple[str, str, bool]] = []
    for ref in sorted(current_refs - base_refs):
        rel_path, pattern, snippet = ref.split(":", 2)
        issues.append(
            (
                rel_path,
                f"new active legacy feedback optimization reference: {pattern} ({snippet[:120]})",
                True,
            )
        )
    return issues


def _is_active_scan_path(rel_path: str) -> bool:
    if not rel_path.startswith(ACTIVE_PREFIXES):
        return False
    return Path(rel_path).suffix in SOURCE_SUFFIXES
