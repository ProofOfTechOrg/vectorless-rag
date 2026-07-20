import { defineConfig } from 'vitest/config'
import viteReact from '@vitejs/plugin-react'

export default defineConfig({
  plugins: [viteReact()],
  test: {
    environment: 'jsdom',
    include: ['tests/**/*.test.{ts,tsx}'],
    setupFiles: ['./tests/setup.ts'],
  },
})
