#!/usr/bin/env python3
"""Validate the frozen 25-document pilot manifest without external services."""

from __future__ import annotations

import json
from collections import Counter
from pathlib import Path

from vectorless_rag.pilot_suite import load_pilot_manifest


def main() -> None:
    path = Path(__file__).resolve().parent.parent / "pilots" / "pageindex-v2-25.json"
    manifest = load_pilot_manifest(path)
    print(
        json.dumps(
            {
                "documents": len(manifest.documents),
                "page_bands": dict(
                    sorted(Counter(document.page_band for document in manifest.documents).items())
                ),
                "outline": Counter(document.pdf_outline for document in manifest.documents),
                "corpus_snapshot_sha256": manifest.corpus_snapshot_sha256,
            },
            indent=2,
            sort_keys=True,
        )
    )


if __name__ == "__main__":
    main()
