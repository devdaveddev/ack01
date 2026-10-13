import { useState } from 'react'
import { useStatus } from '../lib/api.jsx'
import { Btn, Feedback, Rows, Sheet, useAct } from './ui.jsx'

// Shown above every console view while a receive is paused waiting for you.
export default function Approvals() {
  const { status } = useStatus()
  if (!status.approvals.length) return null
  return (
    <div className="approvals" role="region" aria-label="Waiting for you">
      {status.approvals.map((a) => <Approval key={a.id} a={a} />)}
    </div>
  )
}

function Approval({ a }) {
  const act = useAct()
  const c = a.context
  const install = a.action === 'install'
  const files = install ? c.report?.preview || {} : c.preview || {}
  const names = Object.keys(files)
  const [open, setOpen] = useState(names[0] || '')
  const r = c.report
  return (
    <Sheet tone="alert" title={install ? 'Install this part?' : 'Run its tests in the sandbox?'} sub={`${c.peer}/${c.source_id} v${c.version}`}>
      <p className="ctext">{a.detail}</p>
      <Rows rows={[
        ['From', c.peer],
        ['Why you asked', c.intent || '(no intent recorded)'],
        ['Files', (c.files || []).map((f) => `${f.path} (${f.size} B)`).join(', ')],
        c.tests?.cmd && ['Test command', c.tests.cmd.join(' ')],
        r && ['Tests', r.tests_ran ? (r.tests_passed ? 'passed in the sandbox' : 'failed in the sandbox') : 'not run'],
        r && r.conflicts?.length > 0 && ['Conflicts', r.conflicts.join(', ')],
        c.decision && ['Judge says', `${c.decision.action}: ${c.decision.reason}`],
      ]} />
      {names.length > 0 && (
        <div className="codebox">
          <div className="codebox__tabs" role="tablist">
            {names.map((n) => (
              <button key={n} type="button" role="tab" aria-selected={open === n} className={open === n ? 'is-on' : undefined} onClick={() => setOpen(n)}>{n}</button>
            ))}
          </div>
          <pre>{files[open]}</pre>
        </div>
      )}
      {install && r?.diff && (
        <details className="cdetails">
          <summary>Changes against what you have</summary>
          <pre className="diff">{r.diff}</pre>
        </details>
      )}
      <div className="crow">
        <Btn kind="red" disabled={!!act.busy} onClick={() => act.run('approve', { id: a.id, approve: true })}>{install ? 'Install' : 'Run tests'}</Btn>
        <Btn disabled={!!act.busy} onClick={() => act.run('approve', { id: a.id, approve: false })}>Reject</Btn>
        <Feedback act={act} />
      </div>
    </Sheet>
  )
}
