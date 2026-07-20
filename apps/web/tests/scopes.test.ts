import { describe, expect, it } from 'vitest'
import { homeForScopes } from '../src/lib/scopes'

describe('scope-gated navigation', () => {
  it('selects the first authorized operator surface', () => {
    expect(homeForScopes(['chat', 'documents:read'])).toBe('/chat')
    expect(homeForScopes(['documents:read'])).toBe('/documents')
    expect(homeForScopes(['admin:ingest'])).toBe('/ingestion')
    expect(homeForScopes([])).toBe('/settings')
  })
})
