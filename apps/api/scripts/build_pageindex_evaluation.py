#!/usr/bin/env python3
"""Build 120 deterministic source-grounded PageIndex evaluation cases."""

from __future__ import annotations

import hashlib
import json
import re
import unicodedata
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

# Strata whose prompts must restate the finding in different words. Genuine paraphrase
# cannot be produced by string manipulation, so these carry human-reviewable authored
# text bound to their source excerpts by hash.
AUTHORED_STRATA = frozenset({"metadata_constrained_discovery", "long_cross_section"})
AUTHORED_PATH = Path(__file__).resolve().parent.parent / "evaluation/pageindex-v2-authored.json"

MATH_GLYPHS = frozenset("∈∑∗▷⊥≥≤⇒∀∂⟨⟩×÷±∇∏√≈≠∞⊕⊗Σ∫∼‖→≜′ˆ←−αβγδεζηθκλμνξπρστφχψωΓΔΘΛΞΠΣΦΨΩ")
CAPTION = re.compile(r"\b(?:Figure|Fig\.|Table|Algorithm|Listing|Eq\.|Equation)\s*\d", re.I)
VENUE = re.compile(
    r"(?:Published as|Under review as|Preprint\b|arXiv:|Proceedings of|Workshop on|"
    r"Permission to make digital|Copyright|ACM ISBN|doi:|https?://|©)",
    re.I,
)
PSEUDOCODE = re.compile(r"(?:\b\d+:\s)|(?:\bend (?:for|while|if|procedure|function)\b)", re.I)
CITATION = re.compile(r"et al\.,? \d{4}[a-z]?|\[\d+\]")
AUTHOR_RUN = re.compile(r"[A-Z][a-z]+ [A-Z][a-zA-Z.]*,")
# A hyphen before a line break is indistinguishable from a compound hyphen that happens to
# fall there: "dif- ferent" and "LLM- based" have identical shape, so joining corrupts one to
# repair the other. Excerpts are quoted verbatim as reference answers, so such sentences are
# discarded rather than guessed at - the candidate pool is large enough to absorb the loss.
HYPHEN_BREAK = re.compile(r"\w-\s+\w")
# Appendices quote prompt exemplars and sampled model output, and contribution lists break
# into bullet or enumeration fragments; neither is a claim the paper asserts.
EXEMPLAR = re.compile(
    r"\bQ:\s|\bA:\s|[•‣]|\*\*|∗∗|\b\d\)\s|^[A-Za-z]\)|et al\.$"
    # Footnote markers glue their text onto the preceding word ("harmless 1We shorten ...").
    r"|\s\d[A-Z][a-z]{2,}"
    r"|§"
    # Inline symbol definitions ("the gradient g(b) for token type b") read as prose but
    # carry no standalone claim.
    r"|\b[a-z]\([a-z]\)\s"
    # Author blocks and bibliography entries survive the citation rules but carry contacts.
    r"|@|www\.|\.com\b|\.org\b"
    r"|^(?:Left|Right|Top|Bottom|Middle):"
    # Section headings run into the following sentence: a leading all-caps run
    # ("RELEARN SET CONSTRUCTION Building on ...") or a numbered heading mid-string
    # ("... appear in the Appendix. 2. BACKGROUND In this section ...").
    r"|^(?:[A-Z]{2,}\s+){2,}[A-Z]"
    r"|\s\d+(?:\.\d+)*\.\s+[A-Z]{2,}\s"
    # Papers on agents quote their own prompt templates verbatim.
    r"|\bUser Instruction:|\bYou are participating|\$\{|\[Agent\]|\[Expected"
)
# Papers quote generated stories as qualitative samples. They read as clean prose but assert
# nothing, and these papers describe agents as "the model" or "we", never "she" or "grandma".
NARRATIVE = re.compile(
    r"\b(?:she|he|her|him|hers|mom|mommy|dad|daddy|grandma|grandpa|once upon|sister|"
    r"brother|puppy|kitty|toy|candy)\b",
    re.I,
)


def normalize(text: str) -> str:
    # NFKC already folds the ff/fi/fl/ffi/ffl ligatures PDF text layers emit.
    return unicodedata.normalize("NFKC", text)


