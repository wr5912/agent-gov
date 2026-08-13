override SHELL := /bin/sh
override .SHELLFLAGS := -c
_CONTAINER_ACCEPTANCE_EXPECTED_MAKEFILE := $(realpath $(dir $(lastword $(MAKEFILE_LIST)))/Makefile)
_CONTAINER_ACCEPTANCE_MAKEFILE_LIST_GUARD = $(if $(and $(filter 1,$(words $(MAKEFILE_LIST))),$(filter $(_CONTAINER_ACCEPTANCE_EXPECTED_MAKEFILE),$(realpath $(MAKEFILE_LIST)))),,$(error Public container acceptance rejects alternate MAKEFILE_LIST authority))
_CONTAINER_ACCEPTANCE_PUBLIC_TARGETS := ui-smoke ui-feedback-smoke ui-openai-responses-smoke ui-playground-cancel-smoke smoke container-core-smoke container-openapi-check container-live-test container-workspace-pytest-test container-speech-summary-test container-health-e2e langfuse-smoke
ifneq ($(AGENT_GOV_CONTAINER_ACCEPTANCE_ACTIVE),1)
ifneq ($(strip $(filter $(_CONTAINER_ACCEPTANCE_PUBLIC_TARGETS),$(MAKECMDGOALS))),)
ifneq ($(words $(MAKEFILE_LIST)),1)
$(error Public container acceptance rejects alternate MAKEFILE_LIST authority)
endif
ifneq ($(realpath $(MAKEFILE_LIST)),$(_CONTAINER_ACCEPTANCE_EXPECTED_MAKEFILE))
$(error Public container acceptance rejects alternate MAKEFILE_LIST authority)
endif
ifneq ($(strip $(MAKEFILES)),)
$(error Public container acceptance rejects preloaded MAKEFILES)
endif
ifneq ($(strip $(MAKEOVERRIDES)),)
$(error Public container acceptance rejects command-line variable overrides)
endif
_CONTAINER_ACCEPTANCE_SHORT_MAKEFLAGS := $(filter-out --%,$(MAKEFLAGS))
_CONTAINER_ACCEPTANCE_DANGEROUS_MAKEFLAGS := $(filter --eval --eval=% --environment-overrides --ignore-errors --just-print --dry-run --recon --question --touch,$(MAKEFLAGS))
ifneq ($(strip $(_CONTAINER_ACCEPTANCE_DANGEROUS_MAKEFLAGS)$(findstring e,$(_CONTAINER_ACCEPTANCE_SHORT_MAKEFLAGS))$(findstring i,$(_CONTAINER_ACCEPTANCE_SHORT_MAKEFLAGS))$(findstring n,$(_CONTAINER_ACCEPTANCE_SHORT_MAKEFLAGS))$(findstring q,$(_CONTAINER_ACCEPTANCE_SHORT_MAKEFLAGS))$(findstring t,$(_CONTAINER_ACCEPTANCE_SHORT_MAKEFLAGS))),)
$(error Public container acceptance rejects unsafe MAKEFLAGS)
endif
endif
endif
VENV ?= .venv
PYTHON ?= $(VENV)/bin/python
UV ?= uv
LITELLM_LOCAL_MODEL_COST_MAP ?= True
PYTHON_RUN ?= LITELLM_LOCAL_MODEL_COST_MAP=$(LITELLM_LOCAL_MODEL_COST_MAP) $(PYTHON)
COMPOSE_ENV_FILE ?= docker/.env
override COMPOSE_ENV_FILE := $(value COMPOSE_ENV_FILE)
export COMPOSE_ENV_FILE
export AGENT_GOV_COMPOSE_ENV_FILE := $(abspath $(COMPOSE_ENV_FILE))
COMPOSE ?= docker compose --env-file $(COMPOSE_ENV_FILE) -f docker/docker-compose.yml
COMPOSE_UP_FLAGS ?=
# 版本唯一真相源：根 VERSION 文件。使用 GNU Make 内建读取，避免未绑定的外部工具参与验收解析。
export APP_VERSION := $(or $(strip $(file <$(CURDIR)/VERSION)),dev)
PYTHON_TYPECHECK_TARGETS := \
	app/openapi_contract.py \
	app/openapi_example_contracts.py \
	app/openapi_input_documentation.py \
	app/openapi_request_examples.py \
	app/openapi_runtime_request_examples.py \
	app/routers/agent_workspace_packages.py \
	app/agent_testing \
	app/routers/claude_user_input.py \
	app/routers/conversations.py \
	app/routers/responses.py \
	app/runtime/advisory_lock.py \
	app/runtime/agent_git_raw_storage.py \
	app/runtime/agent_git_worktree_operations.py \
	app/runtime/agent_job_types.py \
	app/runtime/agent_workspace_package_schemas.py \
	app/runtime/runtime_bootstrap.py \
	app/runtime/runtime_db_migrations_0033.py \
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
	app/runtime/recovery_cli_support.py \
	app/runtime/recovery_read_only_git.py \
	app/runtime/runtime_db_migrations_0057.py \
	app/runtime/session_turn_admission.py \
	app/runtime/session_turn_recovery.py \
	app/runtime/service_launcher.py \
	app/runtime/workspace_activation_recovery.py \
	app/runtime/workspace_activation_recovery_authority.py \
	app/services/agent_change_set_queries.py \
	app/services/business_agent_presentation.py \
	app/services/agent_workspace_git_operations.py \
	app/services/agent_workspace_activation_recovery.py \
	app/services/agent_workspace_activation_recovery_preflight.py \
	app/services/agent_workspace_activation_recovery_wiring.py \
	app/services/agent_workspace_activation_reconciliation.py \
	app/services/agent_workspace_manifest_identity.py \
	app/services/agent_workspace_package_codec.py \
	app/services/agent_workspace_packages.py \
	app/services/generated_agent_tests.py \
	app/services/improvement_execution_service.py \
	app/services/improvement_governor_service.py \
	app/services/workspace_execution_applier.py \
	app/runtime/stores/agent_job_store.py \
	app/runtime/stores/feedback_case_store.py \
	app/runtime/stores/feedback_store.py \
	app/runtime/stores/improvement_content_store.py \
	app/runtime/stores/improvement_store.py \
	scripts/bootstrap_runtime_volume.py \
	scripts/check_codex_governance.py \
	scripts/check_defensive_security_boundary.py \
	scripts/check_docs_governance.py \
	scripts/check_orphan_tests.py \
	scripts/check_stage_language.py \
	scripts/audit_openapi_contract.py \
	scripts/openapi_request_input_audit.py \
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
	scripts/container_acceptance_bootstrap.py \
	scripts/container_acceptance_candidate.py \
	scripts/container_acceptance_candidate_authority.py \
	scripts/container_acceptance_candidate_cleanup.py \
	scripts/container_acceptance_candidate_cleanup_fs.py \
	scripts/container_acceptance_candidate_git.py \
	scripts/container_acceptance_candidate_projection.py \
	scripts/container_acceptance_candidate_storage.py \
	scripts/container_acceptance_candidate_tools.py \
	scripts/container_acceptance_contract.py \
	scripts/container_acceptance_dependency_authority.py \
	scripts/container_acceptance_dependency_requirements.py \
	scripts/container_acceptance_docker_authority.py \
	scripts/container_acceptance_git_authority.py \
	scripts/container_acceptance_import_authority.py \
	scripts/container_acceptance_image_authority.py \
	scripts/container_acceptance_environment.py \
	scripts/container_acceptance_launcher.py \
	scripts/container_acceptance_launcher_entry.py \
	scripts/container_acceptance_lock.py \
	scripts/container_acceptance_make_gate.py \
	scripts/container_acceptance_profiles.py \
	scripts/container_acceptance_python_runner.py \
	scripts/container_acceptance_receipt.py \
	scripts/container_acceptance_receipt_authority.py \
	scripts/container_acceptance_receipt_lifecycle.py \
	scripts/container_acceptance_receipt_retention.py \
	scripts/container_acceptance_reexec_environment.py \
	scripts/container_acceptance_signals.py \
	scripts/container_acceptance_snapshot_authority.py \
	scripts/container_acceptance_snapshot_exec.py \
	scripts/container_acceptance_terminal_freshness.py \
	scripts/container_acceptance_tool_authority.py \
	scripts/container_acceptance_toolchain.py \
	scripts/container_acceptance_verifier_process.py \
	scripts/container_acceptance_verifier_evidence.py \
	scripts/run_container_acceptance.py \
	scripts/agent_test_acceptance_support.py \
	scripts/run_agent_test_container_e2e.py \
	scripts/verify_speech_summary_container.py

