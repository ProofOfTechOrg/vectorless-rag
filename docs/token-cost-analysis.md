<!-- Content type: Reference -->
<!-- Content plan: Goal: calculate provider costs for both retrieval paths. Audience: engineers and operators. Content: baselines, formulas, measurements, projections, sensitivities, and exclusions. Open questions: representative long-document ingestion and post-migration query usage. -->

# Calculate provider costs for PageIndex and vector RAG

This quantitative appendix calculates provider cost for PageIndex-based and embedding-based retrieval-augmented generation (RAG) over 537 Portable Document Format (PDF) files. It records corpus measurements, token formulas, live ingestion accounting, historical query traces, sensitivity analysis, and volume projections. Read [Compare vector RAG with the PageIndex system](vector-rag-vs-pageindex-analysis.md) for retrieval quality, latency, scaling, security, and the product decision.

## Measurement baselines

The appendix separates current `vr3` measurements from historical `vr2` traces so it does not mix incompatible telemetry.

| Item | Current live baseline | Historical trace baseline |
| --- | --- | --- |
| Repository branch | `master` | `master` |
| Repository commit | `221ca6509e498af3d64e8296179b4e75a209aef1` | `cba8566a3952b4f7d3a855c8ca19d1e3ea433fd0` |
| Measurement date | July 25, 2026 | July 24, 2026 |
| Corpus | 537 local PDFs under `arxiv-pdfs` | Same corpus |
| Generation model | `deepseek-v4-pro` | `deepseek-v4-pro` |
| PageIndex model | `deepseek/deepseek-v4-pro` | `deepseek/deepseek-v4-pro` |
| PageIndex version | `190f8b378be58199ca993566a9214dba72089c54+vr3` | `190f8b378be58199ca993566a9214dba72089c54+vr2` |
| Durable ingestion calls | Available for post-migration attempts | Unavailable |
| Durable query calls | No post-migration sample yet | Unavailable |
| Langfuse trace measurements | Supporting evidence | Primary query-cost evidence |

The vector baseline uses OpenAI `text-embedding-3-small`, 800-token chunks, 100-token overlap, 20 repeated metadata tokens per chunk, and eight retrieved chunks per answer.

## Pricing baseline

Current official list prices define every dollar calculation in this appendix.

| Provider surface | Price per 1 million tokens |
| --- | ---: |
| DeepSeek V4 Pro cache-hit input | $0.003625 |
| DeepSeek V4 Pro cache-miss input | $0.435 |
| DeepSeek V4 Pro output | $0.87 |
| OpenAI `text-embedding-3-small` input | $0.02 |
| OpenAI `text-embedding-3-large` input | $0.13 |

