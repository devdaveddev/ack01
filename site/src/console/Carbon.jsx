import { useEffect, useState } from 'react'
import { api, ago, short, useStatus } from '../lib/api.jsx'
import { Btn, Empty, Sheet } from './ui.jsx'

const BAD = new Set(['NAK', 'HELD', 'DECLINE'])

function detail(e) {
  const bits = []
  if (e.title) bits.push(e.title)
  if (e.type) bits.push(e.type)
  if (e.to) bits.push(`to ${e.to}`)
  if (e.publisher) bits.push(e.publisher)
  if (e.digest) bits.push(`digest ${short(e.digest)}`)
  if (e.digest_seen) bits.push(`computed ${short(e.digest_seen)}`)
  if (e.copies) bits.push(`${e.copies} copies`)
  if (e.reason) bits.push(e.reason)
  if (e.detail) bits.push(e.detail)
  if (e.source_id) bits.push(`${e.owner}/${e.source_id} v${e.version} ${e.status}`)
  if (e.blob) bits.push(`blob ${short(e.blob, 12)} (${e.size} B)`)
  if (e.inviter) bits.push(`invite from ${e.inviter}`)
  return bits.join(', ')
}

export default function Carbon() {
  const { status } = useStatus()
  const [entries, setEntries] = useState(null)
  const [error, setError] = useState('')
  const [quiet, setQuiet] = useState(true)
  const load = () => api('carbon', {}).then((r) => { setEntries(r.entries); setError('') }).catch((e) => setError(e.message))
  useEffect(() => { load() }, [])
  const shown = (entries || []).filter((e) => !quiet || !e.action.startsWith('blob.'))
  return (
    <div className="cgrid">
      <div className="carbon carbon--console">
        <div className="carbon__top">
          <span className="carbon__title">Broker&apos;s carbon copy</span>
          <span className="sheet__sub">Entries that involve {status.me.name}</span>
        </div>
        <div className="crow carbon__tools">
          <Btn onClick={load}>Reload</Btn>
          <label className="check"><input type="checkbox" checked={quiet} onChange={(e) => setQuiet(e.target.checked)} /> Hide blob traffic</label>
        </div>
        {error && <p className="feedback is-bad">{error}</p>}
        <ol className="carbon__list">
          {entries && shown.length === 0 && <li className="empty">Nothing yet.</li>}
          {shown.map((e, i) => (
            <li className="entry" key={i}>
              <span className="entry__seq">{String(shown.length - i).padStart(4, '0')}</span>
              <span className="entry__body">
                <span className="entry__head">
                  <span className={`entry__verb${BAD.has(e.action) ? ' is-bad' : ''}`}>{e.action}</span>
                  <span className="entry__who">{e.agent || 'broker'}</span>
                  <span className="entry__who">{ago(e.ts)}</span>
                </span>
                <span className="entry__detail">{detail(e)}</span>
              </span>
            </li>
          ))}
        </ol>
      </div>
      <Sheet title="Activity" sub="This agent">
        {status.events.length === 0 ? <Empty>Quiet so far.</Empty> : null}
        <ol className="events">
          {status.events.slice(0, 80).map((e, i) => (
            <li key={i} className={e.kind === 'dropped' || e.kind === 'error' || e.kind === 'nak' ? 'is-bad' : undefined}>
              <b>{e.kind}</b> {e.text}
            </li>
          ))}
        </ol>
      </Sheet>
    </div>
  )
}
