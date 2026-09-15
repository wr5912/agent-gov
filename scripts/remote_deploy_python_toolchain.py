#!/usr/bin/env python3
"""Build and verify the remote deploy runner's frozen Python toolchain."""

from __future__ import annotations

import argparse
import hashlib
import importlib.metadata
import json
import os
import platform
import re
import shutil
import stat
import subprocess
import sys
import sysconfig
import tempfile
from collections.abc import Mapping, Sequence
from pathlib import Path
from typing import Literal, TypedDict, cast

SCHEMA = "agentgov-remote-deploy-python-toolchain-v1"
MANIFEST_NAME = "manifest.json"
VERIFIER_NAME = "verify.py"
RECOVERY_SOURCE_FILES = (
    "scripts/__init__.py",
    "scripts/agentscope_atomic_cutover_archive.py",
    "scripts/agentscope_atomic_cutover_bootstrap.py",
    "scripts/agentscope_atomic_cutover_env.py",
    "scripts/agentscope_atomic_cutover_images.py",
    "scripts/remote_deploy_transaction.py",
    "scripts/remote_deploy_runtime.py",
)
SOURCE_FILES = (
    "requirements-api.txt",
    "scripts/selected_env_python_toolchain.py",
    "scripts/remote_deploy_python_toolchain.py",
    *RECOVERY_SOURCE_FILES,
)
IMPORTS = (
    "annotated_types",
    "greenlet",
    "pydantic",
    "pydantic_core",
    "dotenv",
    "yaml",
    "sqlalchemy",
    "typing_extensions",
    "typing_inspection",
)
_SHA256 = re.compile(r"[0-9a-f]{64}")


class PythonAbiContract(TypedDict):
    implementation: str
    version: list[int]
    cache_tag: str | None
    soabi: str | None
    platform: str
    machine: str


class DistributionContract(TypedDict):
    name: str
    version: str


class DirectoryContract(TypedDict):
    path: str
    type: Literal["directory"]
    mode: int


class RegularFileContract(TypedDict):
    path: str
    type: Literal["file"]
    mode: int
    size: int
    sha256: str


FileContract = DirectoryContract | RegularFileContract
ProcessEnvironment = dict[str, str]

SourceBindings = dict[str, str]


class UnverifiedToolchainManifest(TypedDict, total=False):
    schema: object
    python: object
    source_files: object
    distributions: object
    files: object


class ToolchainManifest(TypedDict):
    schema: str
    python: PythonAbiContract
    source_files: SourceBindings
    distributions: list[DistributionContract]
    files: list[FileContract]


class ToolchainError(RuntimeError):
    """The remote deploy Python toolchain cannot be proven trustworthy."""


def _canonical_name(name: str) -> str:
    return re.sub(r"[-_.]+", "-", name).lower()


def _regular_file_sha256(path: Path) -> str:
    metadata = path.lstat()
    if path.is_symlink() or not stat.S_ISREG(metadata.st_mode):
        raise ToolchainError(f"必须是普通非符号链接文件: {path}")
    descriptor = os.open(path, os.O_RDONLY | os.O_CLOEXEC | os.O_NOFOLLOW)
    digest = hashlib.sha256()
    try:
        while chunk := os.read(descriptor, 1024 * 1024):
            digest.update(chunk)
    finally:
        os.close(descriptor)
    return digest.hexdigest()


def _abi_contract() -> PythonAbiContract:
    return {
        "implementation": sys.implementation.name,
        "version": [sys.version_info.major, sys.version_info.minor],
        "cache_tag": sys.implementation.cache_tag,
        "soabi": sysconfig.get_config_var("SOABI"),
        "platform": sysconfig.get_platform(),
        "machine": platform.machine(),
    }


