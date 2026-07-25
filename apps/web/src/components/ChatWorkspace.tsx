import { Badge } from '@astryxdesign/core/Badge'
import { Button } from '@astryxdesign/core/Button'
import {
  ChatComposer,
  ChatLayout,
  ChatMessage,
  ChatMessageBubble,
  ChatMessageList,
  ChatMessageMetadata,
} from '@astryxdesign/core/Chat'
import { Citation as CitationChip } from '@astryxdesign/core/Citation'
import { EmptyState } from '@astryxdesign/core/EmptyState'
import { Heading } from '@astryxdesign/core/Heading'
import { HStack } from '@astryxdesign/core/HStack'
import { IconButton } from '@astryxdesign/core/IconButton'
import { Markdown } from '@astryxdesign/core/Markdown'
import { Text } from '@astryxdesign/core/Text'
import { VStack } from '@astryxdesign/core/VStack'
import { Link, useNavigate, useRouter } from '@tanstack/react-router'
import { ThumbsDown, ThumbsUp } from 'lucide-react'
import { useEffect, useRef, useState } from 'react'
import { useHydrated } from '../hooks/use-hydrated'
import {
  chatErrorSchema,
  chatResponseSchema,
  citationSchema,
  runStartedSchema,
  type Citation,
  type FailureCode,
  type ThreadRun,
} from '../lib/contracts'
import { parseSseStream } from '../lib/sse'
import { submitFeedback } from '../server/functions'

interface Message {
  sender: 'user' | 'assistant'
  text: string
  traceId?: string
  threadId?: string
  route?: string | null
  citations?: Citation[]
  streaming?: boolean
  passivePending?: boolean
  failureCode?: FailureCode
  retryQuestion?: string
}

const EMPTY_RUNS: ThreadRun[] = []

export function ChatWorkspace({
  threadId,
  runs = EMPTY_RUNS,
}: {
  threadId?: string
  runs?: ThreadRun[]
}) {
  return (
    <main className="chat-page" data-testid="chat-page">
      <ChatConversation
        key={conversationKey(threadId, runs)}
        runs={runs}
        threadId={threadId}
      />
    </main>
  )
}

