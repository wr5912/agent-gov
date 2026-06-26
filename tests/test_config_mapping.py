from app.runtime.config_mapping import build_config_mapping
from app.runtime.settings import AppSettings


def test_config_mapping_uses_native_claude_code_paths(tmp_path):
    # main 已并入 /data：workspace/claude-root 由 data_dir 派生，host 映射经 data 挂载（无独立 main 挂载）。
    data = tmp_path / "volume-agent-gov" / "data"
    settings = AppSettings(
        _env_file=None,
        DATA_DIR=data,
        HOST_DATA_MOUNT="./volume-agent-gov/data",
    )
    workspace = settings.main_workspace_dir
    claude_root = settings.main_claude_root
    claude_home = settings.claude_home
    workspace.mkdir(parents=True, exist_ok=True)
    claude_home.mkdir(parents=True, exist_ok=True)
    (workspace / "CLAUDE.md").write_text("# Project", encoding="utf-8")
    (claude_root / ".claude.json").write_text("{}", encoding="utf-8")

    response = build_config_mapping(settings)
    by_kind = {(item.scope, item.kind): item for item in response.mappings}

    assert response.claude_config_mode == "native"
    assert response.claude_root == str(claude_root)
    assert response.claude_config_dir is None
    assert response.claude_global_config_file == str(claude_root / ".claude.json")
    assert response.setting_sources_effective is None
    assert (
        by_kind[("global", "state")].host_mount
        == "volume-agent-gov/data/business-agents/main-agent/claude-root/.claude.json"
    )
    assert (
        by_kind[("project", "instructions")].host_mount
        == "volume-agent-gov/data/business-agents/main-agent/workspace/CLAUDE.md"
    )
    assert by_kind[("global", "state")].exists is True
