from __future__ import annotations

import hashlib
import importlib
import inspect
import io
import json
import os
import shutil
import subprocess
import sys
import tarfile
from pathlib import Path

import pytest
from scripts import agentscope_atomic_cutover_daemon as daemon_support
from scripts import agentscope_atomic_cutover_env as env_support
from scripts import container_acceptance_frozen_runner as frozen_runner
from scripts import container_acceptance_inputs as inputs
from scripts import container_acceptance_refresh as acceptance_refresh
from scripts import container_acceptance_toolchain as acceptance_toolchain
from scripts import run_container_acceptance as acceptance
from scripts import run_selected_env_operation as selected_operation


def _capture_child_environment(child_env: dict[str, str], output: Path, names: tuple[str, ...]) -> dict[str, str | None]:
    code = (
        "import json,os,sys; "
        "names=json.loads(sys.argv[2]); "
        "payload={name:os.environ.get(name) for name in names}; "
        "open(sys.argv[1],'w',encoding='utf-8').write(json.dumps(payload))"
    )
    result = acceptance._run_child([sys.executable, "-c", code, str(output), json.dumps(names)], child_env)
    assert result == 0
    payload = json.loads(output.read_text(encoding="utf-8"))
    assert isinstance(payload, dict)
    return payload


@pytest.mark.parametrize("profile_name", ["core", "langfuse"])
def test_refresh_profile_declares_build_then_published_snapshot_preparation_then_up(profile_name: str) -> None:
    source = inspect.getsource(acceptance_refresh)
    build = '"build", "--pull=false", *profile.build_services'
    inspect_probe = 'actions.inspect_api_image(env, "daemon probe 镜像检查")'
    daemon_boundary = "actions.verify_daemon"
    prepare = '"app.runtime.published_harness_preparation"'
    start = '"up",'

    assert source.count(daemon_boundary) == 2
    assert (
        source.index(build)
        < source.index(inspect_probe)
        < source.index(daemon_boundary)
        < source.index(prepare)
        < source.index(start)
        < source.rindex(daemon_boundary)
        < source.index("docker_monitor.start()")
        < source.index("for container in containers:")
    )
    assert source.index("actions.verify_identity") < source.index("docker_monitor.bind(containers)")
    assert '"--rm"' in source and '"--no-deps"' in source and '"--pull",\n            "never"' in source
    assert '"--service-ports"' not in source
    assert acceptance.PROFILES[profile_name].build_services


def test_normal_deployment_uses_the_same_preparation_entry_before_up() -> None:
    makefile = (Path(__file__).resolve().parents[1] / "Makefile").read_text(encoding="utf-8")
    for target in ("up", "all-up"):
        recipe = makefile.split(f"\n{target}:", 1)[1].split("\n\n", 1)[0]
        assert "$(SELECTED_ENV_RUNNER)" in recipe
        assert f"--operation {target}" in recipe
    start = inspect.getsource(selected_operation._start_stack)
    assert start.index("_preflight") < start.index("_bootstrap") < start.index("_prepare_harnesses") < start.index("_run(command")
    prepare = inspect.getsource(selected_operation._prepare_harnesses)
    assert '"--entrypoint"' in prepare
    assert '"app.runtime.published_harness_preparation"' in prepare


def test_acceptance_runner_allows_only_exact_public_make_targets() -> None:
    target, digest = acceptance._validated_acceptance_command(
        acceptance.PROFILES["core"],
        ["/usr/bin/make", "--no-print-directory", "_container-core-smoke"],
    )
    assert target == "container-core-smoke"
    assert len(digest) == 64 and all(character in "0123456789abcdef" for character in digest)
    mcp_target, _mcp_digest = acceptance._validated_acceptance_command(
        acceptance.PROFILES["core"],
        ["/usr/bin/make", "--no-print-directory", "_container-mcp-technical-smoke"],
    )
    assert mcp_target == "container-mcp-technical-smoke"

    for hostile in (
        ["true"],
        ["/usr/bin/make", "--no-print-directory", "_unknown-acceptance"],
        ["/usr/bin/make", "--no-print-directory", "_container-core-smoke", "PYTHON_RUN=true"],
    ):
        with pytest.raises(acceptance.AcceptanceError, match="公开 Make 验收目标"):
            acceptance._validated_acceptance_command(acceptance.PROFILES["core"], hostile)

    with pytest.raises(acceptance.AcceptanceError, match="profile"):
        acceptance._validated_acceptance_command(
            acceptance.PROFILES["langfuse"],
            ["/usr/bin/make", "--no-print-directory", "_container-core-smoke"],
        )


