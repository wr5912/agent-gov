VENV ?= .venv
PYTHON ?= $(VENV)/bin/python
UV ?= uv
PYTHON_RUN ?= $(PYTHON)
override SHELL := $(if $(AGENTGOV_ACCEPTANCE_BASH),$(AGENTGOV_ACCEPTANCE_BASH),/bin/bash)
override MAKE := $(if $(AGENTGOV_ACCEPTANCE_MAKE),$(AGENTGOV_ACCEPTANCE_MAKE),/usr/bin/make)
CUTOVER_PYTHON_RUN = $(if $(filter 0,$(shell id -u)),$(PYTHON_RUN),sudo -- $(abspath $(PYTHON)))
COMPOSE_ENV_FILE ?= docker/.env
export COMPOSE_ENV_FILE
export AGENT_GOV_COMPOSE_ENV_FILE := $(abspath $(COMPOSE_ENV_FILE))
ifeq ($(strip $(MAKECMDGOALS)),setup)
override AGENTGOV_SOURCE_ARTIFACT_SHA256 := unmanaged
else
_SOURCE_ARTIFACT_SHA256 := $(shell $(PYTHON_RUN) -c 'from pathlib import Path; from scripts.agentscope_atomic_cutover_bootstrap import source_artifact_sha256; print(source_artifact_sha256(Path.cwd()))' 2>/dev/null || printf '__SOURCE_DIGEST_ERROR__')
_SOURCE_ARTIFACT_SHA256_VALID := $(shell printf '%s' '$(_SOURCE_ARTIFACT_SHA256)' | grep -Eq '^[0-9a-f]{64}$$' && printf yes)
ifneq ($(_SOURCE_ARTIFACT_SHA256_VALID),yes)
$(error source artifact SHA-256 generation failed; configure PYTHON_RUN with project Python >=3.11)
endif
override AGENTGOV_SOURCE_ARTIFACT_SHA256 := $(_SOURCE_ARTIFACT_SHA256)
endif
export AGENTGOV_SOURCE_ARTIFACT_SHA256
COMPOSE ?= docker compose --env-file $(COMPOSE_ENV_FILE) -f docker/docker-compose.yml
LANGFUSE_COMPOSE = $(COMPOSE) -f docker/docker-compose.langfuse.yml
COMPOSE_UP_FLAGS ?=
SELECTED_ENV_RUNNER = $(PYTHON_RUN) scripts/run_selected_env_operation.py --env-file "$(COMPOSE_ENV_FILE)"
LOCAL_DEBUG_API_PORT ?= 0
LOCAL_DEBUG_UI_PORT ?= 0
# 版本唯一真相源：根 VERSION 文件。导出给 compose，让镜像 tag ${APP_VERSION} 派生（build/up 自动生效）。
export APP_VERSION := $(shell cat $(CURDIR)/VERSION 2>/dev/null || echo dev)
PYTHON_TYPECHECK_TARGETS := \
	app/api_mode.py \
	app/openapi_contract.py \
	agentscope_runtime \
	app/runtime_gateway \
	app/routers/agent_workspace_packages.py \
	app/agent_testing \
	app/runtime/advisory_lock.py \
	app/runtime/agent_git_raw_storage.py \
	app/runtime/agent_git_worktree_operations.py \
	app/runtime/agent_job_types.py \
	app/runtime/agent_workspace_package_schemas.py \
	app/runtime/runtime_bootstrap.py \
	app/runtime/business_agent_workspace.py \
	app/runtime/managed_agent_policy.py \
	app/runtime/runtime_coordination.py \
	app/runtime/runtime_initialization.py \
	app/runtime/published_harness_preparation.py \
	app/runtime/service_launcher.py \
	app/services/agent_change_set_queries.py \
	app/services/business_agent_presentation.py \
	app/services/agent_workspace_git_operations.py \
	app/services/agent_workspace_manifest_identity.py \
	app/services/agent_workspace_package_codec.py \
	app/services/agent_workspace_packages.py \
	app/services/generated_agent_tests.py \
	app/services/improvement_execution_service.py \
	app/services/improvement_governor_service.py \
	app/services/workspace_execution_applier.py \
	app/runtime/stores/feedback_case_store.py \
	app/runtime/stores/feedback_store.py \
	app/runtime/stores/improvement_content_store.py \
	app/runtime/stores/improvement_store.py \
	scripts/bootstrap_runtime_volume.py \
	scripts/check_codex_governance.py \
	scripts/check_docs_governance.py \
	scripts/check_orphan_tests.py \
	scripts/check_stage_language.py \
	scripts/check_public_bind.py \
	scripts/audit_openapi_contract.py \
	scripts/agentscope_native_contract.py \
	scripts/check_agentscope_cutover.py \
	scripts/codex_governance_typed_output.py \
	scripts/check_test_quality_policy.py \
	scripts/check_no_test_doubles.py \
	scripts/validate_live_acceptance_scenarios.py \
	scripts/run_agentgov_testkit_live.py \
	scripts/run_main_flow_live_targets.py \
	scripts/run_test_lane.py \
	scripts/run_mutation_lane.py \
	scripts/select_impacted_tests.py \
	scripts/compare_test_shadow_evidence.py \
	scripts/evaluate_test_shadow_history.py \
	scripts/test_quality/collection.py \
	scripts/test_quality/coverage.py \
	scripts/test_quality/evidence.py \
	scripts/test_quality/models.py \
	scripts/test_quality/policy.py \
	scripts/diagnose_runtime_health.py \
	packages/agentgov-testkit/src/agentgov_testkit \
	docker/runtime-bootstrap/governor-workspace/tests \
	scripts/runtime_bootstrap_secret_assignments.py \
	scripts/runtime_bootstrap_safety.py \
	scripts/runtime_cleanup.py \
	scripts/cleanup_runtime_artifacts.py \
	scripts/run_main_flow_tests.py \
	scripts/run_container_acceptance.py \
	scripts/verify_container_acceptance_context.py \
	scripts/run_agentscope_live_acceptance.py \
	scripts/run_workspace_reclaim_acceptance.py \
	scripts/workspace_reclaim_acceptance_sessions.py \
	scripts/workspace_reclaim_acceptance_runtime.py \
	scripts/workspace_reclaim_acceptance_watchdog.py \
	scripts/workspace_reclaim_acceptance_watchdog_control.py \
	scripts/agentscope_live_acceptance_cli.py \
	scripts/agentscope_live_acceptance_report.py \
	scripts/agentscope_mcp_live_acceptance.py \
	scripts/run_selected_env_operation.py \
	scripts/selected_env_browser_toolchain.py \
	scripts/selected_env_deployed_context.py \
	scripts/selected_env_deployed_browser.py \
	scripts/verify_deployed_browser_context.py \
	scripts/agentscope_live_acceptance_scenarios.py \
	scripts/runtime_technical_integration_seed.py \
	scripts/agentscope_atomic_cutover.py \
	scripts/agentscope_atomic_cutover_daemon.py \
	scripts/agentscope_atomic_cutover_env.py \
	scripts/agentscope_atomic_cutover_fs.py \
	scripts/agentscope_atomic_cutover_bootstrap.py \
	scripts/agentscope_atomic_cutover_images.py \
	scripts/agentscope_atomic_cutover_lock.py \
	scripts/agentscope_atomic_cutover_rollback.py \
	scripts/agentscope_atomic_cutover_evidence.py \
	scripts/agentscope_atomic_cutover_recovery.py \
	scripts/agentscope_atomic_cutover_support.py \
	scripts/agentscope_atomic_cutover_types.py \
	scripts/langfuse_smoke.py

