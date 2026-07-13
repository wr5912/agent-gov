# 基础镜像拉取由 Docker daemon registry mirror 或内网仓库处理；本项目没有统一可用的国内基础镜像仓库可直接写死。
FROM python:3.11-slim

# 构建阶段依赖源固定使用国内镜像，避免 docker/.env 或宿主环境覆盖。
ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    PIP_INDEX_URL=https://mirrors.aliyun.com/pypi/simple/ \
    PIP_TRUSTED_HOST=mirrors.aliyun.com \
    PIP_DISABLE_PIP_VERSION_CHECK=1 \
    UV_INDEX_URL=https://mirrors.aliyun.com/pypi/simple/ \
    UV_DEFAULT_INDEX=https://mirrors.aliyun.com/pypi/simple/ \
    NPM_CONFIG_REGISTRY=https://registry.npmmirror.com \
    API_HOST=0.0.0.0 \
    API_PORT=8080 \
    GOVERNOR_WORKSPACE_DIR=/governor-workspace \
    DATA_DIR=/data \
    GOVERNOR_CLAUDE_ROOT=/claude-roots/governor

WORKDIR /app

RUN set -eux; \
    if [ -f /etc/apt/sources.list.d/debian.sources ]; then \
        sed -i \
            -e "s|http://deb.debian.org/debian-security|https://mirrors.aliyun.com/debian-security|g" \
            -e "s|https://deb.debian.org/debian-security|https://mirrors.aliyun.com/debian-security|g" \
            -e "s|http://deb.debian.org/debian|https://mirrors.aliyun.com/debian|g" \
            -e "s|https://deb.debian.org/debian|https://mirrors.aliyun.com/debian|g" \
            /etc/apt/sources.list.d/debian.sources; \
    elif [ -f /etc/apt/sources.list ]; then \
        sed -i \
            -e "s|http://deb.debian.org/debian-security|https://mirrors.aliyun.com/debian-security|g" \
            -e "s|https://deb.debian.org/debian-security|https://mirrors.aliyun.com/debian-security|g" \
            -e "s|http://deb.debian.org/debian|https://mirrors.aliyun.com/debian|g" \
            -e "s|https://deb.debian.org/debian|https://mirrors.aliyun.com/debian|g" \
            /etc/apt/sources.list; \
    fi; \
    apt-get update && apt-get install -y --no-install-recommends \
    bash \
    bubblewrap \
    ca-certificates \
    curl \
    git \
    ripgrep \
    socat \
    nodejs \
    npm \
    && rm -rf /var/lib/apt/lists/*

COPY requirements.txt /app/requirements.txt
# python:slim 不内置 uv；这里仅用阿里 PyPI bootstrap uv，项目依赖安装仍统一走 uv。
RUN python -m pip install --no-cache-dir \
        --index-url https://mirrors.aliyun.com/pypi/simple/ \
        --trusted-host mirrors.aliyun.com \
        uv \
    && uv pip install --system --no-cache \
        --index-url https://mirrors.aliyun.com/pypi/simple/ \
        -r /app/requirements.txt

COPY docker/a2ui-requirements.txt /app/docker/a2ui-requirements.txt
COPY docker/vendor/A2UI /app/vendor/A2UI
# a2ui-agent-sdk 来自 Google A2UI v0.9 的 agent_sdks/python 子目录。
# 源码已 vendor 到仓库，避免镜像构建依赖 GitHub；PyPI 依赖仍在 build 阶段下载。
RUN set -eux; \
    uv pip install --system --no-cache \
        --index-url https://mirrors.aliyun.com/pypi/simple/ \
        -r /app/docker/a2ui-requirements.txt \
        /app/vendor/A2UI/agent_sdks/python; \
    LITELLM_LOCAL_MODEL_COST_MAP=True python -c "import importlib; [importlib.import_module(name) for name in ('a2a', 'google.adk', 'google.genai', 'litellm', 'jsonschema', 'dotenv', 'click', 'a2ui')]"

ENV LITELLM_LOCAL_MODEL_COST_MAP=True

COPY VERSION /app/VERSION
COPY app /app/app
COPY scripts/bootstrap_runtime_volume.py /app/scripts/bootstrap_runtime_volume.py
COPY scripts/reconcile_business_agent_workspace_policy.py /app/scripts/reconcile_business_agent_workspace_policy.py
COPY scripts/retire_runtime_seed_assets.py /app/scripts/retire_runtime_seed_assets.py
COPY scripts/runtime_cleanup.py /app/scripts/runtime_cleanup.py
COPY scripts/runtime_template_renderer.py /app/scripts/runtime_template_renderer.py
COPY docker/runtime-volume-seeds /app/docker/runtime-volume-seeds
COPY docker/entrypoint.sh /usr/local/bin/agent-gov-entrypoint

RUN chmod +x /usr/local/bin/agent-gov-entrypoint \
    && mkdir -p /governor-workspace /claude-roots/governor/.claude /data

ENTRYPOINT ["/usr/local/bin/agent-gov-entrypoint"]
CMD ["sh", "-c", "uvicorn app.main:app --host ${API_HOST:-0.0.0.0} --port ${API_PORT:-8080} --log-level ${LOG_LEVEL:-info}"]
