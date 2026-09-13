from __future__ import annotations

import ast
import json
import subprocess
from pathlib import Path

import pytest
from scripts.check_no_test_doubles import (
    JAVASCRIPT_RULES,
    TEST_ONLY_JAVASCRIPT_RULES,
    PythonDoubleVisitor,
    expand_local_import_closure,
    inspect_make_targets,
    load_formal_acceptance_scope,
    scan_make_targets,
    scan_paths,
    scan_shell,
    validate_formal_target_allowlist,
)


def _rules(source: str) -> set[str]:
    tree = ast.parse(source)
    local_functions = frozenset(node.name for node in ast.walk(tree) if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)))
    visitor = PythonDoubleVisitor(Path("scripts/formal_acceptance.py"), local_functions=local_functions)
    visitor.visit(tree)
    return {finding.rule for finding in visitor.findings}


def test_policy_rejects_in_process_asgi_transport() -> None:
    source = "\n".join(
        (
            "import httpx",
            "from fastapi import FastAPI",
            "app = FastAPI()",
            "transport = httpx.ASGITransport(app=app)",
            "client = httpx.AsyncClient(transport=transport)",
        ),
    )
    assert _rules(source) == {"in-process HTTP test client ASGITransport"}


def test_policy_rejects_aliased_httpx_and_framework_test_clients() -> None:
    source = "\n".join(
        (
            "from httpx import ASGITransport as LocalTransport",
            "from fastapi.testclient import TestClient as FastApiClient",
            "from starlette.testclient import TestClient as StarletteClient",
        ),
    )

    assert _rules(source) == {
        "in-process HTTP test client import fastapi.testclient.TestClient",
        "in-process HTTP test client import httpx.ASGITransport",
        "in-process HTTP test client import starlette.testclient.TestClient",
    }


def test_policy_rejects_python_double_and_test_hook_symbols() -> None:
    double_name = "monkey" + "patch"
    response_call = "httpx." + "Response(200)"
    source = (
        f"def test_case({double_name}):\n"
        "    class _FakeRuntime: pass\n"
        "    def _stub_call(): pass\n"
        "    async def fake(): pass\n"
        f"    {response_call}\n"
        "    create_runtime_app(control_transport=value)\n"
        "    ImprovementGovernorService(format_normalized_feedback=formatter)\n"
    )
    rules = _rules(source)
    assert "pytest monkeypatch fixture" in rules
    assert "fabricated httpx.Response" in rules
    assert "test-only production hook control_transport" in rules
    assert "test-only production hook format_normalized_feedback" in rules
    assert "named test-double class _FakeRuntime" in rules
    assert "named test-double function _stub_call" in rules
    assert "named test-double function fake" in rules


def test_policy_accepts_named_helper_class_that_is_not_a_test_double() -> None:
    visitor = PythonDoubleVisitor(Path("scripts/formal_acceptance.py"))
    visitor.visit(ast.parse("class _RuntimeClient: pass\n"))
    assert visitor.findings == set()


def test_policy_rejects_test_defined_http_route_and_registration() -> None:
    visitor = PythonDoubleVisitor(Path("scripts/formal_acceptance.py"))
    visitor.visit(
        ast.parse(
            "@app.post('/internal/runtime-receipts')\nasync def receipt(): pass\napp.add_api_route('/api/runs', receipt, methods=['POST'])\n",
        ),
    )

    assert {finding.rule for finding in visitor.findings} == {
        "acceptance-defined HTTP route replaces a production API boundary",
    }


def test_policy_rejects_simple_namespace_as_a_fabricated_production_object() -> None:
    rules = _rules("from types import SimpleNamespace\nagent = SimpleNamespace(state='ready')\n")
    assert "forbidden test-double symbol SimpleNamespace" in rules


def test_policy_rejects_aliased_unittest_mock_import() -> None:
    assert "test-double module import unittest.mock" in _rules("from unittest import mock as replacement\n")


def test_policy_rejects_http_test_double_libraries() -> None:
    assert _rules("import respx\nfrom responses import RequestsMock\n") == {
        "test-double module import responses",
        "test-double module import respx",
    }


