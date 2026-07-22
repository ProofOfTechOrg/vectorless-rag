import type { ZodType } from 'zod'
import { getRequest } from '@tanstack/react-start/server'
import { apiErrorSchema, identitySchema } from '../lib/contracts'
import { clearSession, requireSession } from './session.server'

class ApiError extends Error {
  constructor(
    message: string,
    readonly status: number,
  ) {
    super(message)
  }
}

function apiUrl(path: string): string {
  const base = process.env.VECTORLESS_API_URL ?? 'http://127.0.0.1:8000'
  return new URL(path, `${base.replace(/\/$/, '')}/`).toString()
}

export async function apiFetch(
  path: string,
  init: RequestInit = {},
  signal?: AbortSignal,
): Promise<Response> {
  const session = requireSession()
  const headers = new Headers(init.headers)
  headers.set('X-API-Key', session.apiKey)
  const requestSignal = signal ?? init.signal ?? getRequest().signal
  const response = await fetch(apiUrl(path), { ...init, headers, signal: requestSignal })
  if (response.status === 401) clearSession()
  if (!response.ok) {
    let message = `API request failed (${response.status})`
    try {
      const parsed = apiErrorSchema.parse(await response.clone().json())
      if (typeof parsed.detail === 'string') message = parsed.detail
    } catch {
      // Preserve the safe status-based fallback for malformed upstream errors.
    }
    throw new ApiError(message, response.status)
  }
  return response
}

export async function apiJson<T>(
  path: string,
  schema: ZodType<T>,
  init: RequestInit = {},
  signal?: AbortSignal,
): Promise<T> {
  const response = await apiFetch(path, init, signal)
  return schema.parse(await response.json())
}

export async function validateApiKey(apiKey: string) {
  const response = await fetch(apiUrl('/v1/auth/me'), {
    headers: { 'X-API-Key': apiKey },
    signal: getRequest().signal,
  })
  if (!response.ok) throw new ApiError(response.status === 401 ? 'Invalid API key' : 'API unavailable', response.status)
  return identitySchema.parse(await response.json())
}
