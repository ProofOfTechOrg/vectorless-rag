import { describe, expect, it } from 'vitest'
import { parseSseStream } from '../src/lib/sse'

function fragmented(parts: string[]) {
  const encoder = new TextEncoder()
  return new ReadableStream<Uint8Array>({
    start(controller) {
      for (const part of parts) controller.enqueue(encoder.encode(part))
      controller.close()
    },
  })
}

describe('SSE parser', () => {
  it('preserves events split across arbitrary chunks', async () => {
    const stream = fragmented([
      'event: answer.',
      'delta\r\ndata: {"delta":"hel',
      'lo"}\r\n\r\nevent: citation\n',
      'data: {"source_id":"doc"}\n\n',
    ])
    const events = []
    for await (const event of parseSseStream(stream)) events.push(event)
    expect(events).toEqual([
      { event: 'answer.delta', data: '{"delta":"hello"}' },
      { event: 'citation', data: '{"source_id":"doc"}' },
    ])
  })
})
