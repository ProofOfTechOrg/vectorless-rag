import { Buffer } from 'node:buffer'
import { createServer } from 'node:http'
import { setTimeout as delay } from 'node:timers/promises'
import { URL } from 'node:url'

const documentId = '11111111-1111-4111-8111-111111111111'
const failedDocumentId = '71309511-00aa-4d06-a60b-728e1fdd3269'
const identity = { name: 'playwright-full', scopes: ['chat', 'documents:read', 'admin:ingest'] }
const documentsOnlyIdentity = { name: 'playwright-documents-only', scopes: ['documents:read'] }
const document = {
  id: documentId,
  arxiv_id: '2607.00001',
  title: 'Deterministic Vectorless Retrieval',
  authors: ['Test Operator'],
  abstract: 'A unique deterministic fixture about cobalt lantern retrieval.',
  topics: ['rag', 'testing'],
  submitted_date: '2026-07-20',
  status: 'ready',
  page_count: 2,
  diagnostic: null,
  created_at: '2026-07-20T00:00:00Z',
  updated_at: '2026-07-20T00:01:00Z',
}
const failedDocument = {
  ...document,
  id: failedDocumentId,
  arxiv_id: '1904.05234',
  title: 'Failed PageIndex Fixture',
  status: 'failed',
  diagnostic: 'The PageIndex provider request failed after all retry attempts.',
}
const citation = { source_type: 'document', source_id: documentId, title: document.title, page_start: 1, page_end: 2 }
const completed = { answer: 'The fixture confirms cobalt lantern retrieval [1].', thread_id: 'thread-1', route: 'documents', trace_id: 'trace-1', citations: [citation], sql_audit_id: null, insufficient: false }

function json(response, status, value) {
  response.writeHead(status, { 'content-type': 'application/json' })
  response.end(JSON.stringify(value))
}

