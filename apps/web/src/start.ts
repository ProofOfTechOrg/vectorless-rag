import { createCsrfMiddleware, createStart } from '@tanstack/react-start'

const csrfMiddleware = createCsrfMiddleware({
  origin: process.env.APP_ORIGIN ?? 'http://localhost:3001',
  filter: (context) => context.handlerType === 'serverFn',
})

export const startInstance = createStart(() => ({
  requestMiddleware: [csrfMiddleware],
}))
