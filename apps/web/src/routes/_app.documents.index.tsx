import { Badge } from '@astryxdesign/core/Badge'
import { Button } from '@astryxdesign/core/Button'
import { Card } from '@astryxdesign/core/Card'
import { EmptyState } from '@astryxdesign/core/EmptyState'
import { Table, proportional } from '@astryxdesign/core/Table'
import { createFileRoute } from '@tanstack/react-router'
import { getDocuments } from '../server/functions'

export const Route = createFileRoute('/_app/documents/')({
  validateSearch: (search: Record<string, unknown>) => ({
    cursor: typeof search.cursor === 'string' ? search.cursor : null,
  }),
  loaderDeps: ({ search }) => ({ cursor: search.cursor }),
  loader: ({ deps }) => getDocuments({ data: deps }),
  pendingComponent: () => <div className="page"><p>Loading documents…</p></div>,
  errorComponent: ({ error }) => <div className="page"><h1>Documents unavailable</h1><p>{error.message}</p></div>,
  component: DocumentsPage,
})

function DocumentsPage() {
  const page = Route.useLoaderData()
  const search = Route.useSearch()
  const rows = page.items.map((document) => ({
    ...document,
    displayTitle: document.title ?? document.arxiv_id ?? document.id,
    authorsText: document.authors.join(', ') || 'Unknown',
    topicsText: document.topics.join(', ') || '—',
  }))
  return (
    <section className="page">
      <header className="page-header">
        <div><h1>Documents</h1><p className="muted">Indexed metadata, processing status, topics, and diagnostics.</p></div>
        <Button label="Ingest documents" href="/ingestion" variant="primary" />
      </header>
      {rows.length === 0 ? (
        <EmptyState title="No documents yet" description="Upload a born-digital PDF from Ingestion." />
      ) : (
        <>
          <div className="table-wrap desktop-table">
            <Table
              data={rows}
              columns={[
                { key: 'displayTitle', header: 'Document', width: proportional(2), renderCell: (row) => <a href={`/documents/${row.id}`}>{row.displayTitle}</a> },
                { key: 'authorsText', header: 'Authors', width: proportional(2) },
                { key: 'status', header: 'Status', width: proportional(1), renderCell: (row) => <Badge label={row.status} variant={statusVariant(row.status)} /> },
                { key: 'topicsText', header: 'Topics', width: proportional(2) },
                { key: 'page_count', header: 'Pages', width: proportional(1), renderCell: (row) => row.page_count ?? '—' },
              ]}
              density="compact"
              hasHover
              dividers="rows"
            />
          </div>
          <div className="document-list">
            {rows.map((document) => (
              <Card key={document.id} padding={4}>
                <div className="stack">
                  <a href={`/documents/${document.id}`}><strong>{document.displayTitle}</strong></a>
                  <span className="muted">{document.authorsText}</span>
                  <div className="inline"><Badge label={document.status} variant={statusVariant(document.status)} /><span>{document.page_count ?? '—'} pages</span></div>
                  {document.diagnostic && <span>{document.diagnostic}</span>}
                </div>
              </Card>
            ))}
          </div>
        </>
      )}
      <div className="inline">
        {search.cursor && <Button label="First page" href="/documents" />}
        {page.next_cursor && <Button label="Next page" onClick={() => window.location.assign(`/documents?cursor=${encodeURIComponent(page.next_cursor!)}`)} />}
      </div>
    </section>
  )
}

function statusVariant(status: string): 'neutral' | 'info' | 'success' | 'warning' | 'error' {
  if (status === 'ready') return 'success'
  if (status === 'failed') return 'error'
  if (status === 'unsupported') return 'warning'
  if (status === 'indexing') return 'info'
  return 'neutral'
}
