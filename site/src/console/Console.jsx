import { AnimatePresence, motion } from 'motion/react'
import { useStatus } from '../lib/api.jsx'
import { enter, exit } from '../lib/motion.js'
import Announce from './Announce.jsx'
import Approvals from './Approvals.jsx'
import Carbon from './Carbon.jsx'
import Exchange from './Exchange.jsx'
import Inbox from './Inbox.jsx'
import Parts from './Parts.jsx'
import Peers from './Peers.jsx'
import { Rows, Sheet } from './ui.jsx'

export const TABS = [
  ['desk', 'Desk', Desk],
  ['announce', 'Announce', Announce],
  ['inbox', 'Sign', Inbox],
  ['exchange', 'Ask & request', Exchange],
  ['peers', 'Peers', Peers],
  ['parts', 'Parts', Parts],
  ['carbon', 'Carbon copy', Carbon],
]

export default function Console({ tab }) {
  const { status, error } = useStatus()
  const current = TABS.find(([id]) => id === tab) || TABS[0]
  const View = current[2]
  const toSign = status ? status.inbox.filter((r) => !r.vote).length : 0
  const counts = { inbox: toSign, peers: 0, announce: status ? status.published.filter((r) => r.status.state === 'pending').length : 0 }

  return (
    <div className="console">
      <div className="console__bar">
        <div className="wrap console__barrow">
          <h1 className="console__title">{current[1]}</h1>
          <nav className="console__tabs" aria-label="Console">
            {TABS.map(([id, label]) => (
              <a key={id} href={`#/${id}`} aria-current={id === current[0] ? 'page' : undefined}>
                {label}{counts[id] ? <span className="badge">{counts[id]}</span> : null}
              </a>
            ))}
            <a href="/classic" className="console__classic">Classic view</a>
          </nav>
        </div>
      </div>
      <div className="wrap console__body">
        {error && <p className="feedback is-bad" role="alert">Can&apos;t reach this agent: {error}</p>}
        {status?.me.broker_error && <p className="feedback is-bad" role="alert">Broker: {status.me.broker_error}</p>}
        {!status ? (
          <p className="empty">Connecting to the agent.</p>
        ) : (
          <>
            <Approvals />
            <AnimatePresence mode="wait" initial={false}>
              <motion.div
                key={current[0]}
                initial={{ opacity: 0, y: 8 }}
                animate={{ opacity: 1, y: 0, transition: enter }}
                exit={{ opacity: 0, transition: exit }}
              >
                <View />
              </motion.div>
            </AnimatePresence>
          </>
        )}
      </div>
    </div>
  )
}

function Desk() {
  const { status } = useStatus()
  const s = status
  const pending = s.published.filter((r) => r.status.state === 'pending').length
  const toSign = s.inbox.filter((r) => !r.vote).length
  const tiles = [
    ['Paired', s.peers.length, '#/peers', 'agents you can ask, request from, and seal copies for'],
    ['Subscribed to me', s.subscriptions.subscribers.length, '#/announce', 'signatories for what you announce'],
    ['I sign for', s.subscriptions.publishers.length, '#/inbox', 'publishers whose announcements reach you'],
    ['To sign', toSign, '#/inbox', s.sign_policy === 'auto' ? 'signing automatically when checks pass' : 'signing by hand'],
    ['Announced, pending', pending, '#/announce', 'waiting on signatures'],
    ['Parts shared', s.parts.length, '#/parts', 'what peers can ask about and request'],
  ]
  return (
    <div className="cstack">
      <div className="tiles">
        {tiles.map(([k, n, href, note]) => (
          <a className="tile" href={href} key={k}>
            <span className="tile__n">{n}</span>
            <span className="tile__k">{k}</span>
            <span className="tile__note">{note}</span>
          </a>
        ))}
      </div>
      <div className="cgrid">
        <Sheet title={s.me.name} sub="This agent">
          <Rows rows={[
            ['Fingerprint', s.me.fingerprint],
            ['Broker', s.me.broker],
            ['Transport', s.transport === 'ack wire' ? 'Ack wire: framed TCP, not HTTP' : 'HTTP (compatibility)'],
            ['Registered', s.me.registered ? 'yes' : 'no'],
            ['Project', s.me.root],
          ]} />
          <p className="ctext">
            Context packs, the full parts editor and the provenance log are in the <a className="inline-link" href="/classic">classic view</a>.
          </p>
        </Sheet>
        <Sheet title="Lately" sub="Activity">
          <ol className="events">
            {s.events.slice(0, 10).map((e, i) => (
              <li key={i} className={e.kind === 'dropped' || e.kind === 'error' || e.kind === 'nak' ? 'is-bad' : undefined}>
                <b>{e.kind}</b> {e.text}
              </li>
            ))}
            {s.events.length === 0 && <li className="empty">Nothing yet. Try the handoff on the landing page.</li>}
          </ol>
        </Sheet>
      </div>
    </div>
  )
}
