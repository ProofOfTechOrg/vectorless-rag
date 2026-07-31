<!-- Content type: Conceptual -->
<!-- Content plan: Goal: decide which retrieval architecture fits this corpus. Audience: engineers and operators. Content: implementation, quality, limits, measurements, costs, decision, and evaluation. Open question: measured vector-versus-PageIndex answer quality. -->

# Compare vector RAG with the PageIndex system

> **Dated design record, July 2026:** This provisional comparison predates hierarchical PageIndex v2, complete-catalog retrieval, frozen full-corpus indexes, and a sealed post-freeze evaluation dataset. Keep the vr3 measurements as historical evidence, but do not use them as a final architecture verdict.

The later [PageIndex v2 evaluation](pageindex-evaluation-handoff.md) measured routing and PageIndex retrieval but did not add a vector arm. It therefore does not change this record’s central conclusion: a final architecture verdict still requires a sealed vector-versus-PageIndex comparison.

This analysis compares conventional embedding and vector-search retrieval-augmented generation (RAG) with this repository’s PageIndex-based vectorless RAG implementation. It covers the implemented retrieval path, indexing lifecycle, retrieval quality, citations, latency, provider cost, scaling, failure modes, and the evidence required for a final product decision.

## Analysis baseline

The code and measurements use a fixed repository and corpus baseline so later changes do not silently alter the conclusion.

| Item | Baseline |
| --- | --- |
| Repository branch | `master` |
| Repository commit | `221ca6509e498af3d64e8296179b4e75a209aef1` |
| Analysis date | July 25, 2026 |
| Corpus | 537 local PDFs under `arxiv-pdfs` |
| Corpus pages | 12,086 |
| Extracted corpus tokens | 11,960,455 with `cl100k_base` |
| Generation model | `deepseek-v4-pro` |
| PageIndex model | `deepseek/deepseek-v4-pro` |
| PageIndex version | `190f8b378be58199ca993566a9214dba72089c54+vr3` |
| Vector baseline | OpenAI `text-embedding-3-small` |
| Vector chunking baseline | 800 tokens with 100-token overlap |
| Vector answer context | Eight 800-token chunks |

The live measurements came from `make pilot-report`, `make query-cost-report DAYS=30`, and aggregate SQL queries over ingestion and query latency. These commands did not read prompts, answers, document names, API keys, or identities.

## Provisional decision

Do not make a final architecture selection from this report. The historical evidence favored conventional hybrid vector RAG, but it measured the flattened `vr3` arm rather than hierarchical v2. Treat that recommendation as the hypothesis the frozen comparison must test.

The strongest case for PageIndex is long-document retrieval. A structure-aware index can find evidence across distant sections when the question and answer use different vocabulary. It can also select coherent page ranges instead of isolated fixed-size chunks.

The measured `vr3` implementation did not preserve the full PageIndex advantage. PostgreSQL lexical search defined the corpus-level candidate set, PageIndex operated only after document selection, and artifact normalization flattened the node hierarchy. Those defects are fixed in v2, but no paid v2 pilot or frozen vector comparison has been run yet.

Do not approve PageIndex indexing for all 537 PDFs yet. The current cost report also blocks approval because the pilot contains no documents above the corpus’s 75th page-count percentile.

## How conventional vector RAG works

Conventional vector RAG converts document passages and questions into dense numeric embeddings. Nearest-neighbor search retrieves passages whose semantic representations are close to the question.

An effective baseline for this corpus would use the following path:

```text
PDF
  → extract page and section text
  → create overlapping section-aware chunks
  → embed each chunk once
  → store embedding, document ID, section, and page range

Question
  → embed the question
  → combine dense similarity with PostgreSQL full-text search
  → apply metadata filters
  → fuse and optionally rerank the candidates
  → send selected chunks to grounded answer synthesis
```

The vector index does not need a separate database at this scale. PostgreSQL with `pgvector` can hold approximately 17,086 baseline chunks alongside the current catalog and full-text index.

A strong comparison must use hybrid retrieval rather than dense similarity alone. Hybrid retrieval retains exact-term and identifier matching while adding semantic recall. Section-aware chunks, neighbor expansion, and page metadata address the main weaknesses of naive fixed-chunk vector RAG.

## How the historical vr3 PageIndex system worked

The measured vr3 system combined metadata routing, lexical corpus search, PageIndex page selection, guarded SQL, and grounded synthesis.

### Indexing path

Ingestion performs these steps for each new PDF:

