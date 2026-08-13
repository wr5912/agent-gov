FROM python:3.11-slim

WORKDIR /e2e
COPY docker/e2e/slow_vllm.py /e2e/slow_vllm.py

EXPOSE 8000
ARG AGENT_GOV_ACCEPTANCE_RUN_ID=unmanaged
ARG AGENT_GOV_ACCEPTANCE_CANDIDATE_TREE=unmanaged
ARG AGENT_GOV_ACCEPTANCE_SELECTED_ENV_SHA256=unmanaged
LABEL io.agentgov.acceptance-run-id="${AGENT_GOV_ACCEPTANCE_RUN_ID}"
LABEL io.agentgov.acceptance-candidate-tree="${AGENT_GOV_ACCEPTANCE_CANDIDATE_TREE}"
LABEL io.agentgov.acceptance-selected-env-sha256="${AGENT_GOV_ACCEPTANCE_SELECTED_ENV_SHA256}"

CMD ["python", "/e2e/slow_vllm.py"]
