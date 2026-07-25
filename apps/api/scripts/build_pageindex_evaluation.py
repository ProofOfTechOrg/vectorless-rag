#!/usr/bin/env python3
"""Build 120 deterministic source-grounded PageIndex evaluation cases."""

from __future__ import annotations

import hashlib
import json
import re
from collections import defaultdict
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from typing import Any, Protocol, cast

import fitz

from vectorless_rag.evaluation_suite import (
    STRATA,
    case_source_hash,
    stable_item_id,
)

SENTENCE = re.compile(r"(?s)([A-Z][^\n]{80,420}?[.!?])(?:\s|$)")


class TextPage(Protocol):
    def get_text(self, option: str) -> str: ...


def pdf_record(path: Path) -> dict[str, Any]:
    raw = path.read_bytes()
    with fitz.open(stream=raw, filetype="pdf") as pdf:
        pages = [cast(TextPage, page).get_text("text") for page in pdf]
        metadata = cast(dict[str, object], pdf.metadata or {})
        title = str(metadata.get("title") or "").strip()
    excerpts: list[tuple[int, str]] = []
    for page_number, text in enumerate(pages, 1):
        normalized = " ".join(text.split())
        match = SENTENCE.search(normalized)
        if match:
            excerpts.append((page_number, match.group(1)))
    if not excerpts:
        raise ValueError(f"PDF has no usable source sentence: {path}")
    return {
        "arxiv_id": path.stem,
        "pdf_sha256": hashlib.sha256(raw).hexdigest(),
        "page_count": len(pages),
        "excerpts": excerpts,
        "title": title,
    }


def span(
    document: dict[str, Any],
    excerpt_index: int = 0,
    *,
    avoid_identity: bool = False,
) -> tuple[dict[str, object], str]:
    arxiv_id = str(document["arxiv_id"]).casefold()
    title = str(document["title"]).casefold()
    excerpts = cast(list[tuple[int, str]], document["excerpts"])
    candidates = [
        value
        for value in excerpts
        if arxiv_id not in value[1].casefold()
        and (len(title) < 12 or title not in value[1].casefold())
    ]
    body_candidates = [value for value in candidates if value[0] > 1]
    if body_candidates:
        candidates = body_candidates
    if not candidates:
        if avoid_identity:
            raise ValueError(f"document has no non-identifying source excerpt: {arxiv_id}")
        candidates = excerpts
    page, excerpt = candidates[excerpt_index % len(candidates)]
    return (
        {
            "arxiv_id": document["arxiv_id"],
            "page_start": page,
            "page_end": page,
            "excerpt_sha256": hashlib.sha256(excerpt.encode()).hexdigest(),
        },
        excerpt,
    )


def make_case(
    split: str,
    stratum: str,
    index: int,
    documents: list[dict[str, Any]],
) -> tuple[dict[str, object], list[dict[str, object]]]:
    exact_phrase = stratum == "unconstrained_discovery" and index < 12
    vocabulary_mismatch = stratum in {
        "metadata_constrained_discovery",
        "long_cross_section",
    }
    if stratum == "unanswerable":
        prompt = (
            "What measured effect did the indexed corpus report for the deliberately "
            f"nonexistent mechanism code QZ-{split[:1].upper()}{index:03d}?"
        )
        source: dict[str, object] = {
            "split": split,
            "stratum": stratum,
            "prompt": prompt,
            "constraints": {},
            "atomic_claims": [],
            "reference_answer": "The indexed corpus does not contain evidence for this claim.",
            "answerable": False,
            "target_documents": [],
            "support_sets": [],
            "vocabulary_mismatch": False,
            "exact_phrase": False,
        }
        excerpts: list[dict[str, object]] = []
    else:
        first_support, first_excerpt = span(
            documents[0],
            index,
            avoid_identity=stratum == "unconstrained_discovery",
        )
        support_spans: list[dict[str, object]] = [first_support]
        claims: list[dict[str, object]] = [{"claim": first_excerpt}]
        excerpts = [
            {
                "arxiv_id": documents[0]["arxiv_id"],
                "page": first_support["page_start"],
                "excerpt": first_excerpt,
            }
        ]
        targets: list[dict[str, object]] = [{"arxiv_id": documents[0]["arxiv_id"], "rank": 1}]
        if stratum in {"long_cross_section", "cross_document"}:
            second_support, second_excerpt = span(documents[-1], index + 3)
            support_spans.append(second_support)
            claims.append({"claim": second_excerpt})
            excerpts.append(
                {
                    "arxiv_id": documents[-1]["arxiv_id"],
                    "page": second_support["page_start"],
                    "excerpt": second_excerpt,
                }
            )
            if documents[-1]["arxiv_id"] != documents[0]["arxiv_id"]:
                targets.append({"arxiv_id": documents[-1]["arxiv_id"], "rank": 2})
        if stratum == "explicit_single_document":
            prompt = (
                "In the document selected by the ArXiv constraint, what specific statement "
                "does the cited source passage make?"
            )
            constraints = {"arxiv_ids": [documents[0]["arxiv_id"]]}
        elif stratum == "unconstrained_discovery":
            phrase = " ".join(first_excerpt.split()[:10])
            prompt = (
                f'Which indexed paper contains the exact phrase "{phrase}", and what '
                "claim does the surrounding passage make?"
                if exact_phrase
                else "Which indexed paper provides the source passage about this distinctive "
                f"observation: {' '.join(first_excerpt.split()[4:18])}?"
            )
            constraints = {}
        elif stratum == "metadata_constrained_discovery":
            prompt = (
                "Within the constrained catalog subset, which paper supports the described "
                "finding, expressed with different terminology, and what does it report?"
            )
            constraints = {"date_from": "2000-01-01"}
        elif stratum == "long_cross_section":
            prompt = (
                "Connect the method statement with the later reported observation in the "
                "constrained long document. Cite both supporting sections."
            )
            constraints = {"arxiv_ids": [documents[0]["arxiv_id"]]}
        else:
            prompt = (
                "Which two indexed papers jointly support the paired source claims, and how "
                "do those claims differ? Cite both documents."
            )
            constraints = {}
        source = {
            "split": split,
            "stratum": stratum,
            "prompt": prompt,
            "constraints": constraints,
            "atomic_claims": claims,
            "reference_answer": " ".join(str(claim["claim"]) for claim in claims),
            "answerable": True,
            "target_documents": targets,
            "support_sets": [{"spans": support_spans}],
            "vocabulary_mismatch": vocabulary_mismatch,
            "exact_phrase": exact_phrase,
        }
    source["item_id"] = str(
        stable_item_id(
            split,
            stratum,
            str(source["prompt"]),
            [
                str(target["arxiv_id"])
                for target in cast(list[dict[str, object]], source["target_documents"])
            ],
        )
    )
    source_hash = case_source_hash(source)
    source["review"] = {
        "status": "pending",
        "reviewer": None,
        "reviewed_at": None,
        "source_hash": source_hash,
        "note": None,
    }
    return source, excerpts


