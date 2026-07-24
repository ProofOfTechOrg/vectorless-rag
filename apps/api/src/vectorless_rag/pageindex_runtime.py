from __future__ import annotations

import os

os.environ.setdefault("LITELLM_LOCAL_MODEL_COST_MAP", "True")

import asyncio
import json
import random
import re
import time
from collections.abc import Awaitable, Callable
from typing import Any, cast

import litellm
from litellm.exceptions import (
    APIConnectionError,
    BadGatewayError,
    InternalServerError,
    RateLimitError,
    ServiceUnavailableError,
)

from vectorless_rag.config import Settings
from vectorless_rag.models import ModelCallOutcome, ModelCallStage
from vectorless_rag.observability import Observability
from vectorless_rag.usage import ModelCallLedger, UsagePersistenceError, normalize_token_usage

FORMAT_CORRECTION = (
    "\nYour prior response was empty or invalid. Return only the requested format without prose."
)
_FENCED_CONTENT = re.compile(r"```(?:json)?\s*(.*?)\s*```", re.IGNORECASE | re.DOTALL)
_TRANSIENT_ERRORS = (
    TimeoutError,
    ConnectionError,
    APIConnectionError,
    RateLimitError,
    InternalServerError,
    ServiceUnavailableError,
    BadGatewayError,
)
_SYNC_COMPLETION = cast(Callable[..., object], litellm.completion)
_ASYNC_COMPLETION = cast(Callable[..., Awaitable[object]], litellm.acompletion)


class PageIndexLLMError(RuntimeError):
    def __init__(
        self,
        failure_code: str,
        diagnostic: str,
        *,
        attempts: int,
        finish_reason: str | None = None,
        telemetry_output: str | None = None,
        telemetry_usage: object = None,
    ) -> None:
        super().__init__(diagnostic)
        self.failure_code = failure_code
        self.diagnostic = diagnostic
        self.attempts = attempts
        self.finish_reason = finish_reason
        self.telemetry_output = telemetry_output
        self.telemetry_usage = telemetry_usage


class InvalidPageIndexJSON(PageIndexLLMError):
    pass


def _repair_json(text: str) -> str:
    output: list[str] = []
    index = 0
    in_string = False
    escaped = False
    while index < len(text):
        character = text[index]
        if in_string:
            output.append(character)
            if escaped:
                escaped = False
            elif character == "\\":
                escaped = True
            elif character == '"':
                in_string = False
            index += 1
            continue
        if character == '"':
            in_string = True
            output.append(character)
            index += 1
            continue
        if text.startswith("None", index):
            previous = text[index - 1] if index else ""
            following = text[index + 4] if index + 4 < len(text) else ""
            if not (previous.isalnum() or previous == "_") and not (
                following.isalnum() or following == "_"
            ):
                output.append("null")
                index += 4
                continue
        if character == ",":
            lookahead = index + 1
            while lookahead < len(text) and text[lookahead].isspace():
                lookahead += 1
            if lookahead < len(text) and text[lookahead] in "]}":
                index += 1
                continue
        output.append(character)
        index += 1
    return "".join(output)


def extract_json(content: str) -> dict[str, Any] | list[Any]:
    if not content.strip():
        raise InvalidPageIndexJSON(
            "pageindex_invalid_json",
            "PageIndex received an empty JSON response.",
            attempts=0,
        )
    fenced = _FENCED_CONTENT.search(content)
    candidate = fenced.group(1).strip() if fenced else content.strip()
    try:
        parsed = json.loads(candidate)
    except json.JSONDecodeError:
        try:
            parsed = json.loads(_repair_json(candidate))
        except json.JSONDecodeError as error:
            raise InvalidPageIndexJSON(
                "pageindex_invalid_json",
                "PageIndex received malformed JSON.",
                attempts=0,
            ) from error
    if not isinstance(parsed, (dict, list)):
        raise InvalidPageIndexJSON(
            "pageindex_invalid_json",
            "PageIndex received JSON with an unexpected top-level type.",
            attempts=0,
        )
    return cast(dict[str, Any] | list[Any], parsed)


