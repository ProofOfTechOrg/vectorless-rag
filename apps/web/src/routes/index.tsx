import { createFileRoute, redirect } from '@tanstack/react-router'
import { homeForScopes } from '../lib/scopes'
import { getSession } from '../server/functions'

export const Route = createFileRoute('/')({
  beforeLoad: async () => {
    const session = await getSession()
    throw redirect({ to: session ? homeForScopes(session.scopes) : '/connect' })
  },
})
