from __future__ import annotations

from pathlib import Path


SOURCE_SUFFIXES = {".py", ".js", ".jsx", ".ts", ".tsx"}
ACTIVE_PREFIXES = ("app/", "frontend/src/")
ACTIVE_PATTERN_SPECS = (
    ("legacy feedback optimization reference", "/optimization-proposals"),
    ("legacy feedback optimization reference", "optimization-proposals/"),
    ("legacy feedback optimization reference", "/proposal-jobs"),
    ("legacy feedback optimization reference", "proposal-jobs/"),
    ("legacy feedback optimization reference", "def proposal_prompt"),
    ("legacy feedback optimization reference", "proposal_prompt("),
    ("legacy feedback optimization reference", "def batch_optimization_plan_prompt"),
    ("legacy feedback optimization reference", "batch_optimization_plan_prompt("),
    ("legacy feedback optimization reference", "ProposalFormattingSignature"),
    ("legacy feedback optimization reference", "FeedbackProposalRegenerateRequest"),
    ("legacy feedback optimization reference", "PROPOSAL_OUTPUT_SCHEMA_VERSION"),
    ("legacy feedback optimization reference", "create_proposal_job("),
    ("legacy feedback optimization reference", "complete_proposal_job("),
    ("legacy feedback optimization reference", "run_proposal_job("),
    ("legacy feedback optimization reference", "queue_proposal_job("),
    ("legacy feedback optimization reference", 'job_type="proposal"'),
    ("legacy feedback optimization reference", "job_type='proposal'"),
    ("legacy feedback optimization reference", 'job_type = "proposal"'),
    ("legacy feedback optimization reference", "job_type = 'proposal'"),
    ("legacy feedback optimization reference", 'job_type == "proposal"'),
    ("legacy feedback optimization reference", "job_type == 'proposal'"),
    ("legacy feedback optimization reference", '"job_type": "proposal"'),
    ("legacy feedback optimization reference", '"job_type": \'proposal\''),
    ("legacy feedback optimization reference", "'job_type': \"proposal\""),
    ("legacy feedback optimization reference", "'job_type': 'proposal'"),
    ("legacy feedback optimization reference", 'job_type: "proposal"'),
    ("legacy feedback optimization reference", "job_type: 'proposal'"),
    ("agent output schema version reference", "_SCHEMA_VERSION"),
    ("agent job output_schema_version reference", "output_schema_version"),
    ("formatter JsonObject payload result reference", "OutputFormatterResult(payload"),
    ("formatter JsonObject payload result reference", "payload: JsonObject"),
    ("formatter payload coercion reference", "_coerce_payload("),
)


def legacy_feedback_active_refs(rel_path: str, text: str) -> set[str]:
    if not _is_active_scan_path(rel_path):
        return set()
    refs: set[str] = set()
    for line in text.splitlines():
        stripped = line.strip()
        if not stripped:
            continue
        for kind, pattern in ACTIVE_PATTERN_SPECS:
            if pattern in stripped:
                refs.add(f"{rel_path}:{kind}:{pattern}:{stripped}")
    return refs


def legacy_feedback_active_ref_issue_specs(
    current_refs: set[str],
    base_refs: set[str],
) -> list[tuple[str, str, bool]]:
    issues: list[tuple[str, str, bool]] = []
    for ref in sorted(current_refs - base_refs):
        rel_path, kind, pattern, snippet = ref.split(":", 3)
        issues.append(
            (
                rel_path,
                f"new active {kind}: {pattern} ({snippet[:120]})",
                True,
            )
        )
    return issues


def _is_active_scan_path(rel_path: str) -> bool:
    if not rel_path.startswith(ACTIVE_PREFIXES):
        return False
    return Path(rel_path).suffix in SOURCE_SUFFIXES
