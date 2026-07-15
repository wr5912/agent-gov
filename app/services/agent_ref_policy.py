from __future__ import annotations

import tempfile
from collections.abc import Callable, Mapping
from pathlib import Path

from app.runtime.agent_git_store import AgentGitError, GitAgentVersionStore
from app.runtime.managed_agent_policy import (
    ManagedAgentPolicyError,
    managed_workspace_policy_paths,
    require_runtime_workspace_policy,
)


def build_ref_policy_validator(
    store: GitAgentVersionStore,
    agent_id: str,
    *,
    data_dir: Path,
    runtime_mode: str,
    runtime_env: Mapping[str, str],
) -> Callable[[str], None]:
    managed_paths = managed_workspace_policy_paths(agent_id)

    def validate(ref: str) -> None:
        try:
            with tempfile.TemporaryDirectory(prefix=f"agentgov-policy-{agent_id}-") as temporary:
                workspace = Path(temporary)
                for relative in managed_paths:
                    content = store.read_text_at_ref(ref, relative)
                    if content is None:
                        continue
                    target = workspace / relative
                    target.parent.mkdir(parents=True, exist_ok=True)
                    target.write_text(content, encoding="utf-8")
                resolved_data_dir = data_dir.resolve()
                runtime_root = Path("/") if resolved_data_dir == Path("/data") else resolved_data_dir.parent
                require_runtime_workspace_policy(
                    workspace=workspace,
                    agent_id=agent_id,
                    runtime_mode=runtime_mode,
                    env=runtime_env,
                    runtime_root=runtime_root,
                )
        except ManagedAgentPolicyError as exc:
            raise AgentGitError(f"Managed Agent policy rejected ref {ref}: {exc}") from exc

    return validate
