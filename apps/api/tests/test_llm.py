from __future__ import annotations

from typing import Any

from pydantic import SecretStr
from pytest import MonkeyPatch

from vectorless_rag.config import Settings
from vectorless_rag.llm import DeepSeekLLM
from vectorless_rag.models import RunRoute
from vectorless_rag.schemas import RouteDecision


class _Runnable:
    def __init__(self, calls: dict[str, Any]) -> None:
        self.calls = calls

    async def ainvoke(self, messages: list[tuple[str, str]], *, config: object) -> RouteDecision:
        self.calls["messages"] = messages
        self.calls["config"] = config
        return RouteDecision(
            route=RunRoute.sql,
            reason="catalog inventory",
            standalone_question="List every paper title",
        )


class _Client:
    def __init__(self, calls: dict[str, Any]) -> None:
        self.calls = calls

    def with_structured_output(self, schema: object, *, method: str) -> _Runnable:
        self.calls["schema"] = schema
        self.calls["method"] = method
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
