# 基础镜像拉取由 Docker daemon registry mirror 或内网仓库处理；本项目没有统一可用的国内基础镜像仓库可直接写死。
FROM node:22-alpine

WORKDIR /ui

COPY frontend/package.json frontend/pnpm-lock.yaml ./
# 构建阶段 pnpm 源固定使用 npmmirror，避免 docker/.env 或宿主环境覆盖。
ENV COREPACK_NPM_REGISTRY=https://registry.npmmirror.com
ENV PNPM_CONFIG_REGISTRY=https://registry.npmmirror.com
RUN corepack enable \
    && corepack prepare pnpm@10.30.3 --activate \
    && pnpm install --frozen-lockfile

COPY frontend/ ./

ENV FRONTEND_PORT=5173
ENV VITE_RUNTIME_API_BASE=http://localhost:48080
ENV VITE_LANGFUSE_URL=http://localhost:43000
ENV VITE_DEV_PROXY_TARGET=http://claude-agent-api:8080
EXPOSE 5173

CMD ["sh", "-c", "pnpm dev --host 0.0.0.0 --port ${FRONTEND_PORT:-5173}"]
