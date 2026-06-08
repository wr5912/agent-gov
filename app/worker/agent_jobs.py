from __future__ import annotations

import asyncio
import logging
import os

from app.runtime.agent_git_store import GitAgentVersionStore
from app.runtime.claude_runtime import ClaudeRuntime
from app.runtime.logging_config import configure_runtime_logging
from app.runtime.session_store import LocalSessionStore
from app.runtime.settings import AppSettings, get_settings, runtime_settings_log_message
from app.runtime.stores.feedback_store import FeedbackStore
from app.services.agent_job_worker import AgentJobWorker
from app.version import APP_VERSION

logger = logging.getLogger(__name__)


def build_worker(settings: AppSettings | None = None) -> AgentJobWorker:
    settings = settings or get_settings()
    session_store = LocalSessionStore(settings.session_dir)
    agent_version_store = GitAgentVersionStore(
        repository_dir=settings.agent_git_repository_dir,
        worktrees_dir=settings.agent_git_worktrees_dir,
        releases_dir=settings.agent_release_archives_dir,
        service_provider=settings.agent_git_service_provider,
        service_url=settings.agent_git_service_url,
        service_public_url=settings.agent_git_service_public_url,
        repository_name=settings.agent_git_repository_name,
        git_user_name=settings.agent_git_user_name,
        git_user_email=settings.agent_git_user_email,
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
        runtime_settings_log_message(settings),
    )
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
    settings = get_settings()
    configure_runtime_logging(settings.log_level)
    logger.info("agent job worker starting")
    await build_worker(settings).run_forever()


if __name__ == "__main__":
    asyncio.run(main())
