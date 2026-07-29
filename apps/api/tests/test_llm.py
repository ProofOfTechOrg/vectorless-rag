from __future__ import annotations

import uuid
from typing import Any

import httpx
import openai
from langchain_core.messages import AIMessage
from pydantic import SecretStr
from pytest import MonkeyPatch, raises

import vectorless_rag.llm as llm_module
from vectorless_rag.config import Settings
from vectorless_rag.llm import DeepSeekLLM, LLMError, StructuredOutputError
from vectorless_rag.models import ModelCallOutcome, ModelCallStage, RunRoute
from vectorless_rag.schemas import RouteDecision
from vectorless_rag.usage import (
    StartedModelCall,
    TokenUsage,
    UsageOwner,
    UsagePersistenceError,
)


class _Runnable:
    def __init__(self, calls: dict[str, Any]) -> None:
        self.calls = calls

    async def ainvoke(
        self, messages: list[tuple[str, str]], *, config: object
    ) -> dict[str, object]:
        events = self.calls.get("ledger_events")
        if isinstance(events, list):
            assert events and events[-1][0] == "started"
        self.calls["provider_invocations"] = int(self.calls.get("provider_invocations", 0)) + 1
        self.calls["messages"] = messages
        self.calls["config"] = config
        before_response = self.calls.get("before_response")
        if callable(before_response):
            before_response()
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


class _Ledger:
    def __init__(self, calls: dict[str, Any], *, fail_finalize: bool = False) -> None:
        self.calls = calls
        self.fail_finalize = fail_finalize

    async def astart(
        self,
        stage: ModelCallStage,
        model: str,
        provider_attempt: int,
        **_: object,
    ) -> StartedModelCall:
        call = StartedModelCall(
            uuid.uuid4(),
            UsageOwner(trace_id=uuid.uuid4().hex, query_run_id=uuid.uuid4()),
            stage,
            model,
            provider_attempt,
            uuid.uuid4(),
        )
        events: list[tuple[object, ...]] = self.calls.setdefault("ledger_events", [])
        events.append(("started", call))
        return call

    async def afinish(
        self,
        call: StartedModelCall | None,
        outcome: ModelCallOutcome,
        finish_reason: str | None,
        usage: TokenUsage,
        **_: object,
    ) -> None:
        if self.fail_finalize:
            raise UsagePersistenceError
        events: list[tuple[object, ...]] = self.calls.setdefault("ledger_events", [])
        events.append(("finished", call, outcome, finish_reason, usage))


class _StatusError(RuntimeError):
    def __init__(self, status_code: int) -> None:
        super().__init__("private provider body")
        self.status_code = status_code


async def test_thinking_structured_output_uses_json_mode(monkeypatch: MonkeyPatch) -> None:
    llm = DeepSeekLLM(Settings(deepseek_api_key=SecretStr("test-key")))
    calls: dict[str, Any] = {}

    def client(thinking: bool, max_output_tokens: int | None = None) -> _Client:
        del thinking, max_output_tokens
        return _Client(calls)

    monkeypatch.setattr(llm, "_client", client)

    result = await llm._invoke(  # pyright: ignore[reportPrivateUsage]
        RouteDecision,
        "Route this request.",
        "paper titles",
        thinking=True,
        stage=ModelCallStage.query_route,
    )

    assert result.route == RunRoute.sql
    assert calls["method"] == "json_mode"
    assert calls["include_raw"] is True
    system_message = calls["messages"][0][1]
    assert "JSON Schema" in system_message
    assert "standalone_question" in system_message


async def test_success_does_not_clear_hard_failure_observed_inflight(
    monkeypatch: MonkeyPatch,
) -> None:
    llm = DeepSeekLLM(Settings(deepseek_api_key=SecretStr("test-key")))
    calls: dict[str, Any] = {
        "before_response": lambda: llm.provider_gate.observe_completion_failure(_StatusError(402))
    }

    def client(thinking: bool, max_output_tokens: int | None = None) -> _Client:
        del thinking, max_output_tokens
        return _Client(calls)

    monkeypatch.setattr(llm, "_client", client)

    await llm._invoke(  # pyright: ignore[reportPrivateUsage]
        RouteDecision,
        "Route this request.",
        "paper titles",
        thinking=False,
        stage=ModelCallStage.query_route,
    )

    assert llm.provider_gate.hard_failure is not None
    assert llm.provider_gate.hard_failure.code == "insufficient_credit"


async def test_non_thinking_structured_output_keeps_function_calling(
    monkeypatch: MonkeyPatch,
) -> None:
    llm = DeepSeekLLM(Settings(deepseek_api_key=SecretStr("test-key")))
    calls: dict[str, Any] = {}

    def client(thinking: bool, max_output_tokens: int | None = None) -> _Client:
        del thinking, max_output_tokens
        return _Client(calls)

    monkeypatch.setattr(llm, "_client", client)

    await llm._invoke(  # pyright: ignore[reportPrivateUsage]
        RouteDecision,
        "Route this request.",
        "paper titles",
        thinking=False,
        stage=ModelCallStage.query_route,
    )

    assert calls["method"] == "function_calling"
    assert calls["messages"][0][1] == "Route this request."


