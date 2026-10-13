import { useEffect, useState } from 'react'
import { api, ago, useStatus } from '../lib/api.jsx'
import { Btn, Empty, Feedback, Input, Select, Sheet, Stamp, useAct } from './ui.jsx'

export default function Exchange() {
  const { status } = useStatus()
  const peers = status.peers.map((p) => p.name)
  const [part, setPart] = useState(null)
  if (peers.length === 0) {
    return (
      <Sheet title="Ask and request" sub="Paired, one to one">
        <Empty>Pair with an agent first (Peers). Questions, requests and parts only travel between paired agents, sealed.</Empty>
      </Sheet>
    )
  }
  return (
    <div className="cgrid">
      <div className="cstack">
        <Ask peers={peers} hints={status.hints} />
        <Sheet title="Answers" sub={status.waiting_answers ? `${status.waiting_answers} on the way` : 'Cached for an hour'}>
          {status.answers.length === 0 ? <Empty>No answers yet.</Empty> : null}
          {status.answers.map((a) => (
            <article className="answer" key={a.peer + a.question}>
              <div className="answer__q">
                <span>{a.peer}</span> {a.question}
                {a.typed && a.typed !== a.question ? <em> (you typed: {a.typed})</em> : null}
              </div>
              <p>{a.summary}</p>
              {a.sources?.length > 0 && (
                <div className="chips">
                  {a.sources.map((s) => (
                    <button type="button" key={s} className="chip chip--btn" onClick={() => setPart({ peer: a.peer, source_id: s })}>
                      Request {s}
                    </button>
                  ))}
                </div>
              )}
              <span className="answer__meta">confidence {a.confidence} / {ago(a.received)}</span>
            </article>
          ))}
        </Sheet>
      </div>
      <div className="cstack">
        <Request peers={peers} preset={part} />
        <Holdings status={status} />
      </div>
    </div>
  )
}

function Ask({ peers, hints }) {
  const act = useAct()
  const [peer, setPeer] = useState(peers[0])
  const [text, setText] = useState('')
  const [exact, setExact] = useState(false)
  const [fix, setFix] = useState(null)

  // Show the spelling fixes before sending, so nothing is changed behind your back.
  useEffect(() => {
    if (exact || !text.trim()) {
      setFix(null)
      return undefined
    }
    const t = setTimeout(() => api('spell', { text }).then((r) => setFix(r.changes.length ? r : null)).catch(() => setFix(null)), 300)
    return () => clearTimeout(t)
  }, [text, exact])

  const ask = async (e) => {
    e.preventDefault()
    const r = await act.run('ask', { peer, text, exact }, (r) => (r.cached ? 'Answered from the cache.' : `Sent to ${peer}. The answer appears below.`))
    if (r) setText('')
  }
  return (
    <Sheet title="Ask" sub="QUERY, sealed">
      <form className="cform" onSubmit={ask}>
        <div className="crow">
          <Select label="Peer" value={peer} onChange={(e) => setPeer(e.target.value)} options={peers} />
          <Input label="Question" value={text} onChange={(e) => setText(e.target.value)} placeholder="how do you retry flaky HTTP calls?" required />
        </div>
        {fix && <p className="input__hint">Will ask: {fix.text}</p>}
        <label className="check"><input type="checkbox" checked={exact} onChange={(e) => setExact(e.target.checked)} /> Send exactly as typed</label>
        {hints?.length > 0 && (
          <div className="chips">
            {hints.map((h) => <button type="button" key={h} className="chip chip--btn" onClick={() => setText(h)}>{h}</button>)}
          </div>
        )}
        <div className="crow">
          <Btn kind="red" type="submit" disabled={!!act.busy}>Ask</Btn>
          <Feedback act={act} />
        </div>
      </form>
    </Sheet>
  )
}

function Request({ peers, preset }) {
  const act = useAct()
  const [f, setF] = useState({ peer: peers[0], source_id: '', reason: '', intent: '' })
  useEffect(() => {
    if (preset) setF((x) => ({ ...x, ...preset }))
  }, [preset])
  const set = (k) => (e) => setF({ ...f, [k]: e.target.value })
  const send = async (e) => {
    e.preventDefault()
    const r = await act.run('request', f, 'Request sent. If the owner agrees, a review lands in Approvals.')
    if (r) setF({ ...f, source_id: '', reason: '', intent: '' })
  }
  return (
    <Sheet title="Request a part" sub="REQUEST, with a reason">
      <form className="cform" onSubmit={send}>
        <div className="crow">
          <Select label="Peer" value={f.peer} onChange={set('peer')} options={peers} />
          <Input label="Part id" value={f.source_id} onChange={set('source_id')} placeholder="retry-backoff" required />
        </div>
        <Input label="Why you need it" value={f.reason} onChange={set('reason')} placeholder="our HTTP calls fail on flaky networks" required />
        <Input label="What you will do with it" value={f.intent} onChange={set('intent')} placeholder="wrap outgoing requests in retry()" required />
        <p className="input__hint">Arrives sealed, goes to quarantine, and asks you twice: before its tests run in a sandbox, and before it is installed.</p>
        <div className="crow">
          <Btn kind="red" type="submit" disabled={!!act.busy}>Send request</Btn>
          <Feedback act={act} />
        </div>
      </form>
    </Sheet>
  )
}

function Holdings({ status }) {
  const act = useAct()
  const updates = Object.values(status.updates || {})
  return (
    <Sheet title="Received" sub="Adopted and adapted">
      {status.requests.length > 0 && (
        <p className="ctext">In progress: {status.requests.map((r) => `${r.source_id} from ${r.peer}`).join(', ')}</p>
      )}
      {status.installs.length === 0 ? <Empty>Nothing installed yet.</Empty> : null}
      <ul className="plain">
        {status.installs.map((i) => (
          <li key={i.peer + i.source_id}>
            <span>{i.peer}/{i.source_id} v{i.version}</span>
            <Stamp bad={i.action === 'REJECT'} rot={2}>{i.action}</Stamp>
          </li>
        ))}
      </ul>
      {updates.length > 0 && (
        <p className="ctext">Updates offered: {updates.map((u) => `${u.manifest?.source_id} v${u.manifest?.version}`).join(', ')}</p>
      )}
      <div className="crow">
        <Btn onClick={() => act.run('audit', {}, (r) => (r.findings.length ? r.findings.map((f) => `${f.kind} ${f.source}: ${f.detail}`).join(' / ') : 'Everything you hold matches its record.'))}>Audit what I hold</Btn>
        <Feedback act={act} />
      </div>
    </Sheet>
  )
}
