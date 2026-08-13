from __future__ import annotations

import hashlib
import json
import os
import shutil
import subprocess
import tempfile
from collections.abc import Iterator
from pathlib import Path

import pytest

from runtime_container_acceptance_test_support import load_module

REPO_ROOT = Path(__file__).resolve().parents[1]
bootstrap = load_module("scripts.container_acceptance_bootstrap", REPO_ROOT / "scripts/container_acceptance_bootstrap.py")


@pytest.fixture
def trusted_temporary_parent() -> Iterator[Path]:
    repository = Path(__file__).resolve().parents[1]
    with tempfile.TemporaryDirectory(prefix="agentgov-cold-start-", dir=repository.parent) as path:
        yield Path(path)


def test_public_inline_loader_carries_actual_source_authority_through_fd_exec(trusted_temporary_parent: Path) -> None:
    source_repository = Path(__file__).resolve().parents[1]
    makefile = (source_repository / "Makefile").read_text(encoding="utf-8")
    assignment = next(line for line in makefile.splitlines() if line.startswith("override CONTAINER_ACCEPTANCE_BOOTSTRAP_LOADER := "))
    loader = assignment.split(" := ", 1)[1]
    python_assignment = next(line for line in makefile.splitlines() if line.startswith("override CONTAINER_ACCEPTANCE_BOOTSTRAP_PYTHON := "))
    bootstrap_python = python_assignment.split(" := ", 1)[1]
    repository = trusted_temporary_parent / "repository"
    scripts = repository / "scripts"
    scripts.mkdir(parents=True)
    app = repository / "app"
    app.mkdir()
    (app / "__init__.py").write_text("", encoding="utf-8")
    (app / "probe.py").write_text("VALUE = 29\n", encoding="utf-8")
    (repository / ".venv/bin").mkdir(parents=True)
    os.symlink(Path(bootstrap.VENV_PYTHON).resolve(strict=True), repository / ".venv/bin/python")
    copied = (
        "container_acceptance_bootstrap.py",
        "container_acceptance_import_authority.py",
    )
    for name in copied:
        shutil.copyfile(source_repository / "scripts" / name, scripts / name)
    toolchain = scripts / "container_acceptance_toolchain.py"
    toolchain.write_text(
        "import json,os,sys\n"
        "from app import probe\n"
        "def main():\n"
        " registry=globals()['_ACTUAL_LOADED_SOURCE_REGISTRY']\n"
        " actual=registry.freeze()\n"
        " evidence={'sources':actual,'repository':globals()['_ACTUAL_REPOSITORY_ROOT'],"
        "'import_root':globals()['_ACTUAL_REPOSITORY_IMPORT_ROOT'],'isolated':sys.flags.isolated,"
        "'linked':os.path.samefile(registry.import_root,registry.repository),"
        "'safe_path':sys.flags.safe_path,'no_site':sys.flags.no_site,'argv':sys.argv[1:]}\n"
        " evidence['probe']=probe.VALUE\n"
        " print(json.dumps(evidence,sort_keys=True,separators=(',',':')))\n"
        " return 0\n",
        encoding="utf-8",
    )

    completed = subprocess.run(
        (
            bootstrap_python,
            "-I",
            "-S",
            "-X",
            "pycache_prefix=/dev/null",
            "-c",
            loader,
            str(scripts / "container_acceptance_bootstrap.py"),
            "launch",
        ),
        cwd=repository,
        check=False,
        capture_output=True,
        text=True,
        env={"LC_ALL": "C.UTF-8"},
        timeout=15,
    )

    assert completed.returncode == 0, completed.stderr
    evidence = json.loads(completed.stdout)
    assert evidence["repository"] == str(repository)
    assert evidence["import_root"].startswith("/proc/self/fd/")
    assert evidence["linked"] is True
    assert evidence["argv"] == ["launch"]
    assert evidence["isolated"] == 1
    assert evidence["safe_path"] is True
    assert evidence["no_site"] == 1
    assert evidence["probe"] == 29
    expected = {f"scripts/{name}": hashlib.sha256((scripts / name).read_bytes()).hexdigest() for name in (*copied, toolchain.name)}
    expected.update({f"app/{name}": hashlib.sha256((app / name).read_bytes()).hexdigest() for name in ("__init__.py", "probe.py")})
    assert evidence["sources"] == dict(sorted(expected.items()))


