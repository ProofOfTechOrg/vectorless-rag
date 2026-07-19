from __future__ import annotations

import hashlib
import json
from contextlib import AbstractContextManager, nullcontext
from typing import Any, cast

from langfuse import Langfuse

from vectorless_rag.config import Settings

SENSITIVE_KEYS = frozenset(
    {
        "authorization",
        "api_key",
        "apikey",
        "password",
        "secret",
        "token",
        "database_url",
        "reasoning_content",
    }
)


def mask_payload(value: Any, *, maximum_string: int = 500) -> Any:
    if isinstance(value, dict):
        return {
            key: "[REDACTED]" if key.lower() in SENSITIVE_KEYS else mask_payload(item)
            for key, item in value.items()
        }
    if isinstance(value, list):
        return [mask_payload(item) for item in value]
    if isinstance(value, str) and len(value) > maximum_string:
        digest = hashlib.sha256(value.encode()).hexdigest()
        return {"sha256": digest, "bytes": len(value.encode()), "preview": value[:120]}
    return value


def langfuse_mask(*, data: Any, **kwargs: dict[str, Any]) -> Any:
    del kwargs
    return mask_payload(data)


class Observability:
    def __init__(self, settings: Settings) -> None:
        self.settings = settings
        self.client: Langfuse | None = None
        if settings.langfuse_public_key and settings.langfuse_secret_key:
            self.client = Langfuse(
                public_key=settings.langfuse_public_key,
                secret_key=settings.langfuse_secret_key.get_secret_value(),
                host=settings.langfuse_host,
                environment=settings.environment,
                release=settings.release,
                mask=langfuse_mask,
            )

    def span(
        self, name: str, trace_id: str, *, input: Any = None, metadata: dict[str, Any] | None = None
    ) -> AbstractContextManager[Any]:
        if self.client is None:
            return nullcontext()
        context = self.client.start_as_current_observation(
            name=name,
            as_type="span",
            trace_context={"trace_id": trace_id},
            input=mask_payload(input),
            metadata=mask_payload(metadata or {}),
        )
        return cast(AbstractContextManager[Any], context)

    def score(self, trace_id: str, name: str, value: float, comment: str | None = None) -> None:
        if self.client is None:
            return
        self.client.create_score(trace_id=trace_id, name=name, value=value, comment=comment)

    def flush(self) -> None:
        if self.client is not None:
            self.client.flush()


def redacted_locator_payload(items: list[dict[str, Any]]) -> dict[str, Any]:
    serialized = json.dumps(items, sort_keys=True, default=str).encode()
    return {"count": len(items), "sha256": hashlib.sha256(serialized).hexdigest()}
