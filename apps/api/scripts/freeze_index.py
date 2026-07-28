#!/usr/bin/env python3
"""Compute the frozen index digest that identifies what an evaluation ran against.

An evaluation result only means something alongside the index that produced it, and the
index is not one artifact: it is one PageIndex tree per document, each with its own digest.
This reduces that set to a single value the same way the corpus manifest reduces the PDFs,
so a run records exactly which trees were queryable.

The digest covers every ready document carrying an active index version, not only the
documents a case targets, because corpus routing searches the whole catalog: adding or
reindexing an unrelated document changes what the router chooses between, and therefore
changes the measurement.
"""

from __future__ import annotations

import argparse
import asyncio
import hashlib
import json
from pathlib import Path

from sqlalchemy import select

from vectorless_rag.config import get_settings
from vectorless_rag.db import Database
from vectorless_rag.evaluation_suite import load_corpus_manifest
from vectorless_rag.models import Document, DocumentStatus, IndexVersion


async def freeze(corpus: Path | None) -> dict[str, object]:
    settings = get_settings()
    database = Database(settings)
    try:
        async with database.session() as session:
            rows = (
                await session.execute(
                    select(
                        Document.arxiv_id,
                        IndexVersion.artifact_sha256,
                        IndexVersion.configuration_hash,
                        IndexVersion.model_name,
                    )
                    .join(IndexVersion, Document.active_index_version_id == IndexVersion.id)
                    .where(Document.status == DocumentStatus.ready)
                    .order_by(Document.arxiv_id)
                )
            ).all()
    finally:
        await database.close()

    if not rows:
        raise SystemExit("no ready document carries an active index version; nothing to freeze")

    # arxiv_id is nullable and carries no uniqueness constraint, so collapsing rows into a
    # dict keyed by it would drop a digest and still produce a well-formed hash: a corpus
    # that lost a document would freeze indistinguishably from a clean smaller one.
    identifiers = [row[0] for row in rows]
    ambiguous = sorted(
        {str(name) for name in identifiers if name is None or identifiers.count(name) > 1}
    )
    if ambiguous:
        raise SystemExit(
            f"{len(ambiguous)} arXiv identifier(s) are missing or shared by several ready "
            f"documents, so the digest would not identify the index: {ambiguous[:5]}"
        )
    indexed = {str(row[0]): str(row[1]) for row in rows}
    # Same construction as the corpus snapshot: sorted identifier and digest pairs, NUL
    # separated so neither field can imitate the delimiter.
    digest = hashlib.sha256(
        "\n".join(f"{arxiv_id}\0{indexed[arxiv_id]}" for arxiv_id in sorted(indexed)).encode()
    ).hexdigest()

    result: dict[str, object] = {
        "index_sha256": digest,
        "indexed_documents": len(indexed),
        "configuration_hashes": sorted({str(row[2]) for row in rows if row[2]}),
        "models": sorted({str(row[3]) for row in rows if row[3]}),
    }
    if corpus is not None:
        required = {document.arxiv_id for document in load_corpus_manifest(corpus).documents}
        absent = sorted(required - set(indexed))
        result["corpus_manifest_documents"] = len(required)
        result["missing_from_index"] = absent
        if absent:
            raise SystemExit(json.dumps({**result, "error": "targets are not indexed"}, indent=2))
    return result


def main() -> None:
    root = Path(__file__).resolve().parent.parent / "evaluation"
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--corpus",
        type=Path,
        default=root / "pageindex-v2-corpus.json",
        help="fail unless every document this manifest targets is indexed",
    )
    parser.add_argument(
        "--no-corpus-check",
        action="store_true",
        help="digest whatever is indexed without checking the evaluation targets",
    )
    args = parser.parse_args()
    print(json.dumps(asyncio.run(freeze(None if args.no_corpus_check else args.corpus)), indent=2))


if __name__ == "__main__":
    main()
