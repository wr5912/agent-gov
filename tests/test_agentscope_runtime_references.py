"""真实临时文件与原生 Read 契约；不替代公共容器沙箱和模型验收。"""

import asyncio
import json
import os
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from threading import Barrier

import pytest
from agentgov_harness_digest import harness_content_digest
from agentscope.state import AgentState
from agentscope.tool import LocalBackend, Read, ToolResponse
from agentscope_runtime.reference_materialization import _activate_reference_staging, remove_private_staging_tree
from agentscope_runtime.workspace_manager import AgentGovWorkspaceManager


def _workspace(tmp_path: Path, *, references: bool = True):
    candidates = tmp_path / "candidates"
    source = candidates / "candidate-docs" / "workspace"
    source.mkdir(parents=True)
    (source / "agent.yaml").write_text("schema_version: 1\n", encoding="utf-8")
    (source / "AGENT.md").write_text("# Documentation helper\n", encoding="utf-8")
    (source / ".env").write_text("PRIVATE_SETTING=host-only\n", encoding="utf-8")
    if references:
        reference = source / "references" / "guides" / "readme.md"
        reference.parent.mkdir(parents=True)
        reference.write_bytes("AgentScope 保存会话；API http://localhost:50400。\r\n".encode())
    runtime = tmp_path / "workspaces"
    runtime.mkdir()
    manager = AgentGovWorkspaceManager(business_agents_root=tmp_path / "business", candidates_root=candidates, workspaces_root=runtime, environ={})
    return source, runtime, manager


def _identity(source: Path) -> str:
    return f"candidate-docs--v-{harness_content_digest(source)}"


def test_runtime_references_are_versioned_and_readable_by_native_read(tmp_path):
    source, runtime, manager = _workspace(tmp_path)
    identity = _identity(source)
    harness, target = manager._materialize(identity)
    assert harness == source and target == runtime / identity
    state = target / ".agentgov-runtime-state"
    reference = state / "references/guides/readme.md"
    assert reference.read_bytes() == (source / "references/guides/readme.md").read_bytes()
    assert not (state / "agent.yaml").exists() and not (state / "AGENT.md").exists() and not (state / ".env").exists()
    chunk = asyncio.run(Read(backend=LocalBackend()).call(str(reference), _agent_state=AgentState()))
    result = ToolResponse().append_chunk(chunk)
    assert result.state == "success"
    assert "AgentScope" in "".join(getattr(block, "text", "") for block in result.content)
    before = reference.stat()
    assert manager._materialize(identity) == (source, target)
    assert (reference.stat().st_ino, reference.stat().st_mtime_ns) == (before.st_ino, before.st_mtime_ns)


def test_runtime_references_materialize_from_read_only_source_directories(tmp_path):
    source, runtime, manager = _workspace(tmp_path)
    references = source / "references"
    guide = references / "guides"
    reference = guide / "readme.md"
    reference.chmod(0o444)
    guide.chmod(0o555)
    references.chmod(0o555)

    identity = _identity(source)
    _, target = manager._materialize(identity)

    copied = target / ".agentgov-runtime-state/references"
    assert copied.stat().st_mode & 0o777 == 0o555
    assert (copied / "guides").stat().st_mode & 0o777 == 0o555
    assert (copied / "guides/readme.md").stat().st_mode & 0o777 == 0o444
    assert not list(runtime.glob(f".{identity}.*"))


def test_concurrent_workspace_creation_cleans_read_only_losing_staging_tree(tmp_path):
    source, runtime, _ = _workspace(tmp_path)
    references = source / "references"
    (references / "guides/readme.md").chmod(0o444)
    (references / "guides").chmod(0o555)
    references.chmod(0o555)
    identity = _identity(source)
    ready = Barrier(2)

    class ConcurrentManager(AgentGovWorkspaceManager):
        def _create_state_atomically(self, target, workspace_id, digest, harness_source):
            ready.wait(timeout=5)
            return super()._create_state_atomically(target, workspace_id, digest, harness_source)

    managers = [
        ConcurrentManager(
            business_agents_root=tmp_path / "business",
            candidates_root=tmp_path / "candidates",
            workspaces_root=runtime,
            environ={},
        )
        for _ in range(2)
    ]
    with ThreadPoolExecutor(max_workers=2) as pool:
        results = list(pool.map(lambda manager: manager._materialize(identity), managers))

    assert results == [(source, runtime / identity)] * 2
    assert not list(runtime.glob(f".{identity}.*"))


