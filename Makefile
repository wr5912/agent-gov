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
	curl -s http://localhost:8080/health | $(PYTHON) -m json.tool

chat:
	curl -s -X POST http://localhost:8080/api/chat \
		-H 'Content-Type: application/json' \
		-H "Authorization: Bearer $${API_KEY:-change-me}" \
		-d '{"message":"你好，请说明你当前可用的 agents 和 skills。","skills_mode":"all"}' | $(PYTHON) -m json.tool

test:
	$(PYTHON) -m compileall app
	$(PYTHON) -m pytest -q
