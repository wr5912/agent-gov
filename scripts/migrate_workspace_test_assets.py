#!/usr/bin/env python3
from __future__ import annotations

import argparse
import hashlib
import json
import os
import shutil
import stat
import subprocess
import sys
import tempfile
from dataclasses import asdict, dataclass
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[1]
if str(REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(REPO_ROOT))

from app.agent_testing.legacy_generated_tests import classify_legacy_generated_test  # noqa: E402

if __package__:
    from .bootstrap_runtime_volume import DEFAULT_BOOTSTRAP_DIR, DEFAULT_ENV_FILE, resolve_runtime_root
else:
    from bootstrap_runtime_volume import DEFAULT_BOOTSTRAP_DIR, DEFAULT_ENV_FILE, resolve_runtime_root

BUILTIN_TEST_AGENT_ID = "security-operations-expert"


@dataclass(frozen=True)
class WorkspaceMigrationResult:
    agent_id: str
    previous_commit_sha: str
    current_commit_sha: str
    tests_added: bool
    legacy_generated_test_files_archived: tuple[str, ...]
    archived_evals_path: str | None
    changed: bool


def migrate_workspace_test_assets(
    *,
    runtime_root: Path,
    bootstrap_dir: Path,
    apply: bool,
) -> list[WorkspaceMigrationResult]:
    agents_root = runtime_root / "data" / "business-agents"
    if not agents_root.is_dir():
        return []
    source_tests = bootstrap_dir / "business-agents" / BUILTIN_TEST_AGENT_ID / "workspace" / "tests"
    _require_real_directory(source_tests, label="built-in tests source")
    workspaces: list[tuple[str, Path]] = []
    for agent_dir in sorted(agents_root.iterdir()):
        if not agent_dir.is_dir() or agent_dir.is_symlink():
            continue
        workspace = agent_dir / "workspace"
        if not workspace.is_dir() or workspace.is_symlink():
            continue
        workspaces.append((agent_dir.name, workspace))
    planned = [
        _migrate_workspace(
            agent_id=agent_id,
            workspace=workspace,
            runtime_root=runtime_root,
            source_tests=source_tests,
            apply=False,
            require_writable=apply,
        )
        for agent_id, workspace in workspaces
    ]
    if not apply:
        return planned
    return [
        _migrate_workspace(
            agent_id=agent_id,
            workspace=workspace,
            runtime_root=runtime_root,
            source_tests=source_tests,
            apply=True,
            require_writable=True,
        )
        for agent_id, workspace in workspaces
    ]


def _migrate_workspace(
    *,
    agent_id: str,
    workspace: Path,
    runtime_root: Path,
    source_tests: Path,
    apply: bool,
    require_writable: bool = False,
) -> WorkspaceMigrationResult:
    _require_git_workspace(workspace)
    if _git(workspace, "status", "--porcelain=v1").stdout.strip():
        raise RuntimeError(f"Refusing to migrate dirty business Agent Workspace: {agent_id}")
    previous_commit = _git(workspace, "rev-parse", "HEAD").stdout.strip()
    evals_dir = workspace / "evals"
    tests_dir = workspace / "tests"
    if agent_id == BUILTIN_TEST_AGENT_ID and tests_dir.exists() and (tests_dir.is_symlink() or not tests_dir.is_dir()):
        raise RuntimeError(f"Built-in business Agent tests path is unsafe: {tests_dir}")
    add_tests = agent_id == BUILTIN_TEST_AGENT_ID and not tests_dir.exists()
    legacy_generated_tests = _legacy_generated_test_files(workspace)
    legacy_generated_test_paths = tuple(path.relative_to(workspace).as_posix() for path in legacy_generated_tests)
    archive_path = _archive_path(runtime_root, agent_id, evals_dir) if evals_dir.is_dir() and not evals_dir.is_symlink() else None
    generated_tests_archive = _generated_tests_archive_path(runtime_root, agent_id, workspace, legacy_generated_tests)
    changed = add_tests or bool(legacy_generated_tests) or archive_path is not None
    if require_writable and changed and not os.access(workspace, os.W_OK | os.X_OK):
        raise RuntimeError(f"Refusing to migrate non-writable business Agent Workspace: {agent_id}")
    if not apply or not changed:
        return WorkspaceMigrationResult(
            agent_id=agent_id,
            previous_commit_sha=previous_commit,
            current_commit_sha=previous_commit,
            tests_added=add_tests,
            legacy_generated_test_files_archived=legacy_generated_test_paths,
            archived_evals_path=archive_path.as_posix() if archive_path else None,
            changed=changed,
        )
    _apply_workspace_migration(
        workspace=workspace,
        source_tests=source_tests,
        add_tests=add_tests,
        legacy_generated_tests=legacy_generated_tests,
        generated_tests_archive=generated_tests_archive,
        evals_dir=evals_dir if archive_path else None,
        archive_path=archive_path,
        previous_commit=previous_commit,
    )
    current_commit = _git(workspace, "rev-parse", "HEAD").stdout.strip()
    return WorkspaceMigrationResult(
        agent_id=agent_id,
        previous_commit_sha=previous_commit,
        current_commit_sha=current_commit,
        tests_added=add_tests,
        legacy_generated_test_files_archived=legacy_generated_test_paths,
        archived_evals_path=archive_path.as_posix() if archive_path else None,
        changed=True,
    )


