from __future__ import annotations

import json
import sqlite3
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Protocol, TypedDict


class OutboxPayload(TypedDict):
    aid: str
    marker: str
    content: str


class PendingOutboxItem(TypedDict):
    dedupe_key: str
    attempts: int
    last_error: str
    updated_at: str


class DiscoveryFailureItem(TypedDict):
    failure_key: str
    category: str
    run_id: int | None
    attempt: int | None
    detail: str
    occurrences: int
    updated_at: str
    resolved_at: str | None


class WatermarkItem(TypedDict):
    stream_key: str
    value: str


class OutboxSnapshot(TypedDict):
    pending: int
    delivered: int
    pending_items: list[PendingOutboxItem]
    discovery_failures: int
    failure_items: list[DiscoveryFailureItem]
    watermarks: list[WatermarkItem]


class RelayStream(Protocol):
    repository: str
    workflow_file: str


@dataclass(frozen=True, order=True)
class RunWatermark:
    updated_at: datetime
    run_id: int
    attempt: int

    def render(self) -> str:
        return f"{self.updated_at.isoformat()}|{self.run_id}|{self.attempt}"


def utc_now() -> str:
    return datetime.now(timezone.utc).isoformat()


def _stream_key(config: RelayStream, event: str) -> str:
    return f"{config.repository}:{config.workflow_file}:{event}"


