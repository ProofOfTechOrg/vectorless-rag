import { Badge } from '@astryxdesign/core/Badge'
import { Button } from '@astryxdesign/core/Button'
import { Card } from '@astryxdesign/core/Card'
import { ChatComposer, ChatMessage, ChatMessageBubble } from '@astryxdesign/core/Chat'
import { Citation as CitationChip } from '@astryxdesign/core/Citation'
import { Markdown } from '@astryxdesign/core/Markdown'
import { Link, useNavigate } from '@tanstack/react-router'
import { useRef, useState } from 'react'
import { chatResponseSchema, citationSchema, type Citation } from '../lib/contracts'
import { parseSseStream } from '../lib/sse'
import { submitFeedback } from '../server/functions'

interface Message {
  sender: 'user' | 'assistant'
  text: string
  traceId?: string
  route?: string | null
  citations?: Citation[]
  streaming?: boolean
}

interface ThreadRun {
  trace_id: string
  route: string | null
  question: string
  answer: string | null
  citations: Citation[]
}

interface ThreadSummary {
  thread_id: string
  title: string
  run_count: number
}

export function ChatWorkspace({
  threadId,
  runs = [],
  threads,
}: {
  threadId?: string
  runs?: ThreadRun[]
  threads: ThreadSummary[]
}) {
  const navigate = useNavigate()
  const abortRef = useRef<AbortController | null>(null)
  const [composer, setComposer] = useState('')
  const [status, setStatus] = useState('Ready')
  const [error, setError] = useState<string | null>(null)
  const [lastQuestion, setLastQuestion] = useState('')
  const [messages, setMessages] = useState<Message[]>(() =>
    runs.flatMap((run) => [
      { sender: 'user' as const, text: run.question },
      {
        sender: 'assistant' as const,
        text: run.answer ?? 'This run did not complete.',
        traceId: run.trace_id,
        route: run.route,
        citations: run.citations,
      },
    ]),
  )
  const isStreaming = messages.some((message) => message.streaming)

  async function send(value: string) {
    const question = value.trim()
    if (!question || isStreaming) return
    setComposer('')
    setLastQuestion(question)
    setError(null)
    setStatus('Starting run…')
    setMessages((current) => [
      ...current,
      { sender: 'user', text: question },
      { sender: 'assistant', text: '', citations: [], streaming: true },
    ])
    const controller = new AbortController()
    abortRef.current = controller
    try {
      const response = await fetch('/api/chat/stream', {
        method: 'POST',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify({ message: question, thread_id: threadId ?? null, constraints: {} }),
        signal: controller.signal,
      })
      if (!response.ok || !response.body) throw new Error(await response.text() || 'Chat failed')
      let completedThread = threadId
      for await (const event of parseSseStream(response.body)) {
        if (event.event === 'route.selected') {
          const route = JSON.parse(event.data).route as string
          setStatus(`Route selected: ${route}`)
          setMessages((current) => updateLast(current, { route }))
        } else if (event.event === 'retrieval.progress') {
          setStatus('Retrieving grounded evidence…')
        } else if (event.event === 'answer.delta') {
          const delta = JSON.parse(event.data).delta as string
          setStatus('Writing answer…')
          setMessages((current) => updateLast(current, { text: `${current.at(-1)?.text ?? ''}${delta}` }))
        } else if (event.event === 'citation') {
          const citation = citationSchema.parse(JSON.parse(event.data))
          setMessages((current) => updateLast(current, { citations: [...(current.at(-1)?.citations ?? []), citation] }))
        } else if (event.event === 'run.completed') {
          const result = chatResponseSchema.parse(JSON.parse(event.data))
          completedThread = result.thread_id
          setMessages((current) => updateLast(current, {
            text: result.answer,
            traceId: result.trace_id,
            route: result.route,
            citations: result.citations,
            streaming: false,
          }))
          setStatus(result.insufficient ? 'Completed with insufficient evidence' : 'Completed')
        } else if (event.event === 'error') {
          throw new Error('The API could not complete this run')
        }
      }
      if (completedThread && completedThread !== threadId) {
        await navigate({ to: '/chat/$threadId', params: { threadId: completedThread }, replace: true })
      }
    } catch (reason) {
      if (controller.signal.aborted) {
        setStatus('Cancelled')
      } else {
        const message = reason instanceof Error ? reason.message : 'Chat failed'
        setError(message)
        setStatus('Failed')
      }
      setMessages((current) => updateLast(current, { streaming: false }))
    } finally {
      abortRef.current = null
    }
  }

  return (
    <div className="page chat-layout">
      <div className="chat-scroll" aria-live="polite">
        <header className="page-header">
          <div>
            <h1>Grounded research chat</h1>
            <p className="muted">Answers stay tied to indexed documents and page ranges.</p>
          </div>
          <Button label="New thread" href="/chat" variant="secondary" />
        </header>
        {threads.length > 0 && (
          <Card padding={3}>
            <div className="inline" aria-label="Recent threads">
              <strong>Recent</strong>
              {threads.slice(0, 5).map((thread) => (
                <Link key={thread.thread_id} to="/chat/$threadId" params={{ threadId: thread.thread_id }}>
                  {thread.title} ({thread.run_count})
                </Link>
              ))}
            </div>
          </Card>
        )}
        {messages.length === 0 && (
          <Card padding={6}><p>Ask a catalog question or request a cited comparison across your indexed papers.</p></Card>
        )}
        {messages.map((message, index) => (
          <ChatMessage key={`${index}-${message.sender}`} sender={message.sender} name={message.sender === 'assistant' ? 'Vectorless RAG' : 'You'}>
            <ChatMessageBubble>
              {message.sender === 'assistant' ? (
                <Markdown isStreaming={message.streaming} headingLevelStart={3} autolink="gfm">{message.text || '…'}</Markdown>
              ) : message.text}
            </ChatMessageBubble>
            {message.route && <Badge label={message.route} variant="info" />}
            {message.citations && message.citations.length > 0 && (
              <div className="citation-list">
                {message.citations.map((citation, citationIndex) => (
                  <a key={`${citation.source_id}-${citationIndex}`} href={citation.source_type === 'document' ? `/documents/${citation.source_id}` : undefined}>
                    <CitationChip source={{ title: `${citation.title}${pageLabel(citation)}` }} number={citationIndex + 1} />
                  </a>
                ))}
              </div>
            )}
            {message.traceId && (
              <div className="inline">
                <span className="muted">Was this grounded?</span>
                <Button label="Helpful" size="sm" variant="ghost" clickAction={async () => { await submitFeedback({ data: { traceId: message.traceId!, value: 1 } }) }} />
                <Button label="Not helpful" size="sm" variant="ghost" clickAction={async () => { await submitFeedback({ data: { traceId: message.traceId!, value: -1 } }) }} />
              </div>
            )}
          </ChatMessage>
        ))}
        <p className="status-line" role="status">{status}</p>
        {error && <div className="inline"><span>{error}</span><Button label="Retry" onClick={() => send(lastQuestion)} /></div>}
      </div>
      <div className="chat-composer">
        <ChatComposer
          value={composer}
          onChange={setComposer}
          onSubmit={send}
          onStop={() => abortRef.current?.abort()}
          isStopShown={isStreaming}
          placeholder="Ask about the indexed corpus…"
        />
      </div>
    </div>
  )
}

function updateLast(messages: Message[], patch: Partial<Message>): Message[] {
  if (!messages.length) return messages
  return [...messages.slice(0, -1), { ...messages[messages.length - 1], ...patch }]
}

function pageLabel(citation: Citation): string {
  if (!citation.page_start) return ''
  return citation.page_end && citation.page_end !== citation.page_start
    ? ` · pp. ${citation.page_start}–${citation.page_end}`
    : ` · p. ${citation.page_start}`
}