def is_prose(text: str) -> bool:
    """Reject figure captions, running headers, reference entries, math, and pseudocode.

    The generator quotes excerpts verbatim as reference answers, so a candidate is only
    usable when it reads as a self-contained prose claim rather than extracted page
    furniture.
    """
    if CAPTION.search(text) or VENUE.search(text) or PSEUDOCODE.search(text):
        return False
    if EXEMPLAR.search(text) or NARRATIVE.search(text) or HYPHEN_BREAK.search(text):
        return False
    if len(CITATION.findall(text)) >= 2 or len(AUTHOR_RUN.findall(text)) >= 3:
        return False
    # Unbalanced parentheses mean the sentence regex cut a clause mid-reference.
    if text.count("(") != text.count(")"):
        return False
    if sum(character in MATH_GLYPHS for character in text) > 2:
        return False
    if "�" in text or "??" in text:
        return False
    words = text.split()
    if not 12 <= len(words) <= 60:
        return False
    # anchor() truncates at the first quote, so a quote inside the opening words would
    # leave too little text to identify the passage.
    if re.search(r'["“”]', " ".join(words[:5])):
        return False
    letters = sum(character.isalpha() or character.isspace() for character in text)
    if letters / len(text) < 0.86:
        return False
    if sum(character.isdigit() for character in text) / len(text) > 0.04:
        return False
    # Prose is mostly lowercase function and content words, not label or acronym soup.
    lowercase = sum(1 for word in words if word[:1].islower() and word.isalpha())
    return lowercase / len(words) >= 0.55


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
        normalized = normalize(" ".join(text.split()))
        # Every prose sentence is a candidate; taking only the first match per page made
        # whichever caption or running header sat at the top of the page the sole option.
        for match in SENTENCE.finditer(normalized):
            sentence = match.group(1)
            if is_prose(sentence):
                excerpts.append((page_number, sentence))
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
    after_page: int = 0,
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
    if after_page:
        # long_cross_section claims a "later reported observation"; enforce that ordering
        # instead of trusting an excerpt offset to land on a subsequent page.
        later = [value for value in candidates if value[0] > after_page]
        if not later:
            raise ValueError(f"document has no source excerpt after page {after_page}: {arxiv_id}")
        candidates = later
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


def anchor(excerpt: str, words: int = 9) -> str:
    """Quote the opening of an excerpt, stopping before any quote character it contains.

    Prompts wrap the anchor in double quotes, so an excerpt that itself quotes something
    ("An example would be, \"Set an alarm\"") would otherwise produce a prompt whose quoted
    span cannot be parsed - by a reader or by a retrieval system matching the phrase.
    """
    head = " ".join(excerpt.split()[:words])
    return re.split(r'["“”]', head)[0].strip()