const server = createServer(async (request, response) => {
  const url = new URL(request.url ?? '/', 'http://127.0.0.1:8010')
  if (url.pathname === '/health/ready') return json(response, 200, { status: 'ready' })
  const apiKey = request.headers['x-api-key']
  if (!apiKey) return json(response, 401, { detail: 'missing API key' })
  if (apiKey !== 'vrag_test_full' && apiKey !== 'mock-documents-only') return json(response, 401, { detail: 'invalid API key' })
  const requestIdentity = apiKey === 'mock-documents-only' ? documentsOnlyIdentity : identity
  if (url.pathname === '/v1/auth/me') return json(response, 200, requestIdentity)
  if (url.pathname === '/v1/threads') return json(response, 200, { items: [{ thread_id: 'thread-1', title: 'Cobalt lantern question', run_count: 1, last_activity_at: '2026-07-20T00:02:00Z' }], next_cursor: null })
  if (url.pathname === '/v1/threads/thread-1') return json(response, 200, {
    thread_id: 'thread-1',
    runs: [{ trace_id: completed.trace_id, route: completed.route, question: 'What does the cobalt lantern fixture prove?', answer: completed.answer, citations: completed.citations, insufficient: false, status: 'completed', failure_code: null, created_at: '2026-07-20T00:02:00Z' }],
  })
  if (url.pathname === '/v1/threads/thread-failed') return json(response, 200, {
    thread_id: 'thread-failed',
    runs: [{ trace_id: 'trace-failed', route: null, question: 'Make this fail', answer: null, citations: [], insufficient: true, status: 'failed', failure_code: 'request_failed', created_at: '2026-07-20T00:03:00Z' }],
  })
  if (url.pathname === '/v1/documents') return json(response, 200, { items: [document, failedDocument], next_cursor: null })
  if (url.pathname === `/v1/documents/${documentId}`) return json(response, 200, document)
  if (url.pathname === `/v1/documents/${failedDocumentId}`) return json(response, 200, failedDocument)
  if (url.pathname === '/v1/admin/ingestion/sync') return json(response, 200, { discovered: 1, queued: 1, deduplicated: 0, pilot_limit_reached: false })
  if (url.pathname.startsWith('/v1/admin/ingestion/')) return json(response, 200, { id: '22222222-2222-4222-8222-222222222222', document_id: documentId, status: 'completed', attempts: 1, diagnostic: null, metrics: { pages: 2 }, created_at: '2026-07-20T00:00:00Z', updated_at: '2026-07-20T00:01:00Z' })
  if (url.pathname === `/v1/admin/documents/${failedDocumentId}/retry`) {
    if (request.method !== 'POST') return json(response, 405, { detail: 'method not allowed' })
    if (!requestIdentity.scopes.includes('admin:ingest')) return json(response, 403, { detail: 'insufficient scope' })
    return json(response, 200, { id: '33333333-3333-4333-8333-333333333333', document_id: failedDocumentId, status: 'queued', attempts: 0, diagnostic: null, metrics: {}, created_at: '2026-07-20T00:04:00Z', updated_at: '2026-07-20T00:04:00Z' })
  }
  if (url.pathname === '/v1/admin/documents') {
    const chunks = []
    for await (const chunk of request) chunks.push(chunk)
    const body = Buffer.concat(chunks).toString('latin1')
    if (!body.includes('filename="fixture.pdf"') || !body.includes('application/pdf') || !body.includes('%PDF')) return json(response, 422, { detail: 'invalid multipart PDF' })
    return json(response, 200, { id: '22222222-2222-4222-8222-222222222222', document_id: documentId, status: 'queued', attempts: 0, diagnostic: null, metrics: {}, created_at: '2026-07-20T00:00:00Z', updated_at: '2026-07-20T00:00:00Z' })
  }
  if (url.pathname.endsWith('/feedback')) {
    const chunks = []
    for await (const chunk of request) chunks.push(chunk)
    let payload
    try {
      payload = JSON.parse(Buffer.concat(chunks).toString())
    } catch {
      return json(response, 400, { detail: 'feedback body must be JSON' })
    }
    if (
      request.method !== 'POST'
      || request.headers['content-type'] !== 'application/json'
      || url.pathname !== '/v1/runs/trace-1/feedback'
      || !Number.isInteger(payload.value)
      || ![-1, 1].includes(payload.value)
    ) {
      return json(response, 422, { detail: 'invalid feedback request' })
    }
    if (payload.value === -1) return json(response, 503, { detail: 'feedback unavailable' })
    response.writeHead(204)
    return response.end()
  }
  if (url.pathname === '/v1/chat/stream') {
    const chunks = []
    for await (const chunk of request) chunks.push(chunk)
    const payload = JSON.parse(Buffer.concat(chunks).toString())
    response.writeHead(200, { 'content-type': 'text/event-stream', 'cache-control': 'no-cache' })
    if (payload.message === 'Make this fail') {
      response.write('event: run.started\ndata: {"thread_id":"thread-failed","trace_id":"trace-failed"}\n\n')
      response.write('event: error\ndata: {"code":"request_failed","message":"The API could not complete this run.","thread_id":"thread-failed","trace_id":"trace-failed"}\n\n')
      return response.end()
    }
    const events = [
      'event: run.started\ndata: {"thread_id":"thread-1","trace_id":"trace-1"}\n\n',
      'event: route.selected\ndata: {"route":"documents"}\n\n',
      'event: answer.delta\ndata: {"delta":"The fixture confirms "}\n\n',
      'event: answer.delta\ndata: {"delta":"cobalt lantern retrieval [1]."}\n\n',
      `event: citation\ndata: ${JSON.stringify(completed.citations[0])}\n\n`,
      `event: run.completed\ndata: ${JSON.stringify(completed)}\n\n`,
    ]
    for (const chunk of events) {
      const midpoint = Math.floor(chunk.length / 2)
      response.write(chunk.slice(0, midpoint))
      await delay(5)
      response.write(chunk.slice(midpoint))
    }
    return response.end()
  }
  return json(response, 404, { detail: 'not found' })
})

server.listen(8010, '127.0.0.1')