def _distribution_contract(dependencies: Path, expected_names: Sequence[str] | None = None) -> list[DistributionContract]:
    records: dict[str, str] = {}
    for distribution in importlib.metadata.distributions(path=[dependencies.as_posix()]):
        name = _canonical_name(distribution.metadata["Name"])
        if name in records:
            raise ToolchainError(f"冻结 Python 闭包含重复 distribution: {name}")
        records[name] = distribution.version
    if expected_names is not None:
        expected = {_canonical_name(name) for name in expected_names}
        if set(records) != expected:
            raise ToolchainError(f"冻结 Python 闭包与 selected-env runner 不一致: {sorted(set(records) ^ expected)}")
    if not records:
        raise ToolchainError("冻结 Python distribution 闭包为空")
    return [DistributionContract(name=name, version=records[name]) for name in sorted(records)]


def _validate_requirements(source_root: Path, distributions: Sequence[DistributionContract]) -> None:
    try:
        from packaging.requirements import Requirement
    except ImportError as exc:
        raise ToolchainError("本机项目环境缺少 requirements 校验依赖") from exc
    installed = {item["name"]: item["version"] for item in distributions}
    for raw_line in (source_root / "requirements-api.txt").read_text(encoding="utf-8").splitlines():
        line = raw_line.strip()
        if not line or line.startswith(("#", "-r ")):
            continue
        requirement = Requirement(line)
        name = _canonical_name(requirement.name)
        if name in installed and requirement.specifier and installed[name] not in requirement.specifier:
            raise ToolchainError(f"冻结 Python 依赖不满足 requirements-api.txt: {name}")


def _file_contract(root: Path) -> list[FileContract]:
    records: list[FileContract] = []
    for path in sorted(root.rglob("*")):
        relative = path.relative_to(root).as_posix()
        metadata = path.lstat()
        if path.is_symlink():
            raise ToolchainError(f"冻结 Python 闭包含符号链接: {relative}")
        if stat.S_ISDIR(metadata.st_mode):
            records.append(DirectoryContract(path=relative, type="directory", mode=stat.S_IMODE(metadata.st_mode)))
        elif stat.S_ISREG(metadata.st_mode):
            records.append(
                RegularFileContract(
                    path=relative,
                    type="file",
                    mode=stat.S_IMODE(metadata.st_mode),
                    size=metadata.st_size,
                    sha256=_regular_file_sha256(path),
                )
            )
        else:
            raise ToolchainError(f"冻结 Python 闭包含特殊文件: {relative}")
    if not records:
        raise ToolchainError("冻结 Python 文件闭包为空")
    return records


def _write_exclusive(path: Path, payload: bytes, mode: int) -> None:
    descriptor = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_CLOEXEC | os.O_NOFOLLOW, mode)
    try:
        view = memoryview(payload)
        while view:
            view = view[os.write(descriptor, view) :]
        os.fsync(descriptor)
    finally:
        os.close(descriptor)


def _materialize_recovery_runner(source_root: Path, toolchain: Path) -> None:
    recovery_root = toolchain / "recovery"
    for relative in RECOVERY_SOURCE_FILES:
        source = source_root / relative
        destination = recovery_root / relative
        destination.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
        shutil.copyfile(source, destination)
        destination.chmod(0o400)
    for directory in sorted((path for path in recovery_root.rglob("*") if path.is_dir()), reverse=True):
        directory.chmod(0o500)
    recovery_root.chmod(0o500)


def build_bundle(source_root: Path, output: Path) -> ToolchainManifest:
    """Materialize the existing selected-env toolchain and bind it to target source."""
    source_root = source_root.resolve(strict=True)
    if output.exists() or output.is_symlink():
        raise ToolchainError("远端 Python bundle 目标必须不存在")
    for relative in SOURCE_FILES:
        _regular_file_sha256(source_root / relative)
    if source_root.as_posix() not in sys.path:
        sys.path.insert(0, source_root.as_posix())
    from scripts import selected_env_python_toolchain as selected_toolchain

    output.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
    with tempfile.TemporaryDirectory(prefix="agentgov-remote-python-", dir=output.parent) as raw_build:
        build_root = Path(raw_build)
        environment = selected_toolchain.prepare_python_toolchain(build_root / "closure")
        toolchain = Path(environment[selected_toolchain.PYTHON_ROOT_ENV])
        distributions = _distribution_contract(toolchain / "dependencies", selected_toolchain._REQUIRED_DISTRIBUTIONS)
        _validate_requirements(source_root, distributions)
        toolchain.chmod(0o700)
        _materialize_recovery_runner(source_root, toolchain)
        manifest = ToolchainManifest(
            schema=SCHEMA,
            python=_abi_contract(),
            source_files={relative: _regular_file_sha256(source_root / relative) for relative in SOURCE_FILES},
            distributions=distributions,
            files=_file_contract(toolchain),
        )
        verifier = source_root / "scripts/remote_deploy_python_toolchain.py"
        _write_exclusive(toolchain / VERIFIER_NAME, verifier.read_bytes(), 0o500)
        _write_exclusive(
            toolchain / MANIFEST_NAME,
            (json.dumps(manifest, sort_keys=True, separators=(",", ":")) + "\n").encode(),
            0o400,
        )
        os.rename(toolchain, output)
        output.chmod(0o500)
    return manifest


