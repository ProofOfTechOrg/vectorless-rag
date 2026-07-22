import { Badge } from '@astryxdesign/core/Badge'
import { Banner } from '@astryxdesign/core/Banner'
import { Button } from '@astryxdesign/core/Button'
import { Card } from '@astryxdesign/core/Card'
import { Selector } from '@astryxdesign/core/Selector'
import { createFileRoute, useNavigate, useRouter } from '@tanstack/react-router'
import { useState } from 'react'
import { useAppearance, type Appearance } from '../components/AppearanceProvider'
import { getReadiness, logout } from '../server/functions'

export const Route = createFileRoute('/_app/settings')({
  loader: () => getReadiness(),
  component: SettingsPage,
})

function SettingsPage() {
  const { session } = Route.useRouteContext()
  const readiness = Route.useLoaderData()
  const navigate = useNavigate()
  const router = useRouter()
  const { appearance, setAppearance } = useAppearance()
  const [loggingOut, setLoggingOut] = useState(false)
  return (
    <section className="page">
      <header className="page-header"><div><h1>Settings</h1><p className="muted">Session identity, scopes, service readiness, and appearance.</p></div></header>
      <div className="grid">
        <Card padding={5}>
          <div className="stack">
            <h2>Connection</h2>
            <Banner status={readiness.ready ? 'success' : 'error'} title={readiness.ready ? 'FastAPI is ready' : 'FastAPI is unavailable'} />
            <dl className="metadata">
              <dt>Key name</dt><dd>{session.name}</dd>
              <dt>Session lifetime</dt><dd>Eight hours</dd>
              <dt>Cookie security</dt><dd>Encrypted, HttpOnly, SameSite=Lax</dd>
            </dl>
            <div className="inline">{session.scopes.map((scope) => <Badge key={scope} label={scope} variant="info" />)}</div>
          </div>
        </Card>
        <Card padding={5}>
          <div className="stack">
            <h2>Appearance</h2>
            <Selector
              label="Color mode"
              description="System follows your operating-system preference. This is the only browser-persisted setting."
              options={[
                { value: 'system', label: 'System' },
                { value: 'light', label: 'Light' },
                { value: 'dark', label: 'Dark' },
              ]}
              value={appearance}
              onChange={(value) => setAppearance(value as Appearance)}
            />
          </div>
        </Card>
      </div>
      <Card padding={5}>
        <div className="stack">
          <h2>Setup and session</h2>
          <p>The browser talks only to this TanStack Start origin. The server-side BFF attaches your decrypted key to FastAPI requests.</p>
          <Button
            label="Log out"
            variant="destructive"
            isLoading={loggingOut}
            clickAction={async () => {
              setLoggingOut(true)
              try {
                await logout()
                await navigate({ to: '/connect' })
              } finally {
                router.clearCache()
              }
            }}
          />
        </div>
      </Card>
    </section>
  )
}
