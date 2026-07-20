"""agent_gov_release_remote 的退出码契约测试——真正执行 bash，不是 grep 源码。

存在理由：人工部署入口需要稳定区分
0=健康、2=部署失败但已自动恢复、3=部署与恢复均失败。仅做 `bash -n`
或源码 grep 无法证明真实执行分支会返回这些退出码。P0（health_check 因尾随
`|| true` 恒返回 0）与 readlink -f 误判（无上一版时 exit 1 而非 3）
都是从这个验证缺口溜进来的。

因此这里的测试必须真的 spawn 脚本、真的走完 deploy_release 的分支。
外部依赖用 PATH 注入的替身隔离：
  * docker  —— 全部替身，行为由 FAKE_COMPOSE_UP_RC 控制
  * python3 —— **只**拦截冒烟检查（靠其独有的 agent-gov-release-smoke 标记识别），
               其余（manifest_update / status_json / 路径规范化）一律转发给真解释器，
               以免把被测逻辑一起 stub 掉。
"""

from __future__ import annotations

import gzip
import json
import os
import shutil
import subprocess
import sys
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parents[1]
REMOTE_HELPER = REPO_ROOT / "scripts" / "agent_gov_release_remote"

# 冒烟检查脚本独有的标记（health_check 的 User-Agent），用于在替身里精准识别它。
SMOKE_MARKER = "agent-gov-release-smoke"

_DOCKER_STUB = """#!/usr/bin/env bash
# docker 替身：记录调用，compose up 的结果由 FAKE_COMPOSE_UP_RC 控制，其余一律成功。
printf '%s\\n' "$*" >> "$FAKE_DOCKER_LOG"
if [[ "${1:-}" == "compose" ]]; then
  for arg in "$@"; do
    if [[ "$arg" == "config" ]]; then
      printf '%s\\n' ${FAKE_COMPOSE_IMAGES:-}
      exit 0
    fi
    if [[ "$arg" == "up" ]]; then
      exit "${FAKE_COMPOSE_UP_RC:-0}"
    fi
  done
  exit 0
fi
if [[ "${1:-}" == "image" && "${2:-}" == "inspect" ]]; then
  for image in ${FAKE_MISSING_IMAGES:-}; do
    if [[ "${3:-}" == "$image" ]]; then
      exit 1
    fi
  done
fi
if [[ "${1:-}" == "load" ]]; then
  cat > /dev/null
  exit 0
fi
# docker ps -aq --filter ... —— 输出空表示无残留容器
exit 0
"""

_PYTHON_STUB = """#!/usr/bin/env bash
# python3 替身：只拦截 release 冒烟检查，其余全部转发给真解释器。
# 冒烟按【调用次序】决定成败：FAKE_SMOKE_FAIL_SEQ 里列出的第 N 次调用返回 1。
# 这样才能表达"新版本不健康、回滚目标健康"——deploy_release 会对两者各冒烟一次。
REAL_PYTHON="$FAKE_REAL_PYTHON"
if [[ "${1:-}" == "-" ]]; then
  script="$(cat)"
  if [[ "$script" == *"__SMOKE_MARKER__"* ]]; then
    count=$(( $(cat "$FAKE_SMOKE_COUNTER" 2>/dev/null || echo 0) + 1 ))
    printf '%s' "$count" > "$FAKE_SMOKE_COUNTER"
    for n in ${FAKE_SMOKE_FAIL_SEQ:-}; do
      if [[ "$n" == "$count" ]]; then
        printf '%s\\n' "smoke#${count} -> FAIL" >> "$FAKE_DOCKER_LOG"
        exit 1
      fi
    done
    printf '%s\\n' "smoke#${count} -> ok" >> "$FAKE_DOCKER_LOG"
    exit 0
  fi
  shift
  printf '%s' "$script" | "$REAL_PYTHON" - "$@"
  exit $?
fi
exec "$REAL_PYTHON" "$@"
"""


def _write_exec(path: Path, body: str) -> None:
    path.write_text(body, encoding="utf-8")
    path.chmod(0o755)