def test_isolated_credentials_are_ephemeral_and_consumers_have_one_definition(tmp_path: Path) -> None:
    source = tmp_path / "source.env"
    original = "API_KEY=change-me\nFRONTEND_RUNTIME_API_KEY=old-key\nMODEL_PROVIDER_API_KEY=private-provider\n"
    source.write_text(original, encoding="utf-8")
    first_root = tmp_path / "first"
    second_root = tmp_path / "second"
    first_root.mkdir()
    second_root.mkdir()

    first = acceptance.prepare_isolated_environment(source, "1234-first", first_root)
    second = acceptance.prepare_isolated_environment(source, "1234-second", second_root)

    assert first.overrides["API_KEY"] != second.overrides["API_KEY"]
    assert len(bytes.fromhex(first.overrides["API_KEY"])) == 32
    assert first.overrides["FRONTEND_RUNTIME_API_KEY"] == first.overrides["API_KEY"]
    assert all(
        50400 <= int(first.overrides[key]) <= 50499
        for key in (
            "HOST_PORT",
            "FRONTEND_HOST_PORT",
            "LANGFUSE_HOST_PORT",
            "LANGFUSE_MINIO_HOST_PORT",
            "LANGFUSE_MINIO_CONSOLE_HOST_PORT",
        )
    )
    lines = first.env_file.read_text(encoding="utf-8").splitlines()
    for key in first.overrides:
        assert [line for line in lines if line.startswith(key + "=")] == [key + "=" + first.overrides[key]]
    assert "MODEL_PROVIDER_API_KEY=private-provider" in lines
    assert "change-me" not in first.env_file.read_text(encoding="utf-8")
    assert first.env_file.stat().st_mode & 0o777 == 0o600
    assert source.read_text(encoding="utf-8") == original


def test_child_process_uses_controlled_environment_without_host_config_or_proxies(tmp_path: Path, process_environment) -> None:
    source = tmp_path / "source.env"
    source.write_text("MODEL_PROVIDER_API_KEY=selected-provider\nAGENTSCOPE_MODEL_NAME=selected-model\n", encoding="utf-8")
    host_values = {
        "MODEL_PROVIDER_API_KEY": "host-provider",
        "UNRELATED_PRIVATE_VALUE": "host-private",
        "HTTP_PROXY": "http://uppercase-http.invalid",
        "HTTPS_PROXY": "http://uppercase-https.invalid",
        "ALL_PROXY": "http://uppercase-all.invalid",
        "HOME": str(tmp_path / "hostile-home"),
        "http_proxy": "http://lowercase-http.invalid",
        "https_proxy": "http://lowercase-https.invalid",
        "all_proxy": "http://lowercase-all.invalid",
        "NO_PROXY": "host-no-proxy",
        "no_proxy": "host-no-proxy",
        "DOCKER_HOST": "unix:///tmp/agentgov-test-docker.sock",
        "BUILDX_CONFIG": str(tmp_path / "hostile-buildx-state"),
        "BUILDX_BUILDER": "remote",
        "AGENTGOV_SOURCE_ARTIFACT_SHA256": "0" * 64,
        "REQUIRE_LIVE_RUNTIME": "1",
        "LIVE_ACCEPTANCE_ARGS": "; false # must never reach an acceptance shell",
        "LIVE_ACCEPTANCE_RUNS": "7",
        "LIVE_ACCEPTANCE_CONCURRENCY": "2",
        "LIVE_ACCEPTANCE_REQUIRE_TRACE_COMPLETE": "1",
        "QUALITY_POLICY": "; true # must never select acceptance policy",
        "TEST_ARTIFACT_ROOT": "; true # must never select artifact root",
        "TECHNICAL_SCENARIO_FILE": str(tmp_path / "operator-technical.json"),
    }
    for name, value in host_values.items():
        process_environment.set(name, value)

    (tmp_path / "operator-technical.json").write_text('{"reviewed":"original"}\n', encoding="utf-8")
    private_root = tmp_path / "private"
    private_root.mkdir(mode=0o700)
    private_root.chmod(0o700)
    snapshots = inputs.snapshot_scenario_files(private_root, dict(os.environ))
    isolation = acceptance.prepare_isolated_environment(source, "1234-envcheck", private_root)
    child_env = acceptance.build_acceptance_env(
        acceptance.PROFILES["langfuse"],
        isolation,
        "1234-envcheck",
        dict(os.environ),
        snapshots,
    )
    inspected_names = (
        *sorted(acceptance.PROXY_ENV_KEYS),
        "NO_PROXY",
        "no_proxy",
        "MODEL_PROVIDER_API_KEY",
        "UNRELATED_PRIVATE_VALUE",
        "DOCKER_HOST",
        "DOCKER_CONTEXT",
        "DOCKER_CONFIG",
        "BUILDX_CONFIG",
        "BUILDX_BUILDER",
        "AGENTGOV_SOURCE_ARTIFACT_SHA256",
        "HOME",
        "PATH",
        "REQUIRE_LIVE_RUNTIME",
        "LIVE_ACCEPTANCE_ARGS",
        "LIVE_ACCEPTANCE_RUNS",
        "LIVE_ACCEPTANCE_CONCURRENCY",
        "LIVE_ACCEPTANCE_REQUIRE_TRACE_COMPLETE",
        "QUALITY_POLICY",
        "TEST_ARTIFACT_ROOT",
        "TECHNICAL_SCENARIO_FILE",
        "COMPOSE_ENV_FILE",
    )
    captured = _capture_child_environment(child_env, tmp_path / "child-env.json", inspected_names)

    assert all(captured[name] is None for name in acceptance.PROXY_ENV_KEYS)
    assert captured["NO_PROXY"] == captured["no_proxy"] == acceptance.LOOPBACK_NO_PROXY
    assert captured["MODEL_PROVIDER_API_KEY"] is None
    assert captured["UNRELATED_PRIVATE_VALUE"] is None
    assert captured["DOCKER_HOST"] == "unix:///var/run/docker.sock"
    assert captured["DOCKER_CONTEXT"] is None
    assert captured["DOCKER_CONFIG"] == str(isolation.runtime_root / "docker-config")
    assert captured["BUILDX_CONFIG"] == str(isolation.runtime_root / "buildx-state")
    assert captured["BUILDX_BUILDER"] == "default"
    assert captured["AGENTGOV_SOURCE_ARTIFACT_SHA256"] == isolation.overrides["AGENTGOV_SOURCE_ARTIFACT_SHA256"]
    assert captured["AGENTGOV_SOURCE_ARTIFACT_SHA256"] != host_values["AGENTGOV_SOURCE_ARTIFACT_SHA256"]
    assert captured["HOME"] == str(acceptance_toolchain.TRUSTED_USER_HOME)
    assert captured["PATH"] == acceptance_toolchain.TRUSTED_SYSTEM_PATH
    assert captured["REQUIRE_LIVE_RUNTIME"] == "1"
    assert captured["LIVE_ACCEPTANCE_ARGS"] is None
    assert captured["LIVE_ACCEPTANCE_RUNS"] == "7"
    assert captured["LIVE_ACCEPTANCE_CONCURRENCY"] == "2"
    assert captured["LIVE_ACCEPTANCE_REQUIRE_TRACE_COMPLETE"] is None
    assert captured["QUALITY_POLICY"] is None
    assert captured["TEST_ARTIFACT_ROOT"] is None
    assert "$(LIVE_ACCEPTANCE_ARGS)" not in (acceptance.REPO_ROOT / "Makefile").read_text(encoding="utf-8")
    snapshot_path = Path(str(captured["TECHNICAL_SCENARIO_FILE"]))
    assert snapshot_path != Path(host_values["TECHNICAL_SCENARIO_FILE"])
    assert snapshot_path.read_text(encoding="utf-8") == '{"reviewed":"original"}\n'
    assert snapshot_path.stat().st_mode & 0o777 == 0o600
    assert snapshot_path.parent.stat().st_mode & 0o777 == 0o700
    assert captured["COMPOSE_ENV_FILE"] == str(isolation.env_file)
    assert "MODEL_PROVIDER_API_KEY=selected-provider" in isolation.env_file.read_text(encoding="utf-8").splitlines()