def catalog_subset(
    target: str,
    records: list[dict[str, Any]],
    index: int,
    size: int = 40,
) -> dict[str, object]:
    """Restrict the catalog to a deterministic subset that always contains the target.

    A submission-date window cannot be used here: Document.submitted_date is extracted by
    the model from each PDF's first page, and the corpus stores latest versions whose date
    stamp is the revision date, so a window derived from the arXiv ID excludes most targets
    outright. An identifier subset is the only catalog constraint this generator can verify
    offline, so the stratum narrows the catalog by membership rather than by date.
    """
    others = [str(record["arxiv_id"]) for record in records if record["arxiv_id"] != target]
    stride = max(1, len(others) // max(1, size - 1))
    picked = others[index % stride :: stride][: size - 1]
    return {"arxiv_ids": sorted({target, *picked})}


class AuthoringRequired(Exception):
    """Raised when a paraphrase case has no authored content bound to its current source."""

    def __init__(
        self,
        key: str,
        excerpt_sha256: list[str],
        excerpts: list[dict[str, object]],
    ) -> None:
        super().__init__(key)
        self.key = key
        self.entry: dict[str, object] = {
            "excerpt_sha256": excerpt_sha256,
            "excerpts": excerpts,
        }


def load_authored() -> dict[str, dict[str, Any]]:
    if not AUTHORED_PATH.exists():
        return {}
    return cast(dict[str, dict[str, Any]], json.loads(AUTHORED_PATH.read_text(encoding="utf-8")))


def authored_entry(
    authored: dict[str, dict[str, Any]],
    key: str,
    support_spans: list[dict[str, object]],
    excerpts: list[dict[str, object]],
) -> dict[str, str]:
    """Return authored prompt/answer bound to the exact excerpts it was written against.

    Binding on excerpt hashes makes the pairing fail closed: any change to excerpt
    selection invalidates the authored text instead of silently pairing a paraphrase with
    a passage it no longer describes.
    """
    entry = authored.get(key)
    expected = [str(support["excerpt_sha256"]) for support in support_spans]
    # A hand-edited entry that is malformed routes to the queue like a missing one, so the
    # operator gets the actionable path instead of a bare KeyError on an unnamed entry.
    if (
        entry is None
        or list(entry.get("excerpt_sha256", [])) != expected
        or not str(entry.get("prompt", "")).strip()
        or not str(entry.get("reference_answer", "")).strip()
    ):
        raise AuthoringRequired(key, expected, excerpts)
    return {"prompt": str(entry["prompt"]), "reference_answer": str(entry["reference_answer"])}


def carry_reviews(cases: list[dict[str, object]], previous: Path) -> int:
    """Re-attach completed reviews to rebuilt cases whose content is byte-identical.

    Rebuilding is part of the authoring loop, so discarding every decision on each run would
    make reviewing before authoring finishes pointless. The review source hash is the
    identity: a case whose content changed at all keeps its fresh `pending` record, which is
    the correct outcome because the reviewer approved different text.
    """
    if not previous.exists():
        return 0
    prior: dict[str, dict[str, object]] = {}
    for line in previous.read_text(encoding="utf-8").splitlines():
        if not line.strip():
            continue
        record = cast(dict[str, Any], json.loads(line))
        review = cast(dict[str, object], record["review"])
        if review.get("status") in {"approved", "edited", "rejected"}:
            prior[str(review["source_hash"])] = review
    carried = 0
    for case in cases:
        review = cast(dict[str, object], case["review"])
        match = prior.get(str(review["source_hash"]))
        if match is not None:
            case["review"] = match
            carried += 1
    return carried


def make_case(
    authored: dict[str, dict[str, Any]],
    split: str,
    stratum: str,
    index: int,
    documents: list[dict[str, Any]],
    catalog: list[dict[str, Any]],
) -> tuple[dict[str, object], list[dict[str, object]]]:
    # exact_phrase records whether the prompt quotes the source verbatim, because lexical
    # overlap is exactly what separates keyword retrieval from a structural index.
    exact_phrase = stratum in {"explicit_single_document", "cross_document"} or (
        stratum == "unconstrained_discovery" and index < 12
    )
    vocabulary_mismatch = stratum in AUTHORED_STRATA
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
        second_excerpt = ""
        if stratum in {"long_cross_section", "cross_document"}:
            second_support, second_excerpt = span(
                documents[-1],
                index + 3,
                after_page=(
                    int(cast(int, first_support["page_end"]))
                    if stratum == "long_cross_section"
                    else 0
                ),
            )
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
        entry: dict[str, str] | None = None
        constraints: dict[str, object]
        if stratum in AUTHORED_STRATA:
            entry = authored_entry(
                authored, f"{split}:{stratum}:{index:02d}", support_spans, excerpts
            )
            prompt = entry["prompt"]
            constraints = (
                catalog_subset(str(documents[0]["arxiv_id"]), catalog, index)
                if stratum == "metadata_constrained_discovery"
                else {"arxiv_ids": [documents[0]["arxiv_id"]]}
            )
        elif stratum == "explicit_single_document":
            prompt = (
                "In the document selected by the ArXiv constraint, locate the passage "
                f'beginning "{anchor(first_excerpt)}" and state in full what it reports.'
            )
            constraints = {"arxiv_ids": [documents[0]["arxiv_id"]]}
        elif stratum == "unconstrained_discovery":
            prompt = (
                f'Which indexed paper contains the exact phrase "{anchor(first_excerpt, 10)}", '
                "and what claim does the surrounding passage make?"
                if exact_phrase
                else "Which indexed paper provides the source passage about this distinctive "
                f"observation: {' '.join(first_excerpt.split()[4:18])}?"
            )
            constraints = {}
        elif stratum == "cross_document":
            prompt = (
                "One indexed paper contains a passage beginning "
                f'"{anchor(first_excerpt)}" and a different indexed paper contains a '
                f'passage beginning "{anchor(second_excerpt)}". Identify both papers and '
                "state in full what each passage reports. Cite both documents."
            )
            constraints = {}
        else:
            raise ValueError(f"unhandled stratum: {stratum}")
        source = {
            "split": split,
            "stratum": stratum,
            "prompt": prompt,
            "constraints": constraints,
            "atomic_claims": claims,
            "reference_answer": (
                entry["reference_answer"]
                if entry
                else " ".join(str(claim["claim"]) for claim in claims)
            ),
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

    authored = load_authored()
    cases: list[dict[str, object]] = []
    review_rows: list[dict[str, object]] = []
    pending_authoring: dict[str, dict[str, object]] = {}
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
            try:
                case, excerpts = make_case(
                    authored, split, stratum, local_index, documents, records
                )
            except AuthoringRequired as pending:
                # Keep walking every stratum so one pass enumerates the complete queue.
                pending_authoring[pending.key] = pending.entry
                continue
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

    queue = api_root / "evaluation/pageindex-v2-authoring-queue.json"
    if pending_authoring:
        queue.write_text(
            json.dumps(pending_authoring, indent=2, ensure_ascii=False, sort_keys=True) + "\n",
            encoding="utf-8",
        )
        raise SystemExit(
            f"{len(pending_authoring)} paraphrase cases need authored prompts.\n"
            f"Source excerpts written to {queue}\n"
            f"Write prompt/reference_answer/excerpt_sha256 per key into {AUTHORED_PATH}"
        )
    # A stale queue would otherwise keep advertising authoring work that is already done.
    queue.unlink(missing_ok=True)

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
    carried = carry_reviews(cases, output)
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
    print(f"wrote {len(cases)} cases to {output} ({carried} completed review(s) carried forward)")


if __name__ == "__main__":
    main()
