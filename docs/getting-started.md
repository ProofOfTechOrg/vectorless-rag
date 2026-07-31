<!-- Content type: Tutorial -->
<!-- Content plan: Goal: produce one grounded answer with a page citation from one local PDF. Audience: first-time local operators. Content: prerequisites, initialization, DeepSeek configuration, startup, key creation, ingestion, chat, and safe shutdown. Open questions: none. -->

# Get started with one cited answer

This tutorial starts the local stack, ingests one born-digital PDF, and asks one grounded question. It preserves service data when you stop.

## Check prerequisites

You need:

- Linux or macOS with Docker Engine and Docker Compose 2.22 or later
- GNU Make
- Python 3.12 and `uv`
- A DeepSeek API key with available credit
- One born-digital PDF whose text you may send to DeepSeek

Ingestion and chat incur provider charges. The project defaults limit registration to 25 documents and cap the frozen pilot at $10.

## Clone and initialize the repository

Clone the public repository and create `.env` with generated local secrets:

```bash
git clone https://github.com/ProofOfTechOrg/vectorless-rag.git
cd vectorless-rag
make init
```

`make init` writes `.env` with mode `600`, leaves `DEEPSEEK_API_KEY` blank, and refuses to overwrite an existing file. It never prints generated secrets.

## Configure DeepSeek

Open `.env` and assign your provider key:

```dotenv
DEEPSEEK_API_KEY=your_deepseek_api_key_here
```

Keep `DEEPSEEK_MODEL=deepseek-v4-flash` for serving and `PAGEINDEX_MODEL=deepseek/deepseek-v4-pro` for indexing. The two are priced separately, by `DEEPSEEK_*_COST_PER_MILLION_USD` and `PAGEINDEX_*_COST_PER_MILLION_USD`, because cost reports resolve each recorded call against the rates of the model that produced it. Confirm current availability and cost in the [official DeepSeek model and pricing reference](https://api-docs.deepseek.com/quick_start/pricing/).

Do not commit `.env` or copy its values into logs, issues, shell history, or browser storage.

## Start the complete stack

Start the deployment-style Compose stack:

```bash
make up
make health
```

The first start builds application images and downloads infrastructure images. `make health` succeeds after the console, API, and Langfuse respond.

The local services use these addresses:

| Service | Address |
| --- | --- |
| Operator console | [http://localhost:3001](http://localhost:3001) |
| FastAPI documentation | [http://localhost:8000/docs](http://localhost:8000/docs) |
| Langfuse | [http://localhost:3000](http://localhost:3000) |

## Mint and use a scoped key

Create a key with the scopes needed for chat and ingestion:

```bash
make bootstrap-key NAME=browser-operator SCOPES=chat,documents:read,admin:ingest
```

Copy the one-time `vrag_…` value directly to the **Connect** page. The BFF validates it through FastAPI and stores it in an encrypted HttpOnly cookie for eight hours.

## Ingest one PDF

Open **Ingestion**, choose **Upload PDF**, and select one born-digital file. Wait until the durable job reports **Succeeded**.

The worker sends document content to DeepSeek during PageIndex construction. Stop and read [troubleshooting](troubleshooting.md) if the job reports a provider wait, unsupported document, or terminal failure.

## Ask a cited question

Open **Chat** and ask a question whose answer appears in the uploaded document. A successful response streams to completion and includes a citation with the document and page number.

Open the citation to verify that its page and quoted evidence support the answer. The console links to document detail because the current release does not include an embedded PDF viewer.

## Stop without deleting data

Stop and remove containers while preserving named volumes:

```bash
make down
```

Run `make up` to use the same database and artifacts later. Do not add `-v` unless you intend to delete all local service data.