override TEST_ARTIFACT_ROOT := artifacts/test-quality
override BACKEND_TEST_ARTIFACT_DIR := $(TEST_ARTIFACT_ROOT)/backend-main-full
override QUALITY_POLICY := tests/quality_policy.json
GOVERNANCE_BASE_REF ?=
GOVERNANCE_BASE_REF_ARG := $(if $(strip $(GOVERNANCE_BASE_REF)),--base-ref $(GOVERNANCE_BASE_REF),)
override ACCEPTANCE_PYTHON := $(abspath .venv/bin/python)
override CONTAINER_ACCEPTANCE := $(ACCEPTANCE_PYTHON) scripts/run_container_acceptance.py --env-file "$(COMPOSE_ENV_FILE)"
override REQUIRE_CONTAINER_ACCEPTANCE = [ "$$AGENT_GOV_CONTAINER_ACCEPTANCE_ACTIVE" = "1" ] && [ -n "$$AGENT_GOV_ACCEPTANCE_RUN_ID" ] && "$${AGENTGOV_ACCEPTANCE_PYTHON:?missing bound acceptance python}" scripts/verify_container_acceptance_context.py || { echo "Use the public container acceptance Make target." >&2; exit 1; }

.PHONY: setup build public-bind-check live-acceptance-preflight technical-live-preflight mcp-technical-live-preflight browser-technical-live-preflight up all-up down logs test test-backend coverage main-flow-test main-flow-ui-test main-flow-live-test mutation-test test-double-check openapi-contract-check agentscope-contract-check openapi-type-drift-check container-core-smoke container-openapi-check container-live-test container-technical-live-smoke container-mcp-technical-smoke container-release-candidate smoke compose-diagnose codex-guard cutover-check cutover-inspect sync-version tag ruff-check ruff-format-check pyright typecheck ui-build ui-up ui-recreate ui-stop ui-logs ui-smoke ui-design-parity ui-feedback-smoke ui-playground-cancel-smoke ui-playground-technical-smoke ui-agent-candidate-technical-smoke images-prepare langfuse-prepare langfuse-up langfuse-stop langfuse-logs langfuse-smoke runtime-bootstrap runtime-recreate runtime-validate runtime-clean runtime-migrate-workspace-tests runtime-migrate-workspace-tests-scan local-debug-env local-debug-bootstrap local-debug-validate local-debug-clean runtime-bootstrap-scan runtime-bootstrap-clean clean-runtime-artifacts _runtime-health-diagnose _container-core-smoke _container-openapi-check _container-live-test _container-technical-live-smoke _container-mcp-technical-smoke _container-release-candidate _main-flow-live-test _smoke _ui-smoke _ui-feedback-smoke _ui-playground-cancel-smoke _ui-agent-candidate-technical-smoke _langfuse-smoke

