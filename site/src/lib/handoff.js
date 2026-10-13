// The handoff on the landing page, in two modes that render the same way.
//   live       the page is served by an Ack agent: a real announcement, fanned out by the broker
//              over the Ack wire, re-checked and signed by real subscribers (SHA-256, Ed25519).
//   simulated  no agent behind the page: the same story played in the browser with a stand-in hash.

export function fnv(s) {
  let h = 0x811c9dc5
  for (let i = 0; i < s.length; i++) {
    h ^= s.charCodeAt(i)
    h = Math.imul(h, 0x01000193) >>> 0
  }
  return h.toString(16).padStart(8, '0')
}

export const FILE = 'billing/charge.py'
export const PAYLOAD = [
  'def charge(order):',
  '    for attempt in range(3):',
  '        if gateway.send(order):',
  '            return True',
  '    return False',
]
// The broker's tamper test does the same thing: the first number in the file gets a 0 appended.
export const alter = (lines) => {
  let done = false
  return lines.map((l) => {
    if (done || !/\d/.test(l)) return l
    done = true
    return l.replace(/\d+/, (m) => m + '0')
  })
}
export const firstDiff = (a, b) => a.findIndex((l, i) => l !== b[i])

const TAMPERED = alter(PAYLOAD)
const DIGEST = fnv(PAYLOAD.join('\n'))
const BAD_DIGEST = fnv(TAMPERED.join('\n'))

export const STEPS = { IDLE: 0, ANNOUNCED: 1, DELIVERED: 2, CHECKING: 3, REPLIED: 4 }

const SIM_SIGNATORIES = [
  { id: 'review', name: 'agent/review', role: 'Reads the diff' },
  { id: 'tests', name: 'agent/tests', role: 'Runs the suite', tamperable: true },
  { id: 'lead', name: 'human/lead', role: 'Signs off' },
]

const pad = (n) => String(n).padStart(4, '0')

export function simulatedView(step, tamper) {
  const sigs = SIM_SIGNATORIES.map((s) => {
    const bad = Boolean(s.tamperable && tamper)
    let note = 'Waiting for the copy.'
    if (step === STEPS.DELIVERED) note = 'Copy received.'
    else if (step === STEPS.CHECKING) note = 'Comparing digest.'
    else if (step >= STEPS.REPLIED) note = bad ? 'Digest does not match. Refused.' : 'Digest matches. Signed.'
    return { ...s, bad, note, stamped: step >= STEPS.REPLIED, stampText: bad ? 'Refused' : 'Ack' }
  })

  const log = []
  let seq = 1
  const push = (who, verb, detail, bad = false) => log.push({ seq: pad(seq++), who, verb, detail, bad })
  if (step >= STEPS.ANNOUNCED) push('publisher', 'ANNOUNCE', `code ${FILE}, digest ${DIGEST}`)
  if (step >= STEPS.DELIVERED) push('broker', 'FANOUT', 'copy delivered to 3 subscribers')
  if (step >= STEPS.CHECKING) {
    sigs.forEach((s) => push(s.name, 'VERIFY', `computed ${s.bad ? BAD_DIGEST : DIGEST}, expected ${DIGEST}, ${s.bad ? 'mismatch' : 'match'}`, s.bad))
  }
  if (step >= STEPS.REPLIED) {
    sigs.forEach((s) => push(s.name, s.bad ? 'NAK' : 'ACK', s.bad ? 'digest mismatch, refusing the change' : 'digest matches', s.bad))
    push('broker', tamper ? 'HELD' : 'SEALED', tamper ? '2 of 3 acknowledged, waiting on agent/tests' : '3 of 3 acknowledged', tamper)
  }

  let disp
  if (step === STEPS.IDLE) disp = { word: 'Not filed', note: 'Press submit to begin.', tone: 'dim' }
  else if (step < STEPS.REPLIED) disp = { word: 'Pending', note: 'Signatures are still coming in.', tone: 'dim' }
  else if (tamper) disp = { word: 'Held', note: 'agent/tests received a copy that does not match the announcement and refused it. The other two signed.', tone: 'bad' }
  else disp = { word: 'Sealed', note: 'Every signatory matched the digest and signed.', tone: 'ink' }

  return {
    digest: DIGEST,
    lines: PAYLOAD,
    sigs,
    log,
    next: pad(seq),
    disp,
    done: step >= STEPS.REPLIED,
    bad: tamper && step >= STEPS.DELIVERED ? { who: 'agent/tests', digest: BAD_DIGEST, lines: TAMPERED, hot: firstDiff(PAYLOAD, TAMPERED) } : null,
    mode: 'Simulated',
    transport: 'Ack wire (simulated)',
  }
}

