// @vitest-environment node
import { execFileSync } from 'node:child_process'
import { chmodSync, mkdtempSync, rmSync, writeFileSync } from 'node:fs'
import { tmpdir } from 'node:os'
import { join, resolve } from 'node:path'
import { afterEach, describe, expect, it } from 'vitest'

const SCRIPT = resolve(process.cwd(), 'scripts/resolve-react-doctor-diff-base.sh')
const tmpDirs: string[] = []

function tempDir(): string {
  const dir = mkdtempSync(join(tmpdir(), 'rd-diff-base-'))
  tmpDirs.push(dir)
  return dir
}

function stubGh(body: string): string {
  const dir = tempDir()
  const ghPath = join(dir, 'gh')
  writeFileSync(ghPath, `#!/bin/sh\n${body}\n`)
  chmodSync(ghPath, 0o755)
  return dir
}

function gitRepo(seedRefs: (run: (...args: string[]) => void) => void): string {
  const dir = tempDir()
  const run = (...args: string[]) => {
    execFileSync('git', ['-C', dir, ...args], { stdio: 'pipe' })
  }
  run('init', '-q')
  run('-c', 'user.email=t@t', '-c', 'user.name=t', 'commit', '--allow-empty', '-q', '-m', 'c0')
  seedRefs(run)
  return dir
}

function runResolver(path: string, cwd?: string): string {
  return execFileSync('/bin/sh', [SCRIPT], {
    cwd,
    env: { ...process.env, PATH: path },
    encoding: 'utf8',
  }).trim()
}

const withGh = (ghDir: string): string => `${ghDir}:${process.env.PATH}`

describe('resolve-react-doctor-diff-base.sh', () => {
  afterEach(() => {
    for (const dir of tmpDirs.splice(0)) rmSync(dir, { recursive: true, force: true })
  })

  it("falls back to 'parent' when gh is not installed", () => {
    expect(runResolver(tempDir())).toBe('parent')
  })

  it("falls back to 'parent' when gh exits non-zero", () => {
    expect(runResolver(withGh(stubGh('exit 1')))).toBe('parent')
  })

  it("falls back to 'parent' when gh yields no open PR base", () => {
    expect(runResolver(withGh(stubGh(':')))).toBe('parent')
  })

  it("falls back to 'parent' for an unsafe base ref", () => {
    const ghDir = stubGh("printf '%s\\n' 'evil; rm -rf /'")
    expect(runResolver(withGh(ghDir))).toBe('parent')
  })

  it("falls back to 'parent' when the PR base ref is missing", () => {
    expect(runResolver(withGh(stubGh('echo zzz-nonexistent-branch')))).toBe('parent')
  })

  it("prefers 'origin/<base>' when the remote-tracking ref exists", () => {
    const repo = gitRepo((run) => {
      run('update-ref', 'refs/remotes/origin/featbase', 'HEAD')
    })
    expect(runResolver(withGh(stubGh('echo featbase')), repo)).toBe('origin/featbase')
  })

  it("uses the local '<base>' when no remote-tracking ref exists", () => {
    const repo = gitRepo((run) => {
      run('branch', 'localbase')
    })
    expect(runResolver(withGh(stubGh('echo localbase')), repo)).toBe('localbase')
  })
})
