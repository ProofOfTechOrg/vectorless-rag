from __future__ import annotations

import uuid
from datetime import UTC, datetime, timedelta
from typing import Any, cast

from vectorless_rag.artifact_ops import (
    activate_index_version,
    apply_gc_plan,
    build_gc_plan,
    verify_index_version,
)
from vectorless_rag.models import (
    Document,
    DocumentStatus,
    IndexActivation,
    IndexLifecycleStatus,
    IndexVersion,
)
from vectorless_rag.pageindex_artifact import build_manifest_v2, store_build_v2
from vectorless_rag.storage import MemoryObjectStore


class _ScalarRows:
    def __init__(self, values: list[Any]) -> None:
        self.values = values

    def all(self) -> list[Any]:
        return self.values


class _Session:
    def __init__(
        self,
        *,
        rows: list[list[Any]] | None = None,
        objects: dict[tuple[type[object], uuid.UUID], object] | None = None,
    ) -> None:
        self.rows = list(rows or [])
        self.objects = objects or {}
        self.added: list[object] = []

    async def scalars(self, statement: object) -> _ScalarRows:
        del statement
        return _ScalarRows(self.rows.pop(0))

    async def get(
        self,
        model: type[object],
        identifier: uuid.UUID,
        **options: object,
    ) -> object | None:
        del options
        return self.objects.get((model, identifier))

    def add(self, value: object) -> None:
        self.added.append(value)


def _build(document_id: uuid.UUID):
    return build_manifest_v2(
        {
            "doc_description": "Artifact operations fixture.",
            "structure": [
                {
                    "node_id": "root",
                    "title": "Root",
                    "start_index": 1,
                    "end_index": 2,
                    "summary": "Root summary.",
                }
            ],
        },
        ["first page", "second page"],
        document_id=str(document_id),
        document_sha256="a" * 64,
        metadata={"title": "Fixture"},
        generator_commit="190f8b",
        patch_sha256="b" * 64,
        model="provider/model",
        prompt_profile="pageindex-v2",
        options_profile={"toc": "auto"},
        parser="pymupdf",
        parser_version="1",
    )


def _version(
    *,
    document_id: uuid.UUID,
    version_id: uuid.UUID,
    build: Any,
    verified_at: datetime,
) -> IndexVersion:
    return IndexVersion(
        id=version_id,
        document_id=document_id,
        version_key="v2",
        recipe_key="recipe",
        artifact_object_key=build.manifest_object_key,
        artifact_sha256=build.manifest_sha256,
        artifact_schema_version=2,
        manifest_object_key=build.manifest_object_key,
        manifest_sha256=build.manifest_sha256,
        lifecycle_status=IndexLifecycleStatus.verified,
        verified_at=verified_at,
        created_at=verified_at,
    )


async def test_verify_and_rollback_v2_index() -> None:
    now = datetime(2026, 7, 25, tzinfo=UTC)
    document_id = uuid.uuid4()
    version_id = uuid.uuid4()
    build = _build(document_id)
    store = MemoryObjectStore()
    await store_build_v2(store, build)
    version = _version(
        document_id=document_id,
        version_id=version_id,
        build=build,
        verified_at=now,
    )
    document = Document(
        id=document_id,
        sha256="c" * 64,
        original_filename="document.pdf",
        pdf_object_key="sources/document.pdf",
        status=DocumentStatus.failed,
    )
    session = _Session(
        objects={
            (IndexVersion, version_id): version,
            (Document, document_id): document,
        }
    )

    result = await verify_index_version(cast(Any, session), store, version_id)
    assert result.document_id == document_id
    assert result.page_count == 2
    assert result.node_count == 1

    await activate_index_version(
        cast(Any, session),
        document_id=document_id,
        version_id=version_id,
        actor="operator",
    )
    assert document.active_index_version_id == version_id
    assert document.status == DocumentStatus.ready
    assert len(session.added) == 1
    activation = cast(IndexActivation, session.added[0])
    assert activation.index_version_id == version_id
    assert activation.actor == "operator"


async def test_gc_preserves_active_version_and_recent_orphans() -> None:
    now = datetime(2026, 7, 25, 12, tzinfo=UTC)
    document_id = uuid.uuid4()
    version_id = uuid.uuid4()
    build = _build(document_id)
    store = MemoryObjectStore()
    await store_build_v2(store, build)
    old_orphan = "artifacts/v2/raw/00/old.json"
    recent_orphan = "artifacts/v2/raw/00/recent.json"
    await store.put_bytes(old_orphan, b"old", "application/json")
    await store.put_bytes(recent_orphan, b"recent", "application/json")
    store.modified[old_orphan] = now - timedelta(hours=25)
    store.modified[recent_orphan] = now - timedelta(hours=23)
    version = _version(
        document_id=document_id,
        version_id=version_id,
        build=build,
        verified_at=now - timedelta(days=2),
    )
    session = _Session(
        rows=[
            [version_id],
            [],
            [document_id],
            [version_id],
            [version],
        ]
    )

    plan = await build_gc_plan(cast(Any, session), store, now=now)
    assert plan.protected_version_ids == (version_id,)
    assert set(plan.protected_keys) == {
        build.manifest_object_key,
        build.manifest.raw_object_key,
        *(shard.object_key for shard in build.manifest.page_shards),
    }
    assert plan.delete_keys == (old_orphan,)
    assert plan.retained_orphan_keys == (recent_orphan,)

    await apply_gc_plan(store, plan)
    assert not await store.exists(old_orphan)
    assert await store.exists(recent_orphan)
    assert await store.exists(build.manifest_object_key)
