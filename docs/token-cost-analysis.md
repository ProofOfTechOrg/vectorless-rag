<!-- Content type: Conceptual -->

# Compare token costs for vectorless and embedding RAG

This analysis compares the provider token costs of the current PageIndex-based vectorless retrieval-augmented generation (RAG) system with a conventional embedding and vector-search RAG system. It covers initial Portable Document Format (PDF) indexing, query embeddings, retrieval-time model calls, answer synthesis, retries, cache pricing, query volume, and the defects that currently make `make pilot-report` report zero token use.

## Analysis baseline

The measurements and prices in this document use the following baseline:

| Item | Baseline |
| --- | --- |
| Repository branch | `master` |
| Repository commit | `cba8566a3952b4f7d3a855c8ca19d1e3ea433fd0` |
| Measurement date | July 24, 2026 |
| Corpus | The 537 local PDFs under `arxiv-pdfs` |
| Generation model | `deepseek-v4-pro` |
| PageIndex model | `deepseek/deepseek-v4-pro` |
| PageIndex version | `190f8b378be58199ca993566a9214dba72089c54+vr2` |
| Embedding baseline | OpenAI `text-embedding-3-small` |
| Embedding chunk size | 800 tokens |
| Embedding chunk overlap | 100 tokens |
| Vector answer context | Eight 800-token chunks |

