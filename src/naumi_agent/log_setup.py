"""日志配置."""

from __future__ import annotations

import logging
import sys

_NOISY_THIRD_PARTY_LOGGERS = ("litellm", "LiteLLM")


def setup_logging(level: str = "INFO") -> None:
    logging.basicConfig(
        level=getattr(logging, level.upper(), logging.INFO),
        format="%(asctime)s [%(levelname)s] %(name)s: %(message)s",
        datefmt="%H:%M:%S",
        stream=sys.stderr,
    )
    for logger_name in _NOISY_THIRD_PARTY_LOGGERS:
        logging.getLogger(logger_name).setLevel(logging.ERROR)
