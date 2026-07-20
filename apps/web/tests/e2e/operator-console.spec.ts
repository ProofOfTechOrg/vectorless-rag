import { expect, test } from '@playwright/test'

test.skip(!!process.env.PLAYWRIGHT_LIVE, 'Deterministic mock coverage is separate from live verification')

const key = process.env.PLAYWRIGHT_API_KEY ?? 'vrag_test_full'
const shellWidth = 80 * 16
const operationalWidth = 64 * 16
const readingWidth = 60 * 16
const chatWidth = 60 * 16

async function waitForHydration(page: import('@playwright/test').Page) {
  await expect(page.locator('html')).toHaveAttribute('data-hydrated', 'true')
}

async function connectToChat(page: import('@playwright/test').Page) {
  await page.goto('/connect')
  await expect(page.getByRole('button', { name: 'Connect' })).toBeEnabled()
  await page.getByLabel('API key').fill(key)
  await page.getByRole('button', { name: 'Connect' }).click()
  await expect(page.getByRole('heading', { name: 'Grounded research chat' })).toBeVisible()
  await waitForHydration(page)
}

async function expectAlignedChatColumns(page: import('@playwright/test').Page) {
  const geometry = await page.evaluate(() => {
    const selectors = [
      '[data-testid="chat-intro-column"]',
      '[data-testid="chat-message-list"], .chat-empty-state',
      '[data-testid="chat-composer"]',
    ]
    const shellContent = document.querySelector<HTMLElement>('#astryx-app-shell-main')
    if (!shellContent) throw new Error('Missing AppShell content')
    const shellRect = shellContent.getBoundingClientRect()
    const columns = selectors.map((selector) => {
      const element = document.querySelector<HTMLElement>(selector)
      if (!element) throw new Error(`Missing chat column: ${selector}`)
      const rect = element.getBoundingClientRect()
      return { center: rect.left + rect.width / 2, width: rect.width }
    })
    return {
      columns,
      shellCenter: shellRect.left + shellRect.width / 2,
      scrollWidth: Math.max(document.documentElement.scrollWidth, document.body.scrollWidth),
      viewportWidth: window.innerWidth,
    }
  })

  expect(Math.max(...geometry.columns.map(({ center }) => center)) - Math.min(...geometry.columns.map(({ center }) => center))).toBeLessThanOrEqual(1)
  for (const column of geometry.columns) {
    expect(Math.abs(column.center - geometry.shellCenter)).toBeLessThanOrEqual(1)
    expect(column.width).toBeLessThanOrEqual(chatWidth + 1)
    expect(column.width).toBeLessThanOrEqual(geometry.viewportWidth + 1)
  }
  expect(geometry.scrollWidth).toBeLessThanOrEqual(geometry.viewportWidth)
}

async function expectCenteredSurface(
  page: import('@playwright/test').Page,
  selector: string,
  maxWidth: number,
) {
  const surface = page.locator(selector).first()
  await expect(surface).toBeVisible()
  const geometry = await surface.evaluate((element) => {
    const shellContent = document.querySelector<HTMLElement>('#astryx-app-shell-main')
    if (!shellContent) throw new Error('Missing AppShell content')
    const rect = element.getBoundingClientRect()
    const shellRect = shellContent.getBoundingClientRect()
    return {
      center: rect.left + rect.width / 2,
      shellCenter: shellRect.left + shellRect.width / 2,
      left: rect.left,
      right: rect.right,
      shellLeft: shellRect.left,
      shellRight: shellRect.right,
      width: rect.width,
      scrollWidth: Math.max(document.documentElement.scrollWidth, document.body.scrollWidth),
      viewportWidth: window.innerWidth,
    }
  })

  expect(Math.abs(geometry.center - geometry.shellCenter)).toBeLessThanOrEqual(1)
  expect(geometry.width).toBeLessThanOrEqual(maxWidth + 1)
  expect(geometry.left).toBeGreaterThanOrEqual(geometry.shellLeft - 1)
  expect(geometry.right).toBeLessThanOrEqual(geometry.shellRight + 1)
  expect(geometry.scrollWidth).toBeLessThanOrEqual(geometry.viewportWidth)
}

async function expectConnectCardGeometry(page: import('@playwright/test').Page) {
  const card = page.locator('.connect-page > .astryx-card')
  await expect(card).toBeVisible()
  const geometry = await card.evaluate((element) => {
    const rect = element.getBoundingClientRect()
    return {
      center: rect.left + rect.width / 2,
      viewportCenter: window.innerWidth / 2,
      width: rect.width,
      scrollWidth: Math.max(document.documentElement.scrollWidth, document.body.scrollWidth),
      viewportWidth: window.innerWidth,
    }
  })

  expect(Math.abs(geometry.center - geometry.viewportCenter)).toBeLessThanOrEqual(1)
  expect(geometry.width).toBeLessThanOrEqual(520 + 1)
  expect(geometry.scrollWidth).toBeLessThanOrEqual(geometry.viewportWidth)
}

