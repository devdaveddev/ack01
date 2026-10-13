import { useEffect, useRef, useState } from 'react'
import { AnimatePresence, motion, useAnimationControls } from 'motion/react'
import { spring, enter, exit, STAGGER } from '../lib/motion.js'
import { FILE, STEPS, liveIdle, liveView, simulatedView } from '../lib/handoff.js'
import { AGENT, LIVE, api, useStatus } from '../lib/api.jsx'

const STEP_MS = 1000
const POLL_MS = 450

function useSimulated() {
  const [step, setStep] = useState(STEPS.REPLIED)
  const [running, setRunning] = useState(false)
  const timers = useRef([])
  const clear = () => {
    timers.current.forEach(clearTimeout)
    timers.current = []
  }
  useEffect(() => clear, [])

  const run = () => {
    clear()
    if (running) {
      setRunning(false)
      setStep(STEPS.IDLE)
      return
    }
    setRunning(true)
    setStep(STEPS.ANNOUNCED)
    ;[STEPS.DELIVERED, STEPS.CHECKING, STEPS.REPLIED].forEach((s, i) => {
      timers.current.push(setTimeout(() => {
        setStep(s)
        if (s === STEPS.REPLIED) setRunning(false)
      }, (i + 1) * STEP_MS))
    })
  }
  return { step, running, run }
}

function useLive() {
  const [rec, setRec] = useState(null)
  const [running, setRunning] = useState(false)
  const [error, setError] = useState('')
  const timer = useRef(null)
  const token = useRef(0)
  useEffect(() => () => clearTimeout(timer.current), [])

  const run = async ({ tamper, encrypted }) => {
    clearTimeout(timer.current)
    const mine = ++token.current
    if (running) {
      setRunning(false)
      return
    }
    setError('')
    setRunning(true)
    try {
      let r = await api('handoff', { tamper, encrypted })
      const end = Date.now() + 30000
      const follow = async () => {
        if (mine !== token.current) return
        setRec(r)
        if (r.status.waiting.length === 0 || Date.now() > end) {
          setRunning(false)
          return
        }
        timer.current = setTimeout(async () => {
          try {
            r = await api('handoff/status', { id: r.statement.id })
            setError('')
          } catch (e) {
            setError(`${e.message}. Still watching.`) // a blip; the announcement carries on at the broker
          }
          follow()
        }, POLL_MS)
      }
      follow()
    } catch (e) {
      setError(e.message)
      setRunning(false)
    }
  }
  return { view: liveView(rec), running, run, error }
}

