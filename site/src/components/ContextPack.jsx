import { useEffect, useState } from 'react'

// Build a context pack from public GitHub repositories and websites: a prompt your agent can use.
// The server reads only public addresses and limits how often it builds packs. Hidden when the API
// isn't reachable, so the static copy of the site still works.
export default function ContextPack() {
  const [offline, setOffline] = useState(false)

  useEffect(() => {
    fetch('/api/health').then((r) => { if (!r.ok) setOffline(true) }).catch(() => setOffline(true))
  }, [])
  const [goal, setGoal] = useState('')
  const [repo, setRepo] = useState('')
  const [site, setSite] = useState('')
  const [busy, setBusy] = useState(false)
  const [error, setError] = useState('')
  const [pack, setPack] = useState(null)
  const [copied, setCopied] = useState(false)

  async function build(event) {
    event.preventDefault()
    setError('')
    setPack(null)
    setCopied(false)
    setBusy(true)
    const body = { goal: goal.trim() }
    if (repo.trim()) body.github = [repo.trim()]
    if (site.trim()) body.sites = [site.trim()]
    try {
      const res = await fetch('/api/context', {
        method: 'POST',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify(body),
      })
      const data = await res.json().catch(() => ({}))
      if (!res.ok) throw new Error(data.error || (Array.isArray(data.detail) ? data.detail[0]?.msg : '') || `error ${res.status}`)
      setPack(data)
    } catch (err) {
      if (err instanceof TypeError) setOffline(true)
      else setError(err.message)
    } finally {
      setBusy(false)
    }
  }

  async function copy() {
    try {
      await navigator.clipboard.writeText(pack.prompt)
      setCopied(true)
    } catch {
      setError('Copy failed. Select the text and copy it by hand.')
    }
  }

  if (offline) return null

  return (
    <section className="context" id="context" aria-labelledby="context-title">
      <div className="wrap context__inner">
        <h2 className="display" id="context-title">Build a context pack</h2>
        <p className="context__intro">
          Point at a public GitHub repository or a website and say what you are building. You get a prompt
          with the most relevant code and pages, ready for your own agent. Packs are limited to public
          addresses, and the site builds a few per hour.
        </p>

        <form className="context__form" onSubmit={build}>
          <label className="input">
            <span className="input__label">What are you building?</span>
            <input value={goal} maxLength={200} required minLength={3} placeholder="add retry with backoff to my webhook calls" onChange={(e) => setGoal(e.target.value)} />
          </label>
          <label className="input">
            <span className="input__label">GitHub repository (optional)</span>
            <input value={repo} maxLength={300} placeholder="https://github.com/owner/repo" onChange={(e) => setRepo(e.target.value)} />
          </label>
          <label className="input">
            <span className="input__label">Website (optional)</span>
            <input value={site} maxLength={300} placeholder="https://docs.example.com" onChange={(e) => setSite(e.target.value)} />
          </label>
          <div className="context__actions">
            <button className="btn btn--red" type="submit" disabled={busy || (!repo.trim() && !site.trim())}>
              {busy ? 'Reading the sources' : 'Build the pack'}
            </button>
            {!repo.trim() && !site.trim() && <span className="input__hint">Add a repository or a website.</span>}
          </div>
        </form>

        {error && <p className="feedback is-bad" role="alert">{error}</p>}

        {pack && (
          <div className="context__result" aria-live="polite">
            <div className="context__head">
              <span className="context__name">{pack.name}</span>
              <span className="input__hint">{pack.items} sections from {pack.sources.length} source(s)</span>
              <button className="btn btn--line btn--sm" type="button" onClick={copy}>{copied ? 'Copied' : 'Copy prompt'}</button>
            </div>
            <pre className="context__pre">{pack.prompt}</pre>
          </div>
        )}
      </div>
    </section>
  )
}
