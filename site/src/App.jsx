import { useEffect, useState } from 'react'
import { MotionConfig } from 'motion/react'
import Header from './components/Header.jsx'
import Hero from './components/Hero.jsx'
import Handoff from './components/Handoff.jsx'
import Ledger from './components/Ledger.jsx'
import Agents from './components/Agents.jsx'
import AgentWire from './components/AgentWire.jsx'
import ContextPack from './components/ContextPack.jsx'
import { Clauses, Filing, Footer, Permit, Ways } from './components/Sections.jsx'
import Console from './console/Console.jsx'
import { LIVE, StatusProvider } from './lib/api.jsx'

function WireSection() {
  return (
    <section className="wire-section" id="how" aria-labelledby="how-title">
      <div className="wrap wire-section__inner">
        <h2 className="display" id="how-title">One handoff, step by step</h2>
        <p className="wire-section__intro">Beta announces a change. Alpha recomputes the digest on its own copy and signs. Change the copy and watch alpha refuse.</p>
        <AgentWire />
      </div>
    </section>
  )
}

// "#/announce" and friends are console routes; plain anchors ("#submit") stay on the landing page.
function useRoute() {
  const read = () => (LIVE && window.location.hash.startsWith('#/') ? window.location.hash.slice(2) : '')
  const [route, setRoute] = useState(read)
  useEffect(() => {
    const on = () => {
      const next = read()
      setRoute((prev) => {
        if (prev !== next) window.scrollTo({ top: 0 })
        return next
      })
    }
    window.addEventListener('hashchange', on)
    return () => window.removeEventListener('hashchange', on)
  }, [])
  return route
}

export default function App() {
  const route = useRoute()
  const inConsole = Boolean(route)
  return (
    // reducedMotion="user" drops transform and layout animation for anyone who asks the OS for less motion.
    <MotionConfig reducedMotion="user">
      <StatusProvider every={inConsole ? 1500 : 3000}>
        <div id="top" className="page">
          <a className="skip" href={inConsole ? '#console-main' : '#submit'}>Skip to {inConsole ? 'content' : 'the demo'}</a>
          <Header console={inConsole} />
          {inConsole ? (
            <main id="console-main">
              <Console tab={route.split('/')[0]} />
            </main>
          ) : (
            <main>
              <Hero />
              <WireSection />
              <Handoff />
              <Ledger />
              <Agents />
              <ContextPack />
              <Ways />
              <Clauses />
              <Permit />
              <Filing />
            </main>
          )}
          <Footer />
        </div>
      </StatusProvider>
    </MotionConfig>
  )
}
