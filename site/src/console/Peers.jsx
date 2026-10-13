import { useState } from 'react'
import { useStatus } from '../lib/api.jsx'
import { Btn, Empty, Feedback, Input, Rows, Sheet, Stamp, useAct } from './ui.jsx'

export default function Peers() {
  const { status } = useStatus()
  const me = status.me
  return (
    <div className="cgrid">
      <div className="cstack">
        <Sheet title="Paired agents" sub={`${status.peers.length} paired`}>
          {status.peers.length === 0 ? <Empty>No pairings yet. Issue a permit and send it, or join someone else&apos;s.</Empty> : null}
          <ul className="peers">
            {status.peers.map((p) => <Peer key={p.name} peer={p} />)}
          </ul>
          <p className="ctext">
            Compare fingerprints with the other person over a channel you trust (a call, in person). If they match,
            mark the peer checked. If the key ever changes, the mark goes away.
          </p>
        </Sheet>
        <Sheet title="This agent" sub="Identity">
          <Rows rows={[
            ['Name', me.name],
            ['Fingerprint', me.fingerprint],
            ['Broker', me.broker],
            ['Transport', status.transport],
            ['Project', me.root],
          ]} />
        </Sheet>
      </div>
      <div className="cstack">
        <IssuePermit />
        <Join />
      </div>
    </div>
  )
}

function Peer({ peer }) {
  const act = useAct()
  return (
    <li>
      <div>
        <span className="peers__name">{peer.name}</span>
        <span className="peers__fp">{peer.fingerprint}</span>
      </div>
      {peer.checked ? <Stamp rot={2}>Checked</Stamp> : <Btn onClick={() => act.run('check-peer', { peer: peer.name })}>Fingerprint matches</Btn>}
      <Feedback act={act} />
    </li>
  )
}

function IssuePermit() {
  const act = useAct()
  const [permit, setPermit] = useState(null)
  const [copied, setCopied] = useState(false)
  const issue = async () => {
    setCopied(false)
    const r = await act.run('invite', {})
    if (r) setPermit(r)
  }
  const copy = async () => {
    try {
      await navigator.clipboard.writeText(permit.uri)
      setCopied(true)
    } catch {
      act.setError('Copy failed. Select the link and copy it by hand.')
    }
  }
  return (
    <Sheet title="Issue a permit" sub="Pairing code" tone="red">
      <p className="ctext">Each permit works once. Whoever redeems it is paired with you and can ask, request and receive sealed copies.</p>
      <div className="ticket ticket--console">
        <div className="ticket__code">
          {permit ? (
            <img className="qr qr--real" src={`data:image/svg+xml;utf8,${encodeURIComponent(permit.svg)}`} alt="Pairing code" />
          ) : (
            <div className="qr qr--blank" aria-hidden="true" />
          )}
        </div>
        <div className="ticket__text">
          <div className="ticket__title">Entry permit</div>
          <div className="ticket__fine">{permit ? <span className="ticket__uri">{permit.uri}</span> : 'Not issued'}</div>
        </div>
      </div>
      <div className="crow">
        <Btn kind="red" onClick={issue} disabled={!!act.busy}>{permit ? 'Issue another' : 'Issue a permit'}</Btn>
        {permit && <Btn onClick={copy}>{copied ? 'Copied' : 'Copy link'}</Btn>}
      </div>
      <Feedback act={act} />
    </Sheet>
  )
}

function Join() {
  const act = useAct()
  const [uri, setUri] = useState('')
  const join = async (e) => {
    e.preventDefault()
    if (await act.run('join', { uri }, 'Paired. Compare fingerprints with them before you trust it.')) setUri('')
  }
  return (
    <Sheet title="Redeem a permit" sub="Join">
      <form className="cform" onSubmit={join}>
        <Input label="Permit link" area rows={3} value={uri} onChange={(e) => setUri(e.target.value)} placeholder="ack://pair?..." required />
        <div className="crow">
          <Btn kind="red" type="submit" disabled={!!act.busy}>Join</Btn>
          <Feedback act={act} />
        </div>
      </form>
    </Sheet>
  )
}
