import { LinkProvider } from '@astryxdesign/core/Link'
import { HeadContent, Link, Scripts, createRootRoute } from '@tanstack/react-router'
import { useEffect } from 'react'
import { AppearanceProvider } from '../components/AppearanceProvider'
import { RouteError } from '../components/RouteError'

import '../styles.css'

const THEME_INIT_SCRIPT = `(function(){try{var mode=localStorage.getItem('vectorless:appearance');if(mode==='light'||mode==='dark'){document.documentElement.setAttribute('data-theme',mode);}else{document.documentElement.removeAttribute('data-theme');}}catch(_){document.documentElement.removeAttribute('data-theme');}})();`

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
  }),
  headers: () => ({ 'Cache-Control': 'private, no-store' }),
  errorComponent: ({ error }) => <RouteError error={error} title="Unable to load the console" fullPage />,
  shellComponent: RootDocument,
})

function RootDocument({ children }: { children: React.ReactNode }) {
  return (
    <html lang="en" data-astryx-theme="neutral" suppressHydrationWarning>
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