def _provider_error_code(error: Exception) -> str | None:
    code = getattr(error, "code", None)
    if isinstance(code, str):
        return code
    body = getattr(error, "body", None)
    if isinstance(body, dict):
        values = cast(dict[str, object], body)
        nested = values.get("error")
        if isinstance(nested, dict):
            nested_code = cast(dict[str, object], nested).get("code")
            if isinstance(nested_code, str):
                return nested_code
        body_code = values.get("code")
        if isinstance(body_code, str):
            return body_code
    return None


def _message_content(message: object) -> str:
    content = getattr(message, "content", None)
    return content if isinstance(content, str) else ""


def _has_tool_calls(message: object) -> bool:
    tool_calls = getattr(message, "tool_calls", None)
    return isinstance(tool_calls, list) and len(cast(list[object], tool_calls)) > 0


class PageIndexRuntime:
    def __init__(
        self,
        settings: Settings,
        *,
        completion: Callable[..., object] = _SYNC_COMPLETION,
        acompletion: Callable[..., Awaitable[object]] = _ASYNC_COMPLETION,
        sleep: Callable[[float], None] = time.sleep,
        async_sleep: Callable[[float], Awaitable[None]] = asyncio.sleep,
        random_value: Callable[[], float] = random.random,
        observability: Observability | None = None,
        ledger: ModelCallLedger | None = None,
    ) -> None:
        self.settings = settings
        self.completion = completion
        self.acompletion = acompletion
        self.sleep = sleep
        self.async_sleep = async_sleep
        self.random_value = random_value
        self.observability = observability or Observability(settings)
        self.ledger = ledger or ModelCallLedger(settings.database_url)
        litellm.drop_params = True

    def _messages(
        self, prompt: str, chat_history: list[dict[str, Any]] | None
    ) -> list[dict[str, Any]]:
        messages = list(chat_history or [])
        messages.append({"role": "user", "content": prompt})
        return messages

    def _delay(self, attempt: int) -> float:
        return min(8.0, 0.5 * (2 ** (attempt - 1))) * (0.5 + self.random_value())

    def _read_response(
        self, response: object, *, attempt: int, expect_json: bool
    ) -> tuple[str, str, object]:
        choices = getattr(response, "choices", None)
        if not isinstance(choices, list) or not choices:
            raise PageIndexLLMError(
                "pageindex_invalid_response",
                "PageIndex received a provider response without a completion choice.",
                attempts=attempt,
            )
        choice = cast(list[object], choices)[0]
        message = getattr(choice, "message", None)
        content = _message_content(message)
        usage = getattr(response, "usage", None)
        finish_reason = getattr(choice, "finish_reason", None)
        if not isinstance(finish_reason, str):
            raise PageIndexLLMError(
                "pageindex_invalid_response",
                "PageIndex received a provider response without a finish reason.",
                attempts=attempt,
                telemetry_output=content,
                telemetry_usage=usage,
            )
        if _has_tool_calls(message):
            raise PageIndexLLMError(
                "pageindex_unexpected_tool_call",
                "PageIndex received an unexpected tool call.",
                attempts=attempt,
                finish_reason=finish_reason,
                telemetry_output=content,
                telemetry_usage=usage,
            )
        if finish_reason == "content_filter":
            raise PageIndexLLMError(
                "pageindex_content_filter",
                "The provider rejected PageIndex content under its content policy.",
                attempts=attempt,
                finish_reason=finish_reason,
                telemetry_output=content,
                telemetry_usage=usage,
            )
        if finish_reason == "insufficient_system_resource":
            raise PageIndexLLMError(
                "pageindex_insufficient_system_resource",
                "The provider reported insufficient system resources.",
                attempts=attempt,
                finish_reason=finish_reason,
                telemetry_output=content,
                telemetry_usage=usage,
            )
        if finish_reason not in {"stop", "length"}:
            raise PageIndexLLMError(
                "pageindex_provider_rejected",
                "The provider rejected the PageIndex completion.",
                attempts=attempt,
                finish_reason=finish_reason,
                telemetry_output=content,
                telemetry_usage=usage,
            )
        if finish_reason == "stop" and not content.strip():
            raise PageIndexLLMError(
                "pageindex_empty_response",
                "The provider returned an empty PageIndex completion.",
                attempts=attempt,
                finish_reason=finish_reason,
                telemetry_output=content,
                telemetry_usage=usage,
            )
        if expect_json and finish_reason == "stop":
            try:
                extract_json(content)
            except InvalidPageIndexJSON as error:
                raise PageIndexLLMError(
                    error.failure_code,
                    error.diagnostic,
                    attempts=attempt,
                    finish_reason=finish_reason,
                    telemetry_output=content,
                    telemetry_usage=usage,
                ) from error
        return content, finish_reason, usage

    @staticmethod
    def _retryable(error: Exception) -> bool:
        if isinstance(error, _TRANSIENT_ERRORS):
            return True
        if isinstance(error, PageIndexLLMError):
            return error.failure_code in {
                "pageindex_empty_response",
                "pageindex_invalid_json",
                "pageindex_insufficient_system_resource",
            }
        return _provider_error_code(error) == "insufficient_system_resource"

    @staticmethod
    def _safe_error(error: Exception, attempt: int) -> PageIndexLLMError:
        if isinstance(error, PageIndexLLMError):
            return error
        if isinstance(error, _TRANSIENT_ERRORS) or (
            _provider_error_code(error) == "insufficient_system_resource"
        ):
            return PageIndexLLMError(
                "pageindex_transient_provider_error",
                "The PageIndex provider request failed transiently.",
                attempts=attempt,
            )
        return PageIndexLLMError(
            "pageindex_provider_rejected",
            "The PageIndex provider rejected the request.",
            attempts=attempt,
            finish_reason=_provider_error_code(error),
        )

    def complete(
        self,
        model: str | None,
        prompt: str,
        chat_history: list[dict[str, Any]] | None = None,
        *,
        return_finish_reason: bool = False,
        expect_json: bool = False,
    ) -> str | tuple[str, str]:
        provider_model = (model or self.settings.pageindex_model).removeprefix("litellm/")
        corrected = False
        last_error: PageIndexLLMError | None = None
        for attempt in range(1, self.settings.pageindex_llm_max_attempts + 1):
            current_prompt = prompt + (FORMAT_CORRECTION if corrected else "")
            messages = self._messages(current_prompt, chat_history)
            serialized_prompt = json.dumps(messages, sort_keys=True, separators=(",", ":"))
            call = self.ledger.start(ModelCallStage.pageindex_generation, provider_model, attempt)
            observation = self.observability.generation(
                "pageindex-completion", provider_model, serialized_prompt, attempt=attempt
            )
            try:
                response = self.completion(
                    model=provider_model,
                    messages=messages,
                    temperature=0,
                    num_retries=0,
                    extra_body={"thinking": {"type": "disabled"}},
                )
                content, finish_reason, usage = self._read_response(
                    response, attempt=attempt, expect_json=expect_json
                )
                normalized_usage = normalize_token_usage(usage)
                try:
                    self.ledger.finish(
                        call,
                        ModelCallOutcome.completed,
                        finish_reason,
                        normalized_usage,
                    )
                finally:
                    observation.finish(content, finish_reason=finish_reason, usage=normalized_usage)
                pageindex_reason = "max_output_reached" if finish_reason == "length" else "finished"
                return (content, pageindex_reason) if return_finish_reason else content
            except UsagePersistenceError:
                raise
            except Exception as error:
                safe_error = self._safe_error(error, attempt)
                normalized_usage = normalize_token_usage(safe_error.telemetry_usage)
                try:
                    self.ledger.finish(
                        call,
                        ModelCallOutcome.failed,
                        safe_error.finish_reason,
                        normalized_usage,
                    )
                finally:
                    observation.fail(
                        finish_reason=safe_error.finish_reason,
                        output=safe_error.telemetry_output,
                        usage=normalized_usage,
                    )
                last_error = safe_error
                if not self._retryable(error):
                    raise safe_error from error
                corrected = corrected or safe_error.failure_code in {
                    "pageindex_empty_response",
                    "pageindex_invalid_json",
                }
                if attempt < self.settings.pageindex_llm_max_attempts:
                    self.sleep(self._delay(attempt))
        assert last_error is not None
        raise PageIndexLLMError(
            "pageindex_llm_retry_exhausted",
            "The PageIndex provider request failed after all retry attempts.",
            attempts=self.settings.pageindex_llm_max_attempts,
            finish_reason=last_error.finish_reason,
        ) from last_error

    async def acomplete(
        self,
        model: str | None,
        prompt: str,
        *,
        expect_json: bool = False,
        invalid_json_fallback: dict[str, Any] | list[Any] | None = None,
    ) -> str:
        provider_model = (model or self.settings.pageindex_model).removeprefix("litellm/")
        corrected = False
        last_error: PageIndexLLMError | None = None
        for attempt in range(1, self.settings.pageindex_llm_max_attempts + 1):
            current_prompt = prompt + (FORMAT_CORRECTION if corrected else "")
            messages = self._messages(current_prompt, None)
            serialized_prompt = json.dumps(messages, sort_keys=True, separators=(",", ":"))
            call = await self.ledger.astart(
                ModelCallStage.pageindex_generation, provider_model, attempt
            )
            observation = self.observability.generation(
                "pageindex-completion", provider_model, serialized_prompt, attempt=attempt
            )
            try:
                response = await self.acompletion(
                    model=provider_model,
                    messages=messages,
                    temperature=0,
                    num_retries=0,
                    extra_body={"thinking": {"type": "disabled"}},
                )
                content, finish_reason, usage = self._read_response(
                    response, attempt=attempt, expect_json=expect_json
                )
                normalized_usage = normalize_token_usage(usage)
                try:
                    await self.ledger.afinish(
                        call,
                        ModelCallOutcome.completed,
                        finish_reason,
                        normalized_usage,
                    )
                finally:
                    observation.finish(content, finish_reason=finish_reason, usage=normalized_usage)
                return content
            except UsagePersistenceError:
                raise
            except Exception as error:
                safe_error = self._safe_error(error, attempt)
                normalized_usage = normalize_token_usage(safe_error.telemetry_usage)
                try:
                    await self.ledger.afinish(
                        call,
                        ModelCallOutcome.failed,
                        safe_error.finish_reason,
                        normalized_usage,
                    )
                finally:
                    observation.fail(
                        finish_reason=safe_error.finish_reason,
                        output=safe_error.telemetry_output,
                        usage=normalized_usage,
                    )
                last_error = safe_error
                if not self._retryable(error):
                    raise safe_error from error
                corrected = corrected or safe_error.failure_code in {
                    "pageindex_empty_response",
                    "pageindex_invalid_json",
                }
                if attempt < self.settings.pageindex_llm_max_attempts:
                    await self.async_sleep(self._delay(attempt))
        assert last_error is not None
        if invalid_json_fallback is not None and last_error.failure_code in {
            "pageindex_empty_response",
            "pageindex_invalid_json",
        }:
            return json.dumps(invalid_json_fallback, separators=(",", ":"))
        raise PageIndexLLMError(
            "pageindex_llm_retry_exhausted",
            "The PageIndex provider request failed after all retry attempts.",
            attempts=self.settings.pageindex_llm_max_attempts,
            finish_reason=last_error.finish_reason,
        ) from last_error


_runtime_instance: PageIndexRuntime | None = None


def _runtime() -> PageIndexRuntime:
    global _runtime_instance
    if _runtime_instance is None:
        _runtime_instance = PageIndexRuntime(Settings())
    return _runtime_instance


def llm_completion(
    model: str | None,
    prompt: str,
    chat_history: list[dict[str, Any]] | None = None,
    return_finish_reason: bool = False,
    expect_json: bool = False,
) -> str | tuple[str, str]:
    return _runtime().complete(
        model,
        prompt,
        chat_history,
        return_finish_reason=return_finish_reason,
        expect_json=expect_json,
    )


async def llm_acompletion(
    model: str | None,
    prompt: str,
    expect_json: bool = False,
    invalid_json_fallback: dict[str, Any] | list[Any] | None = None,
) -> str:
    return await _runtime().acomplete(
        model,
        prompt,
        expect_json=expect_json,
        invalid_json_fallback=invalid_json_fallback,
    )
