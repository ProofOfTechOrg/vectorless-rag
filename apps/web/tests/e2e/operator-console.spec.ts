import { expect, test } from '@playwright/test'

test.skip(!!process.env.PLAYWRIGHT_LIVE, 'Deterministic mock coverage is separate from live verification')

const key = process.env.PLAYWRIGHT_API_KEY ?? 'vrag_test_full'
const shellWidth = 80 * 16
const operationalWidth = 64 * 16
const readingWidth = 60 * 16
const chatWidth = 60 * 16

async function waitForHydration(page: import('@playwright/test').Page) {
  await expect(page.locator('html')).toHaveAttribute('data-hydrated', 'true', { timeout: 15_000 })
}

interface AppearanceSnapshot {
  bodyBackground: string
  cardBackground: string
  colorScheme: string
  explicitTheme: string | null
  theme: string | null
  wrapperColorScheme: string
}

async function inspectAppearanceBeforeHydration(
  page: import('@playwright/test').Page,
  {
    appearance,
    colorScheme,
    throwOnRead = false,
  }: {
    appearance: string | null
    colorScheme: 'light' | 'dark'
    throwOnRead?: boolean
  },
) {
  await page.emulateMedia({ colorScheme })
  await page.addInitScript(({ savedAppearance, storageThrows }) => {
    if (storageThrows) {
      Object.defineProperty(Storage.prototype, 'getItem', {
        configurable: true,
        value: () => { throw new Error('Storage is unavailable') },
      })
      return
    }
    if (savedAppearance === null) window.localStorage.removeItem('vectorless:appearance')
    else window.localStorage.setItem('vectorless:appearance', savedAppearance)
  }, { savedAppearance: appearance, storageThrows: throwOnRead })

  const browserErrors: string[] = []
  page.on('console', (message) => {
    if (message.type() === 'error') browserErrors.push(message.text())
  })
  page.on('pageerror', (error) => browserErrors.push(error.message))

  let releaseScripts = () => {}
  const scriptsReleased = new Promise<void>((resolve) => { releaseScripts = resolve })
  await page.route('**/*', async (route) => {
    if (route.request().resourceType() === 'script') await scriptsReleased
    await route.continue()
  })

  const snapshot = () => page.evaluate((): AppearanceSnapshot => {
    const root = document.documentElement
    const wrapper = document.querySelector<HTMLElement>('body > [data-astryx-theme="neutral"]')
    const card = document.querySelector<HTMLElement>('.astryx-card')
    if (!document.body || !wrapper || !card) throw new Error('Appearance fixture did not render')
    return {
      bodyBackground: getComputedStyle(document.body).backgroundColor,
      cardBackground: getComputedStyle(card).backgroundColor,
      colorScheme: getComputedStyle(root).colorScheme,
      explicitTheme: root.getAttribute('data-theme'),
      theme: root.getAttribute('data-astryx-theme'),
      wrapperColorScheme: getComputedStyle(wrapper).colorScheme,
    }
  })

  try {
    await page.goto('/connect', { waitUntil: 'commit' })
    await page.waitForFunction(() => {
      const card = document.querySelector<HTMLElement>('.astryx-card')
      return document.body && card && getComputedStyle(document.body).backgroundColor !== 'rgba(0, 0, 0, 0)'
    })
    await expect(page.locator('html')).not.toHaveAttribute('data-hydrated', 'true')
    const before = await snapshot()

    releaseScripts()
    await waitForHydration(page)
    const after = await snapshot()
    return { after, before, browserErrors }
  } finally {
    releaseScripts()
    await page.unroute('**/*')
  }
}

async function captureWaitingIndicator(page: import('@playwright/test').Page) {
  const handle = await page.waitForFunction(() => {
    const indicator = document.querySelector<HTMLElement>('.chat-waiting')
    if (!indicator) return false
    const dots = [...indicator.querySelectorAll<HTMLElement>('.chat-waiting-dot')]
    return {
      animationName: dots[0] ? getComputedStyle(dots[0]).animationName : '',
      count: dots.length,
      label: indicator.getAttribute('aria-label'),
    }
  })
  const snapshot = await handle.jsonValue()
  if (!snapshot) throw new Error('Waiting indicator did not render')
  return snapshot
}

