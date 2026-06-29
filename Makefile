VENV ?= .venv
PYTHON ?= $(VENV)/bin/python
UV ?= uv
LITELLM_LOCAL_MODEL_COST_MAP ?= True
PYTHON_RUN ?= LITELLM_LOCAL_MODEL_COST_MAP=$(LITELLM_LOCAL_MODEL_COST_MAP) $(PYTHON)
COMPOSE ?= docker compose --env-file docker/.env -f docker/docker-compose.yml
# 版本唯一真相源：根 VERSION 文件。导出给 compose，让镜像 tag ${APP_VERSION} 派生（build/up 自动生效）。
export APP_VERSION := $(shell cat $(CURDIR)/VERSION 2>/dev/null || echo dev)
PYTHON_TYPECHECK_TARGETS := \
	app/runtime/agent_job_types.py \
	app/runtime/output_formatter.py \
	app/runtime/agent_job_runner.py \
	app/runtime/claude_runtime.py \
	app/services/agent_job_worker.py \
	app/services/feedback_job_orchestrator.py \
	app/runtime/stores/agent_job_store.py \
	app/runtime/stores/feedback_job_store.py \
	app/runtime/stores/feedback_batch_plan_store.py \
	app/runtime/stores/feedback_execution_store.py \
	scripts/bootstrap_runtime_volume.py \
	scripts/check_codex_governance.py \
	scripts/check_docs_governance.py \
	scripts/check_orphan_tests.py \
	scripts/codex_governance_typed_output.py \
	scripts/check_test_coverage_policy.py \
	scripts/runtime_template_renderer.py \
	scripts/runtime_cleanup.py \
	scripts/cleanup_runtime_artifacts.py \
	scripts/run_main_flow_tests.py

COVERAGE_JSON ?= /tmp/agent-gov-coverage.json
COVERAGE_POLICY ?= tests/coverage_policy.json

.PHONY: setup build up down logs test coverage main-flow-test container-live-test smoke zip chat codex-guard sync-version tag ruff-check ruff-format-check pyright typecheck ui-build ui-up ui-stop ui-logs ui-smoke langfuse-dirs langfuse-up langfuse-stop langfuse-logs langfuse-smoke runtime-bootstrap runtime-repair-managed-config runtime-reconcile-business-agent-hitl-policy runtime-clean local-debug-env local-debug-bootstrap local-debug-repair-managed-config local-debug-clean runtime-volume-seeds-scan runtime-volume-seeds-export runtime-volume-seeds-restore runtime-volume-seeds-restore-list runtime-volume-seeds-clean clean-runtime-artifacts

setup:
	cp -n docker/.env.example docker/.env || true
	@if ! command -v $(UV) >/dev/null 2>&1; then echo "uv is required. Install uv before running make setup." >&2; exit 1; fi
	$(UV) venv $(VENV) --python 3.11
	$(UV) pip install --python $(PYTHON) -r requirements.txt pytest
	$(MAKE) langfuse-dirs

build:
	$(COMPOSE) build