def test_policy_rejects_browser_transport_replacement_and_injected_status_reader() -> None:
    source = "\n".join(
        (
            "globalThis.fetch = replacement;",
            "const response = new Response('{}');",
            "const options = { getStatus: async (signal) => ({ status: 'idle' }) };",
            "await browserPage.route('**/api/**', handler);",
        ),
    )
    rules = {rule for line in source.splitlines() for pattern, rule in JAVASCRIPT_RULES + TEST_ONLY_JAVASCRIPT_RULES if pattern.search(line)}
    assert rules == {
        "replaced browser fetch transport",
        "fabricated browser transport response",
        "injected JavaScript collaborator",
        "browser request interception",
    }


def test_policy_rejects_fabricated_browser_document_and_har_replay() -> None:
    source = "\n".join(
        (
            "await page.setContent('<main>synthetic</main>');",
            "await context.addInitScript(() => installSyntheticApi());",
            "await page.routeFromHAR('recorded.har');",
            "document.body.innerHTML = '<button>synthetic</button>';",
            "const nock = require('nock');",
        ),
    )
    rules = {rule for line in source.splitlines() for pattern, rule in JAVASCRIPT_RULES + TEST_ONLY_JAVASCRIPT_RULES if pattern.search(line)}
    assert rules == {
        "browser HAR replay",
        "fabricated browser DOM",
        "fabricated browser page state",
        "JavaScript test-double library",
    }


def test_policy_rejects_local_production_collaborators_but_accepts_bound_services() -> None:
    source = "\n".join(
        (
            "def resolve_agent(agent_id): return True",
            "service = Service(agent_exists=resolve_agent, store_for=lambda agent_id: None)",
            "service.agent_status = resolve_agent",
            "service.agent_version_provider = governance.current_agent_version_id",
            "runner = Runner(store_for=governance._store_for)",
        ),
    )

    assert _rules(source) == {
        "injected production collaborator agent_exists",
        "injected production collaborator agent_status",
        "injected production collaborator store_for",
    }


def test_policy_accepts_pure_algorithm_lambdas() -> None:
    source = "\n".join(
        (
            "ordered = sorted(records, key=lambda item: item.name)",
            "mapped = map(lambda value: value.strip(), values)",
        ),
    )
    assert _rules(source) == set()


def test_policy_rejects_replaced_production_methods_and_trace_provider() -> None:
    source = "\n".join(
        (
            "runner.enqueue = replacement",
            "store.set_langfuse_trace_fetcher(provider.fetch_trace)",
        ),
    )
    assert _rules(source) == {
        "replaced production method enqueue",
        "replaced production trace provider",
    }


def test_policy_rejects_noop_make_and_child_commands() -> None:
    noop_make = "COMPOSE" + "=:"
    noop_child = "tr" + "ue"
    source = "subprocess.run(['make', '-n', 'up', '" + noop_make + "'])\nsubprocess.run(['runner', '--', '" + noop_child + "'])\n"
    assert _rules(source) == {
        "protected Make command assignment",
        "no-op child command",
    }


def test_policy_rejects_all_acceptance_make_control_overrides() -> None:
    source = (
        "subprocess.run(['make', 'container-live-test', 'CONTAINER_ACCEPTANCE=/bin/true'])\n"
        "subprocess.run(['make', 'container-live-test', 'REQUIRE_CONTAINER_ACCEPTANCE=true'])\n"
        "subprocess.run(['make', 'container-live-test', 'MAKE=/tmp/wrapper'])\n"
    )

    assert _rules(source) == {"protected Make command assignment"}


