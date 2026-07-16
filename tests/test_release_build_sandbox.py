"""构建面必须限定在 release 归档内。

背景：`docker compose build` 跑在 **228 控制器**上，那里有 GitHub PAT 和能 SSH 到 232 的
部署私钥；而构建用的 compose 文件来自**被发布提交自己的归档**。于是合并一个把
`build.context` 指向控制器 HOME 的 PR，Dockerfile 一句 COPY + RUN curl 就能把 PAT 和
部署私钥带出去。

这不是「提 PR 的人能 RCE」——合并本来就等于让任意代码部署到 232。但它**静默扩大了
PAT 的爆炸半径**：设计文档只说「合并 = 发布批准」，没说「合并 = 拿到 PAT + 部署私钥」。
这道门把构建面收回归档内，让文档里那句边界成为真的。

范围：只管构建面**伸出归档之外**。归档内的 Dockerfile 想干什么不归这里管——那本就是
「合并 = 发布」授予的。
"""

from __future__ import annotations

import subprocess
import sys
from pathlib import Path

import pytest
import yaml

REPO_ROOT = Path(__file__).resolve().parents[1]
SCRIPTS_DIR = REPO_ROOT / "scripts"
sys.path.insert(0, str(SCRIPTS_DIR))

from assert_release_build_is_sandboxed import (  # noqa: E402
    BuildSandboxViolation,
    assert_build_is_sandboxed,
)

SERVICES = ["agent-gov-litellm-sidecar", "claude-agent-api", "claude-agent-ui"]


def _write_compose(root: Path, services: dict) -> Path:
    path = root / "docker" / "docker-compose.yml"
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(yaml.safe_dump({"services": services}), encoding="utf-8")
    return path


def test_the_real_shipped_compose_passes_the_gate(tmp_path: Path) -> None:
    """仓库现在这份 compose 必须过门——否则这道门一上线就把正常发布挡死。

    刻意用真实文件而非构造样本：门的第一职责是不误伤真实发布。
    """
    archive = tmp_path / "archive"
    (archive / "docker").mkdir(parents=True)
    (archive / "docker" / "docker-compose.yml").write_text(
        (REPO_ROOT / "docker" / "docker-compose.yml").read_text(encoding="utf-8"), encoding="utf-8"
    )

    assert_build_is_sandboxed(archive / "docker" / "docker-compose.yml", archive, SERVICES)


def test_context_pointing_at_the_controller_home_is_rejected(tmp_path: Path) -> None:
    """**核心用例**：把 build.context 指向控制器 HOME（PAT 与部署私钥所在）必须被拒。

    这正是计划里描述的那条路径：
        build: { context: /var/lib/agent-gov-release-controller, dockerfile: docker/Dockerfile }
    再在 Dockerfile 里 COPY .ssh/id_ed25519 + RUN curl 外带。
    """
    archive = tmp_path / "archive"
    archive.mkdir()
    compose = _write_compose(
        archive,
        {"claude-agent-api": {"build": {"context": "/var/lib/agent-gov-release-controller", "dockerfile": "docker/Dockerfile"}}},
    )

    with pytest.raises(BuildSandboxViolation, match="归档之外"):
        assert_build_is_sandboxed(compose, archive, ["claude-agent-api"])


def test_context_escaping_upward_with_dotdot_is_rejected(tmp_path: Path) -> None:
    """相对路径向上逃逸同样要拒——归档根之外就是之外，不看写法。"""
    archive = tmp_path / "archive"
    (archive / "docker").mkdir(parents=True)
    compose = _write_compose(archive, {"claude-agent-api": {"build": {"context": "../../../../etc"}}})

    with pytest.raises(BuildSandboxViolation, match="归档之外"):
        assert_build_is_sandboxed(compose, archive, ["claude-agent-api"])


def test_a_symlink_out_of_the_archive_is_rejected(tmp_path: Path) -> None:
    """context 停在归档内、但它是指向归档外的软链接：必须按解析后的真实目标判定。

    只做字符串前缀比较的实现会在这里放行——被发布的提交完全可以自带一个软链接。
    """
    outside = tmp_path / "controller-home"
    outside.mkdir()
    (outside / "github_token").write_text("ghp_pretend", encoding="utf-8")
    archive = tmp_path / "archive"
    (archive / "docker").mkdir(parents=True)
    # 软链接放在 context 真正解析到的位置（compose 同级目录），否则这条用例测的是
    # 一个不存在的路径，而不是「停在归档内、实指归档外」。
    (archive / "docker" / "sneaky").symlink_to(outside)
    compose = _write_compose(archive, {"claude-agent-api": {"build": {"context": "sneaky"}}})

    with pytest.raises(BuildSandboxViolation, match="归档之外"):
        assert_build_is_sandboxed(compose, archive, ["claude-agent-api"])