def test_buildx_state_binding_rejects_remote_builder_and_mutable_path(tmp_path: Path) -> None:
    source = tmp_path / "source.env"
    source.write_text("AGENTGOV_API_MODE=open\n", encoding="utf-8")
    isolation = acceptance.prepare_isolated_environment(source, "1234-buildx", tmp_path)
    child_env = acceptance.build_acceptance_env(acceptance.PROFILES["core"], isolation, "1234-buildx", dict(os.environ))
    buildx_state = isolation.runtime_root / "buildx-state"
    assert buildx_state.stat().st_mode & 0o777 == 0o700
    acceptance_toolchain.verify_acceptance_toolchain(child_env)
    for replacement in ({"BUILDX_BUILDER": "remote"}, {"BUILDX_CONFIG": str(tmp_path / "external")}):
        with pytest.raises(ValueError, match="Buildx state/builder"):
            acceptance_toolchain.verify_acceptance_toolchain({**child_env, **replacement})
    buildx_state.chmod(0o755)
    with pytest.raises(ValueError, match="0700 真实目录"):
        acceptance_toolchain.verify_acceptance_toolchain(child_env)
    buildx_state.chmod(0o700)
    buildx_state.rmdir()
    buildx_state.symlink_to(tmp_path, target_is_directory=True)
    with pytest.raises(ValueError, match="0700 真实目录"):
        acceptance_toolchain.verify_acceptance_toolchain(child_env)


def test_container_build_contract_injects_computed_source_digest_into_all_local_images(tmp_path: Path) -> None:
    source = tmp_path / "source.env"
    source.write_text("MODEL_PROVIDER_API_KEY=selected-provider\n", encoding="utf-8")
    isolation = acceptance.prepare_isolated_environment(source, "1234-source-digest", tmp_path)
    digest = isolation.overrides["AGENTGOV_SOURCE_ARTIFACT_SHA256"]
    compose = (acceptance.REPO_ROOT / "docker/docker-compose.yml").read_text(encoding="utf-8")

    assert len(digest) == 64 and set(digest) <= set("0123456789abcdef")
    assert sum(line.lstrip().startswith("AGENTGOV_SOURCE_ARTIFACT_SHA256: ") for line in compose.splitlines()) == 3
    for dockerfile in ("Dockerfile", "frontend.Dockerfile", "agentscope-runtime.Dockerfile"):
        content = (acceptance.REPO_ROOT / "docker" / dockerfile).read_text(encoding="utf-8")
        assert "io.agentgov.source-artifact-sha256" in content
        assert "ARG AGENTGOV_SOURCE_ARTIFACT_SHA256" in content


