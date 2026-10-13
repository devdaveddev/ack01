import { useEffect, useState } from 'react'

// One handoff, played between two agents: beta announces, alpha checks its copy and signs.
// Each step is what the protocol does; the broker only carries the messages. The toggle changes
// alpha's copy in transit, so alpha's recomputed digest no longer matches and it refuses.
const CYCLE_MS = 1900
const CLEAN = [
  { wire: null, beta: 'Idle', alpha: 'Idle', line: 'Waiting for a change.' },
  { wire: 'out', beta: 'Announcing', alpha: 'Idle', line: 'beta  ANNOUNCE  code/billing/charge.py  digest 29582b3e' },
  { wire: null, beta: 'Waiting', alpha: 'Recomputing digest', line: 'alpha VERIFY    recomputed 29582b3e  match' },
  { wire: 'back', beta: 'Waiting', alpha: 'Signing', line: 'alpha ACK       signed with its own key' },
  { wire: null, beta: 'Sealed', alpha: 'Signed', line: 'broker SEALED   1 of 1 acknowledged', end: 'sealed' },
]
const CHANGED = [
  { wire: null, beta: 'Idle', alpha: 'Idle', line: 'Waiting for a change.' },
  { wire: 'out', beta: 'Announcing', alpha: 'Idle', line: 'beta  ANNOUNCE  code/billing/charge.py  digest 29582b3e' },
  { wire: null, beta: 'Waiting', alpha: 'Recomputing digest', line: 'alpha VERIFY    recomputed 7c1d90ab  MISMATCH' },
  { wire: 'bad', beta: 'Waiting', alpha: 'Refusing', line: 'alpha NAK       digest differs, signed with its own key' },
  { wire: null, beta: 'Held', alpha: 'Refused', line: 'broker HELD     1 refusal, quorum cannot be reached', end: 'held' },
]

const reducedMotion = () =>
  typeof window !== 'undefined' && window.matchMedia?.('(prefers-reduced-motion: reduce)').matches

export default function AgentWire() {
  const [changed, setChanged] = useState(false)
  const [loop, setLoop] = useState(!reducedMotion())
  const steps = changed ? CHANGED : CLEAN
  const [step, setStep] = useState(reducedMotion() ? steps.length - 1 : 0)

  // Each run ends on its last step (sealed or held). With the loop on, it starts again after a pause.
  useEffect(() => {
    if (!loop) return undefined
    const timer = setInterval(() => setStep((s) => (s + 1) % (steps.length + 1)), CYCLE_MS)
    return () => clearInterval(timer)
  }, [loop, steps.length])

  // Changing the copy starts the handoff over.
  useEffect(() => {
    setStep(0)
  }, [changed])

  const current = steps[Math.min(step, steps.length - 1)]
  const done = step >= steps.length

  function send() {
    setLoop(false)
    setStep(0)
    // Replay the single run once, then stop on its end state.
    setTimeout(() => setStep(1), 60)
    setTimeout(() => setStep(2), 60 + CYCLE_MS)
    setTimeout(() => setStep(3), 60 + 2 * CYCLE_MS)
    setTimeout(() => setStep(4), 60 + 3 * CYCLE_MS)
    setTimeout(() => setStep(5), 60 + 4 * CYCLE_MS)
  }

  const alphaActive = current.wire === 'back' || current.wire === 'bad' || current.line.startsWith('alpha VERIFY')
  const shown = done ? steps[steps.length - 1] : current
  const alphaBad = shown.alpha === 'Refusing' || shown.alpha === 'Refused'

  return (
    <div className="wire" aria-label="Demonstration of one handoff between two agents">
      <div className="wire__controls">
        <button type="button" className="btn btn--red btn--sm" onClick={send}>Send a handoff</button>
        <button
          type="button"
          className={`btn btn--line btn--sm${changed ? ' is-on' : ''}`}
          aria-pressed={changed}
          onClick={() => setChanged((c) => !c)}
        >
          {changed ? 'Copy changed in transit' : "Change alpha's copy"}
        </button>
      </div>

      <div className="wire__agents">
        <div className={`agent${current.wire === 'out' ? ' is-active' : ''}`}>
          <span className="agent__name">beta</span>
          <span className="agent__role">Announces the change</span>
          <span className="agent__status" aria-live="polite">{done ? steps[steps.length - 1].beta : current.beta}</span>
        </div>

        <div className="wire__lane" aria-hidden="true">
          {current.wire && <span key={`${changed}-${step}`} className={`wire__packet is-${current.wire}`} />}
        </div>

        <div className={`agent${alphaActive ? ' is-active' : ''}${alphaBad ? ' is-bad' : ''}`}>
          <span className="agent__name">alpha</span>
          <span className="agent__role">Checks its copy, then signs</span>
          <span className="agent__status">{done ? steps[steps.length - 1].alpha : current.alpha}</span>
        </div>
      </div>

      <p className={`wire__log${alphaBad || steps[steps.length - 1].end === 'held' || current.end === 'held' ? ' is-bad' : ''}`} aria-live="polite">
        {done ? steps[steps.length - 1].line : current.line}
      </p>
    </div>
  )
}
