"""
shared/logging.py
=================
Structured JSON logging used across all Python services.

Every log line is a JSON object with consistent fields:
  - timestamp  : ISO 8601 UTC
  - level      : DEBUG / INFO / WARNING / ERROR / CRITICAL
  - service    : which microservice emitted the log
  - message    : human-readable description
  - **kwargs   : any extra context fields

Usage:
    from shared.logging import get_logger
    log = get_logger("discovery")

    log.info("URL discovered", url="https://example.com", source="websub")
    log.error("Fetch failed", url="https://example.com", status=500)
"""

import json
import logging
import os
import sys
from datetime import datetime, timezone
from typing import Any


class JSONFormatter(logging.Formatter):
    """
    Formats log records as single-line JSON objects.
    Includes all extra keyword arguments passed to log calls.
    """

    def format(self, record: logging.LogRecord) -> str:
        # Base fields always present
        payload: dict[str, Any] = {
            "timestamp": datetime.now(timezone.utc).isoformat(),
            "level": record.levelname,
            "service": getattr(record, "service", "unknown"),
            "logger": record.name,
            "message": record.getMessage(),
        }

        # Attach any extra fields passed via log.info("msg", extra={...})
        skip = {
            "name", "msg", "args", "levelname", "levelno", "pathname",
            "filename", "module", "exc_info", "exc_text", "stack_info",
            "lineno", "funcName", "created", "msecs", "relativeCreated",
            "thread", "threadName", "processName", "process", "message",
            "service", "taskName",
        }
        for key, value in record.__dict__.items():
            if key not in skip:
                payload[key] = value

        # Attach exception info if present
        if record.exc_info:
            payload["exception"] = self.formatException(record.exc_info)

        return json.dumps(payload, default=str)


class TextFormatter(logging.Formatter):
    """
    Human-readable formatter for local development.
    Format: 2024-01-15 10:23:45 [INFO ] discovery | URL discovered url=...
    """
    COLOURS = {
        "DEBUG":    "\033[36m",   # cyan
        "INFO":     "\033[32m",   # green
        "WARNING":  "\033[33m",   # yellow
        "ERROR":    "\033[31m",   # red
        "CRITICAL": "\033[35m",   # magenta
    }
    RESET = "\033[0m"

    def format(self, record: logging.LogRecord) -> str:
        colour = self.COLOURS.get(record.levelname, "")
        reset = self.RESET
        service = getattr(record, "service", "unknown")
        ts = datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M:%S")

        skip = {
            "name", "msg", "args", "levelname", "levelno", "pathname",
            "filename", "module", "exc_info", "exc_text", "stack_info",
            "lineno", "funcName", "created", "msecs", "relativeCreated",
            "thread", "threadName", "processName", "process", "message",
            "service", "taskName",
        }
        extras = {
            k: v for k, v in record.__dict__.items()
            if k not in skip
        }
        extra_str = " ".join(f"{k}={v!r}" for k, v in extras.items())

        base = (
            f"{ts} {colour}[{record.levelname:<8}]{reset} "
            f"{service:<12} | {record.getMessage()}"
        )
        if extra_str:
            base += f"  {extra_str}"
        if record.exc_info:
            base += "\n" + self.formatException(record.exc_info)
        return base


class ServiceAdapter(logging.LoggerAdapter):
    """
    Injects the service name into every log record automatically.
    This means you never need to pass service= manually.
    """

    def process(self, msg, kwargs):
        kwargs.setdefault("extra", {})
        kwargs["extra"]["service"] = self.extra["service"]
        return msg, kwargs


def get_logger(service_name: str) -> ServiceAdapter:
    """
    Returns a logger configured for a specific service.

    Args:
        service_name: Short name of the service, e.g. "discovery", "crawler"

    Returns:
        A ServiceAdapter that injects service_name into every log record.

    Example:
        log = get_logger("discovery")
        log.info("Started", port=8080)
        # → {"timestamp": "...", "level": "INFO", "service": "discovery",
        #     "message": "Started", "port": 8080}
    """
    log_format = os.getenv("LOG_FORMAT", "json").lower()
    log_level = os.getenv("LOG_LEVEL", "INFO").upper()

    logger = logging.getLogger(service_name)
    logger.setLevel(getattr(logging, log_level, logging.INFO))

    # Avoid adding duplicate handlers on re-import
    if not logger.handlers:
        handler = logging.StreamHandler(sys.stdout)
        handler.setFormatter(
            JSONFormatter() if log_format == "json" else TextFormatter()
        )
        logger.addHandler(handler)

    logger.propagate = False
    return ServiceAdapter(logger, {"service": service_name})