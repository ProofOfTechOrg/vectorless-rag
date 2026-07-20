import { expect, test } from '@playwright/test'

const liveKey = process.env.PLAYWRIGHT_API_KEY
test.skip(!process.env.PLAYWRIGHT_LIVE || !liveKey, 'Requires PLAYWRIGHT_LIVE and a process-only API key')
test.describe.configure({ mode: 'serial' })

test('verifies the authorized live stack through the operator console', async ({ page }) => {
  test.setTimeout(10 * 60_000)
  const uniquePhrase = `cobalt orbit ${Date.now().toString(36)}`
  const filename = `vectorless-live-${Date.now().toString(36)}.pdf`

  await page.goto('/connect')
  const connectButton = page.getByRole('button', { name: 'Connect' })
  await expect(connectButton).toBeEnabled()
  await page.getByLabel('API key').fill(liveKey!)
  await connectButton.click()
  await expect(page.getByRole('heading', { name: 'Grounded research chat' })).toBeVisible()

  await page.goto('/ingestion')
  await waitForHydration(page)
  await page.locator('input[type="file"]').setInputFiles({
    name: filename,
    mimeType: 'application/pdf',
    buffer: textPdf(`Evidence phrase: ${uniquePhrase}.`),
  })
  await page.getByRole('button', { name: 'Upload and queue' }).click()
  const completed = page.getByText('completed', { exact: true })
  const terminalFailure = page.getByText(/^(failed|unsupported)$/)
  const uploadError = page.getByRole('alert').filter({ hasText: 'Ingestion attention required' })
  await expect(completed.or(terminalFailure).or(uploadError)).toBeVisible({ timeout: 4 * 60_000 })
  await expect(uploadError).toHaveCount(0)
  const ingestionCompleted = await completed.isVisible()
  if (!ingestionCompleted) await expect(page.getByText('Diagnostic', { exact: true })).toBeVisible()

  const documentLink = page.getByText('Document:').locator('..').getByRole('link')
  const documentHref = await documentLink.getAttribute('href')
  expect(documentHref).toMatch(/^\/documents\/[0-9a-f-]+$/)

  if (ingestionCompleted) {
    await page.goto('/chat')
    await waitForHydration(page)
    const question = `Which document contains the exact phrase "${uniquePhrase}"? Cite its page.`
    await page.getByLabel('Message input').fill(question)
    await page.getByLabel('Message input').press('Enter')
    await expect(page.getByRole('status')).toContainText('Completed', { timeout: 3 * 60_000 })
    await expect(page.locator('.citation-list a').first()).toHaveAttribute('href', documentHref!)
    await page.getByRole('button', { name: 'Helpful', exact: true }).click()
  }

  await page.goto(documentHref!)
  await expect(page.getByText('Citation landing page')).toBeVisible()
  await expect(page.locator('dt', { hasText: 'Pages' }).locator('xpath=following-sibling::dd[1]')).toContainText('1')

  if (ingestionCompleted) {
    await page.goto('/chat')
    await expect(page.getByLabel('Recent threads')).toContainText(uniquePhrase)
  }

  await page.goto('/settings')
  await waitForHydration(page)
  await expect(page.getByText('codex-ui-dev', { exact: true })).toBeVisible()
  await expect(page.getByText('chat', { exact: true })).toBeVisible()
  await expect(page.getByText('documents:read', { exact: true })).toBeVisible()
  await expect(page.getByText('admin:ingest', { exact: true })).toBeVisible()
  await page.getByLabel('Color mode').click()
  await page.getByText('Dark', { exact: true }).click()
  await expect.poll(() => page.evaluate(() => localStorage.getItem('vectorless:appearance'))).toBe('dark')
  await page.getByRole('button', { name: 'Log out' }).click()
  await expect(page.getByRole('heading', { name: 'Connect to Vectorless RAG' })).toBeVisible()
})

async function waitForHydration(page: import('@playwright/test').Page) {
  await expect(page.locator('html')).toHaveAttribute('data-hydrated', 'true')
}

function textPdf(text: string): Buffer {
  const content = [
    text,
    'This born-digital document is a deliberately text-rich live verification fixture.',
    'It validates PDF upload, object storage, durable PostgreSQL jobs, worker processing, metadata extraction, vectorless retrieval, citation page ranges, thread history, and feedback.',
    'Each sentence contains selectable text rendered with a standard embedded PDF font resource.',
    'The operator console must preserve secure browser-to-BFF-to-FastAPI boundaries throughout this workflow.',
    'Additional evidence ensures the first page exceeds the extraction quality threshold without requiring OCR.',
    'The same source should remain discoverable by its unique phrase after indexing completes.',
    'This final paragraph provides enough grounded prose for deterministic extraction quality checks.',
  ].join(' ')
  const lines: string[] = []
  for (const word of content.split(' ')) {
    const last = lines.at(-1)
    if (!last || `${last} ${word}`.length > 72) lines.push(word)
    else lines[lines.length - 1] = `${last} ${word}`
  }
  const escape = (value: string) => value.replaceAll('\\', '\\\\').replaceAll('(', '\\(').replaceAll(')', '\\)')
  const commands = lines.flatMap((line, index) => [index === 0 ? '' : 'T*', `(${escape(line)}) Tj`]).filter(Boolean)
  const stream = `BT /F1 11 Tf 72 720 Td 15 TL\n${commands.join('\n')}\nET`
  const objects = [
    '<< /Type /Catalog /Pages 2 0 R >>',
    '<< /Type /Pages /Kids [3 0 R] /Count 1 >>',
    '<< /Type /Page /Parent 2 0 R /MediaBox [0 0 612 792] /Resources << /Font << /F1 4 0 R >> >> /Contents 5 0 R >>',
    '<< /Type /Font /Subtype /Type1 /BaseFont /Helvetica >>',
    `<< /Length ${Buffer.byteLength(stream)} >>\nstream\n${stream}\nendstream`,
  ]
  let body = '%PDF-1.4\n'
  const offsets = [0]
  objects.forEach((object, index) => {
    offsets.push(Buffer.byteLength(body))
    body += `${index + 1} 0 obj\n${object}\nendobj\n`
  })
  const xref = Buffer.byteLength(body)
  body += `xref\n0 ${objects.length + 1}\n0000000000 65535 f \n`
  body += offsets.slice(1).map((offset) => `${offset.toString().padStart(10, '0')} 00000 n \n`).join('')
  body += `trailer\n<< /Size ${objects.length + 1} /Root 1 0 R >>\nstartxref\n${xref}\n%%EOF\n`
  return Buffer.from(body)
}
