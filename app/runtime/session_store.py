import json
import uuid
from dataclasses import asdict, dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Optional


def utc_now() -> str:
    return datetime.now(timezone.utc).isoformat()


@dataclass
class LocalSession:
    session_id: str
    sdk_session_id: Optional[str] = None
    created_at: str = field(default_factory=utc_now)
    updated_at: str = field(default_factory=utc_now)
    title: Optional[str] = None
    turns: int = 0
    metadata: dict[str, Any] = field(default_factory=dict)


class LocalSessionStore:
    """Tiny JSON-file session store for the API layer.

    Claude Code still owns its internal transcript/session storage. This store only maps
    client-visible session ids to Claude SDK session ids and keeps light metadata.
    """

    def __init__(self, root: Path) -> None:
        self.root = root
        self.root.mkdir(parents=True, exist_ok=True)

    def _path(self, session_id: str) -> Path:
        safe = session_id.replace("/", "_")
        return self.root / f"{safe}.json"

    def create(self, metadata: Optional[dict[str, Any]] = None) -> LocalSession:
        session = LocalSession(session_id=str(uuid.uuid4()), metadata=metadata or {})
        self.save(session)
        return session

    def get_or_create(self, session_id: Optional[str], metadata: Optional[dict[str, Any]] = None) -> LocalSession:
        if session_id:
            existing = self.get(session_id)
            if existing:
                return existing
            session = LocalSession(session_id=session_id, metadata=metadata or {})
            self.save(session)
            return session
        return self.create(metadata=metadata)

    def get(self, session_id: str) -> Optional[LocalSession]:
        path = self._path(session_id)
        if not path.exists():
            return None
        return LocalSession(**json.loads(path.read_text(encoding="utf-8")))

    def save(self, session: LocalSession) -> None:
        session.updated_at = utc_now()
        self._path(session.session_id).write_text(
            json.dumps(asdict(session), ensure_ascii=False, indent=2),
            encoding="utf-8",
        )

    def list(self) -> list[LocalSession]:
        sessions: list[LocalSession] = []
        for path in sorted(self.root.glob("*.json"), key=lambda p: p.stat().st_mtime, reverse=True):
            try:
                sessions.append(LocalSession(**json.loads(path.read_text(encoding="utf-8"))))
            except Exception:
                continue
        return sessions

    def delete(self, session_id: str) -> bool:
        path = self._path(session_id)
        if not path.exists():
            return False
        path.unlink()
        return True
