from __future__ import annotations

import hashlib
import json
import os
import secrets
import subprocess
import sys
from dataclasses import replace
from pathlib import Path
from types import SimpleNamespace

import pytest

from runtime_container_acceptance_test_support import candidate_authority, candidate_reservation, load_module

REPO_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(REPO_ROOT))
environment = load_module("agentgov_container_acceptance_environment_tests", REPO_ROOT / "scripts/container_acceptance_environment.py")
environment.acceptance_contract.acceptance_toolchain.activate_toolchain_authority(
    environment.acceptance_contract.acceptance_toolchain.capture_toolchain_authority()
)


def test_environment_import_does_not_load_runtime_third_party_packages() -> None:
    source = f"""
import importlib.util,sys
from pathlib import Path
repository=Path({str(REPO_ROOT)!r})
sys.path.insert(0,str(repository))
spec=importlib.util.spec_from_file_location('agentgov_environment_cold_import',repository/'scripts/container_acceptance_environment.py')
assert spec is not None and spec.loader is not None
module=importlib.util.module_from_spec(spec)
sys.modules[spec.name]=module
spec.loader.exec_module(module)
assert 'pydantic' not in sys.modules
assert 'httpx' not in sys.modules
"""
    completed = subprocess.run(
        (str(REPO_ROOT / ".venv/bin/python"), "-I", "-P", "-S", "-X", "pycache_prefix=/dev/null", "-c", source),
        cwd=REPO_ROOT,
        check=False,
        capture_output=True,
        text=True,
        env={"LC_ALL": "C.UTF-8"},
        timeout=10,
    )

    assert completed.returncode == 0, completed.stderr


def test_stale_volume_authority_works_without_runtime_third_party_packages() -> None:
    source = f"""
import importlib.util,json,sys
from pathlib import Path
repository=Path({str(REPO_ROOT)!r})
sys.path.insert(0,str(repository))
spec=importlib.util.spec_from_file_location('agentgov_environment_stale_cold',repository/'scripts/container_acceptance_environment.py')
assert spec is not None and spec.loader is not None
module=importlib.util.module_from_spec(spec)
sys.modules[spec.name]=module
spec.loader.exec_module(module)
project='agentgov-acceptance-abcdefgh1234'
volume_name=project+'_agent-test-runs'
container_id='a'*64
mountpoint='/var/lib/docker/volumes/'+volume_name+'/_data'
scope=module.acceptance_support.sandbox_scope_id(volume_name)
def runner(command,*,capture=False):
    del capture
    if command[-3:]==['config','--format','json']:
        return json.dumps({{'name':project,'volumes':{{'agent-test-runs':{{'driver':'local','name':volume_name}}}}}})
    if command[:3]==['docker','volume','inspect']:
        return json.dumps([{{'Name':volume_name,'Driver':'local','Scope':'local','Options':None,'Mountpoint':mountpoint,'Labels':{{'com.docker.compose.project':project,'com.docker.compose.volume':'agent-test-runs'}}}}])
    if command[:2]==['docker','inspect']:
        return json.dumps([{{'Id':container_id,'Config':{{'Labels':{{module.SANDBOX_KIND_LABEL:'true',module.SANDBOX_SCOPE_LABEL:scope}}}},'Mounts':[{{'Type':'volume','Name':volume_name,'Source':mountpoint,'Destination':'/workspace','Driver':'local','RW':False}}]}}])
    raise AssertionError(command)
name=module._resolve_runs_volume(['docker','compose'],project,runner)
volume=module._inspect_runs_volume(name,project_name=project,runner=runner)
module._inspect_volume_user(container_id,volume,runner)
assert 'pydantic' not in sys.modules
assert 'httpx' not in sys.modules
"""
    completed = subprocess.run(
        (str(REPO_ROOT / ".venv/bin/python"), "-I", "-P", "-S", "-X", "pycache_prefix=/dev/null", "-c", source),
        cwd=REPO_ROOT,
        check=False,
        capture_output=True,
        text=True,
        env={"LC_ALL": "C.UTF-8"},
        timeout=10,
    )

    assert completed.returncode == 0, completed.stderr


