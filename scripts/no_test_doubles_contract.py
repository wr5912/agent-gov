"""正式验收 test-double 防伪扫描的类型与规则契约。"""

from __future__ import annotations

import re
from dataclasses import dataclass
from pathlib import Path
from typing import Final

REPO_ROOT: Final = Path(__file__).resolve().parents[1]
DEFAULT_POLICY: Final = Path("tests/quality_policy.json")
PYTHON_SUFFIXES: Final = frozenset({".py"})
JAVASCRIPT_SUFFIXES: Final = frozenset({".js", ".jsx", ".mjs", ".cjs", ".ts", ".tsx"})
SHELL_SUFFIXES: Final = frozenset({".sh"})
LOCAL_SOURCE_SUFFIXES: Final = PYTHON_SUFFIXES | JAVASCRIPT_SUFFIXES | SHELL_SUFFIXES
IGNORED_PARTS: Final = frozenset({"node_modules", "dist", "coverage", "artifacts", "__pycache__"})
FORBIDDEN_PYTHON_NAMES: Final = frozenset(
    {
        "AsyncMock",
        "MagicMock",
        "Mock",
        "MockTransport",
        "PropertyMock",
        "SimpleNamespace",
        "monkeypatch",
    },
)
IN_PROCESS_HTTP_CLIENT_NAMES: Final = frozenset({"ASGITransport", "TestClient"})
IN_PROCESS_HTTP_CLIENT_IMPORTS: Final = {
    "httpx": frozenset({"ASGITransport"}),
    "fastapi.testclient": frozenset({"TestClient"}),
    "starlette.testclient": frozenset({"TestClient"}),
}
FORBIDDEN_PYTHON_MODULE_PREFIXES: Final = (
    "builtins",
    "aioresponses",
    "httpretty",
    "importlib.machinery",
    "importlib.util",
    "pytest_httpserver",
    "responses",
    "respx",
    "runpy",
    "unittest.mock",
    "vcr",
)
DYNAMIC_PYTHON_NAMES: Final = frozenset({"__builtins__", "__import__", "compile", "eval", "exec", "globals", "locals"})
FORBIDDEN_TEST_HOOK_KEYWORDS: Final = frozenset(
    {
        "control_transport",
        "format_normalized_feedback",
        "receipt_transport",
    },
)
PRODUCTION_COLLABORATOR_KEYWORDS: Final = frozenset(
    {
        "agent_exists",
        "agent_status",
        "agent_version_provider",
        "get_change_set",
        "latest_passed_test_run",
        "read_version_store_for",
        "release_candidate",
        "require_api_key",
        "run_candidate",
        "store_for",
        "trace_fetcher",
        "version_store_for",
    },
)
REPLACED_PRODUCTION_METHODS: Final = frozenset({"enqueue", "fetch_trace", "invoke"})
TRACE_FETCHER_SETTERS: Final = frozenset({"set_langfuse_trace_fetcher"})
PROTECTED_MAKE_VARIABLES: Final = (
    "ACCEPTANCE_PYTHON",
    "AGENTGOV_ACCEPTANCE_AWK",
    "AGENTGOV_ACCEPTANCE_BASH",
    "AGENTGOV_ACCEPTANCE_CURL",
    "AGENTGOV_ACCEPTANCE_DOCKER",
    "AGENTGOV_ACCEPTANCE_GIT",
    "AGENTGOV_ACCEPTANCE_MAKE",
    "AGENTGOV_ACCEPTANCE_NODE",
    "AGENTGOV_ACCEPTANCE_PYTHON",
    "COMPOSE",
    "CONTAINER_ACCEPTANCE",
    "MAKE",
    "PYTHON",
    "PYTHON_RUN",
    "QUALITY_POLICY",
    "REQUIRE_CONTAINER_ACCEPTANCE",
    "SHELL",
    "VENV",
)
PROTECTED_MAKE_ASSIGNMENT: Final = re.compile(rf"^(?:{'|'.join(PROTECTED_MAKE_VARIABLES)})=.+$")
MAKE_TARGET_PATTERN: Final = re.compile(
    r"^([A-Za-z0-9_./%+-]+(?:\s+[A-Za-z0-9_./%+-]+)*)\s*:(?!=)\s*(.*)$",
)
MAKE_VARIABLE_ASSIGNMENT: Final = re.compile(
    r"^(?:(?:override|export|private)\s+)*(?P<name>[A-Za-z_][A-Za-z0-9_]*)\s*"
    r"(?P<operator>:=|\?=|\+=|=)\s*(?P<value>.*)$",
)
MAKE_VARIABLE_REFERENCE: Final = re.compile(r"\$\((?P<paren>[A-Za-z_][A-Za-z0-9_]*)\)|\$\{(?P<brace>[A-Za-z_][A-Za-z0-9_]*)\}")
LOCAL_SOURCE_REFERENCE: Final = re.compile(
    r"(?<![A-Za-z0-9_./-])((?:\.{0,2}/)?(?:[A-Za-z0-9_.-]+/)+[A-Za-z0-9_./-]+\.(?:py|jsx|mjs|cjs|tsx|js|ts|sh))"
    r"(?![A-Za-z0-9_.-])",
)
RECURSIVE_MAKE_MARKER: Final = re.compile(r"(?:\$\((?:MAKE)\)|\$\{(?:MAKE)\}|(?<![A-Za-z0-9_./-])(?:/usr/bin/)?make)(?:\s|$)")
PACKAGE_SCRIPT_INVOCATION: Final = re.compile(
    r"\b(?P<manager>pnpm|npm|yarn)\s+(?:(?:--dir|--prefix)\s+(?P<directory>[^\s]+)\s+)?(?:run\s+)(?P<script>[A-Za-z0-9_.:-]+)",
)
INTERPRETER_INVOCATION: Final = re.compile(
    r"(?:^|[;&|]\s*|\b(?:then|do)\s+)\s*[-+@]*"
    r"(?:[A-Za-z_][A-Za-z0-9_]*=(?:\"[^\"]*\"|'[^']*'|[^\s;&|]+)\s+)*"
    r"(?P<executable>(?:[\"']?[^\s\"']*/)?(?:python(?:\d+(?:\.\d+)*)?|(?:ba)?sh|node)[\"']?|"
    r"\$\(\s*abspath\s+[^)]*(?:python|node|bash)[^)]*\)|"
    r"[\"']?\$\${[^}\n]*(?:PYTHON|NODE|BASH)[^}\n]*}[\"']?)"
    r"\s+(?P<argument>[\"'][^\"']+[\"']|[^\s;&|]+)",
    re.IGNORECASE,
)
MAKE_RULES: Final = (
    (re.compile(r"(?i)(?<![A-Za-z0-9])(?:fake|mock|stub)(?![A-Za-z0-9])"), "named test double"),
    (
        re.compile(rf"(?:{'|'.join(PROTECTED_MAKE_VARIABLES)})\s*=\s*\S+(?:\s|$)"),
        "protected Make command assignment",
    ),
    (re.compile(r"--\s+(?::|true)(?:\s|$)"), "no-op child command"),
    (re.compile(r"^\s*@?(?::|true)(?:\s|$)"), "no-op target recipe"),
    (re.compile(r"^\s*[-+@]*(?:/usr)?/bin/true(?:\s|$)"), "no-op target recipe"),
    (re.compile(r"^\s*[-+@]*exit\s+0(?:\s|$)"), "no-op target recipe"),
)
DYNAMIC_MAKE_RULES: Final = (
    (re.compile(r"^\s*(?:-?include|sinclude)\b"), "Make include directive is not statically auditable"),
    (
        re.compile(r"^\s*(?:(?:override|export|private)\s+)*define\b"),
        "Make define directive is not statically auditable",
    ),
    (re.compile(r"\$[({]\s*(?:eval|call)\b"), "dynamic Make eval/call is not statically auditable"),
    (re.compile(r"\$[({]\s*file\b"), "dynamic Make file function is not statically auditable"),
)
AUDITED_MAKE_SHELL_LINES: Final = frozenset(
    {
        "CUTOVER_PYTHON_RUN = $(if $(filter 0,$(shell id -u)),$(PYTHON_RUN),sudo -- $(abspath $(PYTHON)))",
        "_SOURCE_ARTIFACT_SHA256 := $(shell $(PYTHON_RUN) -c 'from pathlib import Path; from scripts.agentscope_atomic_cutover_bootstrap import source_artifact_sha256; print(source_artifact_sha256(Path.cwd()))' 2>/dev/null || printf '__SOURCE_DIGEST_ERROR__')",
        "_SOURCE_ARTIFACT_SHA256_VALID := $(shell printf '%s' '$(_SOURCE_ARTIFACT_SHA256)' | grep -Eq '^[0-9a-f]{64}$$' && printf yes)",
        "export APP_VERSION := $(shell cat $(CURDIR)/VERSION 2>/dev/null || echo dev)",
    }
)
DEPLOYED_FORMAL_TARGETS: Final = frozenset({"ui-playground-deployed-smoke"})
CANONICAL_FORMAL_MAKE_BINDINGS: Final = {
    "ACCEPTANCE_PYTHON": "override ACCEPTANCE_PYTHON := $(abspath .venv/bin/python)",
    "COMPOSE": "COMPOSE ?= docker compose --env-file $(COMPOSE_ENV_FILE) -f docker/docker-compose.yml",
    "CONTAINER_ACCEPTANCE": ('override CONTAINER_ACCEPTANCE := $(ACCEPTANCE_PYTHON) scripts/run_container_acceptance.py --env-file "$(COMPOSE_ENV_FILE)"'),
    "MAKE": "override MAKE := $(if $(AGENTGOV_ACCEPTANCE_MAKE),$(AGENTGOV_ACCEPTANCE_MAKE),/usr/bin/make)",
    "PYTHON": "PYTHON ?= $(VENV)/bin/python",
    "PYTHON_RUN": "PYTHON_RUN ?= $(PYTHON)",
    "QUALITY_POLICY": "override QUALITY_POLICY := tests/quality_policy.json",
    "REQUIRE_CONTAINER_ACCEPTANCE": (
        'override REQUIRE_CONTAINER_ACCEPTANCE = [ "$$AGENT_GOV_CONTAINER_ACCEPTANCE_ACTIVE" = "1" ] '
        '&& [ -n "$$AGENT_GOV_ACCEPTANCE_RUN_ID" ] && '
        '"$${AGENTGOV_ACCEPTANCE_PYTHON:?missing bound acceptance python}" '
        'scripts/verify_container_acceptance_context.py || { echo "Use the public container acceptance Make target." >&2; exit 1; }'
    ),
    "SHELL": "override SHELL := $(if $(AGENTGOV_ACCEPTANCE_BASH),$(AGENTGOV_ACCEPTANCE_BASH),/bin/bash)",
    "VENV": "VENV ?= .venv",
}
CANONICAL_FORMAL_DISPATCHES: Final = {
    "ui-playground-deployed-smoke": (
        '@$(ACCEPTANCE_PYTHON) scripts/run_selected_env_operation.py --env-file "$(COMPOSE_ENV_FILE)" --operation ui-playground-deployed-smoke'
    ),
    "smoke": "$(CONTAINER_ACCEPTANCE) --profile core -- $(MAKE) --no-print-directory _smoke",
    "ui-smoke": "$(CONTAINER_ACCEPTANCE) --profile core -- $(MAKE) --no-print-directory _ui-smoke",
    "container-core-smoke": "$(CONTAINER_ACCEPTANCE) --profile core -- $(MAKE) --no-print-directory _container-core-smoke",
    "container-openapi-check": "$(CONTAINER_ACCEPTANCE) --profile core -- $(MAKE) --no-print-directory _container-openapi-check",
    "container-live-test": "$(CONTAINER_ACCEPTANCE) --profile langfuse -- $(MAKE) --no-print-directory _container-live-test",
    "container-technical-live-smoke": ("$(CONTAINER_ACCEPTANCE) --profile langfuse -- $(MAKE) --no-print-directory _container-technical-live-smoke"),
    "container-mcp-technical-smoke": ("$(CONTAINER_ACCEPTANCE) --profile core -- $(MAKE) --no-print-directory _container-mcp-technical-smoke"),
    "container-release-candidate": ("$(CONTAINER_ACCEPTANCE) --profile langfuse -- $(MAKE) --no-print-directory _container-release-candidate"),
    "main-flow-live-test": "$(CONTAINER_ACCEPTANCE) --profile langfuse -- $(MAKE) --no-print-directory _main-flow-live-test",
    "ui-feedback-smoke": (
        "$(CONTAINER_ACCEPTANCE) --profile langfuse -- $(MAKE) --no-print-directory _ui-feedback-smoke AGENT_GOV_FORMAL_BROWSER_ACCEPTANCE=1 BROWSER=both"
    ),
    "ui-playground-cancel-smoke": (
        "$(CONTAINER_ACCEPTANCE) --profile core -- $(MAKE) --no-print-directory _ui-playground-cancel-smoke AGENT_GOV_FORMAL_BROWSER_ACCEPTANCE=1 BROWSER=both"
    ),
    "ui-playground-technical-smoke": (
        '@REAL_SCENARIO_FILE="$${BROWSER_TECHNICAL_SCENARIO_FILE}" REAL_ACCEPTANCE_AGENT_ID=security-operations-expert '
        "$(CONTAINER_ACCEPTANCE) --profile core -- $(MAKE) --no-print-directory "
        "_ui-playground-cancel-smoke AGENT_GOV_FORMAL_BROWSER_ACCEPTANCE=0 "
        "BROWSER=both REAL_ACCEPTANCE_AGENT_ID=security-operations-expert"
    ),
    "ui-agent-candidate-technical-smoke": (
        "$(CONTAINER_ACCEPTANCE) --profile langfuse -- $(MAKE) --no-print-directory _ui-agent-candidate-technical-smoke BROWSER=both"
    ),
    "langfuse-smoke": "$(CONTAINER_ACCEPTANCE) --profile langfuse -- $(MAKE) --no-print-directory _langfuse-smoke",
}
CANONICAL_FORMAL_PREREQUISITES: Final = {
    "ui-playground-deployed-smoke": frozenset(),
    "smoke": frozenset(),
    "ui-smoke": frozenset(),
    "container-core-smoke": frozenset(),
    "container-openapi-check": frozenset(),
    "container-live-test": frozenset({"live-acceptance-preflight"}),
    "container-technical-live-smoke": frozenset({"technical-live-preflight"}),
    "container-mcp-technical-smoke": frozenset({"mcp-technical-live-preflight"}),
    "container-release-candidate": frozenset({"live-acceptance-preflight"}),
    "main-flow-live-test": frozenset({"live-acceptance-preflight"}),
    "ui-feedback-smoke": frozenset({"live-acceptance-preflight"}),
    "ui-playground-cancel-smoke": frozenset({"live-acceptance-preflight"}),
    "ui-playground-technical-smoke": frozenset({"browser-technical-live-preflight"}),
    "ui-agent-candidate-technical-smoke": frozenset({"technical-live-preflight"}),
    "langfuse-smoke": frozenset({"live-acceptance-preflight"}),
}
CANONICAL_AUXILIARY_RECIPE_SHA256: Final = {
    "live-acceptance-preflight": "4cb54a358c26dbdfe90ff89d9ede2f6e5e157db7abfb4fad2ef7b2b190bca019",
    "technical-live-preflight": "25249463aff5ef58a8ad0ffa7db7ad49c9fd57cfc4f4a4f900f70f14b20adee4",
    "mcp-technical-live-preflight": "452e0a0eee4b3e7fc5a1115a5deb95d4cf3afcc75fe5257d9b62d6c10a89aaa8",
    "browser-technical-live-preflight": "2ab62ba7842a58bc221d42bbff9e2d5f256fce38523de6a93220152ab009ee62",
}
CANONICAL_PRIVATE_RECIPE_SHA256: Final = {
    "_smoke": "720e4bb00477f56f2f695155d8a31009ae9421695a9bf47db5223fa417f664fd",
    "_ui-smoke": "6ba423ddd271ed1201431e71f19ae8311822236565a3c0fc9cbe33b6ec72bb19",
    "_container-core-smoke": "498dbe122e9c79f554f31763fe60f723134c469c4439a0d4264b1d33bdf37616",
    "_container-openapi-check": "68427368aa41276221b9d70d433bfb71dd23a3aa2938d2cf2a8145971515493f",
    "_container-live-test": "01affab30f4862709d550c70e6c64bd194e83f7366d098cffda6ec89a4aa4fad",
    "_container-technical-live-smoke": "9bac6458bce120bd03ec84ec877e08b0f83a684f1718f45659ffcc1c840b317c",
    "_container-mcp-technical-smoke": "736f7b89853b6ee14c78b01fdaa73d0f20db35bd90822a45fc926053f30a4677",
    "_container-release-candidate": "f29b32fae420fe010d20fbeb030496740aa1eba24872ef78c5bf7f14aba55f65",
    "_main-flow-live-test": "1f78ce7c109117b68c832a58be2f2d4392cf5766afe8f8734536ba8c1322614d",
    "_ui-feedback-smoke": "53584def08c19054dd3173a2a80e89de155ea90f494eae2708f8305c61003519",
    "_ui-playground-cancel-smoke": "e298564925817dbc6ac5ad42cf1b96b1821c41cf715fe0f71032c72f5776d619",
    "_ui-agent-candidate-technical-smoke": "3a3099440d4b93ec04667428bb6bf549c1782eaa81ab046303db2b3bfe87beda",
    "_langfuse-smoke": "eef73420d738b9d151bbab2055ad94d3d4b1ccdbbaba8233254c4555ec0db1d9",
}
_VERIFY_CONTEXT = "scripts/verify_container_acceptance_context.py"
_RUNNER = "scripts/run_container_acceptance.py"
_VALIDATE_SCENARIOS = "scripts/validate_live_acceptance_scenarios.py"
REQUIRED_FORMAL_TARGET_FILES: Final = {
    "ui-playground-deployed-smoke": frozenset({"scripts/run_selected_env_operation.py"}),
    "smoke": frozenset({_RUNNER, _VERIFY_CONTEXT, "scripts/diagnose_runtime_health.py"}),
    "_smoke": frozenset({_VERIFY_CONTEXT, "scripts/diagnose_runtime_health.py"}),
    "ui-smoke": frozenset({_RUNNER, _VERIFY_CONTEXT}),
    "_ui-smoke": frozenset({_VERIFY_CONTEXT}),
    "container-core-smoke": frozenset({_RUNNER, _VERIFY_CONTEXT, "scripts/audit_openapi_contract.py", "scripts/diagnose_runtime_health.py"}),
    "_container-core-smoke": frozenset({_VERIFY_CONTEXT, "scripts/audit_openapi_contract.py", "scripts/diagnose_runtime_health.py"}),
    "container-openapi-check": frozenset({_RUNNER, _VERIFY_CONTEXT, "scripts/audit_openapi_contract.py"}),
    "_container-openapi-check": frozenset({_VERIFY_CONTEXT, "scripts/audit_openapi_contract.py"}),
    "container-live-test": frozenset({_RUNNER, _VERIFY_CONTEXT, _VALIDATE_SCENARIOS, "scripts/run_agentscope_live_acceptance.py"}),
    "_container-live-test": frozenset({_VERIFY_CONTEXT, "scripts/run_agentscope_live_acceptance.py"}),
    "container-technical-live-smoke": frozenset({_RUNNER, _VERIFY_CONTEXT, _VALIDATE_SCENARIOS, "scripts/run_agentscope_live_acceptance.py"}),
    "_container-technical-live-smoke": frozenset({_VERIFY_CONTEXT, "scripts/run_agentscope_live_acceptance.py"}),
    "container-mcp-technical-smoke": frozenset({_RUNNER, _VERIFY_CONTEXT, _VALIDATE_SCENARIOS, "scripts/run_agentscope_live_acceptance.py"}),
    "_container-mcp-technical-smoke": frozenset({_VERIFY_CONTEXT, "scripts/run_agentscope_live_acceptance.py"}),
    "container-release-candidate": frozenset(
        {
            _RUNNER,
            _VERIFY_CONTEXT,
            _VALIDATE_SCENARIOS,
            "scripts/check_no_test_doubles.py",
            "scripts/check_test_quality_policy.py",
            "scripts/run_agentgov_testkit_live.py",
            "scripts/run_agentscope_live_acceptance.py",
            "scripts/verify_improvement_ui_real_container.mjs",
            "scripts/verify_playground_cancel.mjs",
        }
    ),
    "_container-release-candidate": frozenset(
        {
            _VERIFY_CONTEXT,
            "scripts/check_no_test_doubles.py",
            "scripts/check_test_quality_policy.py",
            "scripts/run_agentgov_testkit_live.py",
            "scripts/run_agentscope_live_acceptance.py",
            "scripts/verify_improvement_ui_real_container.mjs",
            "scripts/verify_playground_cancel.mjs",
        }
    ),
    "main-flow-live-test": frozenset({_RUNNER, _VERIFY_CONTEXT, _VALIDATE_SCENARIOS, "scripts/run_main_flow_live_targets.py"}),
    "_main-flow-live-test": frozenset({_VERIFY_CONTEXT, "scripts/run_main_flow_live_targets.py"}),
    "ui-feedback-smoke": frozenset({_RUNNER, _VERIFY_CONTEXT, _VALIDATE_SCENARIOS, "scripts/verify_improvement_ui_real_container.mjs"}),
    "_ui-feedback-smoke": frozenset({_VERIFY_CONTEXT, "scripts/verify_improvement_ui_real_container.mjs"}),
    "ui-playground-cancel-smoke": frozenset({_RUNNER, _VERIFY_CONTEXT, _VALIDATE_SCENARIOS, "scripts/verify_playground_cancel.mjs"}),
    "_ui-playground-cancel-smoke": frozenset({_VERIFY_CONTEXT, "scripts/verify_playground_cancel.mjs"}),
    "ui-playground-technical-smoke": frozenset({_RUNNER, _VERIFY_CONTEXT, _VALIDATE_SCENARIOS, "scripts/verify_playground_cancel.mjs"}),
    "ui-agent-candidate-technical-smoke": frozenset({_RUNNER, _VERIFY_CONTEXT, _VALIDATE_SCENARIOS, "scripts/verify_agent_candidate_lifecycle.mjs"}),
    "_ui-agent-candidate-technical-smoke": frozenset({_VERIFY_CONTEXT, "scripts/verify_agent_candidate_lifecycle.mjs"}),
    "langfuse-smoke": frozenset({_RUNNER, _VERIFY_CONTEXT, _VALIDATE_SCENARIOS, "scripts/langfuse_smoke.py"}),
    "_langfuse-smoke": frozenset({_VERIFY_CONTEXT, "scripts/langfuse_smoke.py"}),
}
DOUBLE_CLASS_PREFIXES: Final = ("Fake", "Mock", "Stub")
DOUBLE_FUNCTION_PREFIXES: Final = ("fake_", "mock_", "stub_")
HTTP_ROUTE_DECORATORS: Final = frozenset(
    {"get", "post", "put", "patch", "delete", "options", "head", "route", "websocket"},
)
HTTP_ROUTE_REGISTRATION_METHODS: Final = frozenset(
    {"add_api_route", "add_route", "add_websocket_route"},
)
HTTP_ROUTE_TYPES: Final = frozenset({"Route", "WebSocketRoute"})
JAVASCRIPT_RULES: Final = (
    (re.compile(r"\bvi\s*\.\s*(?:mock|fn|spyOn|stubGlobal|stubEnv|useFakeTimers|advanceTimers\w*|restoreAllMocks|clearAllMocks)\s*\("), "Vitest test double"),
    (re.compile(r"\bt\s*\.\s*mock\s*\."), "Node test double"),
    (re.compile(r"\b(?:page|context|route)\s*\.\s*(?:route|fulfill|abort)\s*\("), "browser request interception"),
    (re.compile(r"\b(?:page|context)\s*\.\s*routeFromHAR\s*\("), "browser HAR replay"),
    (re.compile(r"\b(?:page|context)\s*\.\s*(?:setContent|addInitScript)\s*\("), "fabricated browser page state"),
    (re.compile(r"\bdocument(?:\s*\.\s*body)?\s*\.\s*innerHTML\s*="), "fabricated browser DOM"),
    (
        re.compile(r"(?i)(?:^|[/'\"])(?:axios-mock-adapter|fetch-mock|msw|nock|sinon)(?:[/'\"]|$)"),
        "JavaScript test-double library",
    ),
    (re.compile(r"(?<![A-Za-z0-9])_*(?:Fake|Mock|Stub)[A-Z][A-Za-z0-9_]*\b"), "named test double"),
    (re.compile(r"(?<![A-Za-z0-9])_*(?:fake|mock|stub)[A-Z_][A-Za-z0-9_]*\b"), "named test double"),
    (re.compile(r"\bimport\s*\(\s*(?!['\"])", re.MULTILINE), "dynamic JavaScript import is not auditable"),
    (re.compile(r"\brequire\s*\(\s*(?!['\"])", re.MULTILINE), "dynamic JavaScript require is not auditable"),
    (re.compile(r"\b(?:eval|Function)\s*\("), "dynamic JavaScript code execution is not auditable"),
    (re.compile(r"\bnew\s+Function\s*\("), "dynamic JavaScript code execution is not auditable"),
    (
        re.compile(
            r"\b(?:const|let|var)\s+[A-Za-z_$][\w$]*\s*=\s*"
            r"(?:(?:require|eval|Function)\b(?!\s*\()|"
            r"(?:module|globalThis|global|window)\s*\.\s*(?:require|eval|Function)\b(?!\s*\())"
        ),
        "aliased JavaScript loader or evaluator is not auditable",
    ),
    (
        re.compile(r"\[\s*['\"](?:require|eval|Function)['\"]\s*\]"),
        "computed JavaScript loader or evaluator access is not auditable",
    ),
    (
        re.compile(r"\b(?:globalThis|global|window)\s*\["),
        "computed JavaScript global access is not auditable",
    ),
)
TEST_ONLY_JAVASCRIPT_RULES: Final = (
    (re.compile(r"\b(?:globalThis|window)\s*\.\s*fetch\s*="), "replaced browser fetch transport"),
    (re.compile(r"\bnew\s+(?:Response|ReadableStream)\s*\("), "fabricated browser transport response"),
    (
        re.compile(
            r"\b(?:fetch|getRun|getStatus|request|transport)\s*:\s*"
            r"(?:async\s*)?(?:\([^)]*\)|[A-Za-z_$][\w$]*)\s*=>",
        ),
        "injected JavaScript collaborator",
    ),
    (re.compile(r"\.\s*(?:route|fulfill)\s*\("), "browser request interception"),
)
JAVASCRIPT_LOCAL_IMPORT: Final = re.compile(
    r"(?:\bfrom\s*|\bimport\s*(?:\(\s*)?|\brequire\s*\(\s*)['\"](\.{1,2}/[^'\"]+)['\"]",
)


@dataclass(frozen=True, order=True)
class Finding:
    path: str
    line: int
    rule: str

    def render(self) -> str:
        return f"{self.path}:{self.line}: {self.rule}"


@dataclass(frozen=True)
class FormalAcceptanceScope:
    files: tuple[Path, ...]
    make_targets: tuple[str, ...]


@dataclass(frozen=True)
class FormalMakeInspection:
    files: tuple[Path, ...]
    findings: tuple[Finding, ...]
    targets: tuple[str, ...]


@dataclass
class MakeRule:
    prerequisites: set[str]
    recipes: list[tuple[int, str]]


@dataclass(frozen=True)
class ParsedMakefile:
    rules: dict[str, MakeRule]
    variables: dict[str, str]
