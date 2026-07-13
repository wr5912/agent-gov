from __future__ import annotations

import hashlib
import json
import stat
from pathlib import Path

import pytest
import scripts.retire_runtime_seed_assets as retired_assets
from scripts.retire_runtime_seed_assets import RetirementSafetyError, retire_runtime_seed_assets

REPO_ROOT = Path(__file__).resolve().parents[1]
CANONICAL_REGISTRY = REPO_ROOT / "docker/runtime-volume-seeds/workspace-policy/retired-seed-assets.json"
TARGET_RELATIVE_PATH = Path("data/business-agents/main-agent/workspace/evals/alert-triage-false-positive.json")
EXPECTED_SEED_SHA256 = "199b3cf4873d881aca4bdd980846d1f538cb424ea62e67962d3e63f877495c41"


def _write_registry(tmp_path: Path, *, content: bytes, relative_path: Path = TARGET_RELATIVE_PATH) -> Path:
    registry = tmp_path / "retired-seed-assets.json"
    registry.write_text(
        json.dumps(
            {
                "version": 1,
                "assets": [
                    {
                        "id": "test-retired-seed-v1",
                        "relative_path": relative_path.as_posix(),
                        "sha256": hashlib.sha256(content).hexdigest(),
                    }
                ],
            }
        ),
        encoding="utf-8",
    )
    return registry


def _runtime_with_target(tmp_path: Path, content: bytes) -> tuple[Path, Path]:
    runtime_root = tmp_path / "runtime"
    target = runtime_root / TARGET_RELATIVE_PATH
    target.parent.mkdir(parents=True)
    target.write_bytes(content)
    return runtime_root, target


def _single_result(result: retired_assets.RetirementResult) -> retired_assets.AssetRetirementResult:
    assert len(result["assets"]) == 1
    return result["assets"][0]


def test_canonical_registry_targets_only_the_removed_seed_revision() -> None:
    payload = json.loads(CANONICAL_REGISTRY.read_text(encoding="utf-8"))

    assert payload == {
        "version": 1,
        "assets": [
            {
                "id": "main-agent-alert-triage-false-positive-v1",
                "relative_path": TARGET_RELATIVE_PATH.as_posix(),
                "sha256": EXPECTED_SEED_SHA256,
            }
        ],
    }
    assert not (REPO_ROOT / "docker/runtime-volume-seeds" / TARGET_RELATIVE_PATH).exists()


def test_matching_seed_is_verified_backed_up_removed_and_idempotent(tmp_path: Path) -> None:
    original = b'{"managed": "retired-seed"}\n'
    runtime_root, target = _runtime_with_target(tmp_path, original)
    registry = _write_registry(tmp_path, content=original)

    first = retire_runtime_seed_assets(runtime_root=runtime_root, registry_path=registry, apply=True, operator="pytest")

    asset = _single_result(first)
    assert asset["status"] == "removed"
    assert asset["relative_path"] == TARGET_RELATIVE_PATH.as_posix()
    assert not target.exists()
    assert asset["backup"] is not None
    backup = runtime_root / asset["backup"]
    assert backup.read_bytes() == original
    assert hashlib.sha256(backup.read_bytes()).hexdigest() == hashlib.sha256(original).hexdigest()
    assert stat.S_IMODE(backup.stat().st_mode) == 0o600
    assert stat.S_IMODE(backup.parent.stat().st_mode) == 0o700

    audit_files = sorted((runtime_root / "data/.retired-seed-assets/audit").glob("*.json"))
    assert len(audit_files) == 1
    audit = json.loads(audit_files[0].read_text(encoding="utf-8"))
    assert audit["operator"] == "pytest"
    assert audit["assets"] == first["assets"]
    assert str(tmp_path) not in json.dumps(first)
    assert str(tmp_path) not in audit_files[0].read_text(encoding="utf-8")

    second = retire_runtime_seed_assets(runtime_root=runtime_root, registry_path=registry, apply=True, operator="pytest")

    assert _single_result(second)["status"] == "absent"
    assert backup.read_bytes() == original


def test_modified_seed_is_preserved_without_backup_and_startup_can_continue(tmp_path: Path) -> None:
    retired_content = b"old managed seed\n"
    user_content = b"user changed this evaluation\n"
    runtime_root, target = _runtime_with_target(tmp_path, user_content)
    registry = _write_registry(tmp_path, content=retired_content)

    result = retire_runtime_seed_assets(runtime_root=runtime_root, registry_path=registry, apply=True)

    asset = _single_result(result)
    assert asset["status"] == "preserved_modified"
    assert asset["backup"] is None
    assert target.read_bytes() == user_content
    assert not (runtime_root / "data/.retired-seed-assets/backups").exists()


@pytest.mark.parametrize("symlink_location", ["target", "parent"])
def test_symlink_in_target_path_fails_closed_without_touching_external_content(tmp_path: Path, symlink_location: str) -> None:
    original = b"old managed seed\n"
    runtime_root = tmp_path / "runtime"
    (runtime_root / "data").mkdir(parents=True)
    outside = tmp_path / "outside"
    outside.mkdir()
    external_file = outside / "external.json"
    external_file.write_bytes(original)
    target = runtime_root / TARGET_RELATIVE_PATH
    if symlink_location == "target":
        target.parent.mkdir(parents=True)
        target.symlink_to(external_file)
    else:
        agents_parent = runtime_root / "data/business-agents"
        agents_parent.mkdir(parents=True)
        (agents_parent / "main-agent").symlink_to(outside, target_is_directory=True)
    registry = _write_registry(tmp_path, content=original)

    with pytest.raises(RetirementSafetyError) as exc_info:
        retire_runtime_seed_assets(runtime_root=runtime_root, registry_path=registry, apply=True)

    assert external_file.read_bytes() == original
    assert str(tmp_path) not in str(exc_info.value)
    assert not (runtime_root / "data/.retired-seed-assets/backups").exists()