@pytest.mark.parametrize("key", ["secrets", "ssh"])
def test_host_secret_mounts_into_the_build_are_rejected(tmp_path: Path, key: str) -> None:
    """build.secrets / build.ssh 会把宿主机凭据挂进构建过程——等于绕开归档限定。"""
    archive = tmp_path / "archive"
    archive.mkdir()
    compose = _write_compose(
        archive,
        {"claude-agent-api": {"build": {"context": ".", key: ["id=pat,src=/etc/agent-gov-release-controller/github_token"]}}},
    )

    with pytest.raises(BuildSandboxViolation, match=f"build.{key}"):
        assert_build_is_sandboxed(compose, archive, ["claude-agent-api"])


def test_additional_contexts_outside_the_archive_are_rejected(tmp_path: Path) -> None:
    """additional_contexts 是同一个洞的另一个入口，不能只堵 context。"""
    archive = tmp_path / "archive"
    archive.mkdir()
    compose = _write_compose(
        archive,
        {"claude-agent-api": {"build": {"context": ".", "additional_contexts": {"creds": "/etc/agent-gov-release-controller"}}}},
    )

    with pytest.raises(BuildSandboxViolation, match="归档之外"):
        assert_build_is_sandboxed(compose, archive, ["claude-agent-api"])


@pytest.mark.parametrize(
    "remote",
    [
        "https://github.com/attacker/payload.git",
        "git@github.com:attacker/payload.git",
        "github.com/attacker/payload.git",
        "git://example.test/payload.git",
    ],
)
def test_a_remote_build_context_is_rejected(tmp_path: Path, remote: str) -> None:
    """compose 允许 git/URL 远程 context——那同样是伸出被发布的代码。

    这类值当成相对路径去 resolve 会「解析」进归档里，从而被误判为合法；必须先按来源识别。
    它虽不读控制器磁盘，但破坏不可变发布：同一个 SHA 两次构建可以拉到不同内容。
    """
    archive = tmp_path / "archive"
    archive.mkdir()
    compose = _write_compose(archive, {"claude-agent-api": {"build": {"context": remote}}})

    with pytest.raises(BuildSandboxViolation, match="远程来源"):
        assert_build_is_sandboxed(compose, archive, ["claude-agent-api"])


def test_a_remote_additional_context_is_rejected(tmp_path: Path) -> None:
    """additional_contexts 的远程来源同理（`image:` / `oci-layout:` 等）。"""
    archive = tmp_path / "archive"
    archive.mkdir()
    compose = _write_compose(
        archive,
        {"claude-agent-api": {"build": {"context": ".", "additional_contexts": {"payload": "image:evil/x:latest"}}}},
    )

    with pytest.raises(BuildSandboxViolation, match="归档之外"):
        assert_build_is_sandboxed(compose, archive, ["claude-agent-api"])


def test_a_service_without_a_build_section_is_allowed(tmp_path: Path) -> None:
    """只 image、不 build 的服务不该被这道门误伤。"""
    archive = tmp_path / "archive"
    archive.mkdir()
    compose = _write_compose(archive, {"claude-agent-api": {"image": "agent-gov-api:1.0.0"}})

    assert_build_is_sandboxed(compose, archive, ["claude-agent-api"])


def test_a_missing_service_is_rejected_rather_than_silently_skipped(tmp_path: Path) -> None:
    """要构建的 service 不存在时必须报错。

    静默跳过会让这道门在 compose 重命名服务后变成一张白纸——检查了个寂寞。
    """
    archive = tmp_path / "archive"
    archive.mkdir()
    compose = _write_compose(archive, {"other": {"image": "x"}})

    with pytest.raises(BuildSandboxViolation, match="缺少要构建的 service"):
        assert_build_is_sandboxed(compose, archive, ["claude-agent-api"])


def test_the_cli_exits_nonzero_and_explains_itself(tmp_path: Path) -> None:
    """真实执行 CLI：部署脚本靠退出码拦截，不能只有库函数会抛。"""
    archive = tmp_path / "archive"
    archive.mkdir()
    compose = _write_compose(
        archive, {"claude-agent-api": {"build": {"context": "/var/lib/agent-gov-release-controller"}}}
    )

    completed = subprocess.run(
        [
            sys.executable,
            str(SCRIPTS_DIR / "assert_release_build_is_sandboxed.py"),
            "--compose",
            str(compose),
            "--archive-root",
            str(archive),
            "--service",
            "claude-agent-api",
        ],
        check=False,
        capture_output=True,
        text=True,
    )

    assert completed.returncode == 1
    assert "拒绝构建" in completed.stderr
    assert "归档之外" in completed.stderr
