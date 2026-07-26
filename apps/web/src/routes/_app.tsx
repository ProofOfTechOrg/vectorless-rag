import { AppShell } from '@astryxdesign/core/AppShell'
import { Badge } from '@astryxdesign/core/Badge'
import { Button } from '@astryxdesign/core/Button'
import { CommandPalette } from '@astryxdesign/core/CommandPalette'
import { SideNav, SideNavItem, SideNavSection } from '@astryxdesign/core/SideNav'
import { Text } from '@astryxdesign/core/Text'
import { TopNav, TopNavHeading } from '@astryxdesign/core/TopNav'
import { createStaticSource } from '@astryxdesign/core/Typeahead'
import { createFileRoute, Outlet, redirect, useNavigate, useRouterState } from '@tanstack/react-router'
import { FileUp, Library, MessageSquare, Search, Settings } from 'lucide-react'
import { useEffect, useMemo, useState } from 'react'
import { useReadiness } from '../hooks/use-readiness'
import type { Identity } from '../lib/contracts'
import { getSession, getThreads } from '../server/functions'

export const Route = createFileRoute('/_app')({
  beforeLoad: async ({ abortController }) => {
    const session = await getSession({ signal: abortController.signal })
    if (!session) throw redirect({ to: '/connect' })
    return { session }
  },
  loader: ({ context, abortController }) =>
    context.session.scopes.includes('chat')
      ? getThreads({ data: {}, signal: abortController.signal })
      : null,
  component: OperatorShell,
})

const destinations = [
  { id: '/chat', label: 'Chat', icon: MessageSquare, requiredScope: 'chat' },
  { id: '/documents', label: 'Documents', icon: Library, requiredScope: 'documents:read' },
  { id: '/ingestion', label: 'Ingestion', icon: FileUp, requiredScope: 'admin:ingest' },
  { id: '/settings', label: 'Settings', icon: Settings, requiredScope: undefined },
] as const satisfies ReadonlyArray<{ id: string; label: string; icon: typeof MessageSquare; requiredScope?: Identity['scopes'][number] }>

function OperatorShell() {
  const { session } = Route.useRouteContext()
  const threadPage = Route.useLoaderData()
  const navigate = useNavigate()
  const pathname = useRouterState({ select: (state) => state.location.pathname })
  const [isSideNavCollapsed, setIsSideNavCollapsed] = useState(false)
  const [paletteOpen, setPaletteOpen] = useState(false)
  const ready = useReadiness()
  const allowedDestinations = useMemo(
    () => destinations.filter((item) => !item.requiredScope || session.scopes.includes(item.requiredScope)),
    [session.scopes],
  )
  const source = useMemo(() => createStaticSource(allowedDestinations), [allowedDestinations])
  useEffect(() => {
    const onKeyDown = (event: KeyboardEvent) => {
      if ((event.metaKey || event.ctrlKey) && event.key.toLowerCase() === 'k') {
        event.preventDefault()
        setPaletteOpen(true)
      }
    }
    window.addEventListener('keydown', onKeyDown)
    return () => window.removeEventListener('keydown', onKeyDown)
  }, [])

  const nav = (
    <SideNav
      collapsible={{
        isCollapsed: isSideNavCollapsed,
        onCollapsedChange: setIsSideNavCollapsed,
      }}
    >
      <SideNavSection title="Workspace" isHeaderHidden>
        {allowedDestinations.map((item) => (
          <SideNavItem
            key={item.id}
            label={item.label}
            icon={item.icon}
            href={item.id}
            isSelected={pathname === item.id || pathname.startsWith(`${item.id}/`)}
          />
        ))}
      </SideNavSection>
      {threadPage ? (
        <SideNavSection title="Recent chats">
          {threadPage.items.length > 0 ? threadPage.items.map((thread) => {
            const href = `/chat/${encodeURIComponent(thread.thread_id)}`
            return (
              <SideNavItem
                key={thread.thread_id}
                endContent={<Text type="supporting">{turnLabel(thread.run_count)}</Text>}
                href={href}
                isSelected={pathname === href}
                label={thread.title}
              />
            )
          }) : (
            <SideNavItem isDisabled label="No recent chats yet" />
          )}
        </SideNavSection>
      ) : null}
    </SideNav>
  )

  return (
    <>
      <AppShell
        className={isSideNavCollapsed ? 'operator-shell operator-shell--side-nav-collapsed' : 'operator-shell'}
        variant="elevated"
        contentPadding={0}
        sideNav={nav}
        mobileNav={{ breakpoint: 'md' }}
        topNav={
          <TopNav
            className="operator-top-nav"
            data-testid="operator-top-nav"
            heading={<TopNavHeading heading="Vectorless RAG" headingHref={allowedDestinations[0]?.id ?? '/settings'} />}
            endContent={
              <div className="inline">
                <Badge label={ready === null ? 'Checking API' : ready ? 'API ready' : 'API unavailable'} variant={ready ? 'success' : ready === false ? 'error' : 'neutral'} />
                <Button label="Search commands" icon={<Search size={16} />} variant="ghost" onClick={() => setPaletteOpen(true)} />
              </div>
            }
          />
        }
      >
        <Outlet />
      </AppShell>
      <CommandPalette
        isOpen={paletteOpen}
        onOpenChange={setPaletteOpen}
        searchSource={source}
        onValueChange={(path) => {
          setPaletteOpen(false)
          const destination = allowedDestinations.find((item) => item.id === path)
          if (destination) void navigate({ to: destination.id })
        }}
        emptyBootstrapText={`Connected as ${session.name}`}
      />
    </>
  )
}

function turnLabel(runCount: number): string {
  return `${runCount} ${runCount === 1 ? 'turn' : 'turns'}`
}