async function expectBoundedOperatorShell(page: import('@playwright/test').Page, expectedSideNavWidth: number) {
  const geometry = await page.evaluate(() => {
    const shell = document.querySelector<HTMLElement>('.operator-shell')
    const topNav = document.querySelector<HTMLElement>('[data-testid="operator-top-nav"]')
    const main = document.querySelector<HTMLElement>('#astryx-app-shell-main')
    const sideNav = document.querySelector<HTMLElement>('.astryx-side-nav')
    if (!shell || !topNav || !main) throw new Error('Missing operator layout surface')

    const shellStyle = getComputedStyle(shell)
    const shellRect = shell.getBoundingClientRect()
    const topNavRect = topNav.getBoundingClientRect()
    const mainRect = main.getBoundingClientRect()
    const sideNavRect = sideNav?.getBoundingClientRect()
    const sideNavWidth = Number.parseFloat(shellStyle.getPropertyValue('--operator-side-nav-width'))

    return {
      mainLeft: mainRect.left,
      mainRight: mainRect.right,
      shellCenter: shellRect.left + shellRect.width / 2,
      shellLeft: shellRect.left,
      shellRight: shellRect.right,
      shellWidth: shellRect.width,
      sideNavWidth,
      sideNavLeft: sideNavRect?.left ?? null,
      sideNavRight: sideNavRect?.right ?? null,
      topNavLeft: topNavRect.left,
      topNavRight: topNavRect.right,
      topNavWidth: topNavRect.width,
      viewportCenter: window.innerWidth / 2,
      viewportWidth: window.innerWidth,
      scrollWidth: Math.max(document.documentElement.scrollWidth, document.body.scrollWidth),
    }
  })

  expect(Math.abs(geometry.sideNavWidth - expectedSideNavWidth)).toBeLessThanOrEqual(1)
  expect(Math.abs(geometry.shellCenter - geometry.viewportCenter)).toBeLessThanOrEqual(1)
  expect(geometry.shellWidth).toBeLessThanOrEqual(shellWidth + 1)
  expect(Math.abs(geometry.shellWidth - Math.min(geometry.viewportWidth, shellWidth))).toBeLessThanOrEqual(1)
  expect(Math.abs(geometry.topNavLeft - geometry.shellLeft)).toBeLessThanOrEqual(1)
  expect(Math.abs(geometry.topNavRight - geometry.shellRight)).toBeLessThanOrEqual(1)
  expect(Math.abs(geometry.topNavWidth - geometry.shellWidth)).toBeLessThanOrEqual(1)
  expect(geometry.mainLeft).toBeGreaterThanOrEqual(geometry.shellLeft - 1)
  expect(geometry.mainRight).toBeLessThanOrEqual(geometry.shellRight + 1)
  if (expectedSideNavWidth > 0) {
    expect(geometry.sideNavLeft).not.toBeNull()
    expect(geometry.sideNavRight).not.toBeNull()
    expect(geometry.sideNavLeft as number).toBeGreaterThanOrEqual(geometry.shellLeft - 1)
    expect(geometry.sideNavRight as number).toBeLessThanOrEqual(geometry.shellRight + 1)
  }
  expect(geometry.scrollWidth).toBeLessThanOrEqual(geometry.viewportWidth)
}

async function expectExpandedTypeScale(page: import('@playwright/test').Page) {
  const sizes = await page.evaluate(() => {
    const tokens = ['4xs', '3xs', '2xs', 'xs', 'sm', 'base', 'lg', 'xl', '2xl', '3xl', '4xl', '5xl']
    const probe = document.createElement('span')
    document.body.append(probe)
    const values = tokens.map((token) => {
      probe.style.fontSize = `var(--font-size-${token})`
      return Number.parseFloat(getComputedStyle(probe).fontSize)
    })
    probe.remove()
    return values
  })

  const increase = 2 * 96 / 72
  const neutralScale = [6, 7, 8, 10, 12, 14, 17, 20, 24, 29, 35, 42]
  for (const [index, size] of sizes.entries()) {
    expect(Math.abs(size - (neutralScale[index] + increase))).toBeLessThanOrEqual(0.02)
  }
}

async function navigateFromSideNav(
  page: import('@playwright/test').Page,
  destination: 'Chat' | 'Documents' | 'Ingestion' | 'Settings',
) {
  if ((page.viewportSize()?.width ?? 0) <= 768) {
    await page.getByRole('button', { name: 'Open navigation' }).click()
    await page.getByRole('dialog', { name: 'Navigation' }).getByRole('link', { name: destination, exact: true }).click()
    return
  }
  const link = page.locator('.astryx-side-nav').getByRole('link', { name: destination, exact: true }).first()
  await link.click()
}

async function setAppearance(page: import('@playwright/test').Page, appearance: 'Light' | 'Dark') {
  await navigateFromSideNav(page, 'Settings')
  await expect(page.getByRole('heading', { name: 'Settings', exact: true })).toBeVisible()
  await page.getByLabel('Color mode').click()
  await page.getByText(appearance, { exact: true }).click()
}