def test_make_source_digest_uses_supported_python_and_fails_closed() -> None:
    makefile = (acceptance.REPO_ROOT / "Makefile").read_text(encoding="utf-8")
    valid = subprocess.run(
        ["make", "--no-print-directory", "-n", "build", f"PYTHON_RUN={sys.executable}"],
        cwd=acceptance.REPO_ROOT,
        check=False,
        capture_output=True,
        text=True,
    )
    invalid = subprocess.run(
        ["make", "--no-print-directory", "-n", "build", "PYTHON_RUN=/bin/false"],
        cwd=acceptance.REPO_ROOT,
        check=False,
        capture_output=True,
        text=True,
    )

    assert valid.returncode == 0, valid.stderr
    assert invalid.returncode != 0
    assert "source artifact SHA-256 generation failed" in invalid.stderr
    assert "$(PYTHON_RUN) -c" in makefile
    assert "python3 -c 'from pathlib import Path; from scripts.agentscope_atomic_cutover_bootstrap" not in makefile


def test_selected_env_wins_over_conflicting_host_value_in_real_compose_process(tmp_path: Path, process_environment) -> None:
    docker = shutil.which("docker")
    if docker is None or subprocess.run([docker, "compose", "version"], check=False, capture_output=True).returncode != 0:
        pytest.skip("docker compose is unavailable")
    for name in ("AGENTSCOPE_MODEL_NAME", "NO_PROXY", "APP_VERSION", "AGENTGOV_RUNTIME_VERSION"):
        process_environment.set(name, "from-host")
    source = tmp_path / "source.env"
    source.write_text(
        "MODEL_PROVIDER_API_KEY=selected-provider\nAGENTSCOPE_MODEL_NAME=from-selected-env\n"
        "NO_PROXY=from-selected-env\nAPP_VERSION=from-selected-env\n"
        "AGENTGOV_RUNTIME_VERSION=from-selected-env\n",
        encoding="utf-8",
    )
    isolation = acceptance.prepare_isolated_environment(source, "1234-compose-env", tmp_path)
    child_env = acceptance.build_acceptance_env(acceptance.PROFILES["core"], isolation, "1234-compose-env", dict(os.environ))
    result = subprocess.run(
        [*acceptance.compose_command(acceptance.PROFILES["core"], isolation.env_file, docker_path=docker), "config", "--format", "json"],
        cwd=acceptance.REPO_ROOT,
        env=child_env,
        check=False,
        capture_output=True,
        text=True,
    )

    assert result.returncode == 0, result.stderr
    services = json.loads(result.stdout)["services"]
    image_tag = isolation.overrides["APP_VERSION"]
    runtime = services["agentscope-runtime"]
    assert image_tag == "acceptance-env"
    assert runtime["image"] == f"agent-gov-agentscope-runtime:{image_tag}"
    assert services["agent-gov-api"]["image"] == f"agent-gov-api:{image_tag}"
    assert services["agent-gov-ui"]["image"] == f"agent-gov-ui:{image_tag}"
    assert runtime["environment"]["AGENTGOV_RUNTIME_VERSION"] == (acceptance.REPO_ROOT / "VERSION").read_text(encoding="utf-8").strip()
    assert runtime["environment"]["AGENTGOV_RUNTIME_VERSION"] != image_tag
    assert runtime["environment"]["AGENTSCOPE_MODEL_NAME"] == "from-selected-env"
    assert services["agent-gov-api"]["environment"]["NO_PROXY"] == acceptance.LOOPBACK_NO_PROXY


@pytest.mark.parametrize("version, message", [(None, "缺少产品 VERSION"), ("", "产品 VERSION 无效"), ("bad/version", "产品 VERSION 无效")])
def test_acceptance_rejects_missing_or_invalid_frozen_version(tmp_path: Path, version: str | None, message: str) -> None:
    frozen_root = tmp_path / "frozen-source"
    frozen_root.mkdir()
    if version is not None:
        (frozen_root / "VERSION").write_text(version, encoding="utf-8")
    selected = tmp_path / "source.env"
    selected.write_text("MODEL_PROVIDER_API_KEY=selected-provider\n", encoding="utf-8")
    with pytest.raises(acceptance.AcceptanceError, match=message):
        acceptance.prepare_isolated_environment(selected, "1234-invalid-version", tmp_path, source_root=frozen_root, source_digest="a" * 64)


def test_acceptance_fingerprint_covers_selected_and_effective_configuration(tmp_path: Path, monkeypatch) -> None:
    monkeypatch.setattr(inputs, "_tracked_and_untracked_paths", lambda *_args: ())
    source = tmp_path / "source.env"
    source.write_text("AGENTSCOPE_MODEL_NAME=selected-model\n", encoding="utf-8")
    isolation = acceptance.prepare_isolated_environment(source, "1234-fingerprint", tmp_path)
    child_env = acceptance.build_acceptance_env(acceptance.PROFILES["core"], isolation, "1234-fingerprint", {"PATH": os.defpath})
    original = acceptance.acceptance_fingerprint(source, isolation.env_file, child_env)

    assert acceptance.acceptance_fingerprint(source, isolation.env_file, child_env) == original
    changed_child_env = {**child_env, "REAL_ACTION_TIMEOUT_MS": "12345"}
    assert acceptance.acceptance_fingerprint(source, isolation.env_file, changed_child_env) != original
    isolation.env_file.write_text(isolation.env_file.read_text(encoding="utf-8") + "AGENTSCOPE_MODEL_NAME=changed\n", encoding="utf-8")
    assert acceptance.acceptance_fingerprint(source, isolation.env_file, child_env) != original


