from __future__ import annotations

import json
import uuid
from dataclasses import dataclass
from pathlib import Path
from typing import Literal

from pydantic import BaseModel, ConfigDict, Field
from sqlalchemy import func, select, text
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine

from vectorless_rag.config import Settings
from vectorless_rag.models import (
    IngestionAttempt,
    IngestionAttemptStatus,
    IngestionJob,
    MeasurementClassificationEvent,
    ModelCall,
)
from vectorless_rag.observability import Observability

CLASSIFICATION_LOCK = 8_674_422_019


class ClassificationEntry(BaseModel):
    model_config = ConfigDict(extra="forbid")

    attempt_id: uuid.UUID
    action: Literal["exclude", "reinstate"]
    reason: Literal[
        "insufficient_credit",
        "pre_freeze_code_bug",
        "manual_review",
        "invalid_experiment",
        "legacy_missing_telemetry",
    ]
    replacement: Literal["latest_successful_same_document"] | uuid.UUID
    justification: str = Field(min_length=1, max_length=2_000)


class ClassificationManifest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    version: Literal[1]
    actor: str = Field(min_length=1, max_length=128)
    entries: list[ClassificationEntry] = Field(min_length=1)


@dataclass(frozen=True)
class ResolvedClassification:
    entry: ClassificationEntry
    attempt: IngestionAttempt
    replacement: IngestionAttempt
    prior_event_id: uuid.UUID | None
    already_applied: bool


def read_classification_manifest(path: Path) -> ClassificationManifest:
    value: object = json.loads(path.read_text(encoding="utf-8"))
    return ClassificationManifest.model_validate(value)


async def resolve_classifications(
    session: AsyncSession,
    manifest: ClassificationManifest,
) -> list[ResolvedClassification]:
    if len({entry.attempt_id for entry in manifest.entries}) != len(manifest.entries):
        raise ValueError("classification manifest contains duplicate attempts")
    resolved: list[ResolvedClassification] = []
    for entry in manifest.entries:
        attempt = await session.get(IngestionAttempt, entry.attempt_id)
        if attempt is None:
            raise ValueError(f"classification attempt does not exist: {entry.attempt_id}")
        attempt_job = await session.get(IngestionJob, attempt.job_id)
        if attempt_job is None:
            raise ValueError(f"classification attempt has no job: {entry.attempt_id}")
        if isinstance(entry.replacement, uuid.UUID):
            replacement = await session.get(IngestionAttempt, entry.replacement)
        else:
            replacement = await session.scalar(
                select(IngestionAttempt)
                .join(IngestionJob, IngestionJob.id == IngestionAttempt.job_id)
                .where(
                    IngestionAttempt.status == IngestionAttemptStatus.completed,
                    IngestionAttempt.created_at > attempt.created_at,
                    IngestionJob.document_id == attempt_job.document_id,
                )
                .order_by(IngestionAttempt.created_at.desc(), IngestionAttempt.id.desc())
                .limit(1)
            )
        if replacement is None:
            raise ValueError(f"no later successful replacement for {entry.attempt_id}")
        if replacement.status != IngestionAttemptStatus.completed:
            raise ValueError(f"replacement attempt is not successful: {replacement.id}")
        replacement_job = await session.get(IngestionJob, replacement.job_id)
        if replacement_job is None or replacement_job.document_id != attempt_job.document_id:
            raise ValueError(f"replacement belongs to a different document: {replacement.id}")
        prior = await session.scalar(
            select(MeasurementClassificationEvent)
            .where(MeasurementClassificationEvent.ingestion_attempt_id == entry.attempt_id)
            .order_by(
                MeasurementClassificationEvent.created_at.desc(),
                MeasurementClassificationEvent.id.desc(),
            )
            .limit(1)
        )
        already = (
            prior is not None
            and prior.action == entry.action
            and prior.reason == entry.reason
            and prior.replacement_attempt_id == replacement.id
        )
        resolved.append(
            ResolvedClassification(
                entry=entry,
                attempt=attempt,
                replacement=replacement,
                prior_event_id=prior.id if prior is not None else None,
                already_applied=already,
            )
        )
    return resolved


async def classification_report(
    settings: Settings,
    *,
    manifest: ClassificationManifest | None = None,
    apply: bool = False,
) -> dict[str, object]:
    engine = create_async_engine(
        settings.database_admin_url,
        isolation_level="SERIALIZABLE",
        pool_pre_ping=True,
    )
    sessions = async_sessionmaker(engine, expire_on_commit=False)
    try:
        sync_scores: list[tuple[str, str]] = []
        async with sessions() as session, session.begin():
            await session.execute(
                text("SELECT pg_advisory_xact_lock(:lock)"),
                {"lock": CLASSIFICATION_LOCK},
            )
            resolved = (
                await resolve_classifications(session, manifest) if manifest is not None else []
            )
            if apply and manifest is not None:
                for item in resolved:
                    if item.already_applied:
                        continue
                    session.add(
                        MeasurementClassificationEvent(
                            ingestion_attempt_id=item.attempt.id,
                            action=item.entry.action,
                            reason=item.entry.reason,
                            actor=manifest.actor,
                            replacement_attempt_id=item.replacement.id,
                            prior_event_id=item.prior_event_id,
                            justification=item.entry.justification,
                            evidence={
                                "replacement_status": item.replacement.status.value,
                                "same_document": True,
                            },
                        )
                    )
                    sync_scores.append(
                        (
                            item.attempt.trace_id,
                            (
                                f"excluded:{item.entry.reason}"
                                if item.entry.action == "exclude"
                                else "eligible:reinstated"
                            ),
                        )
                    )
                await session.flush()
            raw_attempts = await session.scalar(select(func.count()).select_from(IngestionAttempt))
            raw_calls = await session.scalar(select(func.count()).select_from(ModelCall))
            raw_tokens = await session.scalar(
                select(func.coalesce(func.sum(ModelCall.total_tokens), 0))
            )
            effective = (
                (
                    await session.execute(
                        text(
                            """
                        SELECT
                          count(*) FILTER (WHERE effective_action = 'exclude') AS excluded,
                          count(*) FILTER (WHERE effective_action = 'reinstate') AS reinstated
                        FROM agent.effective_measurement_classifications
                        """
                        )
                    )
                )
                .mappings()
                .one()
            )
            report: dict[str, object] = {
                "apply": apply,
                "manifest_count": len(resolved),
                "new_event_count": sum(not item.already_applied for item in resolved),
                "raw": {
                    "attempts": int(raw_attempts or 0),
                    "calls": int(raw_calls or 0),
                    "measured_tokens": int(raw_tokens or 0),
                },
                "effective": {
                    "excluded": int(effective["excluded"] or 0),
                    "reinstated": int(effective["reinstated"] or 0),
                },
                "entries": [
                    {
                        "attempt_id": str(item.attempt.id),
                        "reason": item.entry.reason,
                        "replacement_attempt_id": str(item.replacement.id),
                        "already_applied": item.already_applied,
                    }
                    for item in resolved
                ],
            }
        observability = Observability(settings)
        if sync_scores and observability.client is not None:
            for trace_id, value in sync_scores:
                observability.categorical_score(
                    trace_id,
                    "measurement_eligibility",
                    value,
                )
            observability.flush()
            report["langfuse_sync_attempted"] = len(sync_scores)
        else:
            report["langfuse_sync_attempted"] = 0
        return report
    finally:
        await engine.dispose()
