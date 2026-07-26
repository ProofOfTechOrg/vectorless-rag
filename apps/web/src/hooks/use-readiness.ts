import { useEffect, useState } from 'react'
import { getReadiness } from '../server/functions'

export function useReadiness(): boolean | null {
  const [ready, setReady] = useState<boolean | null>(null)

  useEffect(() => {
    let ignore = false
    getReadiness({})
      .then((value) => {
        if (!ignore) setReady(value.ready)
      })
      .catch(() => {
        if (!ignore) setReady(false)
      })
    return () => {
      ignore = true
    }
  }, [])

  return ready
}
