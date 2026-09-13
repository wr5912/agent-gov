# 基础镜像拉取由 Docker daemon registry mirror 或内网仓库处理；本项目没有统一可用的国内基础镜像仓库可直接写死。
FROM node:22-alpine@sha256:c610fcdfb1d5b4740dd70c284ed3cb16bb857e0f7166196e36a5501df7a3aa32

WORKDIR /ui

COPY frontend/package.json frontend/pnpm-lock.yaml ./
# 构建阶段 pnpm 源固定使用 npmmirror，避免 docker/.env 或宿主环境覆盖。
ENV COREPACK_NPM_REGISTRY=https://registry.npmmirror.com
ENV NPM_CONFIG_REGISTRY=https://registry.npmmirror.com
RUN corepack enable \
    && corepack prepare pnpm@10.30.3 --activate \
    && pnpm install --frozen-lockfile

COPY frontend/ ./

ENV FRONTEND_PORT=5173
ENV VITE_RUNTIME_API_BASE=http://localhost:50400
ENV VITE_LANGFUSE_URL=http://localhost:50402
ENV VITE_DEV_PROXY_TARGET=http://agent-gov-api:8080
EXPOSE 5173

ARG AGENT_GOV_ACCEPTANCE_RUN_ID=unmanaged
ARG AGENTGOV_SOURCE_ARTIFACT_SHA256=unmanaged
RUN source_digest="${AGENTGOV_SOURCE_ARTIFACT_SHA256}"; \
    test "${#source_digest}" -eq 64; \
    case "$source_digest" in *[!0-9a-f]*) exit 1 ;; esac
LABEL io.agentgov.acceptance-run-id="${AGENT_GOV_ACCEPTANCE_RUN_ID}" \
      io.agentgov.source-artifact-sha256="${AGENTGOV_SOURCE_ARTIFACT_SHA256}"

CMD ["sh", "-c", "pnpm dev --host 0.0.0.0 --port ${FRONTEND_PORT:-5173}"]
