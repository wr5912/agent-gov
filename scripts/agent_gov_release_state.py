from __future__ import annotations

import contextlib
import fcntl
import json
import os
import re
import sqlite3
import urllib.error
import urllib.request
from collections.abc import Iterator, Mapping, Sequence
from dataclasses import dataclass
from datetime import datetime, timezone
from enum import StrEnum
from pathlib import Path
from typing import Any, TypedDict, cast


class ControllerError(RuntimeError):
    """A fail-closed release controller error."""


class TransportError(ControllerError):
    """GitHub 传输层故障（DNS/超时/连接拒绝/5xx/限流）。

    与业务违规（血缘非法、AID 不唯一、合并者未授权）**必须分流**：业务违规是关于这个
    提交本身的终局判定，永久隔离是对的；传输故障只说明"这一次没问到"，隔离它会让
    一次网络抖动永久废掉一个完全合法的发布——而 poll 每 30 秒一轮、等 CI 窗口默认
    2 小时，期间约 240 轮 × N 次调用，抖一次的概率并不低。
    """


class ReleaseStatus(StrEnum):
    DISCOVERED = "discovered"
    WAITING_CI = "waiting_ci"
    DEPLOYING = "deploying"
    SUCCEEDED = "succeeded"
    ROLLED_BACK = "rolled_back"
    FAILED = "failed"
    SUPERSEDED = "superseded"
    QUARANTINED = "quarantined"


ALLOWED_TRANSITIONS: dict[ReleaseStatus, frozenset[ReleaseStatus]] = {
    ReleaseStatus.DISCOVERED: frozenset(
        {
            ReleaseStatus.WAITING_CI,
            ReleaseStatus.SUPERSEDED,
            ReleaseStatus.QUARANTINED,
        }
    ),
    ReleaseStatus.WAITING_CI: frozenset(
        {
            ReleaseStatus.DEPLOYING,
            ReleaseStatus.SUPERSEDED,
            ReleaseStatus.QUARANTINED,
        }
    ),
    ReleaseStatus.DEPLOYING: frozenset(
        {
            ReleaseStatus.WAITING_CI,
            ReleaseStatus.SUCCEEDED,
            ReleaseStatus.ROLLED_BACK,
            ReleaseStatus.FAILED,
        }
    ),
    # 人工回滚：运维用 `releasectl rollback --approved-by` 把一个已成功的发布换下线。
    # 此前该动作绕过状态机、只裸写 active metadata，于是 30 秒后 poll 看到"head 仍是
    # SUCCEEDED"就把 active 覆写回去——机器上跑 A、治理面说 B，且永不自愈。
    # 把它建模成一条边，rollback() 才能走 finalize_release 的原子写。
    ReleaseStatus.SUCCEEDED: frozenset({ReleaseStatus.ROLLED_BACK}),
    ReleaseStatus.ROLLED_BACK: frozenset(),
    ReleaseStatus.FAILED: frozenset(),
    ReleaseStatus.SUPERSEDED: frozenset(),
    # 隔离不是终态，而是"等待人工裁决"：只有 `releasectl unquarantine --approved-by`
    # 会走这条边，自动路径永远不会。把人工出口**建模进状态机**而不是绕过它，
    # 否则运维唯一的解封手段就是手改 sqlite。
    ReleaseStatus.QUARANTINED: frozenset({ReleaseStatus.WAITING_CI}),
}


class ReleaseRecord(TypedDict):
    commit_sha: str
    pr_number: int | None
    aid_identifiers: str | None
    status: str
    reason: str | None
    workflow_url: str | None
    workflow_run_id: int | None
    release_id: str | None
    discovered_at: str
    updated_at: str


class ReleaseEvent(TypedDict):
    id: int
    commit_sha: str | None
    event_type: str
    details: str | None
    created_at: str


class OutboxRecord(TypedDict):
    id: int
    dedupe_key: str
    kind: str
    payload: str
    status: str
    attempts: int
    last_error: str | None
    created_at: str
    updated_at: str


