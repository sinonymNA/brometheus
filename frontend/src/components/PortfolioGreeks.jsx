import React from 'react'
import { formatGreek, formatMoney } from '../utils/formatting.js'

function GreekRow({ label, value, display, sub, color }) {
  return (
    <div className="flex items-baseline justify-between py-2 border-b border-border last:border-0">
      <div>
        <span className="text-xs muted uppercase tracking-widest">{label}</span>
        {sub && <span className="text-xs muted ml-2">{sub}</span>}
      </div>
      <div className="text-right">
        <span
          className="font-mono font-semibold text-base"
          style={{ color: color || '#E8EAED' }}
        >
          {display}
        </span>
      </div>
    </div>
  )
}

export default function PortfolioGreeks({ greeks }) {
  const g = greeks || { delta: 0, gamma: 0, vega: 0, theta: 0, rho: 0 }

  const deltaColor = Math.abs(g.delta) < 0.1 ? '#6B7280' : g.delta > 0 ? '#00D9FF' : '#FFB800'

  const gammaEmoji = g.gamma >= 0 ? '📈' : '📉'
  const gammaColor = g.gamma >= 0 ? '#00FF88' : '#FF0055'

  const vegaColor  = g.vega <= 0 ? '#00FF88' : '#FF0055'
  const vegaIcon   = g.vega <= 0 ? '🟢' : '🔴'
  const vegaLabel  = g.vega <= 0 ? 'short vol' : 'long vol'

  const thetaColor = g.theta < 0 ? '#00FF88' : '#FF0055'

  return (
    <div className="px-3 py-1">
      <GreekRow
        label="Delta"
        sub="directional"
        display={formatGreek(g.delta, 2)}
        color={deltaColor}
      />
      <GreekRow
        label="Gamma"
        sub={`${gammaEmoji} convexity`}
        display={formatGreek(g.gamma, 4)}
        color={gammaColor}
      />
      <GreekRow
        label="Vega"
        sub={`${vegaIcon} ${vegaLabel}`}
        display={formatGreek(g.vega, 4)}
        color={vegaColor}
      />
      <GreekRow
        label="Theta"
        sub="daily decay"
        display={formatMoney(g.theta * 100, 0) + '/day'}
        color={thetaColor}
      />
      <GreekRow
        label="Rho"
        sub="rate sensitivity"
        display={formatGreek(g.rho, 4)}
        color="#6B7280"
      />
    </div>
  )
}
