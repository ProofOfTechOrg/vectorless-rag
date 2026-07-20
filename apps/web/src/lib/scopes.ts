import type { Identity } from './contracts'

export function homeForScopes(scopes: Identity['scopes']): '/chat' | '/documents' | '/ingestion' | '/settings' {
  if (scopes.includes('chat')) return '/chat'
  if (scopes.includes('documents:read')) return '/documents'
  if (scopes.includes('admin:ingest')) return '/ingestion'
  return '/settings'
}
