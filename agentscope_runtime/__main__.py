"""AgentScope Runtime 容器入口。"""

import uvicorn

from .service import create_runtime_app
from .settings import RuntimeSettings


def main() -> None:
    """Start exactly one Uvicorn worker for the in-memory message bus."""

    settings = RuntimeSettings.from_env()
    uvicorn.run(
        create_runtime_app(settings),
        host=settings.host,
        port=settings.port,
        workers=1,
        access_log=False,
    )


if __name__ == "__main__":
    main()
