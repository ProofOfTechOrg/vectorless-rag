import { createFileRoute } from '@tanstack/react-router'
import { z } from 'zod'
import { apiFetch } from '../server/api.server'
import { assertRequestOrigin } from '../server/session.server'

const requestSchema = z.object({
  message: z.string().trim().min(1).max(20_000),
  thread_id: z.string().min(8).max(128).nullable().optional(),
  constraints: z
    .object({
      arxiv_ids: z.array(z.string()).max(50).default([]),
      authors: z.array(z.string()).max(20).default([]),
      topics: z.array(z.string()).max(20).default([]),
      date_from: z.string().nullable().optional(),
      date_to: z.string().nullable().optional(),
    })
    .default({ arxiv_ids: [], authors: [], topics: [] }),
})

export const Route = createFileRoute('/api/chat/stream')({
  server: {
    handlers: {
      POST: async ({ request }) => {
        assertRequestOrigin(request)
        const payload = requestSchema.parse(await request.json())
        const upstream = await apiFetch(
          '/v1/chat/stream',
          {
            method: 'POST',
            headers: { 'Content-Type': 'application/json', Accept: 'text/event-stream' },
            body: JSON.stringify(payload),
          },
          request.signal,
        )
        return new Response(upstream.body, {
          status: upstream.status,
          headers: {
            'Content-Type': 'text/event-stream',
            'Cache-Control': 'no-cache, no-transform',
            Connection: 'keep-alive',
            'X-Accel-Buffering': 'no',
          },
        })
      },
    },
  },
})