// A live announcement as /api/handoff/status returns it: {statement, votes, status, tampered, files, carbon}.
export function liveView(rec) {
  if (!rec) return null
  const st = rec.statement
  const lines = (rec.files?.[FILE] || PAYLOAD.join('\n')).replace(/\n$/, '').split('\n')
  const fanned = rec.carbon.some((e) => e.action === 'FANOUT')
  const sigs = st.subscribers.map((name) => {
    const v = rec.votes[name]?.vote
    const bad = v?.verdict === 'NAK'
    let note = fanned ? 'Copy delivered. Recomputing the digest.' : 'Waiting for the copy.'
    if (v && !bad) note = `Computed ${short(v.digest_seen)}. Matches. Signed.`
    else if (v) note = v.digest_seen ? `Computed ${short(v.digest_seen)}. Does not match. Refused.` : `${v.reason}. Refused.`
    return {
      id: name, name, bad, note, stamped: Boolean(v), stampText: bad ? 'Refused' : 'Ack',
      role: st.encrypted ? 'Sealed copy, opened with its pair key' : 'Open copy',
    }
  })

  const log = rec.carbon.map((e, i) => {
    const bad = e.action === 'NAK' || e.action === 'HELD'
    let detail = e.detail || ''
    if (e.action === 'ANNOUNCE') detail = `${e.title}, digest ${short(e.digest)}, ${e.encrypted ? 'sealed copies' : 'open copy'}`
    if (e.action === 'FANOUT') detail = `copy delivered to ${e.copies} subscriber${e.copies === 1 ? '' : 's'}`
    if (e.action === 'ACK' || e.action === 'NAK') detail = `computed ${short(e.digest_seen) || 'nothing'}, ${e.reason}`
    return { seq: pad(i + 1), who: e.agent || 'broker', verb: e.action, detail, bad }
  })

  const s = rec.status
  const refused = st.subscribers.filter((n) => rec.votes[n]?.vote.verdict === 'NAK')
  let disp
  if (s.state === 'sealed') disp = { word: 'Sealed', note: `${s.acks} of ${s.of} signed. Quorum was ${s.quorum}.${refused.length ? ` ${refused.join(', ')} refused.` : ''}`, tone: 'ink' }
  else if (s.state === 'held') disp = { word: 'Held', note: `${refused.join(', ')} refused ${refused.length === 1 ? 'its' : 'their'} copy. ${s.acks} of ${s.of} signed; quorum is ${s.quorum}, so it cannot seal.`, tone: 'bad' }
  else disp = { word: 'Pending', note: `Waiting on ${s.waiting.join(', ')}.`, tone: 'dim' }

  const t = rec.tampered[0]
  const badLines = alter(lines)
  return {
    id: st.id,
    digest: short(st.digest),
    lines,
    sigs,
    log,
    next: pad(log.length + 1),
    disp,
    done: s.waiting.length === 0 || s.state !== 'pending',
    bad: t ? { who: t, digest: short(rec.votes[t]?.vote.digest_seen) || 'checking', lines: badLines, hot: firstDiff(lines, badLines) } : null,
    mode: 'Live',
    transport: st.encrypted ? 'Ack wire, sealed per subscriber' : 'Ack wire, open',
  }
}

// Live mode before anything was submitted: the real signatories, nothing filed yet.
export function liveIdle(subscribers, encrypted) {
  const sigs = subscribers.map((name) => ({
    id: name, name, bad: false, stamped: false, stampText: 'Ack', note: 'Waiting for an announcement.',
    role: encrypted ? 'Sealed copy, opened with its pair key' : 'Open copy',
  }))
  return {
    digest: 'set when announced', lines: PAYLOAD, sigs, log: [], next: pad(1), done: false, bad: null, mode: 'Live',
    disp: { word: 'Not filed', note: 'Press submit to begin.', tone: 'dim' },
    transport: encrypted ? 'Ack wire, sealed per subscriber' : 'Ack wire, open',
  }
}

const short = (hex, n = 16) => (hex ? String(hex).slice(0, n) : '')