TEST_ARTIFACT_ROOT ?= artifacts/test-quality
BACKEND_TEST_ARTIFACT_DIR ?= $(TEST_ARTIFACT_ROOT)/backend-main-full
QUALITY_POLICY ?= tests/quality_policy.json
GOVERNANCE_BASE_REF ?=
GOVERNANCE_BASE_REF_ARG := $(if $(strip $(GOVERNANCE_BASE_REF)),--base-ref $(GOVERNANCE_BASE_REF),)
override CONTAINER_ACCEPTANCE_REPO_ROOT := $(abspath $(dir $(lastword $(MAKEFILE_LIST))))
override CONTAINER_ACCEPTANCE_BOOTSTRAP_PYTHON := /usr/bin/python3.10
override CONTAINER_ACCEPTANCE_BOOTSTRAP := $(CONTAINER_ACCEPTANCE_REPO_ROOT)/scripts/container_acceptance_bootstrap.py
override CONTAINER_ACCEPTANCE_TOOLCHAIN := $(CONTAINER_ACCEPTANCE_REPO_ROOT)/scripts/container_acceptance_toolchain.py
override CONTAINER_ACCEPTANCE_BOOTSTRAP_LOADER := import hashlib,os,stat,sys;python_fd=os.open('/proc/self/exe',os.O_RDONLY|os.O_CLOEXEC);python_before=os.fstat(python_fd);python_stream=os.fdopen(python_fd,'rb',closefd=False);python_bytes=python_stream.read(536870913);python_stream.close();python_after=os.fstat(python_fd);identity=lambda value:(value.st_dev,value.st_ino,value.st_mode,value.st_uid,value.st_gid,value.st_size,value.st_mtime_ns,value.st_ctime_ns);record=lambda value:(value.st_dev,value.st_ino,stat.S_IMODE(value.st_mode),value.st_uid,value.st_gid,value.st_size,value.st_mtime_ns,value.st_ctime_ns);python_valid=stat.S_ISREG(python_before.st_mode) and python_before.st_size<=536870912 and len(python_bytes)==python_before.st_size and identity(python_before)==identity(python_after) and python_bytes[:4]==b'\x7fELF';python_valid or (_ for _ in ()).throw(SystemExit(1));python_digest=hashlib.sha256(python_bytes).hexdigest();os.close(python_fd);path=os.path.abspath(sys.argv[1]);repository=os.path.dirname(os.path.dirname(path));parts=repository.split(os.sep)[1:];directory_flags=os.O_RDONLY|os.O_CLOEXEC|os.O_DIRECTORY|os.O_NOFOLLOW;root_fd=os.open('/',directory_flags);exec('for part in parts:\n child=os.open(part,directory_flags,dir_fd=root_fd)\n opened=os.fstat(child)\n linked=os.stat(part,dir_fd=root_fd,follow_symlinks=False)\n trusted=opened.st_uid in {0,os.geteuid()} and (not opened.st_mode&stat.S_IWOTH or opened.st_mode&stat.S_ISVTX and opened.st_uid==0)\n (stat.S_ISDIR(opened.st_mode) and identity(opened)==identity(linked) and trusted) or (_ for _ in ()).throw(SystemExit(1))\n os.close(root_fd)\n root_fd=child');root_before=os.fstat(root_fd);expected_path=os.path.join(repository,'scripts','container_acceptance_bootstrap.py');path==expected_path or (_ for _ in ()).throw(SystemExit(1));scripts_fd=os.open('scripts',directory_flags,dir_fd=root_fd);scripts_before=os.fstat(scripts_fd);scripts_linked=os.stat('scripts',dir_fd=root_fd,follow_symlinks=False);identity(scripts_before)==identity(scripts_linked) or (_ for _ in ()).throw(SystemExit(1));fd=os.open('container_acceptance_bootstrap.py',os.O_RDONLY|os.O_CLOEXEC|os.O_NOFOLLOW,dir_fd=scripts_fd);before=os.fstat(fd);stream=os.fdopen(fd,'rb',closefd=False);encoded=stream.read(1048577);stream.close();after=os.fstat(fd);linked=os.stat('container_acceptance_bootstrap.py',dir_fd=scripts_fd,follow_symlinks=False);valid=stat.S_ISREG(before.st_mode) and before.st_size<=1048576 and len(encoded)==before.st_size and identity(before)==identity(after)==identity(linked) and before.st_uid in {0,os.geteuid()} and not before.st_mode&2;valid or (_ for _ in ()).throw(SystemExit(1));digest=hashlib.sha256(encoded).hexdigest();os.close(fd);os.close(scripts_fd);namespace={'__name__':'__main__','__file__':path,'__package__':None,'__spec__':None,'__agentgov_loaded_sha256__':digest,'__agentgov_system_python_sha256__':python_digest,'_ACTUAL_REPOSITORY_ROOT':repository,'_ACTUAL_REPOSITORY_FD':root_fd,'_ACTUAL_REPOSITORY_IDENTITY':record(root_before)};sys.argv=[path,*sys.argv[2:]];exec(compile(encoded,path,'exec'),namespace)
override CONTAINER_ACCEPTANCE = $(_CONTAINER_ACCEPTANCE_MAKEFILE_LIST_GUARD)"/usr/bin/env" -i COMPOSE_ENV_FILE="$${COMPOSE_ENV_FILE}" ALL_PROXY="$${ALL_PROXY-}" HTTPS_PROXY="$${HTTPS_PROXY-}" HTTP_PROXY="$${HTTP_PROXY-}" NO_PROXY="$${NO_PROXY-}" all_proxy="$${all_proxy-}" https_proxy="$${https_proxy-}" http_proxy="$${http_proxy-}" no_proxy="$${no_proxy-}" LANG="$${LANG-}" LANGUAGE="$${LANGUAGE-}" TZ="$${TZ-}" "$(CONTAINER_ACCEPTANCE_BOOTSTRAP_PYTHON)" -I -S -X pycache_prefix=/dev/null -c "$(CONTAINER_ACCEPTANCE_BOOTSTRAP_LOADER)" "$(CONTAINER_ACCEPTANCE_BOOTSTRAP)" launch
override REQUIRE_CONTAINER_ACCEPTANCE = $(PYTHON_RUN) "$(CONTAINER_ACCEPTANCE_REPO_ROOT)/scripts/container_acceptance_make_gate.py" check "$@"

