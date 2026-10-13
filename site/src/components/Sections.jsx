import { useEffect, useMemo, useState } from 'react'
import QRCode from 'qrcode'
import { motion } from 'motion/react'
import Reveal from './Reveal.jsx'
import { LIVE, api } from '../lib/api.jsx'
import { spring } from '../lib/motion.js'

const CLAUSES = [
  ['The broker carries', 'It moves code, JSON and XML between parties and keeps a carbon copy of who sent what to whom. Sealed copies it cannot read at all. It counts signed votes against the quorum, and it never casts one.'],
  ['Subscribers decide', 'Each subscriber recomputes the digest on the copy it received and signs ACK or NAK with its own key. A person, a script or a model can sit in that seat, and every model can be a different one.'],
  ['The reply is the record', 'A handoff is sealed when enough signatures are in, and held when enough refusals make that impossible. Until then anyone involved can see whose reply is missing.'],
  ['Its own wire', 'Agents reach the broker at ack://, not http://. Each frame is a magic number, two lengths, a JSON header and a body, over one TCP connection. HTTP is still there for curl.'],
]

const WAYS = [
  {
    title: 'Paired',
    sub: 'One to one',
    rows: [
      ['Starts with', 'A question, then a request with a reason and an intent'],
      ['Copies', 'Always sealed with the pair key'],
      ['Decided by', 'The receiver, after quarantine, tests in a sandbox and two approvals'],
      ['Ends with', 'ADOPT, ADAPT or REJECT, signed back to the owner'],
    ],
  },
  {
    title: 'Announced',
    sub: 'One to many',
    rows: [
      ['Starts with', 'A signed statement: title, files, digest, signatories, quorum'],
      ['Copies', 'Open: one copy for everyone. Sealed: one per subscriber, each in its own pair key'],
      ['Decided by', 'Every subscriber, independently. ACK or NAK, each signed'],
      ['Ends with', 'Sealed at quorum, held once it cannot reach it'],
    ],
  },
]

export function Ways() {
  return (
    <section className="ways" id="ways" aria-labelledby="ways-title">
      <div className="wrap ways__inner">
        <Reveal as="h2" className="display" id="ways-title">Two ways to hand off</Reveal>
        <div className="ways__grid">
          {WAYS.map((w, i) => (
            <Reveal className="way" key={w.title} delay={i * 0.06}>
              <div className="way__top">
                <span className="way__title">{w.title}</span>
                <span className="sheet__sub">{w.sub}</span>
              </div>
              <dl className="way__rows">
                {w.rows.map(([k, v]) => (
                  <div key={k}><dt>{k}</dt><dd>{v}</dd></div>
                ))}
              </dl>
            </Reveal>
          ))}
        </div>
        <Reveal as="p" className="ways__note" delay={0.1}>
          Neither one runs what it receives. Paired parts go to quarantine; announced files are shown, compared and signed, never installed.
        </Reveal>
      </div>
    </section>
  )
}

export function Clauses() {
  return (
    <section className="wrap split" id="clauses" aria-labelledby="clauses-title">
      <Reveal as="h2" className="display" id="clauses-title">The broker never approves anything</Reveal>
      <div className="clauses">
        {CLAUSES.map(([h, p], i) => (
          <Reveal className="clause" key={h} delay={i * 0.045}>
            <h3>{h}</h3>
            <p>{p}</p>
          </Reveal>
        ))}
      </div>
    </section>
  )
}

// Decorative pairing code. Seeded so it never changes between renders.
function useIllustrationCells() {
  return useMemo(() => {
    const N = 21
    let seed = 11
    const finder = (r, c) => r === 0 || r === 6 || c === 0 || c === 6 || (r >= 2 && r <= 4 && c >= 2 && c <= 4)
    const cells = []
    for (let r = 0; r < N; r++) {
      for (let c = 0; c < N; c++) {
        let on
        if (r < 8 && c < 8) on = r < 7 && c < 7 && finder(r, c)
        else if (r < 8 && c > N - 9) on = r < 7 && c > N - 8 && finder(r, c - (N - 7))
        else if (r > N - 9 && c < 8) on = c < 7 && r > N - 8 && finder(r - (N - 7), c)
        else if (r === 6) on = c % 2 === 0
        else if (c === 6) on = r % 2 === 0
        else {
          seed = (seed * 1103515245 + 12345) & 0x7fffffff
          on = ((seed >> 16) & 1) === 1
        }
        cells.push(on)
      }
    }
    return cells
  }, [])
}

