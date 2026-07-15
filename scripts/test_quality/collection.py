from __future__ import annotations

import ast
import hashlib
import subprocess
import sys
from dataclasses import dataclass
from pathlib import Path


@dataclass(frozen=True)
class CollectionResult:
    nodeids: tuple[str, ...]
    digest: str


def nodeid_digest(nodeids: list[str] | tuple[str, ...]) -> str:
    payload = "\n".join(sorted(set(nodeids))).encode("utf-8")
    return hashlib.sha256(payload).hexdigest()


def collect_pytest_nodes(
    selectors: list[str] | None = None,
    *,
    repo_root: Path,
    python_executable: str = sys.executable,
) -> CollectionResult:
    requested = selectors or ["tests"]
    result = subprocess.run(
        [python_executable, "-m", "pytest", "--collect-only", "-q", "--disable-warnings", *requested],
        cwd=repo_root,
        capture_output=True,
        text=True,
        check=False,
    )
    if result.returncode != 0:
        output = "\n".join(part.strip() for part in (result.stdout, result.stderr) if part.strip())
        if len(output) > 6000:
            output = f"{output[:6000]}\n... collection output truncated ..."
        raise RuntimeError(f"pytest collection failed:\n{output}")
    nodeids = tuple(sorted({line.strip() for line in result.stdout.splitlines() if line.startswith("tests/") and "::" in line}))
    if not nodeids:
        raise RuntimeError(f"pytest collection produced no leaf nodeids for selectors: {requested}")
    return CollectionResult(nodeids=nodeids, digest=nodeid_digest(nodeids))


def selector_matches_node(selector: str, nodeid: str) -> bool:
    if "::" in selector:
        return nodeid == selector or ("[" not in selector and nodeid.startswith(f"{selector}["))
    return nodeid == selector or nodeid.startswith(f"{selector}::")


def expand_selectors(selectors: list[str], nodeids: tuple[str, ...]) -> tuple[str, ...]:
    return tuple(sorted({nodeid for selector in selectors for nodeid in nodeids if selector_matches_node(selector, nodeid)}))


def validate_pytest_selector(selector: str, *, repo_root: Path) -> list[str]:
    if "::" not in selector:
        return [f"pytest binding {selector} must include a test function"]
    path_text, *parts = selector.split("::")
    path = repo_root / path_text
    if not path.is_file():
        return [f"pytest binding {selector} references missing file {path_text}"]
    try:
        tree = ast.parse(path.read_text(encoding="utf-8"), filename=path_text)
    except SyntaxError as exc:
        return [f"pytest binding {selector} references unparseable test file {path_text}:{exc.lineno}"]
    scope: list[ast.stmt] = tree.body
    for index, raw_part in enumerate(parts):
        part = raw_part.split("[", 1)[0]
        last = index == len(parts) - 1
        expected = (ast.FunctionDef, ast.AsyncFunctionDef) if last else (ast.ClassDef,)
        match = next((node for node in scope if isinstance(node, expected) and node.name == part), None)
        if match is None:
            kind = "test function" if last else "test class"
            return [f"pytest binding {selector} references missing {kind} {part}"]
        scope = match.body
    return []


def collect_pytest_nodeids(
    selectors: list[str],
    *,
    repo_root: Path,
    python_executable: str = sys.executable,
) -> list[str]:
    if not selectors:
        return []
    try:
        collected = collect_pytest_nodes(selectors, repo_root=repo_root, python_executable=python_executable)
    except RuntimeError as exc:
        return [f"pytest could not collect one or more bound nodeids:\n{exc}"]
    missing = [selector for selector in selectors if not expand_selectors([selector], collected.nodeids)]
    return [f"pytest selector expands to zero leaf nodeids: {selector}" for selector in missing]
