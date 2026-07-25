import { useEffect, useState, type Dispatch, type SetStateAction } from 'react'
import type { Job } from '../lib/contracts'
import { getJob } from '../server/functions'

const TERMINAL_JOB_STATUSES = new Set<Job['status']>([
  'completed',
  'failed',
  'unsupported',
  'deduplicated',
  'cancelled',
  'abandoned',
])

export type JobLoader = (options: { data: { jobId: string } }) => Promise<Job>

export function isTerminalJob(status: Job['status']): boolean {
  return TERMINAL_JOB_STATUSES.has(status)
}

function isTransientPollingError(reason: unknown): boolean {
  if (typeof reason !== 'object' || reason === null || !('status' in reason)) return true
  const status = reason.status
  return typeof status !== 'number' || status === 429 || status >= 500
}

export function useIngestionJobPolling(
  job: Job | null,
  setJob: Dispatch<SetStateAction<Job | null>>,
  loadJob: JobLoader = getJob,
): string | null {
  const [pollingError, setPollingError] = useState<string | null>(null)
  const jobId = job?.id ?? null
  const jobStatus = job?.status ?? null

  useEffect(() => {
    if (jobId === null || jobStatus === null || isTerminalJob(jobStatus)) return

    let ignored = false
    let timer: number | undefined

    const schedule = (delay: number) => {
      timer = window.setTimeout(poll, delay)
    }

    const poll = async () => {
      try {
        const current = await loadJob({ data: { jobId } })
        if (ignored) return
        setPollingError(null)
        setJob((active) => (active?.id === jobId ? current : active))
        if (!isTerminalJob(current.status)) schedule(1_500)
      } catch (reason) {
        if (ignored) return
        if (!isTransientPollingError(reason)) {
          setPollingError('Job polling stopped because the status request was rejected.')
          return
        }
        setPollingError('Job status is temporarily unavailable; polling will continue.')
        schedule(5_000)
      }
    }

    schedule(1_500)
    return () => {
      ignored = true
      if (timer !== undefined) window.clearTimeout(timer)
    }
  }, [jobId, jobStatus, loadJob, setJob])

  return pollingError
}