def _apply_workspace_migration(
    *,
    workspace: Path,
    source_tests: Path,
    add_tests: bool,
    legacy_generated_tests: tuple[Path, ...],
    generated_tests_archive: Path | None,
    evals_dir: Path | None,
    archive_path: Path | None,
    previous_commit: str,
) -> None:
    tests_added = False
    try:
        if evals_dir is not None and archive_path is not None:
            _archive_directory(evals_dir, archive_path)
            shutil.rmtree(evals_dir)
        if add_tests:
            shutil.copytree(source_tests, workspace / "tests", copy_function=shutil.copy2)
            tests_added = True
        if legacy_generated_tests and generated_tests_archive is not None:
            _archive_files(workspace, legacy_generated_tests, generated_tests_archive)
            for path in legacy_generated_tests:
                path.unlink()
        changed_paths = [
            name
            for name, enabled in (
                ("evals", evals_dir is not None),
                ("tests", add_tests or bool(legacy_generated_tests)),
            )
            if enabled
        ]
        _git(workspace, "add", "-A", "--", *changed_paths)
        if _git(workspace, "diff", "--cached", "--quiet", check=False).returncode != 0:
            _git(workspace, "commit", "-m", "Migrate Workspace pytest assets")
        if _git(workspace, "status", "--porcelain=v1").stdout.strip():
            raise RuntimeError(f"Workspace migration left uncommitted changes: {workspace}")
    except Exception:
        _git(workspace, "reset", "--hard", previous_commit, check=False)
        if tests_added:
            shutil.rmtree(workspace / "tests", ignore_errors=True)
        elif legacy_generated_tests and generated_tests_archive is not None:
            for source in generated_tests_archive.rglob("*"):
                if source.is_file():
                    destination = workspace / source.relative_to(generated_tests_archive)
                    destination.parent.mkdir(parents=True, exist_ok=True)
                    shutil.copy2(source, destination)
        if evals_dir is not None and archive_path is not None and archive_path.is_dir() and not evals_dir.exists():
            shutil.copytree(archive_path, evals_dir, copy_function=shutil.copy2)
        raise


def _legacy_generated_test_files(workspace: Path) -> tuple[Path, ...]:
    tests_dir = workspace / "tests"
    if not tests_dir.exists():
        return ()
    _require_real_directory(tests_dir, label="business Agent tests")
    for entry in tests_dir.rglob("*"):
        if entry.is_symlink():
            raise RuntimeError(f"Business Agent tests contain unsupported symlink: {entry}")
    legacy: list[Path] = []
    for path in sorted(tests_dir.glob("test_*.py")):
        if not path.is_file() or path.is_symlink():
            raise RuntimeError(f"Business Agent test must be a regular file: {path}")
        source = path.read_text(encoding="utf-8")
        classification = classify_legacy_generated_test(source, filename=str(path))
        if classification == "not_marked":
            continue
        if classification != "archivable_weak_test":
            raise RuntimeError(f"Marked legacy generated test has an unknown structure; refusing automatic removal: {path}")
        legacy.append(path)
    return tuple(legacy)


def _archive_path(runtime_root: Path, agent_id: str, source: Path) -> Path:
    digest = _directory_digest(source)
    return runtime_root / "data" / "archived-legacy-test-assets" / agent_id / digest / "evals"


def _generated_tests_archive_path(
    runtime_root: Path,
    agent_id: str,
    workspace: Path,
    files: tuple[Path, ...],
) -> Path | None:
    if not files:
        return None
    digest = _files_digest(workspace, files)
    return runtime_root / "data" / "archived-legacy-test-assets" / agent_id / digest / "generated-pytest"