up:
	$(COMPOSE) up -d

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
	@frontend_port=$${FRONTEND_HOST_PORT:-$$(awk -F= '$$1 == "FRONTEND_HOST_PORT" {sub(/^[^=]*=/, ""); print; exit}' docker/.env 2>/dev/null)}; \
	frontend_url=$${FRONTEND_URL:-http://localhost:$${frontend_port:-45173}}; \
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

langfuse-dirs:
	$(PYTHON_RUN) scripts/bootstrap_runtime_volume.py --quiet
	@runtime_root=$$($(PYTHON_RUN) -c 'from pathlib import Path; import sys; sys.path.insert(0, "scripts"); from bootstrap_runtime_volume import resolve_runtime_root; print(resolve_runtime_root(None, Path("docker/.env")).as_posix())'); \
	chmod a+rwx "$$runtime_root/langfuse" "$$runtime_root/langfuse/postgres" "$$runtime_root/langfuse/clickhouse" "$$runtime_root/langfuse/clickhouse/data" "$$runtime_root/langfuse/clickhouse/logs" "$$runtime_root/langfuse/redis" "$$runtime_root/langfuse/minio" 2>/dev/null || true

langfuse-up: langfuse-dirs
	$(COMPOSE) --profile langfuse up -d langfuse-postgres langfuse-clickhouse langfuse-redis langfuse-minio langfuse-web langfuse-worker

langfuse-stop:
	$(COMPOSE) --profile langfuse stop langfuse-worker langfuse-web langfuse-minio langfuse-redis langfuse-clickhouse langfuse-postgres

langfuse-logs:
	$(COMPOSE) --profile langfuse logs -f langfuse-web langfuse-worker

langfuse-smoke:
	$(PYTHON_RUN) scripts/langfuse_smoke.py --env-file docker/.env

runtime-bootstrap:
	$(PYTHON_RUN) scripts/bootstrap_runtime_volume.py

runtime-repair-managed-config:
	$(PYTHON_RUN) scripts/bootstrap_runtime_volume.py --repair-managed-config

runtime-reconcile-business-agent-hitl-policy:
	$(PYTHON_RUN) scripts/reconcile_business_agent_hitl_policy.py --apply

runtime-clean:
	$(PYTHON_RUN) scripts/cleanup_runtime_artifacts.py --runtime-artifacts

local-debug-env:
	cp -n docker/.env.local-debug.example docker/.env.local-debug || true

local-debug-bootstrap: local-debug-env
	$(PYTHON_RUN) scripts/bootstrap_runtime_volume.py --env-file docker/.env.local-debug --runtime-volume-mode local-debug

local-debug-repair-managed-config: local-debug-env
	$(PYTHON_RUN) scripts/bootstrap_runtime_volume.py --env-file docker/.env.local-debug --runtime-volume-mode local-debug --repair-managed-config

local-debug-clean: local-debug-env
	$(PYTHON_RUN) scripts/cleanup_runtime_artifacts.py --env-file docker/.env.local-debug --runtime-volume-mode local-debug --runtime-artifacts

runtime-volume-seeds-scan:
	$(PYTHON_RUN) scripts/runtime_template_safety.py verify docker/runtime-volume-seeds

runtime-volume-seeds-export:
	$(PYTHON_RUN) scripts/export_runtime_template.py

runtime-volume-seeds-clean:
	$(PYTHON_RUN) scripts/cleanup_runtime_artifacts.py --template-artifacts

clean-runtime-artifacts: runtime-clean local-debug-clean runtime-volume-seeds-clean

runtime-volume-seeds-restore:
	@if [ -z "$(BACKUP)" ]; then echo "BACKUP=<backup-file> is required" >&2; exit 1; fi
	$(PYTHON_RUN) scripts/restore_runtime_template_backup.py --backup "$(BACKUP)"

runtime-volume-seeds-restore-list:
	$(PYTHON_RUN) scripts/restore_runtime_template_backup.py --list

smoke:
	@host_port=$${HOST_PORT:-$$(awk -F= '$$1 == "HOST_PORT" {sub(/^[^=]*=/, ""); print; exit}' docker/.env 2>/dev/null)}; \
	api_base=$${API_BASE:-$$(awk -F= '$$1 == "API_BASE" {sub(/^[^=]*=/, ""); print; exit}' docker/.env 2>/dev/null)}; \
	api_base=$${api_base:-http://localhost:$${host_port:-48080}}; \
	curl -s "$$api_base/health" | $(PYTHON_RUN) -m json.tool

chat:
	@host_port=$${HOST_PORT:-$$(awk -F= '$$1 == "HOST_PORT" {sub(/^[^=]*=/, ""); print; exit}' docker/.env 2>/dev/null)}; \
	api_key=$${API_KEY:-$$(awk -F= '$$1 == "API_KEY" {sub(/^[^=]*=/, ""); print; exit}' docker/.env 2>/dev/null)}; \
	api_base=$${API_BASE:-$$(awk -F= '$$1 == "API_BASE" {sub(/^[^=]*=/, ""); print; exit}' docker/.env 2>/dev/null)}; \
	api_base=$${api_base:-http://localhost:$${host_port:-48080}}; \
	curl -s -X POST "$$api_base/api/chat" \
		-H 'Content-Type: application/json' \
		-H "Authorization: Bearer $${api_key:-change-me}" \
		-d '{"message":"你好，请说明你当前可用的 agents 和 skills。","skills_mode":"all"}' | $(PYTHON_RUN) -m json.tool

codex-guard:
	$(PYTHON_RUN) scripts/check_codex_governance.py --mode fail
	$(PYTHON_RUN) scripts/check_version_consistency.py

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

test: codex-guard
	$(PYTHON_RUN) -m compileall app
	$(PYTHON_RUN) -m pytest -q --cov=app --cov=scripts --cov-branch --cov-report=term-missing:skip-covered --cov-report=json:$(COVERAGE_JSON)
	$(PYTHON_RUN) scripts/check_test_coverage_policy.py --coverage-json $(COVERAGE_JSON) --policy $(COVERAGE_POLICY)

coverage:
	$(PYTHON_RUN) -m pytest -q --cov=app --cov=scripts --cov-branch --cov-report=term-missing:skip-covered --cov-report=json:$(COVERAGE_JSON)
	$(PYTHON_RUN) scripts/check_test_coverage_policy.py --coverage-json $(COVERAGE_JSON) --policy $(COVERAGE_POLICY)

main-flow-test:
	$(PYTHON_RUN) scripts/run_main_flow_tests.py --policy $(COVERAGE_POLICY)

container-live-test:
	$(COMPOSE) run --rm -v "$(CURDIR):/app" -w /app claude-agent-api sh -lc 'python -m pytest -q -rs tests/test_live_runtime_acceptance.py'
