from __future__ import annotations

import os
import subprocess
import sys
from collections.abc import Callable
from pathlib import Path

import pytest


REPO_ROOT = Path(__file__).resolve().parents[1]
SCRIPT = REPO_ROOT / "scripts" / "check_codex_governance.py"


def _write_lines(path: Path, count: int) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text("x = 1\n" * count, encoding="utf-8")


def _git(root: Path, *args: str) -> None:
    env = os.environ.copy()
    env.update(
        {
            "GIT_AUTHOR_NAME": "Test",
            "GIT_AUTHOR_EMAIL": "test@example.com",
            "GIT_COMMITTER_NAME": "Test",
            "GIT_COMMITTER_EMAIL": "test@example.com",
        }
    )
    subprocess.run(["git", "-C", str(root), *args], check=True, env=env, capture_output=True)


def _commit_all(root: Path, message: str = "baseline") -> None:
    _git(root, "add", ".")
    _git(root, "commit", "-m", message)


def _init_repo(root: Path) -> None:
    _git(root, "init")
    _git(root, "config", "user.name", "Test")
    _git(root, "config", "user.email", "test@example.com")


def _run_guard(root: Path, mode: str = "fail", *extra: str) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        [sys.executable, str(SCRIPT), "--root", str(root), "--mode", mode, *extra],
        check=False,
        capture_output=True,
        text=True,
    )


def _dict_return_source(name: str) -> str:
    return f"def {name}() -> " "dict[str, object]:\n    return {}\n"


def _dict_any_arg_source(name: str) -> str:
    return "from typing import Any\n\n" f"def {name}(payload: " "dict[str, Any]) -> None:\n    return None\n"


def _typing_dict_any_arg_source(name: str) -> str:
    return "import typing\n\n" f"def {name}(payload: " "typing.Dict[str, typing.Any]) -> None:\n    return None\n"


def _dict_any_key_arg_source(name: str) -> str:
    return "from typing import Any\n\n" f"def {name}(payload: " "dict[Any, str]) -> None:\n    return None\n"


def _nested_dict_any_arg_source(name: str) -> str:
    return "from typing import Any\n\n" f"def {name}(payload: " "list[dict[str, Any]]) -> None:\n    return None\n"


def _quoted_dict_any_arg_source(name: str) -> str:
    return "from typing import Any\n\n" f"def {name}(payload: " '"dict[str, Any]"' ") -> None:\n    return None\n"


def _dict_optional_any_arg_source(name: str) -> str:
    return (
        "from typing import Any, Optional\n\n"
        f"def {name}(payload: "
        "dict[str, Optional[Any]]) -> None:\n    return None\n"
    )


def _mapping_any_arg_source(name: str) -> str:
    return (
        "from collections.abc import Mapping\n"
        "from typing import Any\n\n"
        f"def {name}(payload: "
        "Mapping[str, Any]) -> None:\n    return None\n"
    )


def _mutable_mapping_any_arg_source(name: str) -> str:
    return (
        "from typing import Any, MutableMapping\n\n"
        f"def {name}(payload: "
        "MutableMapping[str, Any]) -> None:\n    return None\n"
    )


def _dict_any_return_source(name: str) -> str:
    return "from typing import Any\n\n" f"def {name}() -> " "dict[str, Any]:\n    return {}\n"


def _dict_any_union_return_source(name: str) -> str:
    return "from typing import Any\n\n" f"def {name}() -> " "dict[str, Any | None]:\n    return {}\n"


def _dict_any_field_source(_name: str) -> str:
    return "from typing import Any\n\n" "class Response:\n" "    payload: " "dict[str, Any]\n"


def _dict_any_alias_source(_name: str) -> str:
    return "from typing import Any\n\n" "Payload = " "dict[str, Any]\n"


def _defaultdict_any_alias_source(_name: str) -> str:
    return "from typing import Any, DefaultDict\n\n" "Payload = " "DefaultDict[str, Any]\n"