def test_client_uses_per_call_output_cap(monkeypatch: MonkeyPatch) -> None:
    llm = DeepSeekLLM(
        Settings(
            deepseek_api_key=SecretStr("test-key"),
            pageindex_max_output_tokens=8_192,
        )
    )
    calls: list[dict[str, object]] = []

    def client(**kwargs: object) -> object:
        calls.append(kwargs)
        return object()

    monkeypatch.setattr(llm_module, "ChatDeepSeek", client)

    llm._client(False, 4_096)  # pyright: ignore[reportPrivateUsage]
    llm._client(False)  # pyright: ignore[reportPrivateUsage]

    assert calls[0]["max_tokens"] == 4_096
    assert calls[1]["max_tokens"] == 8_192


async def test_per_call_output_cap_is_preserved_across_retries(
    monkeypatch: MonkeyPatch,
) -> None:
    llm = DeepSeekLLM(Settings(deepseek_api_key=SecretStr("test-key")))
    calls: list[tuple[int, int | None]] = []

    async def invoke(
        schema: type[RouteDecision],
        system: str,
        user: str,
        thinking: bool,
        *,
        stage: ModelCallStage,
        max_output_tokens: int | None = None,
        attempt: int = 1,
        **kwargs: object,
    ) -> RouteDecision:
        del schema, system, user, thinking, stage, kwargs
        calls.append((attempt, max_output_tokens))
        if attempt == 1:
            raise TimeoutError
        return RouteDecision(
            route=RunRoute.sql,
            reason="catalog inventory",
            standalone_question="List every paper title",
        )

    async def no_sleep(delay: float) -> None:
        del delay

    monkeypatch.setattr(llm, "_invoke", invoke)
    monkeypatch.setattr(llm_module.asyncio, "sleep", no_sleep)

    await llm.structured(
        RouteDecision,
        "system",
        "user",
        thinking=False,
        stage=ModelCallStage.query_route,
        max_output_tokens=4_096,
    )

    assert calls == [(1, 4_096), (2, 4_096)]


async def test_http_200_response_validation_failure_is_retried(
    monkeypatch: MonkeyPatch,
) -> None:
    llm = DeepSeekLLM(Settings(deepseek_api_key=SecretStr("test-key")))
    attempts: list[int] = []
    request = httpx.Request("POST", "https://api.deepseek.com/chat/completions")
    response = httpx.Response(200, request=request)

    async def invoke(*args: object, attempt: int = 1, **kwargs: object) -> RouteDecision:
        del args, kwargs
        attempts.append(attempt)
        if attempt == 1:
            raise openai.APIResponseValidationError(
                response=response,
                body={"invalid": True},
            )
        return RouteDecision(
            route=RunRoute.sql,
            reason="catalog inventory",
            standalone_question="List every paper title",
        )

    async def no_sleep(delay: float) -> None:
        del delay

    monkeypatch.setattr(llm, "_invoke", invoke)
    monkeypatch.setattr(llm_module.asyncio, "sleep", no_sleep)

    result = await llm.structured(
        RouteDecision,
        "system",
        "user",
        thinking=False,
        stage=ModelCallStage.query_route,
    )

    assert result.route == RunRoute.sql
    assert attempts == [1, 2]


async def test_usage_row_starts_before_provider_and_same_row_is_finalized(
    monkeypatch: MonkeyPatch,
) -> None:
    calls: dict[str, Any] = {}
    llm = DeepSeekLLM(
        Settings(deepseek_api_key=SecretStr("test-key")),
        ledger=_Ledger(calls),  # pyright: ignore[reportArgumentType]
    )

    def client(thinking: bool, max_output_tokens: int | None = None) -> _Client:
        del thinking, max_output_tokens
        return _Client(calls)

    monkeypatch.setattr(llm, "_client", client)

    await llm.structured(
        RouteDecision,
        "system",
        "private prompt",
        thinking=False,
        stage=ModelCallStage.query_route,
    )

    events = calls["ledger_events"]
    assert events[0][0] == "started"
    assert events[1][0] == "finished"
    assert events[1][1] is events[0][1]
    assert events[1][2] == ModelCallOutcome.completed
    assert calls["provider_invocations"] == 1


async def test_usage_finalize_failure_never_retries_provider(
    monkeypatch: MonkeyPatch,
) -> None:
    calls: dict[str, Any] = {}
    llm = DeepSeekLLM(
        Settings(deepseek_api_key=SecretStr("test-key")),
        ledger=_Ledger(calls, fail_finalize=True),  # pyright: ignore[reportArgumentType]
    )

    def client(thinking: bool, max_output_tokens: int | None = None) -> _Client:
        del thinking, max_output_tokens
        return _Client(calls)

    monkeypatch.setattr(llm, "_client", client)

    with raises(LLMError) as error:
        await llm.structured(
            RouteDecision,
            "system",
            "private prompt",
            thinking=False,
            stage=ModelCallStage.query_route,
        )

    assert error.value.failure_code == "usage_persistence_failed"
    assert calls["provider_invocations"] == 1


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
        await llm.structured(
            RouteDecision,
            "system",
            "private prompt",
            thinking=False,
            stage=ModelCallStage.query_route,
        )

    assert error.value.failure_code == "provider_rejected"
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
        await llm.structured(
            RouteDecision,
            "system",
            "private prompt",
            thinking=False,
            stage=ModelCallStage.query_route,
        )

    assert error.value.failure_code == "llm_content_filter"
    assert error.value.finish_reason == "content_filter"
    assert error.value.attempts == 1
    assert calls == 1