@pytest.fixture(autouse=True)
def _synthetic_candidate_verification(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(environment.acceptance_candidate, "verify_candidate_snapshot", lambda _candidate: None)
    monkeypatch.setattr(
        environment.acceptance_candidate,
        "require_snapshot_loaded_file",
        lambda _candidate, _loaded_file, _relative: None,
    )
    monkeypatch.setattr(
        environment.acceptance_contract,
        "build_managed_environment",
        lambda _environ, *, managed_values: environment.acceptance_contract.ManagedAcceptanceEnvironment(managed_values),
    )


def _run_id() -> str:
    return f"1700000000-{secrets.token_hex(6)}"


def _identity(path: Path) -> object:
    return environment.candidate_authority.CandidatePathIdentity.from_stat(path.stat(follow_symlinks=False))


def _candidate(tmp_path: Path, profile_name: str = "agent-test") -> object:
    run_id = _run_id()
    parent = tmp_path / "candidates"
    parent.mkdir(mode=0o700)
    reservation = candidate_reservation(environment, run_id=run_id, profile=profile_name, parent=parent)
    prepared = candidate_authority(
        environment,
        run_id=run_id,
        profile=profile_name,
        reservation=reservation,
        reserved_receipt_sha256="e" * 64,
    )
    root = reservation.root
    repository = root / environment.candidate_authority.SNAPSHOT_REPOSITORY
    runtime = root / environment.candidate_authority.SNAPSHOT_RUNTIME
    repository.joinpath("docker/runtime-bootstrap").mkdir(parents=True)
    repository.joinpath("VERSION").write_text("0.1.0\n", encoding="utf-8")
    runtime.mkdir(mode=0o700)
    env_file = root / environment.candidate_authority.SNAPSHOT_ENV
    env_file.write_text("API_KEY=test\n", encoding="utf-8")
    env_file.chmod(0o400)
    version_digest = hashlib.sha256(repository.joinpath("VERSION").read_bytes()).hexdigest()
    loaded = (environment.candidate_authority.LoadedSourceIdentity("VERSION", version_digest),)
    snapshot = replace(
        prepared.snapshot,
        parent_identity=_identity(parent),
        root=root,
        root_identity=_identity(root),
        repository_root=repository,
        repository_identity=_identity(repository),
        env_file=env_file,
        env_identity=_identity(env_file),
        runtime_root=runtime,
        runtime_identity=_identity(runtime),
        runtime_bootstrap=repository / "docker/runtime-bootstrap",
        runtime_bootstrap_identity=_identity(repository / "docker/runtime-bootstrap"),
        loaded_sources=loaded,
        loaded_sources_sha256=environment.candidate_authority.loaded_sources_digest(loaded),
    )
    return replace(prepared, snapshot=snapshot)


def _ports() -> object:
    values = iter(range(58080, 58100))
    return lambda: next(values)


def test_runtime_uses_precreated_candidate_child_and_retains_it_for_a_cleanup(tmp_path: Path) -> None:
    candidate = _candidate(tmp_path)
    profile = environment.acceptance_contract.PROFILES["agent-test"]
    runtime = environment.prepare_isolated_runtime(candidate, profile)
    external = tmp_path / "external"
    external.mkdir()
    external.joinpath("keep.txt").write_text("keep", encoding="utf-8")
    runtime.path.joinpath("volumes/data/external-link").symlink_to(external, target_is_directory=True)
    runtime.path.joinpath("volumes/data/runtime.txt").write_text("runtime", encoding="utf-8")

    environment.cleanup_candidate_runtime_root(runtime)

    assert candidate.runtime_root.is_dir()
    assert not tuple(candidate.runtime_root.iterdir())
    assert external.joinpath("keep.txt").read_text(encoding="utf-8") == "keep"


def test_all_profiles_create_private_runtime_environment_children(tmp_path: Path) -> None:
    for profile_name in environment.acceptance_contract.PROFILES:
        profile_root = tmp_path / profile_name
        profile_root.mkdir()
        candidate = _candidate(profile_root, profile_name)
        profile = environment.acceptance_contract.PROFILES[profile_name]
        runtime = environment.prepare_isolated_runtime(candidate, profile)
        try:
            for leaf in ("home", "xdg-config", "buildx", "tmp", "screenshots"):
                path = runtime.path / leaf
                assert path.is_dir() and path.stat().st_mode & 0o777 == 0o700
            if profile.isolated_runtime:
                assert (runtime.path / "volumes/data").is_dir()
        finally:
            environment.cleanup_candidate_runtime_root(runtime)


def test_existing_runtime_child_fails_closed_and_leaves_stable_root(tmp_path: Path) -> None:
    candidate = _candidate(tmp_path)
    candidate.runtime_root.joinpath("home").mkdir()
    candidate.runtime_root.joinpath("home/marker").write_text("residue", encoding="utf-8")

    with pytest.raises(environment.AcceptanceEnvironmentError, match="准备失败"):
        environment.prepare_isolated_runtime(candidate, environment.acceptance_contract.PROFILES["agent-test"])

    assert candidate.runtime_root.is_dir()
    assert not tuple(candidate.runtime_root.iterdir())


def test_runtime_root_replacement_is_reported_without_deleting_replacement(tmp_path: Path) -> None:
    candidate = _candidate(tmp_path)
    runtime = environment.prepare_isolated_runtime(candidate, environment.acceptance_contract.PROFILES["agent-test"])
    displaced = candidate.snapshot.root / "displaced-runtime"
    runtime.path.rename(displaced)
    runtime.path.mkdir()
    marker = runtime.path / "replacement"
    marker.write_text("keep", encoding="utf-8")

    with pytest.raises(environment.AcceptanceEnvironmentError, match="identity|residue"):
        environment.cleanup_candidate_runtime_root(runtime)

    assert marker.read_text(encoding="utf-8") == "keep"
    assert displaced.joinpath("home").is_dir()


def test_candidate_root_replacement_is_reported_without_path_cleanup(tmp_path: Path) -> None:
    candidate = _candidate(tmp_path)
    runtime = environment.prepare_isolated_runtime(candidate, environment.acceptance_contract.PROFILES["agent-test"])
    displaced = candidate.snapshot.parent / "displaced-candidate"
    candidate.snapshot.root.rename(displaced)
    candidate.snapshot.root.mkdir()
    replacement_runtime = candidate.snapshot.root / "runtime"
    replacement_runtime.mkdir()
    replacement_runtime.joinpath("replacement").write_text("keep", encoding="utf-8")

    with pytest.raises(environment.AcceptanceEnvironmentError, match="父目录 identity|residue"):
        environment.cleanup_candidate_runtime_root(runtime)

    assert replacement_runtime.joinpath("replacement").read_text(encoding="utf-8") == "keep"
    assert displaced.joinpath("runtime/home").is_dir()


def test_runtime_descriptors_are_not_inherited(tmp_path: Path) -> None:
    candidate = _candidate(tmp_path)
    runtime = environment.prepare_isolated_runtime(candidate, environment.acceptance_contract.PROFILES["agent-test"])
    try:
        assert os.get_inheritable(runtime.snapshot_parent_descriptor) is False
        assert os.get_inheritable(runtime.parent_descriptor) is False
        assert os.get_inheritable(runtime.root_descriptor) is False
    finally:
        environment.cleanup_candidate_runtime_root(runtime)


def test_snapshot_runner_reopens_and_verifies_prepared_runtime(tmp_path: Path) -> None:
    candidate = _candidate(tmp_path)
    profile = environment.acceptance_contract.PROFILES["agent-test"]
    prepared = environment.prepare_isolated_runtime(candidate, profile)
    prepared.close()

    reopened = environment.open_prepared_runtime(candidate, profile)

    environment.cleanup_candidate_runtime_root(reopened)
    assert candidate.runtime_root.is_dir()
    assert not tuple(candidate.runtime_root.iterdir())


def test_snapshot_runner_rejects_replaced_runtime_child_without_cleanup(tmp_path: Path) -> None:
    candidate = _candidate(tmp_path)
    profile = environment.acceptance_contract.PROFILES["agent-test"]
    prepared = environment.prepare_isolated_runtime(candidate, profile)
    prepared.close()
    original = candidate.runtime_root / "home"
    original.rename(candidate.runtime_root / "original-home")
    replacement = candidate.runtime_root / "home"
    replacement.mkdir(mode=0o700)
    replacement.joinpath("keep").write_text("keep", encoding="utf-8")

    with pytest.raises(environment.AcceptanceEnvironmentError, match="布局"):
        environment.open_prepared_runtime(candidate, profile)

    assert replacement.joinpath("keep").read_text(encoding="utf-8") == "keep"


def test_runtime_cleanup_entry_budget_is_bounded(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    candidate = _candidate(tmp_path)
    runtime = environment.prepare_isolated_runtime(candidate, environment.acceptance_contract.PROFILES["agent-test"])
    runtime.path.joinpath("volumes/data/one").write_text("one", encoding="utf-8")
    runtime.path.joinpath("volumes/data/two").write_text("two", encoding="utf-8")
    monkeypatch.setattr(environment, "_MAX_CLEANUP_ENTRIES", 1)

    with pytest.raises(environment.AcceptanceEnvironmentError, match="条目超限"):
        environment.cleanup_candidate_runtime_root(runtime)
    assert candidate.runtime_root.exists()


@pytest.mark.parametrize("profile_name", ["agent-test", "isolated-health"])
def test_isolated_profile_paths_are_derived_only_from_candidate_runtime(tmp_path: Path, profile_name: str) -> None:
    candidate = _candidate(tmp_path, profile_name)
    profile = environment.acceptance_contract.PROFILES[profile_name]
    runtime = environment.prepare_isolated_runtime(candidate, profile)
    try:
        managed = environment.build_acceptance_env(
            profile,
            candidate,
            runtime,
            {"HOME": "/redirected", "AGENT_GOV_ACCEPTANCE_LOCK_FD": "999"},
            available_port=_ports(),
        )
        assert managed[environment.RUNTIME_ROOT_ENV] == str(candidate.runtime_root)
        assert managed["HOME"] == str(candidate.runtime_root / "home")
        assert managed["HOST_RUNTIME_VOLUME_ROOT"] == str(candidate.runtime_root / "volumes")
        assert managed["COMPOSE_ENV_FILE"] == str(candidate.env_file)
        assert managed["LITELLM_LOCAL_MODEL_COST_MAP"] == "True"
        assert managed["VERIFY_SCREENSHOT_DIR"] == str(candidate.runtime_root / "screenshots")
        assert managed[environment.acceptance_contract.acceptance_toolchain.PNPM_DEPENDENCY_ROOT_ENV] == str(candidate.snapshot.pnpm_dependencies.root)
        assert managed["AGENT_GOV_ACCEPTANCE_PNPM_DEPENDENCIES_SHA256"] == candidate.snapshot.pnpm_dependencies.sha256
        assert "AGENT_GOV_ACCEPTANCE_LOCK_FD" not in managed
    finally:
        environment.cleanup_candidate_runtime_root(runtime)


@pytest.mark.parametrize("profile_name", ["core", "langfuse"])
def test_shared_profiles_keep_persistent_mounts_outside_private_home(tmp_path: Path, profile_name: str) -> None:
    candidate = _candidate(tmp_path, profile_name)
    profile = environment.acceptance_contract.PROFILES[profile_name]
    runtime = environment.prepare_isolated_runtime(candidate, profile)
    try:
        managed = environment.build_acceptance_env(profile, candidate, runtime, {}, available_port=_ports())
        persistent = environment.tool_authority.trusted_home() / "volume-agent-gov"
        assert managed["HOME"] == str(candidate.runtime_root / "home")
        assert managed["HOST_RUNTIME_VOLUME_ROOT"] == str(persistent)
        assert managed["HOST_DATA_MOUNT"] == str(persistent / "data")
        if profile_name == "langfuse":
            assert managed["LANGFUSE_POSTGRES_DATA_MOUNT"] == str(persistent / "langfuse/postgres")
            assert managed["LANGFUSE_MINIO_DATA_MOUNT"] == str(persistent / "langfuse/minio")
    finally:
        environment.cleanup_candidate_runtime_root(runtime)


def test_stale_isolated_cleanup_uses_only_frozen_scope_before_candidate(monkeypatch: pytest.MonkeyPatch) -> None:
    run_id = _run_id()
    recovery = object()
    calls: list[tuple[str, object]] = []

    def cleanup_scope(**values: object) -> None:
        calls.append(("docker", values))

    monkeypatch.setattr(environment.acceptance_support, "cleanup_stale_acceptance_scope", cleanup_scope)
    monkeypatch.setattr(
        environment.acceptance_candidate,
        "recover_and_cleanup_candidate_snapshot",
        lambda authority: calls.append(("candidate", authority)),
    )

    def docker_runner(_command: list[str]) -> str:
        return ""

    environment.cleanup_stale_prepared(
        SimpleNamespace(profile="agent-test", run_id=run_id, candidate_snapshot=recovery),
        docker_runner,
    )

    assert [call[0] for call in calls] == ["docker", "candidate"]
    scope = calls[0][1]
    assert isinstance(scope, dict)
    assert scope["project_name"] == f"agentgov-acceptance-{environment._runtime_token(run_id)}"
    assert scope["expected_services"] == environment.acceptance_contract.PROFILES["agent-test"].expected_services
    assert scope["docker_runner"] is docker_runner


def test_stale_shared_cleanup_never_touches_live_docker_scope(monkeypatch: pytest.MonkeyPatch) -> None:
    recovery = object()
    docker_cleanup = pytest.fail
    monkeypatch.setattr(environment.acceptance_support, "cleanup_stale_acceptance_scope", docker_cleanup)
    cleaned: list[object] = []
    monkeypatch.setattr(environment.acceptance_candidate, "recover_and_cleanup_candidate_snapshot", cleaned.append)

    environment.cleanup_stale_prepared(
        SimpleNamespace(profile="core", run_id=_run_id(), candidate_snapshot=recovery),
        lambda _command: pytest.fail("shared profile must not query Docker"),
    )

    assert cleaned == [recovery]


def test_stale_docker_cleanup_failure_preserves_candidate(monkeypatch: pytest.MonkeyPatch) -> None:
    cleaned: list[object] = []
    monkeypatch.setattr(
        environment.acceptance_support,
        "cleanup_stale_acceptance_scope",
        lambda **_values: (_ for _ in ()).throw(environment.acceptance_support.AcceptanceSupportError("failed")),
    )
    monkeypatch.setattr(environment.acceptance_candidate, "recover_and_cleanup_candidate_snapshot", cleaned.append)

    with pytest.raises(environment.AcceptanceEnvironmentError, match="Docker scope"):
        environment.cleanup_stale_prepared(
            SimpleNamespace(profile="isolated-health", run_id=_run_id(), candidate_snapshot=object()),
            lambda _command: "",
        )

    assert cleaned == []


def test_agent_test_cleanup_bounds_compose_stop_before_runner_deadline() -> None:
    project = "agentgov-acceptance-abcdefgh1234"
    compose_base = ["/usr/bin/docker", "compose", "--project-name", project]
    volume = f"{project}_agent-test-runs"
    network = f"{project}_default"
    config = json.dumps(
        {
            "name": project,
            "volumes": {"agent-test-runs": {"driver": "local", "name": volume}},
            "networks": {"default": {"driver": "bridge", "name": network}},
        }
    )
    commands: list[list[str]] = []

    def runner(command: list[str], *, capture: bool = False) -> str:
        del capture
        commands.append(command)
        if command[-3:] == ["config", "--format", "json"]:
            return config
        return ""

    cleaned: list[str] = []
    runtime = SimpleNamespace(cleanup=lambda: cleaned.append("runtime"))
    environment.cleanup_isolated_runtime(
        profile_name="agent-test",
        expected_services=environment.acceptance_contract.PROFILES["agent-test"].expected_services,
        compose_base=compose_base,
        project_name=project,
        run_id="1700000000-abcdef123456",
        runtime=runtime,
        runner=runner,
    )

    timeout = environment._COMPOSE_STOP_TIMEOUT_SECONDS
    assert [*compose_base, "stop", "--timeout", timeout, "agent-test-worker"] in commands
    assert [*compose_base, "down", "--timeout", timeout, "--volumes", "--remove-orphans"] in commands
    assert cleaned == ["runtime"]


def test_isolated_health_cleanup_uses_network_only_model() -> None:
    project = "agentgov-acceptance-health123"
    compose_base = ["/usr/bin/docker", "compose", "--project-name", project]
    config = json.dumps(
        {
            "name": project,
            "networks": {"default": {"driver": "bridge", "name": f"{project}_default"}},
        }
    )
    commands: list[list[str]] = []

    def runner(command: list[str], *, capture: bool = False) -> str:
        del capture
        commands.append(command)
        if command[-3:] == ["config", "--format", "json"]:
            return config
        return ""

    cleaned: list[str] = []
    environment.cleanup_isolated_runtime(
        profile_name="isolated-health",
        expected_services=environment.acceptance_contract.PROFILES["isolated-health"].expected_services,
        compose_base=compose_base,
        project_name=project,
        run_id="1700000000-health123456",
        runtime=SimpleNamespace(cleanup=lambda: cleaned.append("runtime")),
        runner=runner,
    )

    timeout = environment._COMPOSE_STOP_TIMEOUT_SECONDS
    assert [*compose_base, "down", "--timeout", timeout, "--volumes", "--remove-orphans"] in commands
    assert not any(command[1:3] in (["volume", "ls"], ["volume", "inspect"], ["volume", "rm"]) for command in commands)
    assert cleaned == ["runtime"]
