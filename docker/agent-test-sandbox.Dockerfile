# 基础镜像拉取由 Docker daemon registry mirror 或内网仓库处理；构建依赖固定走国内 PyPI。
FROM python:3.11-slim AS builder

WORKDIR /build

COPY VERSION /build/VERSION
COPY packages/agentgov-testkit /build/packages/agentgov-testkit

RUN python -m pip install --no-cache-dir \
        --index-url https://mirrors.aliyun.com/pypi/simple/ \
        --trusted-host mirrors.aliyun.com \
        uv \
    && uv pip install --target /opt/agent-test --no-cache \
        --index-url https://mirrors.aliyun.com/pypi/simple/ \
        httpx==0.28.1 \
        pytest==9.0.3 \
        /build/packages/agentgov-testkit

FROM python:3.11-slim AS runtime-root

COPY --from=builder /opt/agent-test /usr/local/lib/python3.11/site-packages

RUN mkdir -p /workspace /output \
    && chown 65532:65532 /workspace /output

# ``python:3.11-slim`` 的镜像配置包含构建期 Env。新的 scratch 最终层保留完整
# 运行文件系统并清空继承配置，使 executor 能证明 sandbox 只收到平台定义的环境。
FROM scratch

COPY --from=runtime-root / /

WORKDIR /workspace
USER 65532:65532

LABEL io.agentgov.agent-test.sandbox="true"

ARG AGENT_GOV_ACCEPTANCE_RUN_ID=unmanaged
ARG AGENT_GOV_ACCEPTANCE_CANDIDATE_TREE=unmanaged
ARG AGENT_GOV_ACCEPTANCE_SELECTED_ENV_SHA256=unmanaged
LABEL io.agentgov.acceptance-run-id="${AGENT_GOV_ACCEPTANCE_RUN_ID}" \
      io.agentgov.acceptance-candidate-tree="${AGENT_GOV_ACCEPTANCE_CANDIDATE_TREE}" \
      io.agentgov.acceptance-selected-env-sha256="${AGENT_GOV_ACCEPTANCE_SELECTED_ENV_SHA256}"

ENTRYPOINT []
CMD ["/usr/local/bin/python", "-I", "-P", "-m", "pytest", "-q", "--import-mode=importlib", "-p", "agentgov_testkit.pytest_plugin", "tests"]