DeepSeek V4 Pro charges $0.435 per 1 million cache-miss input tokens, $0.003625 per 1 million cache-hit input tokens, and $0.87 per 1 million output tokens. See [DeepSeek models and pricing](https://api-docs.deepseek.com/quick_start/pricing).

OpenAI charges $0.02 per 1 million tokens for `text-embedding-3-small` and $0.13 per 1 million tokens for `text-embedding-3-large`. See the [OpenAI `text-embedding-3-small` model page](https://developers.openai.com/api/docs/models/text-embedding-3-small).

Provider prices can change. Recalculate every dollar amount before approving a large ingestion run.

## Cost conclusion

Conventional vector RAG costs less at both indexing time and answer time under the stated assumptions.

| Cost surface | Current PageIndex system | Conventional vector RAG |
| --- | ---: | ---: |
| Initial 537-PDF indexing | $94 to $115 | $0.28 with `text-embedding-3-small` |
| Initial indexing with shared LLM metadata extraction | Included above | Approximately $1.40 |
| Successful document answer, measured 10-document pilot | $0.00613 | $0.00360 with route classification and eight chunks |
| Successful document answer, projected 537-document corpus | Approximately $0.0114 | $0.00360 with route classification and eight chunks |
| 25-token query embedding | None | $0.0000005 |

The current system is already a RAG system. It replaces vector similarity search with PostgreSQL metadata search followed by large language model (LLM) selection over PageIndex trees. The relevant distinction is vectorless, reasoning-based RAG versus embedding and vector-search RAG.

PageIndex can still be the right choice when it produces enough additional retrieval quality to justify the cost and latency. Token cost alone does not justify it.

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

## How the current system spends tokens while indexing

Each multi-page PDF incurs one metadata call and many PageIndex calls. Single-page documents bypass PageIndex generation, but this corpus has no single-page PDFs.

### Metadata extraction

The ingestion path extracts text from the first three pages, truncates it to 60,000 characters, and asks DeepSeek for structured bibliographic metadata. See [`extract_metadata`](../apps/api/src/vectorless_rag/ingestion.py#L108-L128).

Live metadata calls consumed approximately:

| Usage per document | Tokens |
| --- | ---: |
| Input | 3,900 |
| Output | 450 |

Applying those observations to 537 documents gives:

| Full-corpus metadata usage | Tokens | Cost |
| --- | ---: | ---: |
| Input | 2,094,300 | $0.91 at cache-miss pricing |
| Output | 241,650 | $0.21 |
| Total | 2,335,950 | Approximately $1.12 |

This metadata cost belongs to the current product rather than to PageIndex retrieval itself. A vector system that preserves the same catalog features should pay the same cost. A retrieval-only vector system can omit it.

### PageIndex tree construction

PageIndex sends page and section text to DeepSeek repeatedly while it detects or generates a table of contents, maps physical pages, verifies headings, repairs mappings, summarizes nodes, and generates a document description. The adapter enables node summaries and document descriptions in [`PageIndexAdapter.index`](../apps/api/src/vectorless_rag/pageindex_adapter.py#L134-L187).

Nine successful documents retained complete PageIndex token telemetry. One completed pilot document lacked retained PageIndex child telemetry and is excluded from the per-page calculation.

| Successful-trace measurement | Value |
| --- | ---: |
| Documents | 9 |
| Pages | 109 |
| PageIndex model calls | 760 |
| Input tokens | 1,430,618 |
| Output tokens | 257,328 |
| Calls per page | 6.972 |
| Input tokens per page | 13,124.94 |
| Output tokens per page | 2,360.81 |

The source corpus contains about 975 extracted tokens per page. PageIndex bills about 15,486 input and output tokens per page because its tree construction calls reuse document text and produce structured reasoning outputs.

### Full-corpus PageIndex projection

Two projections define a practical range. The first applies the observed tokens-per-page ratios. The second fits input and output tokens to page count across the nine successful traces.

| Projection | Input tokens, including metadata | Output tokens, including metadata | Cache-miss cost |
| --- | ---: | ---: | ---: |
| Constant observed tokens per page | 160,722,274 | 28,774,368 | $94.95 |
| Linear page-count regression | 197,168,909 | 33,612,260 | $115.01 |

The older automatic PageIndex telemetry retained DeepSeek cache buckets and observed a 1.21% input cache-hit rate. Applying that rate reduces the range to $94.12 through $113.99.

Confidence: moderate. The underlying trace counts are measured, but nine documents cannot establish the exact behavior of the corpus’s 157-page upper tail. The regression explains 84% of input-token variance and 52% of output-token variance.

The live pilot incurred approximately $1.1 across the original jobs, failures, and retries before all ten documents reached a completed state. Scaling that observed spend by the corpus page ratio also lands inside the $94 to $115 range.

Hosted PageIndex charges one credit per indexed page, with top-up credits priced at $0.01. The equivalent top-up cost for 12,086 pages is $120.86, excluding the subscription. See [PageIndex subscription pricing](https://docs.pageindex.ai/subscription).

### PageIndex indexing formula

Use the following formula when provider prices or telemetry change:

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

Adding the current product’s $1.12 metadata-extraction estimate produces:

| Product-parity vector index | Estimated cost |
| --- | ---: |
| `text-embedding-3-small` plus metadata | $1.40 |
| `text-embedding-3-large` plus metadata | $2.94 |

The PageIndex indexing range is:

- 339 to 410 times the retrieval-only cost of `text-embedding-3-small`
- 68 to 82 times the cost of `text-embedding-3-small` plus shared metadata extraction
- 32 to 39 times the cost of `text-embedding-3-large` plus shared metadata extraction

The small embedding baseline creates approximately 17,086 vectors. A 1,536-dimensional float32 representation uses about 100 MiB before vector-index overhead. Storage and database compute are infrastructure costs, not token costs.

## How the current system spends tokens while answering

A document-content request can invoke five DeepSeek stages before returning a grounded answer. A second retrieval round adds two more calls.

The stages are:

1. Route the request and resolve conversation history
2. Select up to eight documents from as many as 50 candidates
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

### Measured document-answer cost

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

The earlier application telemetry observed a 26.96% input cache-hit rate. Applying that rate would reduce the estimate to about $0.0049. The current telemetry discards cache buckets, so $0.00613 is the auditable cache-miss estimate rather than the exact billed amount.

### Measured costs for other outcomes

The local query history contains 57 runs. The following values use generation traces associated with each query run and exclude PageIndex ingestion.

| Outcome | Traces | Average calls | Average input | Average output | Cache-miss cost |
| --- | ---: | ---: | ---: | ---: | ---: |
| Sufficient document answer | 3 | 5.00 | 10,738 | 1,678 | $0.00613 |
| Insufficient document answer | 4 | 1.00 | 716 | 196 | $0.00048 |
| Successful SQL route | 30 | 3.13 | 1,903 | 2,237 | $0.00277 |
| Clarification route | 9 | 1.00 | 880 | 194 | $0.00055 |
| Help route | 2 | 0 | 0 | 0 | $0 |
| Errored document route | 1 | 4.00 | 59,486 | 7,269 | $0.03220 |

Structured Query Language (SQL) routes answer catalog and ingestion-status questions from PostgreSQL metadata. They do not retrieve document pages.

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

Confidence: moderate-low. The 50-candidate payload size follows directly from the code and current ten-candidate traces. The projection assumes that later stages select the same number of documents and pages. Selecting more documents, taking a second retrieval round, or approaching the 40,000-token evidence limit increases cost.

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

The measured pilot PageIndex path costs less than the vector baseline only when vector synthesis needs more than about 12,700 input tokens after preserving the same route and output costs. The projected full-corpus PageIndex path reaches parity only when vector synthesis needs about 24,800 input tokens.

A hosted LLM reranker adds another model charge. A local cross-encoder reranker adds compute and latency but no provider token charge. Neither reranker is included in the baseline.

## Query-volume comparison

The volume table applies the measured pilot cost, the projected 50-candidate cost, and the eight-chunk vector baseline.

| Successful content answers | Pilot PageIndex | Projected full-corpus PageIndex | Vector RAG with route | Query embeddings alone |
| ---: | ---: | ---: | ---: | ---: |
| 1,000 | $6.13 | $11.40 | $3.60 | $0.0005 |
| 10,000 | $61.31 | $114.00 | $36.03 | $0.005 |
| 100,000 | $613.08 | $1,140.00 | $360.25 | $0.05 |
| 1,000,000 | $6,130.76 | $11,400.00 | $3,602.53 | $0.50 |

No token-cost break-even emerges from higher query volume under these assumptions. PageIndex has both a higher initial indexing cost and a higher per-answer cost.

## Reindexing and retry costs

Index invalidation changes long-term cost even when the first index fits the budget.

The PageIndex artifact key includes the PDF hash, PageIndex version, metadata prompt version, and PageIndex model. See [`artifact_version_key`](../apps/api/src/vectorless_rag/pageindex_adapter.py#L250-L254) and its use in [`IngestionProcessor.process`](../apps/api/src/vectorless_rag/ingestion.py#L280-L304).

Changing any of these values creates a new artifact version and can trigger the full PageIndex cost:

- PDF contents
- PageIndex version
- Metadata prompt version
- PageIndex model

A conventional vector index requires re-embedding when the PDF, chunking policy, or embedding model changes. Answer prompts and answer-model changes do not require re-embedding.

Both systems can update only changed documents when their index configuration remains stable. PageIndex retries cost more because an unsuccessful attempt can consume multiple generation calls before failing.

## Why `make pilot-report` reports zero tokens

The current pilot report cannot support a full-corpus cost decision. It reports zero tokens while Langfuse contains PageIndex and metadata generations.

Four defects cause the result:

1. Completed ingestion metrics store page count, extraction quality, elapsed time, and artifact bytes, but no token fields. See [`IngestionProcessor.process`](../apps/api/src/vectorless_rag/ingestion.py#L327-L333).
2. `build_pilot_report` reads missing `input_tokens` and `output_tokens` fields with a zero default. See [`build_pilot_report`](../apps/api/src/vectorless_rag/ingestion.py#L369-L404).
3. The report sums only completed jobs, excluding model tokens spent by failed attempts.
4. The CLI selects the first ten job rows rather than all attempts and the latest state of ten documents. See [`pilot_report`](../apps/api/src/vectorless_rag/cli.py#L59-L81).

The live database contains 15 ingestion job rows: 10 completed rows and 5 failed rows. `make pilot-report` selects the first ten rows and reports 7 completed and 3 failed, even though all ten pilot documents eventually completed.

The report also projects by document count:

```text
projection_factor = 537 / completed_documents
```

This method ignores page count even though the pilot documents average 12.2 pages and the corpus averages 22.5 pages.

### Missing cache information

The custom observability layer normalizes usage to only `input`, `output`, and `total`. See [`normalized_token_usage`](../apps/api/src/vectorless_rag/observability.py#L55-L85).

It discards provider fields needed for exact billing:

- Cache-hit input tokens
- Cache-miss input tokens
- Reasoning output tokens
- Provider-specific usage subcategories

Older automatic observations retained those fields. They measured a 1.21% PageIndex input cache-hit rate and a 26.96% application-call cache-hit rate. New observations cannot reproduce those calculations.

## Changes required before full-corpus approval

The cost gate needs job-level accounting and a page-aware projection before it can protect against an unintended ingestion run.

Required changes:

1. Record metadata and PageIndex usage separately for every ingestion attempt
2. Preserve cache-hit input, cache-miss input, output, and reasoning token buckets
3. Associate PageIndex child-process generations with the ingestion job
4. Include failed attempts and successful retries in actual spend
5. Report final document state separately from job-attempt counts
6. Project token volume by page count and page-count bands
7. Apply current provider prices to cache-hit input, cache-miss input, and output separately
8. Report measured spend and projected spend without mixing them

The next pilot should sample the corpus by page-count band rather than filename order:

- Documents near the 10th percentile, about 10 pages
- Documents near the median, about 19 pages
- Documents near the 90th percentile, about 37 pages
- Several documents above 75 pages
- Documents with and without a detected table of contents

A 20 to 30 document stratified pilot will constrain the projection better than the current ten shorter documents.

## Decision guidance

Choose conventional vector RAG when retrieval quality is comparable. It reduces initial provider cost, per-query model calls, latency, retry exposure, and the number of changes that force reindexing.

Keep PageIndex when evaluation shows enough improvement on the target questions. Candidate workloads include questions that require cross-section navigation, evidence from distant pages, document-structure reasoning, or comparisons whose relevant passages share little vocabulary.

A cost-controlled hybrid can use vector retrieval first and invoke PageIndex only when:

- Vector retrieval confidence falls below an evaluated threshold
- The question explicitly asks for cross-section or whole-document reasoning
- The first answer fails citation or sufficiency checks
- An offline evaluation identifies a question class where PageIndex wins

Do not use the current zero-token pilot report to approve full-corpus indexing. At current prices, reserve approximately $115 for the initial PageIndex run, plus retry margin, until corrected telemetry establishes a tighter figure.

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
| Successful pilot PageIndex token counts | High | Provider-reported live trace usage |
| $94 to $115 full-corpus PageIndex projection | Moderate | Nine complete traces; pilot is shorter than corpus |
| $0.28 small-model vector index | Moderate-high | Exact corpus tokens plus stated chunk assumptions |
| $0.00613 pilot document-answer cost | High for the three measured successes | Trace totals and current list prices |
| Approximately $0.0114 full-corpus document-answer cost | Moderate-low | Candidate payload extrapolation; later stages unmeasured at full scale |
| Vector RAG answer comparison | Moderate | Explicit chunk, route, and output assumptions |