def test_policy_scope_ignores_unit_fault_injection_and_selects_formal_live_assets(tmp_path: Path) -> None:
    scripts = tmp_path / "scripts"
    tests = tmp_path / "tests"
    scripts.mkdir()
    tests.mkdir()
    formal = scripts / "formal_live.py"
    unit = tests / "test_fault_injection.py"
    formal.write_text("result = subprocess.run(['python', '--version'])\n", encoding="utf-8")
    unit.write_text("from unittest.mock import Mock\n", encoding="utf-8")
    policy = tmp_path / "quality_policy.json"
    policy.write_text(
        json.dumps(
            {
                "test_evidence": {
                    "formal_live_selectors": ["scripts/formal_live.py"],
                    "formal_live_targets": ["formal-live"],
                }
            }
        ),
        encoding="utf-8",
    )

    scope = load_formal_acceptance_scope(policy, repo_root=tmp_path)

    assert scope.files == (formal.resolve(),)
    assert scan_paths(scope.files, repo_root=tmp_path) == ()
    assert {item.rule for item in scan_paths((unit.resolve(),), repo_root=tmp_path)} == {
        "test-double module import unittest.mock",
    }


def test_policy_scans_python_and_javascript_local_import_closure(tmp_path: Path) -> None:
    scripts = tmp_path / "scripts"
    helpers = scripts / "helpers"
    helpers.mkdir(parents=True)
    scripts.joinpath("__init__.py").write_text("", encoding="utf-8")
    helpers.joinpath("__init__.py").write_text("", encoding="utf-8")
    python_root = scripts / "formal.py"
    python_helper = helpers / "transport.py"
    javascript_root = scripts / "formal.mjs"
    javascript_helper = helpers / "transport.mjs"
    unrelated = helpers / "unrelated.py"
    python_root.write_text("from scripts.helpers import transport\n", encoding="utf-8")
    python_helper.write_text("from unittest.mock import Mock\n", encoding="utf-8")
    javascript_root.write_text('import "./helpers/transport.mjs";\n', encoding="utf-8")
    javascript_helper.write_text("globalThis.fetch = replacement;\n", encoding="utf-8")
    unrelated.write_text("from unittest.mock import MagicMock\n", encoding="utf-8")

    closure = expand_local_import_closure(
        (python_root.resolve(), javascript_root.resolve()),
        repo_root=tmp_path,
    )

    assert python_helper.resolve() in closure
    assert javascript_helper.resolve() in closure
    assert unrelated.resolve() not in closure
    assert {item.rule for item in scan_paths(closure, repo_root=tmp_path)} == {
        "test-double module import unittest.mock",
        "replaced browser fetch transport",
    }


def test_policy_resolves_script_directory_and_package_source_roots(tmp_path: Path) -> None:
    scripts = tmp_path / "scripts"
    script_package = scripts / "test_quality"
    package = tmp_path / "packages" / "testkit" / "src" / "agentgov_testkit"
    script_package.mkdir(parents=True)
    package.mkdir(parents=True)
    entrypoint = scripts / "formal.py"
    policy_helper = script_package / "policy.py"
    package_helper = package / "transport.py"
    entrypoint.write_text(
        "from test_quality import policy\nfrom agentgov_testkit import transport\n",
        encoding="utf-8",
    )
    script_package.joinpath("__init__.py").write_text("", encoding="utf-8")
    package.joinpath("__init__.py").write_text("", encoding="utf-8")
    policy_helper.write_text("from unittest.mock import Mock\n", encoding="utf-8")
    package_helper.write_text("from unittest.mock import MagicMock\n", encoding="utf-8")

    closure = expand_local_import_closure((entrypoint.resolve(),), repo_root=tmp_path)

    assert policy_helper.resolve() in closure
    assert package_helper.resolve() in closure
    assert {item.path for item in scan_paths(closure, repo_root=tmp_path)} == {
        "scripts/test_quality/policy.py",
        "packages/testkit/src/agentgov_testkit/transport.py",
    }


def test_policy_scans_only_selected_formal_make_targets(tmp_path: Path) -> None:
    makefile = tmp_path / "Makefile"
    makefile.write_text(
        "formal-live:\n\trunner -- true\nunit-test:\n\tPYTHON_RUN=: pytest -q\n",
        encoding="utf-8",
    )

    findings = scan_make_targets(makefile, ("formal-live",), repo_root=tmp_path)

    assert [(item.path, item.rule) for item in findings] == [
        ("Makefile#formal-live", "no-op child command"),
    ]


