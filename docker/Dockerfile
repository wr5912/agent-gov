# 基础镜像镜像源依赖部署环境；如内部仓库提供国内 Python 镜像，可在 docker/.env 覆盖 PYTHON_IMAGE。
ARG PYTHON_IMAGE=python:3.11-slim
FROM ${PYTHON_IMAGE}

ARG APT_MIRROR=https://mirrors.aliyun.com/debian
ARG APT_SECURITY_MIRROR=https://mirrors.aliyun.com/debian-security
ARG PIP_INDEX_URL=https://mirrors.aliyun.com/pypi/simple/
ARG PIP_TRUSTED_HOST=mirrors.aliyun.com
ARG NPM_REGISTRY=https://registry.npmmirror.com

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    PIP_INDEX_URL=${PIP_INDEX_URL} \
    PIP_TRUSTED_HOST=${PIP_TRUSTED_HOST} \
    PIP_DISABLE_PIP_VERSION_CHECK=1 \
    UV_INDEX_URL=${PIP_INDEX_URL} \
    NPM_CONFIG_REGISTRY=${NPM_REGISTRY} \
    API_HOST=0.0.0.0 \
    API_PORT=8080 \
    WORKSPACE_DIR=/workspace \
    DATA_DIR=/data \
    CLAUDE_ROOT=/root \
    CLAUDE_HOME=/root/.claude

WORKDIR /app

RUN set -eux; \
    if [ -f /etc/apt/sources.list.d/debian.sources ]; then \
        sed -i \
            -e "s|http://deb.debian.org/debian-security|${APT_SECURITY_MIRROR}|g" \
            -e "s|https://deb.debian.org/debian-security|${APT_SECURITY_MIRROR}|g" \
            -e "s|http://deb.debian.org/debian|${APT_MIRROR}|g" \
            -e "s|https://deb.debian.org/debian|${APT_MIRROR}|g" \
            /etc/apt/sources.list.d/debian.sources; \
    elif [ -f /etc/apt/sources.list ]; then \
        sed -i \
            -e "s|http://deb.debian.org/debian-security|${APT_SECURITY_MIRROR}|g" \
            -e "s|https://deb.debian.org/debian-security|${APT_SECURITY_MIRROR}|g" \
            -e "s|http://deb.debian.org/debian|${APT_MIRROR}|g" \
            -e "s|https://deb.debian.org/debian|${APT_MIRROR}|g" \
            /etc/apt/sources.list; \
    fi; \
    apt-get update && apt-get install -y --no-install-recommends \
    bash \
    ca-certificates \
    curl \
    git \
    ripgrep \
    nodejs \
    npm \
    && rm -rf /var/lib/apt/lists/*

COPY requirements.txt /app/requirements.txt
# python:slim 不内置 uv；这里仅用阿里 PyPI bootstrap uv，项目依赖安装仍统一走 uv。
RUN python -m pip install --no-cache-dir uv \
    && uv pip install --system --no-cache -r /app/requirements.txt

COPY docker/a2ui-requirements.txt /app/docker/a2ui-requirements.txt
COPY docker/vendor/A2UI /app/vendor/A2UI
# a2ui-agent-sdk 来自 Google A2UI v0.9 的 agent_sdks/python 子目录。
# 源码已 vendor 到仓库，避免镜像构建依赖 GitHub；PyPI 依赖仍在 build 阶段下载。
RUN set -eux; \
    uv pip install --system --no-cache \
        -r /app/docker/a2ui-requirements.txt \
        /app/vendor/A2UI/agent_sdks/python; \
    LITELLM_LOCAL_MODEL_COST_MAP=True python -c "import importlib; [importlib.import_module(name) for name in ('a2a', 'google.adk', 'google.genai', 'litellm', 'jsonschema', 'dotenv', 'click', 'a2ui')]"

ENV LITELLM_LOCAL_MODEL_COST_MAP=True

COPY app /app/app
COPY docker/entrypoint.sh /usr/local/bin/claude-agent-runtime-entrypoint

RUN chmod +x /usr/local/bin/claude-agent-runtime-entrypoint \
    && mkdir -p /workspace /data /root/.claude

ENTRYPOINT ["/usr/local/bin/claude-agent-runtime-entrypoint"]
CMD ["sh", "-c", "uvicorn app.main:app --host ${API_HOST:-0.0.0.0} --port ${API_PORT:-8080}"]
