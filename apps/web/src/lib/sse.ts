export interface SseEvent {
  event: string
  data: string
}

export async function* parseSseStream(
  stream: ReadableStream<Uint8Array>,
): AsyncGenerator<SseEvent> {
  const reader = stream.getReader()
  const decoder = new TextDecoder()
  let buffer = ''
  try {
    while (true) {
      const { done, value } = await reader.read()
      buffer += decoder.decode(value, { stream: !done }).replaceAll('\r\n', '\n')
      let boundary = buffer.indexOf('\n\n')
      while (boundary >= 0) {
        const block = buffer.slice(0, boundary)
        buffer = buffer.slice(boundary + 2)
        let event = 'message'
        const data: string[] = []
        for (const line of block.split('\n')) {
          if (line.startsWith('event:')) event = line.slice(6).trimStart()
          if (line.startsWith('data:')) data.push(line.slice(5).trimStart())
        }
        if (data.length) yield { event, data: data.join('\n') }
        boundary = buffer.indexOf('\n\n')
      }
      if (done) break
    }
  } finally {
    reader.releaseLock()
  }
}