def test_policy_scans_formal_make_prerequisite_closure(tmp_path: Path) -> None:
    makefile = tmp_path / "Makefile"
    makefile.write_text(
        "formal-live: neutral-helper\n\tpython scripts/formal.py\nneutral-helper:\n\ttrue\nunrelated:\n\tPYTHON_RUN=: pytest -q\n",
        encoding="utf-8",
    )
    scripts = tmp_path / "scripts"
    scripts.mkdir()
    scripts.joinpath("formal.py").write_text("print('real process')\n", encoding="utf-8")

    findings = scan_make_targets(makefile, ("formal-live",), repo_root=tmp_path)

    assert [(item.path, item.rule) for item in findings] == [
        ("Makefile#neutral-helper", "no-op target recipe"),
    ]


def test_policy_scans_targets_reached_by_recursive_make(tmp_path: Path) -> None:
    makefile = tmp_path / "Makefile"
    makefile.write_text(
        "formal-live:\n\t$(MAKE) --no-print-directory concealed-helper\nconcealed-helper:\n\trunner -- true\n",
        encoding="utf-8",
    )

    findings = scan_make_targets(makefile, ("formal-live",), repo_root=tmp_path)

    assert [(item.path, item.rule) for item in findings] == [
        ("Makefile#concealed-helper", "no-op child command"),
    ]


def test_policy_adds_make_invoked_sources_to_formal_import_closure(tmp_path: Path) -> None:
    scripts = tmp_path / "scripts"
    scripts.mkdir()
    scripts.joinpath("entrypoint.py").write_text("from scripts import hidden_transport\n", encoding="utf-8")
    scripts.joinpath("hidden_transport.py").write_text("from unittest.mock import Mock\n", encoding="utf-8")
    makefile = tmp_path / "Makefile"
    makefile.write_text(
        "RUN_FORMAL = python scripts/entrypoint.py\nformal-live: helper\nhelper:\n\t$(RUN_FORMAL)\n",
        encoding="utf-8",
    )

    inspection = inspect_make_targets(makefile, ("formal-live",), repo_root=tmp_path)
    closure = expand_local_import_closure(inspection.files, repo_root=tmp_path)

    assert inspection.findings == ()
    assert scripts.joinpath("entrypoint.py").resolve() in closure
    assert scripts.joinpath("hidden_transport.py").resolve() in closure
    assert {finding.rule for finding in scan_paths(closure, repo_root=tmp_path)} == {
        "test-double module import unittest.mock",
    }


def test_policy_resolves_make_invoked_package_script_source(tmp_path: Path) -> None:
    frontend = tmp_path / "frontend"
    scripts = tmp_path / "scripts"
    frontend.mkdir()
    scripts.mkdir()
    frontend.joinpath("package.json").write_text(
        json.dumps({"scripts": {"verify:formal": "cd .. && node scripts/formal.mjs"}}),
        encoding="utf-8",
    )
    scripts.joinpath("formal.mjs").write_text("globalThis.fetch = replacement;\n", encoding="utf-8")
    makefile = tmp_path / "Makefile"
    makefile.write_text(
        "formal-live:\n\tpnpm --dir frontend run verify:formal\n",
        encoding="utf-8",
    )

    inspection = inspect_make_targets(makefile, ("formal-live",), repo_root=tmp_path)

    assert inspection.findings == ()
    assert inspection.files == (scripts.joinpath("formal.mjs").resolve(),)
    assert {finding.rule for finding in scan_paths(inspection.files, repo_root=tmp_path)} == {
        "replaced browser fetch transport",
    }


def test_policy_fails_closed_for_dynamic_make_script_path(tmp_path: Path) -> None:
    makefile = tmp_path / "Makefile"
    makefile.write_text('formal-live:\n\tpython "$$FORMAL_ENTRYPOINT"\n', encoding="utf-8")

    findings = scan_make_targets(makefile, ("formal-live",), repo_root=tmp_path)

    assert [(item.path, item.rule) for item in findings] == [
        ("Makefile#formal-live", "dynamic Python/Node script path is not auditable"),
    ]


