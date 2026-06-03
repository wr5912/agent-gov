from __future__ import annotations

import asyncio
import logging
import os

from app.runtime.agent_version_store import AgentVersionStore
from app.runtime.claude_runtime import ClaudeRuntime
from app.runtime.session_store import LocalSessionStore
from app.runtime.settings import get_settings
from app.runtime.stores.feedback_store import FeedbackStore
from app.services.agent_job_worker import AgentJobWorker
from app.version import APP_VERSION

logger = logging.getLogger(__name__)


def configure_logging() -> None:
    level_name = os.getenv("AGENT_JOB_WORKER_LOG_LEVEL", "INFO").upper()
    logging.basicConfig(
        level=getattr(logging, level_name, logging.INFO),
        format="%(asctime)s %(levelname)s %(name)s %(message)s",
    )


def build_worker() -> AgentJobWorker:
    settings = get_settings()
    session_store = LocalSessionStore(settings.session_dir)
    agent_version_store = AgentVersionStore(
        versions_dir=settings.agent_versions_dir,
        workspace_dir=settings.main_workspace_dir,
        claude_root=settings.main_claude_root,
    )
    agent_version_store.ensure_bootstrap()
    feedback_store = FeedbackStore(
        data_dir=settings.data_dir,
        workspace_dir=settings.main_workspace_dir,
        agent_version_provider=agent_version_store.current_version_id,
        runtime_version=APP_VERSION,
        enable_debug_evidence=settings.enable_feedback_debug_evidence,
    )
    runtime = ClaudeRuntime(settings, session_store, feedback_store, agent_version_store)
    feedback_store.set_langfuse_trace_fetcher(runtime.fetch_langfuse_trace)
    poll_interval = float(os.getenv("AGENT_JOB_WORKER_POLL_INTERVAL_SECONDS", "2"))
    logger.info(
        "agent job worker configured data_dir=%s poll_interval_seconds=%s",
        settings.data_dir,
        poll_interval,
    )
    return AgentJobWorker(
        feedback_store=feedback_store,
        run_profile_json=lambda **kwargs: runtime._run_profile_json(**kwargs),
        poll_interval_seconds=poll_interval,
    )


async def main() -> None:
    configure_logging()
    logger.info("agent job worker starting")
    await build_worker().run_forever()


if __name__ == "__main__":
    asyncio.run(main())