class ReleaseSnapshot(TypedDict):
    metadata: dict[str, str]
    releases: list[ReleaseRecord]
    events: list[ReleaseEvent]
    outbox: list[OutboxRecord]


def utc_now() -> str:
    return datetime.now(timezone.utc).isoformat()


@dataclass(frozen=True)
class ControllerConfig:
    repository: str
    branch: str
    environment: str
    deploy_host: str
    deploy_user: str
    remote_dir: str
    state_dir: Path
    deploy_script: Path
    github_api_url: str
    multica_profile: str
    quality_check: str
    workflow_file: str
    allowed_mergers: tuple[str, ...]
    release_sre_agent: str
    release_sre_metadata_key: str
    require_branch_protection: bool
    ci_timeout_seconds: int

    @classmethod
    def from_environment(cls) -> ControllerConfig:
        scripts_dir = Path(__file__).resolve().parent
        state_dir = Path(
            os.environ.get("AGENT_GOV_STATE_DIR", "~/.local/state/agent-gov-release-controller")
        ).expanduser()
        allowed_mergers = tuple(
            item.strip()
            for item in os.environ.get("AGENT_GOV_ALLOWED_MERGERS", "wr5912").split(",")
            if item.strip()
        )
        config = cls(
            repository=os.environ.get("AGENT_GOV_REPOSITORY", "wr5912/agent-gov"),
            branch=os.environ.get("AGENT_GOV_BRANCH", "master"),
            environment=os.environ.get("AGENT_GOV_ENVIRONMENT", "staging-232"),
            deploy_host=os.environ.get("AGENT_GOV_DEPLOY_HOST", "172.16.112.232"),
            deploy_user=os.environ.get("AGENT_GOV_DEPLOY_USER", "root"),
            remote_dir=os.environ.get("AGENT_GOV_REMOTE_DIR", "~/work/agent-gov"),
            state_dir=state_dir,
            deploy_script=Path(
                os.environ.get(
                    "AGENT_GOV_DEPLOY_SCRIPT",
                    str(scripts_dir / "deploy_agent_gov_to_host"),
                )
            ).expanduser(),
            github_api_url=os.environ.get("GITHUB_API_URL", "https://api.github.com").rstrip("/"),
            multica_profile=os.environ.get("MULTICA_PROFILE", "release-controller"),
            quality_check=os.environ.get("AGENT_GOV_QUALITY_CHECK", "quality-gate"),
            workflow_file=os.environ.get(
                "AGENT_GOV_WORKFLOW_FILE", ".github/workflows/governance.yml"
            ),
            allowed_mergers=allowed_mergers,
            release_sre_agent=os.environ.get("AGENT_GOV_RELEASE_SRE_AGENT", "release-sre"),
            release_sre_metadata_key=os.environ.get(
                "AGENT_GOV_RELEASE_SRE_METADATA_KEY", "release_sre_issue_id"
            ),
            require_branch_protection=os.environ.get(
                "AGENT_GOV_REQUIRE_BRANCH_PROTECTION", "true"
            ).lower()
            not in {"0", "false", "no"},
            ci_timeout_seconds=int(os.environ.get("AGENT_GOV_CI_TIMEOUT_SECONDS", "7200")),
        )
        config.validate()
        return config

    def validate(self) -> None:
        if not re.fullmatch(r"[A-Za-z0-9_.-]+/[A-Za-z0-9_.-]+", self.repository):
            raise ControllerError(f"invalid repository: {self.repository}")
        for label, value in (
            ("branch", self.branch),
            ("environment", self.environment),
            ("quality check", self.quality_check),
            ("release SRE agent", self.release_sre_agent),
            ("release SRE metadata key", self.release_sre_metadata_key),
        ):
            if not re.fullmatch(r"[A-Za-z0-9_.-]+", value):
                raise ControllerError(f"invalid {label}: {value}")
        if not re.fullmatch(r"[A-Za-z0-9_./-]+\.ya?ml", self.workflow_file):
            raise ControllerError(f"invalid workflow file: {self.workflow_file}")
        if not self.allowed_mergers or any(
            not re.fullmatch(r"[A-Za-z0-9-]+", login) for login in self.allowed_mergers
        ):
            raise ControllerError("AGENT_GOV_ALLOWED_MERGERS must contain valid GitHub logins")
        if self.ci_timeout_seconds < 60:
            raise ControllerError("AGENT_GOV_CI_TIMEOUT_SECONDS must be at least 60")
        if not self.deploy_script.is_file():
            raise ControllerError(f"deploy script does not exist: {self.deploy_script}")

    @property
    def owner_repo(self) -> tuple[str, str]:
        owner, repository = self.repository.split("/", 1)
        return owner, repository