def _make_release(root: Path, release_id: str) -> Path:
    """造一个 deploy_release 能走完的最小 release 目录。"""
    directory = root / "releases" / release_id
    (directory / "images").mkdir(parents=True)
    (directory / "docker").mkdir(parents=True)
    (directory / "scripts").mkdir(parents=True)

    # load_release_images 会跑真 `gzip -dc <archive> | docker load`（docker 才是替身），
    # 所以归档必须是真正的 gzip 流；内容无所谓。
    archive = directory / "images" / "app.tar.gz"
    archive.write_bytes(gzip.compress(b"fake-image-archive"))
    checksum = subprocess.run(
        ["sha256sum", archive.name],
        cwd=archive.parent,
        capture_output=True,
        text=True,
        check=True,
    ).stdout
    (directory / "images" / "app.tar.gz.sha256").write_text(checksum, encoding="utf-8")

    (directory / "docker" / "docker-compose.yml").write_text("services: {}\n", encoding="utf-8")
    # compose_up 从这里读镜像 tag
    (directory / ".app-version").write_text("3.0.0-exitcontract\n", encoding="utf-8")
    # health_check 末尾的诊断步骤会调它；它带 || true，但文件必须存在
    _write_exec(
        directory / "scripts" / "diagnose_runtime_health.py",
        "#!/usr/bin/env python3\nraise SystemExit(0)\n",
    )
    # diagnose_release 会从 release 目录调它（真实脚本硬编码 docker/.env，见 #70）
    _write_exec(
        directory / "scripts" / "compose_diagnose.sh",
        "#!/usr/bin/env bash\nexit 0\n",
    )
    (directory / "release.json").write_text(
        json.dumps(
            {
                "release_id": release_id,
                "commit_sha": "0" * 40,
                "environment": "staging-232",
                "status": "prepared",
                "image_digests": {},
            }
        ),
        encoding="utf-8",
    )
    return directory


def _make_legacy_deployment(root: Path) -> None:
    (root / "docker").mkdir(parents=True, exist_ok=True)
    (root / "docker" / "docker-compose.yml").write_text("services: {}\n", encoding="utf-8")
    (root / "VERSION").write_text("2.8.8\n", encoding="utf-8")


@pytest.fixture()
def release_root(tmp_path: Path) -> Path:
    root = tmp_path / "release-root"
    (root / "shared").mkdir(parents=True)
    # ensure_shared_env 要求私有 env 已就位。两处关键隔离：
    #   * HOST_RUNTIME_VOLUME_ROOT 指向 tmp——否则 prepare_runtime_root 会去动真实的
    #     ${HOME}/volume-agent-gov（本机上它是 root 属主，install 会失败并污染测试信号）。
    #   * LANGFUSE_* UID/GID 用当前用户——install -o/-g 在非 root 下才不会失败。
    #     生产上该脚本以 root 跑，这些默认值（999/101/65532）是对的。
    (root / "shared" / "docker.env").write_text(
        "\n".join(
            [
                # 端口取容器环境约定（50000 + 容器端口）。具体值对本用例无意义——假 docker
                # 不监听任何端口——但**不得取本机私有调试端口族**（4 开头的五位）：
                # 仓库策略禁止把它提交进受版本控制的文件，见 test_repository_env_policy。
                "HOST_PORT=58080",
                "FRONTEND_HOST_PORT=55173",
                "LANGFUSE_HOST_PORT=53000",
                "CONTAINER_NAME_PREFIX=agent-gov-exitcontract",
                f"HOST_RUNTIME_VOLUME_ROOT={tmp_path / 'runtime'}",
                *(
                    f"LANGFUSE_{service}_{kind}={value}"
                    for service in ("POSTGRES", "CLICKHOUSE", "REDIS", "MINIO")
                    for kind, value in (("UID", os.getuid()), ("GID", os.getgid()))
                ),
                "",
            ]
        ),
        encoding="utf-8",
    )
    return root


def _run_deploy(
    release_root: Path,
    release_id: str,
    tmp_path: Path,
    *,
    smoke_fail_seq: str = "",
    compose_up_rc: int = 0,
    compose_images: tuple[str, ...] = (),
    missing_images: tuple[str, ...] = (),
) -> subprocess.CompletedProcess[str]:
    bin_dir = tmp_path / "fakebin"
    bin_dir.mkdir(exist_ok=True)
    docker_log = tmp_path / "docker.log"
    docker_log.touch()

    _write_exec(bin_dir / "docker", _DOCKER_STUB)
    _write_exec(bin_dir / "python3", _PYTHON_STUB.replace("__SMOKE_MARKER__", SMOKE_MARKER))

    real_python = shutil.which("python3") or sys.executable
    env = {
        **os.environ,
        "PATH": f"{bin_dir}:{os.environ['PATH']}",
        "FAKE_REAL_PYTHON": real_python,
        "FAKE_DOCKER_LOG": str(docker_log),
        "FAKE_SMOKE_COUNTER": str(tmp_path / "smoke.count"),
        "FAKE_SMOKE_FAIL_SEQ": smoke_fail_seq,
        "FAKE_COMPOSE_UP_RC": str(compose_up_rc),
        "FAKE_COMPOSE_IMAGES": " ".join(compose_images),
        "FAKE_MISSING_IMAGES": " ".join(missing_images),
    }
    return subprocess.run(
        [
            "bash",
            str(REMOTE_HELPER),
            "deploy",
            "--release-root",
            str(release_root),
            "--release-id",
            release_id,
        ],
        capture_output=True,
        text=True,
        env=env,
    )


