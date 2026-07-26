import { act, renderHook } from '@testing-library/react'
import { beforeEach, describe, expect, it, vi } from 'vitest'
import { useReadiness } from '../src/hooks/use-readiness'

const getReadiness = vi.hoisted(() => vi.fn())

vi.mock('../src/server/functions', () => ({ getReadiness }))

function deferred<T>() {
  let resolve: (value: T) => void = () => undefined
  let reject: (reason: unknown) => void = () => undefined
  const promise = new Promise<T>((resolvePromise, rejectPromise) => {
    resolve = resolvePromise
    reject = rejectPromise
  })
  return { promise, reject, resolve }
}

describe('API readiness', () => {
  beforeEach(() => getReadiness.mockReset())

  it('ignores the stale StrictMode probe without aborting it', async () => {
    const stale = deferred<{ ready: boolean }>()
    const active = deferred<{ ready: boolean }>()
    getReadiness
      .mockReturnValueOnce(stale.promise)
      .mockReturnValueOnce(active.promise)

    const hook = renderHook(() => useReadiness(), { reactStrictMode: true })

    expect(getReadiness).toHaveBeenCalledTimes(2)
    expect(getReadiness).toHaveBeenNthCalledWith(1, {})
    expect(getReadiness).toHaveBeenNthCalledWith(2, {})

    await act(async () => stale.resolve({ ready: true }))
    expect(hook.result.current).toBeNull()

    await act(async () => active.resolve({ ready: false }))
    expect(hook.result.current).toBe(false)
  })

  it('marks the active probe unavailable when it fails', async () => {
    getReadiness.mockResolvedValueOnce({ ready: true })
    const active = deferred<{ ready: boolean }>()
    getReadiness.mockReturnValueOnce(active.promise)

    const hook = renderHook(() => useReadiness(), { reactStrictMode: true })

    await act(async () => active.reject(new Error('unavailable')))
    expect(hook.result.current).toBe(false)
  })
})