See [DeepSeek models and pricing](https://api-docs.deepseek.com/quick_start/pricing) and the [OpenAI `text-embedding-3-small` model page](https://developers.openai.com/api/docs/models/text-embedding-3-small). Provider prices can change, so recalculate every dollar amount before a corpus-wide run.

## Current cost summary

The current live report replaces the historical zero-token pilot output. The query figures still come from historical traces because the migration did not backfill the 57 existing query runs.

| Cost surface | PageIndex path | Vector path |
| --- | ---: | ---: |
| Initial 537-PDF indexing, current projection | $69.38 to $76.37, low confidence | $0.2802 for embeddings |
| Initial indexing with shared metadata extraction | Included above | Approximately $1.10 |
| Conservative PageIndex budget | $115 plus retry margin | Not applicable |
| Successful document answer, historical pilot trace | $0.00613 | $0.00360 with route classification and eight chunks |
| Successful document answer, 50-candidate projection | Approximately $0.0114 | $0.00360 |
| 25-token query embedding | None | $0.0000005 |

The current PageIndex projection costs 248 to 273 times the embedding-only vector baseline. It costs 63 to 69 times the product-parity vector baseline.

## Measured corpus size

The corpus measurement parsed every local PDF with PyMuPDF and tokenized its extracted text with `cl100k_base`. No corpus files changed.

| Metric | Measured value |
| --- | ---: |
| PDFs | 537 |
| PDF bytes | 1,460,167,730 bytes |
| Pages | 12,086 |
| Mean pages per document | 22.507 |
| Median pages per document | 19 |
| 10th percentile pages | 10 |
| 90th percentile pages | 37 |
| Minimum pages | 4 |
| Maximum pages | 157 |
| Extracted characters | 43,457,751 |
| `cl100k_base` tokens | 11,960,455 |
| Mean tokens per document | 22,272.7 |
| Median tokens per document | 19,546 |
| Tokens per page | 975.049 |
| Characters per token | 3.688 |

The ten pilot documents average 12.2 pages. The full corpus averages 22.5 pages, so the pilot documents are 46% shorter than the corpus average. A projection based only on document count understates full-corpus PageIndex use.

`cl100k_base` is suitable for estimating the OpenAI embedding workload. DeepSeek uses a different tokenizer, so the PageIndex projection uses provider-reported token counts from live traces instead of the corpus token count.

## Calculate PageIndex indexing cost

Each multi-page ingestion attempt invokes one metadata stage and a sequence of PageIndex calls. Single-page documents bypass PageIndex generation, but this corpus has no single-page PDFs.

### Current live ingestion accounting

The durable report separates metadata and PageIndex calls across every recorded post-migration attempt, including failures.

| Stage | Calls | Cache-hit input | Cache-miss input | Output | Known cost |
| --- | ---: | ---: | ---: | ---: | ---: |
| Metadata | 17 | 21,504 | 34,664 | 6,146 | $0.020504 |
| PageIndex | 961 | 273,024 | 1,475,012 | 119,916 | $0.746947 |
| Combined | 978 | 294,528 | 1,509,676 | 126,062 | $0.767451 |

The report includes 10 completed and seven failed ingestion attempts. Four calls lack reported usage, and 15 older jobs predate durable attempt accounting. The measured cost is therefore known post-migration spend, not complete lifetime spend.

The ingestion path extracts up to 60,000 characters from the first three pages and asks DeepSeek for structured bibliographic metadata. See [`extract_metadata`](../apps/api/src/vectorless_rag/ingestion.py#L131-L153).

PageIndex sends page and section text to DeepSeek while it builds structure, maps headings to pages, repairs mappings, summarizes nodes, and generates a document description. The adapter enables node summaries and document descriptions in [`PageIndexAdapter.index`](../apps/api/src/vectorless_rag/pageindex_adapter.py#L117-L231).

### Current `vr3` full-corpus projection

The projection sample uses the latest telemetry-complete created or rebuilt artifact for each of 10 documents. It covers 122 pages and contains 845 model calls.

| Sample measurement | Value |
| --- | ---: |
| Input tokens | 1,526,188 |
| Cache-hit input tokens | 124,288 |
| Cache-miss input tokens | 1,401,900 |
| Output tokens | 111,583 |
| Total tokens | 1,637,771 |
| Known cost | $0.707354 |

The report projects metadata by document count and PageIndex usage by page count:

| Stage | Projected input | Projected output | Projected cost |
| --- | ---: | ---: | ---: |
| Metadata | 2,016,167 | 238,267 | $0.823396 |
| PageIndex | 147,473,273 | 10,614,480 | $68.555457 |
| Combined page-ratio projection | 149,489,440 | 10,852,747 | $69.378853 |
| Combined linear regression | 167,416,767 | 12,302,393 | $76.369098 |

Confidence is low. Eight sample documents fall at or below the corpus’s 25th page-count percentile. The sample has no documents between the 75th and 90th percentiles or above the 90th percentile.

The current report also records $0.060096 of observed retry overhead. It cannot produce a retry-adjusted projection because four attempts have incomplete telemetry.

### Historical `vr2` sensitivity

The historical traces remain useful as an upper sensitivity case because they recorded a different output-token profile.

| Historical successful-trace measurement | Value |
| --- | ---: |
| Documents | 9 |
| Pages | 109 |
| PageIndex model calls | 760 |
| Input tokens | 1,430,618 |
| Output tokens | 257,328 |
| Calls per page | 6.972 |
| Input tokens per page | 13,124.94 |
| Output tokens per page | 2,360.81 |

The historical constant-per-page projection cost $94.95 at cache-miss pricing. Its linear page-count regression cost $115.01. Applying the observed 1.21% input cache-hit rate reduced the range to $94.12 through $113.99.

Do not merge the historical token counts with the current `vr3` sample. Use $115 plus retry margin as a conservative authorization budget until a representative `vr3` pilot covers the missing page-count bands.

Hosted PageIndex charges one credit per indexed page, with top-up credits priced at $0.01. The equivalent top-up cost for 12,086 pages is $120.86, excluding the subscription. See [PageIndex subscription pricing](https://docs.pageindex.ai/subscription).

### PageIndex indexing formula

Use the following formula when provider prices or telemetry change. The token variables contain PageIndex-only usage, while `metadata_cost` is the separately projected metadata stage:

```text
pageindex_cost =
    cache_miss_input_tokens × cache_miss_input_price
  + cache_hit_input_tokens  × cache_hit_input_price
  + output_tokens           × output_price
  + metadata_cost
```

All token prices in this formula use dollars per token. Divide provider prices quoted per 1 million tokens by 1,000,000 first.

## How conventional vector RAG spends tokens while indexing

The vector baseline parses and chunks PDFs locally, then embeds every chunk once. Deterministic parsing and chunking do not create provider token charges.

For source token count `T`, chunk size `C`, overlap `O`, and repeated chunk metadata `M`, estimate embedded tokens as:

```text
embedded_tokens =
    T × C / (C - O)
  + T / (C - O) × M
```

Substituting this corpus and the baseline assumptions gives:

```text
T = 11,960,455
C = 800
O = 100
M = 20

embedded_tokens ≈ 14,010,819
chunks ≈ 17,086
```

| Embedding model | Provider price per 1 million tokens | Initial embedding cost |
| --- | ---: | ---: |
| OpenAI `text-embedding-3-small` | $0.02 | $0.2802 |
| OpenAI `text-embedding-3-large` | $0.13 | $1.8214 |
| Local embedding model | $0 provider charge | Hardware and operator cost remain |

Adding the current report’s $0.823396 cache-aware metadata projection produces:

| Product-parity vector index | Estimated cost |
| --- | ---: |
| `text-embedding-3-small` plus metadata | $1.1036 |
| `text-embedding-3-large` plus metadata | $2.6448 |

The current PageIndex indexing range is:

- 248 to 273 times the retrieval-only cost of `text-embedding-3-small`
- 63 to 69 times the cost of `text-embedding-3-small` plus shared metadata extraction
- 26 to 29 times the cost of `text-embedding-3-large` plus shared metadata extraction

The small embedding baseline creates approximately 17,086 vectors. A 1,536-dimensional float32 representation uses about 100&nbsp;MiB before vector-index overhead. Storage and database compute are infrastructure costs, not token costs.

## Calculate historical PageIndex query cost

The historical query traces show how generation stages dominate answer cost. No post-migration query sample exists yet.

The stages are:

1. Route the request and resolve conversation history
2. Select up to eight documents from a maximum of 50 candidates
3. Select PageIndex tree nodes
4. Decide whether the fetched evidence is sufficient
5. Synthesize the answer with citations
6. Repeat node selection and sufficiency once when the first evidence set is insufficient

The route and synthesis calls live in [`GraphRunner.run`](../apps/api/src/vectorless_rag/graph.py#L227-L524). Candidate selection, tree-node selection, and sufficiency evaluation live in [`retrieval.py`](../apps/api/src/vectorless_rag/retrieval.py#L124-L246).

The configured retrieval limits are:

| Limit | Value |
| --- | ---: |
| Candidate documents | 50 |
| Selected documents | 8 |
| Page ranges per document | 4 |
| Fetched pages | 24 |
| Evidence tokens | 40,000 |
| Retrieval rounds | 2 |
| Structured-output attempts per call | 3 |

See [`Settings`](../apps/api/src/vectorless_rag/config.py#L21-L59) for the limits except retrieval rounds, which are fixed in [`PageIndexRetriever.retrieve`](../apps/api/src/vectorless_rag/retrieval.py#L179-L246).

### Historical document-answer cost

Three successful document-route traces returned sufficient, cited answers. Each trace made five model calls.

| Component | Average input tokens | Average output tokens |
| --- | ---: | ---: |
| Route and retrieval reasoning | 8,117 | 1,441 |
| Final synthesis | 2,620 | 237 |
| Total | 10,738 | 1,678 |

At cache-miss list prices:

```text
10,738 × $0.435 / 1,000,000
+ 1,678 × $0.87 / 1,000,000
= $0.00613 per successful answer
```

The historical application telemetry observed a 26.96% input cache-hit rate. Applying that rate would reduce the estimate to about $0.0049. The durable schema now preserves cache buckets, but the migration did not backfill these 57 query runs. The $0.00613 value remains a cache-miss estimate rather than exact billed cost.

### Historical costs for other outcomes

The local query history contains 57 runs, including Structured Query Language (SQL) routes. The following values use generation traces associated with each query run and exclude PageIndex ingestion.

| Outcome | Traces | Average calls | Average input | Average output | Cache-miss cost |
| --- | ---: | ---: | ---: | ---: | ---: |
| Sufficient document answer | 3 | 5.00 | 10,738 | 1,678 | $0.00613 |
| Insufficient document answer | 4 | 1.00 | 716 | 196 | $0.00048 |
| Successful SQL route | 30 | 3.13 | 1,903 | 2,237 | $0.00277 |
| Clarification route | 9 | 1.00 | 880 | 194 | $0.00055 |
| Help route | 2 | 0 | 0 | 0 | $0 |
| Errored document route | 1 | 4.00 | 59,486 | 7,269 | $0.03220 |

SQL routes answer catalog and ingestion-status questions from PostgreSQL metadata. They do not retrieve document pages.

The successful document-answer sample is small. Treat its average as a measured pilot point, not a production percentile.

### Full-corpus candidate-selection effect

The candidate query fills the result set to `candidate_limit` when fewer than 50 full-text matches exist. Once at least 50 documents are ready, unrestricted document questions send 50 candidate metadata records to DeepSeek. Metadata constraints or a hybrid-route restriction can reduce that count. See [`CorpusRouter.candidates`](../apps/api/src/vectorless_rag/retrieval.py#L69-L122).

The ten-document pilot’s candidate-selection calls consumed about 3,026 input tokens. Linear payload scaling to 50 documents adds about 12,100 input tokens:

```text
pilot successful answer input     ≈ 10,738 tokens
additional candidate input        ≈ 12,100 tokens
projected full-corpus input       ≈ 22,800 tokens
observed output                   ≈  1,678 tokens
projected cache-miss answer cost  ≈ $0.0114
```

Confidence: moderate-low. The 50-candidate payload size follows directly from the code and historical ten-candidate traces. The projection assumes that later stages select the same number of documents and pages. Selecting more documents, taking a second retrieval round, or approaching the 40,000-token evidence limit increases cost.

The 40,000-token evidence limit is not a request-level billing cap. The system can submit that evidence to sufficiency evaluation more than once and then submit it again for synthesis. Structured-output retries can repeat any failed call up to three times.

## How conventional vector RAG spends tokens while answering

A lean vector RAG request embeds the question, retrieves chunks locally, and sends the selected chunks to the answer model. Product parity adds the current route-classification call.

For query tokens `Q`, retrieved chunk count `K`, chunk tokens `C`, prompt overhead `H`, and output tokens `A`:

```text
vector_query_cost =
    Q × embedding_price
  + (K × C + H) × generation_input_price
  + A × generation_output_price
  + optional_route_cost
  + optional_reranking_cost
```

The baseline uses:

| Variable | Value |
| --- | ---: |
| Query tokens | 25 |
| Retrieved chunks | 8 |
| Tokens per chunk | 800 |
| Prompt overhead | 500 |
| Answer output | 237 |
| Route input | 625 |
| Route output | 141 |

The 237-token answer output matches the current system’s measured final synthesis output. The route usage matches the successful document traces.

| Vector answer component | Cost |
| --- | ---: |
| 25-token query embedding | $0.0000005 |
| Route classification | $0.0003945 |
| 6,900 generation-input tokens | $0.0030015 |
| 237 generation-output tokens | $0.0002065 |
| Total | $0.0036025 |

The query embedding contributes $0.50 after 1 million questions. Answer generation contributes thousands of dollars at that volume.

### Sensitivity to retrieved chunk count

Chunk count changes the vector answer cost more than the query embedding does.

| Retrieved chunks | Generation input | Cost including route and output |
| --- | ---: | ---: |
| 4 | 3,700 | $0.00221 |
| 8 | 6,900 | $0.00360 |
| 16 | 13,300 | $0.00639 |

The historical pilot PageIndex path costs less than the vector baseline only when vector synthesis needs more than about 12,700 input tokens after preserving the same route and output costs. The historical full-candidate PageIndex projection reaches parity only when vector synthesis needs about 24,800 input tokens.

A hosted large language model (LLM) reranker adds another model charge. A local cross-encoder reranker adds compute and latency but no provider token charge. Neither reranker is included in the baseline.

## Query-volume comparison

The volume table applies the measured pilot cost, the projected 50-candidate cost, and the eight-chunk vector baseline.

| Successful content answers | Historical pilot PageIndex | Historical 50-candidate PageIndex | Vector RAG with route | Query embeddings alone |
| ---: | ---: | ---: | ---: | ---: |
| 1,000 | $6.13 | $11.40 | $3.60 | $0.0005 |
| 10,000 | $61.31 | $114.00 | $36.03 | $0.005 |
| 100,000 | $613.08 | $1,140.00 | $360.25 | $0.05 |
| 1,000,000 | $6,130.76 | $11,400.00 | $3,602.53 | $0.50 |

No token-cost break-even emerges from higher query volume under these assumptions. PageIndex has both a higher initial indexing cost and a higher per-answer cost.

## Reindexing and retry costs

Index invalidation changes long-term cost even when the first index fits the budget.

The PageIndex artifact key includes the PDF hash, PageIndex version, metadata prompt version, and PageIndex model. See [`artifact_version_key`](../apps/api/src/vectorless_rag/pageindex_adapter.py#L264-L268) and its use in [`IngestionProcessor.process`](../apps/api/src/vectorless_rag/ingestion.py#L316-L328).

Changing any of these values creates a new artifact version and can trigger the full PageIndex cost:

- PDF contents
- PageIndex version
- Metadata prompt version
- PageIndex model

A conventional vector index requires re-embedding when the PDF, chunking policy, or embedding model changes. Answer prompts and answer-model changes do not require re-embedding.

Both systems can update only changed documents when their index configuration remains stable. PageIndex retries cost more because an unsuccessful attempt can consume multiple generation calls before failing.

## Interpret current durable reporting

Commits `f98fae5` and `19d9a49` replaced the zero-token pilot report with durable model-call accounting and page-aware projections.

The current implementation records:

- Metadata and PageIndex calls against ingestion attempts
- Cache-hit, cache-miss, unattributed input, output, and reasoning buckets
- Completed, failed, and started call outcomes
- Failed attempts and successful retries
- Final document state separately from job and attempt state
- Page-ratio, page-band, and linear-regression projections
- Measured and projected spend as separate values

`make pilot-report` now reports 1,930,266 measured tokens and $0.767451 of known post-migration ingestion cost. It keeps `full_corpus_approved` and `approval_ready` false.

### Remaining ingestion gaps

The current report cannot reconstruct complete lifetime spend because legacy jobs lack attempt records and four attempts have incomplete telemetry.

The next pilot must cover these corpus page-count bands:

- Documents between the 75th and 90th percentiles, 27 to 37 pages
- Documents above the 90th percentile, more than 37 pages
- Documents near the 157-page maximum
- Documents with and without a detected table of contents

A 20 to 30 document stratified pilot will constrain the projection better than the current ten shorter documents. The report should produce an eligible retry-adjusted projection before corpus-wide approval.

### Remaining query gaps

`make query-cost-report DAYS=30` finds 57 historical runs and zero durable model-call rows. The migration did not backfill those calls.

The report correctly marks 55 runs incomplete. It assigns zero cost only to two static help responses that require no model call. New document, hybrid, SQL, clarification, failure, and cancellation samples must populate the durable query ledger before it can replace the historical trace estimates.

## Reproduce the cost reports

Run the aggregate reports from the repository root:

```bash
make pilot-report
make query-cost-report DAYS=30
```

At this baseline, the pilot report returns schema version 2, 10 ready documents, $0.767451 of known measured spend, and a blocked full-corpus gate. The query report returns 57 runs, zero durable model calls, and 55 incomplete runs.

## Use the canonical architecture decision

This appendix establishes provider-cost arithmetic but does not choose the retrieval architecture. Read [Compare vector RAG with the PageIndex system](vector-rag-vs-pageindex-analysis.md) for the current recommendation, retrieval-quality evidence, latency, scaling, security, and the required A/B evaluation.

## Costs excluded from this analysis

This document compares provider token charges. It excludes:

- PostgreSQL or vector-database compute
- Vector-index memory and storage overhead
- MinIO artifact storage
- Langfuse, ClickHouse, and Redis resources
- Worker CPU and memory during PDF parsing
- Local embedding or reranking hardware
- Network transfer
- Provider taxes, committed-use discounts, or enterprise pricing
- Engineering time
- Latency and answer-quality differences

These costs belong in a total-cost-of-ownership analysis. They do not change the conclusion that query-embedding charges are negligible compared with generation charges.

## Confidence summary

Use the confidence levels below when making the indexing decision.

| Claim | Confidence | Basis |
| --- | --- | --- |
| Corpus pages, characters, and `cl100k_base` tokens | High | Every local PDF measured |
| Current provider list prices | High | Official provider documentation |
| Current post-migration ingestion token counts | High for recorded calls | Durable provider-reported usage; four calls lack usage |
| $69.38 current page-ratio projection | Low | Ten short documents; upper page-count bands missing |
| $76.37 current linear projection | Low | Ten documents; upper page-count bands missing |
| $94 to $115 historical sensitivity | Moderate for `vr2` only | Nine complete historical traces |
| $0.28 small-model vector index | Moderate-high | Exact corpus tokens plus stated chunk assumptions |
| $0.00613 historical pilot document-answer cost | High for three traces | Historical trace totals and current list prices |
| Approximately $0.0114 full-corpus document-answer cost | Moderate-low | Candidate payload extrapolation; later stages unmeasured at full scale |
| Vector RAG answer comparison | Moderate | Explicit chunk, route, and output assumptions |
