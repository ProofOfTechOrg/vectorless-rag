from __future__ import annotations

from types import SimpleNamespace
from typing import Any, cast

import pytest
from litellm.exceptions import AuthenticationError, BadRequestError

from vectorless_rag.config import Settings
from vectorless_rag.pageindex_runtime import (
    InvalidPageIndexJSON,
    PageIndexLLMError,
    PageIndexRuntime,
    extract_json,
)


class _Observation:
    def finish(self, output: str, *, finish_reason: str | None, usage: object = None) -> None:
        del output, finish_reason, usage

    def fail(
        self,
        *,
        finish_reason: str | None = None,
        output: str | bytes | None = None,
        usage: object = None,
    ) -> None:
        del finish_reason, output, usage


class _Observability:
    def generation(self, name: str, model: str, prompt: str, *, attempt: int) -> _Observation:
        del name, model, prompt, attempt
        return _Observation()


def _response(
    content: str,
    finish_reason: str = "stop",
    *,
    tool_calls: list[object] | None = None,
    reasoning_content: str | None = None,
) -> object:
    return SimpleNamespace(
        choices=[
            SimpleNamespace(
                finish_reason=finish_reason,
                message=SimpleNamespace(
                    content=content,
                    tool_calls=tool_calls or [],
                    reasoning_content=reasoning_content,
                ),
            )
        ],
        usage=SimpleNamespace(prompt_tokens=11, completion_tokens=7, total_tokens=18),
    )


def _runtime(
    completion: Any, *, acompletion: Any | None = None, attempts: int = 3
) -> PageIndexRuntime:
    async def unused_async(**kwargs: object) -> object:
        del kwargs
        raise AssertionError("unexpected async completion")

    return PageIndexRuntime(
        Settings(pageindex_llm_max_attempts=attempts),
        completion=completion,
        acompletion=acompletion or unused_async,
        sleep=lambda delay: None,
        async_sleep=cast(Any, _async_noop),
        random_value=lambda: 0.5,
        observability=cast(Any, _Observability()),
    )


async def _async_noop(delay: float) -> None:
    del delay


def test_strict_json_parsing_allows_fences_and_narrow_repairs() -> None:
    assert extract_json('prose\n```json\n{"value": None,}\n```') == {"value": None}
    assert extract_json('{"None": "None", "items": [1, 2,]}') == {
        "None": "None",
        "items": [1, 2],
    }
    with pytest.raises(InvalidPageIndexJSON, match="malformed"):
        extract_json("not json")


def test_sync_valid_json_and_length_continuation() -> None:
    calls: list[dict[str, object]] = []

    def valid_completion(**kwargs: object) -> object:
        calls.append(kwargs)
        return _response('{"ok": true}')

    def length_completion(**kwargs: object) -> object:
        del kwargs
        return _response("partial", "length")

    valid = _runtime(valid_completion)
    assert valid.complete("provider/model", "prompt", expect_json=True) == '{"ok": true}'
    assert calls[0]["extra_body"] == {"thinking": {"type": "disabled"}}

    length = _runtime(length_completion)
    assert length.complete(
        "provider/model", "prompt", return_finish_reason=True, expect_json=True
    ) == ("partial", "max_output_reached")


def test_empty_and_invalid_json_append_correction_then_recover() -> None:
    calls: list[list[dict[str, str]]] = []
    responses = iter([_response(""), _response("invalid"), _response('{"ok": true}')])

    def completion(**kwargs: object) -> object:
        calls.append(cast(list[dict[str, str]], kwargs["messages"]))
        return next(responses)

    result = _runtime(completion).complete("provider/model", "private prompt", expect_json=True)

    assert result == '{"ok": true}'
    assert "prior response was empty or invalid" not in calls[0][-1]["content"]
    assert "prior response was empty or invalid" in calls[1][-1]["content"]
    assert "prior response was empty or invalid" in calls[2][-1]["content"]


def test_transient_exception_and_insufficient_resource_retry() -> None:
    responses: list[object] = [
        ConnectionError("private transport detail"),
        _response("", "insufficient_system_resource"),
        _response("ready"),
    ]

    def completion(**kwargs: object) -> object:
        del kwargs
        response = responses.pop(0)
        if isinstance(response, Exception):
            raise response
        return response

    assert _runtime(completion).complete("provider/model", "prompt") == "ready"


@pytest.mark.parametrize(
    ("response", "code"),
    [
        (_response("blocked", "content_filter"), "pageindex_content_filter"),
        (
            _response("tool", "tool_calls", tool_calls=[{"name": "unsafe"}]),
            "pageindex_unexpected_tool_call",
        ),
    ],
)
def test_deterministic_provider_rejections_are_not_retried(response: object, code: str) -> None:
    calls = 0

    def completion(**kwargs: object) -> object:
        nonlocal calls
        del kwargs
        calls += 1
        return response

    with pytest.raises(PageIndexLLMError) as error:
        _runtime(completion).complete("provider/model", "secret document prompt")
    assert error.value.failure_code == code
    assert "secret document prompt" not in str(error.value)
    assert calls == 1


