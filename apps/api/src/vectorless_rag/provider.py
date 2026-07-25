from __future__ import annotations

import hashlib
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from typing import Any, cast

import httpx
from sqlalchemy.ext.asyncio import AsyncSession

from vectorless_rag.config import Settings
from vectorless_rag.models import ProviderAvailability, ProviderState

DEEPSEEK_BALANCE_URL = "https://api.deepseek.com/user/balance"
STALE_POSITIVE_WINDOW = timedelta(minutes=5)
STATUS_CACHE_WINDOW = timedelta(seconds=30)


@dataclass(frozen=True)
class ProviderObservation:
    availability: ProviderAvailability
    reason: str | None
    checked_at: datetime
    stale: bool

    @property
    def admits_model_work(self) -> bool:
        return self.availability == ProviderAvailability.available


@dataclass(frozen=True)
class ProviderFailure:
    code: str
    retryable: bool
    http_status: int | None
    blocks_provider: bool


class ProviderUnavailableError(RuntimeError):
    failure_code = "provider_unavailable"
    diagnostic = "The model provider is unavailable."

    def __init__(self, reason: str, *, retryable: bool = False) -> None:
        super().__init__(self.diagnostic)
        self.reason = reason
        self.retryable = retryable
        self.attempts = 0
        self.finish_reason: str | None = None


def provider_configuration_hash(settings: Settings) -> str:
    value = f"deepseek\0{settings.deepseek_model}\0{settings.pageindex_model}"
    return hashlib.sha256(value.encode()).hexdigest()


def _status_code(error: BaseException) -> int | None:
    for name in ("status_code", "http_status"):
        value = getattr(error, name, None)
        if isinstance(value, int) and not isinstance(value, bool):
            return value
    response = getattr(error, "response", None)
    value = getattr(response, "status_code", None)
    return value if isinstance(value, int) and not isinstance(value, bool) else None


def classify_provider_failure(error: BaseException) -> ProviderFailure:
    status = _status_code(error)
    if status == 402:
        return ProviderFailure("insufficient_credit", False, status, True)
    if status == 401:
        return ProviderFailure("authentication_failed", False, status, True)
    if status == 403:
        return ProviderFailure("permission_denied", False, status, True)
    if status == 429:
        return ProviderFailure("rate_limited", True, status, False)
    if status is not None and 500 <= status <= 599:
        return ProviderFailure("provider_server_error", True, status, False)
    if isinstance(
        error,
        (
            TimeoutError,
            ConnectionError,
            httpx.TimeoutException,
            httpx.NetworkError,
        ),
    ):
        return ProviderFailure("network_error", True, status, False)
    return ProviderFailure("provider_rejected", False, status, False)


