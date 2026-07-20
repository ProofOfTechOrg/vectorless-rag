import { Theme } from '@astryxdesign/core/theme'
import { neutralTheme } from '@astryxdesign/theme-neutral'
import { createContext, useContext, useEffect, useMemo, useState, type ReactNode } from 'react'

export type Appearance = 'system' | 'light' | 'dark'
const STORAGE_KEY = 'vectorless:appearance'
// ASTRYX 0.1.6's ESM "built" entry imports its icon module without an
// extension, which Node SSR cannot resolve. The source theme has the same
// tokens and registry; marking it built makes Theme use the imported CSS.
const prebuiltNeutralTheme = { ...neutralTheme, __built: true as const }
const AppearanceContext = createContext<{
  appearance: Appearance
  setAppearance: (appearance: Appearance) => void
} | null>(null)

export function AppearanceProvider({ children }: { children: ReactNode }) {
  const [appearance, setAppearance] = useState<Appearance>(() => {
    if (typeof window === 'undefined') return 'system'
    const saved = window.localStorage.getItem(STORAGE_KEY)
    return saved === 'system' || saved === 'light' || saved === 'dark' ? saved : 'system'
  })
  useEffect(() => {
    window.localStorage.setItem(STORAGE_KEY, appearance)
  }, [appearance])
  const value = useMemo(() => ({ appearance, setAppearance }), [appearance])
  return (
    <AppearanceContext.Provider value={value}>
      <Theme theme={prebuiltNeutralTheme} mode={appearance}>{children}</Theme>
    </AppearanceContext.Provider>
  )
}

export function useAppearance() {
  const value = useContext(AppearanceContext)
  if (!value) throw new Error('useAppearance must be used inside AppearanceProvider')
  return value
}
