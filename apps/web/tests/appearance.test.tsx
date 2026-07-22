import { Button } from '@astryxdesign/core/Button'
import { fireEvent, render, screen, waitFor } from '@testing-library/react'
import { describe, expect, it, vi } from 'vitest'
import { AppearanceProvider, useAppearance } from '../src/components/AppearanceProvider'

function Foundation() {
  const { appearance, setAppearance } = useAppearance()
  return (
    <div data-foundation-check>
      <span>{appearance}</span>
      <Button label="Use dark" variant="primary" onClick={() => setAppearance('dark')} />
    </div>
  )
}

describe('ASTRYX foundation and appearance', () => {
  it('mounts neutralTheme and persists only the appearance preference', async () => {
    render(<AppearanceProvider><Foundation /></AppearanceProvider>)
    expect(screen.getByText('system')).toBeTruthy()
    fireEvent.click(screen.getByRole('button', { name: 'Use dark' }))
    await waitFor(() => expect(window.localStorage.getItem('vectorless:appearance')).toBe('dark'))
    expect(document.querySelector('[data-foundation-check] button')).toBeTruthy()
    expect(Object.keys(window.localStorage)).toEqual(['vectorless:appearance'])
  })

  it('restores a saved preference without overwriting it during hydration', async () => {
    window.localStorage.setItem('vectorless:appearance', 'dark')
    const setItem = vi.spyOn(Storage.prototype, 'setItem')

    render(<AppearanceProvider><Foundation /></AppearanceProvider>)

    await waitFor(() => expect(screen.getByText('dark')).toBeTruthy())
    expect(setItem).not.toHaveBeenCalled()
    expect(window.localStorage.getItem('vectorless:appearance')).toBe('dark')
    setItem.mockRestore()
  })

  it('falls back to system for an invalid stored value', () => {
    window.localStorage.setItem('vectorless:appearance', 'sepia')

    render(<AppearanceProvider><Foundation /></AppearanceProvider>)

    expect(screen.getByText('system')).toBeTruthy()
    expect(window.localStorage.getItem('vectorless:appearance')).toBe('sepia')
  })

  it('falls back to system when storage is inaccessible', () => {
    const getItem = vi.spyOn(Storage.prototype, 'getItem')
      .mockImplementation(() => { throw new Error('Storage is unavailable') })

    render(<AppearanceProvider><Foundation /></AppearanceProvider>)

    expect(screen.getByText('system')).toBeTruthy()
    getItem.mockRestore()
  })

  it('changes the current appearance when persistence fails', () => {
    const setItem = vi.spyOn(Storage.prototype, 'setItem')
      .mockImplementation(() => { throw new Error('Storage is unavailable') })

    render(<AppearanceProvider><Foundation /></AppearanceProvider>)
    fireEvent.click(screen.getByRole('button', { name: 'Use dark' }))

    expect(screen.getByText('dark')).toBeTruthy()
    setItem.mockRestore()
  })
})