setup:
	cp -n docker/.env.example docker/.env || true
	@if ! command -v $(UV) >/dev/null 2>&1; then echo "uv is required. Install uv before running make setup." >&2; exit 1; fi
	$(UV) venv $(VENV) --python 3.11
	$(UV) pip install --python $(PYTHON) -r requirements.txt -e packages/agentgov-testkit
	$(PYTHON_RUN) scripts/initialize_runtime_shared_secret.py --env-file "$(COMPOSE_ENV_FILE)"

build:
	$(SELECTED_ENV_RUNNER) --operation build

public-bind-check:
	$(PYTHON_RUN) scripts/check_public_bind.py --env-file "$(COMPOSE_ENV_FILE)"

live-acceptance-preflight:
	@case "$${REQUIRE_LIVE_RUNTIME:-}" in 1|true|yes|on) ;; *) \
		echo "Set REQUIRE_LIVE_RUNTIME=1 to authorize real provider calls." >&2; exit 2 ;; \
	esac
	@test -n "$${REAL_ACCEPTANCE_AGENT_ID:-}" || { \
		echo "REAL_ACCEPTANCE_AGENT_ID must name the reviewed scenario set's registered business Agent." >&2; exit 2; \
	}
	@test -n "$${REAL_SCENARIO_FILE:-}" && test -f "$$REAL_SCENARIO_FILE" || { \
		echo "REAL_SCENARIO_FILE must name an existing operator-reviewed file outside the repository." >&2; exit 2; \
	}
	@$(PYTHON_RUN) scripts/validate_live_acceptance_scenarios.py \
		--scenario-file "$${REAL_SCENARIO_FILE}" \
		--expected-agent-id "$${REAL_ACCEPTANCE_AGENT_ID}" >/dev/null

technical-live-preflight:
	@case "$${REQUIRE_LIVE_RUNTIME:-}" in 1|true|yes|on) ;; *) \
		echo "Set REQUIRE_LIVE_RUNTIME=1 to authorize real provider calls." >&2; exit 2 ;; \
	esac
	@test -n "$${TECHNICAL_SCENARIO_FILE:-}" && test -f "$$TECHNICAL_SCENARIO_FILE" || { \
		echo "TECHNICAL_SCENARIO_FILE must name an existing external technical scenario file." >&2; exit 2; \
	}
	@$(PYTHON_RUN) scripts/validate_live_acceptance_scenarios.py \
		--scenario-file "$${TECHNICAL_SCENARIO_FILE}" \
		--expected-agent-id runtime-technical-integration-package >/dev/null

mcp-technical-live-preflight:
	@case "$${REQUIRE_LIVE_RUNTIME:-}" in 1|true|yes|on) ;; *) \
		echo "Set REQUIRE_LIVE_RUNTIME=1 to authorize real provider calls." >&2; exit 2 ;; \
	esac
	@test -n "$${TECHNICAL_SCENARIO_FILE:-}" && test -f "$$TECHNICAL_SCENARIO_FILE" || { \
		echo "TECHNICAL_SCENARIO_FILE must name an existing external MCP technical scenario file." >&2; exit 2; \
	}
	@$(PYTHON_RUN) scripts/validate_live_acceptance_scenarios.py \
		--scenario-file "$${TECHNICAL_SCENARIO_FILE}" \
		--expected-agent-id runtime-mcp-technical-integration-package >/dev/null

browser-technical-live-preflight:
	@case "$${REQUIRE_LIVE_RUNTIME:-}" in 1|true|yes|on) ;; *) \
		echo "Set REQUIRE_LIVE_RUNTIME=1 to authorize real provider calls." >&2; exit 2 ;; \
	esac
	@test -n "$${BROWSER_TECHNICAL_SCENARIO_FILE:-}" && test -f "$$BROWSER_TECHNICAL_SCENARIO_FILE" || { \
		echo "BROWSER_TECHNICAL_SCENARIO_FILE must name an existing external technical scenario file." >&2; exit 2; \
	}
	@$(ACCEPTANCE_PYTHON) scripts/validate_live_acceptance_scenarios.py \
		--scenario-file "$${BROWSER_TECHNICAL_SCENARIO_FILE}" \
		--expected-agent-id security-operations-expert >/dev/null

up:
	@test -z "$(strip $(COMPOSE_UP_FLAGS))" || test "$(strip $(COMPOSE_UP_FLAGS))" = "--force-recreate" || { echo "COMPOSE_UP_FLAGS only permits --force-recreate on public up/all-up" >&2; exit 2; }
	$(SELECTED_ENV_RUNNER) --operation up $(if $(filter --force-recreate,$(strip $(COMPOSE_UP_FLAGS))),--force-recreate,)

all-up:
	@test -z "$(strip $(COMPOSE_UP_FLAGS))" || test "$(strip $(COMPOSE_UP_FLAGS))" = "--force-recreate" || { echo "COMPOSE_UP_FLAGS only permits --force-recreate on public up/all-up" >&2; exit 2; }
	$(SELECTED_ENV_RUNNER) --operation all-up $(if $(filter --force-recreate,$(strip $(COMPOSE_UP_FLAGS))),--force-recreate,)

