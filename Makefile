VENV ?= .venv
PYTHON ?= $(VENV)/bin/python
UV ?= uv
PYTHON_RUN ?= $(PYTHON)
COMPOSE_ENV_FILE ?= docker/.env
export COMPOSE_ENV_FILE
export AGENT_GOV_COMPOSE_ENV_FILE := $(abspath $(COMPOSE_ENV_FILE))
COMPOSE ?= docker compose --env-file $(COMPOSE_ENV_FILE) -f docker/docker-compose.yml
LANGFUSE_COMPOSE = $(COMPOSE) -f docker/docker-compose.langfuse.yml
COMPOSE_UP_FLAGS ?=
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
	scripts/check_agentscope_cutover.py \
	scripts/codex_governance_typed_output.py \
	scripts/check_test_quality_policy.py \
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
	scripts/run_agentscope_live_acceptance.py \
	scripts/runtime_acceptance_fixture.py \
	scripts/agentscope_atomic_cutover.py \
	scripts/agentscope_atomic_cutover_evidence.py \
	scripts/agentscope_atomic_cutover_recovery.py \
	scripts/agentscope_atomic_cutover_support.py \
	scripts/agentscope_atomic_cutover_types.py \
	scripts/langfuse_smoke.py

TEST_ARTIFACT_ROOT ?= artifacts/test-quality
BACKEND_TEST_ARTIFACT_DIR ?= $(TEST_ARTIFACT_ROOT)/backend-main-full
QUALITY_POLICY ?= tests/quality_policy.json
GOVERNANCE_BASE_REF ?=
GOVERNANCE_BASE_REF_ARG := $(if $(strip $(GOVERNANCE_BASE_REF)),--base-ref $(GOVERNANCE_BASE_REF),)
CONTAINER_ACCEPTANCE := $(PYTHON_RUN) scripts/run_container_acceptance.py --env-file "$(COMPOSE_ENV_FILE)"
REQUIRE_CONTAINER_ACCEPTANCE = [ "$$AGENT_GOV_CONTAINER_ACCEPTANCE_ACTIVE" = "1" ] && [ -n "$$AGENT_GOV_ACCEPTANCE_RUN_ID" ] || { echo "Use the public container acceptance Make target." >&2; exit 1; }

.PHONY: setup build public-bind-check up all-up down logs test test-backend coverage main-flow-test main-flow-ui-test mutation-test openapi-contract-check openapi-type-drift-check container-core-smoke container-openapi-check container-live-test smoke compose-diagnose codex-guard cutover-check cutover-inspect cutover-prepare cutover-execute cutover-finalize cutover-recover-finalize cutover-restore sync-version tag ruff-check ruff-format-check pyright typecheck ui-build ui-up ui-stop ui-logs ui-smoke ui-design-parity ui-feedback-smoke ui-playground-cancel-smoke langfuse-prepare langfuse-up langfuse-stop langfuse-logs langfuse-smoke runtime-bootstrap runtime-validate runtime-clean runtime-migrate-workspace-tests runtime-migrate-workspace-tests-scan local-debug-env local-debug-bootstrap local-debug-validate local-debug-clean runtime-bootstrap-scan runtime-bootstrap-clean clean-runtime-artifacts _runtime-health-diagnose _container-core-smoke _container-openapi-check _container-live-test _smoke _ui-smoke _ui-feedback-smoke _ui-playground-cancel-smoke _langfuse-smoke

setup:
	cp -n docker/.env.example docker/.env || true
	@if ! command -v $(UV) >/dev/null 2>&1; then echo "uv is required. Install uv before running make setup." >&2; exit 1; fi
	$(UV) venv $(VENV) --python 3.11
	$(UV) pip install --python $(PYTHON) -r requirements.txt -e packages/agentgov-testkit

build:
	$(COMPOSE) build

public-bind-check:
	$(PYTHON_RUN) scripts/check_public_bind.py --env-file "$(COMPOSE_ENV_FILE)"

up: public-bind-check cutover-inspect
	@$(MAKE) --no-print-directory runtime-bootstrap
	@$(MAKE) --no-print-directory runtime-prepare-harnesses
	@if ! $(COMPOSE) up -d --wait $(COMPOSE_UP_FLAGS); then \
		$(MAKE) --no-print-directory compose-diagnose; \
		exit 1; \
	fi
	@$(MAKE) --no-print-directory _runtime-health-diagnose

all-up: public-bind-check cutover-inspect
	@$(MAKE) --no-print-directory runtime-bootstrap
	@$(MAKE) --no-print-directory runtime-prepare-harnesses
	@$(MAKE) --no-print-directory langfuse-prepare
	@if ! $(LANGFUSE_COMPOSE) --profile langfuse up -d --wait --remove-orphans $(COMPOSE_UP_FLAGS); then \
		COMPOSE_PROFILE=langfuse $(MAKE) --no-print-directory compose-diagnose; \
		exit 1; \
	fi
	@$(MAKE) --no-print-directory _runtime-health-diagnose