def load_github_token() -> str:
    credentials_directory = os.environ.get("CREDENTIALS_DIRECTORY")
    if credentials_directory:
        credential_path = Path(credentials_directory) / "github_token"
        if credential_path.is_file():
            token = credential_path.read_text(encoding="utf-8").strip()
            if token:
                return token
    for name in ("GITHUB_TOKEN", "GH_TOKEN"):
        token = os.environ.get(name, "").strip()
        if token:
            return token
    raise ControllerError(
        "GitHub credential is unavailable; use systemd LoadCredential=github_token"
    )


def sanitized_environment(config: ControllerConfig) -> Mapping[str, str]:
    """派生子进程环境：剥掉 GitHub 凭据，注入部署目标。

    与 load_github_token 是同一条边界的两端（凭据进 / 凭据出），故与 ControllerConfig
    同处；控制器与对账器都要 fork 部署脚本，共用这一份剥离逻辑，避免哪天漏掉一处。
    """
    environment = os.environ.copy()
    for credential_name in ("GITHUB_TOKEN", "GH_TOKEN", "CREDENTIALS_DIRECTORY"):
        environment.pop(credential_name, None)
    environment.update({"DEPLOY_USER": config.deploy_user, "REMOTE_DIR": config.remote_dir})
    return environment


class GitHubClient:
    def __init__(self, *, api_url: str, token: str) -> None:
        self._api_url = api_url
        self._token = token

    def get(self, path: str) -> Any:
        request = urllib.request.Request(
            f"{self._api_url}{path}",
            headers={
                "Accept": "application/vnd.github+json",
                "Authorization": f"Bearer {self._token}",
                "User-Agent": "agent-gov-release-controller",
                "X-GitHub-Api-Version": "2022-11-28",
            },
        )
        try:
            with urllib.request.urlopen(request, timeout=30) as response:
                body = response.read()
        except urllib.error.HTTPError as exc:
            body = exc.read(2000).decode("utf-8", "replace")
            # 5xx 与 429 是"这一次没问到"，不是关于该提交的判定；401/403/404 等
            # 客户端错误说明凭据或路径本身有问题，保持原有的 fail-closed 语义。
            error = TransportError if exc.code >= 500 or exc.code == 429 else ControllerError
            raise error(
                f"GitHub API GET {path} failed with HTTP {exc.code}: {body}"
            ) from exc
        except urllib.error.URLError as exc:
            raise TransportError(f"GitHub API GET {path} failed: {exc.reason}") from exc
        except TimeoutError as exc:
            raise TransportError(f"GitHub API GET {path} timed out") from exc
        return json.loads(body) if body else None


