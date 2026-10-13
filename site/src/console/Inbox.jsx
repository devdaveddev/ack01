import { useState } from 'react'
import { ago, short, useStatus } from '../lib/api.jsx'
import { Btn, Empty, Feedback, Input, Sheet, Stamp, stateStamp, useAct } from './ui.jsx'

export default function Inbox() {
  const { status } = useStatus()
  return (
    <div className="cgrid">
      <div className="cstack">
        <Sheet title="To sign" sub={`${status.inbox.filter((r) => !r.vote).length} waiting on you`}>
          {status.inbox.length === 0 ? <Empty>No announcements yet. Subscribe to a publisher and they arrive here.</Empty> : null}
          {status.inbox.map((r) => <Item key={r.statement.id} rec={r} />)}
        </Sheet>
      </div>
      <div className="cstack">
        <Policy policy={status.sign_policy} />
        <Subscriptions subs={status.subscriptions} />
      </div>
    </div>
  )
}

function Policy({ policy }) {
  const act = useAct()
  const set = (p) => act.run('sign-policy', { policy: p })
  return (
    <Sheet title="How I sign" sub="Policy">
      <div className="chips">
        <label className={`chip${policy === 'auto' ? ' is-on' : ''}`}>
          <input type="radio" name="policy" checked={policy === 'auto'} onChange={() => set('auto')} />Automatically
        </label>
        <label className={`chip${policy === 'manual' ? ' is-on' : ''}`}>
          <input type="radio" name="policy" checked={policy === 'manual'} onChange={() => set('manual')} />By hand
        </label>
      </div>
      <p className="ctext">
        {policy === 'auto'
          ? 'ACK goes out as soon as the publisher’s signature verifies and the digest of the copy matches.'
          : 'Copies that check out wait here for you to read and sign.'}{' '}
        A copy that fails its check is refused either way.
      </p>
      <Feedback act={act} />
    </Sheet>
  )
}

function Subscriptions({ subs }) {
  const act = useAct()
  const [name, setName] = useState('')
  const add = async (e) => {
    e.preventDefault()
    if (await act.run('subscribe', { publisher: name.trim() }, `Subscribed to ${name.trim()}.`)) setName('')
  }
  return (
    <Sheet title="Subscriptions" sub="Whose announcements I sign">
      <form className="crow" onSubmit={add}>
        <Input label="Publisher" value={name} onChange={(e) => setName(e.target.value)} placeholder="beta" required />
        <Btn type="submit" disabled={!!act.busy}>Subscribe</Btn>
      </form>
      <Feedback act={act} />
      <ul className="plain">
        {subs.publishers.map((p) => (
          <li key={p}>
            <span>{p}</span>
            <Btn onClick={() => act.run('subscribe', { publisher: p, on: false }, `Unsubscribed from ${p}.`)}>Unsubscribe</Btn>
          </li>
        ))}
        {subs.publishers.length === 0 && <li className="empty">Not subscribed to anyone.</li>}
      </ul>
      <p className="ctext">
        Subscribed to me: {subs.subscribers.length ? subs.subscribers.join(', ') : 'nobody yet'}.
      </p>
    </Sheet>
  )
}

function Item({ rec }) {
  const act = useAct()
  const [reason, setReason] = useState('')
  const st = rec.statement
  const c = rec.check
  const v = rec.vote
  const files = Object.entries(rec.files || {})
  return (
    <article className="filed">
      <div className="filed__head">
        <div>
          <h4 className="filed__title">{st.title}</h4>
          <span className="filed__meta">
            from {st.publisher} / {st.encrypted ? 'sealed for me' : 'open copy'} / {ago(rec.received)}
          </span>
        </div>
        {v ? <Stamp bad={v.verdict === 'NAK'}>{v.verdict === 'NAK' ? 'Refused' : 'Signed'}</Stamp> : <Stamp dim>Unsigned</Stamp>}
      </div>

      <dl className="checks">
        <div className={c.signature ? '' : 'is-bad'}><dt>Publisher signature</dt><dd>{c.signature ? 'verifies' : 'does not verify'}</dd></div>
        <div><dt>Announced digest</dt><dd>{short(st.digest)}</dd></div>
        <div className={c.match ? '' : 'is-bad'}><dt>Digest of my copy</dt><dd>{c.digest_seen ? short(c.digest_seen) : 'none'} {c.match ? '(match)' : '(mismatch)'}</dd></div>
        <div><dt>Quorum</dt><dd>{st.quorum} of {st.subscribers.length}: {st.subscribers.join(', ')}</dd></div>
      </dl>
      {c.problem && <p className="feedback is-bad">{c.problem}</p>}

      {files.length > 0 && (
        <details className="cdetails">
          <summary>Read the {files.length === 1 ? 'file' : `${files.length} files`} (shown as text, never run)</summary>
          {files.map(([path, text]) => (
            <div key={path} className="codebox">
              <div className="codebox__bar">{path}</div>
              <pre>{text}</pre>
            </div>
          ))}
        </details>
      )}

      {v ? (
        <p className="filed__tally">
          {v.verdict}: {v.reason}{rec.status ? ` / ${rec.status.state}, ${rec.status.acks} of ${rec.status.of} signed` : ''}
        </p>
      ) : (
        <div className="crow">
          <Input label="Reason (optional)" value={reason} onChange={(e) => setReason(e.target.value)} placeholder="Read it, matches what we discussed" />
          <Btn kind="red" disabled={!!act.busy || !(c.signature && c.match)} onClick={() => act.run('vote', { id: st.id, verdict: 'ACK', reason })}>Sign ACK</Btn>
          <Btn disabled={!!act.busy} onClick={() => act.run('vote', { id: st.id, verdict: 'NAK', reason })}>Refuse</Btn>
        </div>
      )}
      {rec.status && v && <div className="filed__state">{stateStamp(rec.status.state)}</div>}
      <Feedback act={act} />
    </article>
  )
}