export default function Handoff() {
  const { status } = useStatus()
  const subscribers = status?.subscriptions?.subscribers || []
  const live = LIVE && subscribers.length > 0
  const [tamper, setTamper] = useState(false)
  const [encrypted, setEncrypted] = useState(false)
  const sim = useSimulated()
  const real = useLive()
  const sheet = useAnimationControls()

  const unpaired = subscribers.filter((s) => !(status?.peers || []).some((p) => p.name === s))
  const canSeal = live && unpaired.length === 0
  const view = !live ? simulatedView(sim.step, tamper) : real.view || liveIdle(subscribers, encrypted && canSeal)
  const running = live ? real.running : sim.running

  // The sheet takes a small hit when the final stamp lands.
  const wasDone = useRef(view.done)
  useEffect(() => {
    if (view.done && !wasDone.current) {
      sheet.start({ y: [0, 5, -2, 0], transition: { duration: 0.3, times: [0, 0.3, 0.65, 1], delay: 0.62 } })
    }
    wasDone.current = view.done
  }, [view.done, sheet])

  const submit = () => (live ? real.run({ tamper, encrypted: encrypted && canSeal }) : sim.run())
  const target = subscribers[0]

  return (
    <section className="stage" id="submit" aria-labelledby="submit-title">
      <div className="wrap stage__inner">
        <div className="stage__head">
          <div className="stage__intro">
            <h2 id="submit-title" className="display">Submit a handoff</h2>
            {live ? (
              <p>
                This one is real. {AGENT} announces a change over the Ack wire with its SHA-256 digest and an
                Ed25519 signature. The broker fans a copy out to {subscribers.join(', ')}. Each one recomputes the
                digest on the copy it actually received, then signs ACK or NAK with its own key. The quorum is all
                of them. Change {target}&apos;s copy in transit and see who notices.
              </p>
            ) : (
              <p>
                This runs in your browser. A change is announced with a digest. The broker passes a copy to three
                signatories. Each one recomputes the digest on the copy it received, then signs or refuses. Change one
                copy in transit and see who notices. The hash here is a stand in.
                {LIVE ? ` Nobody subscribes to ${AGENT} yet, so this run is simulated; ` : ' Run '}
                {LIVE ? <a className="inline-link" href="#/announce">add a subscriber in the console</a> : <code>python -m ack web --demo</code>}
                {LIVE ? '.' : ' to watch real agents do it.'}
              </p>
            )}
          </div>
          <div className="stage__controls">
            <motion.button
              type="button"
              className={`btn btn--ghost${tamper ? ' is-on' : ''}`}
              aria-pressed={tamper}
              onClick={() => setTamper((t) => !t)}
              whileTap={{ scale: 0.97 }}
              transition={spring}
            >
              {tamper ? (live ? `${target}'s copy changed` : 'Copy changed in transit') : (live ? `Change ${target}'s copy` : 'Change one copy in transit')}
            </motion.button>
            {live && (
              <motion.button
                type="button"
                className={`btn btn--ghost${encrypted && canSeal ? ' is-sealed' : ''}`}
                aria-pressed={encrypted && canSeal}
                disabled={!canSeal}
                title={canSeal ? 'One copy per subscriber, sealed with that pair key' : `Not paired with ${unpaired.join(', ')}`}
                onClick={() => setEncrypted((e) => !e)}
                whileTap={{ scale: 0.97 }}
                transition={spring}
              >
                {encrypted && canSeal ? 'Copies sealed' : 'Open copy'}
              </motion.button>
            )}
            <motion.button type="button" className="btn btn--red" onClick={submit} whileTap={{ scale: 0.97 }} transition={spring}>
              {running ? 'Stop' : 'Submit handoff'}
            </motion.button>
          </div>
        </div>
        {real.error && live && <p className="stage__error" role="alert">{real.error}</p>}

        <div className="stage__grid">
          <motion.div className="sheet" animate={sheet}>
            <div className="sheet__top">
              <span className="sheet__title">Form ACK-01</span>
              <span className="sheet__sub">Handoff receipt / {view.mode}</span>
            </div>

            <dl className="fields fields--sheet">
              <div className="field"><dt>Type</dt><dd>Code</dd></div>
              <div className="field"><dt>File</dt><dd>{FILE}</dd></div>
              <div className="field"><dt>Announced digest</dt><dd className="strong">{view.digest}</dd></div>
              <div className="field"><dt>Carried by</dt><dd>{view.transport}</dd></div>
            </dl>

            <div className="code" role="group" aria-label="Payload as announced">
              {view.lines.map((l, i) => <div key={i}>{l}</div>)}
            </div>

            <AnimatePresence initial={false}>
              {view.bad && (
                <motion.div
                  key="bad"
                  className="code code--bad"
                  initial={{ opacity: 0, y: -8 }}
                  animate={{ opacity: 1, y: 0, transition: enter }}
                  exit={{ opacity: 0, transition: exit }}
                  layout="position"
                >
                  <div className="code__bar">
                    <span>Copy delivered to {view.bad.who}</span>
                    <span className="typed">digest {view.bad.digest}</span>
                  </div>
                  <div className="code__body" role="group" aria-label={`Payload as delivered to ${view.bad.who}`}>
                    {view.bad.lines.map((l, i) => (
                      <div key={i} className={i === view.bad.hot ? 'is-hot' : undefined}>{l}</div>
                    ))}
                  </div>
                </motion.div>
              )}
            </AnimatePresence>

            <motion.div className="sigs" layout="position">
              <div className="sigs__label">Signatories</div>
              {view.sigs.map((s, i) => (
                <div className="sig" key={s.id}>
                  <div className="sig__who">
                    <span className="sig__name">{s.name}</span>
                    <span className="sig__role">{s.role}</span>
                  </div>
                  <span className={`sig__note${s.stamped && s.bad ? ' is-bad' : ''}${s.stamped && !s.bad ? ' is-ink' : ''}`}>{s.note}</span>
                  <div className="sig__slot">
                    <AnimatePresence>
                      {s.stamped && (
                        <motion.span
                          key={s.stampText}
                          className={`stamp${s.bad ? ' stamp--bad' : ''}`}
                          initial={{ scale: 1.9, opacity: 0 }}
                          animate={{ scale: 1, opacity: 1, transition: { ...spring, delay: i * 0.16 } }}
                          exit={{ opacity: 0, transition: exit }}
                        >
                          {s.stampText}
                        </motion.span>
                      )}
                    </AnimatePresence>
                  </div>
                </div>
              ))}
            </motion.div>

            <motion.div className="disp" layout="position">
              <div className="disp__text">
                <span className="disp__label">Disposition</span>
                <span className={`disp__word is-${view.disp.tone}`} aria-live="polite">{view.disp.word}</span>
                <span className="disp__note">{view.disp.note}</span>
              </div>
              <AnimatePresence>
                {view.done && view.disp.word !== 'Pending' && (
                  <motion.span
                    key={view.disp.word + (view.id || '')}
                    className={`stamp stamp--big${view.disp.tone === 'bad' ? ' stamp--bad' : ''}`}
                    initial={{ scale: 2.1, opacity: 0 }}
                    animate={{ scale: 1, opacity: 1, transition: { ...spring, delay: 0.5 } }}
                    exit={{ opacity: 0, transition: exit }}
                  >
                    {view.disp.word}
                  </motion.span>
                )}
              </AnimatePresence>
            </motion.div>
          </motion.div>

          <div className="carbon">
            <div className="carbon__top">
              <span className="carbon__title">Broker&apos;s carbon copy</span>
              <span className="sheet__sub">{view.mode === 'Live' ? 'From the broker’s audit log' : 'Simulated'}</span>
            </div>
            <ol className="carbon__list">
              <AnimatePresence initial={false}>
                {view.log.map((e) => (
                  <motion.li
                    key={(view.id || 'sim') + e.seq}
                    className="entry"
                    initial={{ opacity: 0, x: -10 }}
                    animate={{ opacity: 1, x: 0, transition: { ...enter, delay: STAGGER / 2 } }}
                    exit={{ opacity: 0, transition: exit }}
                  >
                    <span className="entry__seq">{e.seq}</span>
                    <span className="entry__body">
                      <span className="entry__head">
                        <span className={`entry__verb${e.bad ? ' is-bad' : ''}`}>{e.verb}</span>
                        <span className="entry__who">{e.who}</span>
                      </span>
                      <span className="entry__detail">{e.detail}</span>
                    </span>
                  </motion.li>
                ))}
              </AnimatePresence>
              <li className="entry entry--next" aria-hidden="true">
                <span className="entry__seq">{view.next}</span>
                <span className="entry__blank">________</span>
              </li>
            </ol>
          </div>
        </div>
      </div>
    </section>
  )
}
