FROM python:3.11-slim

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    WORKSPACE_DIR=/workspace \
    DATA_DIR=/data \
    CLAUDE_HOME=/home/agentuser/.claude \
    CLAUDE_CONFIG_DIR=/data/claude-config

WORKDIR /app

RUN apt-get update && apt-get install -y --no-install-recommends \
    bash \
    ca-certificates \
    curl \
    git \
    ripgrep \
    nodejs \
    npm \
    && rm -rf /var/lib/apt/lists/*

COPY requirements.txt /app/requirements.txt
RUN pip install --no-cache-dir --upgrade pip \
    && pip install --no-cache-dir -r /app/requirements.txt

COPY app /app/app

RUN useradd -m -u 10001 agentuser \
    && mkdir -p /workspace /data /data/claude-config /home/agentuser/.claude \
    && chown -R agentuser:agentuser /workspace /data /home/agentuser/.claude /app

USER agentuser

EXPOSE 8080

CMD ["uvicorn", "app.main:app", "--host", "0.0.0.0", "--port", "8080"]
