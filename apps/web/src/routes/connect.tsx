import { Banner } from '@astryxdesign/core/Banner'
import { Button } from '@astryxdesign/core/Button'
import { Card } from '@astryxdesign/core/Card'
import { TextInput } from '@astryxdesign/core/TextInput'
import { createFileRoute, redirect, useNavigate } from '@tanstack/react-router'
import { useState, useSyncExternalStore } from 'react'
import { homeForScopes } from '../lib/scopes'
import { connect, getSession } from '../server/functions'

export const Route = createFileRoute('/connect')({
  beforeLoad: async () => {
    if (await getSession()) throw redirect({ to: '/chat' })
  },
  component: ConnectPage,
})

function ConnectPage() {
  const navigate = useNavigate()
  const [apiKey, setApiKey] = useState('')
  const [error, setError] = useState<string | null>(null)
  const [pending, setPending] = useState(false)
  const hydrated = useSyncExternalStore(emptySubscribe, () => true, () => false)

  async function submit(event: React.FormEvent<HTMLFormElement>) {
    event.preventDefault()
    const submittedKey = new FormData(event.currentTarget).get('apiKey')
    const key = typeof submittedKey === 'string' ? submittedKey : apiKey
    setError(null)
    setPending(true)
    try {
      const identity = await connect({ data: { apiKey: key } })
      await navigate({ to: homeForScopes(identity.scopes) })
    } catch (reason) {
      setError(reason instanceof Error ? reason.message : 'Unable to connect')
    } finally {
      setPending(false)
    }
  }

  return (
    <main className="connect-page">
      <Card maxWidth={520} width="100%" padding={6}>
        <form className="stack" onSubmit={submit}>
          <header>
            <p className="muted">Vectorless RAG Operator Console</p>
            <h1>Connect to Vectorless RAG</h1>
            <p className="muted">Your key is encrypted into an HttpOnly eight-hour session cookie.</p>
          </header>
          {error && <Banner status="error" title="Connection failed" description={error} />}
          <TextInput
            label="API key"
            description="The key never enters browser storage, URLs, or client-readable cookies."
            type="password"
            value={apiKey}
            onChange={setApiKey}
            placeholder="vrag_…"
            htmlName="apiKey"
            isRequired
            width="100%"
          />
          <Button label="Connect" variant="primary" type="submit" isLoading={pending} isDisabled={!hydrated || pending} />
          <Banner
            status="info"
            title="Need a key?"
            description="An operator can mint one with: make bootstrap-key NAME=browser-operator"
          />
        </form>
      </Card>
    </main>
  )
}

function emptySubscribe() {
  return () => {}
}
