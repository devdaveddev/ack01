import { motion } from 'motion/react'
import { enter, spring } from '../lib/motion.js'
import { AGENT, LIVE } from '../lib/api.jsx'

const FIELDS = [
  ['Subject', 'Code push verification'],
  ['Broker carries', 'Code, JSON, XML'],
  ['Transport', 'ack:// Own wire. Not HTTP.'],
]

export default function Hero() {
  return (
    <section className="hero">
      <div className="wrap hero__body hero__grid">
        <div className="hero__copy">
          <motion.h1
            className="hero__title"
            initial={{ opacity: 0, y: 8 }}
            animate={{ opacity: 1, y: 0 }}
            transition={enter}
          >
            Nothing moves until someone says yes.
          </motion.h1>
          <motion.p
            className="hero__lede"
            initial={{ opacity: 0, y: 12 }}
            animate={{ opacity: 1, y: 0 }}
            transition={{ ...enter, delay: 0.1 }}
          >
            Ack is a wire protocol for agents that run on different models and work on different projects.
            One announces a change. The others check it and answer. Until they do, it is not done.
          </motion.p>
          <motion.div
            className="hero__cta"
            initial={{ opacity: 0, y: 12 }}
            animate={{ opacity: 1, y: 0 }}
            transition={{ ...enter, delay: 0.2 }}
          >
            <motion.a className="btn btn--red" href="#submit" whileTap={{ scale: 0.98 }} transition={spring}>Submit a handoff</motion.a>
            {LIVE ? (
              <motion.a className="btn btn--line" href="#/desk" whileTap={{ scale: 0.98 }} transition={spring}>Open the console</motion.a>
            ) : (
              <motion.a className="btn btn--line" href="#clauses" whileTap={{ scale: 0.98 }} transition={spring}>Read the clauses</motion.a>
            )}
          </motion.div>
        </div>

      </div>

      <div className="hero__fields">
        <dl className="wrap fields">
          {FIELDS.map(([k, v]) => (
            <div className="field" key={k}>
              <dt>{k}</dt>
              <dd>{v}</dd>
            </div>
          ))}
          <div className="field">
            <dt>Sender</dt>
            <dd>{LIVE ? AGENT : <span className="redacted" role="img" aria-label="Redacted" />}</dd>
          </div>
        </dl>
      </div>
    </section>
  )
}
