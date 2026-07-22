import { createFileRoute, redirect } from '@tanstack/react-router'
import { homeForScopes } from '../lib/scopes'
import { getSession } from '../server/functions'

export const Route = createFileRoute('/')({
  beforeLoad: async ({ abortController }) => {
    const session = await getSession({ signal: abortController.signal })
    throw redirect({ to: session ? homeForScopes(session.scopes) : '/connect' })
  },
})
