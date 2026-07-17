VENV ?= .venv
PYTHON ?= $(VENV)/bin/python
UV ?= uv
LITELLM_LOCAL_MODEL_COST_MAP ?= True
PYTHON_RUN ?= LITELLM_LOCAL_MODEL_COST_MAP=$(LITELLM_LOCAL_MODEL_COST_MAP) $(PYTHON)
COMPOSE_ENV_FILE ?= docker/.env
export COMPOSE_ENV_FILE
export AGENT_GOV_COMPOSE_ENV_FILE := $(abspath $(COMPOSE_ENV_FILE))
COMPOSE ?= docker compose --env-file $(COMPOSE_ENV_FILE) -f docker/docker-compose.yml
# 版本唯一真相源：根 VERSION 文件。导出给 compose，让镜像 tag ${APP_VERSION} 派生（build/up 自动生效）。
export APP_VERSION := $(shell cat $(CURDIR)/VERSION 2>/dev/null || echo dev)
PYTHON_TYPECHECK_TARGETS := \
	app/openapi_contract.py \
	app/routers/agent_workspace_packages.py \
	app/routers/claude_user_input.py \
	app/routers/conversations.py \
	app/routers/responses.py \
	app/runtime/api_auth.py \
	app/runtime/advisory_lock.py \
	app/runtime/agent_git_raw_storage.py \
	app/runtime/agent_job_types.py \
	app/runtime/agent_workspace_package_schemas.py \
	app/runtime/business_agent_seed_catalog.py \
	app/runtime/business_agent_workspace.py \
	app/runtime/claude_prompt_suggestions.py \
	app/runtime/claude_runtime_permissions.py \
	app/runtime/claude_runtime_stream.py \
	app/runtime/claude_user_input_service.py \
	app/runtime/output_formatter.py \
	app/runtime/agent_job_runner.py \
	app/runtime/claude_runtime.py \
	app/runtime/model_provider.py \
	app/runtime/model_provider_capabilities.py \
	app/runtime/openai_responses_adapter.py \
	app/runtime/openai_responses_schemas.py \
	app/runtime/openai_responses_stream.py \
	app/runtime/managed_agent_policy.py \
	app/runtime/runtime_coordination.py \
	app/runtime/runtime_initialization.py \
	app/runtime/service_launcher.py \
	app/runtime/records/response_disposition_records.py \
	app/runtime/response_disposition_control.py \
	app/runtime/response_disposition_db.py \
	app/runtime/response_disposition_stream.py \
	app/runtime/stores/response_disposition_claim_store.py \
	app/services/agent_change_set_queries.py \
	app/services/agent_workspace_package_codec.py \
	app/services/agent_workspace_packages.py \
	app/services/improvement_execution_service.py \
	app/services/improvement_governor_service.py \
	app/services/workspace_execution_applier.py \
	app/runtime/stores/agent_job_store.py \
	app/runtime/stores/feedback_case_store.py \
	app/runtime/stores/feedback_eval_store.py \
	app/runtime/stores/feedback_store.py \
	app/runtime/stores/improvement_content_store.py \
	app/runtime/stores/improvement_store.py \
	scripts/bootstrap_runtime_volume.py \
	scripts/agent_gov_ci_relay_store.py \
	scripts/agent_gov_ci_status_relay.py \
	scripts/agent_gov_multica.py \
	scripts/check_pr_aid.py \
	scripts/check_codex_governance.py \
	scripts/check_docs_governance.py \
	scripts/check_orphan_tests.py \
	scripts/check_stage_language.py \
	scripts/audit_openapi_contract.py \
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
	scripts/snapshot_legacy_release_controller_audit.py \
	scripts/verify_agent_gov_ci_evidence.py \
	scripts/runtime_template_secret_assignments.py \
	scripts/runtime_template_safety.py \
	scripts/runtime_cleanup.py \
	scripts/cleanup_runtime_artifacts.py \
	scripts/run_main_flow_tests.py

TEST_ARTIFACT_ROOT ?= artifacts/test-quality
BACKEND_TEST_ARTIFACT_DIR ?= $(TEST_ARTIFACT_ROOT)/backend-main-full
QUALITY_POLICY ?= tests/quality_policy.json
GOVERNANCE_BASE_REF ?=
GOVERNANCE_BASE_REF_ARG := $(if $(strip $(GOVERNANCE_BASE_REF)),--base-ref $(GOVERNANCE_BASE_REF),)

.PHONY: setup build up down logs test test-backend coverage main-flow-test main-flow-ui-test mutation-test ci-static openapi-contract-check container-openapi-check container-live-test container-health-e2e smoke compose-diagnose zip chat codex-guard sync-version tag ruff-check ruff-format-check pyright typecheck ui-build ui-up ui-stop ui-logs ui-smoke ui-design-parity ui-feedback-smoke langfuse-dirs langfuse-up langfuse-stop langfuse-logs langfuse-smoke runtime-bootstrap runtime-validate runtime-clean local-debug-env local-debug-bootstrap local-debug-validate local-debug-clean runtime-volume-seeds-scan runtime-volume-seeds-clean clean-runtime-artifacts