class ProviderGate:
    def __init__(
        self,
        settings: Settings,
        *,
        client: httpx.AsyncClient | None = None,
    ) -> None:
        self.settings = settings
        self.client = client
        self._hard_failure: ProviderFailure | None = None

    @property
    def hard_failure(self) -> ProviderFailure | None:
        return self._hard_failure

    def observe_completion_failure(self, error: BaseException) -> ProviderFailure:
        failure = classify_provider_failure(error)
        if failure.blocks_provider:
            self._hard_failure = failure
        return failure

    def observe_completion_success(self) -> None:
        self._hard_failure = None

    async def status(
        self,
        session: AsyncSession,
        *,
        refresh: bool = True,
    ) -> ProviderObservation:
        now = datetime.now(UTC)
        configuration_hash = provider_configuration_hash(self.settings)
        state = await session.get(ProviderState, "deepseek")
        if self.settings.deepseek_api_key is None:
            return await self._persist(
                session,
                state,
                ProviderAvailability.unconfigured,
                "not_configured",
                now,
                configuration_hash,
            )
        if self._hard_failure is not None:
            return await self._persist(
                session,
                state,
                ProviderAvailability.unavailable,
                self._hard_failure.code,
                now,
                configuration_hash,
            )
        if (
            not refresh
            and state is not None
            and state.configuration_hash == configuration_hash
            and now - state.checked_at <= STATUS_CACHE_WINDOW
        ):
            return ProviderObservation(
                availability=state.availability,
                reason=state.reason,
                checked_at=state.checked_at,
                stale=False,
            )
        try:
            available = await self._balance_available()
        except Exception:
            if (
                state is not None
                and state.configuration_hash == configuration_hash
                and state.last_available_at is not None
                and now - state.last_available_at <= STALE_POSITIVE_WINDOW
            ):
                return ProviderObservation(
                    availability=ProviderAvailability.available,
                    reason="stale_positive",
                    checked_at=state.checked_at,
                    stale=True,
                )
            return await self._persist(
                session,
                state,
                ProviderAvailability.unknown,
                "observation_failed",
                now,
                configuration_hash,
            )
        if available:
            return await self._persist(
                session,
                state,
                ProviderAvailability.available,
                None,
                now,
                configuration_hash,
            )
        self._hard_failure = ProviderFailure("insufficient_credit", False, 402, True)
        return await self._persist(
            session,
            state,
            ProviderAvailability.unavailable,
            "insufficient_credit",
            now,
            configuration_hash,
        )

    async def require_available(self, session: AsyncSession) -> ProviderObservation:
        observation = await self.status(session)
        if not observation.admits_model_work:
            raise ProviderUnavailableError(
                observation.reason or observation.availability.value,
                retryable=observation.availability == ProviderAvailability.unknown,
            )
        return observation

    async def record_completion_result(
        self,
        session: AsyncSession,
        error: BaseException | None,
    ) -> None:
        now = datetime.now(UTC)
        configuration_hash = provider_configuration_hash(self.settings)
        state = await session.get(ProviderState, "deepseek")
        if error is None:
            self.observe_completion_success()
            await self._persist(
                session,
                state,
                ProviderAvailability.available,
                None,
                now,
                configuration_hash,
            )
            return
        failure = self.observe_completion_failure(error)
        if failure.blocks_provider:
            await self._persist(
                session,
                state,
                ProviderAvailability.unavailable,
                failure.code,
                now,
                configuration_hash,
            )

    async def _balance_available(self) -> bool:
        assert self.settings.deepseek_api_key is not None
        owns_client = self.client is None
        client = self.client or httpx.AsyncClient(
            timeout=httpx.Timeout(10.0),
            follow_redirects=False,
        )
        try:
            response = await client.get(
                DEEPSEEK_BALANCE_URL,
                headers={
                    "Authorization": (
                        "Bearer " + self.settings.deepseek_api_key.get_secret_value()
                    ),
                    "Accept": "application/json",
                },
            )
            response.raise_for_status()
            payload = response.json()
            if not isinstance(payload, dict):
                raise ValueError("balance response is not an object")
            available = cast(dict[str, Any], payload).get("is_available")
            if not isinstance(available, bool):
                raise ValueError("balance response has no availability")
            return available
        finally:
            if owns_client:
                await client.aclose()

    @staticmethod
    async def _persist(
        session: AsyncSession,
        state: ProviderState | None,
        availability: ProviderAvailability,
        reason: str | None,
        checked_at: datetime,
        configuration_hash: str,
    ) -> ProviderObservation:
        if state is None:
            state = ProviderState(
                provider="deepseek",
                availability=availability,
                reason=reason,
                checked_at=checked_at,
                configuration_hash=configuration_hash,
                last_available_at=(
                    checked_at if availability == ProviderAvailability.available else None
                ),
            )
            session.add(state)
        else:
            state.availability = availability
            state.reason = reason
            state.checked_at = checked_at
            state.configuration_hash = configuration_hash
            if availability == ProviderAvailability.available:
                state.last_available_at = checked_at
        await session.flush()
        return ProviderObservation(
            availability=availability,
            reason=reason,
            checked_at=checked_at,
            stale=False,
        )