def test_concurrent_reference_backfill_cleans_read_only_losing_staging_tree(tmp_path):
    source, runtime, _ = _workspace(tmp_path)
    identity = _identity(source)
    target = runtime / identity
    state = target / ".agentgov-runtime-state"
    state.mkdir(parents=True)
    (target / ".agentgov-runtime-cache").mkdir()
    (target / ".agentgov-runtime-workspace.json").write_text(
        json.dumps({"workspace_id": identity, "harness_digest": harness_content_digest(source)}),
        encoding="utf-8",
    )
    references = source / "references"
    (references / "guides/readme.md").chmod(0o444)
    (references / "guides").chmod(0o555)
    references.chmod(0o555)
    ready = Barrier(2)

    class ConcurrentManager(AgentGovWorkspaceManager):
        def _materialize_references(self, harness_source, state_root, digest):
            if not (state_root / "references").exists():
                ready.wait(timeout=5)
            return super()._materialize_references(harness_source, state_root, digest)

    managers = [
        ConcurrentManager(
            business_agents_root=tmp_path / "business",
            candidates_root=tmp_path / "candidates",
            workspaces_root=runtime,
            environ={},
        )
        for _ in range(2)
    ]
    with ThreadPoolExecutor(max_workers=2) as pool:
        results = list(pool.map(lambda current: current._materialize(identity), managers))

    assert results == [(source, target)] * 2
    assert (state / "references/guides/readme.md").is_file()
    assert not list(state.glob(".agentgov-references-*"))


def test_reference_activation_deterministically_exercises_real_rename_loser(tmp_path):
    state = tmp_path / "state"
    state.mkdir()
    target = state / "references"
    expected = {"guides/readme.md": b"fixed version"}
    staging_roots = []
    for index in range(2):
        staging = state / f".agentgov-references-{index}"
        guide = staging / "guides"
        guide.mkdir(parents=True)
        (guide / "readme.md").write_bytes(expected["guides/readme.md"])
        (guide / "readme.md").chmod(0o444)
        guide.chmod(0o555)
        staging.chmod(0o555)
        staging_roots.append(staging)
    ready = Barrier(2)

    def activate(staging: Path) -> bool:
        ready.wait(timeout=5)
        try:
            _activate_reference_staging(staging, target, expected)
            return staging.exists()
        finally:
            remove_private_staging_tree(staging)

    with ThreadPoolExecutor(max_workers=2) as pool:
        loser_staging_states = list(pool.map(activate, staging_roots))

    assert sorted(loser_staging_states) == [False, True]
    assert (target / "guides/readme.md").read_bytes() == expected["guides/readme.md"]
    assert not list(state.glob(".agentgov-references-*"))


def test_workspace_root_fsync_failure_after_rename_is_not_misclassified_as_race(tmp_path):
    source, runtime, _ = _workspace(tmp_path)
    identity = _identity(source)

    class RootFsyncFailureManager(AgentGovWorkspaceManager):
        @staticmethod
        def _fsync_directory(path):
            if path == runtime:
                raise OSError("controlled workspace-root fsync failure")
            AgentGovWorkspaceManager._fsync_directory(path)

    manager = RootFsyncFailureManager(
        business_agents_root=tmp_path / "business",
        candidates_root=tmp_path / "candidates",
        workspaces_root=runtime,
        environ={},
    )

    with pytest.raises(OSError, match="workspace-root fsync failure"):
        manager._materialize(identity)

    assert (runtime / identity / ".agentgov-runtime-workspace.json").is_file()
    assert not list(runtime.glob(f".{identity}.*"))


