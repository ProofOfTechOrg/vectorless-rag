import { AlertDialog } from '@astryxdesign/core/AlertDialog'
import { Banner } from '@astryxdesign/core/Banner'
import { Badge } from '@astryxdesign/core/Badge'
import { Button } from '@astryxdesign/core/Button'
import { Card } from '@astryxdesign/core/Card'
import { FileInput } from '@astryxdesign/core/FileInput'
import { ProgressBar } from '@astryxdesign/core/ProgressBar'
import { createFileRoute, Link } from '@tanstack/react-router'
import { useState } from 'react'
import { isTerminalJob, useIngestionJobPolling } from '../hooks/use-ingestion-job'
import { jobSchema, type Job } from '../lib/contracts'
import { retryJob, syncCorpus } from '../server/functions'

export const Route = createFileRoute('/_app/ingestion')({ component: IngestionPage })

function IngestionPage() {
  const { session } = Route.useRouteContext()
  const allowed = session.scopes.includes('admin:ingest')
  const [file, setFile] = useState<File | null>(null)
  const [job, setJob] = useState<Job | null>(null)
  const [uploading, setUploading] = useState(false)
  const [forceOpen, setForceOpen] = useState(false)
  const [message, setMessage] = useState<string | null>(null)
  const [error, setError] = useState<string | null>(null)
  const pollingError = useIngestionJobPolling(job, setJob)

  async function upload() {
    if (!file) return
    setUploading(true)
    setError(null)
    const body = new FormData()
    body.set('file', file, file.name)
    try {
      const response = await fetch('/api/documents/upload', { method: 'POST', body })
      if (!response.ok) throw new Error(await response.text())
      const created = jobSchema.parse(await response.json())
      setJob(created)
    } catch (reason) {
      setError(reason instanceof Error ? reason.message : 'Upload failed')
    } finally {
      setUploading(false)
    }
  }

  async function retry() {
    if (job?.status !== 'failed') return
    setError(null)
    try {
      const replacement = await retryJob({ data: { jobId: job.id } })
      setJob(replacement)
    } catch (reason) {
      setError(reason instanceof Error ? reason.message : 'Retry failed')
    }
  }

  async function synchronize(force: boolean) {
    setError(null)
    try {
      const result = await syncCorpus({ data: { force } })
      setMessage(`Discovered ${result.discovered}, queued ${result.queued}, deduplicated ${result.deduplicated}.`)
      if (result.pilot_limit_reached) setError('The ten-document pilot limit was reached; review the pilot before enabling full-corpus indexing.')
    } catch (reason) {
      setError(reason instanceof Error ? reason.message : 'Sync failed')
    } finally {
      setForceOpen(false)
    }
  }

  return (
    <section className="page">
      <header className="page-header"><div><h1>Ingestion</h1><p className="muted">Upload a PDF or scan the fixed mounted corpus.</p></div></header>
      {!allowed && <Banner status="warning" title="Read-only key" description="This key lacks admin:ingest. API authorization remains enforced even if UI controls are bypassed." />}
      {error && <Banner status="error" title="Ingestion attention required" description={error} />}
      {pollingError && <Banner status="warning" title="Polling delayed" description={pollingError} />}
      {message && <Banner status="success" title="Sync requested" description={message} isDismissable onDismiss={() => setMessage(null)} />}
      <div className="grid">
        <Card padding={5}>
          <div className="stack">
            <h2>Upload PDF</h2>
            <FileInput
              label="Born-digital PDF"
              description="One PDF, up to the API's configured 200 MB limit. OCR-only files are unsupported."
              value={file}
              onChange={(value) => setFile(value instanceof File ? value : null)}
              accept="application/pdf,.pdf"
              mode="dropzone"
              width="100%"
              isDisabled={!allowed}
              disabledMessage="admin:ingest scope is required"
              isLoading={uploading}
            />
            <Button label="Upload and queue" variant="primary" clickAction={upload} isDisabled={!allowed || !file} />
          </div>
        </Card>
        <Card padding={5}>
          <div className="stack">
            <h2>Mounted corpus</h2>
            <p>Scans direct PDF children of the configured read-only mount. Files are deduplicated by content.</p>
            <Banner status="warning" title="Pilot safety gate" description="Do not run this against a large existing corpus unless you intend to queue the pilot batch." />
            <div className="inline">
              <Button label="Sync new files" clickAction={() => synchronize(false)} isDisabled={!allowed} />
              <Button label="Force reprocess…" variant="destructive" onClick={() => setForceOpen(true)} isDisabled={!allowed} />
            </div>
          </div>
        </Card>
      </div>
      {job && (
        <Card padding={5}>
          <div className="stack">
            <div className="inline"><h2>Job</h2><Badge label={job.status} variant={job.status === 'completed' ? 'success' : job.status === 'failed' ? 'error' : 'info'} /></div>
            {!isTerminalJob(job.status) && <ProgressBar value={job.status === 'running' ? 60 : 20} label="Processing document" />}
            <p>Attempts: {job.attempts} · Document: <Link to="/documents/$documentId" params={{ documentId: job.document_id }}>{job.document_id}</Link></p>
            {job.diagnostic && <Banner status={job.status === 'failed' ? 'error' : 'info'} title="Diagnostic" description={job.diagnostic} />}
            {job.status === 'failed' && <Button label="Retry ingestion" clickAction={retry} isDisabled={!allowed} />}
            {Object.keys(job.metrics).length > 0 && <pre>{JSON.stringify(job.metrics, null, 2)}</pre>}
          </div>
        </Card>
      )}
      <AlertDialog
        isOpen={forceOpen}
        onOpenChange={setForceOpen}
        title="Force corpus reprocessing?"
        description="This can enqueue work for already-known PDFs and consume model capacity."
        actionLabel="Force reprocess"
        onAction={() => synchronize(true)}
      />
    </section>
  )
}