_runtime-health-diagnose:
	@python_bin="$(PYTHON)"; \
	if [ ! -x "$$python_bin" ]; then python_bin=$$(command -v python3 2>/dev/null || true); fi; \
	if [ -z "$$python_bin" ]; then \
		echo "Runtime readiness verification failed: neither $(PYTHON) nor python3 is available." >&2; exit 1; \
	else \
		"$$python_bin" scripts/diagnose_runtime_health.py --env-file "$(COMPOSE_ENV_FILE)" --require-ready; \
	fi

down:
	$(SELECTED_ENV_RUNNER) --operation down

logs:
	$(SELECTED_ENV_RUNNER) --operation logs

ui-build:
	$(SELECTED_ENV_RUNNER) --operation ui-build

ui-up:
	$(SELECTED_ENV_RUNNER) --operation ui-up

ui-recreate:
	$(SELECTED_ENV_RUNNER) --operation ui-recreate

ui-stop:
	$(SELECTED_ENV_RUNNER) --operation ui-stop

ui-logs:
	$(SELECTED_ENV_RUNNER) --operation ui-logs

ui-smoke:
	$(CONTAINER_ACCEPTANCE) --profile core -- $(MAKE) --no-print-directory _ui-smoke

_ui-smoke:
	@$(REQUIRE_CONTAINER_ACCEPTANCE)
	@frontend_port=$${FRONTEND_HOST_PORT:-$$($${AGENTGOV_ACCEPTANCE_AWK:?missing bound acceptance awk} -F= '$$1 == "FRONTEND_HOST_PORT" {sub(/^[^=]*=/, ""); print; exit}' "$(COMPOSE_ENV_FILE)" 2>/dev/null)}; \
	frontend_url=$${FRONTEND_URL:-http://localhost:$${frontend_port:-50401}}; \
	i=1; \
	while [ $$i -le 30 ]; do \
		if "$${AGENTGOV_ACCEPTANCE_CURL:?missing bound acceptance curl}" --noproxy '*' -fsS "$$frontend_url" >/dev/null; then \
			echo "Frontend OK: $$frontend_url"; \
			exit 0; \
		fi; \
		sleep 1; \
		i=$$((i + 1)); \
	done; \
	echo "Frontend failed: $$frontend_url" >&2; \
	exit 1

ui-design-parity: ui-feedback-smoke
	@echo "UI design parity is verified only by the real-container feedback flow."

ui-feedback-smoke: live-acceptance-preflight
	$(CONTAINER_ACCEPTANCE) --profile langfuse -- $(MAKE) --no-print-directory \
		_ui-feedback-smoke AGENT_GOV_FORMAL_BROWSER_ACCEPTANCE=1 BROWSER=both

_ui-feedback-smoke:
	@$(REQUIRE_CONTAINER_ACCEPTANCE)
	@frontend_port=$${FRONTEND_HOST_PORT:-$$($${AGENTGOV_ACCEPTANCE_AWK:?missing bound acceptance awk} -F= '$$1 == "FRONTEND_HOST_PORT" {sub(/^[^=]*=/, ""); print; exit}' "$(COMPOSE_ENV_FILE)" 2>/dev/null)}; \
	host_port=$${HOST_PORT:-$$($${AGENTGOV_ACCEPTANCE_AWK:?missing bound acceptance awk} -F= '$$1 == "HOST_PORT" {sub(/^[^=]*=/, ""); print; exit}' "$(COMPOSE_ENV_FILE)" 2>/dev/null)}; \
	api_key=$$($${AGENTGOV_ACCEPTANCE_AWK:?missing bound acceptance awk} -F= '$$1 == "FRONTEND_RUNTIME_API_KEY" || $$1 == "API_KEY" {sub(/^[^=]*=/, ""); print; exit}' "$(COMPOSE_ENV_FILE)" 2>/dev/null); \
	RUNTIME_UI_BASE="http://localhost:$${frontend_port:-50401}" \
	RUNTIME_API_BASE="http://localhost:$${host_port:-50400}" \
	RUNTIME_API_KEY="$$api_key" \
	AGENT_GOV_FORMAL_BROWSER_ACCEPTANCE="$${AGENT_GOV_FORMAL_BROWSER_ACCEPTANCE:-0}" \
	BROWSER="$${BROWSER:-chromium}" \
	VERIFY_SCREENSHOT_DIR="$${VERIFY_SCREENSHOT_DIR:-/tmp/agentgov-ui-feedback-smoke}" \
	"$${AGENTGOV_ACCEPTANCE_NODE:?missing bound acceptance node}" scripts/verify_improvement_ui_real_container.mjs

ui-playground-cancel-smoke: live-acceptance-preflight
	$(CONTAINER_ACCEPTANCE) --profile core -- $(MAKE) --no-print-directory \
		_ui-playground-cancel-smoke AGENT_GOV_FORMAL_BROWSER_ACCEPTANCE=1 BROWSER=both

.PHONY: ui-playground-deployed-smoke
ui-playground-deployed-smoke:
	@$(ACCEPTANCE_PYTHON) scripts/run_selected_env_operation.py --env-file "$(COMPOSE_ENV_FILE)" --operation ui-playground-deployed-smoke

.PHONY: ui-playground-deployed-recovery-smoke
ui-playground-deployed-recovery-smoke:
	@$(ACCEPTANCE_PYTHON) scripts/run_selected_env_operation.py --env-file "$(COMPOSE_ENV_FILE)" --operation ui-playground-deployed-recovery-smoke

.PHONY: ui-self-use-governance-smoke
ui-self-use-governance-smoke:
	@$(ACCEPTANCE_PYTHON) scripts/run_self_use_acceptance.py --env-file "$(COMPOSE_ENV_FILE)" \
		--workspace-package "$(SELF_USE_WORKSPACE_PACKAGE)" --docs-scenarios "$(SELF_USE_DOCS_SCENARIOS)" \
		--soc-scenarios "$(SELF_USE_SOC_SCENARIOS)" --report "$(SELF_USE_REPORT)" \
		--existing-docs-commit "$(SELF_USE_EXISTING_DOCS_COMMIT)"

ui-playground-technical-smoke: browser-technical-live-preflight
	@REAL_SCENARIO_FILE="$${BROWSER_TECHNICAL_SCENARIO_FILE}" \
	REAL_ACCEPTANCE_AGENT_ID=security-operations-expert \
	$(CONTAINER_ACCEPTANCE) --profile core -- $(MAKE) --no-print-directory \
		_ui-playground-cancel-smoke AGENT_GOV_FORMAL_BROWSER_ACCEPTANCE=0 \
		BROWSER=both REAL_ACCEPTANCE_AGENT_ID=security-operations-expert

ui-agent-candidate-technical-smoke: technical-live-preflight
	$(CONTAINER_ACCEPTANCE) --profile langfuse -- $(MAKE) --no-print-directory \
		_ui-agent-candidate-technical-smoke BROWSER=both

_ui-playground-cancel-smoke:
	@$(REQUIRE_CONTAINER_ACCEPTANCE)
	@frontend_port=$${FRONTEND_HOST_PORT:-$$($${AGENTGOV_ACCEPTANCE_AWK:?missing bound acceptance awk} -F= '$$1 == "FRONTEND_HOST_PORT" {sub(/^[^=]*=/, ""); print; exit}' "$(COMPOSE_ENV_FILE)" 2>/dev/null)}; \
	host_port=$${HOST_PORT:-$$($${AGENTGOV_ACCEPTANCE_AWK:?missing bound acceptance awk} -F= '$$1 == "HOST_PORT" {sub(/^[^=]*=/, ""); print; exit}' "$(COMPOSE_ENV_FILE)" 2>/dev/null)}; \
	api_key=$$($${AGENTGOV_ACCEPTANCE_AWK:?missing bound acceptance awk} -F= '$$1 == "FRONTEND_RUNTIME_API_KEY" || $$1 == "API_KEY" {sub(/^[^=]*=/, ""); print; exit}' "$(COMPOSE_ENV_FILE)" 2>/dev/null); \
	RUNTIME_UI_BASE="http://localhost:$${frontend_port:-50401}" \
	RUNTIME_API_BASE="http://localhost:$${host_port:-50400}" \
	RUNTIME_API_KEY="$$api_key" \
	AGENT_GOV_FORMAL_BROWSER_ACCEPTANCE="$${AGENT_GOV_FORMAL_BROWSER_ACCEPTANCE:-0}" \
	BROWSER="$${BROWSER:-both}" \
	VERIFY_SCREENSHOT_DIR="$${VERIFY_SCREENSHOT_DIR:-/tmp/agentgov-ui-playground-cancel}" \
	"$${AGENTGOV_ACCEPTANCE_NODE:?missing bound acceptance node}" scripts/verify_playground_cancel.mjs

_ui-agent-candidate-technical-smoke:
	@$(REQUIRE_CONTAINER_ACCEPTANCE)
	@frontend_port=$${FRONTEND_HOST_PORT:-$$($${AGENTGOV_ACCEPTANCE_AWK:?missing bound acceptance awk} -F= '$$1 == "FRONTEND_HOST_PORT" {sub(/^[^=]*=/, ""); print; exit}' "$(COMPOSE_ENV_FILE)" 2>/dev/null)}; \
	host_port=$${HOST_PORT:-$$($${AGENTGOV_ACCEPTANCE_AWK:?missing bound acceptance awk} -F= '$$1 == "HOST_PORT" {sub(/^[^=]*=/, ""); print; exit}' "$(COMPOSE_ENV_FILE)" 2>/dev/null)}; \
	api_key=$$($${AGENTGOV_ACCEPTANCE_AWK:?missing bound acceptance awk} -F= '$$1 == "FRONTEND_RUNTIME_API_KEY" || $$1 == "API_KEY" {sub(/^[^=]*=/, ""); print; exit}' "$(COMPOSE_ENV_FILE)" 2>/dev/null); \
	RUNTIME_UI_BASE="http://localhost:$${frontend_port:-50401}" \
	RUNTIME_API_BASE="http://localhost:$${host_port:-50400}" \
	RUNTIME_API_KEY="$$api_key" \
	BROWSER="$${BROWSER:-both}" \
	"$${AGENTGOV_ACCEPTANCE_NODE:?missing bound acceptance node}" scripts/verify_agent_candidate_lifecycle.mjs

.PHONY: langfuse-env
langfuse-env:
	$(PYTHON_RUN) scripts/initialize_langfuse_env.py --env-file "$(COMPOSE_ENV_FILE)"

images-prepare:
	$(SELECTED_ENV_RUNNER) --operation images-prepare

langfuse-prepare:
	$(SELECTED_ENV_RUNNER) --operation langfuse-prepare

langfuse-up:
	$(SELECTED_ENV_RUNNER) --operation langfuse-up

langfuse-stop:
	$(SELECTED_ENV_RUNNER) --operation langfuse-stop

langfuse-logs:
	$(SELECTED_ENV_RUNNER) --operation langfuse-logs

langfuse-smoke: live-acceptance-preflight
	$(CONTAINER_ACCEPTANCE) --profile langfuse -- $(MAKE) --no-print-directory _langfuse-smoke

_langfuse-smoke:
	@$(REQUIRE_CONTAINER_ACCEPTANCE)
	$(PYTHON_RUN) scripts/langfuse_smoke.py \
		--env-file "$(COMPOSE_ENV_FILE)" \
		--scenario-file "$${REAL_SCENARIO_FILE}" \
		--agent-id "$${REAL_ACCEPTANCE_AGENT_ID}"

runtime-bootstrap:
	$(SELECTED_ENV_RUNNER) --operation runtime-bootstrap

runtime-recreate:
	@case "$(RUNTIME_RECREATE_REQUIRE_IDLE)" in ""|0|1) ;; *) echo "RUNTIME_RECREATE_REQUIRE_IDLE must be 0 or 1" >&2; exit 2;; esac
	$(SELECTED_ENV_RUNNER) --operation runtime-recreate $(if $(filter 1,$(RUNTIME_RECREATE_REQUIRE_IDLE)),--require-idle,)

