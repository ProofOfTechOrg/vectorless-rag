import { Banner } from '@astryxdesign/core/Banner'
import { Badge } from '@astryxdesign/core/Badge'
import { Breadcrumbs, BreadcrumbItem } from '@astryxdesign/core/Breadcrumbs'
import { Button } from '@astryxdesign/core/Button'
import { Card } from '@astryxdesign/core/Card'
import { createFileRoute } from '@tanstack/react-router'
import { useState } from 'react'
import { RouteError } from '../components/RouteError'
import type { Job } from '../lib/contracts'
import { getDocument, retryDocument } from '../server/functions'

export const Route = createFileRoute('/_app/documents/$documentId')({
  loader: ({ params, abortController }) => getDocument({
    data: { documentId: params.documentId },
    signal: abortController.signal,
  }),
  pendingComponent: () => <div className="page page-reading"><p>Loading document…</p></div>,
  errorComponent: ({ error }) => <RouteError error={error} title="Document unavailable" />,
  component: DocumentDetail,
})

function DocumentDetail() {
  const document = Route.useLoaderData()
  const { session } = Route.useRouteContext()
  const [replacement, setReplacement] = useState<Job | null>(null)
  const [retryError, setRetryError] = useState<string | null>(null)
  const canRetry = session.scopes.includes('admin:ingest')
  const status = replacement?.status === 'completed'
    ? 'ready'
    : replacement?.status ?? document.status

  async function retry() {
    if (document.status !== 'failed' || replacement) return
    setRetryError(null)
    try {
      setReplacement(await retryDocument({ data: { documentId: document.id } }))
    } catch (reason) {
      setRetryError(reason instanceof Error ? reason.message : 'Retry failed')
    }
  }

  return (
    <article className="page page-reading">
      <Breadcrumbs>
        <BreadcrumbItem href="/documents">Documents</BreadcrumbItem>
        <BreadcrumbItem>{document.title ?? document.arxiv_id ?? 'Document'}</BreadcrumbItem>
      </Breadcrumbs>
      <header className="page-header">
        <div><h1>{document.title ?? 'Untitled document'}</h1><p className="muted">Citation landing page · {document.id}</p></div>
        <Badge label={status} variant={status === 'ready' ? 'success' : status === 'failed' ? 'error' : 'neutral'} />
      </header>
      {retryError ? (
        <Banner status="error" title="Retry failed" description={retryError} />
      ) : null}
      {document.status === 'failed' && !replacement ? (
        <Banner
          status="error"
          title="Ingestion failed"
          description={document.diagnostic ?? 'The ingestion job did not complete.'}
          endContent={
            <Button
              label="Retry ingestion"
              clickAction={retry}
              isDisabled={!canRetry}
              tooltip={canRetry ? undefined : 'admin:ingest scope is required'}
            />
          }
        />
      ) : null}
      {replacement ? (
        <Banner
          status="info"
          title="Retry queued"
          description={`Replacement job ${replacement.id} is queued for processing.`}
        />
      ) : null}
      <Card padding={5}>
        <dl className="metadata">
          <dt>ArXiv ID</dt><dd>{document.arxiv_id ?? 'Not detected'}</dd>
          <dt>Authors</dt><dd>{document.authors.join(', ') || 'Unknown'}</dd>
          <dt>Submitted</dt><dd>{document.submitted_date ?? 'Unknown'}</dd>
          <dt>Pages</dt><dd>{document.page_count ?? 'Unknown'}</dd>
          <dt>Topics</dt><dd>{document.topics.join(', ') || 'None'}</dd>
          <dt>Created</dt><dd>{new Date(document.created_at).toLocaleString()}</dd>
          <dt>Updated</dt><dd>{new Date(document.updated_at).toLocaleString()}</dd>
          <dt>Diagnostic</dt><dd>{replacement ? 'Retry queued' : document.diagnostic ?? 'None'}</dd>
        </dl>
      </Card>
      <Card padding={5}>
        <h2>Abstract</h2>
        <p>{document.abstract ?? 'No abstract was extracted.'}</p>
      </Card>
    </article>
  )
}
