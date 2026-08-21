"""不依赖第三方日志库的轻量 JSON 日志设置。"""

from __future__ import annotations

import json
import logging
import sys
from collections.abc import Mapping, MutableMapping
from typing import TextIO


class _JSONFormatter(logging.Formatter):
    def format(self, record: logging.LogRecord) -> str:
        payload: MutableMapping[str, object] = {
            "time": self.formatTime(record, "%Y-%m-%dT%H:%M:%S%z"),
            "level": record.levelname,
            "logger": record.name,
            "message": record.getMessage(),
            "service": "docreview-api",
        }
        for key, value in record.__dict__.items():
            if key not in payload and key not in _RESERVED_RECORD_FIELDS and _is_json_value(value):
                payload[key] = value
        if record.exc_info:
            payload["exception"] = self.formatException(record.exc_info)
        return json.dumps(payload, ensure_ascii=False, separators=(",", ":"))


_RESERVED_RECORD_FIELDS = frozenset(
    {
        "args",
        "asctime",
        "created",
        "exc_info",
        "exc_text",
        "filename",
        "funcName",
        "levelname",
        "levelno",
        "lineno",
        "module",
        "msecs",
        "msg",
        "name",
        "pathname",
        "process",
        "processName",
        "relativeCreated",
        "stack_info",
        "thread",
        "threadName",
    }
)


def _is_json_value(value: object) -> bool:
    return value is None or isinstance(value, (str, int, float, bool, list, dict))


def configure_json_logging(*, level: str = "INFO", stream: TextIO | None = None) -> logging.Logger:
    logger = logging.getLogger("docreview")
    logger.setLevel(level.upper())
    logger.handlers.clear()
    logger.propagate = False
    handler = logging.StreamHandler(stream or sys.stdout)
    handler.setFormatter(_JSONFormatter())
    logger.addHandler(handler)
    return logger


def log_context(**values: object) -> Mapping[str, object]:
    """为构造结构化日志扩展字段的调用方返回类型化 mapping。"""

    return values


__all__ = ["configure_json_logging", "log_context"]