def test_scenario_snapshot_closes_original_path_to_child_time_of_check_gap(tmp_path: Path) -> None:
    operator_root = tmp_path / "operator"
    operator_root.mkdir()
    original = operator_root / "reviewed.json"
    original.write_bytes(b'{"scenario":"reviewed-before-lock"}\n')
    private_root = tmp_path / "acceptance-root"
    private_root.mkdir(mode=0o700)
    private_root.chmod(0o700)

    snapshots = inputs.snapshot_scenario_files(
        private_root,
        {"REAL_SCENARIO_FILE": str(original)},
    )
    original.write_bytes(b'{"scenario":"changed-after-snapshot"}\n')
    child_paths = inputs.snapshot_environment(snapshots)
    child_path = child_paths.get("REAL_SCENARIO_FILE")

    assert child_path is not None and child_path != str(original)
    assert Path(child_path).read_bytes() == b'{"scenario":"reviewed-before-lock"}\n'
    assert snapshots[0].sha256 != hashlib.sha256(original.read_bytes()).hexdigest()


def test_scenario_snapshot_rejects_symlinked_source_component(tmp_path: Path) -> None:
    operator_root = tmp_path / "operator"
    operator_root.mkdir()
    (operator_root / "reviewed.json").write_text("{}\n", encoding="utf-8")
    linked_root = tmp_path / "linked-operator"
    linked_root.symlink_to(operator_root, target_is_directory=True)
    private_root = tmp_path / "acceptance-root"
    private_root.mkdir(mode=0o700)
    private_root.chmod(0o700)

    with pytest.raises(acceptance.AcceptanceError, match="无符号链接"):
        inputs.snapshot_scenario_files(
            private_root,
            {"REAL_SCENARIO_FILE": str(linked_root / "reviewed.json")},
        )


def test_scenario_snapshot_hash_fails_closed_after_child_side_change(tmp_path: Path) -> None:
    original = tmp_path / "reviewed.json"
    original.write_text('{"scenario":"reviewed"}\n', encoding="utf-8")
    private_root = tmp_path / "acceptance-root"
    private_root.mkdir(mode=0o700)
    private_root.chmod(0o700)
    snapshots = inputs.snapshot_scenario_files(
        private_root,
        {"REAL_SCENARIO_FILE": str(original)},
    )
    source = tmp_path / "source.env"
    source.write_text("AGENTSCOPE_MODEL_NAME=selected-model\n", encoding="utf-8")
    isolation = acceptance.prepare_isolated_environment(source, "1234-snapshot-hash", private_root)
    child_env = acceptance.build_acceptance_env(
        acceptance.PROFILES["core"],
        isolation,
        "1234-snapshot-hash",
        {"PATH": os.defpath},
        snapshots,
    )
    before = acceptance.acceptance_fingerprint(source, isolation.env_file, child_env, snapshots)
    snapshots[0].path.write_text('{"scenario":"tampered"}\n', encoding="utf-8")

    assert before
    with pytest.raises(acceptance.AcceptanceError, match="快照在验收期间发生变化"):
        acceptance.acceptance_fingerprint(source, isolation.env_file, child_env, snapshots)


def test_runner_snapshots_scenarios_after_lock_and_fingerprints_all_three_phases() -> None:
    source = inspect.getsource(acceptance.run_acceptance)
    prepare = inspect.getsource(acceptance._prepare_frozen_acceptance)
    resume = inspect.getsource(frozen_runner.resume_frozen_acceptance)
    refreshed = inspect.getsource(acceptance._run_refreshed_acceptance)
    seal = inspect.getsource(acceptance._seal_acceptance_context)
    verify_result = inspect.getsource(acceptance._verify_refreshed_result)

    assert source.index("fcntl.flock") < source.index("_prepare_frozen_acceptance")
    assert prepare.index("snapshot_scenario_files") < prepare.index("_freeze_acceptance_source")
    assert prepare.index("_freeze_acceptance_source") < prepare.index("materialize_acceptance_toolchain")
    guard_construction = "input_guard = ExecutionMutationGuard"
    assert source.index("_prepare_frozen_acceptance") < source.index(guard_construction)
    assert source.index(guard_construction) < source.index("exec_frozen_runner")
    assert "_daemon_support(" not in source
    assert "_bootstrap_isolated_runtime(" not in source
    assert resume.index("actions.activate_guard") < resume.index("input_guard.check")
    assert resume.index("verify_acceptance_toolchain") < resume.index("check_no_test_doubles.py")
    assert resume.index("check_no_test_doubles.py") < resume.index("actions.verify_plugins")
    assert resume.index("actions.verify_plugins") < resume.index("actions.daemon_support")
    assert resume.index("actions.verify_daemon") < resume.index("actions.bootstrap")
    assert resume.index("actions.bootstrap") < resume.index("actions.run_refreshed")
    assert refreshed.index("_verify_refresh_inputs") < refreshed.index("_seal_acceptance_context")
    assert refreshed.index("_seal_acceptance_context") < refreshed.index("_run_child")
    assert refreshed.index("_run_child") < refreshed.index("_verify_refreshed_result")
    assert seal.index("write_acceptance_context") < seal.index("seal_materialized_input_tree")
    assert seal.index("add_roots") < seal.index("verify_sealed_file")
    assert seal.index("verify_sealed_file") < seal.index("验收回执封存前")
    assert verify_result.index("verify_acceptance_context") < verify_result.index("verify_frozen_running_contract")
    assert verify_result.index("verify_frozen_running_contract") < verify_result.index("docker_monitor.verify")
    assert verify_result.index("_daemon_support(child_env).verify") < verify_result.index("docker_monitor.verify")
    assert verify_result.index("docker_monitor.verify") < verify_result.index("_verify_daemon_boundary")
    assert prepare.index("pre_freeze_source_sha256 = source_fingerprint") < prepare.index("_freeze_acceptance_source")
    assert prepare.index("_freeze_acceptance_source") < prepare.index("source_fingerprint(env_file) != pre_freeze_source_sha256")


