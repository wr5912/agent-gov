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


def _write_text(path: Path, text: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(text, encoding="utf-8")


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


def _formatter_result_basemodel_source() -> str:
    return (
        "from pydantic import BaseModel\n\n"
        "class OutputFormatterResult:\n"
        "    pass\n\n"
        "def format() -> OutputFormatterResult[BaseModel]:\n"
        "    return OutputFormatterResult()\n"
    )


def _runner_basemodel_return_source() -> str:
    return "from pydantic import BaseModel\n\n" "async def run_profile_json() -> BaseModel:\n" "    return BaseModel()\n"


def _completion_raw_output_basemodel_source() -> str:
    return (
        "from pydantic import BaseModel\n"
        "from app.runtime.json_types import JsonObject\n\n"
        "def complete_batch_plan_job(self, job_id: str, raw_output: BaseModel | JsonObject) -> None:\n"
        "    return None\n"
    )


def _run_profile_alias_basemodel_source() -> str:
    return (
        "from typing import Awaitable, Callable\n"
        "from pydantic import BaseModel\n\n"
        "RunProfileJson = Callable[..., Awaitable[BaseModel]]\n"
    )


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


@pytest.mark.parametrize(
    ("rel_path", "source", "expected_message"),
    [
        (
            "app/runtime/output_formatter.py",
            _formatter_result_basemodel_source(),
            "new typed-output stage erasure: format:return:OutputFormatterResult[BaseModel]",
        ),
        (
            "app/runtime/agent_job_runner.py",
            _runner_basemodel_return_source(),
            "new typed-output stage erasure: run_profile_json:return:BaseModel",
        ),
        (
            "app/runtime/stores/feedback_batch_plan_store.py",
            _completion_raw_output_basemodel_source(),
            "new typed-output stage erasure: complete_batch_plan_job:arg:raw_output:BaseModel|JsonObject",
        ),
        (
            "app/services/agent_job_worker.py",
            _run_profile_alias_basemodel_source(),
            "new typed-output stage erasure: RunProfileJson:alias:BaseModel",
        ),
    ],
)
def test_new_typed_output_stage_erasure_fails(
    tmp_path: Path,
    rel_path: str,
    source: str,
    expected_message: str,
) -> None:
    _init_repo(tmp_path)
    _write_lines(tmp_path / "app" / "small.py", 1)
    _commit_all(tmp_path)
    path = tmp_path / rel_path
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(source, encoding="utf-8")

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


def test_new_static_openapi_snapshot_fails(tmp_path: Path) -> None:
    _init_repo(tmp_path)
    _write_lines(tmp_path / "app" / "small.py", 1)
    _commit_all(tmp_path)
    path = tmp_path / "docs" / "开放接口规范.json"
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text('{"openapi":"3.1.0"}\n', encoding="utf-8")

    result = _run_guard(tmp_path)

    assert result.returncode == 1
    assert "new active static OpenAPI snapshot: tracked OpenAPI JSON" in result.stdout


def test_new_agent_output_schema_version_doc_reference_fails(tmp_path: Path) -> None:
    _init_repo(tmp_path)
    _write_lines(tmp_path / "app" / "small.py", 1)
    _commit_all(tmp_path)
    path = tmp_path / "docs" / "plan.md"
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text("expected_schema_version: feedback-optimization-plan-output/v1\n", encoding="utf-8")

    result = _run_guard(tmp_path)

    assert result.returncode == 1
    assert "new active agent output schema version document reference: expected_schema_version" in result.stdout


def test_default_scan_includes_scripts(tmp_path: Path) -> None:
    _init_repo(tmp_path)
    _write_lines(tmp_path / "app" / "small.py", 1)
    _commit_all(tmp_path)
    _write_lines(tmp_path / "scripts" / "large.py", 3)

    result = _run_guard(tmp_path, "fail", "--python-file-lines", "2")

    assert result.returncode == 1
    assert "FAIL: scripts/large.py" in result.stdout


def test_new_active_docs_file_requires_docs_index(tmp_path: Path) -> None:
    _init_repo(tmp_path)
    _write_lines(tmp_path / "app" / "small.py", 1)
    _commit_all(tmp_path)
    _write_text(tmp_path / "docs" / "new-plan.md", "# New Plan\n")

    result = _run_guard(tmp_path)

    assert result.returncode == 1
    assert "FAIL: docs/README.md: docs index is required when adding active docs" in result.stdout


def test_new_active_docs_file_must_be_linked_from_docs_index(tmp_path: Path) -> None:
    _init_repo(tmp_path)
    _write_lines(tmp_path / "app" / "small.py", 1)
    _write_text(tmp_path / "docs" / "README.md", "# Docs\n")
    _commit_all(tmp_path)
    _write_text(tmp_path / "docs" / "new-plan.md", "# New Plan\n")

    result = _run_guard(tmp_path)

    assert result.returncode == 1
    assert "FAIL: docs/new-plan.md: new active docs file is not linked from docs/README.md" in result.stdout


def test_new_active_docs_file_linked_from_docs_index_passes(tmp_path: Path) -> None:
    _init_repo(tmp_path)
    _write_lines(tmp_path / "app" / "small.py", 1)
    _write_text(tmp_path / "docs" / "README.md", "# Docs\n\n- docs/new-plan.md\n")
    _commit_all(tmp_path)
    _write_text(tmp_path / "docs" / "new-plan.md", "# New Plan\n")

    result = _run_guard(tmp_path)

    assert result.returncode == 0
    assert "docs/new-plan.md" not in result.stdout


def test_new_archive_docs_file_requires_archive_index(tmp_path: Path) -> None:
    _init_repo(tmp_path)
    _write_lines(tmp_path / "app" / "small.py", 1)
    _write_text(tmp_path / "docs" / "README.md", "# Docs\n")
    _commit_all(tmp_path)
    _write_text(tmp_path / "docs" / "archive" / "old-plan.md", "# Old Plan\n")

    result = _run_guard(tmp_path)

    assert result.returncode == 1
    assert "FAIL: docs/archive/README.md: archive index is required when adding archived docs" in result.stdout


def test_new_archive_docs_file_listed_in_archive_index_passes(tmp_path: Path) -> None:
    _init_repo(tmp_path)
    _write_lines(tmp_path / "app" / "small.py", 1)
    _write_text(tmp_path / "docs" / "README.md", "# Docs\n")
    _write_text(
        tmp_path / "docs" / "archive" / "README.md",
        "| 原路径 | 归档路径 | 替代文档 | 归档日期 |\n"
        "| --- | --- | --- | --- |\n"
        "| docs/old-plan.md | docs/archive/old-plan.md | docs/new-plan.md | 2026-06-11 |\n",
    )
    _commit_all(tmp_path)
    _write_text(tmp_path / "docs" / "archive" / "old-plan.md", "# Old Plan\n")

    result = _run_guard(tmp_path)

    assert result.returncode == 0
    assert "docs/archive/old-plan.md" not in result.stdout


def test_docs_governance_skill_mirror_drift_fails(tmp_path: Path) -> None:
    _init_repo(tmp_path)
    _write_lines(tmp_path / "app" / "small.py", 1)
    codex_skill = "---\nname: \"docs-governance\"\ndescription: \"docs\"\n---\n\n# Docs\n\nKeep synced.\n"
    claude_skill = (
        "---\nname: \"docs-governance\"\ndescription: \"docs\"\n---\n\n# Docs\n\n"
        "> 本技能与 `.codex/skills/docs-governance/SKILL.md` 同源镜像，修改需两侧同步。\n\nKeep synced.\n"
    )
    _write_text(tmp_path / ".codex" / "skills" / "docs-governance" / "SKILL.md", codex_skill)
    _write_text(tmp_path / ".claude" / "skills" / "docs-governance" / "SKILL.md", claude_skill)
    _commit_all(tmp_path)
    _write_text(tmp_path / ".claude" / "skills" / "docs-governance" / "SKILL.md", f"{claude_skill}\nDrift.\n")

    result = _run_guard(tmp_path)

    assert result.returncode == 1
    assert "mirrored skill differs from .claude/skills/docs-governance/SKILL.md" in result.stdout


def test_new_project_skill_pair_is_discovered_for_mirror_drift(tmp_path: Path) -> None:
    _init_repo(tmp_path)
    _write_lines(tmp_path / "app" / "small.py", 1)
    codex_skill = '---\nname: "new-project-governance"\ndescription: "docs"\n---\n\n# Skill\n\nKeep synced.\n'
    claude_skill = (
        '---\nname: "new-project-governance"\ndescription: "docs"\n---\n\n# Skill\n\n'
        "> 本技能与 `.codex/skills/new-project-governance/SKILL.md` 同源镜像，修改需两侧同步。\n\nKeep synced.\n"
    )
    _write_text(tmp_path / ".codex" / "skills" / "new-project-governance" / "SKILL.md", codex_skill)
    _write_text(tmp_path / ".claude" / "skills" / "new-project-governance" / "SKILL.md", claude_skill)
    _commit_all(tmp_path)
    _write_text(tmp_path / ".claude" / "skills" / "new-project-governance" / "SKILL.md", f"{claude_skill}\nDrift.\n")

    result = _run_guard(tmp_path)

    assert result.returncode == 1
    assert "mirrored skill differs from .claude/skills/new-project-governance/SKILL.md" in result.stdout


def test_new_project_skill_missing_mirror_fails(tmp_path: Path) -> None:
    _init_repo(tmp_path)
    _write_lines(tmp_path / "app" / "small.py", 1)
    _commit_all(tmp_path)
    _write_text(
        tmp_path / ".codex" / "skills" / "new-project-governance" / "SKILL.md",
        '---\nname: "new-project-governance"\ndescription: "docs"\n---\n\n# Skill\n\nKeep synced.\n',
    )

    result = _run_guard(tmp_path)

    assert result.returncode == 1
    assert (
        "mirrored skill pair is incomplete: missing .claude/skills/new-project-governance/SKILL.md"
        in result.stdout
    )


def test_skill_mirror_exclusions_are_not_forced_to_match(tmp_path: Path) -> None:
    _init_repo(tmp_path)
    _write_lines(tmp_path / "app" / "small.py", 1)
    _write_text(
        tmp_path / ".codex" / "skills" / "codex-config-optimizer" / "SKILL.md",
        '---\nname: "codex-config-optimizer"\ndescription: "codex"\n---\n\n# Codex Only\n\nCodex side only.\n',
    )
    _write_text(
        tmp_path / ".codex" / "skills" / "project-skill" / "SKILL.md",
        '---\nname: "project-skill"\ndescription: "project"\n---\n\n# Codex Project\n\nCodex shape.\n',
    )
    _write_text(
        tmp_path / ".claude" / "skills" / "project-skill" / "SKILL.md",
        '---\ndescription: "project"\n---\n\n# Claude Project\n\nClaude shape.\n',
    )
    _commit_all(tmp_path)

    result = _run_guard(tmp_path)

    assert result.returncode == 0
    assert "mirrored skill" not in result.stdout


def test_new_docs_file_with_unfinished_marker_fails(tmp_path: Path) -> None:
    _init_repo(tmp_path)
    _write_lines(tmp_path / "app" / "small.py", 1)
    _write_text(tmp_path / "docs" / "README.md", "# Docs\n\n- docs/new-plan.md\n")
    _commit_all(tmp_path)
    _write_text(tmp_path / "docs" / "new-plan.md", "# New Plan\n\nTODO: fill this later.\n")

    result = _run_guard(tmp_path)

    assert result.returncode == 1
    assert "FAIL: docs/new-plan.md: unfinished marker `TODO` at line 3" in result.stdout


def test_new_docs_file_with_cjk_unfinished_marker_fails(tmp_path: Path) -> None:
    _init_repo(tmp_path)
    _write_lines(tmp_path / "app" / "small.py", 1)
    _write_text(tmp_path / "docs" / "README.md", "# Docs\n\n- docs/new-plan.md\n")
    _commit_all(tmp_path)
    _write_text(tmp_path / "docs" / "new-plan.md", "# 新方案\n\n细节待补充。\n")

    result = _run_guard(tmp_path)

    assert result.returncode == 1
    assert "FAIL: docs/new-plan.md: unfinished marker `待补充` at line 3" in result.stdout


def test_docs_file_with_test_path_is_not_flagged_as_marker(tmp_path: Path) -> None:
    _init_repo(tmp_path)
    _write_lines(tmp_path / "app" / "small.py", 1)
    _write_text(tmp_path / "docs" / "README.md", "# Docs\n\n- docs/guide.md\n")
    _commit_all(tmp_path)
    _write_text(tmp_path / "docs" / "guide.md", "# Guide\n\n运行 tests/test_xxx.py::test_xxx 验证。\n")

    result = _run_guard(tmp_path)

    assert result.returncode == 0
    assert "unfinished marker" not in result.stdout


def test_runtime_env_governance_skill_mirror_drift_fails(tmp_path: Path) -> None:
    _init_repo(tmp_path)
    _write_lines(tmp_path / "app" / "small.py", 1)
    codex_skill = '---\nname: "runtime-env-governance"\ndescription: "runtime"\n---\n\n# Runtime\n\nKeep synced.\n'
    claude_skill = (
        '---\nname: "runtime-env-governance"\ndescription: "runtime"\n---\n\n# Runtime\n\n'
        "> 本技能与 `.codex/skills/runtime-env-governance/SKILL.md` 同源镜像，修改需两侧同步。\n\nKeep synced.\n"
    )
    _write_text(tmp_path / ".codex" / "skills" / "runtime-env-governance" / "SKILL.md", codex_skill)
    _write_text(tmp_path / ".claude" / "skills" / "runtime-env-governance" / "SKILL.md", claude_skill)
    _commit_all(tmp_path)
    _write_text(tmp_path / ".claude" / "skills" / "runtime-env-governance" / "SKILL.md", f"{claude_skill}\nDrift.\n")

    result = _run_guard(tmp_path)

    assert result.returncode == 1
    assert "mirrored skill differs from .claude/skills/runtime-env-governance/SKILL.md" in result.stdout


def test_orphan_test_importing_deleted_symbol_fails(tmp_path: Path) -> None:
    _init_repo(tmp_path)
    _write_lines(tmp_path / "app" / "small.py", 1)
    _write_text(tmp_path / "app" / "mod.py", "def kept():\n    return 1\n")
    _write_text(tmp_path / "tests" / "test_mod.py", "from app.mod import gone\n")
    _commit_all(tmp_path)

    result = _run_guard(tmp_path)

    assert result.returncode == 1
    assert "FAIL: tests/test_mod.py: imports `gone` not defined in `app.mod`" in result.stdout


def test_orphan_test_importing_missing_module_fails(tmp_path: Path) -> None:
    _init_repo(tmp_path)
    _write_lines(tmp_path / "app" / "small.py", 1)
    _write_text(tmp_path / "tests" / "test_removed.py", "import scripts.removed\n")
    _commit_all(tmp_path)

    result = _run_guard(tmp_path)

    assert result.returncode == 1
    assert "FAIL: tests/test_removed.py: imports missing module `scripts.removed`" in result.stdout


def test_orphan_check_passes_for_valid_import(tmp_path: Path) -> None:
    _init_repo(tmp_path)
    _write_lines(tmp_path / "app" / "small.py", 1)
    _write_text(tmp_path / "app" / "mod.py", "def kept():\n    return 1\n")
    _write_text(tmp_path / "tests" / "test_mod.py", "from app.mod import kept\n")
    _commit_all(tmp_path)

    result = _run_guard(tmp_path)

    assert result.returncode == 0
    assert "not defined in" not in result.stdout
    assert "imports missing module" not in result.stdout


def test_test_sync_governance_skill_mirror_drift_fails(tmp_path: Path) -> None:
    _init_repo(tmp_path)
    _write_lines(tmp_path / "app" / "small.py", 1)
    codex_skill = '---\nname: "test-sync-governance"\ndescription: "tests"\n---\n\n# Tests\n\nKeep synced.\n'
    claude_skill = (
        '---\nname: "test-sync-governance"\ndescription: "tests"\n---\n\n# Tests\n\n'
        "> 本技能与 `.codex/skills/test-sync-governance/SKILL.md` 同源镜像，修改需两侧同步。\n\nKeep synced.\n"
    )
    _write_text(tmp_path / ".codex" / "skills" / "test-sync-governance" / "SKILL.md", codex_skill)
    _write_text(tmp_path / ".claude" / "skills" / "test-sync-governance" / "SKILL.md", claude_skill)
    _commit_all(tmp_path)
    _write_text(tmp_path / ".claude" / "skills" / "test-sync-governance" / "SKILL.md", f"{claude_skill}\nDrift.\n")

    result = _run_guard(tmp_path)

    assert result.returncode == 1
    assert "mirrored skill differs from .claude/skills/test-sync-governance/SKILL.md" in result.stdout


def test_agentgov_closeout_sync_skill_mirror_drift_fails(tmp_path: Path) -> None:
    _init_repo(tmp_path)
    _write_lines(tmp_path / "app" / "small.py", 1)
    codex_skill = '---\nname: "agentgov-closeout-sync"\ndescription: "closeout"\n---\n\n# Closeout\n\nKeep synced.\n'
    claude_skill = (
        '---\nname: "agentgov-closeout-sync"\ndescription: "closeout"\n---\n\n# Closeout\n\n'
        "> 本技能与 `.codex/skills/agentgov-closeout-sync/SKILL.md` 同源镜像，修改需两侧同步。\n\nKeep synced.\n"
    )
    _write_text(tmp_path / ".codex" / "skills" / "agentgov-closeout-sync" / "SKILL.md", codex_skill)
    _write_text(tmp_path / ".claude" / "skills" / "agentgov-closeout-sync" / "SKILL.md", claude_skill)
    _commit_all(tmp_path)
    _write_text(tmp_path / ".claude" / "skills" / "agentgov-closeout-sync" / "SKILL.md", f"{claude_skill}\nDrift.\n")

    result = _run_guard(tmp_path)

    assert result.returncode == 1
    assert "mirrored skill differs from .claude/skills/agentgov-closeout-sync/SKILL.md" in result.stdout