.PHONY: runtime-prepare-harnesses
runtime-prepare-harnesses:
	$(SELECTED_ENV_RUNNER) --operation runtime-prepare-harnesses

runtime-validate:
	$(SELECTED_ENV_RUNNER) --operation runtime-validate

cutover-check:
	$(SELECTED_ENV_RUNNER) --operation check

cutover-inspect:
	$(PYTHON_RUN) scripts/agentscope_atomic_cutover.py inspect \
		--env-file "$(COMPOSE_ENV_FILE)" --require-current-or-empty

runtime-clean:
	$(SELECTED_ENV_RUNNER) --operation runtime-clean

.PHONY: runtime-workspace-gc runtime-workspace-gc-apply local-debug-run agent-candidate-compare
runtime-workspace-gc:
	$(SELECTED_ENV_RUNNER) --operation runtime-workspace-gc

runtime-workspace-gc-apply:
	$(SELECTED_ENV_RUNNER) --operation runtime-workspace-gc-apply

.PHONY: runtime-workspace-reclaim-live-smoke
runtime-workspace-reclaim-live-smoke:
	@$(ACCEPTANCE_PYTHON) scripts/run_workspace_reclaim_acceptance.py --env-file "$(COMPOSE_ENV_FILE)"

# 人工对照使用所选真实环境；先加载当前代码和配置，不生成发布门证据。
agent-candidate-compare:
	@test -n "$(COMPARE_AGENT_ID)" -a -n "$(COMPARE_CHANGE_SET_ID)" -a -n "$(COMPARE_SCENARIOS)" -a -n "$(COMPARE_REPORT)" || { echo '请提供 COMPARE_AGENT_ID、COMPARE_CHANGE_SET_ID、COMPARE_SCENARIOS、COMPARE_REPORT'; exit 2; }
	+$(MAKE) --no-print-directory build
	+$(MAKE) --no-print-directory all-up COMPOSE_UP_FLAGS=--force-recreate
	$(PYTHON_RUN) -m scripts.compare_agent_candidate --env-file "$(COMPOSE_ENV_FILE)" \
		--agent-id "$(COMPARE_AGENT_ID)" --change-set-id "$(COMPARE_CHANGE_SET_ID)" \
		--scenarios "$(COMPARE_SCENARIOS)" --report "$(COMPARE_REPORT)"

