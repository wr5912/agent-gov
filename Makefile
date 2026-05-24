VENV ?= .venv
PYTHON ?= $(VENV)/bin/python
UV ?= uv
COMPOSE ?= docker compose --env-file docker/.env -f docker/docker-compose.yml

.PHONY: setup build up down logs test smoke zip chat ui-build ui-up ui-stop ui-logs ui-smoke langfuse-dirs langfuse-up langfuse-stop langfuse-logs langfuse-smoke

setup: langfuse-dirs
	cp -n docker/.env.example docker/.env || true
	mkdir -p docker/volume/main-workspace docker/volume/attribution-workspace docker/volume/proposal-workspace
	mkdir -p docker/volume/claude-roots/main/.claude docker/volume/claude-roots/attribution/.claude docker/volume/claude-roots/proposal/.claude
	mkdir -p docker/volume/data/sessions docker/volume/data/transcripts docker/volume/data/uploads docker/volume/data/outputs docker/volume/data/agent-memory
	mkdir -p docker/volume/data/feedback-signals docker/volume/data/soc-events docker/volume/data/pending-correlations docker/volume/data/feedback-cases
	mkdir -p docker/volume/data/evidence-packages docker/volume/data/feedback-analysis/jobs docker/volume/data/optimization-proposals docker/volume/data/optimization-tasks docker/volume/data/agent-versions/main
	@if [ ! -f docker/volume/claude-roots/main/.claude/settings.json ]; then printf '{}\n' > docker/volume/claude-roots/main/.claude/settings.json; fi
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

langfuse-dirs:
	mkdir -p docker/volume/langfuse/postgres docker/volume/langfuse/clickhouse/data docker/volume/langfuse/clickhouse/logs
	mkdir -p docker/volume/langfuse/redis docker/volume/langfuse/minio
	chmod a+rwx docker/volume/langfuse docker/volume/langfuse/postgres docker/volume/langfuse/clickhouse docker/volume/langfuse/clickhouse/data docker/volume/langfuse/clickhouse/logs docker/volume/langfuse/redis docker/volume/langfuse/minio 2>/dev/null || true

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

test:
	$(PYTHON) -m compileall app
	$(PYTHON) -m pytest -q
