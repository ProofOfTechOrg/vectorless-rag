import { Badge } from '@astryxdesign/core/Badge'
import { Breadcrumbs, BreadcrumbItem } from '@astryxdesign/core/Breadcrumbs'
import { Card } from '@astryxdesign/core/Card'
import { createFileRoute } from '@tanstack/react-router'
import { getDocument } from '../server/functions'

export const Route = createFileRoute('/_app/documents/$documentId')({
  loader: ({ params }) => getDocument({ data: { documentId: params.documentId } }),
  pendingComponent: () => <div className="page page-reading"><p>Loading document…</p></div>,
  errorComponent: ({ error }) => <div className="page page-reading"><h1>Document unavailable</h1><p>{error.message}</p></div>,
  component: DocumentDetail,
})

function DocumentDetail() {
  const document = Route.useLoaderData()
  return (
    <article className="page page-reading">
      <Breadcrumbs>
        <BreadcrumbItem href="/documents">Documents</BreadcrumbItem>
        <BreadcrumbItem>{document.title ?? document.arxiv_id ?? 'Document'}</BreadcrumbItem>
      </Breadcrumbs>
      <header className="page-header">
        <div><h1>{document.title ?? 'Untitled document'}</h1><p className="muted">Citation landing page · {document.id}</p></div>
        <Badge label={document.status} variant={document.status === 'ready' ? 'success' : document.status === 'failed' ? 'error' : 'neutral'} />
      </header>
      <Card padding={5}>
        <dl className="metadata">
          <dt>ArXiv ID</dt><dd>{document.arxiv_id ?? 'Not detected'}</dd>
          <dt>Authors</dt><dd>{document.authors.join(', ') || 'Unknown'}</dd>
          <dt>Submitted</dt><dd>{document.submitted_date ?? 'Unknown'}</dd>
          <dt>Pages</dt><dd>{document.page_count ?? 'Unknown'}</dd>
          <dt>Topics</dt><dd>{document.topics.join(', ') || 'None'}</dd>
          <dt>Created</dt><dd>{new Date(document.created_at).toLocaleString()}</dd>
          <dt>Updated</dt><dd>{new Date(document.updated_at).toLocaleString()}</dd>
          <dt>Diagnostic</dt><dd>{document.diagnostic ?? 'None'}</dd>
        </dl>
      </Card>
      <Card padding={5}>
        <h2>Abstract</h2>
        <p>{document.abstract ?? 'No abstract was extracted.'}</p>
      </Card>
    </article>
  )
}