@pytest.mark.parametrize("existing", ["missing", "different"])
def test_workspace_creation_race_validates_winner_references(tmp_path, existing):
    source, runtime, manager = _workspace(tmp_path)
    identity = _identity(source)
    target = runtime / identity
    state = target / ".agentgov-runtime-state"
    state.mkdir(parents=True)
    (target / ".agentgov-runtime-cache").mkdir()
    (target / ".agentgov-runtime-workspace.json").write_text(
        json.dumps({"workspace_id": identity, "harness_digest": harness_content_digest(source)}),
        encoding="utf-8",
    )
    if existing == "different":
        reference = state / "references/guides/readme.md"
        reference.parent.mkdir(parents=True)
        reference.write_bytes(b"winner contained a different version")

    if existing == "different":
        with pytest.raises(ValueError, match="references differ"):
            manager._create_state_atomically(target, identity, harness_content_digest(source), source)
        assert (state / "references/guides/readme.md").read_bytes() == b"winner contained a different version"
    else:
        manager._create_state_atomically(target, identity, harness_content_digest(source), source)
        assert (state / "references/guides/readme.md").read_bytes() == (source / "references/guides/readme.md").read_bytes()
    assert not list(runtime.glob(f".{identity}.*"))


def test_undeclared_existing_references_fail_closed_without_replacement(tmp_path):
    source, _, manager = _workspace(tmp_path, references=False)
    identity = _identity(source)
    _, target = manager._materialize(identity)
    unexpected = target / ".agentgov-runtime-state/references/unversioned.txt"
    unexpected.parent.mkdir()
    unexpected.write_bytes(b"preserve for diagnosis")

    with pytest.raises(ValueError, match="not declared"):
        manager._materialize(identity)

    assert unexpected.read_bytes() == b"preserve for diagnosis"


def test_pattern_named_directory_matches_digest_and_copy_semantics(tmp_path):
    source, _, manager = _workspace(tmp_path)
    retained = source / "references/archive.pyc/retained.md"
    retained.parent.mkdir()
    retained.write_bytes(b"a directory name is not a bytecode file")
    ignored = source / "references/ignored.pyc"
    ignored.write_bytes(b"bytecode cache")
    identity = _identity(source)

    _, target = manager._materialize(identity)

    copied = target / ".agentgov-runtime-state/references"
    assert (copied / "archive.pyc/retained.md").read_bytes() == retained.read_bytes()
    assert not (copied / "ignored.pyc").exists()


def test_staging_symlink_is_rejected_without_changing_external_permissions(tmp_path):
    _, runtime, _ = _workspace(tmp_path)
    external = tmp_path / "external"
    external.mkdir(mode=0o500)
    staging = runtime / ".agentgov-references-replaced"
    staging.symlink_to(external, target_is_directory=True)

    with pytest.raises(OSError):
        remove_private_staging_tree(staging)

    assert external.stat().st_mode & 0o777 == 0o500
    assert staging.is_symlink()


@pytest.mark.parametrize("kind", ["regular", "fifo"])
def test_excluded_target_subtree_cannot_hide_unversioned_files(tmp_path, kind):
    source, _, manager = _workspace(tmp_path)
    identity = _identity(source)
    _, target = manager._materialize(identity)
    excluded = target / ".agentgov-runtime-state/references/__pycache__"
    excluded.mkdir()
    hidden = excluded / "hidden"
    if kind == "regular":
        hidden.write_bytes(b"unversioned")
    else:
        os.mkfifo(hidden)

    with pytest.raises(ValueError, match="excluded|only regular"):
        manager._materialize(identity)