ifeq ($(AGENT_GOV_CONTAINER_ACCEPTANCE_ACTIVE),1)
override PYTHON_RUN := "$(CONTAINER_ACCEPTANCE_BOOTSTRAP_PYTHON)" -I -S -X pycache_prefix=/dev/null -c "$(CONTAINER_ACCEPTANCE_BOOTSTRAP_LOADER)" "$(CONTAINER_ACCEPTANCE_BOOTSTRAP)" python
override COMPOSE := "$${AGENT_GOV_ACCEPTANCE_DOCKER}" compose --env-file "$${COMPOSE_ENV_FILE}" -f "$(CONTAINER_ACCEPTANCE_REPO_ROOT)/docker/docker-compose.yml"
override CONTAINER_ACCEPTANCE_PNPM := "$${AGENT_GOV_ACCEPTANCE_PNPM}"
override CONTAINER_ACCEPTANCE_MAKE := "$${AGENT_GOV_ACCEPTANCE_MAKE}"
endif

.PHONY: setup build up all-up down logs test test-backend coverage main-flow-test main-flow-ui-test mutation-test openapi-contract-check openapi-type-drift-check container-core-smoke container-openapi-check container-live-test container-workspace-pytest-test container-speech-summary-test container-health-e2e smoke compose-diagnose zip chat codex-guard sync-version tag ruff-check ruff-format-check pyright typecheck ui-build ui-up ui-stop ui-logs ui-smoke ui-design-parity ui-feedback-smoke ui-openai-responses-smoke ui-playground-cancel-smoke langfuse-prepare langfuse-up langfuse-stop langfuse-logs langfuse-smoke runtime-bootstrap runtime-validate workspace-activation-recovery runtime-clean runtime-migrate-workspace-tests runtime-migrate-workspace-tests-scan local-debug-env local-debug-bootstrap local-debug-validate local-debug-clean runtime-bootstrap-scan runtime-bootstrap-clean clean-runtime-artifacts _runtime-health-diagnose _container-health-diagnose _container-core-smoke _container-openapi-check _container-live-test _container-workspace-pytest-test _container-speech-summary-test _container-health-e2e _smoke _ui-smoke _ui-feedback-smoke _ui-openai-responses-smoke _ui-playground-cancel-smoke _langfuse-smoke