def test_policy_fails_closed_when_formal_selector_or_target_is_missing(tmp_path: Path) -> None:
    policy = tmp_path / "quality_policy.json"
    policy.write_text(
        json.dumps(
            {
                "test_evidence": {
                    "formal_live_selectors": ["scripts/missing.py"],
                    "formal_live_targets": ["missing-live"],
                }
            }
        ),
        encoding="utf-8",
    )

    try:
        load_formal_acceptance_scope(policy, repo_root=tmp_path)
    except ValueError as exc:
        assert "matches no assets" in str(exc)
    else:
        raise AssertionError("missing formal acceptance selector must fail closed")

    makefile = tmp_path / "Makefile"
    makefile.write_text("present:\n\tpython -V\n", encoding="utf-8")
    assert scan_make_targets(makefile, ("missing-live",), repo_root=tmp_path)[0].rule == "formal live Make target is missing"


def test_policy_fails_closed_for_shell_reached_from_make_closure(tmp_path: Path) -> None:
    scripts = tmp_path / "scripts"
    scripts.mkdir()
    shell = scripts / "formal.sh"
    shell.write_text("#!/bin/sh\ncurl http://127.0.0.1/health\n", encoding="utf-8")
    makefile = tmp_path / "Makefile"
    makefile.write_text("formal-live:\n\tbash scripts/formal.sh\n", encoding="utf-8")

    inspection = inspect_make_targets(makefile, ("formal-live",), repo_root=tmp_path)

    assert inspection.findings == ()
    assert inspection.files == (shell.resolve(),)
    assert scan_shell(shell, repo_root=tmp_path)[0].rule == "formal shell source is not statically auditable"
    assert scan_paths(inspection.files, repo_root=tmp_path) == scan_shell(shell, repo_root=tmp_path)


def test_policy_rejects_formal_target_allowlist_drift() -> None:
    with pytest.raises(ValueError, match="missing=.*unexpected=rogue-live"):
        validate_formal_target_allowlist(("rogue-live",))


@pytest.mark.parametrize(
    ("make_source", "expected_rule"),
    [
        ("include scripts/override.mk\nformal-live:\n\tpython scripts/formal.py\n", "Make include directive is not statically auditable"),
        ("override define ACTION\ntrue\nendef\nformal-live:\n\t$(ACTION)\n", "Make define directive is not statically auditable"),
        ("SIDE := $(eval FORMAL := true)\nformal-live:\n\techo live\n", "dynamic Make eval/call is not statically auditable"),
        ("SIDE := $(call dispatch,true)\nformal-live:\n\techo live\n", "dynamic Make eval/call is not statically auditable"),
        ("SIDE := $(file >accepted,yes)\nformal-live:\n\techo live\n", "dynamic Make file function is not statically auditable"),
        ("SIDE := $(shell /bin/true)\nformal-live:\n\techo live\n", "unaudited Make shell function"),
    ],
)
def test_policy_fails_closed_for_dynamic_make_syntax(
    tmp_path: Path,
    make_source: str,
    expected_rule: str,
) -> None:
    makefile = tmp_path / "Makefile"
    makefile.write_text(make_source, encoding="utf-8")

    assert expected_rule in {item.rule for item in scan_make_targets(makefile, ("formal-live",), repo_root=tmp_path)}


def test_policy_rejects_make_recipe_override_that_real_make_would_execute(tmp_path: Path) -> None:
    scripts = tmp_path / "scripts"
    scripts.mkdir()
    scripts.joinpath("formal.py").write_text("print('REAL_CHECK')\n", encoding="utf-8")
    makefile = tmp_path / "Makefile"
    makefile.write_text(
        "formal-live:\n\t@python scripts/formal.py\nformal-live:\n\t@echo ACCEPTED_WITHOUT_CHECK\n",
        encoding="utf-8",
    )

    executed = subprocess.run(
        ["/usr/bin/make", "--no-print-directory", "formal-live"],
        cwd=tmp_path,
        check=False,
        capture_output=True,
        text=True,
    )
    findings = scan_make_targets(makefile, ("formal-live",), repo_root=tmp_path)

    assert executed.returncode == 0
    assert executed.stdout.strip() == "ACCEPTED_WITHOUT_CHECK"
    assert "formal Make target has duplicate or overriding rule definitions" in {item.rule for item in findings}


