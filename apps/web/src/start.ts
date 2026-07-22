import { createCsrfMiddleware, createMiddleware, createStart } from '@tanstack/react-start'
import { setResponseHeader } from '@tanstack/react-start/server'

const csrfMiddleware = createCsrfMiddleware({
  origin: process.env.APP_ORIGIN ?? 'http://localhost:3001',
  filter: (context) => context.handlerType === 'serverFn',
})

const noStoreServerFunctions = createMiddleware({ type: 'function' }).server(async ({ next }) => {
  setResponseHeader('Cache-Control', 'private, no-store')
  return next()
})

export const startInstance = createStart(() => ({
  requestMiddleware: [csrfMiddleware],
  functionMiddleware: [noStoreServerFunctions],
}))
