# 基础镜像拉取由 Docker daemon registry mirror 或内网仓库处理；本项目没有统一可用的国内基础镜像仓库可直接写死。
FROM node:22-alpine

WORKDIR /ui

COPY frontend/package.json frontend/package-lock.json* ./
# 构建阶段 npm 源固定使用 npmmirror，避免 docker/.env 或宿主环境覆盖。
ENV NPM_CONFIG_REGISTRY=https://registry.npmmirror.com
RUN npm config set registry "https://registry.npmmirror.com" \
    && npm ci --registry=https://registry.npmmirror.com

COPY frontend/ ./

ENV FRONTEND_PORT=5173
ENV VITE_RUNTIME_API_BASE=http://localhost:58080
ENV VITE_RUNTIME_API_KEY=
ENV VITE_LANGFUSE_URL=http://localhost:53000
ENV VITE_DEV_PROXY_TARGET=http://claude-agent-api:8080
EXPOSE 5173

CMD ["sh", "-c", "npm run dev -- --host 0.0.0.0 --port ${FRONTEND_PORT:-5173}"]