runtime-migrate-workspace-tests-scan:
	$(SELECTED_ENV_RUNNER) --operation runtime-migrate-scan

runtime-migrate-workspace-tests:
	$(SELECTED_ENV_RUNNER) --operation runtime-migrate

local-debug-env:
	cp -n docker/.env.local-debug.example docker/.env.local-debug || true
	$(PYTHON_RUN) scripts/initialize_runtime_shared_secret.py --env-file docker/.env.local-debug

local-debug-run:
	$(PYTHON_RUN) scripts/run_local_debug.py --api-port "$(LOCAL_DEBUG_API_PORT)" --ui-port "$(LOCAL_DEBUG_UI_PORT)"

local-debug-bootstrap: local-debug-env
	$(PYTHON_RUN) scripts/bootstrap_runtime_volume.py --env-file docker/.env.local-debug

local-debug-validate: local-debug-env
	$(PYTHON_RUN) scripts/bootstrap_runtime_volume.py --env-file docker/.env.local-debug --dry-run
	$(PYTHON_RUN) scripts/check_agentscope_cutover.py

local-debug-clean: local-debug-env
	$(PYTHON_RUN) scripts/cleanup_runtime_artifacts.py --env-file docker/.env.local-debug --runtime-volume-mode local-debug --runtime-artifacts