def _payload_dict_return_source() -> str:
    return "class Record:\n" "    def to_payload(self) -> " "dict[str, object]:\n" "        return {}\n"


def test_existing_oversized_file_is_allowed_when_not_growing(tmp_path: Path) -> None:
    _init_repo(tmp_path)
    _write_lines(tmp_path / "app" / "large.py", 5)
    _commit_all(tmp_path)

    result = _run_guard(tmp_path, "fail", "--python-file-lines", "2")

    assert result.returncode == 0
    assert "BASELINE: app/large.py" in result.stdout
    assert "existing oversized file not grown" in result.stdout


def test_existing_oversized_file_growth_fails(tmp_path: Path) -> None:
    _init_repo(tmp_path)
    _write_lines(tmp_path / "app" / "large.py", 5)
    _commit_all(tmp_path)
    _write_lines(tmp_path / "app" / "large.py", 6)

    result = _run_guard(tmp_path, "fail", "--python-file-lines", "2")

    assert result.returncode == 1
    assert "existing oversized file grew: 5 -> 6" in result.stdout


def test_new_oversized_file_fails(tmp_path: Path) -> None:
    _init_repo(tmp_path)
    _write_lines(tmp_path / "app" / "small.py", 1)
    _commit_all(tmp_path)
    _write_lines(tmp_path / "frontend" / "src" / "Large.tsx", 3)

    result = _run_guard(tmp_path, "fail", "--frontend-file-lines", "2")

    assert result.returncode == 1
    assert "new oversized file: 3 > 2" in result.stdout


def test_generated_oversized_file_is_ignored(tmp_path: Path) -> None:
    _init_repo(tmp_path)
    _write_lines(tmp_path / "app" / "small.py", 1)
    _commit_all(tmp_path)
    generated = tmp_path / "frontend" / "src" / "types" / "api.ts"
    generated.parent.mkdir(parents=True, exist_ok=True)
    generated.write_text(
        "/**\n * This file was auto-generated by openapi-typescript.\n */\n"
        + "export type X = string;\n" * 10,
        encoding="utf-8",
    )

    result = _run_guard(tmp_path, "fail", "--frontend-file-lines", "2")

    assert result.returncode == 0
    assert "api.ts" not in result.stdout


def test_warn_mode_reports_but_does_not_fail(tmp_path: Path) -> None:
    _init_repo(tmp_path)
    _write_lines(tmp_path / "app" / "small.py", 1)
    _commit_all(tmp_path)
    _write_lines(tmp_path / "app" / "large.py", 3)

    result = _run_guard(tmp_path, "warn", "--python-file-lines", "2")

    assert result.returncode == 0
    assert "WARN: app/large.py" in result.stdout


def test_existing_long_function_is_allowed_when_not_growing(tmp_path: Path) -> None:
    _init_repo(tmp_path)
    body = "\n".join(f"    value_{index} = {index}" for index in range(4))
    path = tmp_path / "app" / "long_function.py"
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(f"def too_long():\n{body}\n", encoding="utf-8")
    _commit_all(tmp_path)

    result = _run_guard(tmp_path, "fail", "--python-function-lines", "2")

    assert result.returncode == 0
    assert "existing long function too_long not grown" in result.stdout


def test_existing_long_function_growth_fails(tmp_path: Path) -> None:
    _init_repo(tmp_path)
    path = tmp_path / "app" / "long_function.py"
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text("def too_long():\n    a = 1\n    b = 2\n", encoding="utf-8")
    _commit_all(tmp_path)
    path.write_text("def too_long():\n    a = 1\n    b = 2\n    c = 3\n", encoding="utf-8")

    result = _run_guard(tmp_path, "fail", "--python-function-lines", "2")

    assert result.returncode == 1
    assert "existing long function too_long grew: 3 -> 4" in result.stdout


