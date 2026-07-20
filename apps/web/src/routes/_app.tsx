import { AppShell } from '@astryxdesign/core/AppShell'
import { Badge } from '@astryxdesign/core/Badge'
import { Button } from '@astryxdesign/core/Button'
import { CommandPalette } from '@astryxdesign/core/CommandPalette'
import { SideNav, SideNavItem, SideNavSection } from '@astryxdesign/core/SideNav'
import { TopNav, TopNavHeading } from '@astryxdesign/core/TopNav'
import { createStaticSource } from '@astryxdesign/core/Typeahead'
import { createFileRoute, Outlet, redirect, useRouterState } from '@tanstack/react-router'
import { FileUp, Library, MessageSquare, Search, Settings } from 'lucide-react'
import { useEffect, useMemo, useState } from 'react'
import type { Identity } from '../lib/contracts'
import { getReadiness, getSession } from '../server/functions'

export const Route = createFileRoute('/_app')({
  beforeLoad: async () => {
    const session = await getSession()
    if (!session) throw redirect({ to: '/connect' })
    return { session }
  },
  component: OperatorShell,
})

const destinations = [
  { id: '/chat', label: 'Chat', icon: MessageSquare, requiredScope: 'chat' },
  { id: '/documents', label: 'Documents', icon: Library, requiredScope: 'documents:read' },
  { id: '/ingestion', label: 'Ingestion', icon: FileUp, requiredScope: 'admin:ingest' },
  { id: '/settings', label: 'Settings', icon: Settings },
] satisfies Array<{ id: string; label: string; icon: typeof MessageSquare; requiredScope?: Identity['scopes'][number] }>

function OperatorShell() {
  const { session } = Route.useRouteContext()
  const pathname = useRouterState({ select: (state) => state.location.pathname })
  const [paletteOpen, setPaletteOpen] = useState(false)
  const [ready, setReady] = useState<boolean | null>(null)
  const allowedDestinations = useMemo(
    () => destinations.filter((item) => !item.requiredScope || session.scopes.includes(item.requiredScope)),
    [session.scopes],
  )
  const source = useMemo(() => createStaticSource(allowedDestinations), [allowedDestinations])
  useEffect(() => {
    getReadiness().then((value) => setReady(value.ready)).catch(() => setReady(false))
  }, [])
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
    <SideNav collapsible>
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
    </SideNav>
  )

  return (
    <>
      <AppShell
        variant="elevated"
        contentPadding={0}
        sideNav={nav}
        mobileNav={{ breakpoint: 'md' }}
        topNav={
          <TopNav
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
          window.location.assign(path)
        }}
        emptyBootstrapText={`Connected as ${session.name}`}
      />
    </>
  )
}
