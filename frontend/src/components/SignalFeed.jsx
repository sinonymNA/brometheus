import React from 'react'
import { formatTime } from '../utils/formatting.js'

const DIRECTION_COLOR = { long: 'pos', short: 'neg' }
const STRATEGY_COLORS = {
  momentum: '#00D9FF',
  iv_rank:  '#FFB800',
  flow:     '#A78BFA',
}

function StrengthBar({ strength }) {
  const pct = Math.round((strength ?? 0) * 100)
  const color = pct >= 70 ? '#00FF88' : pct >= 40 ? '#FFB800' : '#6B7280'
  return (
    <div className="flex items-center gap-1">
      <div className="w-16 h-1.5 rounded-full overflow-hidden" style={{ background: '#1E2A4A' }}>
        <div
          className="h-full rounded-full transition-all"
          style={{ width: `${pct}%`, background: color }}
        />
      </div>
      <span className="text-xs muted">{pct}</span>
    </div>
  )
}

export default function SignalFeed({ signals = [] }) {
  if (!signals.length) {
    return <div className="text-xs muted px-3 py-4">Waiting for signals…</div>
  }

  return (
    <div className="flex flex-col gap-1 overflow-y-auto max-h-64">
      {signals.slice(0, 10).map((sig, i) => {
        const stratColor = STRATEGY_COLORS[sig.strategy] || '#6B7280'
        const dirClass = DIRECTION_COLOR[sig.direction] || 'neu'
        const isNew = i === 0

        return (
          <div
            key={sig.id ?? i}
            className={`flex items-center gap-2 px-3 py-1.5 rounded border border-border text-xs ${isNew ? 'slide-in' : ''}`}
            style={{ background: '#0F1535', opacity: 1 - i * 0.07 }}
          >
            <span className="muted w-14 shrink-0">{formatTime(sig.timestamp)}</span>
            <span className="accent font-semibold w-12 shrink-0">{sig.symbol}</span>
            <span
              className="w-20 shrink-0 truncate"
              style={{ color: stratColor }}
            >
              {sig.strategy}
            </span>
            <span className={`w-8 shrink-0 uppercase ${dirClass}`}>{sig.direction?.slice(0, 4)}</span>
            <StrengthBar strength={sig.strength} />
            {sig.acted_on && (
              <span className="ml-auto text-xs pos">✓ traded</span>
            )}
          </div>
        )
      })}
    </div>
  )
}