def _archive_files(workspace: Path, files: tuple[Path, ...], destination: Path) -> None:
    expected_digest = _files_digest(workspace, files)
    if destination.exists():
        _require_real_directory(destination, label="existing generated pytest archive")
        archived_files = tuple(path for path in destination.rglob("*") if path.is_file())
        if _files_digest(destination, archived_files) != expected_digest:
            raise RuntimeError(f"Existing generated pytest archive conflicts with source: {destination}")
        return
    destination.parent.mkdir(parents=True, exist_ok=True)
    temporary = Path(tempfile.mkdtemp(prefix=".generated-pytest-archive-", dir=destination.parent))
    try:
        for source in files:
            relative = source.relative_to(workspace)
            target = temporary / relative
            target.parent.mkdir(parents=True, exist_ok=True)
            shutil.copy2(source, target)
        archived_files = tuple(path for path in temporary.rglob("*") if path.is_file())
        if _files_digest(temporary, archived_files) != expected_digest:
            raise RuntimeError("Archived generated pytest digest mismatch")
        os.replace(temporary, destination)
    finally:
        shutil.rmtree(temporary, ignore_errors=True)


def _archive_directory(source: Path, destination: Path) -> None:
    expected_digest = _directory_digest(source)
    if destination.exists():
        _require_real_directory(destination, label="existing evals archive")
        if _directory_digest(destination) != expected_digest:
            raise RuntimeError(f"Existing evals archive conflicts with source: {destination}")
        return
    destination.parent.mkdir(parents=True, exist_ok=True)
    temporary = Path(tempfile.mkdtemp(prefix=".evals-archive-", dir=destination.parent))
    try:
        shutil.rmtree(temporary)
        shutil.copytree(source, temporary, copy_function=shutil.copy2)
        if _directory_digest(temporary) != expected_digest:
            raise RuntimeError(f"Archived evals digest mismatch: {source}")
        os.replace(temporary, destination)
    finally:
        shutil.rmtree(temporary, ignore_errors=True)


def _directory_digest(root: Path) -> str:
    digest = hashlib.sha256()
    for path in sorted(root.rglob("*")):
        mode = path.lstat().st_mode
        if stat.S_ISLNK(mode) or not (stat.S_ISDIR(mode) or stat.S_ISREG(mode)):
            raise RuntimeError(f"Legacy test asset contains unsupported entry: {path}")
        relative = path.relative_to(root).as_posix().encode("utf-8")
        digest.update(relative)
        digest.update(stat.S_IMODE(mode).to_bytes(4, "big"))
        if path.is_file():
            digest.update(path.read_bytes())
    return digest.hexdigest()


def _files_digest(root: Path, files: tuple[Path, ...]) -> str:
    digest = hashlib.sha256()
    for path in sorted(files):
        if path.is_symlink() or not path.is_file():
            raise RuntimeError(f"Legacy generated pytest must be a regular file: {path}")
        relative = path.relative_to(root).as_posix().encode("utf-8")
        digest.update(relative)
        digest.update(stat.S_IMODE(path.stat().st_mode).to_bytes(4, "big"))
        digest.update(path.read_bytes())
    return digest.hexdigest()


def _require_git_workspace(workspace: Path) -> None:
    if not (workspace / ".git").exists():
        raise RuntimeError(f"Business Agent Workspace is not Git-backed: {workspace}")
    _git(workspace, "rev-parse", "--is-inside-work-tree")


def _require_real_directory(path: Path, *, label: str) -> None:
    if path.is_symlink() or not path.is_dir():
        raise RuntimeError(f"{label} must be a real directory: {path}")


def _git(workspace: Path, *args: str, check: bool = True) -> subprocess.CompletedProcess[str]:
    process = subprocess.run(
        ["git", "-c", f"safe.directory={workspace}", "-C", str(workspace), *args],
        capture_output=True,
        text=True,
        check=False,
        env={**os.environ, "GIT_TERMINAL_PROMPT": "0"},
    )
    if check and process.returncode != 0:
        detail = (process.stderr or process.stdout).strip()
        raise RuntimeError(detail or f"git {' '.join(args)} failed in {workspace}")
    return process


def main() -> int:
    parser = argparse.ArgumentParser(description="Migrate legacy business Agent test assets into Workspace pytest ownership.")
    parser.add_argument("--env-file", type=Path, default=DEFAULT_ENV_FILE)
    parser.add_argument("--runtime-root")
    parser.add_argument("--bootstrap-dir", type=Path, default=DEFAULT_BOOTSTRAP_DIR)
    parser.add_argument("--apply", action="store_true", help="Apply changes; default is a read-only scan.")
    args = parser.parse_args()
    runtime_root = resolve_runtime_root(args.runtime_root, args.env_file)
    results = migrate_workspace_test_assets(
        runtime_root=runtime_root,
        bootstrap_dir=args.bootstrap_dir.resolve(),
        apply=args.apply,
    )
    print(json.dumps({"mode": "apply" if args.apply else "scan", "results": [asdict(item) for item in results]}, ensure_ascii=False, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