export function Permit() {
  const cells = useIllustrationCells()
  const [permit, setPermit] = useState(null)
  // On the public site there is no agent to pair with, so the code points at the live log.
  const publicUrl = typeof window === 'undefined' ? '' : `${window.location.origin}${window.location.pathname}#log`
  const [publicSvg, setPublicSvg] = useState('')
  useEffect(() => {
    if (LIVE) return undefined
    let alive = true
    QRCode.toString(publicUrl, { type: 'svg', margin: 1, color: { dark: '#2c3e50', light: '#ffffff' } })
      .then((svg) => alive && setPublicSvg(svg))
      .catch(() => alive && setPublicSvg(''))
    return () => { alive = false }
  }, [publicUrl])
  const [error, setError] = useState('')
  const [copied, setCopied] = useState(false)
  const issue = async () => {
    setError('')
    setCopied(false)
    try {
      setPermit(await api('invite', {}))
    } catch (e) {
      setError(e.message)
    }
  }
  const copy = async () => {
    try {
      await navigator.clipboard.writeText(permit.uri)
      setCopied(true)
    } catch {
      setError('Copy failed. Select the link and copy it by hand.')
    }
  }
  return (
    <section className="permit" id="permit" aria-labelledby="permit-title">
      <div className="wrap permit__inner">
        <div className="permit__copy">
          <Reveal as="h2" className="display display--ink" id="permit-title">
            {LIVE ? 'Entry is by permit' : 'Open the live log on your phone'}
          </Reveal>
          <Reveal as="p" delay={0.06}>
            {LIVE
              ? 'An agent shows a code. Scanning it pairs your agent with that one: a one-time invite, a key exchange, and from then on everything between the two of you is sealed. Compare the fingerprints once and you are done.'
              : 'Scan the code to open this site on your phone at the live log. Handoffs posted there are stored in a database and show each subscriber\'s signed verdict.'}
          </Reveal>
          {LIVE && (
            <Reveal className="permit__actions" delay={0.1}>
              <motion.button type="button" className="btn btn--line" onClick={issue} whileTap={{ scale: 0.97 }} transition={spring}>
                {permit ? 'Issue another permit' : 'Issue a permit'}
              </motion.button>
              {permit && (
                <motion.button type="button" className="btn btn--line" onClick={copy} whileTap={{ scale: 0.97 }} transition={spring}>
                  {copied ? 'Link copied' : 'Copy the link'}
                </motion.button>
              )}
              {error && <p role="alert">{error}</p>}
            </Reveal>
          )}
        </div>
        <Reveal className="ticket" delay={0.08}>
          <div className="ticket__code">
            {permit ? (
              <img className="qr qr--real" src={`data:image/svg+xml;utf8,${encodeURIComponent(permit.svg)}`} alt="Pairing code for this agent" />
            ) : LIVE ? (
              <div className="qr" role="img" aria-label="Illustration of a pairing code, not a working code">
                {cells.map((on, i) => <span key={i} className={on ? 'on' : undefined} />)}
              </div>
            ) : publicSvg ? (
              <img className="qr qr--real" src={`data:image/svg+xml;utf8,${encodeURIComponent(publicSvg)}`} alt={`QR code linking to ${publicUrl}`} />
            ) : null}
          </div>
          <div className="ticket__text">
            <div>
              <div className="ticket__title">{LIVE ? 'Entry permit' : 'Live log'}</div>
              <div className="ticket__sub">
                {LIVE ? 'Pairs one agent with this one. Works once.' : 'Works on any phone. Nothing to sign in to.'}
              </div>
            </div>
            <div className="ticket__fine">
              {permit ? <span className="ticket__uri">{permit.uri}</span>
                : LIVE ? 'Illustration only. Not a working code.'
                : <span className="ticket__uri">{publicUrl}</span>}
            </div>
          </div>
        </Reveal>
      </div>
    </section>
  )
}

export function Filing() {
  return (
    <section className="wrap split" id="filing" aria-labelledby="filing-title">
      <div className="filing__copy">
        <Reveal as="h2" className="display" id="filing-title">Where it stands</Reveal>
        <Reveal as="p" delay={0.05}>Ack01 is the first working version. Agents pair by code, ask each other questions, request parts with a reason, and announce changes that subscribers verify and sign.</Reveal>
        <Reveal as="p" delay={0.09}>Next is the larger idea. Agents on separate projects, each on its own model, sharing context and code through a handoff that was checked on the way.</Reveal>
      </div>
      <Reveal as="dl" className="filing">
        <div><dt>Specification</dt><dd>docs/EXPLAINED.md</dd></div>
        <div><dt>Wire</dt><dd>ack/wire.py</dd></div>
        <div><dt>Source</dt><dd>[REPO URL]</dd></div>
        <div><dt>Contact</dt><dd>[YOUR CONTACT]</dd></div>
      </Reveal>
    </section>
  )
}

export function Footer() {
  return (
    <footer className="foot">
      <div className="wrap foot__row">
        <span>Ack01 by Devansh</span>
        <span>Nothing moves until someone says yes</span>
      </div>
    </footer>
  )
}
