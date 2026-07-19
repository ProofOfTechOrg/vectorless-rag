# Evaluation and release gates

Seed Langfuse with at least 85 labeled cases: 20 single-document, 15 cross-document, 15 SQL,
15 hybrid, 10 unanswerable and 10 prompt-injection/adversarial cases. Case records must include
the expected route, document/page locators or SQL properties, and whether refusal is required.
`evaluation/release-v1.jsonl` contains these 85 deterministic cases, built from the local corpus
by `scripts/build_evaluation_dataset.py`. `scripts/seed_evaluation.py` validates category counts
before uploading it to Langfuse. Page expectations should be labeled after the ten-paper pilot.

Release requires all SQL attack tests to pass, every citation to resolve, route accuracy >= 90%,
SQL answer accuracy >= 95%, pilot citation validity >= 90%, and zero unsupported claims in
unanswerable cases. Store automated scores for route correctness, retrieval sufficiency, citation
validity, SQL safety, groundedness and request success.