setup:
	cp -n docker/.env.example docker/.env || true
	@if ! command -v $(UV) >/dev/null 2>&1; then echo "uv is required. Install uv before running make setup." >&2; exit 1; fi
	$(UV) venv $(VENV) --python 3.11
	$(UV) pip install --python $(PYTHON) -r requirements.txt -e packages/agentgov-testkit

build:
	$(COMPOSE) build
	$(COMPOSE) --profile agent-test-build build agent-test-sandbox-image

up:
	@if ! $(COMPOSE) up -d --wait --remove-orphans $(COMPOSE_UP_FLAGS); then \
		$(MAKE) --no-print-directory compose-diagnose; \
		exit 1; \
	fi
	@$(MAKE) --no-print-directory _runtime-health-diagnose

all-up: langfuse-prepare
	@if ! $(COMPOSE) --profile langfuse up -d --wait --remove-orphans $(COMPOSE_UP_FLAGS); then \
		$(MAKE) --no-print-directory compose-diagnose; \
		exit 1; \
	fi
	@$(MAKE) --no-print-directory _runtime-health-diagnose

_runtime-health-diagnose:
	@python_bin="$(PYTHON)"; \
	if [ ! -x "$$python_bin" ]; then python_bin=$$(command -v python3 2>/dev/null || true); fi; \
	if [ -z "$$python_bin" ]; then \
		echo "Runtime health diagnosis skipped: neither $(PYTHON) nor python3 is available."; \
	else \
		"$$python_bin" scripts/diagnose_runtime_health.py --env-file "$(COMPOSE_ENV_FILE)" || true; \
	fi

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
	$(CONTAINER_ACCEPTANCE) --profile core -- make --no-print-directory _ui-smoke

