// @vitest-environment node
import { afterEach, describe, expect, it, vi } from 'vitest'
import { verifyProductionAssets } from '../verify-production-assets.mjs'

function response(body: string, url: string, init: ResponseInit = {}) {
  const value = new Response(body, init)
  Object.defineProperty(value, 'url', { value: url })
  return value
}

afterEach(() => vi.unstubAllGlobals())

describe('production asset verification', () => {
  it('accepts same-origin SSR stylesheets and module scripts', async () => {
    const fetchMock = vi.fn(async (input: URL | RequestInfo) => {
      const url = String(input)
      if (url === 'http://production.test/connect') {
        return response(
          '<link rel="stylesheet" href="/assets/app.css"><script type="module" src="/assets/app.js"></script>',
          url,
          { headers: { 'content-type': 'text/html; charset=utf-8' } },
        )
      }
      if (url === 'http://production.test/assets/app.css') {
        return response('body { color: black; }', url, {
          headers: { 'content-type': 'text/css; charset=utf-8' },
        })
      }
      if (url === 'http://production.test/assets/app.js') {
        return response('export {}', url, {
          headers: { 'content-type': 'text/javascript; charset=utf-8' },
        })
      }
      return response('', url, { status: 404 })
    })
    vi.stubGlobal('fetch', fetchMock)

    const assets = await verifyProductionAssets(new URL('http://production.test'))

    expect(assets).toEqual({
      stylesheets: ['http://production.test/assets/app.css'],
      scripts: ['http://production.test/assets/app.js'],
    })
    expect(fetchMock).toHaveBeenCalledTimes(3)
  })

  it('rejects a missing SSR stylesheet', async () => {
    vi.stubGlobal('fetch', vi.fn(async (input: URL | RequestInfo) => {
      const url = String(input)
      if (url === 'http://production.test/connect') {
        return response('<link rel="stylesheet" href="/assets/missing.css">', url, {
          headers: { 'content-type': 'text/html; charset=utf-8' },
        })
      }
      return response('', url, { status: 404 })
    }))

    await expect(
      verifyProductionAssets(new URL('http://production.test')),
    ).rejects.toThrow('returned HTTP 404, expected 200')
  })
})