def test_new_long_function_fails(tmp_path: Path) -> None:
    _init_repo(tmp_path)
    _write_lines(tmp_path / "app" / "small.py", 1)
    _commit_all(tmp_path)
    body = "\n".join(f"    value_{index} = {index}" for index in range(3))
    path = tmp_path / "app" / "long_function.py"
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(f"def too_long():\n{body}\n", encoding="utf-8")

    result = _run_guard(tmp_path, "fail", "--python-function-lines", "2")

    assert result.returncode == 1
    assert "new long function too_long" in result.stdout


def test_existing_large_class_growth_fails(tmp_path: Path) -> None:
    _init_repo(tmp_path)
    path = tmp_path / "app" / "large_class.py"
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(
        "class Large:\n"
        "    def one(self):\n        return 1\n"
        "    def two(self):\n        return 2\n",
        encoding="utf-8",
    )
    _commit_all(tmp_path)
    path.write_text(
        "class Large:\n"
        "    def one(self):\n        return 1\n"
        "    def two(self):\n        return 2\n"
        "    def three(self):\n        return 3\n",
        encoding="utf-8",
    )

    result = _run_guard(tmp_path, "fail", "--python-class-public-methods", "1")

    assert result.returncode == 1
    assert "existing large class Large grew: 2 -> 3" in result.stdout


def test_existing_router_count_growth_fails(tmp_path: Path) -> None:
    _init_repo(tmp_path)
    path = tmp_path / "app" / "routers" / "many_routes.py"
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(
        "router = object()\n"
        "@router.get('/one')\ndef one():\n    return None\n"
        "@router.get('/two')\ndef two():\n    return None\n",
        encoding="utf-8",
    )
    _commit_all(tmp_path)
    path.write_text(
        "router = object()\n"
        "@router.get('/one')\ndef one():\n    return None\n"
        "@router.get('/two')\ndef two():\n    return None\n"
        "@router.get('/three')\ndef three():\n    return None\n",
        encoding="utf-8",
    )

    result = _run_guard(tmp_path, "fail", "--python-route-count", "1")

    assert result.returncode == 1
    assert "existing oversized router file grew: 2 -> 3" in result.stdout


def test_existing_state_machine_missing_transition_is_allowed(tmp_path: Path) -> None:
    _init_repo(tmp_path)
    path = tmp_path / "app" / "runtime" / "state_machines.py"
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(
        '_KNOWN_STATES = {"job": {"queued"}, "batch": {"draft"}}\n'
        '_TRANSITIONS = {"job": {"queued": set()}}\n',
        encoding="utf-8",
    )
    _commit_all(tmp_path)

    result = _run_guard(tmp_path)

    assert result.returncode == 0
    assert "BASELINE: app/runtime/state_machines.py" in result.stdout


def test_new_state_machine_missing_transition_fails(tmp_path: Path) -> None:
    _init_repo(tmp_path)
    path = tmp_path / "app" / "runtime" / "state_machines.py"
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(
        '_KNOWN_STATES = {"job": {"queued"}}\n'
        '_TRANSITIONS = {"job": {"queued": set()}}\n',
        encoding="utf-8",
    )
    _commit_all(tmp_path)
    path.write_text(
        '_KNOWN_STATES = {"job": {"queued"}, "batch": {"draft"}}\n'
        '_TRANSITIONS = {"job": {"queued": set()}}\n',
        encoding="utf-8",
    )

    result = _run_guard(tmp_path)

    assert result.returncode == 1
    assert "new state machine without transitions: batch" in result.stdout


def test_existing_unowned_dict_return_is_allowed_when_unchanged(tmp_path: Path) -> None:
    _init_repo(tmp_path)
    path = tmp_path / "app" / "service.py"
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(_dict_return_source("legacy"), encoding="utf-8")
    _commit_all(tmp_path)

    result = _run_guard(tmp_path)

    assert result.returncode == 0
    assert "unowned dict return" not in result.stdout