1. Validate the file type, size, PDF signature, and extractable text
2. Extract text from the first three pages
3. Ask DeepSeek for title, authors, abstract, topics, date, and description
4. Run PageIndex over the complete multi-page PDF
5. Normalize the generated structure into node summaries and page ranges
6. Store the normalized tree and complete extracted page text in MinIO
7. Store catalog metadata and the active artifact version in PostgreSQL

PageIndex does not perform one generation per PDF. It invokes the model repeatedly to detect or generate structure, map headings to pages, validate or repair mappings, summarize nodes, and generate a document description.

The successful indexed-document sample used 845 metadata and PageIndex model calls across 10 PDFs, or 84.5 calls per PDF. Call count varies with page count and document structure.

### Query path

A document-content request can use five model stages before it returns an answer:

1. Resolve conversation context and select a route
2. Select up to eight documents from a maximum of 50 candidates
3. Select PageIndex nodes whose page ranges may answer the question
4. Decide whether the retrieved evidence is sufficient
5. Synthesize an answer with evidence labels

If the first evidence set is insufficient, a second PageIndex node-selection and sufficiency round adds two calls. Each structured generation can retry up to three times.

SQL routes use guarded PostgreSQL views for catalog counts, lists, metadata, and ingestion status. Hybrid routes use SQL to identify documents before PageIndex retrieval. Static capability requests bypass the model.

## Indexing does not run for every query

PageIndex generation occurs during ingestion, not ordinary question answering. Queries load the saved artifact from MinIO and select nodes from that artifact.

The system reuses an artifact when its version key exists and the job does not request a forced rebuild. The version key includes:

- PDF SHA-256 digest
- PageIndex version
- Metadata prompt version
- PageIndex model

Changing any value creates a new artifact version and can trigger the complete PageIndex indexing cost. Forced ingestion rebuilds an existing version.

The ingestion processor performs metadata extraction before it checks for a reusable PageIndex artifact. A queued ingestion job can therefore pay for a metadata call even when it reuses the saved tree. Duplicate upload and corpus-sync requests do not create that job unless the caller forces ingestion.

A clearer comparison of the indexing paths is:

| System | Indexing work |
| --- | --- |
| Current PageIndex implementation | One artifact per document version, built with an average of 84.5 measured model calls per successful pilot PDF |
| Conventional vector RAG | One batched embedding pass over the chunks for each document and embedding configuration |

## Historical vr3 corpus-level retrieval quality

The largest vr3 quality risk occurred before PageIndex saw a document. PostgreSQL full-text search selected at most 50 ready documents from title, abstract, description, topics, and ArXiv ID.

The candidate query first uses an English `websearch_to_tsquery`. When fewer than 50 documents match, it ranks remaining documents using an OR query over up to 24 question tokens. It fills any remaining slots by rank and creation time.

DeepSeek receives each candidate’s title, truncated abstract, topics, and full-text-match flag. It selects at most eight documents. It cannot recover a relevant document that PostgreSQL ranked below the 50-document boundary.

This design has predictable consequences:

- Exact titles, ArXiv IDs, known authors, and shared terminology work well
- Metadata constraints reduce the candidate space effectively
- Conceptual questions can miss papers whose metadata uses different vocabulary
- The risk increases as the corpus grows beyond 537 documents
- PageIndex improves retrieval only inside the selected documents

Dense retrieval addresses the vocabulary mismatch at corpus level. A hybrid full-text and dense index would preserve the current exact matching while adding semantic candidate recall.

## Historical vr3 in-document retrieval quality

PageIndex can outperform fixed-size chunk retrieval when a question requires document navigation. Its node titles, summaries, and page ranges expose sections that a dense top-k search can miss.

The vr3 adapter weakened that structural signal in three ways:

1. `_walk_nodes` recursively flattens the PageIndex hierarchy into one list
2. Retrieval omits the artifact’s generated document description
3. Evidence fetching does not merge overlapping node page ranges

The node-selection model receives titles, summaries, page ranges, and node IDs, but it does not receive parent IDs, depth, or child relationships. Selecting an overlapping parent and child can duplicate page text and consume the 24-page and 40,000-token evidence limits.

A strong vector baseline can recover much of PageIndex’s benefit without generation-based indexing:

- Split on detected headings before applying token limits
- Store section titles and hierarchical paths with each chunk
- Retrieve parent sections or neighboring chunks around a matched passage
- Fuse lexical and dense scores
- Remove overlapping passages with maximal marginal relevance or explicit range merging
- Rerank the final candidate set

