# 基础镜像由 Docker daemon mirror 或内网仓库解析；Python 依赖固定走项目国内源。
FROM python:3.11-slim

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    PIP_INDEX_URL=https://mirrors.aliyun.com/pypi/simple/ \
    PIP_TRUSTED_HOST=mirrors.aliyun.com \
    PIP_DISABLE_PIP_VERSION_CHECK=1 \
    UV_INDEX_URL=https://mirrors.aliyun.com/pypi/simple/ \
    UV_DEFAULT_INDEX=https://mirrors.aliyun.com/pypi/simple/ \
    AGENTSCOPE_RUNTIME_HOST=0.0.0.0 \
    AGENTSCOPE_RUNTIME_PORT=8090 \
    AGENTSCOPE_RUNTIME_DATA_DIR=/runtime-data \
    AGENTSCOPE_RUNTIME_BUSINESS_AGENTS_ROOT=/business-agents \
    AGENTSCOPE_RUNTIME_CANDIDATES_ROOT=/candidate-workspaces \
    AGENTSCOPE_RUNTIME_WORKSPACES_ROOT=/runtime-workspaces \
    AGENTSCOPE_RUNTIME_DATABASE_URL=sqlite+aiosqlite:////runtime-data/agentscope.db

WORKDIR /app

RUN set -eux; \
    if [ -f /etc/apt/sources.list.d/debian.sources ]; then \
        sed -i \
            -e "s|http://deb.debian.org/debian-security|https://mirrors.aliyun.com/debian-security|g" \
            -e "s|https://deb.debian.org/debian-security|https://mirrors.aliyun.com/debian-security|g" \
            -e "s|http://deb.debian.org/debian|https://mirrors.aliyun.com/debian|g" \
            -e "s|https://deb.debian.org/debian|https://mirrors.aliyun.com/debian|g" \
            /etc/apt/sources.list.d/debian.sources; \
    fi; \
    apt-get update; \
    apt-get install -y --no-install-recommends \
        bash bubblewrap ca-certificates curl git jq ripgrep socat; \
    /usr/bin/jq --version; \
    rm -rf /var/lib/apt/lists/*; \
    python -m pip install --no-cache-dir \
        --index-url https://mirrors.aliyun.com/pypi/simple/ \
        --trusted-host mirrors.aliyun.com uv

COPY agentscope_runtime/requirements.txt /app/agentscope_runtime/requirements.txt
RUN uv pip install --system --no-cache \
    --index-url https://mirrors.aliyun.com/pypi/simple/ \
    -r /app/agentscope_runtime/requirements.txt

# SDK gateway 在独立 venv 中安装；构建期封存本镜像已安装版本的完整 wheel 闭包。
# /usr 在 Bubblewrap 内只读可见，不能将这些资产放进未挂载的 /opt。
RUN set -eux; \
    install -d -m 755 /usr/local/share/agentgov/gateway-wheels; \
    uv pip freeze --system --exclude-editable > /tmp/gateway-requirements.txt; \
    python -m pip wheel --no-cache-dir --no-deps \
        --wheel-dir /usr/local/share/agentgov/gateway-wheels \
        -r /tmp/gateway-requirements.txt; \
    export UV_OFFLINE=1 \
        UV_FIND_LINKS=file:///usr/local/share/agentgov/gateway-wheels \
        UV_PYTHON=/usr/local/bin/python UV_PYTHON_DOWNLOADS=never; \
    uv venv /tmp/gateway-offline-check; \
    uv pip install --python /tmp/gateway-offline-check/bin/python \
        'mcp<2.0.0' uvicorn fastapi httpx 'agentscope==2.0.8'; \
    uv pip install --python /tmp/gateway-offline-check/bin/python --no-deps agentscope; \
    /tmp/gateway-offline-check/bin/python -I -c \
        'import agentscope, fastapi, uvicorn, httpx, mcp; from importlib.metadata import version; assert version("agentscope") == "2.0.8"; assert int(version("mcp").split(".")[0]) < 2'; \
    /usr/local/bin/uv --version; \
    /usr/bin/rg --version; \
    rm -rf /tmp/gateway-offline-check; \
    rm /tmp/gateway-requirements.txt

COPY agentscope_runtime /app/agentscope_runtime
COPY agentgov_harness_digest.py /app/agentgov_harness_digest.py

ARG AGENT_GOV_RUNTIME_UID=1000
ARG AGENT_GOV_RUNTIME_GID=1000
RUN set -eux; \
    case "${AGENT_GOV_RUNTIME_UID}" in 0|[1-9]*) ;; *) exit 1 ;; esac; \
    case "${AGENT_GOV_RUNTIME_UID}" in *[!0-9]*) exit 1 ;; esac; \
    case "${AGENT_GOV_RUNTIME_GID}" in 0|[1-9]*) ;; *) exit 1 ;; esac; \
    case "${AGENT_GOV_RUNTIME_GID}" in *[!0-9]*) exit 1 ;; esac; \
    install -d -m 2770 -o "${AGENT_GOV_RUNTIME_UID}" -g "${AGENT_GOV_RUNTIME_GID}" \
        /runtime-data /runtime-workspaces

USER ${AGENT_GOV_RUNTIME_UID}:${AGENT_GOV_RUNTIME_GID}

EXPOSE 8090

ARG AGENT_GOV_ACCEPTANCE_RUN_ID=unmanaged
LABEL io.agentgov.acceptance-run-id="${AGENT_GOV_ACCEPTANCE_RUN_ID}"

ENTRYPOINT ["python", "-m", "agentscope_runtime"]
