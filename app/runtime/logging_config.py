from __future__ import annotations

import logging

_LOG_FORMAT = "%(asctime)s %(levelname)s %(name)s %(message)s"
_CONTROLLED_LOGGERS = ("app", "uvicorn", "uvicorn.error", "uvicorn.access")


def resolve_log_level(level_name: str | None) -> int:
    normalized = (level_name or "info").strip().upper()
    level = logging.getLevelName(normalized)
    if isinstance(level, int):
        return level
    return logging.INFO


def configure_runtime_logging(level_name: str | None) -> int:
    level = resolve_log_level(level_name)
    root_logger = logging.getLogger()
    if not root_logger.handlers:
        logging.basicConfig(level=level, format=_LOG_FORMAT)
    else:
        root_logger.setLevel(level)
    for logger_name in _CONTROLLED_LOGGERS:
        logging.getLogger(logger_name).setLevel(level)
    return level
