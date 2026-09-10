from __future__ import annotations

import argparse
from collections.abc import Sequence

import uvicorn

from app.runtime.settings import AppSettings, get_settings


def _serve_api(settings: AppSettings) -> int:
    """启动唯一的 AgentGov API 进程。

    AgentScope Runtime 是独立容器，由 Compose 负责生命周期和 readiness；控制面
    不再准备、校验或启动第二套 Agent Runtime。
    """

    uvicorn.run(
        "app.main:app",
        host=settings.api_host,
        port=settings.api_port,
        log_level=settings.log_level,
    )
    return 0


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="Start the AgentGov control-plane API backed by AgentScope Runtime.")
    parser.add_argument("command", choices=("api",), help="Only the AgentGov API is hosted by this image.")
    return parser


def main(argv: Sequence[str] | None = None) -> int:
    build_parser().parse_args(argv)
    return _serve_api(get_settings())


if __name__ == "__main__":
    raise SystemExit(main())
