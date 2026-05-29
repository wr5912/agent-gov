from concurrent.futures import ThreadPoolExecutor

from app.runtime.feedback_store import FeedbackStore
from app.runtime.runtime_db import make_session_factory
from app.runtime.schemas import FeedbackSignalCreateRequest


def test_runtime_db_reuses_engine_for_same_path(tmp_path):
    db_path = tmp_path / "runtime.sqlite3"

    first = make_session_factory(db_path)
    second = make_session_factory(db_path)

    assert first.kw["bind"] is second.kw["bind"]


def test_feedback_store_sqlite_handles_concurrent_signal_writes(tmp_path):
    store = FeedbackStore(data_dir=tmp_path / "data", agent_version_provider=lambda: "main-v-test")

    def create_signal(index: int) -> str:
        signal = store.create_signal(
            FeedbackSignalCreateRequest(
                session_id=f"session-{index}",
                labels=["concurrency"],
                comment=f"并发反馈 {index}",
            )
        )
        return signal["signal_id"]

    with ThreadPoolExecutor(max_workers=8) as executor:
        signal_ids = list(executor.map(create_signal, range(24)))

    assert len(signal_ids) == 24
    assert len(set(signal_ids)) == 24
    assert len(store.list_signals(limit=50)) == 24
