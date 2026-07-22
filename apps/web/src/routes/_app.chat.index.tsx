import { createFileRoute } from '@tanstack/react-router'
import { ChatWorkspace } from '../components/ChatWorkspace'
import { getThreads } from '../server/functions'

export const Route = createFileRoute('/_app/chat/')({
  loader: ({ abortController }) => getThreads({ data: {}, signal: abortController.signal }),
  component: ChatIndex,
})

function ChatIndex() {
  const threads = Route.useLoaderData()
  return <ChatWorkspace threads={threads.items} />
}