def test_real_isolated_cold_start_freezes_the_runner_closure_without_venv_sources() -> None:
    repository = Path(__file__).resolve().parents[1]
    source = f"""
import hashlib,json,os,stat,sys,types
from pathlib import Path
repository=Path({str(repository)!r})
sys.path.insert(0,str(repository))
from scripts import container_acceptance_import_authority as imports
registry=imports.ActualLoadedSourceRegistry(repository)
helper_path=repository/'scripts/container_acceptance_import_authority.py'
bootstrap_path=repository/'scripts/container_acceptance_bootstrap.py'
toolchain_path=repository/'scripts/container_acceptance_toolchain.py'
helper_digest=hashlib.sha256(helper_path.read_bytes()).hexdigest()
bootstrap_digest=hashlib.sha256(bootstrap_path.read_bytes()).hexdigest()
registry.register_expected('scripts/container_acceptance_import_authority.py',helper_digest)
registry.register_expected('scripts/container_acceptance_bootstrap.py',bootstrap_digest)
sys.meta_path.insert(0,imports._RepositorySourceFinder(registry))
imports._scripts_package(registry)
sys.modules['scripts.container_acceptance_import_authority']=imports
setattr(sys.modules['scripts'],'container_acceptance_import_authority',imports)
captured=registry.capture(toolchain_path,package=False)
toolchain=types.ModuleType('scripts.container_acceptance_toolchain')
toolchain.__file__=str(registry.import_root/'scripts/container_acceptance_toolchain.py')
toolchain.__package__='scripts'
toolchain.__dict__['_ACTUAL_LOADED_SOURCE_REGISTRY']=registry
toolchain.__dict__['_ACTUAL_REPOSITORY_ROOT']=str(repository)
toolchain.__dict__['_ACTUAL_REPOSITORY_IMPORT_ROOT']=str(registry.import_root)
sys.modules[toolchain.__name__]=toolchain
setattr(sys.modules['scripts'],'container_acceptance_toolchain',toolchain)
sys.prefix=str(repository/'.venv')
sys.exec_prefix=sys.prefix
exec(compile(captured.encoded,str(toolchain.__file__),'exec',dont_inherit=True),toolchain.__dict__)
def dependency(path):
 identity=path.stat(follow_symlinks=False)
 digest=hashlib.sha256(str(path).encode()).hexdigest()
 return {{'root':str(path),'device':identity.st_dev,'inode':identity.st_ino,'mode':stat.S_IMODE(identity.st_mode),'uid':identity.st_uid,'gid':identity.st_gid,'mtime_ns':identity.st_mtime_ns,'ctime_ns':identity.st_ctime_ns,'entries':0,'regular_bytes':0,'sha256':digest,'projection_sha256':digest}}
def daemon():
 return {{'socket_authority_sha256':'d'*64,'daemon_identity_sha256':'e'*64}}
authority=toolchain.capture_toolchain_authority(dependency_capturer=dependency,daemon_capturer=daemon)
toolchain.activate_toolchain_authority(authority)
from scripts import container_acceptance_launcher_entry as entry
from scripts import container_acceptance_launcher as launcher
sources=launcher._loaded_sources({{'AGENT_GOV_ACCEPTANCE_LOADED_BOOTSTRAP_SHA256':bootstrap_digest,'AGENT_GOV_ACCEPTANCE_BOOTSTRAP_IMPORT_AUTHORITY_SHA256':helper_digest,'AGENT_GOV_ACCEPTANCE_BOOTSTRAP_TOOLCHAIN_SHA256':captured.sha256}})
external={{}}
for name,module in sys.modules.items():
 origin=getattr(module,'__file__',None)
 if isinstance(origin,str) and '/.venv/' in origin:
  external[name]=origin
print(json.dumps({{'sources':[[item.relative_path,item.sha256] for item in sources],'path':sys.path,'external':external,'forbidden':[name for name in ('pydantic','httpx','fastapi') if name in sys.modules]}},separators=(',',':')))
"""

    completed = subprocess.run(
        (str(repository / ".venv/bin/python"), "-I", "-P", "-S", "-X", "pycache_prefix=/dev/null", "-c", source),
        cwd=repository,
        check=False,
        capture_output=True,
        text=True,
        env={"LC_ALL": "C.UTF-8"},
        timeout=30,
    )

    assert completed.returncode == 0, completed.stderr
    evidence = json.loads(completed.stdout)
    loaded = dict(evidence["sources"])
    assert 0 < len(loaded) <= 64
    assert not any(path.startswith(".venv/") for path in loaded)
    assert not any("site-packages" in path for path in evidence["path"])
    assert evidence["external"] == {}
    assert evidence["forbidden"] == []
    assert {
        "scripts/container_acceptance_make_gate.py",
        "scripts/container_acceptance_verifier_process.py",
        "scripts/container_acceptance_snapshot_authority.py",
        "scripts/run_container_acceptance.py",
    } <= set(loaded)
