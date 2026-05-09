from app.runtime.config_mapping import build_config_mapping
from app.runtime.settings import AppSettings


def test_config_mapping_uses_native_claude_code_paths(tmp_path):
    workspace = tmp_path / "docker" / "volume" / "workspace"
    claude_root = tmp_path / "docker" / "volume" / "claude-root"
    claude_home = claude_root / ".claude"
    data = tmp_path / "docker" / "volume" / "data"
    workspace.mkdir(parents=True)
    claude_home.mkdir(parents=True)
    (workspace / "CLAUDE.md").write_text("# Project", encoding="utf-8")
    (claude_root / ".claude.json").write_text("{}", encoding="utf-8")

    settings = AppSettings(
        _env_file=None,
        WORKSPACE_DIR=workspace,
        DATA_DIR=data,
        CLAUDE_ROOT=claude_root,
        CLAUDE_HOME=claude_home,
        HOST_WORKSPACE_MOUNT="./docker/volume/workspace",
        HOST_CLAUDE_ROOT_MOUNT="./docker/volume/claude-root",
    )

    response = build_config_mapping(settings)
    by_kind = {(item.scope, item.kind): item for item in response.mappings}

    assert response.claude_config_mode == "native"
    assert response.claude_root == str(claude_root)
    assert response.claude_config_dir is None
    assert response.claude_global_config_file == str(claude_root / ".claude.json")
    assert response.setting_sources_effective == ["user", "project", "local"]
    assert by_kind[("global", "state")].host_mount == "docker/volume/claude-root/.claude.json"
    assert by_kind[("project", "instructions")].host_mount == "docker/volume/workspace/CLAUDE.md"
    assert by_kind[("global", "state")].exists is True