def main() -> None:
    api_root = Path(__file__).resolve().parent.parent
    repository_root = api_root.parent.parent
    paths = sorted((repository_root / "arxiv-pdfs").glob("*.pdf"))
    with ThreadPoolExecutor(max_workers=8) as executor:
        records = list(executor.map(pdf_record, paths))
    dev_pool = records[:100]
    regression_pool = records[100:240]
    medium_dev = [record for record in dev_pool if 27 <= int(record["page_count"]) <= 37]
    medium_regression = [
        record for record in regression_pool if 27 <= int(record["page_count"]) <= 37
    ]
    long_dev = [record for record in dev_pool if int(record["page_count"]) > 37]
    long_regression = [record for record in regression_pool if int(record["page_count"]) > 37]
    if (
        len(records) != 537
        or len(long_dev) < 4
        or len(long_regression) < 6
        or len(medium_dev) < 4
        or len(medium_regression) < 6
    ):
        raise ValueError("the tracked 537-PDF corpus snapshot is required")

    offsets: defaultdict[str, int] = defaultdict(int)

    def take(
        split: str,
        count: int,
        *,
        page_band: str | None = None,
    ) -> list[dict[str, Any]]:
        pool = (
            long_dev
            if page_band == "long" and split == "dev"
            else long_regression
            if page_band == "long"
            else medium_dev
            if page_band == "medium" and split == "dev"
            else medium_regression
            if page_band == "medium"
            else dev_pool
            if split == "dev"
            else regression_pool
        )
        key = f"{split}:{page_band or 'all'}"
        start = offsets[key]
        offsets[key] += count
        return pool[start : start + count]

    cases: list[dict[str, object]] = []
    review_rows: list[dict[str, object]] = []
    used: defaultdict[str, set[str]] = defaultdict(set)
    for stratum in STRATA:
        for local_index in range(20):
            split = "dev" if local_index < 8 else "regression"
            required = 0 if stratum == "unanswerable" else 2 if stratum == "cross_document" else 1
            if stratum == "long_cross_section":
                documents = take(
                    split,
                    1,
                    page_band="medium" if local_index % 2 == 0 else "long",
                )
            else:
                documents = take(split, required)
            identifiers = {document["arxiv_id"] for document in documents}
            if used["dev" if split == "regression" else "regression"] & identifiers:
                raise ValueError("generator assigned a document across splits")
            used[split].update(identifiers)
            case, excerpts = make_case(split, stratum, local_index, documents)
            cases.append(case)
            review_rows.append(
                {
                    "item_id": case["item_id"],
                    "split": split,
                    "stratum": stratum,
                    "prompt": case["prompt"],
                    "reference_answer": case["reference_answer"],
                    "excerpts": excerpts,
                }
            )

    target_ids = {
        str(target["arxiv_id"])
        for case in cases
        for target in cast(list[dict[str, object]], case["target_documents"])
    }
    corpus_documents = [
        {
            "arxiv_id": record["arxiv_id"],
            "pdf_sha256": record["pdf_sha256"],
            "page_count": record["page_count"],
        }
        for record in records
        if record["arxiv_id"] in target_ids
    ]
    snapshot_rows = [f"{record['arxiv_id']}\0{record['pdf_sha256']}" for record in records]
    corpus_manifest = {
        "schema_version": 1,
        "corpus_snapshot_sha256": hashlib.sha256("\n".join(snapshot_rows).encode()).hexdigest(),
        "documents": corpus_documents,
    }
    output = api_root / "evaluation/pageindex-v2-regression.jsonl"
    output.write_text(
        "".join(json.dumps(case, ensure_ascii=False, sort_keys=True) + "\n" for case in cases),
        encoding="utf-8",
    )
    (api_root / "evaluation/pageindex-v2-corpus.json").write_text(
        json.dumps(corpus_manifest, indent=2, sort_keys=True) + "\n",
        encoding="utf-8",
    )
    (api_root / "evaluation/pageindex-v2-review-source.json").write_text(
        json.dumps(review_rows, indent=2, ensure_ascii=False, sort_keys=True) + "\n",
        encoding="utf-8",
    )
    print(f"wrote {len(cases)} pending-review cases to {output}")


if __name__ == "__main__":
    main()
