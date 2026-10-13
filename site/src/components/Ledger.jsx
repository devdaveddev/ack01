import { useCallback, useEffect, useState } from 'react'

// The live log: handoffs stored in the database, with each subscriber's signed verdict.
// Hidden when the API isn't reachable, so the static copy of the site still works.
export default function Ledger() {
  const [items, setItems] = useState(null) // null = not loaded, [] = loaded but empty
  const [offline, setOffline] = useState(false)
  const [title, setTitle] = useState('')
  const [body, setBody] = useState('')
  const [alter, setAlter] = useState(false)
  const [error, setError] = useState('')
  const [busy, setBusy] = useState(false)

  const load = useCallback(async () => {
    try {
      const res = await fetch('/api/handoffs?limit=10')
      if (!res.ok) throw new Error(String(res.status))
      setItems((await res.json()).handoffs)
      setOffline(false)
    } catch {
      setOffline(true)
    }
  }, [])

  useEffect(() => {
    load()
  }, [load])

  async function submit(event) {
    event.preventDefault()
    setError('')
    setBusy(true)
    try {
      const res = await fetch('/api/handoffs', {
        method: 'POST',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify({ title, body, altered_for: alter ? ['alpha'] : [] }),
      })
      const data = await res.json()
      if (!res.ok) throw new Error(data.error || `error ${res.status}`)
      setTitle('')
      setBody('')
      setAlter(false)
      await load()
    } catch (err) {
      setError(err.message)
    } finally {
      setBusy(false)
    }
  }

  if (offline || items === null) return null

  return (
    <section className="ledger" id="log" aria-labelledby="log-title">
      <div className="wrap ledger__inner">
        <h2 className="display" id="log-title">The live log</h2>
        <p className="ledger__intro">
          Every handoff below is stored in a database. Beta announces it, then alpha, gamma and delta each
          recompute the digest on the copy they received and sign ACK or NAK.
        </p>

        <form className="ledger__form" onSubmit={submit}>
          <label className="input">
            <span className="input__label">Title</span>
            <input value={title} maxLength={120} required onChange={(e) => setTitle(e.target.value)} />
          </label>
          <label className="input">
            <span className="input__label">Change</span>
            <textarea value={body} rows={3} maxLength={20000} required onChange={(e) => setBody(e.target.value)} />
          </label>
          <label className="check">
            <input type="checkbox" checked={alter} onChange={(e) => setAlter(e.target.checked)} />
            Edit alpha&apos;s copy in transit (alpha should refuse it)
          </label>
          <div className="ledger__actions">
            <button className="btn btn--red" type="submit" disabled={busy}>{busy ? 'Sending' : 'Send to the log'}</button>
            {error && <p className="feedback is-bad" role="alert">{error}</p>}
          </div>
        </form>

        <ol className="ledger__list">
          {items.length === 0 && <li className="empty">Nothing has been announced yet.</li>}
          {items.map((h) => (
            <li className="ledger__item" key={h.id}>
              <div className="ledger__head">
                <span className="ledger__title">{h.title}</span>
                <span className={`stamp${h.state === 'held' ? ' stamp--bad' : ''}`}>{h.state}</span>
              </div>
              <code className="ledger__digest">sha256 {h.digest.slice(0, 16)}</code>
              <ul className="ledger__votes">
                {h.votes.map((v) => (
                  <li key={v.agent}>
                    <span className="votes__who">{v.agent}</span>
                    <span className={v.verdict === 'NAK' ? 'is-bad' : ''}>{v.verdict}</span>
                    <span className="ledger__reason">{v.reason}</span>
                  </li>
                ))}
              </ul>
            </li>
          ))}
        </ol>
      </div>
    </section>
  )
}