async function connectToChat(page: import('@playwright/test').Page, apiKey = key) {
  await page.goto('/connect')
  await waitForHydration(page)
  await expect(page.getByRole('button', { name: 'Connect' })).toBeEnabled()
  await page.getByLabel('API key').fill(apiKey)
  await page.getByRole('button', { name: 'Connect' }).click()
  await expect(page.getByRole('heading', { name: 'Grounded research chat' })).toBeVisible()
  await waitForHydration(page)
}

function sideNavigation(page: import('@playwright/test').Page) {
  return page.getByRole('navigation', { name: 'Side navigation' })
}

function recentChatsGroup(page: import('@playwright/test').Page) {
  return sideNavigation(page).getByRole('group', { name: 'Recent chats' })
}

async function expectAlignedChatColumns(page: import('@playwright/test').Page) {
  const geometry = await page.evaluate(() => {
    const selectors = [
      '[data-testid="chat-intro-column"]',
      '[data-testid="chat-message-list"], .chat-empty-state',
      '[data-testid="chat-composer"]',
    ]
    const chatPage = document.querySelector<HTMLElement>('[data-testid="chat-page"]')
    if (!chatPage) throw new Error('Missing chat page')
    const chatPageRect = chatPage.getBoundingClientRect()
    const columns = selectors.map((selector) => {
      const element = document.querySelector<HTMLElement>(selector)
      if (!element) throw new Error(`Missing chat column: ${selector}`)
      const rect = element.getBoundingClientRect()
      return { center: rect.left + rect.width / 2, width: rect.width }
    })
    return {
      columns,
      chatPageCenter: chatPageRect.left + chatPageRect.width / 2,
      scrollWidth: Math.max(document.documentElement.scrollWidth, document.body.scrollWidth),
      viewportWidth: window.innerWidth,
    }
  })

  expect(Math.max(...geometry.columns.map(({ center }) => center)) - Math.min(...geometry.columns.map(({ center }) => center))).toBeLessThanOrEqual(1)
  for (const column of geometry.columns) {
    expect(Math.abs(column.center - geometry.chatPageCenter)).toBeLessThanOrEqual(1)
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

for (const scenario of [
  {
    appearance: 'dark',
    colorScheme: 'light' as const,
    expected: {
      bodyBackground: 'rgb(27, 27, 27)',
      cardBackground: 'rgb(27, 27, 27)',
      colorScheme: 'dark',
      explicitTheme: 'dark',
      theme: 'neutral',
      wrapperColorScheme: 'dark',
    },
  },
  {
    appearance: 'light',
    colorScheme: 'dark' as const,
    expected: {
      bodyBackground: 'rgb(241, 241, 241)',
      cardBackground: 'rgb(255, 255, 255)',
      colorScheme: 'light',
      explicitTheme: 'light',
      theme: 'neutral',
      wrapperColorScheme: 'light',
    },
  },
]) {
  test(`applies saved ${scenario.appearance} appearance before hydration`, async ({ page }) => {
    const result = await inspectAppearanceBeforeHydration(page, scenario)
    expect(result.before).toEqual(scenario.expected)
    expect(result.after).toEqual(result.before)
    expect(result.browserErrors).toEqual([])
  })
}

test('follows system appearance before hydration and after an OS change', async ({ page }) => {
  const result = await inspectAppearanceBeforeHydration(page, {
    appearance: 'system',
    colorScheme: 'light',
  })
  expect(result.before).toEqual({
    bodyBackground: 'rgb(241, 241, 241)',
    cardBackground: 'rgb(255, 255, 255)',
    colorScheme: 'light dark',
    explicitTheme: null,
    theme: 'neutral',
    wrapperColorScheme: 'light dark',
  })
  expect(result.after).toEqual(result.before)
  expect(result.browserErrors).toEqual([])

  await page.emulateMedia({ colorScheme: 'dark' })
  await expect.poll(() => page.evaluate(() => getComputedStyle(document.body).backgroundColor)).toBe('rgb(27, 27, 27)')
  expect(await page.locator('html').getAttribute('data-theme')).toBeNull()
  await expect(page.locator('.astryx-card')).toHaveCSS('background-color', 'rgb(27, 27, 27)')
})

test('falls back to system appearance before hydration when storage throws', async ({ page }) => {
  const result = await inspectAppearanceBeforeHydration(page, {
    appearance: null,
    colorScheme: 'dark',
    throwOnRead: true,
  })
  expect(result.before.explicitTheme).toBeNull()
  expect(result.before.theme).toBe('neutral')
  expect(result.before.bodyBackground).toBe('rgb(27, 27, 27)')
  expect(result.before.cardBackground).toBe('rgb(27, 27, 27)')
  expect(result.after).toEqual(result.before)
  expect(result.browserErrors).toEqual([])
})

test('falls back to system appearance before hydration for an invalid stored value', async ({ page }) => {
  const result = await inspectAppearanceBeforeHydration(page, {
    appearance: 'sepia',
    colorScheme: 'light',
  })
  expect(result.before.explicitTheme).toBeNull()
  expect(result.before.theme).toBe('neutral')
  expect(result.before.bodyBackground).toBe('rgb(241, 241, 241)')
  expect(result.before.cardBackground).toBe('rgb(255, 255, 255)')
  expect(result.after).toEqual(result.before)
  expect(result.browserErrors).toEqual([])
})

test('marks personalized responses no-store without changing stream caching', async ({ page }) => {
  const documentResponse = await page.goto('/connect')
  expect(documentResponse?.headers()['cache-control']).toBe('private, no-store')

  await waitForHydration(page)
  await expect(page.getByRole('button', { name: 'Connect' })).toBeEnabled()
  await page.getByLabel('API key').fill(key)
  const serverFunctionResponse = page.waitForResponse((response) => (
    response.url().includes('/_serverFn/') && response.request().method() === 'POST'
  ))
  await page.getByRole('button', { name: 'Connect' }).click()
  expect((await serverFunctionResponse).headers()['cache-control']).toBe('private, no-store')
  await expect(page.getByRole('heading', { name: 'Grounded research chat' })).toBeVisible()

  const streamResponse = page.waitForResponse((response) => new URL(response.url()).pathname === '/api/chat/stream')
  await page.getByLabel('Message input').fill('What does the cobalt lantern fixture prove?')
  await page.getByLabel('Message input').press('Enter')
  expect((await streamResponse).headers()['cache-control']).toBe('no-cache, no-transform')
})

test('connects through the UI and exercises the operator workflows', async ({ page }) => {
  await page.goto('/connect')
  await waitForHydration(page)
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
  await expect(recentChatsGroup(page)).toContainText('What does the cobalt lantern fixture prove?')
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
  await expect(page.getByTestId('recent-chats-collapsible')).toHaveCount(0)
  await page.getByRole('button', { name: 'Open navigation' }).click()
  const navigationDrawer = page.getByRole('dialog', { name: 'Navigation' })
  const drawerHistory = navigationDrawer.getByRole('group', { name: 'Recent chats' })
  await expect(navigationDrawer).toBeVisible()
  await expect(drawerHistory.getByRole('link', { name: /Compare amber prism retrieval/ })).toBeVisible()
  await drawerHistory.getByRole('link', { name: /Compare amber prism retrieval/ }).click()
  await expect(page).toHaveURL(/\/chat\/thread-2$/)
  await expect(navigationDrawer).not.toBeVisible()

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

test('animates the assistant waiting indicator while a response is pending', async ({ page }) => {
  await connectToChat(page)

  await page.getByLabel('Message input').fill('Hold the response')
  const animatedSnapshot = captureWaitingIndicator(page)
  await page.getByLabel('Message input').press('Enter')
  expect(await animatedSnapshot).toEqual({
    animationName: 'chat-waiting-bounce',
    count: 3,
    label: 'Waiting for response',
  })
  await expect(page).toHaveURL(/\/chat\/thread-delayed$/)
  await expect(page.getByText('The API could not complete this run.')).toBeVisible()
  await expect(page.locator('.chat-waiting')).toHaveCount(0)

  await page.emulateMedia({ reducedMotion: 'reduce' })
  await page.getByLabel('Message input').fill('Hold the response')
  const reducedMotionSnapshot = captureWaitingIndicator(page)
  await page.getByLabel('Message input').press('Enter')
  expect(await reducedMotionSnapshot).toEqual({
    animationName: 'none',
    count: 3,
    label: 'Waiting for response',
  })
  await expect(page.getByText('The API could not complete this run.')).toBeVisible()
  await expect(page.locator('.chat-waiting')).toHaveCount(0)
})

test('renders historical running queries as passive pending messages', async ({ page }) => {
  await connectToChat(page)
  await page.goto('/chat/thread-running')
  await waitForHydration(page)

  await expect(page.getByLabel('Waiting for response')).toBeVisible()
  await expect(page.getByText('The API could not complete this run.')).toHaveCount(0)
  await expect(page.getByRole('button', { name: 'Retry' })).toHaveCount(0)
  await expect(page.getByText('Rate answer', { exact: true })).toHaveCount(0)
  await expect(page.getByRole('button', { name: 'Stop' })).toHaveCount(0)
  await expect(page.getByTestId('chat-message-list')).not.toHaveAttribute('aria-busy', 'true')
  await expect(page.locator('.chat-status')).toHaveText('Ready')
})

test('distinguishes recent chats and replaces conversation state when switching threads', async ({ page }) => {
  await page.setViewportSize({ width: 1440, height: 1000 })
  await connectToChat(page)

  const navigation = sideNavigation(page)
  const history = recentChatsGroup(page)
  await expect(history).toBeVisible()
  const settingsBox = await navigation.getByRole('link', { name: 'Settings', exact: true }).boundingBox()
  const historyBox = await history.boundingBox()
  expect(settingsBox).not.toBeNull()
  expect(historyBox).not.toBeNull()
  expect((historyBox?.y ?? 0)).toBeGreaterThan(settingsBox?.y ?? Number.POSITIVE_INFINITY)
  await expect(history.getByRole('link', { name: /Compare amber prism retrieval/ })).toContainText('2 turns')
  await expect(history.getByRole('link', { name: /cobalt lantern fixture prove/i })).toContainText('1 turn')
  await expect(page.getByRole('complementary', { name: 'Recent chats' })).toHaveCount(0)
  await expect(page.getByTestId('recent-chats-collapsible')).toHaveCount(0)

  for (const destination of ['Settings', 'Documents', 'Ingestion'] as const) {
    await navigateFromSideNav(page, destination)
    await expect(page.getByRole('heading', { name: destination, exact: true })).toBeVisible()
    await expect(history).toBeVisible()
  }

  await history.getByRole('link', { name: /cobalt lantern fixture prove/i }).click()
  await expect(page).toHaveURL(/\/chat\/thread-1$/)
  await expect(page.getByText('The fixture confirms cobalt lantern retrieval [1].')).toBeVisible()
  await expect(history.getByRole('link', { name: /cobalt lantern fixture prove/i })).toHaveAttribute('aria-current', 'page')

  await history.getByRole('link', { name: /Compare amber prism retrieval/ }).click()
  await expect(page).toHaveURL(/\/chat\/thread-2$/)
  await expect(page.getByText('The amber prism second pass adds catalog evidence.')).toBeVisible()
  await expect(page.getByText('The fixture confirms cobalt lantern retrieval [1].')).toHaveCount(0)
  await expect(history.getByRole('link', { name: /Compare amber prism retrieval/ })).toHaveAttribute('aria-current', 'page')
})

test('clears route data when a different identity reconnects to the same thread URL', async ({ page }) => {
  await connectToChat(page)
  await recentChatsGroup(page).getByRole('link', { name: /cobalt lantern fixture prove/i }).click()
  await expect(page.getByText('The fixture confirms cobalt lantern retrieval [1].')).toBeVisible()

  await page.goto('/settings')
  await waitForHydration(page)
  await page.getByRole('button', { name: 'Log out' }).click()
  await expect(page.getByRole('heading', { name: 'Connect to Vectorless RAG' })).toBeVisible()

  await page.getByLabel('API key').fill('mock-second-chat')
  await page.getByRole('button', { name: 'Connect' }).click()
  await expect(page.getByRole('heading', { name: 'Grounded research chat' })).toBeVisible()
  await recentChatsGroup(page).getByRole('link', { name: /Which chat belongs to this account/ }).click()

  await expect(page).toHaveURL(/\/chat\/thread-1$/)
  await expect(page.getByText('Only the second account can see this answer.')).toBeVisible()
  await expect(page.getByText('The fixture confirms cobalt lantern retrieval [1].')).toHaveCount(0)
})

test('refreshes existing-thread history after a completed stream', async ({ page }, testInfo) => {
  const refreshApiKey = [
    'mock-refresh-chat',
    testInfo.project.name,
    testInfo.workerIndex,
    testInfo.parallelIndex,
    testInfo.repeatEachIndex,
    testInfo.retry,
  ].join(':')
  let chatSubmissions = 0
  page.on('request', (request) => {
    if (new URL(request.url()).pathname === '/api/chat/stream') chatSubmissions += 1
  })

  await connectToChat(page, refreshApiKey)
  const history = recentChatsGroup(page)
  const refreshThread = history.getByRole('link', { name: /initial refresh state/i })
  await expect(refreshThread).toContainText('1 turn')
  await refreshThread.evaluate((link: HTMLAnchorElement) => link.click())
  await expect(page).toHaveURL(/\/chat\/thread-refresh$/)
  await expect(page.getByText('The refresh thread starts with one turn.')).toBeVisible()

  await page.getByLabel('Message input').fill('Add another refresh turn')
  await page.getByLabel('Message input').press('Enter')

  await expect(page.getByText('The refresh thread now has 2 turns.')).toBeVisible({ timeout: 10_000 })
  await expect(refreshThread).toContainText('2 turns', { timeout: 10_000 })
  await expect.poll(async () => {
    const response = await page.request.get('http://127.0.0.1:8010/__test/refresh-state', {
      headers: { 'X-API-Key': refreshApiKey },
    })
    return (await response.json()).thread_list_requests
  }).toBeGreaterThanOrEqual(2)
  await expect(page.getByText('Add another refresh turn', { exact: true })).toHaveCount(1)
  expect(chatSubmissions).toBe(1)
})

test('aborts an in-flight stream when navigating to another recent chat', async ({ page }) => {
  await connectToChat(page)
  await page.getByLabel('Message input').fill('Hold the response')
  await page.getByLabel('Message input').press('Enter')
  await expect(page.locator('.chat-waiting')).toBeVisible()

  await recentChatsGroup(page).getByRole('link', { name: /cobalt lantern fixture prove/i }).click()
  await expect(page).toHaveURL(/\/chat\/thread-1$/)
  await expect(page.getByText('The fixture confirms cobalt lantern retrieval [1].')).toBeVisible()
  await page.waitForTimeout(2_200)
  await expect(page.getByText('Delayed test response')).toHaveCount(0)
  await expect(page.getByText('Run cancelled.')).toHaveCount(0)
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

test('retries a failed document from its detail page', async ({ page }) => {
  await connectToChat(page)
  await page.goto('/documents/71309511-00aa-4d06-a60b-728e1fdd3269')
  await waitForHydration(page)

  await expect(page.getByRole('heading', { name: 'Failed PageIndex Fixture' })).toBeVisible()
  await expect(page.getByText('Ingestion failed', { exact: true })).toBeVisible()
  const retry = page.getByRole('button', { name: 'Retry ingestion', exact: true })
  await expect(retry).toBeEnabled()
  await retry.click()

  await expect(page.getByRole('status').filter({ hasText: 'Retry queued' })).toBeVisible()
  await expect(page.getByText('queued', { exact: true })).toBeVisible()
  await expect(retry).toHaveCount(0)
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
  const history = recentChatsGroup(page)
  await expect(history.getByRole('link')).toHaveCount(2)
  await page.getByRole('button', { name: 'Collapse sidebar' }).click()
  await expect(page.getByRole('button', { name: 'Expand sidebar' })).toBeVisible()
  await expect(history.getByRole('link')).toHaveCount(0)
  await expectCenteredSurface(page, '.page:not(.page-reading)', operationalWidth)
  await expectBoundedOperatorShell(page, 48)

  await page.getByRole('button', { name: 'Expand sidebar' }).click()
  await expect(page.getByRole('button', { name: 'Collapse sidebar' })).toBeVisible()
  await expect(history.getByRole('link')).toHaveCount(2)
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
  await waitForHydration(page)
  await expect(page.getByRole('button', { name: 'Connect' })).toBeEnabled()
  await page.getByLabel('API key').fill('mock-documents-only')
  await page.getByRole('button', { name: 'Connect' }).click()
  await expect(page).toHaveURL(/\/documents$/)
  await expect(page.getByRole('heading', { name: 'Documents' })).toBeVisible()
  await expect(sideNavigation(page).getByRole('group', { name: 'Recent chats' })).toHaveCount(0)
  const stateResponse = await page.request.get('http://127.0.0.1:8010/__test/documents-only-state', {
    headers: { 'X-API-Key': 'mock-documents-only' },
  })
  expect((await stateResponse.json()).thread_list_requests).toBe(0)

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