def test_formal_refresh_is_offline_and_verifies_complete_running_config() -> None:
    source = inspect.getsource(acceptance_refresh)

    assert '"build", "--pull=false"' in source
    assert source.count('"--pull",\n                "never"') == 1
    assert '"up",\n            "-d",\n            "--pull",\n            "never"' in source
    assert source.index("capture_image_inventory(") < source.index('"build", "--pull=false"')
    assert source.count("capture_image_inventory(") == 3
    assert "capture_service_image_ids(" in source
    assert "verify_running_service(" in source


def test_runner_rejects_worktree_change_immediately_after_source_freeze(tmp_path: Path, monkeypatch) -> None:
    selected = tmp_path / "selected.env"
    selected.write_text("AGENTGOV_API_MODE=open\n", encoding="utf-8")
    temp_root = tmp_path / f"agentgov-acceptance-{os.getuid()}-freeze-race"
    fingerprints = iter(("a" * 64, "b" * 64))
    prepared = False

    def make_temp_root(*, prefix: str) -> str:
        assert prefix == f"agentgov-acceptance-{os.getuid()}-"
        temp_root.mkdir(mode=0o700)
        return temp_root.as_posix()

    def freeze(root: Path) -> tuple[Path, str]:
        snapshot = root / "source-snapshot"
        snapshot.mkdir(mode=0o700)
        return snapshot, "c" * 64

    def prepare(*_args, **_kwargs):
        nonlocal prepared
        prepared = True
        raise AssertionError("changed worktree must fail before environment preparation")

    monkeypatch.setattr(acceptance, "LOCK_FILE", tmp_path / "acceptance.lock")
    monkeypatch.setattr(acceptance.tempfile, "mkdtemp", make_temp_root)
    monkeypatch.setattr(acceptance, "snapshot_scenario_files", lambda *_args: ())
    monkeypatch.setattr(acceptance, "source_fingerprint", lambda _path: next(fingerprints))
    monkeypatch.setattr(acceptance, "_freeze_acceptance_source", freeze)
    monkeypatch.setattr(acceptance, "prepare_isolated_environment", prepare)

    with pytest.raises(acceptance.AcceptanceError, match="冻结容器构建 source 期间"):
        acceptance.run_acceptance(
            acceptance.PROFILES["core"],
            selected,
            ["/usr/bin/make", "--no-print-directory", "_container-core-smoke"],
            {},
        )

    assert prepared is False
    assert not temp_root.exists()


def test_daemon_boundary_verifies_identity_nonce_and_identity_in_order(tmp_path: Path, monkeypatch) -> None:
    calls: list[tuple[str, object]] = []

    class Daemon:
        def verify(self, _env, expected):
            calls.append(("identity", expected))

        def verify_host_filesystem(self, _env, probe_root, image_id):
            calls.append(("nonce", (probe_root, image_id)))

    monkeypatch.setattr(acceptance, "_daemon_support", lambda _env: Daemon())
    identity = {"endpoint": "unix:///var/run/docker.sock", "id": "engine-one"}

    acceptance._verify_daemon_boundary(
        {"APP_VERSION": "4.0.0", "DOCKER_HOST": "unix:///var/run/docker.sock"},
        identity,
        tmp_path,
    )

    assert calls == [
        ("identity", identity),
        ("nonce", (tmp_path, acceptance.HOST_FILESYSTEM_PROBE_IMAGE)),
        ("identity", identity),
    ]


@pytest.mark.parametrize(
    "payload",
    [
        "API_KEY=first\nAPI_KEY=second\n",
        "export API_KEY=first\nAPI_KEY=second\n",
    ],
)
def test_acceptance_env_rejects_duplicate_keys(tmp_path: Path, payload: str) -> None:
    selected = tmp_path / "selected.env"
    selected.write_text(payload, encoding="utf-8")

    with pytest.raises(acceptance.AcceptanceError, match="重复键"):
        acceptance.resolve_env_file(acceptance.PROFILES["core"], selected, {})


