import { Button } from '@astryxdesign/core/Button'
import { fireEvent, render, screen, waitFor } from '@testing-library/react'
import { describe, expect, it } from 'vitest'
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
})
