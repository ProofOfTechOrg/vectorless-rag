from __future__ import annotations

import asyncio
import base64
import json
import time
import uuid
from collections.abc import AsyncGenerator, AsyncIterator
from contextlib import AsyncExitStack, asynccontextmanager
from dataclasses import dataclass
from datetime import datetime
from typing import Annotated, Any

from fastapi import Depends, FastAPI, File, HTTPException, Query, Request, UploadFile, status
from fastapi.responses import Response
from langgraph.checkpoint.postgres.aio import AsyncPostgresSaver
from sqlalchemy import and_, desc, func, or_, select
from sqlalchemy.ext.asyncio import AsyncEngine, AsyncSession
from sse_starlette.sse import EventSourceResponse

from vectorless_rag import __version__
from vectorless_rag.auth import (
    AdminPrincipal,
    AnyPrincipal,
    ChatPrincipal,
    Principal,
    ReadPrincipal,
    get_db_session,
)
from vectorless_rag.config import Settings, get_settings
from vectorless_rag.db import Database, make_engine
from vectorless_rag.graph import GraphRunner
from vectorless_rag.ingestion import (
    IngestionRetryConflict,
    UnsupportedPDF,
    register_pdf,
    retry_failed_ingestion,
    sync_corpus,
)
from vectorless_rag.llm import DeepSeekLLM
from vectorless_rag.models import Document, IngestionJob, QueryRun, RunFeedback, RunRoute
from vectorless_rag.observability import Observability
from vectorless_rag.retrieval import CorpusRouter, PageIndexRetriever, VectorlessRetriever
from vectorless_rag.schemas import (
    ChatRequest,
    ChatResponse,
    Citation,
    DocumentPage,
    DocumentSummary,
    FeedbackRequest,
    IdentityResponse,
    JobResponse,
    SyncRequest,
    SyncResponse,
    ThreadPage,
    ThreadResponse,
    ThreadRun,
    ThreadSummary,
)
from vectorless_rag.sql_guard import PostgreSQLAdapter
from vectorless_rag.storage import S3ObjectStore


@dataclass
class Runtime:
    settings: Settings
    database: Database
    sql_engine: AsyncEngine
    store: S3ObjectStore
    llm: DeepSeekLLM | None
    observability: Observability
    checkpointer: Any


def _checkpoint_url(url: str) -> str:
    return url.replace("postgresql+psycopg://", "postgresql://", 1)


@asynccontextmanager
async def lifespan(application: FastAPI) -> AsyncGenerator[None, None]:
    settings = get_settings()
    database = Database(settings)
    sql_engine = make_engine(settings.sql_database_url, pool_size=5)
    store = S3ObjectStore(settings)
    observability = Observability(settings)
    llm = DeepSeekLLM(settings, observability) if settings.deepseek_api_key else None
    async with AsyncExitStack() as stack:
        checkpointer_context = AsyncPostgresSaver.from_conn_string(
            _checkpoint_url(settings.database_url)
        )
        checkpointer = await stack.enter_async_context(checkpointer_context)
        await checkpointer.setup()
        await store.ensure_bucket()
        application.state.runtime = Runtime(
            settings, database, sql_engine, store, llm, observability, checkpointer
        )
        yield
        observability.flush()
        await sql_engine.dispose()
        await database.close()


app = FastAPI(
    title="Vectorless ArXiv RAG",
    version=__version__,
    description="Reasoning-based RAG API for the full-stack operator console.",
    lifespan=lifespan,
)


async def _session_dependency(request: Request) -> AsyncIterator[AsyncSession]:
    runtime: Runtime = request.app.state.runtime
    async with runtime.database.session() as session:
        yield session


app.dependency_overrides[get_db_session] = _session_dependency
Session = Annotated[AsyncSession, Depends(get_db_session)]


@app.get("/health/live", tags=["health"])
async def live() -> dict[str, str]:
    return {"status": "ok"}


@app.get("/health/ready", tags=["health"])
async def ready(request: Request) -> dict[str, str]:
    runtime: Runtime = request.app.state.runtime
    async with runtime.database.engine.connect() as connection:
        await connection.exec_driver_sql("SELECT 1")
    return {"status": "ready"}


@app.get("/v1/auth/me", response_model=IdentityResponse, tags=["auth"])
async def identity(principal: AnyPrincipal) -> IdentityResponse:
    """Return the minimum browser bootstrap identity for any valid API key."""
    return IdentityResponse(name=principal.name, scopes=sorted(principal.scopes))


