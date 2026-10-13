import { AGENT, LIVE, useStatus } from '../lib/api.jsx'

export default function Header({ console: inConsole = false }) {
  const { status, error } = useStatus()
  const waiting = status?.approvals?.length || 0
  return (
    <header className="masthead">
      <div className="wrap masthead__row">
        <a className="masthead__mark" href="#top">Ack01</a>
        <nav className="masthead__nav" aria-label="Sections">
          {inConsole ? (
            <a href="#top">Landing</a>
          ) : (
            <>
              <a href="#submit">Submit</a>
              <a href="#log">Log</a>
              <a href="#agents">Agents</a>
              <a href="#context">Context</a>
              <a href="#ways">Two ways</a>
              <a href="#clauses">Clauses</a>
              <a href="#permit">Permit</a>
            </>
          )}
          {LIVE && (
            <a href="#/desk" className={inConsole ? 'is-here' : undefined}>
              Console{waiting ? <span className="badge" aria-label={`${waiting} waiting for you`}>{waiting}</span> : null}
            </a>
          )}
        </nav>
        <span className="masthead__form">
          {LIVE ? (
            <>
              Agent {AGENT} / {error ? <span className="is-bad">offline</span> : status?.transport || 'connecting'}
            </>
          ) : 'Form ACK-01 / Prototype'}
        </span>
      </div>
    </header>
  )
}