_runtime-health-diagnose:
	@python_bin="$(PYTHON)"; \
	if [ ! -x "$$python_bin" ]; then python_bin=$$(command -v python3 2>/dev/null || true); fi; \
	if [ -z "$$python_bin" ]; then \
		echo "Runtime readiness verification failed: neither $(PYTHON) nor python3 is available." >&2; exit 1; \
	else \
		"$$python_bin" scripts/diagnose_runtime_health.py --env-file "$(COMPOSE_ENV_FILE)" --require-ready; \
	fi

down:
	$(COMPOSE) down

logs:
	$(COMPOSE) logs -f agent-gov-api agentscope-runtime

ui-build:
	$(COMPOSE) build agent-gov-ui

ui-up: public-bind-check
	$(COMPOSE) up -d agent-gov-ui

ui-stop:
	$(COMPOSE) stop agent-gov-ui

ui-logs:
	$(COMPOSE) logs -f agent-gov-ui

ui-smoke:
	$(CONTAINER_ACCEPTANCE) --profile core -- $(MAKE) --no-print-directory _ui-smoke

_ui-smoke:
	@$(REQUIRE_CONTAINER_ACCEPTANCE)
	@frontend_port=$${FRONTEND_HOST_PORT:-$$(awk -F= '$$1 == "FRONTEND_HOST_PORT" {sub(/^[^=]*=/, ""); print; exit}' "$(COMPOSE_ENV_FILE)" 2>/dev/null)}; \
	frontend_url=$${FRONTEND_URL:-http://localhost:$${frontend_port:-50401}}; \
	i=1; \
	while [ $$i -le 30 ]; do \
		if curl -fsS "$$frontend_url" >/dev/null; then \
			echo "Frontend OK: $$frontend_url"; \
			exit 0; \
		fi; \
		sleep 1; \
		i=$$((i + 1)); \
	done; \
	echo "Frontend failed: $$frontend_url" >&2; \
	exit 1

ui-design-parity:
	pnpm --dir frontend run verify:design-parity

ui-feedback-smoke:
	$(CONTAINER_ACCEPTANCE) --profile langfuse -- $(MAKE) --no-print-directory _ui-feedback-smoke

_ui-feedback-smoke:
	@$(REQUIRE_CONTAINER_ACCEPTANCE)
	@frontend_port=$${FRONTEND_HOST_PORT:-$$(awk -F= '$$1 == "FRONTEND_HOST_PORT" {sub(/^[^=]*=/, ""); print; exit}' "$(COMPOSE_ENV_FILE)" 2>/dev/null)}; \
	host_port=$${HOST_PORT:-$$(awk -F= '$$1 == "HOST_PORT" {sub(/^[^=]*=/, ""); print; exit}' "$(COMPOSE_ENV_FILE)" 2>/dev/null)}; \
	api_key=$$(awk -F= '$$1 == "FRONTEND_RUNTIME_API_KEY" || $$1 == "API_KEY" {sub(/^[^=]*=/, ""); print; exit}' "$(COMPOSE_ENV_FILE)" 2>/dev/null); \
	RUNTIME_UI_BASE="http://localhost:$${frontend_port:-50401}" \
	RUNTIME_API_BASE="http://localhost:$${host_port:-50400}" \
	RUNTIME_API_KEY="$$api_key" \
	VERIFY_SCREENSHOT_DIR="$${VERIFY_SCREENSHOT_DIR:-/tmp/agentgov-ui-feedback-smoke}" \
	pnpm --dir frontend run verify:real-container:impl

ui-playground-cancel-smoke:
	$(CONTAINER_ACCEPTANCE) --profile core -- $(MAKE) --no-print-directory _ui-playground-cancel-smoke

_ui-playground-cancel-smoke:
	@$(REQUIRE_CONTAINER_ACCEPTANCE)
	@frontend_port=$${FRONTEND_HOST_PORT:-$$(awk -F= '$$1 == "FRONTEND_HOST_PORT" {sub(/^[^=]*=/, ""); print; exit}' "$(COMPOSE_ENV_FILE)" 2>/dev/null)}; \
	host_port=$${HOST_PORT:-$$(awk -F= '$$1 == "HOST_PORT" {sub(/^[^=]*=/, ""); print; exit}' "$(COMPOSE_ENV_FILE)" 2>/dev/null)}; \
	api_key=$$(awk -F= '$$1 == "FRONTEND_RUNTIME_API_KEY" || $$1 == "API_KEY" {sub(/^[^=]*=/, ""); print; exit}' "$(COMPOSE_ENV_FILE)" 2>/dev/null); \
	RUNTIME_UI_BASE="http://localhost:$${frontend_port:-50401}" \
	RUNTIME_API_BASE="http://localhost:$${host_port:-50400}" \
	RUNTIME_API_KEY="$$api_key" \
	VERIFY_SCREENSHOT_DIR="$${VERIFY_SCREENSHOT_DIR:-/tmp/agentgov-ui-playground-cancel}" \
	pnpm --dir frontend run verify:playground-cancel

.PHONY: langfuse-env
langfuse-env:
	$(PYTHON_RUN) scripts/initialize_langfuse_env.py --env-file "$(COMPOSE_ENV_FILE)"

langfuse-prepare:
	$(LANGFUSE_COMPOSE) --profile langfuse-maintenance run --rm --no-deps -T --pull missing langfuse-volume-init

langfuse-up: public-bind-check langfuse-prepare
	$(LANGFUSE_COMPOSE) --profile langfuse up -d --wait --remove-orphans $(COMPOSE_UP_FLAGS) langfuse-postgres langfuse-clickhouse langfuse-redis langfuse-minio langfuse-web langfuse-worker

langfuse-stop:
	$(LANGFUSE_COMPOSE) --profile langfuse stop langfuse-worker langfuse-web langfuse-minio langfuse-redis langfuse-clickhouse langfuse-postgres

langfuse-logs:
	$(LANGFUSE_COMPOSE) --profile langfuse logs -f langfuse-web langfuse-worker

langfuse-smoke:
	$(CONTAINER_ACCEPTANCE) --profile langfuse -- $(MAKE) --no-print-directory _langfuse-smoke

_langfuse-smoke:
	@$(REQUIRE_CONTAINER_ACCEPTANCE)
	$(PYTHON_RUN) scripts/langfuse_smoke.py --env-file "$(COMPOSE_ENV_FILE)"

runtime-bootstrap:
	$(PYTHON_RUN) scripts/bootstrap_runtime_volume.py --env-file "$(COMPOSE_ENV_FILE)"

.PHONY: runtime-prepare-harnesses
runtime-prepare-harnesses: cutover-inspect
	$(COMPOSE) run --rm --no-deps -T --pull never --entrypoint python agent-gov-api -m app.runtime.published_harness_preparation

runtime-validate:
	$(PYTHON_RUN) scripts/bootstrap_runtime_volume.py --env-file "$(COMPOSE_ENV_FILE)" --dry-run
	$(PYTHON_RUN) scripts/check_agentscope_cutover.py

cutover-check: runtime-validate
	$(COMPOSE) config --services

cutover-inspect:
	$(PYTHON_RUN) scripts/agentscope_atomic_cutover.py inspect \
		--env-file "$(COMPOSE_ENV_FILE)" --require-current-or-empty

cutover-prepare:
	@test -n "$(CUTOVER_ROLLBACK_COMPOSE_FILE)" || { \
		echo "CUTOVER_ROLLBACK_COMPOSE_FILE is required (path to the still-live legacy Compose file)" >&2; \
		exit 2; \
	}
	$(PYTHON_RUN) scripts/agentscope_atomic_cutover.py prepare \
		--env-file "$(COMPOSE_ENV_FILE)" --backup-dir "$(CUTOVER_BACKUP_DIR)" \
		--rollback-compose-file "$(CUTOVER_ROLLBACK_COMPOSE_FILE)" \
		--confirmation-token "$(CUTOVER_CONFIRMATION_TOKEN)"

cutover-execute:
	$(PYTHON_RUN) scripts/agentscope_atomic_cutover.py execute \
		--manifest "$(CUTOVER_MANIFEST)" --confirmation-token "$(CUTOVER_CONFIRMATION_TOKEN)"

cutover-finalize:
	$(PYTHON_RUN) scripts/agentscope_atomic_cutover.py finalize \
		--manifest "$(CUTOVER_MANIFEST)" --evidence-file "$(CUTOVER_EVIDENCE_FILE)" \
		--confirmation-token "$(CUTOVER_CONFIRMATION_TOKEN)"

cutover-recover-finalize:
	$(PYTHON_RUN) scripts/agentscope_atomic_cutover.py recover-finalize \
		--manifest "$(CUTOVER_MANIFEST)" \
		$(if $(strip $(CUTOVER_EVIDENCE_FILE)),--evidence-file "$(CUTOVER_EVIDENCE_FILE)") \
		--confirmation-token "$(CUTOVER_CONFIRMATION_TOKEN)"

cutover-restore:
	$(PYTHON_RUN) scripts/agentscope_atomic_cutover.py restore \
		--manifest "$(CUTOVER_MANIFEST)" --confirmation-token "$(CUTOVER_CONFIRMATION_TOKEN)"

runtime-clean:
	$(PYTHON_RUN) scripts/cleanup_runtime_artifacts.py --env-file "$(COMPOSE_ENV_FILE)" --runtime-artifacts

runtime-migrate-workspace-tests-scan:
	$(PYTHON_RUN) scripts/migrate_workspace_test_assets.py --env-file "$(COMPOSE_ENV_FILE)"

runtime-migrate-workspace-tests:
	$(PYTHON_RUN) scripts/migrate_workspace_test_assets.py --env-file "$(COMPOSE_ENV_FILE)" --apply

local-debug-env:
	cp -n docker/.env.local-debug.example docker/.env.local-debug || true

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

container-live-test:
	@case "$${REQUIRE_LIVE_RUNTIME:-}" in 1|true|yes|on) ;; *) \
		echo "Set REQUIRE_LIVE_RUNTIME=1 to authorize real provider calls." >&2; exit 2 ;; \
	esac
	$(CONTAINER_ACCEPTANCE) --profile langfuse -- $(MAKE) --no-print-directory _container-live-test