runtime-bootstrap-scan:
	$(PYTHON_RUN) scripts/runtime_bootstrap_safety.py verify docker/runtime-bootstrap

runtime-bootstrap-clean:
	$(PYTHON_RUN) scripts/cleanup_runtime_artifacts.py --bootstrap-artifacts

clean-runtime-artifacts: runtime-clean local-debug-clean runtime-bootstrap-clean

smoke:
	$(CONTAINER_ACCEPTANCE) --profile core -- $(MAKE) --no-print-directory _smoke

_smoke:
	@$(REQUIRE_CONTAINER_ACCEPTANCE)
	@$(PYTHON_RUN) scripts/diagnose_runtime_health.py --env-file "$(COMPOSE_ENV_FILE)" --require-ready

container-core-smoke:
	$(CONTAINER_ACCEPTANCE) --profile core -- $(MAKE) --no-print-directory _container-core-smoke

_container-core-smoke:
	@$(REQUIRE_CONTAINER_ACCEPTANCE)
	+@$(MAKE) --no-print-directory --keep-going --jobs=3 _smoke _ui-smoke _container-openapi-check

container-live-test: live-acceptance-preflight
	$(CONTAINER_ACCEPTANCE) --profile langfuse -- $(MAKE) --no-print-directory _container-live-test

_container-live-test:
	@$(REQUIRE_CONTAINER_ACCEPTANCE)
	$(PYTHON_RUN) scripts/run_agentscope_live_acceptance.py \
		--env-file "$(COMPOSE_ENV_FILE)" \
		--scenario-file "$${REAL_SCENARIO_FILE}" \
		--agent-id "$${REAL_ACCEPTANCE_AGENT_ID}" \
		--require-trace-complete

container-technical-live-smoke: technical-live-preflight
	$(CONTAINER_ACCEPTANCE) --profile langfuse -- $(MAKE) --no-print-directory _container-technical-live-smoke

_container-technical-live-smoke:
	@$(REQUIRE_CONTAINER_ACCEPTANCE)
	$(PYTHON_RUN) scripts/run_agentscope_live_acceptance.py \
		--env-file "$(COMPOSE_ENV_FILE)" \
		--scenario-file "$${TECHNICAL_SCENARIO_FILE}" \
		--technical-integration-seed \
		--require-trace-complete

container-mcp-technical-smoke: mcp-technical-live-preflight
	$(CONTAINER_ACCEPTANCE) --profile core -- $(MAKE) --no-print-directory _container-mcp-technical-smoke

_container-mcp-technical-smoke:
	@$(REQUIRE_CONTAINER_ACCEPTANCE)
	$(PYTHON_RUN) scripts/run_agentscope_live_acceptance.py \
		--env-file "$(COMPOSE_ENV_FILE)" \
		--scenario-file "$${TECHNICAL_SCENARIO_FILE}" \
		--mcp-technical-seed \
		--capability mcp_readonly --runs 1 --concurrency 1

container-release-candidate: live-acceptance-preflight
	$(CONTAINER_ACCEPTANCE) --profile langfuse -- $(MAKE) --no-print-directory _container-release-candidate

_container-release-candidate:
	@$(REQUIRE_CONTAINER_ACCEPTANCE)
	$(PYTHON_RUN) scripts/check_no_test_doubles.py
	$(PYTHON_RUN) scripts/run_agentscope_live_acceptance.py \
		--env-file "$(COMPOSE_ENV_FILE)" \
		--scenario-file "$${REAL_SCENARIO_FILE}" \
		--agent-id "$${REAL_ACCEPTANCE_AGENT_ID}" \
		--runs 50 --concurrency 10 --require-trace-complete
	$(PYTHON_RUN) scripts/run_agentgov_testkit_live.py \
		--env-file "$(COMPOSE_ENV_FILE)" \
		--scenario-file "$${REAL_SCENARIO_FILE}" \
		--agent-id "$${REAL_ACCEPTANCE_AGENT_ID}"
	+@$(MAKE) --no-print-directory _ui-playground-cancel-smoke \
		AGENT_GOV_FORMAL_BROWSER_ACCEPTANCE=1 BROWSER=both
	+@$(MAKE) --no-print-directory _ui-feedback-smoke \
		AGENT_GOV_FORMAL_BROWSER_ACCEPTANCE=1 BROWSER=both
	$(PYTHON_RUN) scripts/check_test_quality_policy.py --manifest-only --policy "$(QUALITY_POLICY)" \
		--fail-on-open-gaps --gap-lane container-live-acceptance

compose-diagnose:
	$(SELECTED_ENV_RUNNER) --operation compose-diagnose

