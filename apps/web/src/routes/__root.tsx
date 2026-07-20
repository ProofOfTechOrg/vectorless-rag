import { LinkProvider } from '@astryxdesign/core/Link'
import { HeadContent, Link, Scripts, createRootRoute } from '@tanstack/react-router'
import { useEffect } from 'react'
import { AppearanceProvider } from '../components/AppearanceProvider'
import { RouteError } from '../components/RouteError'

import appCss from '../styles.css?url'

const THEME_INIT_SCRIPT = `(function(){try{var key='vectorless:appearance';var mode=localStorage.getItem(key);if(mode!=='light'&&mode!=='dark')mode='system';var dark=mode==='dark'||(mode==='system'&&matchMedia('(prefers-color-scheme: dark)').matches);document.documentElement.style.colorScheme=dark?'dark':'light';}catch(_){}})();`

export const Route = createRootRoute({
  head: () => ({
    meta: [
      {
        charSet: 'utf-8',
      },
      {
        name: 'viewport',
        content: 'width=device-width, initial-scale=1',
      },
      {
        title: 'Vectorless RAG Operator Console',
      },
    ],
    links: [
      {
        rel: 'stylesheet',
        href: appCss,
      },
    ],
  }),
  errorComponent: ({ error }) => <RouteError error={error} title="Unable to load the console" fullPage />,
  shellComponent: RootDocument,
})

function RootDocument({ children }: { children: React.ReactNode }) {
  return (
    <html lang="en" suppressHydrationWarning>
      <head>
        <script dangerouslySetInnerHTML={{ __html: THEME_INIT_SCRIPT }} />
        <HeadContent />
      </head>
      <body>
        <AppearanceProvider>
          <LinkProvider component={Link}>
            <HydrationMarker />
            {children}
          </LinkProvider>
        </AppearanceProvider>
        <Scripts />
      </body>
    </html>
  )
}

function HydrationMarker() {
  useEffect(() => {
    document.documentElement.dataset.hydrated = 'true'
    return () => {
      delete document.documentElement.dataset.hydrated
    }
  }, [])
  return null
}