def test_non_regular_target_fails_closed(tmp_path: Path) -> None:
    original = b"old managed seed\n"
    runtime_root = tmp_path / "runtime"
    target = runtime_root / TARGET_RELATIVE_PATH
    target.mkdir(parents=True)
    registry = _write_registry(tmp_path, content=original)

    with pytest.raises(RetirementSafetyError, match="must be a regular file"):
        retire_runtime_seed_assets(runtime_root=runtime_root, registry_path=registry, apply=True)

    assert target.is_dir()
    assert not (runtime_root / "data/.retired-seed-assets/backups").exists()


def test_symlink_runtime_root_fails_closed(tmp_path: Path) -> None:
    original = b"old managed seed\n"
    real_runtime, target = _runtime_with_target(tmp_path, original)
    linked_runtime = tmp_path / "runtime-link"
    linked_runtime.symlink_to(real_runtime, target_is_directory=True)
    registry = _write_registry(tmp_path, content=original)

    with pytest.raises(RetirementSafetyError, match="real directory"):
        retire_runtime_seed_assets(runtime_root=linked_runtime, registry_path=registry, apply=True)

    assert target.read_bytes() == original
    assert not (real_runtime / "data/.retired-seed-assets").exists()


def test_interrupted_delete_is_recovered_from_private_pending_file(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    original = b"old managed seed\n"
    runtime_root, target = _runtime_with_target(tmp_path, original)
    registry = _write_registry(tmp_path, content=original)
    real_unlink = retired_assets._unlink_verified_pending

    def interrupt_once(runtime: Path, asset: retired_assets.RetiredSeedAsset) -> None:
        monkeypatch.setattr(retired_assets, "_unlink_verified_pending", real_unlink)
        raise OSError("simulated interruption")

    monkeypatch.setattr(retired_assets, "_unlink_verified_pending", interrupt_once)
    with pytest.raises(OSError, match="simulated interruption"):
        retire_runtime_seed_assets(runtime_root=runtime_root, registry_path=registry, apply=True)

    pending = runtime_root / "data/.retired-seed-assets/pending/test-retired-seed-v1"
    backup = runtime_root / f"data/.retired-seed-assets/backups/test-retired-seed-v1/{hashlib.sha256(original).hexdigest()}"
    assert not target.exists()
    assert pending.read_bytes() == original
    assert backup.read_bytes() == original

    recovered = retire_runtime_seed_assets(runtime_root=runtime_root, registry_path=registry, apply=True)

    assert _single_result(recovered)["status"] == "recovered_removed"
    assert not pending.exists()
    assert not target.exists()
    assert backup.read_bytes() == original


@pytest.mark.parametrize(
    "relative_path",
    [Path("../outside.json"), Path("data/.retired-seed-assets/backups/poison"), Path("workspace/file.json")],
)
def test_registry_path_escape_and_control_tree_targets_are_rejected(tmp_path: Path, relative_path: Path) -> None:
    runtime_root = tmp_path / "runtime"
    (runtime_root / "data").mkdir(parents=True)
    registry = _write_registry(tmp_path, content=b"x", relative_path=relative_path)

    with pytest.raises(ValueError):
        retire_runtime_seed_assets(runtime_root=runtime_root, registry_path=registry, apply=True)

    assert not (runtime_root / "data/.retired-seed-assets").exists()


def test_operator_cannot_inject_private_paths_or_content_into_audit(tmp_path: Path) -> None:
    runtime_root, target = _runtime_with_target(tmp_path, b"managed\n")
    registry = _write_registry(tmp_path, content=target.read_bytes())

    with pytest.raises(ValueError, match="safe identifier"):
        retire_runtime_seed_assets(
            runtime_root=runtime_root,
            registry_path=registry,
            apply=True,
            operator="/private/path\nsecret",
        )

    assert target.exists()
    assert not (runtime_root / "data/.retired-seed-assets").exists()


def test_container_entrypoint_runs_retirement_after_bootstrap_and_workspace_policy() -> None:
    dockerfile = (REPO_ROOT / "docker/Dockerfile").read_text(encoding="utf-8")
    entrypoint = (REPO_ROOT / "docker/entrypoint.sh").read_text(encoding="utf-8")
    script_name = "retire_runtime_seed_assets.py"

    assert f"COPY scripts/{script_name} /app/scripts/{script_name}" in dockerfile
    assert entrypoint.index("bootstrap_runtime_volume.py") < entrypoint.index("reconcile_business_agent_workspace_policy.py")
    assert entrypoint.index("reconcile_business_agent_workspace_policy.py") < entrypoint.index(script_name)
    assert entrypoint.rindex('relax_volume_permissions "${DATA_DIR:-/data}"') < entrypoint.index(script_name)
    assert f"python /app/scripts/{script_name}" in entrypoint
    assert "--registry /app/docker/runtime-volume-seeds/workspace-policy/retired-seed-assets.json" in entrypoint
    assert "--apply" in entrypoint[entrypoint.index(script_name) :]
    assert "|| true" not in entrypoint[entrypoint.index(script_name) :]