_ui-smoke:
	@$(REQUIRE_CONTAINER_ACCEPTANCE)
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
	$(CONTAINER_ACCEPTANCE) --profile core -- make --no-print-directory _ui-feedback-smoke

_ui-feedback-smoke:
	@$(REQUIRE_CONTAINER_ACCEPTANCE)
	@frontend_port=$${FRONTEND_HOST_PORT:-$$(awk -F= '$$1 == "FRONTEND_HOST_PORT" {sub(/^[^=]*=/, ""); print; exit}' "$(COMPOSE_ENV_FILE)" 2>/dev/null)}; \
	host_port=$${HOST_PORT:-$$(awk -F= '$$1 == "HOST_PORT" {sub(/^[^=]*=/, ""); print; exit}' "$(COMPOSE_ENV_FILE)" 2>/dev/null)}; \
	api_key=$$(awk -F= '$$1 == "FRONTEND_RUNTIME_API_KEY" || $$1 == "API_KEY" {sub(/^[^=]*=/, ""); print; exit}' "$(COMPOSE_ENV_FILE)" 2>/dev/null); \
	if ! RUNTIME_UI_BASE="http://localhost:$${frontend_port:-55173}" \
	RUNTIME_API_BASE="http://localhost:$${host_port:-58080}" \
	RUNTIME_API_KEY="$$api_key" \
	VERIFY_SCREENSHOT_DIR="$${VERIFY_SCREENSHOT_DIR:-/tmp/agentgov-ui-feedback-smoke}" \
	$(CONTAINER_ACCEPTANCE_PNPM) --silent --dir frontend run verify:real-container:impl >/dev/null 2>&1; then \
		echo "failure_phase=ui_feedback_browser failure_code=ChildExit" >&2; \
		exit 1; \
	fi; \
	echo "UI_FEEDBACK_CONTAINER_OK"

ui-openai-responses-smoke:
	$(CONTAINER_ACCEPTANCE) --profile core -- make --no-print-directory _ui-openai-responses-smoke

ui-playground-cancel-smoke:
	$(CONTAINER_ACCEPTANCE) --profile core -- make --no-print-directory _ui-playground-cancel-smoke

_ui-playground-cancel-smoke:
	@$(REQUIRE_CONTAINER_ACCEPTANCE)
	@frontend_port=$${FRONTEND_HOST_PORT:-$$(awk -F= '$$1 == "FRONTEND_HOST_PORT" {sub(/^[^=]*=/, ""); print; exit}' "$(COMPOSE_ENV_FILE)" 2>/dev/null)}; \
	host_port=$${HOST_PORT:-$$(awk -F= '$$1 == "HOST_PORT" {sub(/^[^=]*=/, ""); print; exit}' "$(COMPOSE_ENV_FILE)" 2>/dev/null)}; \
	api_key=$$(awk -F= '$$1 == "FRONTEND_RUNTIME_API_KEY" || $$1 == "API_KEY" {sub(/^[^=]*=/, ""); print; exit}' "$(COMPOSE_ENV_FILE)" 2>/dev/null); \
	if ! RUNTIME_UI_BASE="http://localhost:$${frontend_port:-55173}" \
	RUNTIME_API_BASE="http://localhost:$${host_port:-58080}" \
	RUNTIME_API_KEY="$$api_key" \
	VERIFY_SCREENSHOT_DIR="$${VERIFY_SCREENSHOT_DIR:-/tmp/agentgov-ui-playground-cancel}" \
	$(CONTAINER_ACCEPTANCE_PNPM) --silent --dir frontend run verify:playground-cancel >/dev/null 2>&1; then \
		echo "failure_phase=playground_cancel_browser failure_code=ChildExit" >&2; \
		exit 1; \
	fi; \
	echo "PLAYGROUND_CANCEL_CONTAINER_OK"