function ChatConversation({
  threadId,
  runs,
}: {
  threadId?: string
  runs: ThreadRun[]
}) {
  const navigate = useNavigate()
  const router = useRouter()
  const abortRef = useRef<AbortController | null>(null)
  const hydrated = useHydrated()
  const [composer, setComposer] = useState('')
  const [status, setStatus] = useState('Ready')
  const [messages, setMessages] = useState<Message[]>(() =>
    runs.flatMap((run) => [
      { sender: 'user' as const, text: run.question },
      {
        sender: 'assistant' as const,
        text: run.status === 'running'
          ? ''
          : run.answer ?? failureMessage(run.failure_code),
        traceId: run.trace_id,
        threadId,
        route: run.route,
        citations: run.citations,
        passivePending: run.status === 'running',
        failureCode: run.status === 'failed' ? run.failure_code ?? undefined : undefined,
        retryQuestion: run.status === 'failed' ? run.question : undefined,
      },
    ]),
  )
  const isStreaming = messages.some((message) => message.streaming)

  useEffect(() => () => {
    const controller = abortRef.current
    abortRef.current = null
    controller?.abort()
  }, [])

  async function send(value: string) {
    const question = value.trim()
    if (!question || isStreaming) return
    setComposer('')
    setStatus('Starting run…')
    setMessages((current) => [
      ...current,
      { sender: 'user', text: question },
      { sender: 'assistant', text: '', citations: [], streaming: true, retryQuestion: question },
    ])
    const controller = new AbortController()
    abortRef.current = controller
    let completedThread = threadId
    let traceId: string | undefined
    let finished = false
    let shouldNavigate = true
    let hasCanonicalTerminalEvent = false
    try {
      const response = await fetch('/api/chat/stream', {
        method: 'POST',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify({ message: question, thread_id: threadId ?? null, constraints: {} }),
        signal: controller.signal,
      })
      if (!response.ok || !response.body) throw new Error(await response.text() || 'Chat failed')
      for await (const event of parseSseStream(response.body)) {
        if (event.event === 'run.started') {
          const started = runStartedSchema.parse(JSON.parse(event.data))
          completedThread = started.thread_id
          traceId = started.trace_id
          setMessages((current) => updateLast(current, { traceId, threadId: started.thread_id }))
        } else if (event.event === 'route.selected') {
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
          hasCanonicalTerminalEvent = true
          completedThread = result.thread_id
          setMessages((current) => updateLast(current, {
            text: result.answer,
            traceId: result.trace_id,
            route: result.route,
            citations: result.citations,
            streaming: false,
            failureCode: undefined,
            retryQuestion: undefined,
          }))
          finished = true
          setStatus(result.insufficient ? 'Completed with insufficient evidence' : 'Completed')
        } else if (event.event === 'error') {
          const failure = chatErrorSchema.parse(JSON.parse(event.data))
          hasCanonicalTerminalEvent = true
          completedThread = failure.thread_id
          traceId = failure.trace_id
          setMessages((current) => updateLast(current, {
            text: failure.message,
            traceId,
            streaming: false,
            failureCode: failure.code,
          }))
          finished = true
          setStatus('Failed')
        }
      }
      if (!finished) throw new Error('The chat stream ended before completion.')
    } catch (reason) {
      if (controller.signal.aborted) {
        shouldNavigate = false
        if (abortRef.current === controller) {
          setStatus('Cancelled')
          setMessages((current) => updateLast(current, {
            text: 'Run cancelled.',
            streaming: false,
            failureCode: 'request_failed',
          }))
        }
      } else {
        const message = reason instanceof Error ? reason.message : 'Chat failed'
        setStatus('Failed')
        setMessages((current) => updateLast(current, {
          text: message,
          traceId,
          streaming: false,
          failureCode: 'request_failed',
        }))
      }
    } finally {
      if (abortRef.current === controller) abortRef.current = null
    }
    if (shouldNavigate && completedThread && completedThread !== threadId) {
      await navigate({ to: '/chat/$threadId', params: { threadId: completedThread }, replace: true })
    }
    if (shouldNavigate && hasCanonicalTerminalEvent) {
      await router.invalidate({ sync: true })
    }
  }

  return (
    <>
      <div className="chat-intro">
        <VStack className="chat-column" data-testid="chat-intro-column" gap={4}>
          <HStack align="start" gap={3} justify="between" wrap="wrap">
            <VStack gap={1}>
              <Heading level={1}>Grounded research chat</Heading>
              <Text as="p" display="block" type="supporting">
                Answers stay tied to indexed documents and page ranges.
              </Text>
            </VStack>
            <Button label="New chat" href="/chat" variant="secondary" />
          </HStack>
        </VStack>
      </div>
      <ChatLayout
        className="chat-conversation"
        data-testid="chat-layout"
        density="spacious"
        emptyState={(
          <div className="chat-column chat-empty-state">
            <EmptyState
              description="Ask a catalog question or request a cited comparison across your indexed papers."
              headingLevel={2}
              title="Ask the indexed corpus"
            />
          </div>
        )}
        composer={(
          <ChatComposer
            className="chat-column"
            data-testid="chat-composer"
            density="spacious"
            value={composer}
            onChange={setComposer}
            onSubmit={send}
            onStop={() => abortRef.current?.abort()}
            isDisabled={!hydrated}
            isStopShown={isStreaming}
            footerActions={(
              <Text className="chat-status" role="status" type="supporting">
                {status}
              </Text>
            )}
            placeholder="Ask about the indexed corpus…"
          />
        )}
      >
        {messages.length > 0 ? (
          <ChatMessageList
            className="chat-column"
            data-testid="chat-message-list"
            density="spacious"
            isStreaming={isStreaming}
          >
            {messages.map((message, index) => {
              const hasMetadata = !!(
                message.failureCode
                || message.route
                || message.citations?.length
                || (message.traceId && !message.failureCode && !message.passivePending)
              )

              return (
                <ChatMessage key={`${index}-${message.sender}`} sender={message.sender}>
                  <ChatMessageBubble
                    className={message.sender === 'assistant' ? 'chat-answer-bubble' : undefined}
                    data-testid={`${message.sender}-bubble`}
                    name={(
                      <Text type="supporting" weight="semibold">
                        {message.sender === 'assistant' ? 'Vectorless RAG' : 'You'}
                      </Text>
                    )}
                    variant={message.sender === 'assistant' ? 'ghost' : 'filled'}
                    metadata={hasMetadata ? (
                      <VStack className="chat-message-metadata" gap={2}>
                        {message.failureCode ? (
                          <HStack align="center" gap={2} wrap="wrap">
                            <ChatMessageMetadata status="error" />
                            {message.retryQuestion ? (
                              <Button
                                label="Retry"
                                size="sm"
                                variant="ghost"
                                onClick={() => send(message.retryQuestion!)}
                              />
                            ) : null}
                          </HStack>
                        ) : null}
                        {message.route ? <Badge label={message.route} variant="info" /> : null}
                        {message.citations && message.citations.length > 0 ? (
                          <HStack className="citation-list" gap={2} wrap="wrap">
                            {message.citations.map((citation, citationIndex) => (
                              citation.source_type === 'document' ? (
                                <Link key={`${citation.source_id}-${citationIndex}`} to="/documents/$documentId" params={{ documentId: citation.source_id }}>
                                  <CitationChip source={{ title: `${citation.title}${pageLabel(citation)}` }} number={citationIndex + 1} />
                                </Link>
                              ) : (
                                <span key={`${citation.source_id}-${citationIndex}`}>
                                  <CitationChip source={{ title: `${citation.title}${pageLabel(citation)}` }} number={citationIndex + 1} />
                                </span>
                              )
                            ))}
                          </HStack>
                        ) : null}
                        {message.traceId
                          && !message.failureCode
                          && !message.passivePending
                          && message.threadId === threadId ? (
                          <AnswerFeedback traceId={message.traceId} />
                        ) : null}
                      </VStack>
                    ) : undefined}
                  >
                    {message.sender === 'assistant' && message.text ? (
                      <Markdown contentWidth="100%" isStreaming={message.streaming} headingLevelStart={3} autolink="gfm">
                        {message.text}
                      </Markdown>
                    ) : message.sender === 'assistant'
                      && (message.streaming || message.passivePending) ? (
                      <WaitingForResponse />
                    ) : message.text}
                  </ChatMessageBubble>
                </ChatMessage>
              )
            })}
          </ChatMessageList>
        ) : null}
      </ChatLayout>
    </>
  )
}

