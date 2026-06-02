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
                f"PROJECT_ROOT={tmp_path.as_posix()}",
                "API_PORT=8080",
                "WORKSPACE_DIR=${PROJECT_ROOT}/docker/volume/main-workspace",
                "DATA_DIR=${PROJECT_ROOT}/docker/volume/data",
                "CLAUDE_ROOT=${PROJECT_ROOT}/docker/volume/claude-roots/main",
                "LANGFUSE_BASE_URL=http://localhost:53000",
                "",
            ]
        ),
        encoding="utf-8",
    )

    settings = AppSettings(_env_file=(base_env, local_env))

    assert AppSettings.model_config["env_file"] == ("docker/.env", "docker/.env.local")
    assert settings.api_port == 8080
    assert settings.workspace_dir == tmp_path / "docker" / "volume" / "main-workspace"
    assert settings.main_workspace_dir == settings.workspace_dir
    assert settings.attribution_analyzer_workspace_dir == tmp_path / "docker" / "volume" / "attribution-analyzer-workspace"
    assert settings.data_dir == tmp_path / "docker" / "volume" / "data"
    assert settings.claude_root == tmp_path / "docker" / "volume" / "claude-roots" / "main"
    assert settings.main_claude_root == settings.claude_root
    assert settings.attribution_analyzer_claude_root == tmp_path / "docker" / "volume" / "claude-roots" / "attribution-analyzer"
    assert settings.claude_home == settings.claude_root / ".claude"
    assert settings.langfuse_base_url == "http://localhost:53000"
