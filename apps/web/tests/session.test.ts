// @vitest-environment node
import { describe, expect, it } from 'vitest'
import { decryptSession, encryptSession, originMatches, sessionCookieName } from '../src/server/session.server'

const secret = Buffer.alloc(32, 7)
const identity = { name: 'test-key', scopes: ['chat'] as const }

describe('encrypted session cookie', () => {
  it('round-trips without exposing the API key', () => {
    const token = encryptSession({ apiKey: 'vrag_secret_value', identity: { ...identity, scopes: [...identity.scopes] }, expiresAt: 2_000 }, secret)
    expect(token).not.toContain('vrag_secret_value')
    expect(decryptSession(token, secret, 1_000)?.identity.name).toBe('test-key')
  })

  it('rejects tampering and expiry', () => {
    const token = encryptSession({ apiKey: 'secret', identity: { ...identity, scopes: [...identity.scopes] }, expiresAt: 2_000 }, secret)
    const tagStart = token.lastIndexOf('.') + 1
    const tampered = `${token.slice(0, tagStart)}${token[tagStart] === 'A' ? 'B' : 'A'}${token.slice(tagStart + 1)}`
    expect(decryptSession(tampered, secret, 1_000)).toBeNull()
    expect(decryptSession(token, secret, 2_001)).toBeNull()
  })

  it('uses a __Host cookie only in production', () => {
    expect(sessionCookieName(true)).toBe('__Host-vectorless-session')
    expect(sessionCookieName(false)).toBe('vectorless-session')
  })
})

describe('origin checks', () => {
  it('requires the exact scheme, host, and port', () => {
    expect(originMatches('https://rag.example.com', 'https://rag.example.com')).toBe(true)
    expect(originMatches('http://rag.example.com', 'https://rag.example.com')).toBe(false)
    expect(originMatches('https://rag.example.com:444', 'https://rag.example.com')).toBe(false)
    expect(originMatches(null, 'https://rag.example.com')).toBe(false)
  })
})
