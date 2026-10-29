"""Structured operational logging without signal content or provider secrets."""

import json
import logging
from datetime import UTC, datetime


class JsonFormatter(logging.Formatter):
    def format(self, record: logging.LogRecord) -> str:
        """Format only allowlisted metadata; do not serialize arbitrary exception details."""
        entry: dict[str, object] = {
            "timestamp": datetime.now(UTC).isoformat(),
            "level": record.levelname,
            "event": record.getMessage(),
        }
        for key in (
            "request_id",
            "ticket_id",
            "document_id",
            "task_id",
            "tool",
            "tool_origin",
            "step",
            "outcome",
            "parameter_names",
            "model",
            "null_fields",
            "input_tokens",
            "output_tokens",
            "code",
            "validation_fields",
            "validation_error_types",
            "attempt",
            "status",
            "duration_ms",
            "total_tokens",
        ):
            if key in record.__dict__:
                entry[key] = record.__dict__[key]
        return json.dumps(entry)


def configure_logging() -> None:
    """Configure the application logger without enabling verbose HTTP client logs."""
    logger = logging.getLogger("triage")
    handler = logging.StreamHandler()
    handler.setFormatter(JsonFormatter())
    logger.handlers = [handler]
    logger.setLevel(logging.INFO)
    logger.propagate = False