@pytest.mark.parametrize("link_parent", [False, True])
def test_acceptance_env_rejects_leaf_and_parent_symlinks(tmp_path: Path, link_parent: bool) -> None:
    real_parent = tmp_path / "real"
    real_parent.mkdir()
    target = real_parent / "selected.env"
    target.write_text("AGENTGOV_API_MODE=open\n", encoding="utf-8")
    if link_parent:
        alias = tmp_path / "alias"
        alias.symlink_to(real_parent, target_is_directory=True)
        selected = alias / "selected.env"
    else:
        selected = tmp_path / "selected-link.env"
        selected.symlink_to(target)

    with pytest.raises(acceptance.AcceptanceError, match="符号链接"):
        acceptance.resolve_env_file(acceptance.PROFILES["core"], selected, {})


def test_acceptance_env_reader_rejects_directory_entry_swap(tmp_path: Path, monkeypatch) -> None:
    selected = tmp_path / "selected.env"
    selected.write_text("API_KEY=stable-before-read\n", encoding="utf-8")
    original = tmp_path / "selected.original"
    real_read = env_support.os.read
    swapped = False

    def swapping_read(descriptor: int, size: int) -> bytes:
        nonlocal swapped
        payload = real_read(descriptor, size)
        if payload and not swapped:
            swapped = True
            selected.rename(original)
            selected.write_text("API_KEY=replaced-during-read\n", encoding="utf-8")
        return payload

    monkeypatch.setattr(env_support.os, "read", swapping_read)

    with pytest.raises(acceptance.AcceptanceError, match="读取期间发生变化"):
        acceptance.resolve_env_file(acceptance.PROFILES["core"], selected, {})


def test_daemon_capture_binds_unix_socket_metadata(monkeypatch) -> None:
    mode = 0o140660
    metadata = type(
        "SocketMetadata",
        (),
        {"st_dev": 17, "st_ino": 29, "st_uid": 41, "st_mode": mode},
    )()
    support = daemon_support.CutoverDaemonSupport(error_type=acceptance.AcceptanceError, run_command=lambda *_a, **_k: "")
    monkeypatch.setattr(support, "_effective_endpoint", lambda _env: "unix:///run/docker.sock")
    monkeypatch.setattr(support, "_daemon_id", lambda _env: "engine-one")
    monkeypatch.setattr(daemon_support.os, "stat", lambda *_args, **_kwargs: metadata)

    assert support.capture({}) == {
        "endpoint": "unix:///run/docker.sock",
        "id": "engine-one",
        "socket_device": 17,
        "socket_inode": 29,
        "socket_uid": 41,
        "socket_mode": mode,
    }


def _write_test_image_archive(
    archive: Path,
    *,
    architecture: str,
    image: str,
    source_digest: str,
) -> None:
    config = json.dumps(
        {
            "architecture": architecture,
            "os": "linux",
            "config": {"Labels": {"io.agentgov.source-artifact-sha256": source_digest}},
        },
        sort_keys=True,
        separators=(",", ":"),
    ).encode()
    config_name = f"{hashlib.sha256(config).hexdigest()}.json"
    manifest = json.dumps([{"Config": config_name, "RepoTags": [image], "Layers": []}]).encode()
    with tarfile.open(archive, "w:gz") as stream:
        for name, payload in (("manifest.json", manifest), (config_name, config)):
            member = tarfile.TarInfo(name)
            member.size = len(payload)
            stream.addfile(member, io.BytesIO(payload))


def test_image_archive_metadata_is_verified_without_loading(tmp_path: Path) -> None:
    archive_support = importlib.import_module("scripts.agentscope_atomic_cutover_archive")
    archive = tmp_path / "project-images.tar.gz"
    digest = "a" * 64
    image = "agent-gov-api:4.0.0"
    _write_test_image_archive(archive, architecture="amd64", image=image, source_digest=digest)

    archive_support.validate_image_archive(
        archive,
        architecture="amd64",
        expected_images=frozenset({image}),
        source_digest=digest,
    )
    with pytest.raises(archive_support.ImageArchiveError, match="platform"):
        archive_support.validate_image_archive(archive, architecture="arm64")
    with pytest.raises(archive_support.ImageArchiveError, match="source artifact"):
        archive_support.validate_image_archive(archive, architecture="amd64", source_digest="b" * 64)


def test_acceptance_rejects_frozen_source_drift_before_child_command(tmp_path: Path, monkeypatch) -> None:
    source_env = tmp_path / "source.env"
    effective_env = tmp_path / "effective.env"
    source_root = tmp_path / "source-snapshot"
    runtime_root = tmp_path / "runtime-root"
    source_env.write_text("A=1\n", encoding="utf-8")
    effective_env.write_text("A=1\n", encoding="utf-8")
    source_root.mkdir()
    runtime_root.mkdir()
    isolation = acceptance.IsolatedEnvironment(
        effective_env,
        runtime_root,
        "project",
        "container",
        {"AGENTGOV_SOURCE_ARTIFACT_SHA256": "a" * 64},
        source_root,
    )
    child_called = False

    def child(*_args, **_kwargs) -> int:
        nonlocal child_called
        child_called = True
        return 0

    monkeypatch.setattr(acceptance, "refresh_profile", lambda *_args: ())
    monkeypatch.setattr(acceptance, "source_artifact_sha256", lambda _root: "b" * 64)
    monkeypatch.setattr(acceptance, "_run_child", child)
    child_env = acceptance_toolchain.toolchain_environment(acceptance_toolchain.capture_acceptance_toolchain(dict(os.environ), ""))
    child_env[inputs.LIVE_SOURCE_ROOT_ENV] = str(source_root)

    try:
        with pytest.raises(acceptance.AcceptanceError, match="冻结的容器构建 source"):
            acceptance._run_refreshed_acceptance(
                acceptance.PROFILES["core"],
                source_env,
                ["must-not-run"],
                isolation,
                child_env,
                {},
                (),
                "c" * 64,
                "d" * 64,
            )
    finally:
        acceptance._ACTIVE_DOCKER_MONITOR = None

    assert not child_called