class StateStore:
    def __init__(self, path: Path) -> None:
        path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
        self._connection = sqlite3.connect(path)
        self._connection.row_factory = sqlite3.Row
        self._initialize()

    def close(self) -> None:
        self._connection.close()

    def _initialize(self) -> None:
        with self._connection:
            self._connection.executescript(
                """
                PRAGMA journal_mode=WAL;
                PRAGMA foreign_keys=ON;
                CREATE TABLE IF NOT EXISTS metadata (
                    key TEXT PRIMARY KEY,
                    value TEXT NOT NULL
                );
                CREATE TABLE IF NOT EXISTS releases (
                    commit_sha TEXT PRIMARY KEY,
                    pr_number INTEGER,
                    aid_identifiers TEXT,
                    status TEXT NOT NULL,
                    reason TEXT,
                    workflow_url TEXT,
                    workflow_run_id INTEGER,
                    release_id TEXT,
                    discovered_at TEXT NOT NULL,
                    updated_at TEXT NOT NULL
                );
                -- 血缘快照：某个提交的 PR 归属一旦校验通过就在此固化，之后只读快照。
                -- GitHub 允许 PR 合并后继续编辑标题/正文，若每次回放都查 live PR，
                -- 任何人改一下自己已合并 PR 的正文即可让该提交永久隔离，进而让之后
                -- 每一个新 head 的血缘校验都失败——整条发布链死锁且无解封命令。
                -- 固化后，发布依据的是"门禁通过那一刻的事实"，与 PR 之后如何编辑无关。
                CREATE TABLE IF NOT EXISTS commit_links (
                    commit_sha TEXT PRIMARY KEY,
                    pr_number INTEGER NOT NULL,
                    aid_identifier TEXT NOT NULL,
                    merged_by TEXT NOT NULL,
                    resolved_at TEXT NOT NULL
                );
                CREATE TABLE IF NOT EXISTS events (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    commit_sha TEXT,
                    event_type TEXT NOT NULL,
                    details TEXT,
                    created_at TEXT NOT NULL
                );
                CREATE TABLE IF NOT EXISTS outbox (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    dedupe_key TEXT NOT NULL UNIQUE,
                    kind TEXT NOT NULL,
                    payload TEXT NOT NULL,
                    status TEXT NOT NULL DEFAULT 'pending',
                    attempts INTEGER NOT NULL DEFAULT 0,
                    last_error TEXT,
                    created_at TEXT NOT NULL,
                    updated_at TEXT NOT NULL
                );
                """
            )

    def get_metadata(self, key: str) -> str | None:
        row = self._connection.execute(
            "SELECT value FROM metadata WHERE key = ?", (key,)
        ).fetchone()
        return str(row["value"]) if row else None

    def set_metadata(self, key: str, value: str) -> None:
        with self._connection:
            self._connection.execute(
                "INSERT INTO metadata(key, value) VALUES(?, ?) "
                "ON CONFLICT(key) DO UPDATE SET value = excluded.value",
                (key, value),
            )

    def discover(self, commit_sha: str) -> None:
        now = utc_now()
        with self._connection:
            self._connection.execute(
                "INSERT OR IGNORE INTO releases(commit_sha, status, discovered_at, updated_at) "
                "VALUES(?, ?, ?, ?)",
                (commit_sha, ReleaseStatus.DISCOVERED, now, now),
            )

    def get_release(self, commit_sha: str) -> sqlite3.Row | None:
        return self._connection.execute(
            "SELECT * FROM releases WHERE commit_sha = ?", (commit_sha,)
        ).fetchone()

    def get_release_by_id(self, release_id: str) -> sqlite3.Row | None:
        return self._connection.execute(
            "SELECT * FROM releases WHERE release_id = ?", (release_id,)
        ).fetchone()

    def next_pending(self) -> sqlite3.Row | None:
        return self._connection.execute(
            "SELECT * FROM releases WHERE status IN (?, ?) "
            "ORDER BY discovered_at DESC, rowid DESC LIMIT 1",
            (ReleaseStatus.DISCOVERED, ReleaseStatus.WAITING_CI),
        ).fetchone()

    def set_linkage(
        self,
        commit_sha: str,
        *,
        pr_number: int,
        aid_identifiers: list[str],
        release_id: str,
    ) -> None:
        with self._connection:
            self._connection.execute(
                "UPDATE releases SET pr_number = ?, aid_identifiers = ?, release_id = ?, "
                "updated_at = ? WHERE commit_sha = ?",
                (
                    pr_number,
                    json.dumps(aid_identifiers, separators=(",", ":")),
                    release_id,
                    utc_now(),
                    commit_sha,
                ),
            )

    def get_commit_link(self, commit_sha: str) -> sqlite3.Row | None:
        """读取某提交已固化的 PR 归属快照；没有则返回 None。"""
        cursor = self._connection.execute(
            "SELECT * FROM commit_links WHERE commit_sha = ?", (commit_sha,)
        )
        return cursor.fetchone()

    def put_commit_link(
        self,
        commit_sha: str,
        *,
        pr_number: int,
        aid_identifier: str,
        merged_by: str,
    ) -> None:
        """固化某提交的 PR 归属。

        只在校验**通过**时调用：失败（含传输故障）绝不固化，否则一次网络抖动
        就会被永久记成"这个提交非法"。INSERT OR IGNORE 保证首次写入即定论——
        后续回放读到的永远是门禁通过那一刻的事实。
        """
        with self._connection:
            self._connection.execute(
                "INSERT OR IGNORE INTO commit_links"
                "(commit_sha, pr_number, aid_identifier, merged_by, resolved_at) "
                "VALUES (?, ?, ?, ?, ?)",
                (commit_sha, pr_number, aid_identifier, merged_by, utc_now()),
            )

    def set_workflow(self, commit_sha: str, run_id: int, workflow_url: str) -> None:
        with self._connection:
            self._connection.execute(
                "UPDATE releases SET workflow_run_id = ?, workflow_url = ?, updated_at = ? "
                "WHERE commit_sha = ?",
                (run_id, workflow_url, utc_now(), commit_sha),
            )

    def transition(
        self,
        commit_sha: str,
        target: ReleaseStatus,
        *,
        reason: str | None = None,
    ) -> None:
        row = self.get_release(commit_sha)
        if row is None:
            raise ControllerError(f"unknown release commit: {commit_sha}")
        source = ReleaseStatus(row["status"])
        if source == target:
            with self._connection:
                self._connection.execute(
                    "UPDATE releases SET reason = ?, updated_at = ? WHERE commit_sha = ?",
                    (reason, utc_now(), commit_sha),
                )
            return
        if target not in ALLOWED_TRANSITIONS[source]:
            raise ControllerError(f"illegal release transition: {source} -> {target}")
        with self._connection:
            self._connection.execute(
                "UPDATE releases SET status = ?, reason = ?, updated_at = ? WHERE commit_sha = ?",
                (target, reason, utc_now(), commit_sha),
            )

    def finalize_release(
        self,
        commit_sha: str,
        target: ReleaseStatus,
        *,
        reason: str,
        metadata: dict[str, str],
        outbox: Sequence[tuple[str, str, Mapping[str, object]]],
    ) -> None:
        """Atomically persist a terminal release, cursors and notifications."""
        now = utc_now()
        with self._connection:
            row = self._connection.execute(
                "SELECT status FROM releases WHERE commit_sha = ?", (commit_sha,)
            ).fetchone()
            if row is None:
                raise ControllerError(f"unknown release commit: {commit_sha}")
            source = ReleaseStatus(row["status"])
            if target not in ALLOWED_TRANSITIONS[source]:
                raise ControllerError(f"illegal release transition: {source} -> {target}")
            self._connection.execute(
                "UPDATE releases SET status = ?, reason = ?, updated_at = ? "
                "WHERE commit_sha = ?",
                (target, reason, now, commit_sha),
            )
            for key, value in metadata.items():
                self._connection.execute(
                    "INSERT INTO metadata(key, value) VALUES(?, ?) "
                    "ON CONFLICT(key) DO UPDATE SET value = excluded.value",
                    (key, value),
                )
            for dedupe_key, kind, payload in outbox:
                self._connection.execute(
                    "INSERT OR IGNORE INTO outbox"
                    "(dedupe_key, kind, payload, created_at, updated_at) "
                    "VALUES(?, ?, ?, ?, ?)",
                    (
                        dedupe_key,
                        kind,
                        json.dumps(payload, ensure_ascii=False),
                        now,
                        now,
                    ),
                )

    def supersede_pending(self, except_sha: str) -> None:
        rows = self._connection.execute(
            "SELECT commit_sha FROM releases WHERE commit_sha != ? AND status IN (?, ?)",
            (except_sha, ReleaseStatus.DISCOVERED, ReleaseStatus.WAITING_CI),
        ).fetchall()
        for row in rows:
            self.transition(
                str(row["commit_sha"]),
                ReleaseStatus.SUPERSEDED,
                reason=f"superseded by newer master head {except_sha}",
            )

    def recover_incomplete(self) -> None:
        rows = self._connection.execute(
            "SELECT commit_sha FROM releases WHERE status = ?", (ReleaseStatus.DEPLOYING,)
        ).fetchall()
        for row in rows:
            self.transition(
                str(row["commit_sha"]),
                ReleaseStatus.WAITING_CI,
                reason="controller restarted during deployment; idempotent reconciliation queued",
            )

    def enqueue_outbox(
        self,
        dedupe_key: str,
        kind: str,
        payload: Mapping[str, object],
    ) -> None:
        now = utc_now()
        with self._connection:
            self._connection.execute(
                "INSERT OR IGNORE INTO outbox(dedupe_key, kind, payload, created_at, updated_at) "
                "VALUES(?, ?, ?, ?, ?)",
                (dedupe_key, kind, json.dumps(payload, ensure_ascii=False), now, now),
            )

    def pending_outbox(self, limit: int = 20) -> list[sqlite3.Row]:
        return self._connection.execute(
            "SELECT * FROM outbox WHERE status = 'pending' ORDER BY id LIMIT ?", (limit,)
        ).fetchall()

    def mark_outbox_delivered(self, row_id: int) -> None:
        with self._connection:
            self._connection.execute(
                "UPDATE outbox SET status = 'delivered', attempts = attempts + 1, "
                "last_error = NULL, updated_at = ? WHERE id = ?",
                (utc_now(), row_id),
            )

    def mark_outbox_failed(self, row_id: int, error: str) -> None:
        with self._connection:
            self._connection.execute(
                "UPDATE outbox SET attempts = attempts + 1, last_error = ?, updated_at = ? "
                "WHERE id = ?",
                (error[:2000], utc_now(), row_id),
            )

    def outbox_delivered(self, dedupe_key: str) -> bool:
        row = self._connection.execute(
            "SELECT status FROM outbox WHERE dedupe_key = ?", (dedupe_key,)
        ).fetchone()
        return bool(row and row["status"] == "delivered")

    def add_event(self, event_type: str, details: str, commit_sha: str | None = None) -> None:
        with self._connection:
            self._connection.execute(
                "INSERT INTO events(commit_sha, event_type, details, created_at) VALUES(?, ?, ?, ?)",
                (commit_sha, event_type, details, utc_now()),
            )

    def snapshot(self) -> ReleaseSnapshot:
        releases = [
            cast(ReleaseRecord, dict(row))
            for row in self._connection.execute(
                "SELECT * FROM releases ORDER BY discovered_at DESC, rowid DESC"
            )
        ]
        events = [
            cast(ReleaseEvent, dict(row))
            for row in self._connection.execute("SELECT * FROM events ORDER BY id DESC LIMIT 50")
        ]
        outbox = [
            cast(OutboxRecord, dict(row))
            for row in self._connection.execute("SELECT * FROM outbox ORDER BY id DESC LIMIT 50")
        ]
        metadata = {
            str(row["key"]): str(row["value"])
            for row in self._connection.execute("SELECT key, value FROM metadata")
        }
        return {"metadata": metadata, "releases": releases, "events": events, "outbox": outbox}


@contextlib.contextmanager
def controller_lock(state_dir: Path) -> Iterator[None]:
    state_dir.mkdir(parents=True, exist_ok=True, mode=0o700)
    lock_path = state_dir / "controller.lock"
    with lock_path.open("a+", encoding="utf-8") as handle:
        try:
            fcntl.flock(handle.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError as exc:
            raise ControllerError("another controller invocation holds the release lock") from exc
        yield
