from __future__ import annotations

import logging
import os


_OPTIONAL_AWS_WARNING_MARKERS = (
    "could not pre-load bedrock-runtime response stream shape",
    "could not pre-load sagemaker-runtime response stream shape",
)


class _LiteLLMOptionalAwsWarningFilter(logging.Filter):
    def filter(self, record: logging.LogRecord) -> bool:
        message = record.getMessage()
        return not any(marker in message for marker in _OPTIONAL_AWS_WARNING_MARKERS)


_FILTER = _LiteLLMOptionalAwsWarningFilter()
_CONFIGURED = False


def configure_litellm_import_defaults() -> None:
    """Apply project defaults before DSPy imports LiteLLM."""

    global _CONFIGURED
    os.environ.setdefault("LITELLM_LOCAL_MODEL_COST_MAP", "True")
    if _CONFIGURED:
        return
    logging.getLogger("LiteLLM").addFilter(_FILTER)
    _CONFIGURED = True