class OutboxStore:
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
                CREATE TABLE IF NOT EXISTS outbox (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    dedupe_key TEXT NOT NULL UNIQUE,
                    payload TEXT NOT NULL,
                    status TEXT NOT NULL DEFAULT 'pending'
                        CHECK(status IN ('pending', 'delivered')),
                    attempts INTEGER NOT NULL DEFAULT 0,
                    last_error TEXT,
                    created_at TEXT NOT NULL,
                    updated_at TEXT NOT NULL
                );
                CREATE TABLE IF NOT EXISTS discovery_failures (
                    failure_key TEXT PRIMARY KEY,
                    category TEXT NOT NULL,
                    run_id INTEGER,
                    attempt INTEGER,
                    detail TEXT NOT NULL,
                    replay_payload TEXT,
                    resolved_at TEXT,
                    occurrences INTEGER NOT NULL DEFAULT 1,
                    created_at TEXT NOT NULL,
                    updated_at TEXT NOT NULL
                );
                CREATE TABLE IF NOT EXISTS discovery_watermarks (
                    stream_key TEXT PRIMARY KEY,
                    updated_at TEXT NOT NULL,
                    run_id INTEGER NOT NULL,
                    attempt INTEGER NOT NULL
                );
                """
            )
            failure_columns = {str(row["name"]) for row in self._connection.execute("PRAGMA table_info(discovery_failures)")}
            if "replay_payload" not in failure_columns:
                self._connection.execute("ALTER TABLE discovery_failures ADD COLUMN replay_payload TEXT")
            if "resolved_at" not in failure_columns:
                self._connection.execute("ALTER TABLE discovery_failures ADD COLUMN resolved_at TEXT")

    def enqueue(self, dedupe_key: str, payload: OutboxPayload) -> bool:
        now = utc_now()
        with self._connection:
            result = self._connection.execute(
                "INSERT OR IGNORE INTO outbox(dedupe_key, payload, created_at, updated_at) VALUES(?, ?, ?, ?)",
                (
                    dedupe_key,
                    json.dumps(payload, ensure_ascii=False),
                    now,
                    now,
                ),
            )
        return result.rowcount == 1

    def has_run_attempt(
        self,
        repository: str,
        run_id: int,
        attempt: int,
    ) -> bool:
        repository_aware = f"github-run:{repository}:{run_id}:{attempt}:*"
        legacy = f"github-run:{run_id}:{attempt}:*"
        row = self._connection.execute(
            "SELECT 1 FROM outbox WHERE dedupe_key GLOB ? OR dedupe_key GLOB ? LIMIT 1",
            (repository_aware, legacy),
        ).fetchone()
        return row is not None

    def pending(
        self,
        limit: int = 100,
        *,
        unattempted_only: bool = False,
    ) -> list[sqlite3.Row]:
        condition = " AND attempts = 0" if unattempted_only else ""
        return self._connection.execute(
            f"SELECT * FROM outbox WHERE status = 'pending'{condition} ORDER BY id LIMIT ?",
            (limit,),
        ).fetchall()

    def mark_delivered(self, row_id: int) -> None:
        with self._connection:
            self._connection.execute(
                "UPDATE outbox SET status = 'delivered', attempts = attempts + 1, last_error = NULL, updated_at = ? WHERE id = ?",
                (utc_now(), row_id),
            )

    def mark_failed(self, row_id: int, detail: str) -> None:
        with self._connection:
            self._connection.execute(
                "UPDATE outbox SET attempts = attempts + 1, last_error = ?, updated_at = ? WHERE id = ?",
                (detail[:2000], utc_now(), row_id),
            )

    def record_failure(
        self,
        *,
        failure_key: str,
        category: str,
        detail: str,
        run_id: int | None = None,
        attempt: int | None = None,
        replay_payload: str | None = None,
    ) -> None:
        now = utc_now()
        with self._connection:
            self._connection.execute(
                """
                INSERT INTO discovery_failures(
                    failure_key, category, run_id, attempt, detail, replay_payload,
                    occurrences, created_at, updated_at
                ) VALUES(?, ?, ?, ?, ?, ?, 1, ?, ?)
                ON CONFLICT(failure_key) DO UPDATE SET
                    category = excluded.category,
                    run_id = excluded.run_id,
                    attempt = excluded.attempt,
                    detail = excluded.detail,
                    replay_payload = COALESCE(
                        excluded.replay_payload,
                        discovery_failures.replay_payload
                    ),
                    resolved_at = NULL,
                    occurrences = discovery_failures.occurrences + 1,
                    updated_at = excluded.updated_at
                """,
                (
                    failure_key,
                    category,
                    run_id,
                    attempt,
                    detail[:2000],
                    replay_payload,
                    now,
                    now,
                ),
            )

    def retryable_failures(
        self,
        repository: str,
        limit: int = 50,
    ) -> list[sqlite3.Row]:
        return self._connection.execute(
            """
            SELECT failure_key, replay_payload
            FROM discovery_failures
            WHERE resolved_at IS NULL
              AND replay_payload IS NOT NULL
              AND run_id IS NOT NULL
              AND attempt IS NOT NULL
              AND failure_key GLOB ?
            ORDER BY updated_at, failure_key
            LIMIT ?
            """,
            (f"github-run:{repository}:*", limit),
        ).fetchall()

    def resolve_run_failures(
        self,
        repository: str,
        run_id: int,
        attempt: int,
    ) -> None:
        now = utc_now()
        with self._connection:
            self._connection.execute(
                """
                UPDATE discovery_failures
                SET resolved_at = ?, updated_at = ?
                WHERE run_id = ?
                  AND attempt = ?
                  AND failure_key GLOB ?
                  AND resolved_at IS NULL
                """,
                (
                    now,
                    now,
                    run_id,
                    attempt,
                    f"github-run:{repository}:{run_id}:{attempt}:*",
                ),
            )

    def failure_evidence(self, limit: int = 50) -> list[DiscoveryFailureItem]:
        rows = self._connection.execute(
            "SELECT failure_key, category, run_id, attempt, detail, occurrences, "
            "updated_at, resolved_at FROM discovery_failures "
            "ORDER BY updated_at DESC LIMIT ?",
            (limit,),
        ).fetchall()
        return [
            {
                "failure_key": str(row["failure_key"]),
                "category": str(row["category"]),
                "run_id": int(row["run_id"]) if row["run_id"] is not None else None,
                "attempt": int(row["attempt"]) if row["attempt"] is not None else None,
                "detail": str(row["detail"]),
                "occurrences": int(row["occurrences"]),
                "updated_at": str(row["updated_at"]),
                "resolved_at": (str(row["resolved_at"]) if row["resolved_at"] is not None else None),
            }
            for row in rows
        ]

    def get_watermark(
        self,
        config: RelayStream,
        event: str,
    ) -> RunWatermark | None:
        row = self._connection.execute(
            "SELECT updated_at, run_id, attempt FROM discovery_watermarks WHERE stream_key = ?",
            (_stream_key(config, event),),
        ).fetchone()
        if row is None:
            return None
        return RunWatermark(
            updated_at=datetime.fromisoformat(str(row["updated_at"])),
            run_id=int(row["run_id"]),
            attempt=int(row["attempt"]),
        )

    def set_watermark(
        self,
        config: RelayStream,
        event: str,
        watermark: RunWatermark,
    ) -> None:
        with self._connection:
            self._connection.execute(
                """
                INSERT INTO discovery_watermarks(stream_key, updated_at, run_id, attempt)
                VALUES(?, ?, ?, ?)
                ON CONFLICT(stream_key) DO UPDATE SET
                    updated_at = excluded.updated_at,
                    run_id = excluded.run_id,
                    attempt = excluded.attempt
                """,
                (
                    _stream_key(config, event),
                    watermark.updated_at.isoformat(),
                    watermark.run_id,
                    watermark.attempt,
                ),
            )

    def watermarks(self) -> list[WatermarkItem]:
        return [
            {
                "stream_key": str(row["stream_key"]),
                "value": RunWatermark(
                    updated_at=datetime.fromisoformat(str(row["updated_at"])),
                    run_id=int(row["run_id"]),
                    attempt=int(row["attempt"]),
                ).render(),
            }
            for row in self._connection.execute("SELECT stream_key, updated_at, run_id, attempt FROM discovery_watermarks ORDER BY stream_key")
        ]

    def snapshot(self) -> OutboxSnapshot:
        counts = {str(row["status"]): int(row["count"]) for row in self._connection.execute("SELECT status, COUNT(*) AS count FROM outbox GROUP BY status")}
        pending = [
            {
                "dedupe_key": str(row["dedupe_key"]),
                "attempts": int(row["attempts"]),
                "last_error": str(row["last_error"] or ""),
                "updated_at": str(row["updated_at"]),
            }
            for row in self._connection.execute("SELECT dedupe_key, attempts, last_error, updated_at FROM outbox WHERE status = 'pending' ORDER BY id LIMIT 50")
        ]
        failures = self.failure_evidence()
        failure_count = int(self._connection.execute("SELECT COUNT(*) FROM discovery_failures WHERE resolved_at IS NULL").fetchone()[0])
        return {
            "pending": counts.get("pending", 0),
            "delivered": counts.get("delivered", 0),
            "pending_items": pending,
            "discovery_failures": failure_count,
            "failure_items": failures,
            "watermarks": self.watermarks(),
        }
