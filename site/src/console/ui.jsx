import { useState } from 'react'
import { motion } from 'motion/react'
import { api, useStatus } from '../lib/api.jsx'
import { spring } from '../lib/motion.js'

// Runs one API action at a time, refreshes the status after, and keeps the last error or note.
export function useAct() {
  const { refresh } = useStatus()
  const [busy, setBusy] = useState('')
  const [error, setError] = useState('')
  const [note, setNote] = useState('')
  const run = async (action, body = {}, done) => {
    setBusy(action)
    setError('')
    setNote('')
    try {
      const r = await api(action, body)
      if (done) setNote(typeof done === 'function' ? done(r) : done)
      await refresh()
      return r
    } catch (e) {
      setError(e.message)
      return null
    } finally {
      setBusy('')
    }
  }
  return { run, busy, error, note, setError, setNote }
}

export function Sheet({ title, sub, children, className = '', tone }) {
  return (
    <section className={`csheet${tone ? ` csheet--${tone}` : ''} ${className}`}>
      {(title || sub) && (
        <div className="csheet__top">
          {title && <h3 className="csheet__title">{title}</h3>}
          {sub && <span className="sheet__sub">{sub}</span>}
        </div>
      )}
      {children}
    </section>
  )
}

export function Btn({ kind = 'line', children, ...rest }) {
  return (
    <motion.button type="button" className={`btn btn--${kind} btn--sm`} whileTap={{ scale: 0.97 }} transition={spring} {...rest}>
      {children}
    </motion.button>
  )
}

export function Stamp({ children, bad, dim, rot = -3 }) {
  return (
    <motion.span
      className={`stamp stamp--sm${bad ? ' stamp--bad' : ''}${dim ? ' stamp--dim' : ''}`}
      initial={{ scale: 1.6, opacity: 0, rotate: rot }}
      animate={{ scale: 1, opacity: 1, rotate: rot, transition: spring }}
    >
      {children}
    </motion.span>
  )
}

export function Input({ label, hint, area, ...rest }) {
  return (
    <label className="input">
      <span className="input__label">{label}</span>
      {area ? <textarea {...rest} /> : <input {...rest} />}
      {hint && <span className="input__hint">{hint}</span>}
    </label>
  )
}

export function Select({ label, options, ...rest }) {
  return (
    <label className="input">
      <span className="input__label">{label}</span>
      <select {...rest}>
        {options.map((o) => (Array.isArray(o) ? <option key={o[0]} value={o[0]}>{o[1]}</option> : <option key={o} value={o}>{o}</option>))}
      </select>
    </label>
  )
}

export function Feedback({ act }) {
  if (act.error) return <p className="feedback is-bad" role="alert">{act.error}</p>
  if (act.note) return <p className="feedback" role="status">{act.note}</p>
  return null
}

export function Empty({ children }) {
  return <p className="empty">{children}</p>
}

export function Rows({ rows }) {
  return (
    <dl className="rows">
      {rows.filter(Boolean).map(([k, v]) => (
        <div key={k}><dt>{k}</dt><dd>{v}</dd></div>
      ))}
    </dl>
  )
}

export function stateStamp(state) {
  if (state === 'sealed') return <Stamp>Sealed</Stamp>
  if (state === 'held') return <Stamp bad>Held</Stamp>
  return <Stamp dim>Pending</Stamp>
}
