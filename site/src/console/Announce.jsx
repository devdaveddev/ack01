import { useEffect, useMemo, useState } from 'react'
import { AGENT, api, ago, short, useStatus } from '../lib/api.jsx'
import { Btn, Empty, Feedback, Input, Select, Sheet, Stamp, stateStamp, useAct } from './ui.jsx'

export default function Announce() {
  const { status } = useStatus()
  const subs = status.subscriptions.subscribers
  const peers = new Set(status.peers.map((p) => p.name))
  return (
    <div className="cgrid">
      <Compose subs={subs} peers={peers} />
      <div className="cstack">
        <Sheet title="Announced" sub={`${status.published.length} filed`}>
          <Refresh />
          {status.published.length === 0 ? <Empty>Nothing announced yet. Compose one, or submit a handoff from the landing page.</Empty> : null}
          {status.published.map((r) => <Published key={r.statement.id} rec={r} />)}
        </Sheet>
      </div>
    </div>
  )
}

function Refresh() {
  const act = useAct()
  return (
    <div className="crow crow--end">
      <Btn onClick={() => act.run('refresh', {}, 'Tallies pulled from the broker.')} disabled={!!act.busy}>Pull tallies</Btn>
      <Feedback act={act} />
    </div>
  )
}

function Compose({ subs, peers }) {
  const act = useAct()
  const [tree, setTree] = useState([])
  const [filter, setFilter] = useState('')
  const [picked, setPicked] = useState([])
  const [title, setTitle] = useState('')
  const [paste, setPaste] = useState({ path: '', content: '' })
  const [chosen, setChosen] = useState(null) // null means everyone subscribed
  const [encrypted, setEncrypted] = useState(false)
  const [quorum, setQuorum] = useState('')
  const [tamper, setTamper] = useState('')

  useEffect(() => {
    api('tree').then((r) => setTree(r.files)).catch(() => setTree([]))
  }, [])

  const to = chosen ?? subs
  const unpaired = to.filter((s) => !peers.has(s))
  const shown = useMemo(() => tree.filter((f) => f.toLowerCase().includes(filter.toLowerCase())).slice(0, 150), [tree, filter])
  const toggle = (list, item) => (list.includes(item) ? list.filter((x) => x !== item) : [...list, item])

  const submit = async (e) => {
    e.preventDefault()
    const r = await act.run('announce', {
      title, files: picked, paste, encrypted, subscribers: to, quorum: quorum || undefined, tamper: tamper ? [tamper] : [],
    }, (r) => `Announced. Waiting on ${r.status.waiting.join(', ')}.`)
    if (r) {
      setTitle('')
      setPicked([])
      setPaste({ path: '', content: '' })
      setTamper('')
    }
  }

  if (subs.length === 0) {
    return (
      <Sheet title="Announce a change" sub="Nobody subscribed yet">
        <p className="ctext">
          An announcement goes to agents that subscribe to you. Ask a peer to subscribe from their console
          (Inbox, then Subscribe to <b>{AGENT}</b>), or with <code>python -m ack agent ... subscribe {AGENT}</code>.
        </p>
      </Sheet>
    )
  }

  return (
    <Sheet title="Announce a change" sub="Signed, fanned out, counted">
      <form className="cform" onSubmit={submit}>
        <Input label="Title" value={title} onChange={(e) => setTitle(e.target.value)} placeholder="Retry fix for charge()" required />

        <div className="input">
          <span className="input__label">Files from this project ({picked.length} picked)</span>
          <input value={filter} onChange={(e) => setFilter(e.target.value)} placeholder="Filter files" aria-label="Filter files" />
          <div className="picker" role="group" aria-label="Project files">
            {shown.map((f) => (
              <label key={f} className={picked.includes(f) ? 'is-on' : undefined}>
                <input type="checkbox" checked={picked.includes(f)} onChange={() => setPicked(toggle(picked, f))} />
                <span>{f}</span>
              </label>
            ))}
            {shown.length === 0 && <span className="empty">No files match.</span>}
          </div>
        </div>

        <details className="cdetails">
          <summary>Or paste a file</summary>
          <Input label="Path" value={paste.path} onChange={(e) => setPaste({ ...paste, path: e.target.value })} placeholder="notes/change.md" />
          <Input label="Content" area rows={6} value={paste.content} onChange={(e) => setPaste({ ...paste, content: e.target.value })} />
        </details>

        <fieldset className="input">
          <legend className="input__label">Signatories</legend>
          <div className="chips">
            {subs.map((s) => (
              <label key={s} className={`chip${to.includes(s) ? ' is-on' : ''}`}>
                <input type="checkbox" checked={to.includes(s)} onChange={() => setChosen(toggle(to, s))} />
                {s}{peers.has(s) ? '' : ' (not paired)'}
              </label>
            ))}
          </div>
        </fieldset>

        <div className="crow">
          <fieldset className="input">
            <legend className="input__label">Copies</legend>
            <div className="chips">
              <label className={`chip${!encrypted ? ' is-on' : ''}`}>
                <input type="radio" name="mode" checked={!encrypted} onChange={() => setEncrypted(false)} />Open
              </label>
              <label className={`chip${encrypted ? ' is-on' : ''}${unpaired.length ? ' is-off' : ''}`} title={unpaired.length ? `Pair with ${unpaired.join(', ')} first` : ''}>
                <input type="radio" name="mode" checked={encrypted} disabled={unpaired.length > 0} onChange={() => setEncrypted(true)} />Sealed
              </label>
            </div>
          </fieldset>
          <Input label={`Quorum (of ${to.length})`} type="number" min={1} max={to.length} value={quorum}
            onChange={(e) => setQuorum(e.target.value)} placeholder={String(to.length)} />
        </div>
        <p className="input__hint">
          {encrypted
            ? 'Sealed: one copy per signatory, encrypted with that pair key. The broker stores ciphertext it cannot read.'
            : 'Open: one plain copy everyone fetches. The broker, and anyone holding the blob id, can read it.'}
        </p>

        <Select label="Test the check" value={tamper} onChange={(e) => setTamper(e.target.value)}
          options={[['', 'Send every copy as announced'], ...to.map((s) => [s, `Alter ${s}'s copy in transit`])]} />

        <div className="crow">
          <Btn kind="red" type="submit" disabled={!!act.busy || to.length === 0}>{act.busy ? 'Announcing' : 'Announce'}</Btn>
          <Feedback act={act} />
        </div>
      </form>
    </Sheet>
  )
}

function Published({ rec }) {
  const st = rec.statement
  const s = rec.status
  return (
    <article className="filed">
      <div className="filed__head">
        <div>
          <h4 className="filed__title">{st.title}</h4>
          <span className="filed__meta">
            {st.encrypted ? 'Sealed' : 'Open'} / digest {short(st.digest)} / {ago(st.ts)}
          </span>
        </div>
        {stateStamp(s.state)}
      </div>
      <p className="filed__files">{st.files.join(', ')}</p>
      <ul className="votes">
        {st.subscribers.map((n) => {
          const v = rec.votes[n]?.vote
          return (
            <li key={n}>
              <span className="votes__who">{n}{rec.tampered?.includes(n) ? <em> copy altered (test)</em> : null}</span>
              <span className="votes__why">{v ? `${v.reason}` : 'waiting'}</span>
              <span className="votes__stamp">{v ? <Stamp bad={v.verdict === 'NAK'}>{v.verdict === 'NAK' ? 'Refused' : 'Ack'}</Stamp> : null}</span>
            </li>
          )
        })}
      </ul>
      <p className="filed__tally">{s.acks} of {s.of} signed, {s.naks} refused, quorum {s.quorum}</p>
    </article>
  )
}
