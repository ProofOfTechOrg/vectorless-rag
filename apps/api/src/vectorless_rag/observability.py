from __future__ import annotations

import hashlib
import json
from contextlib import AbstractContextManager, nullcontext
from typing import Any, Protocol, cast

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
        payload = cast(dict[object, object], value)
        return {
            key: (
                "[REDACTED]"
                if isinstance(key, str) and key.lower() in SENSITIVE_KEYS
                else mask_payload(item)
            )
            for key, item in payload.items()
        }
    if isinstance(value, list):
        return [mask_payload(item) for item in cast(list[object], value)]
    if isinstance(value, str) and len(value) > maximum_string:
        digest = hashlib.sha256(value.encode()).hexdigest()
        return {"sha256": digest, "bytes": len(value.encode()), "preview": value[:120]}
    return value


def langfuse_mask(*, data: Any, **kwargs: dict[str, Any]) -> Any:
    del kwargs
    return mask_payload(data)


def content_fingerprint(value: str | bytes) -> dict[str, str | int]:
    encoded = value.encode() if isinstance(value, str) else value
    return {"sha256": hashlib.sha256(encoded).hexdigest(), "bytes": len(encoded)}


def normalized_token_usage(usage: object) -> dict[str, int]:
    if usage is None:
        return {}
    if isinstance(usage, dict):
        values = cast(dict[str, object], usage)
    else:
        values = {
            name: getattr(usage, name, None)
            for name in (
                "input_tokens",
                "output_tokens",
                "total_tokens",
                "prompt_tokens",
                "completion_tokens",
            )
        }
    aliases = {
        "input": ("input_tokens", "prompt_tokens"),
        "output": ("output_tokens", "completion_tokens"),
        "total": ("total_tokens",),
    }
    result: dict[str, int] = {}
    for target, names in aliases.items():
        for name in names:
            value = values.get(name)
            if isinstance(value, int) and not isinstance(value, bool) and value >= 0:
                result[target] = value
                break
    if "total" not in result and "input" in result and "output" in result:
        result["total"] = result["input"] + result["output"]
    return result


class _GenerationHandle(Protocol):
    def update(self, **kwargs: Any) -> object: ...

    def end(self) -> object: ...


class GenerationObservation:
    def __init__(self, handle: _GenerationHandle | None, attempt: int) -> None:
        self.handle = handle
        self.attempt = attempt

    def finish(
        self, output: str | bytes, *, finish_reason: str | None, usage: object = None
    ) -> None:
        if self.handle is None:
            return
        metadata: dict[str, object] = {"attempt": self.attempt}
        if finish_reason is not None:
            metadata["finish_reason"] = finish_reason
        self.handle.update(
            output=content_fingerprint(output),
            metadata=metadata,
            usage_details=normalized_token_usage(usage),
        )
        self.handle.end()

    def fail(
        self,
        *,
        finish_reason: str | None = None,
        output: str | bytes | None = None,
        usage: object = None,
    ) -> None:
        if self.handle is None:
            return
        metadata: dict[str, object] = {"attempt": self.attempt}
        if finish_reason is not None:
            metadata["finish_reason"] = finish_reason
        payload: dict[str, object] = {"metadata": metadata, "level": "ERROR"}
        if output is not None:
            payload["output"] = content_fingerprint(output)
        normalized_usage = normalized_token_usage(usage)
        if normalized_usage:
            payload["usage_details"] = normalized_usage
        self.handle.update(**payload)
        self.handle.end()


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

    def generation(
        self, name: str, model: str, prompt: str | bytes, *, attempt: int
    ) -> GenerationObservation:
        if self.client is None:
            return GenerationObservation(None, attempt)
        handle = self.client.start_observation(
            name=name,
            as_type="generation",
            model=model,
            input=content_fingerprint(prompt),
            metadata={"attempt": attempt},
        )
        return GenerationObservation(cast(_GenerationHandle, handle), attempt)

    def flush(self) -> None:
        if self.client is not None:
            self.client.flush()


def redacted_locator_payload(items: list[dict[str, Any]]) -> dict[str, Any]:
    serialized = json.dumps(items, sort_keys=True, default=str).encode()
    return {"count": len(items), "sha256": hashlib.sha256(serialized).hexdigest()}