_ui-openai-responses-smoke:
	@$(REQUIRE_CONTAINER_ACCEPTANCE)
	@frontend_port=$${FRONTEND_HOST_PORT:-$$(awk -F= '$$1 == "FRONTEND_HOST_PORT" {sub(/^[^=]*=/, ""); print; exit}' "$(COMPOSE_ENV_FILE)" 2>/dev/null)}; \
	host_port=$${HOST_PORT:-$$(awk -F= '$$1 == "HOST_PORT" {sub(/^[^=]*=/, ""); print; exit}' "$(COMPOSE_ENV_FILE)" 2>/dev/null)}; \
	api_key=$$(awk -F= '$$1 == "FRONTEND_RUNTIME_API_KEY" || $$1 == "API_KEY" {sub(/^[^=]*=/, ""); print; exit}' "$(COMPOSE_ENV_FILE)" 2>/dev/null); \
	if ! RUNTIME_UI_BASE="http://localhost:$${frontend_port:-55173}" \
	RUNTIME_API_BASE="http://localhost:$${host_port:-58080}" \
	RUNTIME_BROWSER_API_BASE="http://localhost:$${host_port:-58080}" \
	RUNTIME_API_KEY="$$api_key" \
	VERIFY_SCREENSHOT_DIR="$${VERIFY_SCREENSHOT_DIR:-/tmp/agentgov-openai-responses-smoke}" \
	$(CONTAINER_ACCEPTANCE_PNPM) --silent --dir frontend run verify:openai-responses-container:impl >/dev/null 2>&1; then \
		echo "failure_phase=openai_responses_browser failure_code=ChildExit" >&2; \
		exit 1; \
	fi; \
	echo "OPENAI_RESPONSES_CONTAINER_OK"

langfuse-prepare:
	$(COMPOSE) --profile langfuse-maintenance run --rm --no-deps -T --pull missing langfuse-volume-init

langfuse-up: langfuse-prepare
	$(COMPOSE) --profile langfuse up -d --wait --remove-orphans $(COMPOSE_UP_FLAGS) langfuse-postgres langfuse-clickhouse langfuse-redis langfuse-minio langfuse-web langfuse-worker

langfuse-stop:
	$(COMPOSE) --profile langfuse stop langfuse-worker langfuse-web langfuse-minio langfuse-redis langfuse-clickhouse langfuse-postgres

langfuse-logs:
	$(COMPOSE) --profile langfuse logs -f langfuse-web langfuse-worker

langfuse-smoke:
	$(CONTAINER_ACCEPTANCE) --profile langfuse -- make --no-print-directory _langfuse-smoke

_langfuse-smoke:
	@$(REQUIRE_CONTAINER_ACCEPTANCE)
	$(PYTHON_RUN) scripts/langfuse_smoke.py --env-file "$(COMPOSE_ENV_FILE)"

runtime-bootstrap:
	$(COMPOSE) run --rm --no-deps claude-agent-api prepare

runtime-validate:
	$(COMPOSE) run --rm --no-deps claude-agent-api validate

WORKSPACE_ACTIVATION_RECOVERY_ARGS ?= list

workspace-activation-recovery:
	$(COMPOSE) run --rm --no-deps claude-agent-api workspace-activation-recovery $(WORKSPACE_ACTIVATION_RECOVERY_ARGS)

runtime-clean:
	$(PYTHON_RUN) scripts/cleanup_runtime_artifacts.py --env-file "$(COMPOSE_ENV_FILE)" --runtime-artifacts

runtime-migrate-workspace-tests-scan:
	$(PYTHON_RUN) scripts/migrate_workspace_test_assets.py --env-file "$(COMPOSE_ENV_FILE)"

runtime-migrate-workspace-tests:
	$(PYTHON_RUN) scripts/migrate_workspace_test_assets.py --env-file "$(COMPOSE_ENV_FILE)" --apply

local-debug-env:
	cp -n docker/.env.local-debug.example docker/.env.local-debug || true

local-debug-bootstrap: local-debug-env
	$(PYTHON_RUN) -m app.runtime.service_launcher prepare

