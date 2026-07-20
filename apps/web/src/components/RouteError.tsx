import { Banner } from '@astryxdesign/core/Banner'
import { Button } from '@astryxdesign/core/Button'
import { Card } from '@astryxdesign/core/Card'
import { useRouter } from '@tanstack/react-router'

type RouteErrorProps = {
  error: unknown
  title: string
  fullPage?: boolean
}

export function RouteError({ error, title, fullPage = false }: RouteErrorProps) {
  const router = useRouter()
  const message = error instanceof Error ? error.message.trim() : ''
  const description = message && message.toLowerCase() !== 'fetch failed'
    ? message
    : 'The request did not complete. Check your connection and try again.'

  return (
    <main className={fullPage ? 'connect-page' : 'page page-reading'}>
      <Card maxWidth={560} width="100%" padding={6}>
        <div className="stack">
          <header>
            <p className="muted">Vectorless RAG Operator Console</p>
            <h1>{title}</h1>
          </header>
          <Banner
            status="error"
            title="Request failed"
            description={description}
            endContent={
              <Button
                label="Try again"
                variant="primary"
                clickAction={() => router.invalidate({ sync: true })}
              />
            }
          />
        </div>
      </Card>
    </main>
  )
}
