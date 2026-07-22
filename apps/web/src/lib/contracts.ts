import { z } from 'zod'

const scopeSchema = z.enum(['chat', 'documents:read', 'admin:ingest'])
export const identitySchema = z.object({
  name: z.string(),
  scopes: z.array(scopeSchema),
})
export type Identity = z.infer<typeof identitySchema>

export const citationSchema = z.object({
  source_type: z.enum(['document', 'sql']),
  source_id: z.string(),
  title: z.string(),
  node_id: z.string().nullable().optional(),
  page_start: z.number().nullable().optional(),
  page_end: z.number().nullable().optional(),
  retrieval_reason: z.string().nullable().optional(),
  sql_audit_id: z.string().nullable().optional(),
})
export type Citation = z.infer<typeof citationSchema>

export const chatResponseSchema = z.object({
  answer: z.string(),
  thread_id: z.string(),
  route: z.enum(['sql', 'documents', 'hybrid', 'clarify']),
  trace_id: z.string(),
  citations: z.array(citationSchema),
  sql_audit_id: z.string().nullable().optional(),
  insufficient: z.boolean(),
})
export type ChatResponse = z.infer<typeof chatResponseSchema>

export const failureCodeSchema = z.enum(['deadline_exceeded', 'request_failed'])
export type FailureCode = z.infer<typeof failureCodeSchema>
export const runStartedSchema = z.object({ thread_id: z.string(), trace_id: z.string() })
export const chatErrorSchema = z.object({
  code: failureCodeSchema,
  message: z.string(),
  thread_id: z.string(),
  trace_id: z.string(),
})

const threadRunSchema = z.object({
  trace_id: z.string(),
  route: z.enum(['sql', 'documents', 'hybrid', 'clarify']).nullable(),
  question: z.string(),
  answer: z.string().nullable(),
  citations: z.array(citationSchema),
  insufficient: z.boolean(),
  status: z.enum(['completed', 'failed']),
  failure_code: failureCodeSchema.nullable(),
  created_at: z.string(),
})
export type ThreadRun = z.infer<typeof threadRunSchema>
export const threadSchema = z.object({ thread_id: z.string(), runs: z.array(threadRunSchema) })
const threadSummarySchema = z.object({
  thread_id: z.string(),
  title: z.string(),
  run_count: z.number().int(),
  last_activity_at: z.string(),
})
export type ThreadSummary = z.infer<typeof threadSummarySchema>
export const threadPageSchema = z.object({
  items: z.array(threadSummarySchema),
  next_cursor: z.string().nullable(),
})

export const documentSchema = z.object({
  id: z.string(),
  arxiv_id: z.string().nullable(),
  title: z.string().nullable(),
  authors: z.array(z.string()),
  abstract: z.string().nullable(),
  topics: z.array(z.string()),
  submitted_date: z.string().nullable(),
  status: z.enum(['discovered', 'queued', 'indexing', 'ready', 'unsupported', 'failed']),
  page_count: z.number().int().nullable(),
  diagnostic: z.string().nullable().optional(),
  created_at: z.string(),
  updated_at: z.string(),
})
export type Document = z.infer<typeof documentSchema>
export const documentPageSchema = z.object({
  items: z.array(documentSchema),
  next_cursor: z.string().nullable(),
})

export const jobSchema = z.object({
  id: z.string(),
  document_id: z.string(),
  status: z.enum(['queued', 'running', 'completed', 'failed', 'unsupported']),
  attempts: z.number().int(),
  diagnostic: z.string().nullable(),
  metrics: z.record(z.string(), z.json()),
  created_at: z.string(),
  updated_at: z.string(),
})
export type Job = z.infer<typeof jobSchema>
export const syncResponseSchema = z.object({
  discovered: z.number().int(),
  queued: z.number().int(),
  deduplicated: z.number().int(),
  pilot_limit_reached: z.boolean(),
})

export const apiErrorSchema = z.looseObject({ detail: z.unknown().optional() })
