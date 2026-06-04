VENV ?= .venv
PYTHON ?= $(VENV)/bin/python
PYTHON_BOOTSTRAP ?= python3
UV ?= uv
COMPOSE ?= docker compose --env-file docker/.env -f docker/docker-compose.yml

.PHONY: setup build up down logs test smoke zip chat codex-guard ui-build ui-up ui-stop ui-logs ui-smoke ui-feedback-smoke langfuse-dirs langfuse-up langfuse-stop langfuse-logs langfuse-smoke runtime-bootstrap runtime-template-scan runtime-template-export runtime-template-restore runtime-template-restore-list

setup: langfuse-dirs
	cp -n docker/.env.example docker/.env || true
	$(PYTHON_BOOTSTRAP) scripts/bootstrap_runtime_volume.py
	@if ! command -v $(UV) >/dev/null 2>&1; then echo "uv is required. Install uv before running make setup." >&2; exit 1; fi
	$(UV) venv $(VENV) --python 3.11
	$(UV) pip install --python $(PYTHON) -r requirements.txt pytest

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

ui-feedback-smoke:
	@frontend_port=$${FRONTEND_HOST_PORT:-$$(awk -F= '$$1 == "FRONTEND_HOST_PORT" {sub(/^[^=]*=/, ""); print; exit}' docker/.env 2>/dev/null)}; \
	host_port=$${HOST_PORT:-$$(awk -F= '$$1 == "HOST_PORT" {sub(/^[^=]*=/, ""); print; exit}' docker/.env 2>/dev/null)}; \
	ui_base=$${RUNTIME_UI_BASE:-http://localhost:$${frontend_port:-55173}}; \
	api_base=$${RUNTIME_API_BASE:-http://localhost:$${host_port:-58080}}; \
	RUNTIME_UI_BASE="$$ui_base" RUNTIME_API_BASE="$$api_base" pnpm --dir frontend verify:feedback-browser

langfuse-dirs:
	$(PYTHON_BOOTSTRAP) scripts/bootstrap_runtime_volume.py --quiet
	@runtime_root=$$($(PYTHON_BOOTSTRAP) -c 'from pathlib import Path; import sys; sys.path.insert(0, "scripts"); from bootstrap_runtime_volume import resolve_runtime_root; print(resolve_runtime_root(None, Path("docker/.env")).as_posix())'); \
	chmod a+rwx "$$runtime_root/langfuse" "$$runtime_root/langfuse/postgres" "$$runtime_root/langfuse/clickhouse" "$$runtime_root/langfuse/clickhouse/data" "$$runtime_root/langfuse/clickhouse/logs" "$$runtime_root/langfuse/redis" "$$runtime_root/langfuse/minio" 2>/dev/null || true

langfuse-up: langfuse-dirs
	$(COMPOSE) --profile langfuse up -d langfuse-postgres langfuse-clickhouse langfuse-redis langfuse-minio langfuse-web langfuse-worker

langfuse-stop:
	$(COMPOSE) --profile langfuse stop langfuse-worker langfuse-web langfuse-minio langfuse-redis langfuse-clickhouse langfuse-postgres

langfuse-logs:
	$(COMPOSE) --profile langfuse logs -f langfuse-web langfuse-worker

langfuse-smoke:
	@langfuse_port=$${LANGFUSE_HOST_PORT:-$$(awk -F= '$$1 == "LANGFUSE_HOST_PORT" {sub(/^[^=]*=/, ""); print; exit}' docker/.env 2>/dev/null)}; \
	langfuse_url=$${LANGFUSE_NEXTAUTH_URL:-$$(awk -F= '$$1 == "LANGFUSE_NEXTAUTH_URL" {sub(/^[^=]*=/, ""); print; exit}' docker/.env 2>/dev/null)}; \
	langfuse_url=$${langfuse_url:-http://localhost:$${langfuse_port:-53000}}; \
	i=1; \
	while [ $$i -le 60 ]; do \
		if curl -fsS -o /dev/null "$$langfuse_url/api/public/health"; then \
			echo "Langfuse health OK: $$langfuse_url/api/public/health"; \
			exit 0; \
		fi; \
		sleep 2; \
		i=$$((i + 1)); \
	done; \
	echo "Langfuse health failed: $$langfuse_url/api/public/health" >&2; \
	exit 1

runtime-bootstrap:
	$(PYTHON_BOOTSTRAP) scripts/bootstrap_runtime_volume.py

runtime-template-scan:
	$(PYTHON) scripts/runtime_template_safety.py verify docker/runtime-template

runtime-template-export:
	$(PYTHON) scripts/export_runtime_template.py

runtime-template-restore:
	@if [ -z "$(BACKUP)" ]; then echo "BACKUP=<backup-file> is required" >&2; exit 1; fi
	$(PYTHON) scripts/restore_runtime_template_backup.py --backup "$(BACKUP)"

runtime-template-restore-list:
	$(PYTHON) scripts/restore_runtime_template_backup.py --list

smoke:
	@host_port=$${HOST_PORT:-$$(awk -F= '$$1 == "HOST_PORT" {sub(/^[^=]*=/, ""); print; exit}' docker/.env 2>/dev/null)}; \
	api_base=$${API_BASE:-$$(awk -F= '$$1 == "API_BASE" {sub(/^[^=]*=/, ""); print; exit}' docker/.env 2>/dev/null)}; \
	api_base=$${api_base:-http://localhost:$${host_port:-58080}}; \
	curl -s "$$api_base/health" | $(PYTHON) -m json.tool

chat:
	@host_port=$${HOST_PORT:-$$(awk -F= '$$1 == "HOST_PORT" {sub(/^[^=]*=/, ""); print; exit}' docker/.env 2>/dev/null)}; \
	api_key=$${API_KEY:-$$(awk -F= '$$1 == "API_KEY" {sub(/^[^=]*=/, ""); print; exit}' docker/.env 2>/dev/null)}; \
	api_base=$${API_BASE:-$$(awk -F= '$$1 == "API_BASE" {sub(/^[^=]*=/, ""); print; exit}' docker/.env 2>/dev/null)}; \
	api_base=$${api_base:-http://localhost:$${host_port:-58080}}; \
	curl -s -X POST "$$api_base/api/chat" \
		-H 'Content-Type: application/json' \
		-H "Authorization: Bearer $${api_key:-change-me}" \
		-d '{"message":"你好，请说明你当前可用的 agents 和 skills。","skills_mode":"all"}' | $(PYTHON) -m json.tool

codex-guard:
	$(PYTHON) scripts/check_codex_governance.py --mode fail

test: codex-guard
	$(PYTHON) -m compileall app
	$(PYTHON) -m pytest -q
