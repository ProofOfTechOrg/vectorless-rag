import { defineConfig } from 'vitest/config'
import viteReact from '@vitejs/plugin-react'

export default defineConfig({
  plugins: [viteReact()],
  test: {
    environment: 'jsdom',
    include: ['tests/**/*.test.{ts,tsx}', 'scripts/__tests__/**/*.test.ts'],
    setupFiles: ['./tests/setup.ts'],
  },
})
