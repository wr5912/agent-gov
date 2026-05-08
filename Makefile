VENV ?= .venv
PYTHON ?= $(VENV)/bin/python
UV ?= uv
COMPOSE ?= docker compose --env-file docker/.env -f docker/docker-compose.yml

.PHONY: setup build up down logs test smoke zip

setup:
	cp -n docker/.env.example docker/.env || true
	mkdir -p docker/volume/workspace docker/volume/claude-root/.claude/skills docker/volume/claude-root/.claude/agents
	mkdir -p docker/volume/claude-root/.claude/commands docker/volume/claude-root/.claude/output-styles
	mkdir -p docker/volume/data/sessions docker/volume/data/transcripts docker/volume/data/uploads docker/volume/data/outputs docker/volume/data/agent-memory
	@if [ ! -f docker/volume/claude-root/.claude/settings.json ]; then printf '{}\n' > docker/volume/claude-root/.claude/settings.json; fi
	@if [ ! -f docker/volume/claude-root/.claude/CLAUDE.md ]; then printf '# User Claude Instructions\n' > docker/volume/claude-root/.claude/CLAUDE.md; fi
	@if [ ! -f docker/volume/claude-root/.claude.json ]; then printf '{}\n' > docker/volume/claude-root/.claude.json; fi
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