function conversationKey(threadId: string | undefined, runs: ThreadRun[]): string {
  const latestRun = runs.at(-1)
  return [threadId ?? 'new', runs.length, latestRun?.trace_id ?? '', latestRun?.status ?? ''].join(':')
}

function WaitingForResponse() {
  return (
    <output aria-label="Waiting for response" className="chat-waiting">
      <span aria-hidden="true" className="chat-waiting-dots">
        <span className="chat-waiting-dot" />
        <span className="chat-waiting-dot" />
        <span className="chat-waiting-dot" />
      </span>
    </output>
  )
}

type FeedbackValue = -1 | 1
type FeedbackStatus = 'idle' | 'submitting' | 'saved' | 'error'

function AnswerFeedback({ traceId }: { traceId: string }) {
  const [value, setValue] = useState<FeedbackValue | null>(null)
  const [feedbackStatus, setFeedbackStatus] = useState<FeedbackStatus>('idle')

  async function saveFeedback(nextValue: FeedbackValue) {
    if (feedbackStatus === 'submitting' || feedbackStatus === 'saved') return
    setValue(nextValue)
    setFeedbackStatus('submitting')
    try {
      await submitFeedback({ data: { traceId, value: nextValue } })
      setFeedbackStatus('saved')
    } catch {
      setValue(null)
      setFeedbackStatus('error')
    }
  }

  const isLocked = feedbackStatus === 'submitting' || feedbackStatus === 'saved'
  const statusLabel = feedbackStatus === 'submitting'
    ? 'Saving feedback…'
    : feedbackStatus === 'saved'
      ? value === 1 ? 'Marked helpful' : 'Marked not helpful'
      : feedbackStatus === 'error'
        ? 'Feedback wasn’t saved. Try again.'
        : 'Rate answer'

  return (
    <HStack aria-label="Answer feedback" align="center" gap={1} wrap="wrap">
      <Text role="status" type="supporting">{statusLabel}</Text>
      <IconButton
        icon={<ThumbsUp aria-hidden="true" size={16} />}
        isDisabled={isLocked}
        isLoading={feedbackStatus === 'submitting' && value === 1}
        label="Mark answer as helpful"
        onClick={() => void saveFeedback(1)}
        size="sm"
        tooltip="Helpful"
        variant={value === 1 ? 'secondary' : 'ghost'}
      />
      <IconButton
        icon={<ThumbsDown aria-hidden="true" size={16} />}
        isDisabled={isLocked}
        isLoading={feedbackStatus === 'submitting' && value === -1}
        label="Mark answer as not helpful"
        onClick={() => void saveFeedback(-1)}
        size="sm"
        tooltip="Not helpful"
        variant={value === -1 ? 'secondary' : 'ghost'}
      />
    </HStack>
  )
}

function failureMessage(code: FailureCode | null): string {
  switch (code) {
    case 'deadline_exceeded':
      return 'The request timed out.'
    case 'provider_unavailable':
      return 'The model provider is unavailable. Document browsing remains available.'
    case 'upstream_invalid_response':
      return 'The model provider returned an invalid response.'
    default:
      return 'The API could not complete this run.'
  }
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
