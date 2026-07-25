from __future__ import annotations

import uuid
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta

from sqlalchemy import desc, select
from sqlalchemy.ext.asyncio import AsyncSession

from vectorless_rag.models import (
    Document,
    DocumentStatus,
    ExperimentIndexVersion,
    IndexActivation,
    IndexActivationReason,
    IndexLifecycleStatus,
    IndexVersion,
)
from vectorless_rag.pageindex_artifact import LoadedPageIndexV2, load_manifest_v2
from vectorless_rag.storage import ObjectInfo, ObjectStore


@dataclass(frozen=True)
class ArtifactVerification:
    index_version_id: uuid.UUID
    document_id: uuid.UUID
    manifest_sha256: str
    page_count: int
    node_count: int


@dataclass(frozen=True)
class GarbageCollectionPlan:
    generated_at: datetime
    grace_cutoff: datetime
    protected_version_ids: tuple[uuid.UUID, ...]
    protected_keys: tuple[str, ...]
    delete_keys: tuple[str, ...]
    delete_bytes: int
    retained_orphan_keys: tuple[str, ...]


async def verify_index_version(
    session: AsyncSession,
    store: ObjectStore,
    version_id: uuid.UUID,
) -> ArtifactVerification:
    version = await session.get(IndexVersion, version_id)
    if (
        version is None
        or version.artifact_schema_version != 2
        or version.manifest_object_key is None
        or version.manifest_sha256 is None
    ):
        raise ValueError("index version is not a PageIndex-v2 artifact")
    loaded = await load_manifest_v2(
        store,
        version.manifest_object_key,
        version.manifest_sha256,
    )
    if loaded.manifest.document_id != str(version.document_id):
        raise ValueError("manifest document identity does not match the index version")
    return ArtifactVerification(
        index_version_id=version.id,
        document_id=version.document_id,
        manifest_sha256=version.manifest_sha256,
        page_count=loaded.manifest.quality.page_count,
        node_count=loaded.manifest.quality.node_count,
    )


async def activate_index_version(
    session: AsyncSession,
    *,
    document_id: uuid.UUID,
    version_id: uuid.UUID,
    actor: str,
    reason: IndexActivationReason = IndexActivationReason.rollback,
) -> None:
    document = await session.get(Document, document_id, with_for_update=True)
    version = await session.get(IndexVersion, version_id, with_for_update=True)
    if document is None or version is None or version.document_id != document_id:
        raise ValueError("index version does not belong to the requested document")
    if (
        version.lifecycle_status not in {IndexLifecycleStatus.active, IndexLifecycleStatus.verified}
        or version.verified_at is None
    ):
        raise ValueError("only a verified index version can be activated")
    if document.active_index_version_id == version.id:
        return
    prior = document.active_index_version_id
    session.add(
        IndexActivation(
            document_id=document.id,
            prior_index_version_id=prior,
            index_version_id=version.id,
            reason=reason,
            actor=actor[:128],
        )
    )
    document.active_index_version_id = version.id
    document.status = DocumentStatus.ready


def _manifest_keys(loaded: LoadedPageIndexV2, manifest_key: str) -> set[str]:
    return {
        manifest_key,
        loaded.manifest.raw_object_key,
        *(shard.object_key for shard in loaded.manifest.page_shards),
    }


async def build_gc_plan(
    session: AsyncSession,
    store: ObjectStore,
    *,
    now: datetime | None = None,
    grace: timedelta = timedelta(hours=24),
) -> GarbageCollectionPlan:
    generated_at = now or datetime.now(UTC)
    grace_cutoff = generated_at - grace
    active_ids = set(
        (
            await session.scalars(
                select(Document.active_index_version_id).where(
                    Document.active_index_version_id.is_not(None)
                )
            )
        ).all()
    )
    frozen_ids = set((await session.scalars(select(ExperimentIndexVersion.index_version_id))).all())
    newest_ids: set[uuid.UUID] = set()
    document_ids = list((await session.scalars(select(Document.id))).all())
    for document_id in document_ids:
        newest_ids.update(
            (
                await session.scalars(
                    select(IndexVersion.id)
                    .where(
                        IndexVersion.document_id == document_id,
                        IndexVersion.artifact_schema_version == 2,
                        IndexVersion.verified_at.is_not(None),
                        IndexVersion.lifecycle_status.in_(
                            [IndexLifecycleStatus.active, IndexLifecycleStatus.verified]
                        ),
                    )
                    .order_by(desc(IndexVersion.verified_at), desc(IndexVersion.created_at))
                    .limit(2)
                )
            ).all()
        )
    protected_ids = {value for value in active_ids if value is not None} | newest_ids | frozen_ids
    versions = list(
        (
            await session.scalars(select(IndexVersion).where(IndexVersion.id.in_(protected_ids)))
        ).all()
    )
    protected_keys: set[str] = set()
    for version in versions:
        if version.manifest_object_key is None or version.manifest_sha256 is None:
            continue
        loaded = await load_manifest_v2(
            store,
            version.manifest_object_key,
            version.manifest_sha256,
        )
        protected_keys.update(_manifest_keys(loaded, version.manifest_object_key))

    objects = await store.list_objects("artifacts/v2/")
    delete: list[ObjectInfo] = []
    retained_orphans: list[str] = []
    for item in objects:
        if item.key in protected_keys:
            continue
        if item.last_modified is None or item.last_modified > grace_cutoff:
            retained_orphans.append(item.key)
            continue
        delete.append(item)
    return GarbageCollectionPlan(
        generated_at=generated_at,
        grace_cutoff=grace_cutoff,
        protected_version_ids=tuple(sorted(protected_ids, key=str)),
        protected_keys=tuple(sorted(protected_keys)),
        delete_keys=tuple(item.key for item in delete),
        delete_bytes=sum(item.size for item in delete),
        retained_orphan_keys=tuple(sorted(retained_orphans)),
    )


async def apply_gc_plan(store: ObjectStore, plan: GarbageCollectionPlan) -> None:
    await store.delete_objects(list(plan.delete_keys))