async function expectAppWidthTiers(page: import('@playwright/test').Page, sideNavWidth: number) {
  for (const destination of [
    'Documents',
    'Ingestion',
    'Settings',
  ] as const) {
    await navigateFromSideNav(page, destination)
    await expect(page.getByRole('heading', { name: destination, exact: true })).toBeVisible()
    await expectCenteredSurface(page, '.page:not(.page-reading)', operationalWidth)
    await expectBoundedOperatorShell(page, sideNavWidth)
  }

  await navigateFromSideNav(page, 'Chat')
  await expect(page.getByRole('heading', { name: 'Grounded research chat' })).toBeVisible()
  await expectAlignedChatColumns(page)

  await navigateFromSideNav(page, 'Documents')
  await page.getByRole('link', { name: 'Deterministic Vectorless Retrieval' }).first().click()
  await expect(page.getByText('Citation landing page')).toBeVisible()
  await expectCenteredSurface(page, '.page.page-reading', readingWidth)
}

test('connects through the UI and exercises the operator workflows', async ({ page }) => {
  await page.goto('/connect')
  const connectButton = page.getByRole('button', { name: 'Connect' })
  await expect(connectButton).toBeEnabled()
  await page.getByLabel('API key').fill(key)
  await connectButton.click()
  await expect(page.getByRole('heading', { name: 'Grounded research chat' })).toBeVisible()
  await waitForHydration(page)

  await page.getByLabel('Message input').fill('What does the cobalt lantern fixture prove?')
  await page.getByLabel('Message input').press('Enter')
  await expect(page.getByText(/cobalt lantern retrieval/)).toBeVisible()
  await expect(page.getByText('documents', { exact: true })).toBeVisible()
  await expect(page.getByLabel('Recent threads')).toContainText('Cobalt lantern question')
  await expect(page.locator('.citation-list a')).toHaveAttribute(
    'href',
    '/documents/11111111-1111-4111-8111-111111111111',
  )
  await page.getByRole('button', { name: 'Mark answer as helpful', exact: true }).click()
  await expect(page.getByText('Marked helpful', { exact: true })).toBeVisible()

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
  await expect(assistantMessage.getByText('Rate answer', { exact: true })).toBeVisible()
  await expect(assistantMessage.getByRole('button', { name: 'Mark answer as helpful', exact: true })).toBeVisible()
  await expect(assistantMessage.getByRole('button', { name: 'Mark answer as not helpful', exact: true })).toBeVisible()
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

test('surfaces feedback failures and allows a retry', async ({ page }) => {
  await connectToChat(page)
  await page.getByLabel('Message input').fill('What does the cobalt lantern fixture prove?')
  await page.getByLabel('Message input').press('Enter')

  const assistantMessage = page.locator('article[data-sender="assistant"]').last()
  await expect(assistantMessage.getByText(/cobalt lantern retrieval/)).toBeVisible()
  await assistantMessage.getByRole('button', { name: 'Mark answer as not helpful', exact: true }).click()
  await expect(assistantMessage.getByText('Feedback wasn’t saved. Try again.', { exact: true })).toBeVisible()

  const retry = assistantMessage.getByRole('button', { name: 'Mark answer as helpful', exact: true })
  await expect(retry).toBeEnabled()
  await retry.click()
  await expect(assistantMessage.getByText('Marked helpful', { exact: true })).toBeVisible()
})

test('centers operational, reading, connection, and navigation surfaces app-wide', async ({ page }) => {
  test.slow()
  await page.setViewportSize({ width: 1440, height: 1000 })
  await page.goto('/connect')
  await expectConnectCardGeometry(page)
  await expectExpandedTypeScale(page)

  await page.setViewportSize({ width: 390, height: 844 })
  await expectConnectCardGeometry(page)

  await page.setViewportSize({ width: 1440, height: 1000 })
  await connectToChat(page)
  await setAppearance(page, 'Light')
  await expectAppWidthTiers(page, 260)

  await navigateFromSideNav(page, 'Documents')
  await expect(page.getByRole('heading', { name: 'Documents', exact: true })).toBeVisible()
  await page.getByRole('button', { name: 'Collapse sidebar' }).click()
  await expect(page.getByRole('button', { name: 'Expand sidebar' })).toBeVisible()
  await expectCenteredSurface(page, '.page:not(.page-reading)', operationalWidth)
  await expectBoundedOperatorShell(page, 48)

  await page.getByRole('button', { name: 'Expand sidebar' }).click()
  await expect(page.getByRole('button', { name: 'Collapse sidebar' })).toBeVisible()
  await expectBoundedOperatorShell(page, 260)

  await setAppearance(page, 'Dark')
  await expectAppWidthTiers(page, 260)

  await page.setViewportSize({ width: 390, height: 844 })
  await expectAppWidthTiers(page, 0)
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
  await connectToChat(page)
  await page.getByLabel('Message input').fill('Make this fail')
  await page.getByLabel('Message input').press('Enter')

  await expect(page).toHaveURL(/\/chat\/thread-failed$/)
  await expect(page.getByText('The API could not complete this run.')).toBeVisible()
  await expect(page.getByRole('button', { name: 'Retry' })).toBeVisible()
  await expect(page.locator('.chat-status')).toHaveText('Ready')
})