def test_policy_rejects_dynamic_python_and_javascript_loading_of_omitted_double(tmp_path: Path) -> None:
    scripts = tmp_path / "scripts"
    scripts.mkdir()
    omitted = scripts / "omitted_fake.py"
    omitted.write_text("from unittest.mock import Mock\n", encoding="utf-8")
    python_entrypoint = scripts / "formal.py"
    python_entrypoint.write_text(
        "import importlib\nmodule_name = 'scripts.omitted_' + 'fake'\nimportlib.import_module(module_name)\n",
        encoding="utf-8",
    )
    javascript_entrypoint = scripts / "formal.mjs"
    javascript_entrypoint.write_text("const name = './omitted-' + 'fake.mjs';\nawait import(name);\n", encoding="utf-8")

    closure = expand_local_import_closure(
        (python_entrypoint.resolve(), javascript_entrypoint.resolve()),
        repo_root=tmp_path,
    )
    rules = {item.rule for item in scan_paths(closure, repo_root=tmp_path)}

    assert omitted.resolve() not in closure
    assert "dynamic Python module loading import_module is not auditable" in rules
    assert "dynamic JavaScript import is not auditable" in rules


@pytest.mark.parametrize(
    ("python_source", "expected_rule"),
    [
        (
            "from importlib import import_module as load\nload('scripts.hidden')\n",
            "dynamic Python module loading import_module is not auditable",
        ),
        (
            "loader = __import__\nloader('scripts.hidden')\n",
            "dynamic Python symbol __import__ is not auditable",
        ),
        (
            "runner = getattr(__builtins__, 'ex' + 'ec')\nrunner(open('scripts/hidden.py').read())\n",
            "computed Python loader access is not auditable",
        ),
    ],
)
def test_policy_rejects_aliased_python_loader_bypasses(
    tmp_path: Path,
    python_source: str,
    expected_rule: str,
) -> None:
    scripts = tmp_path / "scripts"
    scripts.mkdir()
    entrypoint = scripts / "formal.py"
    hidden = scripts / "hidden.py"
    entrypoint.write_text(python_source, encoding="utf-8")
    hidden.write_text("from unittest.mock import Mock\n", encoding="utf-8")

    closure = expand_local_import_closure((entrypoint.resolve(),), repo_root=tmp_path)
    rules = {item.rule for item in scan_paths(closure, repo_root=tmp_path)}

    assert hidden.resolve() not in closure
    assert expected_rule in rules


def test_policy_rejects_aliased_and_computed_javascript_loaders(tmp_path: Path) -> None:
    scripts = tmp_path / "scripts"
    scripts.mkdir()
    entrypoint = scripts / "formal.cjs"
    hidden = scripts / "hidden.cjs"
    entrypoint.write_text(
        "const load = require;\nload('./hidden.cjs');\nconst indirect = module['require'];\n",
        encoding="utf-8",
    )
    hidden.write_text("const HiddenMock = class {};\n", encoding="utf-8")

    closure = expand_local_import_closure((entrypoint.resolve(),), repo_root=tmp_path)
    rules = {item.rule for item in scan_paths(closure, repo_root=tmp_path)}

    assert hidden.resolve() not in closure
    assert "aliased JavaScript loader or evaluator is not auditable" in rules
    assert "computed JavaScript loader or evaluator access is not auditable" in rules