_container-live-test:
	@$(REQUIRE_CONTAINER_ACCEPTANCE)
	$(PYTHON_RUN) scripts/run_agentscope_live_acceptance.py \
		--env-file "$(COMPOSE_ENV_FILE)" $(LIVE_ACCEPTANCE_ARGS)

compose-diagnose:
	@bash scripts/compose_diagnose.sh

codex-guard:
	$(PYTHON_RUN) .codex/skills/codex-config-optimizer/scripts/audit_codex_config.py --fail
	$(PYTHON_RUN) scripts/check_codex_governance.py --mode fail $(GOVERNANCE_BASE_REF_ARG)
	$(PYTHON_RUN) scripts/check_stage_language.py
	$(PYTHON_RUN) scripts/check_version_consistency.py
	$(PYTHON_RUN) scripts/check_agentscope_cutover.py
	$(PYTHON_RUN) scripts/audit_openapi_contract.py --fail
	AGENTGOV_PYTHON="$(abspath $(PYTHON))" bash scripts/check_openapi_type_drift.sh
	$(PYTHON_RUN) scripts/check_docs_governance.py
	$(PYTHON_RUN) scripts/check_test_quality_policy.py --manifest-only --policy $(QUALITY_POLICY)

openapi-contract-check:
	$(PYTHON_RUN) scripts/audit_openapi_contract.py --fail

