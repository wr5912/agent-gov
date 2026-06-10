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
    WORKSPACE_DIR=/main-workspace \
    MAIN_WORKSPACE_DIR=/main-workspace \
    ATTRIBUTION_ANALYZER_WORKSPACE_DIR=/attribution-analyzer-workspace \
    PROPOSAL_GENERATOR_WORKSPACE_DIR=/proposal-generator-workspace \
    EXECUTION_OPTIMIZER_WORKSPACE_DIR=/execution-optimizer-workspace \
    EVAL_CASE_GOVERNOR_WORKSPACE_DIR=/eval-case-governor-workspace \
    REGRESSION_IMPACT_ANALYZER_WORKSPACE_DIR=/regression-impact-analyzer-workspace \
    DATA_DIR=/data \
    CLAUDE_ROOT=/claude-roots/main \
    MAIN_CLAUDE_ROOT=/claude-roots/main \
    ATTRIBUTION_ANALYZER_CLAUDE_ROOT=/claude-roots/attribution-analyzer \
    PROPOSAL_GENERATOR_CLAUDE_ROOT=/claude-roots/proposal-generator \
    EXECUTION_OPTIMIZER_CLAUDE_ROOT=/claude-roots/execution-optimizer \
    EVAL_CASE_GOVERNOR_CLAUDE_ROOT=/claude-roots/eval-case-governor \
    REGRESSION_IMPACT_ANALYZER_CLAUDE_ROOT=/claude-roots/regression-impact-analyzer \
    CLAUDE_HOME=/claude-roots/main/.claude

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
    ca-certificates \
    curl \
    git \
    ripgrep \
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

COPY app /app/app
COPY scripts/bootstrap_runtime_volume.py /app/scripts/bootstrap_runtime_volume.py
COPY scripts/runtime_cleanup.py /app/scripts/runtime_cleanup.py
COPY scripts/runtime_template_renderer.py /app/scripts/runtime_template_renderer.py
COPY docker/runtime-template /app/docker/runtime-template
COPY docker/entrypoint.sh /usr/local/bin/agent-gov-entrypoint

RUN chmod +x /usr/local/bin/agent-gov-entrypoint \
    && mkdir -p /main-workspace /attribution-analyzer-workspace /proposal-generator-workspace \
        /execution-optimizer-workspace /eval-case-governor-workspace /regression-impact-analyzer-workspace \
        /claude-roots/main/.claude /claude-roots/attribution-analyzer/.claude /claude-roots/proposal-generator/.claude \
        /claude-roots/execution-optimizer/.claude /claude-roots/eval-case-governor/.claude \
        /claude-roots/regression-impact-analyzer/.claude /data

ENTRYPOINT ["/usr/local/bin/agent-gov-entrypoint"]
CMD ["sh", "-c", "uvicorn app.main:app --host ${API_HOST:-0.0.0.0} --port ${API_PORT:-8080} --log-level ${LOG_LEVEL:-info}"]
