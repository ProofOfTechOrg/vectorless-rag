import { expect, test } from '@playwright/test'

test.skip(!!process.env.PLAYWRIGHT_LIVE, 'Deterministic mock coverage is separate from live verification')

const key = process.env.PLAYWRIGHT_API_KEY ?? 'vrag_test_full'

async function waitForHydration(page: import('@playwright/test').Page) {
  await expect(page.locator('html')).toHaveAttribute('data-hydrated', 'true')
}

test('connects through the UI and exercises the operator workflows', async ({ page }) => {
  await page.goto('/connect')
  const connectButton = page.getByRole('button', { name: 'Connect' })
  await expect(connectButton).toBeEnabled()
  await page.getByLabel('API key').fill(key)
  await connectButton.click()
  await expect(page.getByRole('heading', { name: 'Grounded research chat' })).toBeVisible()

  await page.getByLabel('Message input').fill('What does the cobalt lantern fixture prove?')
  await page.getByLabel('Message input').press('Enter')
  await expect(page.getByText(/cobalt lantern retrieval/)).toBeVisible()
  await expect(page.getByText('documents', { exact: true })).toBeVisible()
  await expect(page.getByLabel('Recent threads')).toContainText('Cobalt lantern question')
  await expect(page.locator('.citation-list a')).toHaveAttribute(
    'href',
    '/documents/11111111-1111-4111-8111-111111111111',
  )
  await page.getByRole('button', { name: 'Helpful', exact: true }).click()

  await page.goto('/documents')
  await waitForHydration(page)
  const fixtureDocument = page.getByRole('link', { name: 'Deterministic Vectorless Retrieval' })
  await expect(fixtureDocument).toBeVisible()
  await expect(fixtureDocument).toHaveAttribute('href', /\/documents\/[0-9a-f-]+$/)
  await page.goto(await fixtureDocument.getAttribute('href') as string)
  await expect(page.getByText('Citation landing page')).toBeVisible()

  await page.goto('/ingestion')
  await waitForHydration(page)
  await page.locator('input[type="file"]').setInputFiles({
    name: 'fixture.pdf',
    mimeType: 'application/pdf',
    buffer: Buffer.from('%PDF-1.4\n1 0 obj<</Type/Catalog>>endobj\n%%EOF'),
  })
  await page.getByRole('button', { name: 'Upload and queue' }).click()
  await expect(page.getByText('completed', { exact: true })).toBeVisible()

  await page.goto('/settings')
  await waitForHydration(page)
  await expect(page.getByText('playwright-full', { exact: true })).toBeVisible()
  await page.getByLabel('Color mode').click()
  await page.getByText('Dark', { exact: true }).click()
  await page.getByRole('button', { name: 'Log out' }).click()
  await expect(page.getByRole('heading', { name: 'Connect to Vectorless RAG' })).toBeVisible()
})

test('redirects protected routes to connect without a session', async ({ page }) => {
  await page.goto('/documents')
  await expect(page).toHaveURL(/\/connect$/)
})

test('returns an existing documents-only session to its scoped home', async ({ page }) => {
  await page.goto('/connect')
  await expect(page.getByRole('button', { name: 'Connect' })).toBeEnabled()
  await page.getByLabel('API key').fill('mock-documents-only')
  await page.getByRole('button', { name: 'Connect' }).click()
  await expect(page).toHaveURL(/\/documents$/)
  await expect(page.getByRole('heading', { name: 'Documents' })).toBeVisible()

  await page.goto('/connect')
  await expect(page).toHaveURL(/\/documents$/)
})
