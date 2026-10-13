import { useEffect, useMemo, useState } from 'react'
import { api, useStatus } from '../lib/api.jsx'
import { Btn, Empty, Feedback, Input, Sheet, Stamp, useAct } from './ui.jsx'

export default function Parts() {
  const { status } = useStatus()
  return (
    <div className="cgrid">
      <div className="cstack">
        <Sheet title="Shared parts" sub={`${status.parts.length} offered`}>
          {status.parts.length === 0 ? <Empty>Nothing shared yet. Peers can only ask about and request what is listed here.</Empty> : null}
          {status.parts.map((p) => <Part key={p.id} part={p} />)}
        </Sheet>
      </div>
      <ShareForm />
    </div>
  )
}

function Part({ part }) {
  const act = useAct()
  const holders = Object.keys(part.holders || {})
  return (
    <article className="filed">
      <div className="filed__head">
        <div>
          <h4 className="filed__title">{part.id} <span className="filed__meta">v{part.version}</span></h4>
          <span className="filed__meta">{part.topic}</span>
        </div>
        {part.current ? <Stamp rot={2}>Current</Stamp> : <Stamp bad rot={2}>Changed</Stamp>}
      </div>
      <p className="ctext">{part.summary}</p>
      <p className="filed__files">{part.base ? `${part.base}/ ` : ''}{part.files.join(', ')}</p>
      <p className="filed__tally">
        Allowed: {part.allow.join(', ') || 'nobody'} / held by: {holders.join(', ') || 'nobody'}
      </p>
      <div className="crow">
        {!part.current && holders.length > 0 && (
          <Btn kind="red" onClick={() => act.run('update', { source_id: part.id }, (r) => `Told ${r.notified} holder(s).`)}>Send update</Btn>
        )}
        <Btn onClick={() => act.run('unshare', { id: part.id }, 'No longer shared.')}>Stop sharing</Btn>
        <Feedback act={act} />
      </div>
    </article>
  )
}

const BLANK = { id: '', topic: '', summary: '', base: '', tests: '', allow: '', tags: '', deps: '', notes: '' }

function ShareForm() {
  const act = useAct()
  const [f, setF] = useState(BLANK)
  const [tree, setTree] = useState([])
  const [picked, setPicked] = useState([])
  useEffect(() => {
    api('tree').then((r) => setTree(r.files)).catch(() => setTree([]))
  }, [])
  const set = (k) => (e) => setF({ ...f, [k]: e.target.value })
  const base = f.base.trim().replace(/\/+$/, '')
  const shown = useMemo(() => tree.filter((p) => !base || p.startsWith(base + '/')).slice(0, 150), [tree, base])
  const rel = (p) => (base ? p.slice(base.length + 1) : p)
  const toggle = (p) => setPicked(picked.includes(p) ? picked.filter((x) => x !== p) : [...picked, p])

  const submit = async (e) => {
    e.preventDefault()
    const r = await act.run('share', { ...f, files: picked.map(rel) }, `Sharing ${f.id}.`)
    if (r) {
      setF(BLANK)
      setPicked([])
    }
  }
  return (
    <Sheet title="Share a part" sub="Owner side">
      <form className="cform" onSubmit={submit}>
        <div className="crow">
          <Input label="Part id" value={f.id} onChange={set('id')} placeholder="retry-backoff" required />
          <Input label="Folder" value={f.base} onChange={set('base')} placeholder="lib/http" />
        </div>
        <Input label="Topic" value={f.topic} onChange={set('topic')} placeholder="retry flaky HTTP calls" required />
        <Input label="One-line summary" value={f.summary} onChange={set('summary')} required />
        <div className="input">
          <span className="input__label">Files ({picked.length} picked)</span>
          <div className="picker" role="group" aria-label="Project files">
            {shown.map((p) => (
              <label key={p} className={picked.includes(p) ? 'is-on' : undefined}>
                <input type="checkbox" checked={picked.includes(p)} onChange={() => toggle(p)} />
                <span>{p}</span>
              </label>
            ))}
            {shown.length === 0 && <span className="empty">No files under that folder.</span>}
          </div>
        </div>
        <div className="crow">
          <Input label="Allowed peers" value={f.allow} onChange={set('allow')} placeholder="alpha, gamma" hint="Names, or * for every paired agent" />
          <Input label="Test command" value={f.tests} onChange={set('tests')} placeholder="python -m unittest" />
        </div>
        <div className="crow">
          <Input label="Tags" value={f.tags} onChange={set('tags')} placeholder="http, retry" />
          <Input label="Dependencies" value={f.deps} onChange={set('deps')} />
        </div>
        <Input label="Notes for answers" area rows={3} value={f.notes} onChange={set('notes')} hint="Summarised when someone asks. Never sent whole." />
        <div className="crow">
          <Btn kind="red" type="submit" disabled={!!act.busy}>Share</Btn>
          <Feedback act={act} />
        </div>
      </form>
    </Sheet>
  )
}