def _runner(runtime: Runtime) -> GraphRunner:
    if runtime.llm is None:
        raise HTTPException(status.HTTP_503_SERVICE_UNAVAILABLE, "DeepSeek is not configured")
    router = CorpusRouter(runtime.settings, runtime.llm)
    pageindex = PageIndexRetriever(runtime.settings, runtime.store, runtime.llm)
    retriever = VectorlessRetriever(runtime.settings, router, pageindex)
    return GraphRunner(
        runtime.settings,
        runtime.llm,
        retriever,
        PostgreSQLAdapter(runtime.sql_engine, runtime.settings),
        runtime.observability,
        runtime.checkpointer,
    )


async def _execute_chat(
    request: Request,
    payload: ChatRequest,
    principal: Principal,
    session: AsyncSession,
    *,
    thread_id: str | None = None,
    trace_id: str | None = None,
) -> ChatResponse:
    runtime: Runtime = request.app.state.runtime
    thread_id = thread_id or payload.thread_id or uuid.uuid4().hex
    trace_id = trace_id or uuid.uuid4().hex
    run = QueryRun(
        trace_id=trace_id,
        thread_id=thread_id,
        api_key_id=principal.api_key_id,
        question=payload.message,
    )
    session.add(run)
    await session.flush()
    started = time.monotonic()
    try:
        async with asyncio.timeout(runtime.settings.request_deadline_seconds):
            result = await _runner(runtime).run(session, payload, run)
    except TimeoutError as exc:
        run.error = "request deadline exceeded"
        run.insufficient = True
        run.latency_ms = round((time.monotonic() - started) * 1000)
        raise HTTPException(status.HTTP_504_GATEWAY_TIMEOUT, run.error) from exc
    except asyncio.CancelledError:
        run.error = "client cancelled request"
        run.insufficient = True
        raise
    except Exception as exc:
        run.error = str(exc)[:1000]
        run.insufficient = True
        run.latency_ms = round((time.monotonic() - started) * 1000)
        raise
    route = RunRoute(result["route"])
    citations = [Citation.model_validate(item) for item in result.get("citations", [])]
    run.route = route
    run.answer = result["answer"]
    run.citations = [item.model_dump(mode="json") for item in citations]
    run.insufficient = result.get("insufficient", False)
    run.latency_ms = round((time.monotonic() - started) * 1000)
    sql_audit_value = result.get("sql_audit_id")
    sql_audit_id = uuid.UUID(sql_audit_value) if sql_audit_value else None
    runtime.observability.score(trace_id, "request_success", 1)
    return ChatResponse(
        answer=result["answer"],
        thread_id=thread_id,
        route=route,
        trace_id=trace_id,
        citations=citations,
        sql_audit_id=sql_audit_id,
        insufficient=run.insufficient,
    )


@app.post("/v1/chat", response_model=ChatResponse, tags=["chat"])
async def chat(
    request: Request,
    payload: ChatRequest,
    principal: ChatPrincipal,
    session: Session,
) -> ChatResponse:
    return await _execute_chat(request, payload, principal, session)


@app.post("/v1/chat/stream", tags=["chat"])
async def chat_stream(
    request: Request,
    payload: ChatRequest,
    principal: ChatPrincipal,
    session: Session,
) -> EventSourceResponse:
    thread_id = payload.thread_id or uuid.uuid4().hex
    trace_id = uuid.uuid4().hex

    async def events() -> AsyncIterator[dict[str, str]]:
        yield {
            "event": "run.started",
            "data": json.dumps({"thread_id": thread_id, "trace_id": trace_id}),
        }
        try:
            result = await _execute_chat(
                request,
                payload,
                principal,
                session,
                thread_id=thread_id,
                trace_id=trace_id,
            )
            if await request.is_disconnected():
                return
            yield {"event": "route.selected", "data": json.dumps({"route": result.route})}
            yield {
                "event": "retrieval.progress",
                "data": json.dumps({"citations": len(result.citations)}),
            }
            for start in range(0, len(result.answer), 80):
                if await request.is_disconnected():
                    return
                yield {
                    "event": "answer.delta",
                    "data": json.dumps({"delta": result.answer[start : start + 80]}),
                }
            for citation in result.citations:
                yield {"event": "citation", "data": citation.model_dump_json()}
            yield {"event": "run.completed", "data": result.model_dump_json()}
        except asyncio.CancelledError:
            raise
        except Exception as exc:
            failure_code = (
                "deadline_exceeded"
                if isinstance(exc, HTTPException)
                and exc.status_code == status.HTTP_504_GATEWAY_TIMEOUT
                else "request_failed"
            )
            message = (
                "The request timed out."
                if failure_code == "deadline_exceeded"
                else "The API could not complete this run."
            )
            yield {
                "event": "error",
                "data": json.dumps(
                    {
                        "code": failure_code,
                        "message": message,
                        "thread_id": thread_id,
                        "trace_id": trace_id,
                    }
                ),
            }

    return EventSourceResponse(events(), ping=15, send_timeout=30)


