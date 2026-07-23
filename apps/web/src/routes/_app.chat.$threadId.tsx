import { createFileRoute } from '@tanstack/react-router'
import { ChatWorkspace } from '../components/ChatWorkspace'
import { getThread } from '../server/functions'

export const Route = createFileRoute('/_app/chat/$threadId')({
  loader: ({ params, abortController }) =>
    getThread({ data: { threadId: params.threadId }, signal: abortController.signal }),
  component: ThreadChat,
})

function ThreadChat() {
  const thread = Route.useLoaderData()
  return <ChatWorkspace threadId={thread.thread_id} runs={thread.runs} />
}
