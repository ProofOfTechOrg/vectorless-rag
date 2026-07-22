import { describe, expect, it } from 'vitest'
import { chatResponseSchema, threadSchema } from '../src/lib/contracts'

describe('chat route contracts', () => {
  it('accepts help for current and historical runs', () => {
    const response = chatResponseSchema.parse({
      answer: 'Supported tasks',
      thread_id: 'thread-help',
      route: 'help',
      trace_id: 'trace-help',
      citations: [],
      insufficient: false,
    })
    const thread = threadSchema.parse({
      thread_id: 'thread-help',
      runs: [{
        trace_id: 'trace-help',
        route: 'help',
        question: 'What can you do?',
        answer: 'Supported tasks',
        citations: [],
        insufficient: false,
        status: 'completed',
        failure_code: null,
        created_at: '2026-07-23T00:00:00Z',
      }],
    })

    expect(response.route).toBe('help')
    expect(thread.runs[0]?.route).toBe('help')
  })
})
