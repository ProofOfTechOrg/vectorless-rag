/* global AbortSignal, URL, console, fetch, process, setTimeout */
import { spawn } from 'node:child_process'
import { once } from 'node:events'
import { fileURLToPath } from 'node:url'
import net from 'node:net'
import path from 'node:path'

const packageRoot = path.resolve(path.dirname(fileURLToPath(import.meta.url)), '..')
const stylesheetContentTypes = new Set(['text/css'])
const scriptContentTypes = new Set([
  'application/ecmascript',
  'application/javascript',
  'text/ecmascript',
  'text/javascript',
])

function attribute(tag, name) {
  const match = new RegExp(`\\b${name}\\s*=\\s*(?:"([^"]*)"|'([^']*)'|([^\\s"'=<>]+))`, 'i')
    .exec(tag)
  return match?.[1] ?? match?.[2] ?? match?.[3] ?? null
}

function sameOriginUrl(value, documentUrl) {
  if (!value) return null
  const candidate = new URL(value, documentUrl)
  return candidate.origin === documentUrl.origin ? candidate : null
}

export function extractProductionAssets(html, documentUrl) {
  const stylesheets = new Set()
  const scripts = new Set()
  for (const match of html.matchAll(/<link\b[^>]*>/gi)) {
    const rel = attribute(match[0], 'rel')
    if (!rel?.toLowerCase().split(/\s+/).includes('stylesheet')) continue
    const url = sameOriginUrl(attribute(match[0], 'href'), documentUrl)
    if (url) stylesheets.add(url.href)
  }
  for (const match of html.matchAll(/<script\b[^>]*>/gi)) {
    if (attribute(match[0], 'type')?.trim().toLowerCase() !== 'module') continue
    const url = sameOriginUrl(attribute(match[0], 'src'), documentUrl)
    if (url) scripts.add(url.href)
  }
  return {
    stylesheets: [...stylesheets],
    scripts: [...scripts],
  }
}

async function requireAsset(url, kind) {
  const response = await fetch(url)
  if (response.status !== 200) {
    throw new Error(`${kind} asset ${url} returned HTTP ${response.status}, expected 200`)
  }
  const contentType = response.headers.get('content-type')?.split(';', 1)[0]?.trim().toLowerCase()
  const allowed = kind === 'stylesheet' ? stylesheetContentTypes : scriptContentTypes
  if (!contentType || !allowed.has(contentType)) {
    throw new Error(`${kind} asset ${url} returned content type ${contentType ?? '<missing>'}`)
  }
}

export async function verifyProductionAssets(baseUrl) {
  const connectUrl = new URL('/connect', baseUrl)
  const response = await fetch(connectUrl)
  if (response.status !== 200) {
    throw new Error(`/connect returned HTTP ${response.status}, expected 200`)
  }
  const documentUrl = new URL(response.url)
  const assets = extractProductionAssets(await response.text(), documentUrl)
  if (assets.stylesheets.length === 0) {
    throw new Error('/connect did not reference a same-origin stylesheet')
  }
  await Promise.all([
    ...assets.stylesheets.map((url) => requireAsset(url, 'stylesheet')),
    ...assets.scripts.map((url) => requireAsset(url, 'module script')),
  ])
  return assets
}

async function reservePort() {
  const server = net.createServer()
  server.unref()
  server.listen(0, '127.0.0.1')
  await once(server, 'listening')
  const address = server.address()
  if (!address || typeof address === 'string') throw new Error('failed to reserve a local port')
  const { port } = address
  server.close()
  await once(server, 'close')
  return port
}

function signalProcess(child, signal) {
  if (!child.pid) return
  try {
    if (process.platform === 'win32') child.kill(signal)
    else process.kill(-child.pid, signal)
  } catch (error) {
    if (error?.code !== 'ESRCH') throw error
  }
}

async function stopServer(child) {
  if (child.exitCode !== null || child.signalCode !== null) return
  if (!child.pid) return
  signalProcess(child, 'SIGTERM')
  await Promise.race([once(child, 'exit').catch(() => undefined), new Promise((resolve) => {
    setTimeout(resolve, 3_000)
  })])
  if (child.exitCode === null && child.signalCode === null) {
    signalProcess(child, 'SIGKILL')
    await once(child, 'exit').catch(() => undefined)
  }
}

async function waitForServer(child, connectUrl, output) {
  const deadline = Date.now() + 15_000
  while (Date.now() < deadline) {
    if (child.exitCode !== null || child.signalCode !== null) {
      throw new Error(`production server exited before it was ready\n${output()}`)
    }
    try {
      await fetch(connectUrl, { signal: AbortSignal.timeout(1_000) })
      return
    } catch {
      await new Promise((resolve) => {
        setTimeout(resolve, 100)
      })
    }
  }
  throw new Error(`production server did not become ready\n${output()}`)
}

async function startProductionServer() {
  const port = await reservePort()
  const baseUrl = new URL(`http://127.0.0.1:${port}`)
  const executable = path.join(packageRoot, 'node_modules', '.bin', 'srvx')
  const child = spawn(
    executable,
    ['serve', '--prod', '--static=../client', '--entry=dist/server/server.js'],
    {
      cwd: packageRoot,
      detached: process.platform !== 'win32',
      env: {
        ...process.env,
        APP_ORIGIN: baseUrl.origin,
        HOST: '127.0.0.1',
        NODE_ENV: 'production',
        PORT: String(port),
        SESSION_SECRET: process.env.SESSION_SECRET ?? '00'.repeat(32),
      },
      stdio: ['ignore', 'pipe', 'pipe'],
    },
  )
  let output = ''
  const appendOutput = (chunk) => {
    output = `${output}${chunk}`.slice(-8_000)
  }
  child.stdout.on('data', appendOutput)
  child.stderr.on('data', appendOutput)
  child.on('error', appendOutput)
  try {
    await waitForServer(child, new URL('/connect', baseUrl), () => output)
    return { baseUrl, child }
  } catch (error) {
    await stopServer(child)
    throw error
  }
}

async function main() {
  const suppliedBaseUrl = process.argv[2]
  let spawned
  try {
    const baseUrl = suppliedBaseUrl ? new URL(suppliedBaseUrl) : (spawned = await startProductionServer()).baseUrl
    const assets = await verifyProductionAssets(baseUrl)
    console.log(
      `Verified ${assets.stylesheets.length} stylesheet(s) and ${assets.scripts.length} module script(s) from ${new URL('/connect', baseUrl)}`,
    )
  } finally {
    if (spawned) await stopServer(spawned.child)
  }
}

if (fileURLToPath(import.meta.url) === path.resolve(process.argv[1] ?? '')) {
  main().catch((error) => {
    console.error(error instanceof Error ? error.message : error)
    process.exitCode = 1
  })
}
