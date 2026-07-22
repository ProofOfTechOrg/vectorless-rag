import { createFileRoute } from '@tanstack/react-router'
import { ChatWorkspace } from '../components/ChatWorkspace'
import { getThread, getThreads } from '../server/functions'

export const Route = createFileRoute('/_app/chat/$threadId')({
  loader: ({ params, abortController }) => Promise.all([
    getThread({ data: { threadId: params.threadId }, signal: abortController.signal }),
    getThreads({ data: {}, signal: abortController.signal }),
  ]),
  component: ThreadChat,
})

function ThreadChat() {
  const [thread, threads] = Route.useLoaderData()
  return <ChatWorkspace threadId={thread.thread_id} runs={thread.runs} threads={threads.items} />
}
