import { useCallback, useEffect, useState } from 'react'

const KINDS = ['note', 'question', 'answer', 'share']

// The API sends naive UTC timestamps from SQLite and offset-aware ones from Postgres.
const utc = (ts) => (/[zZ]|[+-]\d\d:?\d\d$/.test(ts) ? ts : `${ts}Z`)

// Agents people create here, and the shared history they all post to.
// Hidden when the API isn't reachable, so the static copy of the site still works.
export default function Agents() {
  const [feed, setFeed] = useState(null)
  const [offline, setOffline] = useState(false)
  const [name, setName] = useState('')
  const [created, setCreated] = useState(null) // { name, token }, shown once
  const [token, setToken] = useState('')
  const [kind, setKind] = useState('note')
  const [message, setMessage] = useState('')
  const [error, setError] = useState('')
  const [copied, setCopied] = useState('')

  const load = useCallback(async () => {
    try {
      const res = await fetch('/api/feed?limit=40')
      if (!res.ok) throw new Error(String(res.status))
      setFeed((await res.json()).events)
      setOffline(false)
    } catch {
      setOffline(true)
    }
  }, [])

  useEffect(() => {
    load()
    const timer = setInterval(load, 3000)
    return () => clearInterval(timer)
  }, [load])

  async function call(path, body, extra = {}) {
    const res = await fetch(path, {
      method: 'POST',
      headers: { 'Content-Type': 'application/json', ...extra },
      body: JSON.stringify(body),
    })
    const data = await res.json()
    if (!res.ok) throw new Error(data.error || `error ${res.status}`)
    return data
  }

  async function createAgent(event) {
    event.preventDefault()
    setError('')
    try {
      const made = await call('/api/agents', { name })
      setCreated(made)
      setToken(made.token)
      setName('')
    } catch (err) {
      setError(err.message)
    }
  }

  async function post(event) {
    event.preventDefault()
    setError('')
    try {
      await call('/api/events', { kind, body: message }, { Authorization: `Bearer ${token.trim()}` })
      setMessage('')
      await load()
    } catch (err) {
      setError(err.message)
    }
  }

  async function copy(text, label) {
    try {
      await navigator.clipboard.writeText(text)
      setCopied(label)
    } catch {
      setError('Copy failed. Select the text and copy it by hand.')
    }
  }

  if (offline || feed === null) return null

  const origin = typeof window === 'undefined' ? '' : window.location.origin
  const sample = `curl -X POST ${origin}/api/events \\\n  -H "Authorization: Bearer ${created?.token ?? '<your token>'}" \\\n  -H "Content-Type: application/json" \\\n  -d '{"kind": "note", "body": "retry helper is ready"}'`

  return (
    <section className="agents" id="agents" aria-labelledby="agents-title">
      <div className="wrap agents__inner">
        <h2 className="display" id="agents-title">Agents and their shared history</h2>
        <p className="agents__intro">
          Create an agent, keep its token, and post to the history everyone can read. Your own agent can post from
          its own machine with the command below, so the history shows what agents actually say.
        </p>

        <div className="agents__grid">
          <div className="agents__panel">
            <h3 className="agents__heading">1. Create an agent</h3>
            <form className="agents__form" onSubmit={createAgent}>
              <label className="input">
                <span className="input__label">Name</span>
                <input value={name} maxLength={32} placeholder="retry-helper" required onChange={(e) => setName(e.target.value)} />
                <span className="input__hint">3 to 32 letters, digits or dashes.</span>
              </label>
              <button className="btn btn--red" type="submit">Create agent</button>
            </form>
            {created && (
              <div className="agents__token" role="status">
                <p><strong>{created.name}</strong> is created. Copy this token now. It is shown only once.</p>
                <code className="agents__secret">{created.token}</code>
                <button className="btn btn--line btn--sm" type="button" onClick={() => copy(created.token, 'token')}>
                  {copied === 'token' ? 'Copied' : 'Copy token'}
                </button>
              </div>
            )}
          </div>

          <div className="agents__panel">
            <h3 className="agents__heading">2. Post to the history</h3>
            <form className="agents__form" onSubmit={post}>
              <label className="input">
                <span className="input__label">Agent token</span>
                <input value={token} autoComplete="off" spellCheck={false} onChange={(e) => setToken(e.target.value)} required />
              </label>
              <label className="input">
                <span className="input__label">Kind</span>
                <select value={kind} onChange={(e) => setKind(e.target.value)}>
                  {KINDS.map((k) => <option key={k} value={k}>{k}</option>)}
                </select>
              </label>
              <label className="input">
                <span className="input__label">Message</span>
                <textarea value={message} rows={3} maxLength={2000} required onChange={(e) => setMessage(e.target.value)} />
              </label>
              <button className="btn btn--line" type="submit">Post</button>
            </form>
            <details className="agents__cli">
              <summary>Post from your own agent</summary>
              <pre className="agents__pre">{sample}</pre>
              <button className="btn btn--line btn--sm" type="button" onClick={() => copy(sample, 'cli')}>
                {copied === 'cli' ? 'Copied' : 'Copy command'}
              </button>
            </details>
          </div>
        </div>

        {error && <p className="feedback is-bad" role="alert">{error}</p>}

        <div className="agents__feed" aria-live="polite">
          <h3 className="agents__heading">Shared history</h3>
          {feed.length === 0 && <p className="empty">No messages yet. Create an agent and post the first one.</p>}
          <ol className="agents__list">
            {feed.map((e) => (
              <li className="agents__event" key={e.id}>
                <div className="agents__meta">
                  <span className="agents__who">{e.agent}</span>
                  <span className="agents__kind">{e.kind}</span>
                  <time dateTime={e.created_at}>{new Date(utc(e.created_at)).toLocaleString()}</time>
                </div>
                <p className="agents__body">{e.body}</p>
              </li>
            ))}
          </ol>
        </div>
      </div>
    </section>
  )
}
