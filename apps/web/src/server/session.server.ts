import { createCipheriv, createDecipheriv, randomBytes } from 'node:crypto'
import { getRequestHeader, setResponseHeader } from '@tanstack/react-start/server'
import { z } from 'zod'
import { identitySchema, type Identity } from '../lib/contracts'

const SESSION_SECONDS = 8 * 60 * 60
const sessionPayloadSchema = z.object({
  apiKey: z.string().min(1),
  identity: identitySchema,
  expiresAt: z.number().int(),
})
export type SessionPayload = z.infer<typeof sessionPayloadSchema>

export function sessionCookieName(production = process.env.NODE_ENV === 'production') {
  return production ? '__Host-vectorless-session' : 'vectorless-session'
}

function secretBytes(value = process.env.SESSION_SECRET): Buffer {
  if (!value) throw new Error('SESSION_SECRET is required')
  const bytes = /^[a-f\d]{64}$/i.test(value)
    ? Buffer.from(value, 'hex')
    : Buffer.from(value, 'base64')
  if (bytes.length !== 32) throw new Error('SESSION_SECRET must encode exactly 32 bytes')
  return bytes
}

export function encryptSession(
  payload: SessionPayload,
  secret = secretBytes(),
): string {
  const iv = randomBytes(12)
  const cipher = createCipheriv('aes-256-gcm', secret, iv)
  const ciphertext = Buffer.concat([
    cipher.update(JSON.stringify(payload), 'utf8'),
    cipher.final(),
  ])
  return ['v1', iv.toString('base64url'), ciphertext.toString('base64url'), cipher.getAuthTag().toString('base64url')].join('.')
}

export function decryptSession(
  token: string,
  secret = secretBytes(),
  now = Date.now(),
): SessionPayload | null {
  try {
    const [version, ivValue, ciphertextValue, tagValue] = token.split('.')
    if (version !== 'v1' || !ivValue || !ciphertextValue || !tagValue) return null
    const decipher = createDecipheriv('aes-256-gcm', secret, Buffer.from(ivValue, 'base64url'))
    decipher.setAuthTag(Buffer.from(tagValue, 'base64url'))
    const plaintext = Buffer.concat([
      decipher.update(Buffer.from(ciphertextValue, 'base64url')),
      decipher.final(),
    ]).toString('utf8')
    const payload = sessionPayloadSchema.parse(JSON.parse(plaintext))
    return payload.expiresAt > now ? payload : null
  } catch {
    return null
  }
}

function readCookie(header: string | undefined, name: string): string | null {
  if (!header) return null
  for (const part of header.split(/;\s*/)) {
    const separator = part.indexOf('=')
    if (separator > 0 && part.slice(0, separator) === name) return part.slice(separator + 1)
  }
  return null
}

export function readSession(): SessionPayload | null {
  const token = readCookie(getRequestHeader('cookie'), sessionCookieName())
  return token ? decryptSession(token) : null
}

export function issueSession(apiKey: string, identity: Identity): void {
  const expiresAt = Date.now() + SESSION_SECONDS * 1000
  const production = process.env.NODE_ENV === 'production'
  const secure = production ? '; Secure' : ''
  setResponseHeader(
    'Set-Cookie',
    `${sessionCookieName(production)}=${encryptSession({ apiKey, identity, expiresAt })}; HttpOnly${secure}; SameSite=Lax; Path=/; Max-Age=${SESSION_SECONDS}`,
  )
}

export function clearSession(): void {
  const production = process.env.NODE_ENV === 'production'
  const secure = production ? '; Secure' : ''
  setResponseHeader(
    'Set-Cookie',
    `${sessionCookieName(production)}=; HttpOnly${secure}; SameSite=Lax; Path=/; Max-Age=0`,
  )
}

export function requireSession(): SessionPayload {
  const session = readSession()
  if (!session) throw new Error('Authentication required')
  return session
}

export function originMatches(origin: string | null, appOrigin: string): boolean {
  try {
    return origin !== null && new URL(origin).origin === new URL(appOrigin).origin
  } catch {
    return false
  }
}

export function assertRequestOrigin(request: Request): void {
  assertOrigin(request.headers.get('origin'))
}

export function assertCurrentRequestOrigin(): void {
  assertOrigin(getRequestHeader('origin') ?? null)
}

function assertOrigin(origin: string | null): void {
  const appOrigin = process.env.APP_ORIGIN ?? 'http://localhost:3001'
  if (!originMatches(origin, appOrigin)) {
    throw new Response('Forbidden origin', { status: 403 })
  }
}