def _load_manifest(bundle: Path) -> UnverifiedToolchainManifest:
    try:
        manifest = json.loads((bundle / MANIFEST_NAME).read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        raise ToolchainError("远端 Python toolchain manifest 无效") from exc
    if not isinstance(manifest, dict) or manifest.get("schema") != SCHEMA:
        raise ToolchainError("远端 Python toolchain schema 无效")
    return cast(UnverifiedToolchainManifest, manifest)


def _manifest_sha256(bundle: Path) -> str:
    return _regular_file_sha256(bundle / MANIFEST_NAME)


def _absolute_real_directory(path: Path, label: str) -> Path:
    absolute = Path(os.path.abspath(path))
    if path != absolute or path.is_symlink():
        raise ToolchainError(f"{label} 必须是绝对普通目录")
    try:
        resolved = path.resolve(strict=True)
    except OSError as exc:
        raise ToolchainError(f"{label} 不存在") from exc
    if resolved != path or not path.is_dir():
        raise ToolchainError(f"{label} 必须是绝对普通目录")
    return path


def _verify_source_bindings(manifest: UnverifiedToolchainManifest, source_root: Path | None) -> None:
    bindings = manifest.get("source_files")
    if not isinstance(bindings, dict) or set(bindings) != set(SOURCE_FILES):
        raise ToolchainError("远端 Python toolchain 源码绑定无效")
    if source_root is None:
        return
    source_root = _absolute_real_directory(source_root, "候选 source")
    for relative in SOURCE_FILES:
        digest = bindings.get(relative)
        if not isinstance(digest, str) or _SHA256.fullmatch(digest) is None or _regular_file_sha256(source_root / relative) != digest:
            raise ToolchainError(f"远端 Python toolchain 与部署源码不一致: {relative}")


def _verify_file_contract(bundle: Path, manifest: UnverifiedToolchainManifest) -> None:
    expected_root = {"runtime", "dependencies", "recovery", MANIFEST_NAME, VERIFIER_NAME}
    if {path.name for path in bundle.iterdir()} != expected_root:
        raise ToolchainError("远端 Python toolchain 根文件集合无效")
    root_metadata = bundle.lstat()
    if root_metadata.st_uid != os.geteuid() or stat.S_IMODE(root_metadata.st_mode) != 0o500:
        raise ToolchainError("远端 Python toolchain 根 owner/mode 无效")
    for name, mode in ((MANIFEST_NAME, 0o400), (VERIFIER_NAME, 0o500)):
        metadata = (bundle / name).lstat()
        if metadata.st_uid != os.geteuid() or stat.S_IMODE(metadata.st_mode) != mode:
            raise ToolchainError(f"远端 Python toolchain {name} owner/mode 无效")
    records = manifest.get("files")
    if not isinstance(records, list):
        raise ToolchainError("远端 Python toolchain 文件清单无效")
    expected_paths: set[str] = set()
    for item in records:
        if not isinstance(item, dict) or not isinstance(item.get("path"), str):
            raise ToolchainError("远端 Python toolchain 文件记录无效")
        relative = item["path"]
        if not relative or Path(relative).is_absolute() or ".." in Path(relative).parts or relative in expected_paths:
            raise ToolchainError("远端 Python toolchain 文件路径无效")
        expected_paths.add(relative)
        path = bundle / relative
        metadata = path.lstat()
        actual_type = "directory" if stat.S_ISDIR(metadata.st_mode) else "file" if stat.S_ISREG(metadata.st_mode) else "invalid"
        if path.is_symlink() or metadata.st_uid != os.geteuid() or actual_type != item.get("type") or stat.S_IMODE(metadata.st_mode) != item.get("mode"):
            raise ToolchainError(f"远端 Python toolchain 类型或 mode 漂移: {relative}")
        if actual_type == "file" and (metadata.st_size != item.get("size") or _regular_file_sha256(path) != item.get("sha256")):
            raise ToolchainError(f"远端 Python toolchain 文件摘要漂移: {relative}")
    actual_paths = {
        path.relative_to(bundle).as_posix() for root in (bundle / "runtime", bundle / "dependencies", bundle / "recovery") for path in (root, *root.rglob("*"))
    }
    if actual_paths != expected_paths:
        raise ToolchainError("远端 Python toolchain 文件集合漂移")


def _runner_validation_code() -> str:
    return """
import importlib
import importlib.metadata
import json
import pathlib
import platform
import sys
import sysconfig
manifest = json.loads(pathlib.Path(sys.argv[1]).read_text(encoding='utf-8'))
actual_python = {
    'implementation': sys.implementation.name,
    'version': [sys.version_info.major, sys.version_info.minor],
    'cache_tag': sys.implementation.cache_tag,
    'soabi': sysconfig.get_config_var('SOABI'),
    'platform': sysconfig.get_platform(),
    'machine': platform.machine(),
}
assert actual_python == manifest['python']
actual = {item['name']: importlib.metadata.distribution(item['name']).version for item in manifest['distributions']}
assert actual == {item['name']: item['version'] for item in manifest['distributions']}
for name in sys.argv[2].split(','):
    importlib.import_module(name)
sys.path.insert(0, str(pathlib.Path(sys.argv[1]).parent / 'recovery'))
importlib.import_module('scripts.remote_deploy_transaction')
importlib.import_module('scripts.remote_deploy_runtime')
if len(sys.argv) == 4:
    source_root = pathlib.Path(sys.argv[3])
    assert (source_root / 'scripts/run_selected_env_operation.py').is_file()
print(json.dumps({'python': actual_python, 'distributions': actual}, sort_keys=True))
""".strip()


def _verify_executable(bundle: Path, manifest: UnverifiedToolchainManifest, source_root: Path | None) -> None:
    python = bundle / "runtime/bin/python"
    environment = {
        "HOME": bundle.as_posix(),
        "PATH": os.defpath,
        "PYTHONHOME": (bundle / "runtime").as_posix(),
        "PYTHONPATH": (bundle / "dependencies").as_posix(),
        "PYTHONNOUSERSITE": "1",
        "PYTHONDONTWRITEBYTECODE": "1",
    }
    command = [python.as_posix(), "-S", "-c", _runner_validation_code(), (bundle / MANIFEST_NAME).as_posix(), ",".join(IMPORTS)]
    if source_root is not None:
        command.append(source_root.resolve(strict=True).as_posix())
    result = subprocess.run(command, check=False, capture_output=True, text=True, env=environment)
    if result.returncode != 0:
        raise ToolchainError("远端 Python toolchain ABI、distribution 或可执行导入校验失败")


def verify_bundle(
    bundle: Path,
    source_root: Path | None = None,
    *,
    require_cache_key: bool = False,
) -> ToolchainManifest:
    """Verify every portable identity and execute the frozen interpreter for real."""
    bundle = _absolute_real_directory(bundle, "远端 Python toolchain")
    manifest = _load_manifest(bundle)
    if require_cache_key and bundle.name != _manifest_sha256(bundle):
        raise ToolchainError("远端 Python toolchain cache key 与 manifest 摘要不一致")
    _verify_source_bindings(manifest, source_root)
    source_files = manifest.get("source_files")
    if not isinstance(source_files, dict):
        raise ToolchainError("远端 Python toolchain 源码绑定无效")
    verifier_digest = source_files.get("scripts/remote_deploy_python_toolchain.py")
    if verifier_digest != _regular_file_sha256(bundle / VERIFIER_NAME):
        raise ToolchainError("远端 Python toolchain verifier 摘要无效")
    _verify_file_contract(bundle, manifest)
    distributions = manifest.get("distributions")
    if not isinstance(distributions, list) or _distribution_contract(bundle / "dependencies") != distributions:
        raise ToolchainError("远端 Python distribution 版本清单漂移")
    _verify_executable(bundle, manifest, source_root)
    return cast(ToolchainManifest, manifest)


def _source_artifact_sha256(source_root: Path) -> str:
    root_text = source_root.as_posix()
    if root_text not in sys.path:
        sys.path.insert(0, root_text)
    from scripts.agentscope_atomic_cutover_bootstrap import source_artifact_sha256

    return source_artifact_sha256(source_root)


def _bind_source_import_root(source_root: Path) -> None:
    for root in (source_root, Path(__file__).resolve().parents[1]):
        root_text = root.as_posix()
        if root_text not in sys.path:
            sys.path.insert(0, root_text)


def _parse_env_values(source_root: Path, payload: bytes) -> Mapping[str, str]:
    root_text = source_root.as_posix()
    if root_text not in sys.path:
        sys.path.insert(0, root_text)
    from scripts.agentscope_atomic_cutover_env import parse_selected_env_payload

    bindings = parse_selected_env_payload(payload)
    return {binding.key: binding.value or "" for binding in bindings if binding.key is not None}


def _enabled(values: Mapping[str, str], key: str, default: str) -> bool:
    value = values.get(key, default).strip().casefold()
    if value in {"true", "1", "yes", "on", "t", "y"}:
        return True
    if value in {"false", "0", "no", "off", "f", "n"}:
        return False
    raise ToolchainError(f"{key} 必须是布尔值")


def _validate_required_private_env(values: Mapping[str, str]) -> bool:
    from scripts.selected_env_operation_contract import RETIRED_CUTOVER_KEYS

    if values.get("AGENTGOV_API_MODE", "").strip() != "open":
        raise ToolchainError("候选 env 必须明确使用 AGENTGOV_API_MODE=open")
    api_key = values.get("API_KEY", "").strip()
    required = ("API_KEY", "AGENTGOV_RUNTIME_SHARED_SECRET", "MODEL_PROVIDER_API_KEY")
    for key in required:
        value = values.get(key, "").strip()
        if not value or value == "change-me" or value.startswith("replace-with-"):
            raise ToolchainError(f"候选 env 缺少有效私有配置: {key}")
    if values.get("FRONTEND_RUNTIME_API_KEY", "").strip() != api_key:
        raise ToolchainError("候选 env 的 API_KEY 与 FRONTEND_RUNTIME_API_KEY 必须一致")
    populated = sorted(key for key in RETIRED_CUTOVER_KEYS if values.get(key, "").strip())
    if populated:
        raise ToolchainError("候选 env 仍包含已退役 cutover 状态")
    langfuse_enabled = _enabled(values, "LANGFUSE_ENABLED", "false")
    local_langfuse = langfuse_enabled and values.get("LANGFUSE_BASE_URL", "http://langfuse-web:3000").rstrip("/") == "http://langfuse-web:3000"
    if langfuse_enabled:
        _validate_langfuse_private_env(values, self_hosted=local_langfuse)
    return local_langfuse


def _validate_langfuse_private_env(values: Mapping[str, str], *, self_hosted: bool) -> None:
    required = ["LANGFUSE_PUBLIC_KEY", "LANGFUSE_SECRET_KEY"]
    if self_hosted:
        required.extend(
            (
                "LANGFUSE_SALT",
                "LANGFUSE_NEXTAUTH_SECRET",
                "LANGFUSE_POSTGRES_PASSWORD",
                "LANGFUSE_CLICKHOUSE_PASSWORD",
                "LANGFUSE_REDIS_AUTH",
                "LANGFUSE_MINIO_ROOT_PASSWORD",
            )
        )
    if self_hosted and values.get("LANGFUSE_INIT_USER_EMAIL", "").strip():
        required.append("LANGFUSE_INIT_USER_PASSWORD")
    for key in required:
        value = values.get(key, "").strip()
        forbidden = value.startswith(("change-me", "replace-with-")) or "agentgov-local" in value
        if len(value) < 16 or forbidden or value.startswith("langfuse-") or value == "minio":
            raise ToolchainError(f"候选 env 缺少有效 Langfuse 私有配置: {key}")
    if self_hosted:
        encryption_key = values.get("LANGFUSE_ENCRYPTION_KEY", "").strip()
        if _SHA256.fullmatch(encryption_key) is None or encryption_key == "0" * 64:
            raise ToolchainError("候选 env 的 LANGFUSE_ENCRYPTION_KEY 无效")


def validate_candidate_env(source_root: Path, env_file: Path) -> bool:
    _bind_source_import_root(source_root)
    from scripts.remote_deploy_transaction import read_stable_regular_file

    payload, _identity = read_stable_regular_file(env_file)
    return _validate_required_private_env(_parse_env_values(source_root, payload))


def _frozen_environment(bundle: Path) -> ProcessEnvironment:
    return {
        "HOME": os.environ.get("HOME", bundle.as_posix()),
        "PATH": os.defpath,
        "DOCKER_HOST": "unix:///var/run/docker.sock",
        "PYTHONHOME": (bundle / "runtime").as_posix(),
        "PYTHONPATH": (bundle / "dependencies").as_posix(),
        "PYTHONNOUSERSITE": "1",
        "PYTHONDONTWRITEBYTECODE": "1",
    }


def _run_candidate_command(command: Sequence[str], *, cwd: Path, environment: Mapping[str, str], label: str) -> None:
    result = subprocess.run(command, cwd=cwd, env=environment, check=False, capture_output=True, text=True)
    if result.returncode != 0:
        raise ToolchainError(f"候选部署 {label} 失败")


def _run_candidate_preflight(bundle: Path, stage_root: Path, candidate_env: Path) -> None:
    environment = _frozen_environment(bundle)
    python = (bundle / "runtime/bin/python").as_posix()
    runner = (stage_root / "scripts/run_selected_env_operation.py").as_posix()
    _run_candidate_command(
        [python, runner, "--env-file", candidate_env.as_posix(), "--env-base-dir", (stage_root / "docker").as_posix(), "--operation", "runtime-validate"],
        cwd=stage_root,
        environment=environment,
        label="selected-env 二阶段预检",
    )


def prepare_candidate(
    bundle: Path,
    stage_root: Path,
    live_root: Path,
    expected_source_sha256: str,
) -> bool:
    """Prepare and fully preflight a private env inside an isolated source stage."""
    stage_root = _absolute_real_directory(stage_root, "候选 source stage")
    live_root = _absolute_real_directory(live_root, "live source")
    if stage_root.parent != live_root.parent or not stage_root.name.startswith(f"{live_root.name}.stage."):
        raise ToolchainError("候选 source stage 未位于 live source 的 sibling boundary")
    if _SHA256.fullmatch(expected_source_sha256) is None:
        raise ToolchainError("候选 source digest 无效")
    _bind_source_import_root(stage_root)
    from scripts.remote_deploy_transaction import (
        CandidateState,
        capture_live_env,
        read_stable_regular_file,
        verify_live_env,
        write_candidate_state,
    )

    verify_bundle(bundle, stage_root, require_cache_key=True)
    if _source_artifact_sha256(stage_root) != expected_source_sha256:
        raise ToolchainError("候选 source 与本地构建 artifact 不一致")
    candidate_env = stage_root / "docker/.env"
    if candidate_env.exists() or candidate_env.is_symlink():
        raise ToolchainError("候选 source stage 意外包含私有 env")
    live_payload, anchor = capture_live_env(live_root / "docker/.env")
    payload = live_payload or read_stable_regular_file(stage_root / "docker/.env.example")[0]
    _write_exclusive(candidate_env, payload, 0o600)
    python = bundle / "runtime/bin/python"
    _run_candidate_command(
        [python.as_posix(), (stage_root / "scripts/initialize_runtime_shared_secret.py").as_posix(), "--env-file", candidate_env.as_posix()],
        cwd=stage_root,
        environment=_frozen_environment(bundle),
        label="Runtime shared secret 初始化",
    )
    (stage_root / "docker/.env.bak-runtime-secret.lock").unlink(missing_ok=True)
    candidate_payload, _identity = read_stable_regular_file(candidate_env)
    local_langfuse = validate_candidate_env(stage_root, candidate_env)
    _run_candidate_preflight(bundle, stage_root, candidate_env)
    verify_live_env(live_root / "docker/.env", anchor)
    if _source_artifact_sha256(stage_root) != expected_source_sha256:
        raise ToolchainError("候选 source 在预检期间变化")
    state = CandidateState(
        live_root=live_root.as_posix(),
        stage_root=stage_root.as_posix(),
        source_sha256=expected_source_sha256,
        candidate_env_sha256=hashlib.sha256(candidate_payload).hexdigest(),
        live_env=anchor,
    )
    write_candidate_state(stage_root, state)
    return local_langfuse


def activate_candidate(bundle: Path, stage_root: Path, live_root: Path, backup_root: Path) -> Path:
    """Finish the already-persisted source activation transaction."""
    stage_root = _absolute_real_directory(stage_root, "候选 source stage")
    live_root = _absolute_real_directory(live_root, "live source")
    _bind_source_import_root(stage_root)
    from scripts.remote_deploy_transaction import activate_transaction, load_transaction

    verify_bundle(bundle, stage_root, require_cache_key=True)
    transaction = load_transaction(live_root)
    if transaction.stage_root != stage_root.as_posix() or transaction.backup_root != Path(os.path.abspath(backup_root)).as_posix():
        raise ToolchainError("持久部署事务路径不匹配")
    activate_transaction(live_root)
    return Path(transaction.backup_root)


def main(argv: Sequence[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    subparsers = parser.add_subparsers(dest="command", required=True)
    build = subparsers.add_parser("bundle")
    build.add_argument("--source-root", type=Path, required=True)
    build.add_argument("--output", type=Path, required=True)
    verify = subparsers.add_parser("verify")
    verify.add_argument("--bundle", type=Path, required=True)
    verify.add_argument("--source-root", type=Path)
    verify.add_argument("--require-cache-key", action="store_true")
    candidate = subparsers.add_parser("prepare-candidate")
    candidate.add_argument("--bundle", type=Path, required=True)
    candidate.add_argument("--stage-root", type=Path, required=True)
    candidate.add_argument("--live-root", type=Path, required=True)
    candidate.add_argument("--source-sha256", required=True)
    activate = subparsers.add_parser("activate-candidate")
    activate.add_argument("--bundle", type=Path, required=True)
    activate.add_argument("--stage-root", type=Path, required=True)
    activate.add_argument("--live-root", type=Path, required=True)
    activate.add_argument("--backup-root", type=Path, required=True)
    args = parser.parse_args(argv)
    try:
        if args.command == "bundle":
            build_bundle(args.source_root, args.output)
            print("远端部署 Python toolchain 已生成")
        elif args.command == "verify":
            verify_bundle(args.bundle, args.source_root, require_cache_key=args.require_cache_key)
            print("远端部署 Python toolchain 校验通过")
        elif args.command == "prepare-candidate":
            local_langfuse = prepare_candidate(args.bundle, args.stage_root, args.live_root, args.source_sha256)
            print("1" if local_langfuse else "0")
        else:
            backup = activate_candidate(args.bundle, args.stage_root, args.live_root, args.backup_root)
            print(backup)
    except (ToolchainError, RuntimeError, OSError, subprocess.SubprocessError, UnicodeError, ValueError):
        if args.command == "prepare-candidate":
            parser.exit(1, "候选部署预检失败；未触碰 live env 或 Compose 状态。\n")
        if args.command == "activate-candidate":
            parser.exit(1, "候选部署激活失败；live source/env 状态需要人工核验；未执行 Compose。\n")
        parser.exit(1, "远端部署 Python toolchain 构建或校验失败；未执行候选 env、source 激活或 Compose。\n")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
