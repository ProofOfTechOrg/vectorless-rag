import { createFileRoute } from '@tanstack/react-router'
import { ChatWorkspace } from '../components/ChatWorkspace'

export const Route = createFileRoute('/_app/chat/')({
  component: ChatIndex,
})

function ChatIndex() {
  return <ChatWorkspace />
}