openapi-type-drift-check:
	AGENTGOV_PYTHON="$(abspath $(PYTHON))" bash scripts/check_openapi_type_drift.sh

container-openapi-check:
	$(CONTAINER_ACCEPTANCE) --profile core -- $(MAKE) --no-print-directory _container-openapi-check

_container-openapi-check:
	@$(REQUIRE_CONTAINER_ACCEPTANCE)
	@host_port=$${HOST_PORT:-$$(awk -F= '$$1 == "HOST_PORT" {sub(/^[^=]*=/, ""); print; exit}' "$(COMPOSE_ENV_FILE)" 2>/dev/null)}; \
	api_base=$${API_BASE:-$$(awk -F= '$$1 == "API_BASE" {sub(/^[^=]*=/, ""); print; exit}' "$(COMPOSE_ENV_FILE)" 2>/dev/null)}; \
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
	mkdir -p $(BACKEND_TEST_ARTIFACT_DIR)
	$(PYTHON_RUN) -m compileall app
	$(PYTHON_RUN) scripts/run_test_lane.py --policy $(QUALITY_POLICY) --lane main-full --artifact-dir $(BACKEND_TEST_ARTIFACT_DIR)
	$(PYTHON_RUN) scripts/check_docs_governance.py --collect-pytest

test:
	+$(MAKE) --no-print-directory codex-guard
	+$(MAKE) --no-print-directory test-backend

coverage:
	$(PYTHON_RUN) scripts/run_test_lane.py --policy $(QUALITY_POLICY) --lane main-full --artifact-dir $(BACKEND_TEST_ARTIFACT_DIR)

main-flow-test:
	$(PYTHON_RUN) scripts/run_main_flow_tests.py --policy $(QUALITY_POLICY)

main-flow-ui-test:
	$(PYTHON_RUN) scripts/run_main_flow_tests.py --policy $(QUALITY_POLICY) --ui-only --artifact-root $(TEST_ARTIFACT_ROOT)/frontend-ui

mutation-test:
	$(PYTHON_RUN) scripts/run_mutation_lane.py --policy $(QUALITY_POLICY) --artifact-dir $(TEST_ARTIFACT_ROOT)/mutation