local-debug-validate: local-debug-env
	$(PYTHON_RUN) -m app.runtime.service_launcher validate

local-debug-clean: local-debug-env
	$(PYTHON_RUN) scripts/cleanup_runtime_artifacts.py --env-file docker/.env.local-debug --runtime-volume-mode local-debug --runtime-artifacts

runtime-bootstrap-scan:
	$(PYTHON_RUN) scripts/runtime_bootstrap_safety.py verify docker/runtime-bootstrap

runtime-bootstrap-clean:
	$(PYTHON_RUN) scripts/cleanup_runtime_artifacts.py --bootstrap-artifacts

clean-runtime-artifacts: runtime-clean local-debug-clean runtime-bootstrap-clean

smoke:
	$(CONTAINER_ACCEPTANCE) --profile core -- make --no-print-directory _smoke

_smoke:
	@$(REQUIRE_CONTAINER_ACCEPTANCE)
	@$(PYTHON_RUN) scripts/diagnose_runtime_health.py --env-file "$(COMPOSE_ENV_FILE)" --require-ready

container-core-smoke:
	$(CONTAINER_ACCEPTANCE) --profile core -- make --no-print-directory _container-core-smoke

_container-core-smoke:
	@$(REQUIRE_CONTAINER_ACCEPTANCE)
	+@$(CONTAINER_ACCEPTANCE_MAKE) --no-print-directory --keep-going _smoke _ui-smoke _container-openapi-check

compose-diagnose:
	@bash scripts/compose_diagnose.sh

