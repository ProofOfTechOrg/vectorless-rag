from __future__ import annotations

import hashlib
import hmac
import secrets
from dataclasses import dataclass
from datetime import UTC, datetime
from typing import Annotated

from fastapi import Depends, HTTPException, Security, status
from fastapi.security import APIKeyHeader, SecurityScopes
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from vectorless_rag.config import Settings, get_settings
from vectorless_rag.models import ApiKey

VALID_SCOPES = frozenset({"chat", "documents:read", "admin:ingest"})
api_key_header = APIKeyHeader(name="X-API-Key", auto_error=False)


def hash_api_key(raw_key: str, pepper: str) -> str:
    return hmac.new(pepper.encode(), raw_key.encode(), hashlib.sha256).hexdigest()


def generate_api_key() -> tuple[str, str]:
    prefix = secrets.token_hex(4)
    return prefix, f"vrag_{prefix}_{secrets.token_urlsafe(32)}"


def validate_scopes(scopes: set[str]) -> None:
    invalid = scopes - VALID_SCOPES
    if invalid:
        raise ValueError(f"unknown scopes: {', '.join(sorted(invalid))}")


@dataclass(frozen=True)
class Principal:
    api_key_id: object
    name: str
    scopes: frozenset[str]


async def get_db_session() -> AsyncSession:
    raise RuntimeError("database dependency was not initialized")


async def authenticate(
    security_scopes: SecurityScopes,
    raw_key: Annotated[str | None, Depends(api_key_header)],
    session: Annotated[AsyncSession, Depends(get_db_session)],
    settings: Annotated[Settings, Depends(get_settings)],
) -> Principal:
    if not raw_key:
        raise HTTPException(status.HTTP_401_UNAUTHORIZED, "missing API key")
    key_hash = hash_api_key(raw_key, settings.api_key_pepper.get_secret_value())
    record = await session.scalar(select(ApiKey).where(ApiKey.key_hash == key_hash))
    if record is None or record.disabled or not hmac.compare_digest(record.key_hash, key_hash):
        raise HTTPException(status.HTTP_401_UNAUTHORIZED, "invalid API key")
    required = set(security_scopes.scopes)
    if not required.issubset(record.scopes):
        raise HTTPException(status.HTTP_403_FORBIDDEN, "API key lacks required scope")
    record.last_used_at = datetime.now(UTC)
    return Principal(record.id, record.name, frozenset(record.scopes))


ChatPrincipal = Annotated[Principal, Security(authenticate, scopes=["chat"])]
ReadPrincipal = Annotated[Principal, Security(authenticate, scopes=["documents:read"])]
AdminPrincipal = Annotated[Principal, Security(authenticate, scopes=["admin:ingest"])]