def test_runtime_references_fill_missing_copy_without_replacing_existing_state(tmp_path):
    source, runtime, manager = _workspace(tmp_path)
    identity = _identity(source)
    target = runtime / identity
    state = target / ".agentgov-runtime-state"
    state.mkdir(parents=True)
    (target / ".agentgov-runtime-cache").mkdir()
    marker = target / ".agentgov-runtime-workspace.json"
    marker.write_text(json.dumps({"workspace_id": identity, "harness_digest": harness_content_digest(source)}), encoding="utf-8")
    retained = state / "existing-session-state.json"
    retained.write_bytes(b'{ "preserved": true }\n')
    before = marker.read_bytes(), retained.read_bytes(), state.stat().st_ino
    manager._materialize(identity)
    assert (state / "references/guides/readme.md").read_bytes() == (source / "references/guides/readme.md").read_bytes()
    assert (marker.read_bytes(), retained.read_bytes(), state.stat().st_ino) == before


@pytest.mark.parametrize("change", ["modified", "missing", "extra", "symlink"])
def test_runtime_references_refuse_different_existing_copy_without_overwriting(tmp_path, change):
    source, _, manager = _workspace(tmp_path)
    identity = _identity(source)
    _, target = manager._materialize(identity)
    references = target / ".agentgov-runtime-state/references"
    reference = references / "guides/readme.md"
    if change == "modified":
        reference.write_bytes(b"retained local content")
    elif change == "missing":
        reference.unlink()
    elif change == "extra":
        (references / "extra.txt").write_bytes(b"retained extra content")
    else:
        reference.unlink()
        reference.symlink_to(source / "references/guides/readme.md")
    before = {path.relative_to(references).as_posix(): path.read_bytes() for path in references.rglob("*") if path.is_file()}
    with pytest.raises(ValueError, match="references differ|symlink"):
        manager._materialize(identity)
    assert {path.relative_to(references).as_posix(): path.read_bytes() for path in references.rglob("*") if path.is_file()} == before


def test_reference_changes_require_a_new_harness_identity(tmp_path):
    source, _, manager = _workspace(tmp_path)
    identity = _identity(source)
    _, previous = manager._materialize(identity)
    old_bytes = (previous / ".agentgov-runtime-state/references/guides/readme.md").read_bytes()
    (source / "references/guides/readme.md").write_bytes(b"new version of the actual documentation")
    assert _identity(source) != identity
    with pytest.raises(ValueError, match="tree digest"):
        manager._materialize(identity)
    _, current = manager._materialize(_identity(source))
    assert current != previous
    assert (previous / ".agentgov-runtime-state/references/guides/readme.md").read_bytes() == old_bytes
    assert (current / ".agentgov-runtime-state/references/guides/readme.md").read_bytes() == b"new version of the actual documentation"


def test_no_references_preserve_legacy_digest_and_state_layout(tmp_path):
    source, _, manager = _workspace(tmp_path, references=False)
    # 固定旧版两项 Harness 向量；新增可选根不得改变无 references 的已有绑定。
    assert harness_content_digest(source) == "f06c5a7fd7253c3d060605f91c06a35a6fda5ff62bc2cfd3d2bf1059980f98ca"
    _, target = manager._materialize(_identity(source))
    assert list((target / ".agentgov-runtime-state").iterdir()) == []


@pytest.mark.parametrize("kind", ["file", "symlink", "fifo"])
def test_references_reject_non_directory_root_and_non_regular_assets(tmp_path, kind):
    source, runtime, manager = _workspace(tmp_path, references=False)
    references = source / "references"
    if kind == "file":
        references.write_text("not a reference directory", encoding="utf-8")
    elif kind == "symlink":
        references.symlink_to(source / "AGENT.md")
    else:
        references.mkdir()
        os.mkfifo(references / "stream")
    with pytest.raises(ValueError, match="non-symlink directory|symlink|non-regular"):
        manager._materialize(_identity(source))
    assert list(runtime.iterdir()) == []


def test_reference_cache_files_do_not_enter_digest_or_execution_copy(tmp_path):
    source, _, manager = _workspace(tmp_path)
    identity = _identity(source)
    cache = source / "references/__pycache__"
    cache.mkdir()
    (cache / "cached.pyc").write_bytes(b"not a reference asset")
    assert _identity(source) == identity
    _, target = manager._materialize(identity)
    assert not (target / ".agentgov-runtime-state/references/__pycache__").exists()