def test_retry_exhaustion_is_typed_and_prompt_free() -> None:
    def empty_completion(**kwargs: object) -> object:
        del kwargs
        return _response("")

    with pytest.raises(PageIndexLLMError) as error:
        _runtime(empty_completion, attempts=3).complete("provider/model", "secret document prompt")
    assert error.value.failure_code == "pageindex_empty_response"
    assert error.value.attempts == 3
    assert "secret document prompt" not in error.value.diagnostic


def test_reasoning_content_is_never_used_as_the_final_answer() -> None:
    def reasoning_only(**kwargs: object) -> object:
        del kwargs
        return _response("", reasoning_content='{"private": "reasoning"}')

    with pytest.raises(PageIndexLLMError) as error:
        _runtime(reasoning_only, attempts=1).complete("provider/model", "prompt")

    assert error.value.failure_code == "pageindex_empty_response"


@pytest.mark.parametrize(
    ("provider_error", "expected_code"),
    [
        (
            AuthenticationError("unauthorized", "provider", "model"),
            "pageindex_provider_unavailable",
        ),
        (
            BadRequestError("invalid request", "model", "provider"),
            "pageindex_provider_rejected",
        ),
    ],
)
def test_authentication_and_bad_request_errors_are_not_retried(
    provider_error: Exception,
    expected_code: str,
) -> None:
    calls = 0

    def rejected(**kwargs: object) -> object:
        nonlocal calls
        del kwargs
        calls += 1
        raise provider_error

    with pytest.raises(PageIndexLLMError) as error:
        _runtime(rejected).complete("provider/model", "private prompt")

    assert error.value.failure_code == expected_code
    assert "private prompt" not in error.value.diagnostic
    assert calls == 1


async def test_async_invalid_then_valid() -> None:
    responses = iter([_response("bad"), _response('{"answer": "yes"}')])
    calls: list[dict[str, object]] = []

    async def completion(**kwargs: object) -> object:
        calls.append(kwargs)
        return next(responses)

    def unused_sync(**kwargs: object) -> object:
        del kwargs
        return _response("unused")

    runtime = _runtime(unused_sync, acompletion=completion)
    assert await runtime.acomplete("provider/model", "prompt", expect_json=True) == (
        '{"answer": "yes"}'
    )
    assert all(call["extra_body"] == {"thinking": {"type": "disabled"}} for call in calls)


async def test_async_invalid_json_fallback_cannot_convert_failure_to_a_fact() -> None:
    calls = 0

    async def invalid(**kwargs: object) -> object:
        nonlocal calls
        del kwargs
        calls += 1
        return _response("not json")

    def unused_sync(**kwargs: object) -> object:
        del kwargs
        return _response("unused")

    runtime = _runtime(unused_sync, acompletion=invalid)
    with pytest.raises(PageIndexLLMError) as error:
        await runtime.acomplete(
            "provider/model",
            "prompt",
            expect_json=True,
            invalid_json_fallback={"answer": "no"},
        )
    assert error.value.failure_code == "pageindex_invalid_json"
    assert calls == 3


async def test_async_json_fallback_does_not_mask_provider_rejection() -> None:
    async def rejected(**kwargs: object) -> object:
        del kwargs
        raise BadRequestError("invalid request", "model", "provider")

    def unused_sync(**kwargs: object) -> object:
        del kwargs
        return _response("unused")

    runtime = _runtime(unused_sync, acompletion=rejected)
    with pytest.raises(PageIndexLLMError) as error:
        await runtime.acomplete(
            "provider/model",
            "prompt",
            expect_json=True,
            invalid_json_fallback={"answer": "no"},
        )
    assert error.value.failure_code == "pageindex_provider_rejected"


def test_control_characters_from_pdf_text_do_not_discard_the_response() -> None:
    """PDF text layers carry raw control bytes that the model quotes back inside JSON.

    A lambda extracted from one corpus paper arrives as 0x15, so strict parsing rejected
    an otherwise valid response and burned every retry on a deterministic failure.
    """
    payload = '{"thinking": "title \x15 in FastEmit", "answer": "yes"}'
    assert extract_json(payload) == {"thinking": "title \x15 in FastEmit", "answer": "yes"}


def test_malformed_json_is_still_rejected() -> None:
    with pytest.raises(InvalidPageIndexJSON):
        extract_json('{"unterminated": ')