setup:
	cp -n docker/.env.example docker/.env || true
	@if ! command -v $(UV) >/dev/null 2>&1; then echo "uv is required. Install uv before running make setup." >&2; exit 1; fi
	$(UV) venv $(VENV) --python 3.11
	$(UV) pip install --python $(PYTHON) -r requirements.txt
	$(MAKE) langfuse-dirs

build:
	$(COMPOSE) build

up:
	@if ! $(COMPOSE) up -d --wait --remove-orphans; then \
		$(MAKE) --no-print-directory compose-diagnose; \
		exit 1; \
	fi
	@$(PYTHON_RUN) scripts/diagnose_runtime_health.py --env-file "$(COMPOSE_ENV_FILE)" || true

down:
	$(COMPOSE) down

logs:
	$(COMPOSE) logs -f claude-agent-api

ui-build:
	$(COMPOSE) build claude-agent-ui

ui-up:
	$(COMPOSE) up -d claude-agent-ui

ui-stop:
	$(COMPOSE) stop claude-agent-ui

ui-logs:
	$(COMPOSE) logs -f claude-agent-ui

ui-smoke:
	@frontend_port=$${FRONTEND_HOST_PORT:-$$(awk -F= '$$1 == "FRONTEND_HOST_PORT" {sub(/^[^=]*=/, ""); print; exit}' "$(COMPOSE_ENV_FILE)" 2>/dev/null)}; \
	frontend_url=$${FRONTEND_URL:-http://localhost:$${frontend_port:-55173}}; \
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
	@if [ -z "$$RUNTIME_UI_BASE" ]; then echo "RUNTIME_UI_BASE=<real-container-ui> is required" >&2; exit 1; fi
	@if [ -z "$$RUNTIME_API_BASE" ]; then echo "RUNTIME_API_BASE=<real-container-api> is required" >&2; exit 1; fi
	@VERIFY_SCREENSHOT_DIR="$${VERIFY_SCREENSHOT_DIR:-/tmp/agentgov-ui-feedback-smoke}" pnpm --dir frontend run verify:real-container

langfuse-dirs:
	@runtime_root=$$($(PYTHON_RUN) -c 'from pathlib import Path; import sys; sys.path.insert(0, "scripts"); from bootstrap_runtime_volume import resolve_runtime_root; print(resolve_runtime_root(None, Path(sys.argv[1])).as_posix())' "$(COMPOSE_ENV_FILE)"); \
	mkdir -p "$$runtime_root/langfuse/postgres" "$$runtime_root/langfuse/clickhouse/data" "$$runtime_root/langfuse/clickhouse/logs" "$$runtime_root/langfuse/redis" "$$runtime_root/langfuse/minio"; \
	chmod a+rwx "$$runtime_root/langfuse" "$$runtime_root/langfuse/postgres" "$$runtime_root/langfuse/clickhouse" "$$runtime_root/langfuse/clickhouse/data" "$$runtime_root/langfuse/clickhouse/logs" "$$runtime_root/langfuse/redis" "$$runtime_root/langfuse/minio" 2>/dev/null || true

langfuse-up: langfuse-dirs
	$(COMPOSE) --profile langfuse up -d langfuse-postgres langfuse-clickhouse langfuse-redis langfuse-minio langfuse-web langfuse-worker

langfuse-stop:
	$(COMPOSE) --profile langfuse stop langfuse-worker langfuse-web langfuse-minio langfuse-redis langfuse-clickhouse langfuse-postgres

langfuse-logs:
	$(COMPOSE) --profile langfuse logs -f langfuse-web langfuse-worker

langfuse-smoke:
	$(PYTHON_RUN) scripts/langfuse_smoke.py --env-file "$(COMPOSE_ENV_FILE)"

runtime-bootstrap:
	$(COMPOSE) run --rm --no-deps claude-agent-api prepare

runtime-validate:
	$(COMPOSE) run --rm --no-deps claude-agent-api validate

runtime-clean:
	$(PYTHON_RUN) scripts/cleanup_runtime_artifacts.py --env-file "$(COMPOSE_ENV_FILE)" --runtime-artifacts

local-debug-env:
	cp -n docker/.env.local-debug.example docker/.env.local-debug || true

local-debug-bootstrap: local-debug-env
	$(PYTHON_RUN) -m app.runtime.service_launcher prepare

local-debug-validate: local-debug-env
	$(PYTHON_RUN) -m app.runtime.service_launcher validate

local-debug-clean: local-debug-env
	$(PYTHON_RUN) scripts/cleanup_runtime_artifacts.py --env-file docker/.env.local-debug --runtime-volume-mode local-debug --runtime-artifacts

runtime-volume-seeds-scan:
	$(PYTHON_RUN) scripts/runtime_template_safety.py verify docker/runtime-volume-seeds

runtime-volume-seeds-clean:
	$(PYTHON_RUN) scripts/cleanup_runtime_artifacts.py --template-artifacts

clean-runtime-artifacts: runtime-clean local-debug-clean runtime-volume-seeds-clean

smoke:
	@$(PYTHON_RUN) scripts/diagnose_runtime_health.py --env-file "$(COMPOSE_ENV_FILE)" --require-ready

compose-diagnose:
	@bash scripts/compose_diagnose.sh

chat:
	@host_port=$${HOST_PORT:-$$(awk -F= '$$1 == "HOST_PORT" {sub(/^[^=]*=/, ""); print; exit}' "$(COMPOSE_ENV_FILE)" 2>/dev/null)}; \
	api_key=$${API_KEY:-$$(awk -F= '$$1 == "API_KEY" {sub(/^[^=]*=/, ""); print; exit}' "$(COMPOSE_ENV_FILE)" 2>/dev/null)}; \
	api_base=$${API_BASE:-$$(awk -F= '$$1 == "API_BASE" {sub(/^[^=]*=/, ""); print; exit}' "$(COMPOSE_ENV_FILE)" 2>/dev/null)}; \
	api_base=$${api_base:-http://localhost:$${host_port:-58080}}; \
	curl -s -X POST "$$api_base/api/chat" \
		-H 'Content-Type: application/json' \
		-H "Authorization: Bearer $${api_key:-change-me}" \
		-d '{"message":"你好，请说明你当前可用的 agents 和 skills。","agent_id":"main-agent"}' | $(PYTHON_RUN) -m json.tool

codex-guard:
	$(PYTHON_RUN) .codex/skills/codex-config-optimizer/scripts/audit_codex_config.py --fail
	$(PYTHON_RUN) scripts/check_codex_governance.py --mode fail $(GOVERNANCE_BASE_REF_ARG)
	$(PYTHON_RUN) scripts/check_stage_language.py
	$(PYTHON_RUN) scripts/check_version_consistency.py
	$(PYTHON_RUN) scripts/audit_openapi_contract.py --fail
	$(PYTHON_RUN) scripts/check_docs_governance.py
	$(PYTHON_RUN) scripts/check_test_quality_policy.py --manifest-only --policy $(QUALITY_POLICY)

openapi-contract-check:
	$(PYTHON_RUN) scripts/audit_openapi_contract.py --fail

container-openapi-check:
	@host_port=$${HOST_PORT:-$$(awk -F= '$$1 == "HOST_PORT" {sub(/^[^=]*=/, ""); print; exit}' "$(COMPOSE_ENV_FILE)" 2>/dev/null)}; \
	api_base=$${API_BASE:-$$(awk -F= '$$1 == "API_BASE" {sub(/^[^=]*=/, ""); print; exit}' "$(COMPOSE_ENV_FILE)" 2>/dev/null)}; \
	api_base=$${api_base:-http://localhost:$${host_port:-58080}}; \
	$(PYTHON_RUN) scripts/audit_openapi_contract.py --base-url "$$api_base" --compare-local --fail

container-health-e2e:
	bash scripts/run_healthcheck_container_e2e.sh

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

ci-static: codex-guard typecheck runtime-volume-seeds-scan

test-backend:
	mkdir -p $(BACKEND_TEST_ARTIFACT_DIR)
	$(PYTHON_RUN) -m compileall app
	$(PYTHON_RUN) scripts/run_test_lane.py --policy $(QUALITY_POLICY) --lane main-full --artifact-dir $(BACKEND_TEST_ARTIFACT_DIR)
	$(PYTHON_RUN) scripts/check_docs_governance.py --collect-pytest

test: codex-guard test-backend

coverage:
	$(PYTHON_RUN) scripts/run_test_lane.py --policy $(QUALITY_POLICY) --lane main-full --artifact-dir $(BACKEND_TEST_ARTIFACT_DIR)

main-flow-test:
	$(PYTHON_RUN) scripts/run_main_flow_tests.py --policy $(QUALITY_POLICY)

main-flow-ui-test:
	$(PYTHON_RUN) scripts/run_main_flow_tests.py --policy $(QUALITY_POLICY) --ui-only --artifact-root $(TEST_ARTIFACT_ROOT)/frontend-ui

mutation-test:
	$(PYTHON_RUN) scripts/run_mutation_lane.py --policy $(QUALITY_POLICY) --artifact-dir $(TEST_ARTIFACT_ROOT)/mutation

container-live-test:
	$(COMPOSE) run --rm --entrypoint sh -e REQUIRE_LIVE_RUNTIME=1 -v "$(CURDIR):/app" -w /app claude-agent-api -lc 'python -m pytest -q -rs tests/test_live_runtime_acceptance.py'
