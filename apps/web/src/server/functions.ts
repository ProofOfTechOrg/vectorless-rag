import { createServerFn } from '@tanstack/react-start'
import { getRequest } from '@tanstack/react-start/server'
import { z } from 'zod'
import {
  documentPageSchema,
  documentSchema,
  jobSchema,
  providerStatusSchema,
  syncResponseSchema,
  threadPageSchema,
  threadSchema,
} from '../lib/contracts'
import { apiFetch, apiJson, validateApiKey } from './api.server'
import {
  assertCurrentRequestOrigin,
  clearSession,
  issueSession,
  readSession,
  requireSession,
} from './session.server'

const cursorInput = z.object({ cursor: z.string().nullable().optional() })

export const connect = createServerFn({ method: 'POST' })
  .validator(z.object({ apiKey: z.string().min(1).max(512) }))
  .handler(async ({ data }) => {
    assertCurrentRequestOrigin()
    const identity = await validateApiKey(data.apiKey)
    issueSession(data.apiKey, identity)
    return identity
  })

export const logout = createServerFn({ method: 'POST' }).handler(async () => {
  assertCurrentRequestOrigin()
  clearSession()
  return { ok: true }
})

export const getSession = createServerFn({ method: 'GET' }).handler(async () => {
  return readSession()?.identity ?? null
})

export const getReadiness = createServerFn({ method: 'GET' }).handler(async () => {
  const session = requireSession()
  const response = await fetch(
    new URL('/health/ready', process.env.VECTORLESS_API_URL ?? 'http://127.0.0.1:8000'),
    { signal: getRequest().signal },
  )
  return { ready: response.ok, identity: session.identity }
})

export const getProviderStatus = createServerFn({ method: 'GET' }).handler(() =>
  apiJson('/v1/provider/status', providerStatusSchema),
)

export const getThreads = createServerFn({ method: 'GET' })
  .validator(cursorInput)
  .handler(({ data }) => {
    const cursor = data.cursor ? `&cursor=${encodeURIComponent(data.cursor)}` : ''
    return apiJson(`/v1/threads?limit=25${cursor}`, threadPageSchema)
  })

export const getThread = createServerFn({ method: 'GET' })
  .validator(z.object({ threadId: z.string().min(1).max(128) }))
  .handler(({ data }) => apiJson(`/v1/threads/${encodeURIComponent(data.threadId)}`, threadSchema))

export const getDocuments = createServerFn({ method: 'GET' })
  .validator(cursorInput)
  .handler(({ data }) => {
    const cursor = data.cursor ? `&cursor=${encodeURIComponent(data.cursor)}` : ''
    return apiJson(`/v1/documents?limit=25${cursor}`, documentPageSchema)
  })

export const getDocument = createServerFn({ method: 'GET' })
  .validator(z.object({ documentId: z.uuid() }))
  .handler(({ data }) => apiJson(`/v1/documents/${data.documentId}`, documentSchema))

export const retryDocument = createServerFn({ method: 'POST' })
  .validator(z.object({ documentId: z.uuid() }))
  .handler(({ data }) => {
    assertCurrentRequestOrigin()
    return apiJson(`/v1/admin/documents/${data.documentId}/retry`, jobSchema, {
      method: 'POST',
    })
  })

export const syncCorpus = createServerFn({ method: 'POST' })
  .validator(
    z.object({
      mode: z.enum(['ensure_current', 'rebuild_index', 'reextract_all']),
      arxivIds: z.array(z.string()).max(100).optional(),
    }),
  )
  .handler(({ data }) => {
    assertCurrentRequestOrigin()
    return apiJson('/v1/admin/ingestion/sync', syncResponseSchema, {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({ mode: data.mode, arxiv_ids: data.arxivIds }),
    })
  })

export const getJob = createServerFn({ method: 'GET' })
  .validator(z.object({ jobId: z.uuid() }))
  .handler(({ data }) => apiJson(`/v1/admin/ingestion/${data.jobId}`, jobSchema))

export const retryJob = createServerFn({ method: 'POST' })
  .validator(z.object({ jobId: z.uuid() }))
  .handler(({ data }) => {
    assertCurrentRequestOrigin()
    return apiJson(`/v1/admin/ingestion/${data.jobId}/retry`, jobSchema, {
      method: 'POST',
    })
  })

export const submitFeedback = createServerFn({ method: 'POST' })
  .validator(
    z.object({
      traceId: z.string().min(1),
      value: z.union([z.literal(-1), z.literal(1)]),
      comment: z.string().max(2_000).nullable().optional(),
    }),
  )
  .handler(async ({ data }) => {
    assertCurrentRequestOrigin()
    await apiFetch(`/v1/runs/${encodeURIComponent(data.traceId)}/feedback`, {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({ value: data.value, comment: data.comment }),
    })
    return { ok: true }
  })
