"""日志配置."""

from __future__ import annotations

import logging
import os
import sys

_NOISY_THIRD_PARTY_LOGGERS = ("litellm", "LiteLLM")


def suppress_startup_import_warnings() -> None:
    """Silence optional provider preload warnings before model modules import."""
    if os.getenv("NAUMI_SHOW_STARTUP_WARNINGS"):
        return
    for logger_name in _NOISY_THIRD_PARTY_LOGGERS:
        logging.getLogger(logger_name).setLevel(logging.ERROR)


def setup_logging(level: str = "INFO") -> None:
    logging.basicConfig(
        level=getattr(logging, level.upper(), logging.INFO),
        format="%(asctime)s [%(levelname)s] %(name)s: %(message)s",
        datefmt="%H:%M:%S",
        stream=sys.stderr,
    )
    suppress_startup_import_warnings()
