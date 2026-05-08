VENV ?= .venv
PYTHON ?= $(VENV)/bin/python
UV ?= uv

.PHONY: setup build up down logs test smoke zip

setup:
	cp -n .env.example .env || true
	mkdir -p claude-home data/sessions data/transcripts data/uploads data/outputs data/agent-memory
	@if command -v $(UV) >/dev/null 2>&1; then \
		$(UV) venv $(VENV) --python 3.11; \
		$(UV) pip install --python $(PYTHON) -r requirements.txt pytest; \
	else \
		python3.11 -m venv $(VENV); \
		$(PYTHON) -m pip install -r requirements.txt pytest; \
	fi

build:
	docker compose build

up:
	docker compose up -d

down:
	docker compose down

logs:
	docker compose logs -f claude-agent-api

smoke:
	@host_port=$${HOST_PORT:-$$(awk -F= '$$1 == "HOST_PORT" {sub(/^[^=]*=/, ""); print; exit}' .env 2>/dev/null)}; \
	api_base=$${API_BASE:-$$(awk -F= '$$1 == "API_BASE" {sub(/^[^=]*=/, ""); print; exit}' .env 2>/dev/null)}; \
	api_base=$${api_base:-http://localhost:$${host_port:-8080}}; \
	curl -s "$$api_base/health" | $(PYTHON) -m json.tool

chat:
	@host_port=$${HOST_PORT:-$$(awk -F= '$$1 == "HOST_PORT" {sub(/^[^=]*=/, ""); print; exit}' .env 2>/dev/null)}; \
	api_key=$${API_KEY:-$$(awk -F= '$$1 == "API_KEY" {sub(/^[^=]*=/, ""); print; exit}' .env 2>/dev/null)}; \
	api_base=$${API_BASE:-$$(awk -F= '$$1 == "API_BASE" {sub(/^[^=]*=/, ""); print; exit}' .env 2>/dev/null)}; \
	api_base=$${api_base:-http://localhost:$${host_port:-8080}}; \
	curl -s -X POST "$$api_base/api/chat" \
		-H 'Content-Type: application/json' \
		-H "Authorization: Bearer $${api_key:-change-me}" \
		-d '{"message":"你好，请说明你当前可用的 agents 和 skills。","skills_mode":"all"}' | $(PYTHON) -m json.tool

test:
	$(PYTHON) -m compileall app
	$(PYTHON) -m pytest -q