def _encode_cursor(document: Document) -> str:
    value = json.dumps([document.created_at.isoformat(), str(document.id)]).encode()
    return base64.urlsafe_b64encode(value).decode()


def _decode_cursor(cursor: str) -> tuple[datetime, uuid.UUID]:
    try:
        created, identifier = json.loads(base64.urlsafe_b64decode(cursor.encode()))
        return datetime.fromisoformat(created), uuid.UUID(identifier)
    except Exception as exc:
        raise HTTPException(status.HTTP_422_UNPROCESSABLE_ENTITY, "invalid cursor") from exc


def encode_thread_cursor(last_activity_at: datetime, thread_id: str) -> str:
    value = json.dumps([last_activity_at.isoformat(), thread_id], separators=(",", ":")).encode()
    return base64.urlsafe_b64encode(value).decode()


def decode_thread_cursor(cursor: str) -> tuple[datetime, str]:
    try:
        last_activity, thread_id = json.loads(base64.urlsafe_b64decode(cursor.encode()))
        if not isinstance(last_activity, str) or not isinstance(thread_id, str) or not thread_id:
            raise ValueError("invalid values")
        return datetime.fromisoformat(last_activity), thread_id
    except Exception as exc:
        raise HTTPException(status.HTTP_422_UNPROCESSABLE_CONTENT, "invalid cursor") from exc


def normalize_thread_title(question: str, *, limit: int = 120) -> str:
    normalized = " ".join(question.split())
    if len(normalized) <= limit:
        return normalized
    return normalized[: limit - 1].rstrip() + "…"


@app.get("/v1/documents", response_model=DocumentPage, tags=["documents"])
async def documents(
    principal: ReadPrincipal,
    session: Session,
    limit: int = Query(default=50, ge=1, le=100),
    cursor: str | None = None,
) -> DocumentPage:
    del principal
    statement = (
        select(Document).order_by(desc(Document.created_at), desc(Document.id)).limit(limit + 1)
    )
    if cursor:
        created, identifier = _decode_cursor(cursor)
        statement = statement.where(
            or_(
                Document.created_at < created,
                and_(Document.created_at == created, Document.id < identifier),
            )
        )
    rows = list((await session.scalars(statement)).all())
    has_more = len(rows) > limit
    items = rows[:limit]
    return DocumentPage(
        items=[DocumentSummary.model_validate(item) for item in items],
        next_cursor=_encode_cursor(items[-1]) if has_more else None,
    )


@app.get("/v1/documents/{document_id}", response_model=DocumentSummary, tags=["documents"])
async def document(
    document_id: uuid.UUID, principal: ReadPrincipal, session: Session
) -> DocumentSummary:
    del principal
    record = await session.get(Document, document_id)
    if record is None:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "document not found")
    return DocumentSummary.model_validate(record)


@app.post("/v1/admin/documents", response_model=JobResponse, tags=["admin"])
async def upload_document(
    request: Request,
    principal: AdminPrincipal,
    session: Session,
    file: Annotated[UploadFile, File()],
) -> JobResponse:
    del principal
    runtime: Runtime = request.app.state.runtime
    data = await file.read(runtime.settings.upload_max_bytes + 1)
    try:
        _, job, _ = await register_pdf(
            session, runtime.store, data, file.filename or "upload.pdf", runtime.settings
        )
    except UnsupportedPDF as exc:
        raise HTTPException(status.HTTP_422_UNPROCESSABLE_ENTITY, str(exc)) from exc
    if job is None:
        raise HTTPException(status.HTTP_409_CONFLICT, "document already exists")
    return JobResponse.model_validate(job)


@app.post("/v1/admin/ingestion/sync", response_model=SyncResponse, tags=["admin"])
async def synchronize(
    request: Request,
    payload: SyncRequest,
    principal: AdminPrincipal,
    session: Session,
) -> SyncResponse:
    del principal
    runtime: Runtime = request.app.state.runtime
    return await sync_corpus(session, runtime.store, runtime.settings, force=payload.force)


@app.get("/v1/admin/ingestion/{job_id}", response_model=JobResponse, tags=["admin"])
async def ingestion_job(
    job_id: uuid.UUID, principal: AdminPrincipal, session: Session
) -> JobResponse:
    del principal
    job = await session.get(IngestionJob, job_id)
    if job is None:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "ingestion job not found")
    return JobResponse.model_validate(job)