codex-guard:
	$(PYTHON_RUN) scripts/check_no_test_doubles.py
	$(PYTHON_RUN) .codex/skills/codex-config-optimizer/scripts/audit_codex_config.py --fail
	$(PYTHON_RUN) scripts/check_codex_governance.py --mode fail $(GOVERNANCE_BASE_REF_ARG)
	$(PYTHON_RUN) scripts/check_stage_language.py
	$(PYTHON_RUN) scripts/check_version_consistency.py
	$(PYTHON_RUN) scripts/check_agentscope_cutover.py
	$(PYTHON_RUN) scripts/audit_openapi_contract.py --fail
	$(PYTHON_RUN) scripts/agentscope_native_contract.py --check
	AGENTGOV_PYTHON="$(abspath $(PYTHON))" bash scripts/check_openapi_type_drift.sh
	$(PYTHON_RUN) scripts/check_docs_governance.py
	$(PYTHON_RUN) scripts/check_test_quality_policy.py --manifest-only --policy "$(QUALITY_POLICY)" \
		--fail-on-open-gaps --gap-lane main-flow

test-double-check:
	$(PYTHON_RUN) scripts/check_no_test_doubles.py

openapi-contract-check:
	$(PYTHON_RUN) scripts/audit_openapi_contract.py --fail

agentscope-contract-check:
	$(PYTHON_RUN) scripts/agentscope_native_contract.py --check

openapi-type-drift-check:
	AGENTGOV_PYTHON="$(abspath $(PYTHON))" bash scripts/check_openapi_type_drift.sh

container-openapi-check:
	$(CONTAINER_ACCEPTANCE) --profile core -- $(MAKE) --no-print-directory _container-openapi-check

_container-openapi-check:
	@$(REQUIRE_CONTAINER_ACCEPTANCE)
	@host_port=$${HOST_PORT:-$$($${AGENTGOV_ACCEPTANCE_AWK:?missing bound acceptance awk} -F= '$$1 == "HOST_PORT" {sub(/^[^=]*=/, ""); print; exit}' "$(COMPOSE_ENV_FILE)" 2>/dev/null)}; \
	api_base=$${API_BASE:-$$($${AGENTGOV_ACCEPTANCE_AWK:?missing bound acceptance awk} -F= '$$1 == "API_BASE" {sub(/^[^=]*=/, ""); print; exit}' "$(COMPOSE_ENV_FILE)" 2>/dev/null)}; \
	api_base=$${api_base:-http://localhost:$${host_port:-50400}}; \
	$(PYTHON_RUN) scripts/audit_openapi_contract.py --base-url "$$api_base" --compare-local --fail

sync-version:
	@v=$$(cat VERSION); sed -i '0,/"version":/s/"version": *"[^"]*"/"version": "'$$v'"/' frontend/package.json; echo "synced frontend/package.json -> $$v"

# 发布点打 tag：从单一真相源 VERSION 创建 v<VERSION> 并推 origin；已存在则拒绝（提示先 bump）。
tag:
	@v=$$(cat VERSION); if git rev-parse "v$$v" >/dev/null 2>&1; then echo "tag v$$v 已存在（发布点请先 bump VERSION）"; exit 1; fi; git tag -a "v$$v" -m "release v$$v" && git push origin "v$$v" && echo "tagged + pushed v$$v"

ruff-check:
	$(PYTHON_RUN) -m ruff check $(PYTHON_TYPECHECK_TARGETS)

ruff-format-check:
	$(PYTHON_RUN) -m ruff format --check $(PYTHON_TYPECHECK_TARGETS)

pyright:
	$(PYTHON_RUN) -m pyright

typecheck: ruff-check ruff-format-check pyright

test-backend:
	mkdir -p "$(BACKEND_TEST_ARTIFACT_DIR)"
	$(PYTHON_RUN) -m compileall app
	$(PYTHON_RUN) scripts/run_test_lane.py --policy "$(QUALITY_POLICY)" --lane main-full --artifact-dir "$(BACKEND_TEST_ARTIFACT_DIR)"
	$(PYTHON_RUN) scripts/check_docs_governance.py --collect-pytest

test:
	+$(MAKE) --no-print-directory codex-guard
	+$(MAKE) --no-print-directory test-backend

coverage:
	$(PYTHON_RUN) scripts/run_test_lane.py --policy "$(QUALITY_POLICY)" --lane main-full --artifact-dir "$(BACKEND_TEST_ARTIFACT_DIR)"

main-flow-test:
	$(PYTHON_RUN) scripts/run_main_flow_tests.py --policy "$(QUALITY_POLICY)"

main-flow-ui-test:
	$(PYTHON_RUN) scripts/run_main_flow_tests.py --policy "$(QUALITY_POLICY)" --ui-only --artifact-root "$(TEST_ARTIFACT_ROOT)/frontend-ui"

main-flow-live-test: live-acceptance-preflight
	$(CONTAINER_ACCEPTANCE) --profile langfuse -- $(MAKE) --no-print-directory _main-flow-live-test

_main-flow-live-test:
	@$(REQUIRE_CONTAINER_ACCEPTANCE)
	$(PYTHON_RUN) scripts/run_main_flow_live_targets.py --policy "$(QUALITY_POLICY)"

mutation-test:
	$(PYTHON_RUN) scripts/run_mutation_lane.py --policy "$(QUALITY_POLICY)" --artifact-dir "$(TEST_ARTIFACT_ROOT)/mutation"