@pytest.mark.parametrize(
    "runner_definition",
    [
        "CONTAINER_ACCEPTANCE := /bin/true # python scripts/run_container_acceptance.py",
        "CONTAINER_ACCEPTANCE := /bin/true python scripts/run_container_acceptance.py",
    ],
)
def test_policy_rejects_spoofed_public_acceptance_runner_that_real_make_accepts(
    tmp_path: Path,
    runner_definition: str,
) -> None:
    scripts = tmp_path / "scripts"
    scripts.mkdir()
    for name in (
        "run_container_acceptance.py",
        "verify_container_acceptance_context.py",
        "diagnose_runtime_health.py",
    ):
        scripts.joinpath(name).write_text("raise SystemExit('MUST_NOT_RUN')\n", encoding="utf-8")
    makefile = tmp_path / "Makefile"
    makefile.write_text(
        f"{runner_definition}\nsmoke:\n\t@$(CONTAINER_ACCEPTANCE) scripts/verify_container_acceptance_context.py scripts/diagnose_runtime_health.py\n",
        encoding="utf-8",
    )

    executed = subprocess.run(
        ["/usr/bin/make", "--no-print-directory", "smoke"],
        cwd=tmp_path,
        check=False,
        capture_output=True,
        text=True,
    )
    findings = scan_make_targets(makefile, ("smoke",), repo_root=tmp_path)
    rules = {item.rule for item in findings}

    assert executed.returncode == 0
    assert "MUST_NOT_RUN" not in executed.stderr
    assert "formal Make binding CONTAINER_ACCEPTANCE does not match the canonical command contract" in rules
    assert "public formal Make target does not use the canonical acceptance dispatch" in rules


@pytest.mark.parametrize("recipe", ["@/usr/bin/true", "@/bin/true", "@exit 0"])
def test_policy_rejects_standalone_noop_recipes(tmp_path: Path, recipe: str) -> None:
    makefile = tmp_path / "Makefile"
    makefile.write_text(f"formal-live:\n\t{recipe}\n", encoding="utf-8")

    rules = {item.rule for item in scan_make_targets(makefile, ("formal-live",), repo_root=tmp_path)}

    assert "no-op target recipe" in rules


@pytest.mark.parametrize(
    "replacement",
    [
        "smoke:\n\t@touch /tmp/agentgov-formal-bypass\n\t$(CONTAINER_ACCEPTANCE)",
        "smoke:\n\t$(CONTAINER_ACCEPTANCE)",
    ],
)
def test_policy_rejects_extra_public_recipe_before_or_after_dispatch(
    tmp_path: Path,
    replacement: str,
) -> None:
    source_root = Path(__file__).resolve().parents[1]
    make_source = source_root.joinpath("Makefile").read_text(encoding="utf-8")
    canonical = "\nsmoke:\n\t$(CONTAINER_ACCEPTANCE)"
    if replacement == "smoke:\n\t$(CONTAINER_ACCEPTANCE)":
        replacement += " --profile core -- $(MAKE) --no-print-directory _smoke\n\t@touch /tmp/agentgov-formal-bypass"
    else:
        replacement += " --profile core -- $(MAKE) --no-print-directory _smoke"
    replacement = "\n" + replacement
    makefile = tmp_path / "Makefile"
    makefile.write_text(make_source.replace(canonical, replacement, 1), encoding="utf-8")
    scripts = tmp_path / "scripts"
    scripts.mkdir()
    for name in (
        "run_container_acceptance.py",
        "verify_container_acceptance_context.py",
        "diagnose_runtime_health.py",
    ):
        scripts.joinpath(name).write_bytes(source_root.joinpath("scripts", name).read_bytes())

    rules = {item.rule for item in scan_make_targets(makefile, ("smoke",), repo_root=tmp_path)}

    assert "public formal Make target does not use the canonical acceptance dispatch" in rules


def test_policy_rejects_extra_private_recipe_outside_action_manifest(tmp_path: Path) -> None:
    source_root = Path(__file__).resolve().parents[1]
    make_source = source_root.joinpath("Makefile").read_text(encoding="utf-8")
    makefile = tmp_path / "Makefile"
    makefile.write_text(
        make_source.replace(
            "_smoke:\n\t@$(REQUIRE_CONTAINER_ACCEPTANCE)",
            "_smoke:\n\t@$(REQUIRE_CONTAINER_ACCEPTANCE)\n\t@touch /tmp/agentgov-formal-bypass",
            1,
        ),
        encoding="utf-8",
    )
    scripts = tmp_path / "scripts"
    scripts.mkdir()
    for name in ("verify_container_acceptance_context.py", "diagnose_runtime_health.py"):
        scripts.joinpath(name).write_bytes(source_root.joinpath("scripts", name).read_bytes())

    rules = {item.rule for item in scan_make_targets(makefile, ("_smoke",), repo_root=tmp_path)}

    assert "private formal Make recipes do not match the audited action manifest" in rules
