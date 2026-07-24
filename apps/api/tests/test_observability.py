from typing import Any, cast

from vectorless_rag.config import Settings
from vectorless_rag.observability import (
    Observability,
    content_fingerprint,
    mask_payload,
    normalized_token_usage,
    redacted_locator_payload,
)


def test_masker_removes_credentials_and_reasoning() -> None:
    masked = mask_payload(
        {
            "authorization": "Bearer secret",
            "nested": {"password": "secret", "reasoning_content": "private chain"},
            "safe": "value",
        }
    )
    assert masked["authorization"] == "[REDACTED]"
    assert masked["nested"]["reasoning_content"] == "[REDACTED]"
    assert masked["safe"] == "value"


def test_long_passages_become_hash_size_and_short_preview() -> None:
    value = "x" * 1000
    masked = mask_payload(value)
    assert masked["bytes"] == 1000
    assert len(masked["sha256"]) == 64
    assert len(masked["preview"]) == 120
    locators = redacted_locator_payload([{"document": "a", "page": 1}])
    assert locators["count"] == 1


class _GenerationHandle:
    def __init__(self) -> None:
        self.update_payload: dict[str, Any] | None = None
        self.ended = False

    def update(self, **kwargs: Any) -> object:
        self.update_payload = kwargs
        return self

    def end(self) -> object:
        self.ended = True
        return self


class _LangfuseClient:
    def __init__(self, handle: _GenerationHandle) -> None:
        self.handle = handle
        self.start_payload: dict[str, Any] | None = None

    def start_observation(self, **kwargs: Any) -> _GenerationHandle:
        self.start_payload = kwargs
        return self.handle


class _ScoreClient:
    def __init__(self) -> None:
        self.payload: dict[str, Any] | None = None

    def create_score(self, **kwargs: Any) -> None:
        self.payload = kwargs


def test_score_comments_are_fingerprinted_before_langfuse() -> None:
    private_comment = "private user feedback"
    client = _ScoreClient()
    telemetry = Observability(Settings())
    telemetry.client = cast(Any, client)

    telemetry.score("trace", "user_feedback", 1, private_comment)

    assert client.payload is not None
    assert private_comment not in repr(client.payload)
    assert '"bytes":21' in str(client.payload["comment"])


def test_native_generation_telemetry_contains_only_hashes_and_normalized_tokens() -> None:
    prompt = "private prompt with api-key-secret"
    response = "private response with reasoning_content and cache_read_input_tokens"
    handle = _GenerationHandle()
    client = _LangfuseClient(handle)
    telemetry = Observability(Settings())
    telemetry.client = cast(Any, client)

    observation = telemetry.generation("generation", "provider/model", prompt, attempt=2)
    observation.finish(
        response,
        finish_reason="stop",
        usage={
            "prompt_tokens": 11,
            "completion_tokens": 7,
            "total_tokens": 18,
            "cache_read_input_tokens": 4,
            "cache_creation_input_tokens": 2,
            "reasoning_tokens": 3,
        },
    )

    assert client.start_payload == {
        "name": "generation",
        "as_type": "generation",
        "model": "provider/model",
        "input": content_fingerprint(prompt),
        "metadata": {"attempt": 2},
    }
    assert handle.update_payload == {
        "output": content_fingerprint(response),
        "metadata": {"attempt": 2, "finish_reason": "stop"},
        "usage_details": {
            "input": 11,
            "cache_read_input_tokens": 4,
            "cache_miss_input_tokens": 7,
            "cache_write_input_tokens": 2,
            "unattributed_input_tokens": 0,
            "output": 7,
            "reasoning_output_tokens": 3,
            "total": 18,
        },
    }
    assert handle.ended
    serialized = repr((client.start_payload, handle.update_payload))
    for forbidden in (
        prompt,
        response,
        "api-key-secret",
        "reasoning_content",
    ):
        assert forbidden not in serialized


def test_token_normalization_preserves_valid_buckets_and_repairs_invalid_partitions() -> None:
    assert normalized_token_usage(
        {"input_tokens": 3, "output_tokens": 4, "cache_creation_input_tokens": 99}
    ) == {
        "input": 3,
        "cache_read_input_tokens": 0,
        "cache_miss_input_tokens": 0,
        "cache_write_input_tokens": 0,
        "unattributed_input_tokens": 3,
        "output": 4,
        "reasoning_output_tokens": 0,
        "total": 7,
    }


def test_failed_generation_hashes_provider_output_and_normalizes_available_usage() -> None:
    response = "private malformed response"
    handle = _GenerationHandle()
    client = _LangfuseClient(handle)
    telemetry = Observability(Settings())
    telemetry.client = cast(Any, client)

    telemetry.generation("generation", "provider/model", "private prompt", attempt=3).fail(
        finish_reason="stop",
        output=response,
        usage={"prompt_tokens": 5, "completion_tokens": 2, "cache_read_input_tokens": 50},
    )

    assert handle.update_payload == {
        "metadata": {"attempt": 3, "finish_reason": "stop"},
        "level": "ERROR",
        "output": content_fingerprint(response),
        "usage_details": {
            "input": 5,
            "cache_read_input_tokens": 0,
            "cache_miss_input_tokens": 0,
            "cache_write_input_tokens": 0,
            "unattributed_input_tokens": 5,
            "output": 2,
            "reasoning_output_tokens": 0,
            "total": 7,
        },
    }
    assert response not in repr(handle.update_payload)