def test_new_unowned_dict_return_fails(tmp_path: Path) -> None:
    _init_repo(tmp_path)
    _write_lines(tmp_path / "app" / "small.py", 1)
    _commit_all(tmp_path)
    path = tmp_path / "app" / "service.py"
    path.write_text(_dict_return_source("new_contract"), encoding="utf-8")

    result = _run_guard(tmp_path)

    assert result.returncode == 1
    assert "new unowned dict return annotation: new_contract" in result.stdout


def test_new_owned_payload_dict_return_is_allowed(tmp_path: Path) -> None:
    _init_repo(tmp_path)
    _write_lines(tmp_path / "app" / "small.py", 1)
    _commit_all(tmp_path)
    path = tmp_path / "app" / "runtime" / "records" / "record.py"
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(_payload_dict_return_source(), encoding="utf-8")

    result = _run_guard(tmp_path)

    assert result.returncode == 0
    assert "unowned dict return" not in result.stdout


def test_existing_dict_any_annotation_is_allowed_when_unchanged(tmp_path: Path) -> None:
    _init_repo(tmp_path)
    path = tmp_path / "app" / "legacy.py"
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(_dict_any_arg_source("legacy"), encoding="utf-8")
    _commit_all(tmp_path)

    result = _run_guard(tmp_path)

    assert result.returncode == 0
    assert "broad map[Any]" not in result.stdout


@pytest.mark.parametrize(
    ("source_factory", "expected_message"),
    [
        (_dict_any_arg_source, "new broad map[Any] type boundary: new_contract:arg:payload"),
        (_typing_dict_any_arg_source, "new broad map[Any] type boundary: new_contract:arg:payload"),
        (_dict_any_key_arg_source, "new broad map[Any] type boundary: new_contract:arg:payload"),
        (_quoted_dict_any_arg_source, "new broad map[Any] type boundary: new_contract:arg:payload"),
        (_nested_dict_any_arg_source, "new broad map[Any] type boundary: new_contract:arg:payload"),
        (_dict_optional_any_arg_source, "new broad map[Any] type boundary: new_contract:arg:payload"),
        (_mapping_any_arg_source, "new broad map[Any] type boundary: new_contract:arg:payload"),
        (_mutable_mapping_any_arg_source, "new broad map[Any] type boundary: new_contract:arg:payload"),
        (_dict_any_return_source, "new broad map[Any] type boundary: new_contract:return"),
        (_dict_any_union_return_source, "new broad map[Any] type boundary: new_contract:return"),
        (_dict_any_field_source, "new broad map[Any] type boundary: Response.payload:annotation"),
        (_dict_any_alias_source, "new broad map[Any] type boundary: Payload:annotation"),
        (_defaultdict_any_alias_source, "new broad map[Any] type boundary: Payload:annotation"),
    ],
)
def test_new_map_any_type_boundary_fails(
    tmp_path: Path,
    source_factory: Callable[[str], str],
    expected_message: str,
) -> None:
    _init_repo(tmp_path)
    _write_lines(tmp_path / "app" / "small.py", 1)
    _commit_all(tmp_path)
    path = tmp_path / "app" / "schema.py"
    path.write_text(source_factory("new_contract"), encoding="utf-8")

    result = _run_guard(tmp_path)

    assert result.returncode == 1
    assert expected_message in result.stdout


def test_dict_object_annotation_is_allowed(tmp_path: Path) -> None:
    _init_repo(tmp_path)
    _write_lines(tmp_path / "app" / "small.py", 1)
    _commit_all(tmp_path)
    path = tmp_path / "app" / "schema.py"
    path.write_text("class Response:\n    payload: dict[str, object]\n", encoding="utf-8")

    result = _run_guard(tmp_path)

    assert result.returncode == 0
    assert "broad map[Any]" not in result.stdout


