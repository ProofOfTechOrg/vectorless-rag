<!-- Content type: Reference -->
<!-- Content plan: Goal: record every change the vendored PageIndex patch makes and why. Audience: maintainers of the ingestion pipeline. Content: how the patch is applied and versioned, then each change classified as upstream bug fix or pipeline feature. Open questions: none. -->

# PageIndex runtime patch

Reference for `apps/api/patches/pageindex-runtime.patch`: every change it makes to the vendored PageIndex source, and whether each one fixes an upstream bug or adds something the ingestion pipeline needs. The patch applies to [VectifyAI/PageIndex](https://github.com/VectifyAI/PageIndex) at pinned revision `190f8b378be58199ca993566a9214dba72089c54` and touches `pageindex/client.py`, `pageindex/page_index.py`, `pageindex/retrieve.py`, `pageindex/utils.py`, and `requirements.txt`. The current patch revision is `+vr7`.

## How the patch is applied and versioned

`apps/api/scripts/install-pageindex.sh` clones the pinned revision into `/opt/pageindex` during the image build, runs `git apply --check`, then applies the patch. The worker launches the patched CLI through `apps/api/src/vectorless_rag/run_pageindex.py`, which reaches it via `PYTHONPATH`.

Three identity surfaces track the patch:

- **`pageindex_version`**: pinned as the literal `190f8b378be58199ca993566a9214dba72089c54+vr7` in `config.py`. Bump the `+vrN` suffix whenever patch behavior changes.
- **`patch_sha256`**: computed from the patch file at ingestion time in `pageindex_adapter.py`, so any byte change to the patch shifts artifact identity even without a version bump.
- **`configuration_hash` and `recipe_hash`**: both digest the full version string and the patch digest, so a rebuilt document under a new patch never reuses an old artifact.

One scoping note: the prompt-injection hardening in the vendored source (`_SYSTEM_HARDENING`, `_secure_doc_text`, `_validate_physical_indices`, and the `<user_document>` framing) is not part of this patch. The pinned upstream revision already contains it.

## Change summary

Each change is either a fix for a defect in upstream PageIndex or a feature the pipeline needs that upstream lacks:

| Area | File | Kind |
| --- | --- | --- |
| Model-call plumbing routed through the app runtime | `utils.py` | Feature, plus a silent-failure bug fix |
| Strict JSON (`expect_json=True`) on structured calls | `page_index.py` | Feature, plus removal of a corrupting parser |
| Page grouping rewrite with oversized-page splitting | `page_index.py` | Bug fixes, plus a missing feature |
| Continuation merge (dedupe and page-order sort) | `page_index.py` | Bug fix |
| Coverage no longer gates verification | `page_index.py` | Bug fix |
| Subdivision failure degrades instead of propagating | `page_index.py` | Bug fix |
| Pageless-ToC routing in `tree_parser` | `page_index.py` | Bug fix |
| Offset fallback in `process_toc_with_page_numbers` | `page_index.py` | Crash fix |
| `process_none_page_numbers` robustness | `page_index.py` | Crash fixes |
| Large-node recursion repair | `page_index.py` | Bug fixes |
| Bottom-up direct and subtree summaries | `utils.py` | Missing feature |
| `pypdf` migration and empty-page guards | `client.py`, `retrieve.py`, `utils.py`, `requirements.txt` | Security and crash fix |
| litellm cost-map environment default | `utils.py` | Integration nit |

## Model-call plumbing (`utils.py`)

The patch deletes upstream's `llm_completion`, `llm_acompletion`, and `extract_json` and imports all three from `vectorless_rag.pageindex_runtime` instead. This is the integration seam that makes the vendored code a citizen of the pipeline.

Upstream's helpers retried every exception up to 10 times and then returned an empty string, so a persistently failing provider produced silent garbage trees instead of a failed ingestion. The app runtime replaces that with: per-call usage-ledger rows and cost reservations, Langfuse generation traces, bounded retries with classified failure codes (`pageindex_llm_retry_exhausted`, `pageindex_content_filter`, `pageindex_empty_response`, and the rest of `pageindex_failures.py`), a per-call timeout, and a configured output-token cap. Kind: missing feature (accounting, observability, budget enforcement) plus a bug fix (failures now raise instead of returning `""`).

Upstream's `extract_json` also mangled payloads before parsing: it replaced the substring `None` with `null` anywhere in the text and stripped all newlines, corrupting any title or summary containing them. The runtime's parser handles fenced JSON strictly and tolerates the raw control characters real PDF text puts into model output. Kind: bug fix.

## Strict JSON on structured calls (`page_index.py`)

Eleven call sites that expect a JSON object now pass `expect_json=True`: `check_title_appearance`, `check_title_appearance_in_start`, `toc_detector_single_page`, `check_if_toc_extraction_is_complete`, `check_if_toc_transformation_is_complete`, `detect_page_index`, `toc_index_extractor`, `add_page_number_to_toc`, `generate_toc_continue`, `generate_toc_init`, and `single_toc_item_index_fixer`. The runtime validates the completion as JSON at the call boundary and classifies malformed output as `pageindex_invalid_json` instead of letting a half-parsed dict propagate. Kind: missing feature (failure classification at the source).

## Page grouping rewrite (`page_index.py`)

`page_list_to_group_text` splits the document into token-bounded groups for ToC generation. The rewrite fixes two upstream defects and adds one missing capability:

- **Disjoint groups**: upstream overlapped each group with the previous group's last page. Overlap fed the same page to two generation batches, which made continuation batches re-list the overlap page's sections and inflate the ToC with duplicates. The rewrite drops `overlap_page` entirely.
- **Hard token cap**: upstream targeted an averaged group size rather than the cap, and a single page larger than `max_tokens` shipped a group that overflowed the model context. The rewrite enforces `max_tokens` per group.
- **Oversized-page splitting** (missing feature): a page whose text alone exceeds `max_tokens` is now split by binary search into chunks that each stay under the cap while preserving the `<physical_index_N>` markers the model needs for page attribution. Upstream had no path for this.
- **Empty-group guard**: grouping that produces an empty group raises instead of sending a blank prompt.

## Continuation merge (`page_index.py`)

ToC generation for long documents runs in batches: `generate_toc_init` for the first group, `generate_toc_continue` for each following group. Continuation batches habitually re-list sections they were shown as context, sometimes with a shifted page number and sometimes with a page the chunk validator nullifies to `None`. Upstream concatenated batches blindly (`extend`), duplicating sections.

The patch adds `merge_continuation_entries` and calls it from `process_no_toc`. It drops exact re-listings, keyed on case-folded and whitespace-normalized `(structure, title, physical_index)`, keeps everything else in upstream's keep-both spirit, and stable-sorts the result by page with `None` entries last. The sort matters because downstream `post_processing` derives each entry's `end_index` from the next entry's page; an out-of-order re-listing would manufacture a reversed range and fail the whole document at artifact validation. `tree_parser` filters the `None` entries before `post_processing`, so their tail position is safe. Kind: bug fix.

Revision history: `+vr4` shipped a stricter merge that raised `ValueError` on any conflicting re-listing. In production that failed document `2103.05633` deterministically, because disjoint grouping means a re-listed section's marker is absent from the later chunk, the validator nullifies its page to `None`, and `None` never equals the original page. `+vr5` replaced the raise with the dedupe-and-sort behavior above.

## Coverage no longer gates verification (`page_index.py`)

`verify_toc` returned `0, []` without checking a single title whenever the last placed entry
fell in the document's first half, on the assumption that a table of contents spans its
document. Academic papers break that assumption as a matter of course: references and
appendices routinely fill more than half the pages, so a complete and correct table of
contents for the body scores zero. `meta_processor` reads that zero, finds no further
fallback mode, and raises `Processing failed`, discarding the document.

Six of the tracked corpus's 104 documents failed this way. `2408.00724` is representative:
22 pages, 11 correctly placed entries ending at page 10, with acknowledgments on page 11 and
the bibliography from page 12 on. The patch keeps the early return only for a table of
contents where nothing was placed at all. Coverage is not correctness: every title is still
checked individually and the accuracy thresholds are untouched. Kind: bug fix.

## Subdivision failure degrades instead of propagating (`page_index.py`)

`process_large_node_recursively` splits an oversized node into finer ones. When the nested
`meta_processor` raised, the exception propagated out of the whole build, so a document whose
own tree verified at 100% was discarded because one section refused to split - which is what
happened to `2305.16264`, a 50-page document that now yields 67 nodes. Subdividing is an
enhancement over a node that already carries a verified range and summary, so the patch
returns the node unsubdivided instead, exactly as the existing `depth >= 16` guard does.
Kind: bug fix.

## Pageless-ToC routing in `tree_parser` (`page_index.py`)

Upstream routed a document to the ToC-driven path only when the detected ToC also printed page numbers. A document with a real ToC but no page numbers fell into the no-ToC path, which throws the detected ToC away and regenerates structure from raw text. The patch adds the middle branch: detected ToC without page numbers now routes to `process_toc_no_page_numbers`, which keeps the ToC's structure and locates pages by search. Kind: bug fix (upstream discarded signal it had already paid to detect).

## Offset fallback in `process_toc_with_page_numbers` (`page_index.py`)

`calculate_page_offset` returns `None` when no printed page number can be matched to a physical page. Upstream passed that `None` straight into `add_page_offset_to_toc_json` and crashed. The patch falls back to `process_toc_no_page_numbers` for the whole document instead. Kind: crash fix.

## `process_none_page_numbers` robustness (`page_index.py`)

This function repairs ToC entries that arrived without a physical page by asking the model to place them within a neighbor-bounded page window. The patch fixes five defects in it:

- Detects entries whose `physical_index` is explicitly `None`, not only entries missing the key.
- Defaults the search window to the document's real bounds (`start_index` to `start_index + len(page_list) - 1`); upstream defaulted to `0` and `-1`, which produced empty or nonsensical windows for first and last entries.
- Skips the model call when the computed window contains no pages.
- Uses `pop('page', None)` where upstream's `del` raised `KeyError` on entries without a `page` field.
- Validates the model response's shape before indexing into it; upstream crashed on any malformed reply.

Kind: crash fixes.

## Large-node recursion repair (`page_index.py`)

`process_large_node_recursively` subdivides nodes too large to summarize in one pass. The patch fixes four defects:

- **Recursion cap**: `depth >= 16` returns the node as-is. Upstream had no bound, and a model that kept emitting subdividable children recursed without limit.
- **Subdivision trigger**: upstream required the node to exceed both the page limit and the token limit, and computed pages as `end - start` (off by one). Token-dense short nodes and long sparse nodes both escaped subdivision. The patch uses the true page count and triggers on either limit.
- **In-range filtering**: items the model returns with a page outside the node's own span are discarded before tree building.
- **Child retention**: after subdivision, upstream code rewound `node['end_index']` to the first subsection's start and then filtered children against that rewound end. Because every child was built against the original end, the filter deleted every child of every large node, silently flattening exactly the documents that needed subdivision most. The patch captures `original_end` before the rewind and keeps forward-running children (the artifact layer widens ancestors over them), while still dropping children that start before the node (they would annex a preceding sibling) and children spanning the node's original range (the real recursion hazard). During `+vr5` review, an intermediate version of this filter still lost a first subsection that starts on the node's own first page; comparing against `original_end` fixed that too, and `scripts/test_pageindex_patch.py` pins it.

Kind: bug fixes. Rebuilding document `2103.05633` after the final fix raised its tree from 37 nodes at depth 3 to 45 nodes at depth 4; the difference is structure the old filter was deleting.

## Bottom-up summaries (`utils.py`)

The v2 artifact distinguishes a node's own content from its subtree. Upstream summarized every node independently and in parallel from `node['text']`, produced a single `summary` field, and crashed with `KeyError` when node text was disabled (`--if-add-node-text no`, which is how the pipeline runs it).

The patched `generate_summaries_for_structure` walks each tree bottom-up: it summarizes children first, then produces both a `direct_summary` (the node alone) and a `subtree_summary` (the node with its children's summaries folded into the prompt), storing the subtree summary as `summary` for compatibility. `generate_node_summary` accepts the child summaries and tolerates missing text. Kind: missing feature; the artifact schema's `direct_summary` and `subtree_summary` fields depend on it.

## `pypdf` migration and empty-page guards (`utils.py`)

The patch replaces unmaintained `PyPDF2` with maintained `pypdf` in `client.py`, `retrieve.py`, `utils.py`, and `requirements.txt`. Multi-page artifacts generated through PageIndex record `pypdf` and its installed version in artifact identity; the single-page shortcut records the PyMuPDF parser it actually uses. The parser’s `extract_text()` can return `None` for pages without extractable text, and upstream concatenated that `None` into a string in three places (`extract_text_from_pdf`, `get_text_of_pages`, `get_page_tokens`). Each site now appends `page.extract_text() or ""`. Kind: security and crash fix.

## litellm cost-map default (`utils.py`)

`LITELLM_LOCAL_MODEL_COST_MAP=True` is set before the `litellm` import so the child process uses the library's bundled model-cost table instead of fetching the remote one at import time. Kind: integration nit; keeps the isolated child deterministic and network-quiet at startup.

## Verifying the patch

`apps/api/scripts/test_pageindex_patch.py` runs provider-free regression checks against a patched checkout: marker-preserving page grouping, continuation merge semantics (dedupe, keep-both, page ordering, `None` placement), the large-node first-subsection case, pageless-ToC routing, and bottom-up summary ordering. Run it against any checkout:

```bash
uv --project apps/api run apps/api/scripts/test_pageindex_patch.py /path/to/patched/pageindex
```

Inside the api container the installed copy lives at `/opt/pageindex`, and `install-pageindex.sh` fails the image build if the patch stops applying cleanly to the pinned revision.
