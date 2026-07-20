<!-- Content type: How-to -->

# Run evaluation workflows

Use this page to build or seed the deterministic release dataset and choose between mocked and live frontend verification. The dataset lives at `apps/api/evaluation/release-v1.jsonl`; its builders are under `apps/api/scripts`.

```bash
make evaluation-build
make evaluation-seed DATASET=apps/api/evaluation/release-v1.jsonl
```

The seed command streams the dataset into the API container so local credentials are not copied into files. Keep grounded-answer, route-selection, citation-resolution, SQL-policy, insufficiency, latency, and cost evaluations separate from the deterministic frontend suite.

`make playwright` starts a local mock upstream while retaining the real TanStack UI, server functions/routes, encrypted cookie, authorization gates, multipart proxy, and SSE parser. The mock deliberately fragments SSE and covers stable browser states without model spend. `make playwright-live` is the low-volume authorized integration path described in the operator guide.