def test_selected_env_build_rejects_frozen_source_drift(tmp_path: Path, monkeypatch) -> None:
    selected = tmp_path / "selected.env"
    selected.write_text("AGENTGOV_API_MODE=open\n", encoding="utf-8")

    def freeze(_root: Path, destination: Path) -> str:
        destination.mkdir(mode=0o700)
        (destination / "VERSION").write_text("4.0.0\n", encoding="utf-8")
        return "a" * 64

    def source_digest(root: Path) -> str:
        return "a" * 64 if root == selected_operation.REPO_ROOT else "b" * 64

    monkeypatch.setattr(selected_operation.selected_env_reexec, "freeze_deployable_source", freeze)
    monkeypatch.setattr(selected_operation.selected_env_reexec, "source_artifact_sha256", source_digest)

    with pytest.raises(selected_operation.SelectedEnvError, match="冻结.*deployable source"):
        selected_operation.run_operation(selected, "build")


def test_selected_env_rejects_symlinked_explicit_base_directory(tmp_path: Path) -> None:
    selected = tmp_path / "selected.env"
    selected.write_text("AGENTGOV_API_MODE=open\n", encoding="utf-8")
    real_base = tmp_path / "real-base"
    real_base.mkdir()
    alias = tmp_path / "base-alias"
    alias.symlink_to(real_base, target_is_directory=True)

    with pytest.raises(selected_operation.SelectedEnvError, match="基准目录无效"):
        selected_operation.run_operation(selected, "down", env_base_dir=alias)


def test_acceptance_source_receipt_rejects_changed_source_hash(tmp_path: Path, monkeypatch) -> None:
    monkeypatch.setattr(inputs, "_tracked_and_untracked_paths", lambda *_args: ())
    tmp_path.chmod(0o700)
    selected_env = tmp_path / "selected.env"
    selected_env.write_text("AGENTSCOPE_MODEL_NAME=local\n", encoding="utf-8")
    selected_env.chmod(0o600)
    source_sha256 = inputs.source_artifact_sha256(inputs.REPO_ROOT)
    current = {
        inputs.LIVE_SOURCE_ROOT_ENV: str(inputs.REPO_ROOT),
        inputs.DEPLOYABLE_SOURCE_ROOT_ENV: str(inputs.REPO_ROOT),
        acceptance_toolchain.FORMAL_SOURCE_ROOT_ENV: str(inputs.REPO_ROOT),
        acceptance_toolchain.TOOL_PATH_ENV_KEYS["git"]: "/usr/bin/git",
        "COMPOSE_ENV_FILE": str(selected_env),
    }
    payload = {
        "source_env": str(selected_env),
        "source_fingerprint_sha256": inputs.source_fingerprint(
            selected_env,
            repo_root=inputs.REPO_ROOT,
            git_path=Path("/usr/bin/git"),
        ),
        "frozen_source_sha256": source_sha256,
        "source_env_sha256": hashlib.sha256(selected_env.read_bytes()).hexdigest(),
        "effective_env_sha256": hashlib.sha256(selected_env.read_bytes()).hexdigest(),
        "toolchain": {"artifacts": [{"name": "formal-source", "path": str(inputs.REPO_ROOT)}]},
    }
    inputs._verify_context_sources(current, payload)  # type: ignore[arg-type]
    payload["source_env_sha256"] = "0" * 64
    with pytest.raises(inputs.AcceptanceError, match="验收源 env 已变化"):
        inputs._verify_context_sources(current, payload)  # type: ignore[arg-type]


@pytest.mark.parametrize("field", ["container_id", "image_id"])
def test_container_receipt_rejects_invalid_identity(field: str) -> None:
    identities = [
        {"service": service, "container_id": char * 64, "image_id": f"sha256:{char * 64}"}
        for service, char in zip(sorted(inputs.CORE_ACCEPTANCE_SERVICES), "abc", strict=True)
    ]
    identities[0][field] = "invalid"

    with pytest.raises(inputs.AcceptanceError, match="容器或镜像 ID 无效"):
        inputs._parse_container_identities(identities, inputs.CORE_ACCEPTANCE_SERVICES)


def test_service_url_must_match_observed_docker_port() -> None:
    with pytest.raises(inputs.AcceptanceError, match="agent-gov-api.*Docker 发布端口不一致"):
        inputs._verify_service_url_binding(
            "http://127.0.0.1:50491",
            service="agent-gov-api",
            published_ports=frozenset({50490}),
        )
