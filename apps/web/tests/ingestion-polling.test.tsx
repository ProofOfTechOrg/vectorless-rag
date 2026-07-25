import { act, renderHook } from '@testing-library/react'
import { useState } from 'react'
import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest'
import {
  useIngestionJobPolling,
  type JobLoader,
} from '../src/hooks/use-ingestion-job'
import type { Job } from '../src/lib/contracts'

const JOB_A = '11111111-1111-4111-8111-111111111111'
const JOB_B = '22222222-2222-4222-8222-222222222222'
const DOCUMENT_ID = '33333333-3333-4333-8333-333333333333'

function job(id: string, status: Job['status']): Job {
  return {
    id,
    document_id: DOCUMENT_ID,
    status,
    attempts: status === 'queued' ? 0 : 1,
    mode: 'ensure_current',
    diagnostic: null,
    metrics: {},
    created_at: '2026-07-20T00:00:00Z',
    updated_at: '2026-07-20T00:00:00Z',
  }
}

function renderPolling(initial: Job, loader: JobLoader) {
  return renderHook(() => {
    const [activeJob, setActiveJob] = useState<Job | null>(initial)
    const pollingError = useIngestionJobPolling(activeJob, setActiveJob, loader)
    return { activeJob, setActiveJob, pollingError }
  })
}

describe('durable ingestion polling', () => {
  beforeEach(() => vi.useFakeTimers())
  afterEach(() => vi.useRealTimers())

  it('continues beyond the former 120-poll ceiling', async () => {
    const loader = vi.fn(async () => job(JOB_A, 'running'))
    const hook = renderPolling(job(JOB_A, 'queued'), loader)

    for (let count = 0; count < 121; count += 1) {
      await act(async () => vi.advanceTimersByTimeAsync(1_500))
    }

    expect(loader).toHaveBeenCalledTimes(121)
    expect(hook.result.current.activeJob?.status).toBe('running')
    expect(hook.result.current.pollingError).toBeNull()
  })

  it('backs off transient errors for five seconds and then recovers', async () => {
    const loader = vi
      .fn()
      .mockRejectedValueOnce(new Error('temporary'))
      .mockResolvedValue(job(JOB_A, 'running'))
    const hook = renderPolling(job(JOB_A, 'queued'), loader)

    await act(async () => vi.advanceTimersByTimeAsync(1_500))
    expect(hook.result.current.pollingError).toContain('temporarily unavailable')
    await act(async () => vi.advanceTimersByTimeAsync(4_999))
    expect(loader).toHaveBeenCalledTimes(1)
    await act(async () => vi.advanceTimersByTimeAsync(1))

    expect(loader).toHaveBeenCalledTimes(2)
    expect(hook.result.current.activeJob?.status).toBe('running')
    expect(hook.result.current.pollingError).toBeNull()
  })

  it('stops polling after a deterministic status rejection', async () => {
    const loader = vi.fn().mockRejectedValue(Object.assign(new Error('missing'), { status: 404 }))
    const hook = renderPolling(job(JOB_A, 'queued'), loader)

    await act(async () => vi.advanceTimersByTimeAsync(1_500))
    await act(async () => vi.advanceTimersByTimeAsync(30_000))

    expect(loader).toHaveBeenCalledTimes(1)
    expect(hook.result.current.pollingError).toContain('status request was rejected')
  })

  it('stops scheduling after a terminal response', async () => {
    const loader = vi.fn(async () => job(JOB_A, 'completed'))
    const hook = renderPolling(job(JOB_A, 'running'), loader)

    await act(async () => vi.advanceTimersByTimeAsync(1_500))
    await act(async () => vi.advanceTimersByTimeAsync(30_000))

    expect(loader).toHaveBeenCalledTimes(1)
    expect(hook.result.current.activeJob?.status).toBe('completed')
  })

  it('ignores an old response after retry switches to a replacement job', async () => {
    let resolveOld: ((value: Job) => void) | undefined
    const oldResponse = new Promise<Job>((resolve) => {
      resolveOld = resolve
    })
    const loader = vi.fn(({ data }: { data: { jobId: string } }) =>
      data.jobId === JOB_A ? oldResponse : Promise.resolve(job(JOB_B, 'running')),
    )
    const hook = renderPolling(job(JOB_A, 'queued'), loader)

    await act(async () => vi.advanceTimersByTimeAsync(1_500))
    act(() => hook.result.current.setActiveJob(job(JOB_B, 'queued')))
    await act(async () => resolveOld?.(job(JOB_A, 'completed')))

    expect(hook.result.current.activeJob?.id).toBe(JOB_B)
    await act(async () => vi.advanceTimersByTimeAsync(1_500))
    expect(loader).toHaveBeenLastCalledWith({ data: { jobId: JOB_B } })
  })

  it('clears pending timers when unmounted', async () => {
    const loader = vi.fn(async () => job(JOB_A, 'running'))
    const hook = renderPolling(job(JOB_A, 'queued'), loader)

    hook.unmount()
    await vi.advanceTimersByTimeAsync(10_000)

    expect(loader).not.toHaveBeenCalled()
  })
})
