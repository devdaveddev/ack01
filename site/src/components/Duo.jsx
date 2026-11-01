import { useCallback, useEffect, useRef, useState } from 'react'

// Files each agent writes itself. Anything else in its folder arrived from the other agent.
const OWN = {
  alpha: ['signing.py', 'test_signing.py', 'payment_keys.py', 'deliver.py', 'test_deliver.py'],
  beta: ['retry.py', 'test_retry.py', 'events.py', 'test_events.py'],
}
const SHOWN = new Set(['task', 'test', 'share', 'paired', 'check', 'answer', 'answered', 'offer', 'accepted', 'approval', 'decided', 'ack', 'error'])
const LABEL = {
  task: 'task', test: 'tests', share: 'shares', paired: 'paired', check: 'check', answer: 'answer', answered: 'answered',
  offer: 'offer', accepted: 'accepted', approval: 'approval', decided: 'decision', ack: 'ack', error: 'error',
}
const WIRE = { PAIR: 'pair', QUERY: 'question', ANSWER: 'answer', REQUEST: 'request', CHECK: 'owner check', OFFER: 'signed offer', ACCEPT: 'accept', BUNDLE: 'sealed bundle', ACK: 'verdict' }

// Two real agents running on the server. Hidden when the API isn't reachable, so the static copy of the site still works.
export default function Duo() {
  const [snap, setSnap] = useState(null)
  const [events, setEvents] = useState([])
  const [offline, setOffline] = useState(false)
  const [error, setError] = useState('')
  const [open, setOpen] = useState({ alpha: '', beta: '' })
  const [step, setStep] = useState(false)
  const cursor = useRef(0)
  const run = useRef(0)

  const load = useCallback(async () => {
    try {
      const res = await fetch(`/api/duo?after=${cursor.current}`)
      if (!res.ok) throw new Error(String(res.status))
      const data = await res.json()
      if (data.run !== run.current) { // a new run started: drop the old timeline
        run.current = data.run
        cursor.current = 0
        setEvents([])
        if (data.events.length === 0 || data.events[0].n > 1) return load()
      }
      if (data.events.length) {
        cursor.current = data.events[data.events.length - 1].n
        setEvents((old) => [...old, ...data.events])
      }
      setSnap(data)
      setOffline(false)
    } catch {
      setOffline(true)
    }
  }, [])

  useEffect(() => {
    load()
  }, [load])

  const running = snap?.state === 'running'
  useEffect(() => {
    if (!running) return undefined
    const timer = setInterval(load, 700)
    return () => clearInterval(timer)
  }, [running, load])

  async function next() {
    setError('')
    try {
      const res = await fetch('/api/duo/next', { method: 'POST' })
      if (!res.ok) throw new Error(`error ${res.status}`)
      await load()
    } catch (err) {
      setError(err.message)
    }
  }

  async function start() {
    setError('')
    try {
      const res = await fetch(`/api/duo/start?step=${step}`, { method: 'POST' })
      const data = await res.json()
      if (!res.ok) throw new Error(data.error || `error ${res.status}`)
      cursor.current = 0
      run.current = 0
      setEvents([])
      await load()
    } catch (err) {
      setError(err.message)
    }
  }

  if (offline || snap === null) return null

  const files = snap.files || {}
  const wire = events.filter((e) => e.kind === 'wire')
  const idle = snap.state === 'idle'
  const cards = events.filter((e) => e.kind === 'explain').map((e) => e.card)
  const shown = snap.paused ? cards.find((c) => c.key === snap.paused) : null
  return (
    <section className="duo" id="duo" aria-labelledby="duo-title">
      <div className="wrap duo__inner">
        <h2 className="display" id="duo-title">Two live agents</h2>
        <p className="duo__intro">
          Alpha and beta are real agents on this server, each with its own project folder, keys and thread. First each
          builds something alone and runs its own tests. Then each finds it needs what the other built, and they trade
          it over the Ack protocol: ask, request, signed offer, sealed bundle, tests in a sandbox, a verdict. Alpha also
          keeps one part private, so you can watch a request get refused.
        </p>
        <div className="duo__bar">
          <button className="btn btn--red" type="button" onClick={start} disabled={running}>
            {running ? 'Running…' : idle ? 'Run the live demo' : 'Run it again'}
          </button>
          <label className="duo__step">
            <input type="checkbox" checked={step} disabled={running} onChange={(e) => setStep(e.target.checked)} />
            Step through with explanations
          </label>
          <span className="duo__state" role="status">
            {idle && 'Not run yet. It takes about half a minute.'}
            {running && (snap.paused ? 'Paused. Read the card, then continue.' : `Running, ${snap.elapsed}s in`)}
            {snap.state === 'done' && `Finished in ${snap.elapsed}s`}
            {snap.state === 'failed' && <span className="is-bad">The run failed: {snap.error}</span>}
          </span>
          {error && <span className="is-bad" role="alert">{error}</span>}
        </div>

        {shown && (
          <aside className="duo__card" aria-label="Explanation">
            <h3>{shown.title}</h3>
            <dl>
              <dt>What just happened</dt><dd>{shown.what}</dd>
              <dt>Why it is needed</dt><dd>{shown.why}</dd>
              <dt>The normal way</dt><dd>{shown.normal}</dd>
            </dl>
            <button className="btn btn--red" type="button" onClick={next} autoFocus>
              Continue ({cards.findIndex((c) => c.key === shown.key) + 1} of 6)
            </button>
          </aside>
        )}

        <div className="duo__grid">
          {['alpha', 'beta'].map((name) => (
            <article className="duo__agent" key={name} aria-label={`Agent ${name}`}>
              <header className="duo__head">
                <h3>{name}</h3>
                <span className={`duo__status${running ? ' is-live' : ''}`}>{snap.status[name]}</span>
              </header>
              <ol className="duo__log" aria-live="off">
                {events.filter((e) => e.agent === name && SHOWN.has(e.kind)).map((e) => (
                  <li key={e.n} className={`duo__ev duo__ev--${e.kind}${/DENIED|FAILED|REJECT/.test(e.text) ? ' is-bad' : ''}`}>
                    <span className="duo__kind">{LABEL[e.kind]}</span>
                    <span className="duo__text">{e.text}</span>
                  </li>
                ))}
                {!events.some((e) => e.agent === name) && <li className="duo__empty">Nothing yet.</li>}
              </ol>
              <div className="duo__files">
                <h4>{name}'s project folder</h4>
                {Object.keys(files[name] || {}).length === 0 && <p className="duo__empty">Empty.</p>}
                <ul>
                  {Object.keys(files[name] || {}).map((file) => {
                    const received = !OWN[name].includes(file)
                    return (
                      <li key={file}>
                        <button type="button" className="duo__file" aria-expanded={open[name] === file}
                          onClick={() => setOpen({ ...open, [name]: open[name] === file ? '' : file })}>
                          {file}{received && <em> received from {name === 'alpha' ? 'beta' : 'alpha'}</em>}
                        </button>
                        {open[name] === file && <pre className="duo__code">{files[name][file]}</pre>}
                      </li>
                    )
                  })}
                </ul>
              </div>
            </article>
          ))}
        </div>

        <div className="duo__wire">
          <h3>On the wire</h3>
          <p className="duo__note">What the broker saw. Bodies are sealed with a key only the two agents hold, so the broker sees who and what kind, never the content.</p>
          <ol>
            {wire.map((e) => (
              <li key={e.n}>
                <span className="duo__from">{e.agent}</span>
                <span className="duo__arrow" aria-hidden="true">→</span>
                <span className="duo__msg">{WIRE[e.text] || e.text}</span>
                <span className="duo__arrow" aria-hidden="true">→</span>
                <span className="duo__to">{e.to}</span>
              </li>
            ))}
            {wire.length === 0 && <li className="duo__empty">No messages yet.</li>}
          </ol>
        </div>

        {snap.state === 'done' && snap.summary?.decisions && (
          <div className="duo__result">
            <h3>Result</h3>
            <p>{snap.summary.messages} messages crossed the broker.</p>
            <ul>
              {snap.summary.decisions.map((d) => (
                <li key={d.agent + d.part}>
                  <strong>{d.agent}</strong> {d.decision.toLowerCase()}ed {d.part}: {d.reason}
                  {d.tests_passed ? ', and the owner’s tests passed in its sandbox.' : '.'}
                </li>
              ))}
            </ul>
            {cards.length > 0 && (
              <details className="duo__recap">
                <summary>What each step was for, and the normal way</summary>
                {cards.map((c) => (
                  <section key={c.key}>
                    <h4>{c.title}</h4>
                    <p>{c.why}</p>
                    <p><em>Normal way:</em> {c.normal}</p>
                  </section>
                ))}
              </details>
            )}
          </div>
        )}
      </div>
    </section>
  )
}