chat:
	@host_port=$${HOST_PORT:-$$(awk -F= '$$1 == "HOST_PORT" {sub(/^[^=]*=/, ""); print; exit}' "$(COMPOSE_ENV_FILE)" 2>/dev/null)}; \
	api_key=$${API_KEY:-$$(awk -F= '$$1 == "API_KEY" {sub(/^[^=]*=/, ""); print; exit}' "$(COMPOSE_ENV_FILE)" 2>/dev/null)}; \
	api_base=$${API_BASE:-$$(awk -F= '$$1 == "API_BASE" {sub(/^[^=]*=/, ""); print; exit}' "$(COMPOSE_ENV_FILE)" 2>/dev/null)}; \
	api_base=$${api_base:-http://localhost:$${host_port:-58080}}; \
	agent_id=$${AGENT_ID:-security-operations-expert}; \
	curl -s -X POST "$$api_base/api/chat" \
		-H 'Content-Type: application/json' \
		-H "Authorization: Bearer $${api_key:-change-me}" \
		-d "{\"message\":\"你好，请说明你当前可用的 agents 和 skills。\",\"agent_id\":\"$$agent_id\"}" | $(PYTHON_RUN) -m json.tool

codex-guard:
	$(PYTHON_RUN) .codex/skills/codex-config-optimizer/scripts/audit_codex_config.py --fail
	$(PYTHON_RUN) scripts/check_defensive_security_boundary.py
	$(PYTHON_RUN) scripts/check_codex_governance.py --mode fail $(GOVERNANCE_BASE_REF_ARG)
	$(PYTHON_RUN) scripts/check_stage_language.py
	$(PYTHON_RUN) scripts/check_version_consistency.py
	$(PYTHON_RUN) scripts/audit_openapi_contract.py --fail
	AGENTGOV_PYTHON="$(abspath $(PYTHON))" bash scripts/check_openapi_type_drift.sh
	$(PYTHON_RUN) scripts/check_docs_governance.py
	$(PYTHON_RUN) scripts/check_test_quality_policy.py --manifest-only --policy $(QUALITY_POLICY)

openapi-contract-check:
	$(PYTHON_RUN) scripts/audit_openapi_contract.py --fail

openapi-type-drift-check:
	AGENTGOV_PYTHON="$(abspath $(PYTHON))" bash scripts/check_openapi_type_drift.sh

container-openapi-check:
	$(CONTAINER_ACCEPTANCE) --profile core -- make --no-print-directory _container-openapi-check

_container-openapi-check:
	@$(REQUIRE_CONTAINER_ACCEPTANCE)
	@host_port=$${HOST_PORT:-$$(awk -F= '$$1 == "HOST_PORT" {sub(/^[^=]*=/, ""); print; exit}' "$(COMPOSE_ENV_FILE)" 2>/dev/null)}; \
	api_base=$${API_BASE:-$$(awk -F= '$$1 == "API_BASE" {sub(/^[^=]*=/, ""); print; exit}' "$(COMPOSE_ENV_FILE)" 2>/dev/null)}; \
	api_base=$${api_base:-http://localhost:$${host_port:-58080}}; \
	if ! $(PYTHON_RUN) scripts/audit_openapi_contract.py --base-url "$$api_base" --compare-local --fail >/dev/null 2>&1; then \
		echo "failure_phase=openapi_contract failure_code=ChildExit" >&2; \
		exit 1; \
	fi; \
	if ! RUNTIME_API_BASE="$$api_base" $(CONTAINER_ACCEPTANCE_PNPM) --silent --dir frontend run verify:openapi-docs >/dev/null 2>&1; then \
		echo "failure_phase=openapi_docs_browser failure_code=ChildExit" >&2; \
		exit 1; \
	fi; \
	echo "OPENAPI_CONTAINER_CHECK_OK"

container-health-e2e:
	$(CONTAINER_ACCEPTANCE) --profile isolated-health -- make --no-print-directory _container-health-e2e

_container-health-e2e:
	@$(REQUIRE_CONTAINER_ACCEPTANCE)
	bash scripts/run_healthcheck_container_e2e.sh

_container-health-diagnose:
	@$(PYTHON_RUN) scripts/diagnose_runtime_health.py --api-base "$$API_BASE" --wait-seconds 10

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

container-live-test:
	$(CONTAINER_ACCEPTANCE) --profile core -- make --no-print-directory _container-live-test

_container-live-test:
	@$(REQUIRE_CONTAINER_ACCEPTANCE)
	$(COMPOSE) run --rm --entrypoint sh \
		-e REQUIRE_LIVE_RUNTIME=1 \
		-e PYTHONDONTWRITEBYTECODE=1 \
		-e AGENT_GOV_CONTAINER_ACCEPTANCE_ACTIVE \
		-e AGENT_GOV_ACCEPTANCE_RUN_ID \
		-v "$(CURDIR):/app:ro" -w /app \
		claude-agent-api -lc 'python -m pytest -p no:cacheprovider -q -rs tests/test_live_runtime_acceptance.py'

container-workspace-pytest-test:
	$(CONTAINER_ACCEPTANCE) --profile agent-test -- make --no-print-directory _container-workspace-pytest-test

_container-workspace-pytest-test:
	@$(REQUIRE_CONTAINER_ACCEPTANCE)
	@test "$$AGENT_GOV_CONTAINER_ACCEPTANCE_PROFILE" = "agent-test" || { echo "Agent test lane requires the isolated agent-test profile." >&2; exit 1; }
	@api_key=$${API_KEY:-$$(awk -F= '$$1 == "API_KEY" {sub(/^[^=]*=/, ""); print; exit}' "$(COMPOSE_ENV_FILE)" 2>/dev/null)}; \
	API_KEY="$$api_key" $(PYTHON_RUN) scripts/run_agent_test_container_e2e.py

container-speech-summary-test:
	$(CONTAINER_ACCEPTANCE) --profile core -- make --no-print-directory _container-speech-summary-test

_container-speech-summary-test:
	@$(REQUIRE_CONTAINER_ACCEPTANCE)
	@raw_enabled=$$(awk -F= '$$1 == "ENABLE_AGENT_RUNTIME_RAW_EVENTS" {sub(/^[^=]*=/, ""); print; exit}' "$(COMPOSE_ENV_FILE)" 2>/dev/null | tr '[:upper:]' '[:lower:]'); \
	case "$$raw_enabled" in 1|true|yes|on) ;; \
		*) echo "container-speech-summary-test requires ENABLE_AGENT_RUNTIME_RAW_EVENTS=true in the selected complete Compose env." >&2; exit 1 ;; \
	esac; \
	host_port=$${HOST_PORT:-$$(awk -F= '$$1 == "HOST_PORT" {sub(/^[^=]*=/, ""); print; exit}' "$(COMPOSE_ENV_FILE)" 2>/dev/null)}; \
	api_base=$${API_BASE:-$$(awk -F= '$$1 == "API_BASE" {sub(/^[^=]*=/, ""); print; exit}' "$(COMPOSE_ENV_FILE)" 2>/dev/null)}; \
	api_base=$${api_base:-http://localhost:$${host_port:-58080}}; \
	api_key=$$(awk -F= '$$1 == "API_KEY" {sub(/^[^=]*=/, ""); print; exit}' "$(COMPOSE_ENV_FILE)" 2>/dev/null); \
	API_KEY="$$api_key" $(PYTHON_RUN) scripts/verify_speech_summary_container.py --base-url "$$api_base"
