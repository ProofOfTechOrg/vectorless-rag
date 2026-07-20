import { expect, test } from '@playwright/test'

test.skip(!!process.env.PLAYWRIGHT_LIVE, 'Deterministic mock coverage is separate from live verification')

const key = process.env.PLAYWRIGHT_API_KEY ?? 'vrag_test_full'

async function waitForHydration(page: import('@playwright/test').Page) {
  await expect(page.locator('html')).toHaveAttribute('data-hydrated', 'true')
}

async function connectToChat(page: import('@playwright/test').Page) {
  await page.goto('/connect')
  await expect(page.getByRole('button', { name: 'Connect' })).toBeEnabled()
  await page.getByLabel('API key').fill(key)
  await page.getByRole('button', { name: 'Connect' }).click()
  await expect(page.getByRole('heading', { name: 'Grounded research chat' })).toBeVisible()
}

async function expectAlignedChatColumns(page: import('@playwright/test').Page) {
  const columns = await page.evaluate(() => {
    const selectors = [
      '[data-testid="chat-intro-column"]',
      '[data-testid="chat-message-list"]',
      '[data-testid="chat-composer"]',
    ]
    return selectors.map((selector) => {
      const element = document.querySelector<HTMLElement>(selector)
      if (!element) throw new Error(`Missing chat column: ${selector}`)
      const rect = element.getBoundingClientRect()
      return { center: rect.left + rect.width / 2, width: rect.width }
    })
  })

  expect(Math.max(...columns.map(({ center }) => center)) - Math.min(...columns.map(({ center }) => center))).toBeLessThanOrEqual(1)
  for (const column of columns) {
    expect(column.width).toBeLessThanOrEqual(42 * 16 + 1)
    expect(column.width).toBeLessThanOrEqual((page.viewportSize()?.width ?? 0) + 1)
  }
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

  await page.evaluate(() => Object.assign(window, { __vectorlessSpaMarker: true }))
  await page.getByRole('link', { name: 'Documents', exact: true }).click()
  await expect(page.getByRole('heading', { name: 'Documents' })).toBeVisible()
  expect(await page.evaluate(() => '__vectorlessSpaMarker' in window)).toBe(true)
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

test('keeps the chat on one centered reading axis at desktop and mobile sizes', async ({ page }) => {
  await page.setViewportSize({ width: 1440, height: 1000 })
  await connectToChat(page)

  await expect(page.getByRole('heading', { name: 'Ask the indexed corpus' })).toBeVisible()
  const initialLayout = await page.evaluate(() => {
    const chat = document.querySelector<HTMLElement>('[data-testid="chat-page"]')
    const shellContent = document.querySelector<HTMLElement>('#astryx-app-shell-main')
    const heading = document.querySelector<HTMLElement>('[data-testid="chat-page"] h1')
    const input = document.querySelector<HTMLElement>('[aria-label="Message input"]')
    const supporting = document.querySelector<HTMLElement>('.chat-status')
    const composer = document.querySelector<HTMLElement>('[data-testid="chat-composer"]')
    const layout = document.querySelector<HTMLElement>('[data-testid="chat-layout"]')
    if (!chat || !shellContent || !heading || !input || !supporting || !composer || !layout) {
      throw new Error('Chat layout did not render all required elements')
    }

    const probe = document.createElement('span')
    probe.style.fontSize = 'var(--font-size-lg)'
    chat.append(probe)
    const large = getComputedStyle(probe).fontSize
    probe.style.fontSize = 'var(--font-size-base)'
    const base = getComputedStyle(probe).fontSize
    probe.remove()

    const chatRect = chat.getBoundingClientRect()
    const shellRect = shellContent.getBoundingClientRect()
    const dockInner = composer.parentElement
    const dock = dockInner?.parentElement
    const dockContainer = dock?.parentElement

    return {
      base,
      body: getComputedStyle(input).fontSize,
      bottomGap: Math.abs(shellRect.bottom - chatRect.bottom),
      dockBackgrounds: [dockInner, dock, dockContainer].map((element) => element ? getComputedStyle(element).backgroundColor : null),
      heading: Number.parseFloat(getComputedStyle(heading).fontSize),
      large,
      pageBackground: getComputedStyle(chat).backgroundColor,
      supporting: getComputedStyle(supporting).fontSize,
    }
  })
  expect(initialLayout.bottomGap).toBeLessThanOrEqual(1)
  expect(initialLayout.heading).toBeGreaterThanOrEqual(24)
  expect(initialLayout.body).toBe(initialLayout.large)
  expect(initialLayout.supporting).toBe(initialLayout.base)
  expect(initialLayout.pageBackground).toBe('rgba(0, 0, 0, 0)')
  expect(initialLayout.dockBackgrounds).toEqual([
    'rgba(0, 0, 0, 0)',
    'rgba(0, 0, 0, 0)',
    'rgba(0, 0, 0, 0)',
  ])

  await page.getByLabel('Message input').fill('What does the cobalt lantern fixture prove?')
  await page.getByLabel('Message input').press('Enter')
  await expect(page.getByText(/cobalt lantern retrieval/)).toBeVisible()
  await expect(page.locator('[data-testid="user-bubble"]')).toHaveAttribute('data-variant', 'filled')
  await expect(page.locator('[data-testid="assistant-bubble"]')).toHaveAttribute('data-variant', 'ghost')

  const assistantMessage = page.locator('article[data-sender="assistant"]').last()
  await expect(assistantMessage.getByText('documents', { exact: true })).toBeVisible()
  await expect(assistantMessage.locator('.citation-list a')).toHaveAttribute(
    'href',
    '/documents/11111111-1111-4111-8111-111111111111',
  )
  await expect(assistantMessage.getByRole('button', { name: 'Helpful', exact: true })).toBeVisible()
  await expect(assistantMessage.getByRole('button', { name: 'Not helpful', exact: true })).toBeVisible()
  await expect(page.locator('.chat-status')).toHaveText('Ready')
  await expectAlignedChatColumns(page)

  await page.getByRole('link', { name: 'Settings', exact: true }).click()
  await page.getByLabel('Color mode').click()
  await page.getByText('Dark', { exact: true }).click()
  await page.goBack()
  await expect(page.getByRole('heading', { name: 'Grounded research chat' })).toBeVisible()
  await expectAlignedChatColumns(page)
  await expect(page.locator('[data-testid="chat-layout"]')).toHaveCSS('background-color', 'rgba(0, 0, 0, 0)')

  await page.setViewportSize({ width: 390, height: 844 })
  await expectAlignedChatColumns(page)
  expect(await page.evaluate(() => document.documentElement.scrollWidth)).toBeLessThanOrEqual(390)

  const mobileBounds = await page.evaluate(() => {
    const chat = document.querySelector<HTMLElement>('[data-testid="chat-page"]')
    const shellContent = document.querySelector<HTMLElement>('#astryx-app-shell-main')
    if (!chat || !shellContent) throw new Error('Missing mobile chat layout')
    const chatRect = chat.getBoundingClientRect()
    const shellRect = shellContent.getBoundingClientRect()
    return {
      bottomGap: Math.abs(shellRect.bottom - chatRect.bottom),
      rightOverflow: Math.max(0, chatRect.right - window.innerWidth),
    }
  })
  expect(mobileBounds.bottomGap).toBeLessThanOrEqual(1)
  expect(mobileBounds.rightOverflow).toBe(0)
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

test('persists a failed stream with a safe retryable message', async ({ page }) => {
  await page.goto('/connect')
  await expect(page.getByRole('button', { name: 'Connect' })).toBeEnabled()
  await page.getByLabel('API key').fill(key)
  await page.getByRole('button', { name: 'Connect' }).click()

  await page.getByLabel('Message input').fill('Make this fail')
  await page.getByLabel('Message input').press('Enter')

  await expect(page).toHaveURL(/\/chat\/thread-failed$/)
  await expect(page.getByText('The API could not complete this run.')).toBeVisible()
  await expect(page.getByRole('button', { name: 'Retry' })).toBeVisible()
  await expect(page.locator('.chat-status')).toHaveText('Ready')
})