PageIndex may still win on questions that require distant sections, whole-document organization, or low lexical overlap. The repository has not measured that gain.

## Citation quality

Both designs can produce page citations. Citation support depends on retained page metadata, not on embeddings.

The current system has strong mechanical citation controls:

- Evidence records include document ID, node ID, and page range
- Synthesis must use evidence labels
- Unknown labels and markers fail closed
- Successful document answers require at least one resolvable citation
- Retrieval audits store selected documents, locators, evidence size, and evidence hash

These controls prove that a citation resolves to retrieved evidence. They do not prove that every cited page supports every claim.

A vector implementation can retain the same controls by storing page spans on every chunk and preserving the existing synthesis and citation-validation code. The A/B evaluation must measure claim-level citation precision and recall, not citation presence alone.

## Historical vr3 retrieval limits

The current limits bound response size and model context, but they can also suppress relevant evidence.

| Limit | Value |
| --- | ---: |
| Candidate documents | 50 |
| Selected documents | 8 |
| Page ranges per document | 4 |
| Fetched pages | 24 |
| Evidence tokens | 40,000 |
| Retrieval rounds | 2 |
| Structured-output attempts per call | 3 |
| Request deadline | 120s |

PageIndex v2 removes the 50-document exclusion and reasons over every eligible catalog entry in deterministic 24,000-token batches. It retains at most eight selected documents, expands the nested tree for up to three rounds, and keeps the public 40,000-token evidence ceiling. Evaluation overrides are 6,400, 12,800, and 25,600 tokens.

The 40,000-token evidence limit is not a billing limit. The system can send the evidence to sufficiency evaluation twice and then send it again for synthesis. A structured-output retry can repeat any failed request.

## Measured ingestion behavior

The repaired live pilot report contains complete final states and partial historical retry telemetry.

| Metric | Measured value |
| --- | ---: |
| Ready documents | 10 of 10 |
| Recorded ingestion attempts | 17 |
| Completed attempts | 10 |
| Failed attempts | 7 |
| Metadata and PageIndex calls | 978 |
| Completed model calls | 955 |
| Failed model calls | 23 |
| Calls with reported usage | 974 |
| Input tokens | 1,804,204 |
| Output tokens | 126,062 |
| Total tokens | 1,930,266 |
| Known provider cost | $0.767451 |

Fifteen older jobs predate durable attempt accounting, so the report cannot reconstruct every retry. The current gate labels retry-adjusted projection ineligible because retry telemetry remains incomplete.

Successful ingestion attempts had the following elapsed times:

| Statistic | Successful attempts |
| --- | ---: |
| Attempts | 10 |
| Mean | 68.1s |
| Median | 63.2s |
| 95th percentile | 101.3s |
| Maximum | 103.1s |

The seven failed attempts averaged 33.1s and reached 63.3s. A vector index would replace these generation chains with local parsing and batched embedding requests. Vector indexing latency has not been measured in this repository.

## Measured query latency

Historical query rows expose end-to-end latency but do not contain durable per-stage model-call records.

| Route and result | Runs | Mean | Median | 95th percentile | Maximum |
| --- | ---: | ---: | ---: | ---: | ---: |
| Document, completed | 7 | 14.0s | 6.5s | 28.0s | 28.4s |
| Document, failed | 1 | 120.0s | 120.0s | 120.0s | 120.0s |
| Hybrid, completed | 1 | 22.4s | 22.4s | 22.4s | 22.4s |
| SQL, completed | 30 | 35.2s | 33.5s | 68.7s | 74.7s |

Confidence is low for document and hybrid latency because the samples contain seven and one successful runs. The single document failure reached the configured request deadline.

Vector retrieval should reduce document-route latency because it removes document-selection, node-selection, and sufficiency generations. This is an architectural inference, not a measured result. A vector A/B must record the same end-to-end percentiles.

## Provider cost comparison

Current official list prices match the application’s configured pricing:

| Provider surface | Price per 1 million tokens |
| --- | ---: |
| DeepSeek V4 Pro cache-hit input | $0.003625 |
| DeepSeek V4 Pro cache-miss input | $0.435 |
| DeepSeek V4 Pro output | $0.87 |
| OpenAI `text-embedding-3-small` input | $0.02 |
| OpenAI `text-embedding-3-large` input | $0.13 |

