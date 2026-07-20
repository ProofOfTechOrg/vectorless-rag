import { createFileRoute } from '@tanstack/react-router'
import { jobSchema } from '../lib/contracts'
import { apiFetch } from '../server/api.server'
import { assertRequestOrigin } from '../server/session.server'

export const Route = createFileRoute('/api/documents/upload')({
  server: {
    handlers: {
      POST: async ({ request }) => {
        assertRequestOrigin(request)
        const incoming = await request.formData()
        const file = incoming.get('file')
        if (!(file instanceof File) || file.type !== 'application/pdf') {
          return Response.json({ detail: 'A PDF file is required' }, { status: 422 })
        }
        const form = new FormData()
        form.set('file', file, file.name)
        const upstream = await apiFetch('/v1/admin/documents', {
          method: 'POST',
          body: form,
        }, request.signal)
        return Response.json(jobSchema.parse(await upstream.json()), { status: upstream.status })
      },
    },
  },
})
