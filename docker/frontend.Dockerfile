ARG NODE_IMAGE=node:22-alpine
FROM ${NODE_IMAGE}

WORKDIR /ui

COPY frontend/package.json frontend/package-lock.json* ./
ARG NPM_REGISTRY=https://registry.npmmirror.com
RUN npm config set registry "${NPM_REGISTRY}" && npm ci

COPY frontend/ ./

ENV FRONTEND_PORT=5173
ENV VITE_RUNTIME_API_BASE=http://localhost:58080
ENV VITE_RUNTIME_API_KEY=
ENV VITE_DEV_PROXY_TARGET=http://claude-agent-api:8080
EXPOSE 5173

CMD ["sh", "-c", "npm run dev -- --host 0.0.0.0 --port ${FRONTEND_PORT:-5173}"]