See [DeepSeek models and pricing](https://api-docs.deepseek.com/quick_start/pricing) and the [OpenAI `text-embedding-3-small` model page](https://developers.openai.com/api/docs/models/text-embedding-3-small). Provider prices can change, so recalculate the dollar totals before a corpus-wide run.

### PageIndex indexing projection

The live report uses 10 successful artifact builds covering 122 pages. Eight sample documents fall at or below the corpus’s 25th page-count percentile. No sample document falls above the 75th percentile.

| Projection method | Projected tokens | Projected cost | Confidence |
| --- | ---: | ---: | --- |
| Observed successful-page ratio | 160,342,187 | $69.38 | Low |
| Linear page-count regression | 179,719,160 | $76.37 | Low |

The earlier trace analysis projected $94 to $115 from nine complete PageIndex traces. It used a different PageIndex version and observed output-token profile. Until a representative pilot includes long documents and complete retries, reserve $115 plus retry margin rather than treating $69 as an approval budget.

### Vector indexing projection

The corpus contains 11,960,455 extracted tokens. With 800-token chunks, 100-token overlap, and 20 repeated metadata tokens per chunk, the baseline produces approximately:

| Vector metric | Projection |
| --- | ---: |
| Embedded tokens | 14,010,819 |
| Chunks | 17,086 |
| `text-embedding-3-small` provider cost | $0.2802 |
| Shared LLM metadata extraction | Approximately $0.8234 |
| Product-parity vector indexing | Approximately $1.10 |

The current PageIndex projection costs 248 to 273 times the embedding-only baseline. It costs 63 to 69 times the product-parity vector index.

A 1,536-dimensional float32 embedding for 17,086 chunks requires approximately 100&nbsp;MiB before index overhead. That storage cost does not change the provider-cost conclusion.

### Query cost projection

Historical traces predate durable query-call accounting. The prior trace analysis measured three sufficient document answers at five calls each.

| Successful document answer | Provider cost |
| --- | ---: |
| PageIndex path, measured 10-document pilot | $0.00613 |
| PageIndex path, projected 50-candidate corpus | Approximately $0.0114 |
| Vector path with route call and eight chunks | $0.00360 |
| 25-token query embedding | $0.0000005 |

The measured pilot PageIndex answer costs 1.7 times the vector baseline. The full-candidate projection costs 3.2 times the vector baseline.

The pre-v2 57-run cohort cannot validate those values because its query calls predate durable model-call accounting; the migration deliberately does not invent or backfill usage. New runs write durable call rows. Reports must keep the historical cohort marked legacy/incomplete and may assign zero model cost only to static help responses that made no model call.

## Scaling and operations

Vector retrieval has the stronger scaling model for this application. Approximate nearest-neighbor search can rank the complete corpus without sending 50 metadata records to a generation model.

The historical vr3 PageIndex path had two separate scaling limits:

- Corpus search cannot retrieve a document ranked below the 50-document lexical boundary
- Query cost grows when the selection model receives more candidate metadata

The v2 implementation removes the lexical exclusion and uses complete-catalog, token-bounded map/reduce selection before nested tree search. Query cost still grows with catalog size, but in explicit catalog batches rather than a fixed-recall gate. This document remains provisional until the frozen 25-document v2 pilot is accepted and both comparison arms are frozen against a new sealed evaluation dataset.

Vector indexing also has a smaller retry surface. An embedding pipeline can batch chunks, retry failed batches independently, and persist successful batches. PageIndex can spend dozens of model calls before a late validation or provider error invalidates an attempt.

Both designs can update changed documents without rebuilding the full corpus. PageIndex must rebuild when the PDF, PageIndex version, metadata prompt version, or PageIndex model changes. Vector RAG must re-embed when the PDF, chunking policy, or embedding model changes. Answer-prompt and answer-model changes do not require re-embedding.

## Security and data handling

Both designs can preserve the application’s existing authorization, object storage, audit, citation, and browser-session boundaries.

The current PageIndex indexing path sends document text to DeepSeek repeatedly while it builds each tree. Query-time PageIndex calls send tree summaries and selected page evidence. A hosted embedding baseline sends each chunk once during indexing, while a local embedding model sends no indexing text to an external provider.

Both paths send retrieved evidence to the answer model unless answer generation also runs locally. The security decision should compare provider retention, training, residency, and contractual controls separately from retrieval architecture.

## Evaluation status

The repository contains 85 release evaluation inputs:

| Category | Cases |
| --- | ---: |
| Single-document | 20 |
| Cross-document | 15 |
| Hybrid | 15 |
| SQL | 15 |
| Unanswerable | 10 |
| Adversarial | 10 |

The dataset records expected route, document IDs, citation requirement, SQL-audit requirement, and refusal requirement. It does not contain reference answers or ground-truth evidence page spans.

No completed experiment compares PageIndex with vector retrieval. PageIndex quality claims for this application therefore have unknown confidence.

## Required A/B evaluation

The evaluation must hold routing, synthesis, answer model, prompts, citation validation, and corpus state constant. Only the retrieval implementation should change.

Add reference answers and ground-truth page spans to the document, hybrid, and unanswerable cases. Run each question through:

1. Current lexical selection and PageIndex retrieval
2. Hybrid PostgreSQL full-text and dense retrieval
3. Hybrid retrieval with a reranker

Measure:

- Document recall at 50 and at the final selected-document count
- Ground-truth page recall at the final context size
- Claim-level answer correctness
- Claim-level citation precision and recall
- Correct abstention on unanswerable questions
- Input and output tokens by stage
- Provider cost per completed and failed request
- End-to-end median and 95th-percentile latency
- Provider and structured-output failure rates

Keep PageIndex for a question class only when it produces a repeatable quality improvement that pays for its additional indexing, latency, and failure exposure. Do not infer that improvement from PageIndex vendor benchmarks or citation presence.

## Recommended implementation

The vector baseline should reuse the current application boundaries instead of replacing the complete system.

1. Keep PDF validation, PyMuPDF extraction, MinIO, PostgreSQL metadata, SQL routes, authentication, audit records, synthesis, and citation validation
2. Create section-aware chunks with page spans, approximately 700 to 900 tokens and modest overlap
3. Store embeddings and chunk metadata in PostgreSQL with `pgvector`
4. Combine dense similarity with the existing GIN full-text index and metadata constraints
5. Fuse rankings and deduplicate overlapping passages
6. Rerank the strongest candidates when evaluation shows a gain
7. Send 8 to 12 chunks through the existing grounded synthesis stage
8. Build and cache PageIndex only for evaluated fallback cases

Fallback conditions can include:

- Vector retrieval confidence below an evaluated threshold
- Explicit whole-document or cross-section questions
- A first pass that cannot produce supported citations
- A question class where the A/B evaluation shows a PageIndex advantage

Lazy PageIndex avoids corpus-wide tree-generation cost, but the first request for an unindexed document can incur the measured 63.2-second median build time. Build trees asynchronously for selected high-value documents when that delay is unacceptable.

## Quantitative cost appendix

[`token-cost-analysis.md`](token-cost-analysis.md) is the quantitative appendix for this decision. It contains the corpus measurements, pricing baseline, token formulas, current `vr3` ingestion accounting, historical `vr2` sensitivity, query-volume projections, exclusions, and confidence levels.

Use this document for the architecture decision. Use the appendix to recalculate provider costs when prices, token usage, chunking assumptions, or corpus size change.

## Reproduce the current reports

Run the aggregate application reports from the repository root:

```bash
make pilot-report
make query-cost-report DAYS=30
```

Run the latency aggregates against the local Compose PostgreSQL service. The first query measures ingestion attempts:

```sql
SELECT status, count(*), avg(elapsed_seconds),
       percentile_cont(0.5) WITHIN GROUP (ORDER BY elapsed_seconds),
       percentile_cont(0.95) WITHIN GROUP (ORDER BY elapsed_seconds),
       max(elapsed_seconds)
FROM agent.ingestion_attempts
WHERE elapsed_seconds IS NOT NULL
GROUP BY status;
```

The second query measures end-to-end query runs:

```sql
SELECT
  COALESCE(route::text, 'unselected') AS route,
  CASE
    WHEN failure_code IS NOT NULL OR error IS NOT NULL THEN 'failed'
    WHEN answer IS NOT NULL THEN 'completed'
    ELSE 'running'
  END AS outcome,
  count(*),
  avg(latency_ms),
  percentile_cont(0.5) WITHIN GROUP (ORDER BY latency_ms),
  percentile_cont(0.95) WITHIN GROUP (ORDER BY latency_ms),
  max(latency_ms)
FROM agent.query_runs
WHERE latency_ms IS NOT NULL
GROUP BY route, outcome;
```

Recalculate provider costs from token counts and current list prices before authorizing a large indexing run.
