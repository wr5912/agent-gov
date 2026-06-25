FROM python:3.11-slim

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    PIP_INDEX_URL=https://mirrors.aliyun.com/pypi/simple/ \
    PIP_TRUSTED_HOST=mirrors.aliyun.com \
    PIP_DISABLE_PIP_VERSION_CHECK=1 \
    UV_INDEX_URL=https://mirrors.aliyun.com/pypi/simple/ \
    UV_DEFAULT_INDEX=https://mirrors.aliyun.com/pypi/simple/ \
    LITELLM_LOCAL_MODEL_COST_MAP=True

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
    apt-get update && apt-get install -y --no-install-recommends ca-certificates curl \
    && rm -rf /var/lib/apt/lists/*

RUN python -m pip install --no-cache-dir \
        --index-url https://mirrors.aliyun.com/pypi/simple/ \
        --trusted-host mirrors.aliyun.com \
        uv \
    && uv pip install --system --no-cache \
        --index-url https://mirrors.aliyun.com/pypi/simple/ \
        "litellm[proxy]==1.88.1"

COPY docker/litellm_sidecar_entrypoint.py /app/litellm_sidecar_entrypoint.py

ENTRYPOINT ["python", "/app/litellm_sidecar_entrypoint.py"]