def test_healthy_deploy_exits_zero_and_points_current_at_release(release_root: Path, tmp_path: Path) -> None:
    directory = _make_release(release_root, "staging-232-aaaaaaaaaaaa")

    result = _run_deploy(release_root, "staging-232-aaaaaaaaaaaa", tmp_path)

    assert result.returncode == 0, f"stdout={result.stdout}\nstderr={result.stderr}"
    assert (release_root / "current").resolve() == directory.resolve()
    assert json.loads((directory / "release.json").read_text())["status"] == "succeeded"


def test_smoke_failure_rolls_back_to_previous_and_exits_two(release_root: Path, tmp_path: Path) -> None:
    """体检失败必须回滚并返回 2。

    这条在修复前会失败（拿到 0 并把坏版本标成 succeeded）——
    因为 health_check 末尾的 `|| true` 让函数恒返回 0，冒烟的 SystemExit 被吞掉。
    """
    previous = _make_release(release_root, "staging-232-previous0000")
    (release_root / "current").symlink_to(previous)
    broken = _make_release(release_root, "staging-232-bbbbbbbbbbbb")

    # 第 1 次冒烟 = 新版本（不健康）；第 2 次 = 回滚目标（健康）
    result = _run_deploy(release_root, "staging-232-bbbbbbbbbbbb", tmp_path, smoke_fail_seq="1")

    assert result.returncode == 2, (
        f"体检失败必须返回 2（已回滚）。拿到 0 说明 health_check 又把冒烟失败吞掉了。\nstdout={result.stdout}\nstderr={result.stderr}"
    )
    # 坏版本不得成为 current
    assert (release_root / "current").resolve() == previous.resolve()
    manifest = json.loads((broken / "release.json").read_text())
    assert manifest["status"] == "rolled_back"


def test_first_deploy_failure_without_previous_exits_three(release_root: Path, tmp_path: Path) -> None:
    """全新目标机（无 current）首次部署失败必须返回 3，而不是含义不明的 1。

    这条在修复前会失败（拿到 1）——因为 `readlink -f` 对尚不存在的 current
    仍返回非空路径，把"首次部署"误判为"有上一版可回滚"，
    进而 load_release_images 找不到归档而 die。
    exit 1 也无法让人工部署入口准确区分是否已完成自动恢复。
    """
    _make_release(release_root, "staging-232-cccccccccccc")
    assert not (release_root / "current").exists()

    result = _run_deploy(release_root, "staging-232-cccccccccccc", tmp_path, compose_up_rc=1)

    assert result.returncode == 3, f"无上一版时部署失败必须返回 3（FAILED），不能退化为含义不明的 1。\nstdout={result.stdout}\nstderr={result.stderr}"
    assert not (release_root / "current").exists()


def test_missing_legacy_rollback_image_warns_and_continues_deployment(
    release_root: Path,
    tmp_path: Path,
) -> None:
    _make_legacy_deployment(release_root)
    directory = _make_release(release_root, "staging-232-dddddddddddd")
    legacy_image = "agent-gov-api:2.8.8"

    result = _run_deploy(
        release_root,
        "staging-232-dddddddddddd",
        tmp_path,
        compose_images=(legacy_image,),
        missing_images=(legacy_image,),
    )

    assert result.returncode == 0, f"stdout={result.stdout}\nstderr={result.stderr}"
    assert "WARN: Legacy rollback snapshot is unavailable" in result.stderr
    assert legacy_image in result.stderr
    assert "continuing without a rollback target" in result.stderr
    assert (release_root / "current").resolve() == directory.resolve()
    assert not (release_root / "releases" / "legacy-bootstrap").exists()
    assert not list((release_root / "releases").glob(".legacy-bootstrap.*"))


def test_missing_legacy_rollback_does_not_hide_new_release_failure(
    release_root: Path,
    tmp_path: Path,
) -> None:
    _make_legacy_deployment(release_root)
    directory = _make_release(release_root, "staging-232-eeeeeeeeeeee")
    legacy_image = "agent-gov-api:2.8.8"

    result = _run_deploy(
        release_root,
        "staging-232-eeeeeeeeeeee",
        tmp_path,
        compose_up_rc=1,
        compose_images=(legacy_image,),
        missing_images=(legacy_image,),
    )

    assert result.returncode == 3, f"stdout={result.stdout}\nstderr={result.stderr}"
    assert "WARN: Legacy rollback snapshot is unavailable" in result.stderr
    assert not (release_root / "current").exists()
    assert json.loads((directory / "release.json").read_text())["status"] == "rollback_failed"
