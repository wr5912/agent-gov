from app.runtime.settings import AppSettings


_PROFILE_ENV_KEYS = (
    "API_PORT",
    "WORKSPACE_DIR",
    "MAIN_WORKSPACE_DIR",
    "ATTRIBUTION_ANALYZER_WORKSPACE_DIR",
    "PROPOSAL_GENERATOR_WORKSPACE_DIR",
    "EXECUTION_OPTIMIZER_WORKSPACE_DIR",
    "EVAL_CASE_GOVERNOR_WORKSPACE_DIR",
    "REGRESSION_IMPACT_ANALYZER_WORKSPACE_DIR",
    "DATA_DIR",
    "CLAUDE_ROOT",
    "MAIN_CLAUDE_ROOT",
    "ATTRIBUTION_ANALYZER_CLAUDE_ROOT",
    "PROPOSAL_GENERATOR_CLAUDE_ROOT",
    "EXECUTION_OPTIMIZER_CLAUDE_ROOT",
    "EVAL_CASE_GOVERNOR_CLAUDE_ROOT",
    "REGRESSION_IMPACT_ANALYZER_CLAUDE_ROOT",
    "CLAUDE_HOME",
    "LANGFUSE_BASE_URL",
)


def test_settings_loads_local_env_after_base_env(tmp_path, monkeypatch):
    for key in _PROFILE_ENV_KEYS:
        monkeypatch.delenv(key, raising=False)

    base_env = tmp_path / "docker.env"
    local_env = tmp_path / "docker.env.local"
    base_env.write_text(
        "\n".join(
            [
                "API_PORT=58080",
                "WORKSPACE_DIR=/main-workspace",
                "MAIN_WORKSPACE_DIR=/main-workspace",
                "ATTRIBUTION_ANALYZER_WORKSPACE_DIR=/attribution-analyzer-workspace",
                "DATA_DIR=/data",
                "CLAUDE_ROOT=/claude-roots/main",
                "MAIN_CLAUDE_ROOT=/claude-roots/main",
                "CLAUDE_HOME=/claude-roots/main/.claude",
                "LANGFUSE_BASE_URL=http://langfuse-web:3000",
                "",
            ]
        ),
        encoding="utf-8",
    )
    local_env.write_text(
        "\n".join(
            [
                f"HOST_RUNTIME_VOLUME_ROOT={(tmp_path / 'volume-agent-runtime').as_posix()}",
                "API_PORT=8080",
                "WORKSPACE_DIR=${HOST_RUNTIME_VOLUME_ROOT}/main-workspace",
                "DATA_DIR=${HOST_RUNTIME_VOLUME_ROOT}/data",
                "CLAUDE_ROOT=${HOST_RUNTIME_VOLUME_ROOT}/claude-roots/main",
                "LANGFUSE_BASE_URL=http://localhost:53000",
                "",
            ]
        ),
        encoding="utf-8",
    )

    settings = AppSettings(_env_file=(base_env, local_env))

    assert AppSettings.model_config["env_file"] == ("docker/.env", "docker/.env.local")
    assert settings.api_port == 8080
    assert settings.workspace_dir == tmp_path / "volume-agent-runtime" / "main-workspace"
    assert settings.main_workspace_dir == settings.workspace_dir
    assert settings.attribution_analyzer_workspace_dir == tmp_path / "volume-agent-runtime" / "attribution-analyzer-workspace"
    assert settings.proposal_generator_workspace_dir == tmp_path / "volume-agent-runtime" / "proposal-generator-workspace"
    assert settings.execution_optimizer_workspace_dir == tmp_path / "volume-agent-runtime" / "execution-optimizer-workspace"
    assert settings.eval_case_governor_workspace_dir == tmp_path / "volume-agent-runtime" / "eval-case-governor-workspace"
    assert settings.regression_impact_analyzer_workspace_dir == tmp_path / "volume-agent-runtime" / "regression-impact-analyzer-workspace"
    assert settings.data_dir == tmp_path / "volume-agent-runtime" / "data"
    assert settings.claude_root == tmp_path / "volume-agent-runtime" / "claude-roots" / "main"
    assert settings.main_claude_root == settings.claude_root
    assert settings.attribution_analyzer_claude_root == tmp_path / "volume-agent-runtime" / "claude-roots" / "attribution-analyzer"
    assert settings.proposal_generator_claude_root == tmp_path / "volume-agent-runtime" / "claude-roots" / "proposal-generator"
    assert settings.execution_optimizer_claude_root == tmp_path / "volume-agent-runtime" / "claude-roots" / "execution-optimizer"
    assert settings.eval_case_governor_claude_root == tmp_path / "volume-agent-runtime" / "claude-roots" / "eval-case-governor"
    assert settings.regression_impact_analyzer_claude_root == tmp_path / "volume-agent-runtime" / "claude-roots" / "regression-impact-analyzer"
    assert settings.claude_home == settings.claude_root / ".claude"
    assert settings.langfuse_base_url == "http://localhost:53000"


def test_get_settings_creates_all_profile_dirs(tmp_path, monkeypatch):
    from app.runtime.settings import get_settings

    for key in _PROFILE_ENV_KEYS:
        monkeypatch.delenv(key, raising=False)
    monkeypatch.chdir(tmp_path)
    runtime_root = tmp_path / "runtime"
    monkeypatch.setenv("WORKSPACE_DIR", str(runtime_root / "main-workspace"))
    monkeypatch.setenv("DATA_DIR", str(runtime_root / "data"))
    monkeypatch.setenv("CLAUDE_ROOT", str(runtime_root / "claude-roots" / "main"))
    get_settings.cache_clear()

    settings = get_settings()

    expected_dirs = (
        settings.data_dir,
        settings.main_workspace_dir,
        settings.attribution_analyzer_workspace_dir,
        settings.proposal_generator_workspace_dir,
        settings.execution_optimizer_workspace_dir,
        settings.eval_case_governor_workspace_dir,
        settings.regression_impact_analyzer_workspace_dir,
        settings.main_claude_root,
        settings.attribution_analyzer_claude_root,
        settings.proposal_generator_claude_root,
        settings.execution_optimizer_claude_root,
        settings.eval_case_governor_claude_root,
        settings.regression_impact_analyzer_claude_root,
        settings.claude_home,
        settings.agent_git_repository_dir,
        settings.agent_git_worktrees_dir,
        settings.agent_release_archives_dir,
    )
    assert all(path.is_dir() for path in expected_dirs)

    get_settings.cache_clear()