def test_new_legacy_json_types_import_fails(tmp_path: Path) -> None:
    _init_repo(tmp_path)
    _write_lines(tmp_path / "app" / "small.py", 1)
    _commit_all(tmp_path)
    path = tmp_path / "app" / "runtime" / "records" / "record.py"
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text("from .json_types import JsonObject\n", encoding="utf-8")

    result = _run_guard(tmp_path)

    assert result.returncode == 1
    assert "new legacy JsonObject import from records boundary" in result.stdout


def test_new_record_non_boundary_jsonobject_field_fails(tmp_path: Path) -> None:
    _init_repo(tmp_path)
    _write_lines(tmp_path / "app" / "small.py", 1)
    _commit_all(tmp_path)
    path = tmp_path / "app" / "runtime" / "records" / "record.py"
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(
        "from app.runtime.json_types import JsonObject\n\n"
        "class Record:\n"
        "    stable_entity: JsonObject\n",
        encoding="utf-8",
    )

    result = _run_guard(tmp_path)

    assert result.returncode == 1
    assert "new non-boundary JsonObject record field: Record.stable_entity" in result.stdout


def test_new_record_boundary_jsonobject_field_is_allowed(tmp_path: Path) -> None:
    _init_repo(tmp_path)
    _write_lines(tmp_path / "app" / "small.py", 1)
    _commit_all(tmp_path)
    path = tmp_path / "app" / "runtime" / "records" / "record.py"
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(
        "from app.runtime.json_types import JsonObject\n\n"
        "class Record:\n"
        "    raw_output_json: JsonObject\n",
        encoding="utf-8",
    )

    result = _run_guard(tmp_path)

    assert result.returncode == 0
    assert "non-boundary JsonObject" not in result.stdout


def test_new_store_public_jsonobject_return_fails(tmp_path: Path) -> None:
    _init_repo(tmp_path)
    _write_lines(tmp_path / "app" / "small.py", 1)
    _commit_all(tmp_path)
    path = tmp_path / "app" / "runtime" / "stores" / "store.py"
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(
        "from app.runtime.json_types import JsonObject\n\n"
        "class Store:\n"
        "    def get_entity(self) -> JsonObject:\n"
        "        return {}\n",
        encoding="utf-8",
    )

    result = _run_guard(tmp_path)

    assert result.returncode == 1
    assert "new store public method returning JsonObject: Store.get_entity" in result.stdout


def test_new_legacy_feedback_active_reference_fails(tmp_path: Path) -> None:
    _init_repo(tmp_path)
    _write_lines(tmp_path / "app" / "small.py", 1)
    _commit_all(tmp_path)
    path = tmp_path / "frontend" / "src" / "feedback.ts"
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(
        'export const legacyPath = "/api/feedback-cases/fbc-test/proposal-jobs";\n',
        encoding="utf-8",
    )

    result = _run_guard(tmp_path)

    assert result.returncode == 1
    assert "new active legacy feedback optimization reference: /proposal-jobs" in result.stdout


def test_new_agent_output_schema_version_reference_fails(tmp_path: Path) -> None:
    _init_repo(tmp_path)
    _write_lines(tmp_path / "app" / "small.py", 1)
    _commit_all(tmp_path)
    path = tmp_path / "app" / "runtime" / "schema_versions.py"
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text('NEW_AGENT_OUTPUT_SCHEMA_VERSION = "new-agent-output/v1"\n', encoding="utf-8")

    result = _run_guard(tmp_path)

    assert result.returncode == 1
    assert "new active agent output schema version reference: _SCHEMA_VERSION" in result.stdout


def test_default_scan_includes_scripts(tmp_path: Path) -> None:
    _init_repo(tmp_path)
    _write_lines(tmp_path / "app" / "small.py", 1)
    _commit_all(tmp_path)
    _write_lines(tmp_path / "scripts" / "large.py", 3)

    result = _run_guard(tmp_path, "fail", "--python-file-lines", "2")

    assert result.returncode == 1
    assert "FAIL: scripts/large.py" in result.stdout
