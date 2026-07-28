FROM python:3.11-slim

WORKDIR /e2e
COPY docker/e2e/slow_vllm.py /e2e/slow_vllm.py

EXPOSE 8000
ARG AGENT_GOV_ACCEPTANCE_RUN_ID=unmanaged
LABEL io.agentgov.acceptance-run-id="${AGENT_GOV_ACCEPTANCE_RUN_ID}"

CMD ["python", "/e2e/slow_vllm.py"]