@app.post("/v1/admin/ingestion/{job_id}/retry", response_model=JobResponse, tags=["admin"])
async def retry_ingestion_job(
    job_id: uuid.UUID, principal: AdminPrincipal, session: Session
) -> JobResponse:
    del principal
    job = await session.get(IngestionJob, job_id, with_for_update=True)
    if job is None:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "ingestion job not found")
    try:
        replacement = await retry_failed_ingestion(session, job)
    except IngestionRetryConflict as error:
        raise HTTPException(status.HTTP_409_CONFLICT, str(error)) from error
    return JobResponse.model_validate(replacement)


@app.get("/v1/threads", response_model=ThreadPage, tags=["chat"])
async def threads(
    principal: ChatPrincipal,
    session: Session,
    limit: int = Query(default=25, ge=1, le=100),
    cursor: str | None = None,
) -> ThreadPage:
    first_questions = (
        select(
            QueryRun.api_key_id,
            QueryRun.thread_id,
            QueryRun.question,
            func.row_number()
            .over(
                partition_by=(QueryRun.api_key_id, QueryRun.thread_id),
                order_by=(QueryRun.created_at, QueryRun.id),
            )
            .label("position"),
        )
        .where(QueryRun.api_key_id == principal.api_key_id)
        .subquery()
    )
    grouped = (
        select(
            QueryRun.thread_id.label("thread_id"),
            first_questions.c.question.label("question"),
            func.count(QueryRun.id).label("run_count"),
            func.max(QueryRun.created_at).label("last_activity_at"),
        )
        .join(
            first_questions,
            and_(
                first_questions.c.api_key_id == QueryRun.api_key_id,
                first_questions.c.thread_id == QueryRun.thread_id,
                first_questions.c.position == 1,
            ),
        )
        .where(QueryRun.api_key_id == principal.api_key_id)
        .group_by(QueryRun.thread_id, first_questions.c.question)
        .subquery()
    )
    statement = select(grouped)
    if cursor:
        last_activity_at, thread_id = decode_thread_cursor(cursor)
        statement = statement.where(
            or_(
                grouped.c.last_activity_at < last_activity_at,
                and_(
                    grouped.c.last_activity_at == last_activity_at,
                    grouped.c.thread_id < thread_id,
                ),
            )
        )
    statement = statement.order_by(
        desc(grouped.c.last_activity_at), desc(grouped.c.thread_id)
    ).limit(limit + 1)
    rows = list((await session.execute(statement)).mappings().all())
    has_more = len(rows) > limit
    visible = rows[:limit]
    items = [
        ThreadSummary(
            thread_id=row["thread_id"],
            title=normalize_thread_title(row["question"]),
            run_count=row["run_count"],
            last_activity_at=row["last_activity_at"],
        )
        for row in visible
    ]
    return ThreadPage(
        items=items,
        next_cursor=(
            encode_thread_cursor(items[-1].last_activity_at, items[-1].thread_id)
            if has_more
            else None
        ),
    )


@app.get("/v1/threads/{thread_id}", response_model=ThreadResponse, tags=["chat"])
async def thread_history(
    thread_id: str, principal: ChatPrincipal, session: Session
) -> ThreadResponse:
    statement = (
        select(QueryRun)
        .where(QueryRun.thread_id == thread_id, QueryRun.api_key_id == principal.api_key_id)
        .order_by(QueryRun.created_at)
        .limit(200)
    )
    runs = list((await session.scalars(statement)).all())
    if not runs:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "thread not found")
    return ThreadResponse(
        thread_id=thread_id,
        runs=[
            ThreadRun(
                trace_id=run.trace_id,
                route=run.route,
                question=run.question,
                answer=run.answer,
                citations=run.citations,
                insufficient=run.insufficient,
                status=("completed" if run.error is None and run.answer is not None else "failed"),
                failure_code=(
                    None
                    if run.error is None and run.answer is not None
                    else (
                        "deadline_exceeded"
                        if run.error == "request deadline exceeded"
                        else "request_failed"
                    )
                ),
                created_at=run.created_at,
            )
            for run in runs
        ],
    )


@app.post(
    "/v1/runs/{trace_id}/feedback",
    status_code=204,
    response_class=Response,
    tags=["feedback"],
)
async def feedback(
    trace_id: str,
    payload: FeedbackRequest,
    principal: ChatPrincipal,
    session: Session,
    request: Request,
) -> Response:
    run = await session.scalar(
        select(QueryRun).where(
            QueryRun.trace_id == trace_id, QueryRun.api_key_id == principal.api_key_id
        )
    )
    if run is None:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "run not found")
    session.add(
        RunFeedback(
            trace_id=trace_id,
            api_key_id=principal.api_key_id,
            value=payload.value,
            comment=payload.comment,
        )
    )
    runtime: Runtime = request.app.state.runtime
    runtime.observability.score(trace_id, "user_feedback", payload.value, payload.comment)
    return Response(status_code=204)
