from __future__ import annotations

import subprocess
from pathlib import Path


SOURCE_SUFFIXES = {".py", ".js", ".jsx", ".ts", ".tsx"}
ACTIVE_PREFIXES = ("app/", "frontend/src/")
LEGACY_MIGRATION_PATHS = {"app/runtime/runtime_db_migrations.py"}
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
DOC_PATTERN_SPECS = (
    ("agent output schema version document reference", "expected_schema_version"),
    ("agent output schema version document reference", "attribution-output/v1"),
    ("agent output schema version document reference", "feedback-optimization-plan-output/v1"),
    ("agent output schema version document reference", "execution-plan-output/v1"),
    ("agent output schema version document reference", "eval-case-governance-output/v1"),
    ("agent output schema version document reference", "feedback-eval-case-generation-output/v1"),
    ("agent output schema version document reference", "regression-impact-analysis-output/v1"),
)
STATIC_OPENAPI_SNAPSHOT_PATH = "docs/开放接口规范.json"
STATIC_OPENAPI_SNAPSHOT_REF = (
    f"{STATIC_OPENAPI_SNAPSHOT_PATH}:static OpenAPI snapshot:tracked OpenAPI JSON:tracked file"
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
            if rel_path in LEGACY_MIGRATION_PATHS and pattern == "output_schema_version":
                continue
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


def static_openapi_snapshot_refs(root: Path) -> set[str]:
    path = root / STATIC_OPENAPI_SNAPSHOT_PATH
    return {STATIC_OPENAPI_SNAPSHOT_REF} if path.exists() else set()


def legacy_feedback_repo_refs(root: Path, base_ref: str | None = None) -> set[str]:
    refs = _static_openapi_snapshot_refs(root, base_ref)
    for rel_path, text in _iter_doc_texts(root, base_ref):
        refs.update(_legacy_feedback_doc_refs(rel_path, text))
    return refs


def _is_active_scan_path(rel_path: str) -> bool:
    if not rel_path.startswith(ACTIVE_PREFIXES):
        return False
    return Path(rel_path).suffix in SOURCE_SUFFIXES


def _legacy_feedback_doc_refs(rel_path: str, text: str) -> set[str]:
    refs: set[str] = set()
    for line in text.splitlines():
        stripped = line.strip()
        if not stripped:
            continue
        for kind, pattern in DOC_PATTERN_SPECS:
            if pattern in stripped:
                refs.add(f"{rel_path}:{kind}:{pattern}:{stripped}")
    return refs


def _iter_doc_texts(root: Path, base_ref: str | None) -> list[tuple[str, str]]:
    if base_ref:
        return [
            (rel_path, text)
            for rel_path in _git_list_docs(root, base_ref)
            if (text := _git_show(root, base_ref, rel_path)) is not None
        ]
    docs = []
    readme = root / "README.md"
    if readme.exists():
        docs.append(("README.md", readme.read_text(encoding="utf-8", errors="ignore")))
    docs_root = root / "docs"
    if docs_root.exists():
        for path in sorted(docs_root.rglob("*.md")):
            docs.append((path.relative_to(root).as_posix(), path.read_text(encoding="utf-8", errors="ignore")))
    return docs


def _git_list_docs(root: Path, base_ref: str) -> list[str]:
    result = subprocess.run(
        ["git", "-C", str(root), "-c", "core.quotePath=false", "ls-tree", "-r", "--name-only", base_ref, "--", "README.md", "docs"],
        check=False,
        capture_output=True,
        encoding="utf-8",
        errors="ignore",
        text=True,
    )
    if result.returncode != 0:
        return []
    return [
        line
        for line in result.stdout.splitlines()
        if line == "README.md" or (line.startswith("docs/") and Path(line).suffix == ".md")
    ]


def _git_show(root: Path, base_ref: str, rel_path: str) -> str | None:
    result = subprocess.run(
        ["git", "-C", str(root), "-c", "core.quotePath=false", "show", f"{base_ref}:{rel_path}"],
        check=False,
        capture_output=True,
        encoding="utf-8",
        errors="ignore",
        text=True,
    )
    return result.stdout if result.returncode == 0 else None


def _static_openapi_snapshot_refs(root: Path, base_ref: str | None) -> set[str]:
    if base_ref is None:
        return static_openapi_snapshot_refs(root)
    return {STATIC_OPENAPI_SNAPSHOT_REF} if _git_show(root, base_ref, STATIC_OPENAPI_SNAPSHOT_PATH) else set()
