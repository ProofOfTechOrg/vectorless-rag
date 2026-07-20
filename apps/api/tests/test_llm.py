from __future__ import annotations

from typing import Any

from langchain_core.messages import AIMessage
from pydantic import SecretStr
from pytest import MonkeyPatch, raises

from vectorless_rag.config import Settings
from vectorless_rag.llm import DeepSeekLLM, LLMError, StructuredOutputError
from vectorless_rag.models import RunRoute
from vectorless_rag.schemas import RouteDecision


class _Runnable:
    def __init__(self, calls: dict[str, Any]) -> None:
        self.calls = calls

    async def ainvoke(
        self, messages: list[tuple[str, str]], *, config: object
    ) -> dict[str, object]:
        self.calls["messages"] = messages
        self.calls["config"] = config
        parsed = RouteDecision(
            route=RunRoute.sql,
            reason="catalog inventory",
            standalone_question="List every paper title",
        )
        return {
            "raw": AIMessage(
                content="",
                response_metadata={"finish_reason": "stop"},
                usage_metadata={"input_tokens": 5, "output_tokens": 3, "total_tokens": 8},
            ),
            "parsed": parsed,
            "parsing_error": None,
        }


class _Client:
    def __init__(self, calls: dict[str, Any]) -> None:
        self.calls = calls

    def with_structured_output(
        self, schema: object, *, method: str, include_raw: bool
    ) -> _Runnable:
        self.calls["schema"] = schema
        self.calls["method"] = method
        self.calls["include_raw"] = include_raw
        return _Runnable(self.calls)


async def test_thinking_structured_output_uses_json_mode(monkeypatch: MonkeyPatch) -> None:
    llm = DeepSeekLLM(Settings(deepseek_api_key=SecretStr("test-key")))
    calls: dict[str, Any] = {}

    def client(thinking: bool) -> _Client:
        del thinking
        return _Client(calls)

    monkeypatch.setattr(llm, "_client", client)

    result = await llm._invoke(  # pyright: ignore[reportPrivateUsage]
        RouteDecision, "Route this request.", "paper titles", thinking=True
    )

    assert result.route == RunRoute.sql
    assert calls["method"] == "json_mode"
    assert calls["include_raw"] is True
    system_message = calls["messages"][0][1]
    assert "JSON Schema" in system_message
    assert "standalone_question" in system_message


async def test_non_thinking_structured_output_keeps_function_calling(
    monkeypatch: MonkeyPatch,
) -> None:
    llm = DeepSeekLLM(Settings(deepseek_api_key=SecretStr("test-key")))
    calls: dict[str, Any] = {}

    def client(thinking: bool) -> _Client:
        del thinking
        return _Client(calls)

    monkeypatch.setattr(llm, "_client", client)

    await llm._invoke(  # pyright: ignore[reportPrivateUsage]
        RouteDecision, "Route this request.", "paper titles", thinking=False
    )

    assert calls["method"] == "function_calling"
    assert calls["messages"][0][1] == "Route this request."


async def test_deterministic_provider_rejection_is_sanitized_without_retry(
    monkeypatch: MonkeyPatch,
) -> None:
    llm = DeepSeekLLM(Settings(deepseek_api_key=SecretStr("test-key")))
    calls = 0

    async def reject(*args: object, **kwargs: object) -> RouteDecision:
        nonlocal calls
        del args, kwargs
        calls += 1
        raise ValueError("private provider body")

    monkeypatch.setattr(llm, "_invoke", reject)

    with raises(LLMError) as error:
        await llm.structured(RouteDecision, "system", "private prompt", thinking=False)

    assert error.value.failure_code == "llm_provider_rejected"
    assert error.value.attempts == 1
    assert "private" not in error.value.diagnostic
    assert calls == 1


async def test_content_filter_is_not_retried(monkeypatch: MonkeyPatch) -> None:
    llm = DeepSeekLLM(Settings(deepseek_api_key=SecretStr("test-key")))
    calls = 0

    async def filtered(*args: object, **kwargs: object) -> RouteDecision:
        nonlocal calls
        del args, kwargs
        calls += 1
        raise StructuredOutputError(
            "structured_output_invalid",
            "The model returned an invalid structured response.",
            attempts=1,
            finish_reason="content_filter",
        )

    monkeypatch.setattr(llm, "_invoke", filtered)

    with raises(LLMError) as error:
        await llm.structured(RouteDecision, "system", "private prompt", thinking=False)

    assert error.value.failure_code == "llm_content_filter"
    assert error.value.finish_reason == "content_filter"
    assert error.value.attempts == 1
    assert calls == 1
