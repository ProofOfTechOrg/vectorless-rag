import { fireEvent, render, screen, waitFor } from '@testing-library/react'
import { beforeEach, describe, expect, it, vi } from 'vitest'
import { AppearanceProvider } from '../src/components/AppearanceProvider'
import { RouteError } from '../src/components/RouteError'

const invalidate = vi.fn()

vi.mock('@tanstack/react-router', async (importOriginal) => ({
  ...await importOriginal<typeof import('@tanstack/react-router')>(),
  useRouter: () => ({ invalidate }),
}))

describe('RouteError', () => {
  beforeEach(() => {
    invalidate.mockReset()
    invalidate.mockResolvedValue(undefined)
  })

  it('turns a fetch failure into a recoverable route error', async () => {
    render(
      <AppearanceProvider>
        <RouteError error={new Error('fetch failed')} title="Unable to load the console" />
      </AppearanceProvider>,
    )

    expect(screen.getByRole('heading', { name: 'Unable to load the console' })).toBeTruthy()
    expect(screen.getByText('The request did not complete. Check your connection and try again.')).toBeTruthy()

    fireEvent.click(screen.getByRole('button', { name: 'Try again' }))

    await waitFor(() => expect(invalidate).toHaveBeenCalledWith({ sync: true }))
  })
})
