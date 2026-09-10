from __future__ import annotations

from pathlib import Path
from unittest.mock import Mock

import pytest
from scripts import run_container_acceptance as acceptance


@pytest.mark.parametrize("profile_name", ["core", "langfuse"])
@pytest.mark.parametrize("preparation_fails", [False, True])
def test_startup_prepares_published_snapshots_after_build_before_runtime(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, profile_name: str, preparation_fails: bool
) -> None:
    isolation = acceptance.IsolatedEnvironment(tmp_path / "env", tmp_path / "runtime-root", "test", "test", {})
    monkeypatch.setattr(acceptance, "_validate_service_model", Mock())
    monkeypatch.setattr(acceptance, "_validate_isolated_mounts", Mock())
    monkeypatch.setattr(acceptance, "_verify_container", Mock())
    commands: list[list[str]] = []

    def run(command, **_kwargs):
        commands.append(command)
        if preparation_fails and command[-1] == "app.runtime.published_harness_preparation":
            raise acceptance.AcceptanceError("preparation failed")
        return ""

    monkeypatch.setattr(acceptance, "_run_checked", run)
    if preparation_fails:
        with pytest.raises(acceptance.AcceptanceError, match="preparation failed"):
            acceptance.refresh_profile(acceptance.PROFILES[profile_name], isolation, {acceptance.RUN_ID_ENV: "test"})
        assert not any("up" in command for command in commands)
    else:
        acceptance.refresh_profile(acceptance.PROFILES[profile_name], isolation, {acceptance.RUN_ID_ENV: "test"})
        assert next(index for index, command in enumerate(commands) if "up" in command) == len(commands) - 1
    build_index = next(index for index, command in enumerate(commands) if "build" in command)
    prepare_index = next(index for index, command in enumerate(commands) if command[-1] == "app.runtime.published_harness_preparation")
    assert prepare_index == build_index + 1
    prepare = commands[prepare_index]
    assert "--rm" in prepare and "--no-deps" in prepare and "--service-ports" not in prepare
    assert prepare[prepare.index("--entrypoint") + 1 :] == ["python", "agent-gov-api", "-m", "app.runtime.published_harness_preparation"]


def test_normal_deployment_uses_the_same_preparation_entry_before_up() -> None:
    makefile = (Path(__file__).resolve().parents[1] / "Makefile").read_text(encoding="utf-8")
    for target in ("up", "all-up"):
        recipe = makefile.split(f"\n{target}: ", 1)[1].split("\n\n", 1)[0]
        assert recipe.index("runtime-bootstrap") < recipe.index("runtime-prepare-harnesses") < recipe.index(" up -d")
    assert "runtime-prepare-harnesses: cutover-inspect" in makefile
    assert "--entrypoint python agent-gov-api -m app.runtime.published_harness_preparation" in makefile


def test_isolated_credentials_are_ephemeral_and_consumers_have_one_definition(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    source = tmp_path / "source.env"
    original = "API_KEY=change-me\nFRONTEND_RUNTIME_API_KEY=old-key\nMODEL_PROVIDER_API_KEY=private-provider\n"
    source.write_text(original, encoding="utf-8")
    monkeypatch.setattr(acceptance, "_allocate_loopback_ports", lambda _count: tuple(range(50400, 50405)))
    first_root = tmp_path / "first"
    second_root = tmp_path / "second"
    first_root.mkdir()
    second_root.mkdir()

    first = acceptance.prepare_isolated_environment(source, "1234-first", first_root)
    second = acceptance.prepare_isolated_environment(source, "1234-second", second_root)

    assert first.overrides["API_KEY"] != second.overrides["API_KEY"]
    assert len(bytes.fromhex(first.overrides["API_KEY"])) == 32
    assert first.overrides["FRONTEND_RUNTIME_API_KEY"] == first.overrides["API_KEY"]
    lines = first.env_file.read_text(encoding="utf-8").splitlines()
    for key in first.overrides:
        assert [line for line in lines if line.startswith(key + "=")] == [key + "=" + first.overrides[key]]
    assert "MODEL_PROVIDER_API_KEY=private-provider" in lines
    assert "change-me" not in first.env_file.read_text(encoding="utf-8")
    assert first.env_file.stat().st_mode & 0o777 == 0o600
    assert source.read_text(encoding="utf-8") == original
