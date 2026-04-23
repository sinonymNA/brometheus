import React, { useState, useEffect } from 'react'

function StatusRow({ label, value, valueClass = '' }) {
  return (
    <div className="flex justify-between items-center py-1.5 border-b border-border last:border-0 text-xs">
      <span className="muted">{label}</span>
      <span className={`font-mono ${valueClass}`}>{value}</span>
    </div>
  )
}

function Dot({ connected }) {
  return (
    <span className={`dot ${connected ? 'dot-green' : 'dot-red'} mr-1.5`} />
  )
}

export default function PipelineStatus({ pipelineState = {}, alpacaConnected, redisConnected, marketOpen }) {
  const [secondsAgo, setSecondsAgo] = useState(null)

  useEffect(() => {
    const lastCycle = pipelineState.last_cycle_at
    if (!lastCycle) { setSecondsAgo(null); return }

    function update() {
      const diff = Math.floor((Date.now() - new Date(lastCycle).getTime()) / 1000)
      setSecondsAgo(diff)
    }
    update()
    const iv = setInterval(update, 1000)
    return () => clearInterval(iv)
  }, [pipelineState.last_cycle_at])

  const cycleText = secondsAgo == null
    ? 'never'
    : secondsAgo < 5
    ? 'just now'
    : `${secondsAgo}s ago`

  const cycleClass = secondsAgo == null
    ? 'muted'
    : secondsAgo > 90
    ? 'neg'
    : secondsAgo > 70
    ? 'warning'
    : 'pos'

  return (
    <div className="px-3 py-1">
      <StatusRow
        label="Last cycle"
        value={cycleText}
        valueClass={cycleClass}
      />
      <StatusRow
        label="Options priced"
        value={pipelineState.options_priced_last_cycle ?? '—'}
        valueClass="accent"
      />
      <StatusRow
        label="Market"
        value={marketOpen ? 'OPEN' : 'CLOSED'}
        valueClass={marketOpen ? 'pos' : 'muted'}
      />
      <div className="flex justify-between items-center py-1.5 border-b border-border text-xs">
        <span className="muted">Alpaca</span>
        <span>
          <Dot connected={alpacaConnected} />
          <span className={alpacaConnected ? 'pos' : 'neg'}>
            {alpacaConnected ? 'connected' : 'disconnected'}
          </span>
        </span>
      </div>
      <div className="flex justify-between items-center py-1.5 text-xs">
        <span className="muted">Redis</span>
        <span>
          <Dot connected={redisConnected} />
          <span className={redisConnected ? 'pos' : 'neg'}>
            {redisConnected ? 'connected' : 'disconnected'}
          </span>
        </span>
      </div>
    </div>
  )
}
