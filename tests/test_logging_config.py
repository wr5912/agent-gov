from __future__ import annotations

import logging

from app.runtime.logging_config import configure_runtime_logging, resolve_log_level


def test_resolve_log_level_falls_back_to_info_for_unknown_value() -> None:
    assert resolve_log_level("debug") == logging.DEBUG
    assert resolve_log_level("bad-value") == logging.INFO


def test_configure_runtime_logging_controls_app_and_uvicorn_loggers() -> None:
    logger_names = ("", "app", "uvicorn", "uvicorn.error", "uvicorn.access")
    previous_levels = {name: logging.getLogger(name).level for name in logger_names}

    try:
        configured_level = configure_runtime_logging("debug")

        assert configured_level == logging.DEBUG
        for logger_name in logger_names:
            assert logging.getLogger(logger_name).level == logging.DEBUG
    finally:
        for logger_name, level in previous_levels.items():
            logging.getLogger(logger_name).setLevel(level)
