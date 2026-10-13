import { createContext, useCallback, useContext, useEffect, useRef, useState } from 'react'

// When an Ack agent serves this page it adds <meta name="ack-token"> and <meta name="ack-agent">.
// Opened any other way (vite dev, a static host) the page has no agent behind it and runs simulated.
const meta = (name) => document.querySelector(`meta[name="${name}"]`)?.getAttribute('content') || ''
export const TOKEN = meta('ack-token')
export const AGENT = meta('ack-agent')
export const LIVE = Boolean(TOKEN)

// Reads are safe to repeat, so a dropped connection gets one quiet retry. Actions are never retried:
// the first attempt may have reached the agent.
const READS = new Set(['status', 'tree', 'handoff/status', 'carbon', 'spell', 'refresh'])

export async function api(action, body) {
  const send = () => fetch(`/api/${action}`, {
    method: body === undefined ? 'GET' : 'POST',
    headers: { 'X-Ack-Token': TOKEN, 'Content-Type': 'application/json' },
    body: body === undefined ? undefined : JSON.stringify(body),
  })
  let res
  try {
    res = await send()
  } catch (e) {
    if (!READS.has(action)) throw new Error(`Could not reach the agent (${e.message}). Check the activity log before trying again.`)
    await new Promise((r) => setTimeout(r, 300))
    res = await send()
  }
  const data = await res.json().catch(() => ({}))
  if (!res.ok) throw new Error(data.error || `request failed (${res.status})`)
  return data
}

const StatusContext = createContext({ status: null, error: '', refresh: () => {} })

// One poller for the whole page. It pauses while the tab is hidden.
export function StatusProvider({ children, every = 1500 }) {
  const [status, setStatus] = useState(null)
  const [error, setError] = useState('')
  const timer = useRef(null)
  const misses = useRef(0)

  const refresh = useCallback(async () => {
    if (!LIVE) return
    try {
      setStatus(await api('status'))
      misses.current = 0
      setError('')
    } catch (e) {
      // One missed poll is a blip; two in a row means the agent is really gone.
      if (++misses.current >= 2) setError(e.message)
    }
  }, [])

  useEffect(() => {
    if (!LIVE) return undefined
    let alive = true
    const tick = async () => {
      if (!document.hidden) await refresh()
      if (alive) timer.current = setTimeout(tick, every)
    }
    tick()
    return () => {
      alive = false
      clearTimeout(timer.current)
    }
  }, [refresh, every])

  return <StatusContext.Provider value={{ status, error, refresh }}>{children}</StatusContext.Provider>
}

export const useStatus = () => useContext(StatusContext)

export const short = (hex, n = 16) => (hex ? String(hex).slice(0, n) : '')

export function ago(ts) {
  if (!ts) return ''
  const s = Math.max(0, Date.now() / 1000 - ts)
  if (s < 60) return 'just now'
  if (s < 3600) return `${Math.floor(s / 60)} min ago`
  if (s < 86400) return `${Math.floor(s / 3600)} h ago`
  return new Date(ts * 1000).toLocaleDateString()
}
