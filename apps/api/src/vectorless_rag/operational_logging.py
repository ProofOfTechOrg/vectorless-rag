from __future__ import annotations

import json
import logging
import math
import re
import uuid

_SAFE_STRING = re.compile(r"^[a-zA-Z0-9_.:/+-]{1,128}$")
_COMMON = frozenset(
    {
        "job_id",
        "document_id",
        "attempt_id",
        "trace_id",
        "worker_id",
        "stage",
        "status",
        "failure_code",
        "elapsed_ms",
        "page_count",
        "model_call_count",
        "input_tokens",
        "output_tokens",
        "telemetry_complete",
        "artifact_action",
        "release",
        "route",
        "cost_classification",
    }
)
EVENT_FIELDS = {
    "ingestion_attempt_started": _COMMON,
    "ingestion_attempt_completed": _COMMON,
    "ingestion_attempt_failed": _COMMON,
    "ingestion_attempt_abandoned": _COMMON,
    "query_started": _COMMON,
    "query_completed": _COMMON,
    "query_failed": _COMMON,
    "query_cancelled": _COMMON,
}


def _safe_scalar(value: object) -> str | int | float | bool:
    if isinstance(value, uuid.UUID):
        return str(value)
    if isinstance(value, bool):
        return value
    if isinstance(value, int) and -(2**63) <= value < 2**63:
        return value
    if isinstance(value, float) and math.isfinite(value) and abs(value) <= 1e15:
        return value
    if isinstance(value, str) and _SAFE_STRING.fullmatch(value):
        return value
    raise ValueError("operational event contains an unsafe scalar")


def event_json(event: str, **fields: object) -> str:
    allowed = EVENT_FIELDS.get(event)
    if allowed is None:
        raise ValueError("unknown operational event")
    if not fields.keys() <= allowed:
        raise ValueError("operational event contains a non-allowlisted field")
    payload: dict[str, str | int | float | bool] = {"event": event}
    for key, value in fields.items():
        payload[key] = _safe_scalar(value)
    return json.dumps(payload, sort_keys=True, separators=(",", ":"))


def emit_event(logger: logging.Logger, event: str, **fields: object) -> None:
    logger.info("%s", event_json(event, **fields))
