import React, { useState } from 'react'
import { formatMoney, formatPct, formatDuration, getColorClass } from '../utils/formatting.js'

const COLS = [
  ['closed_at',    'Date'],
  ['symbol',       'Symbol'],
  ['strategy',     'Strategy'],
  ['entry_price',  'Entry'],
  ['exit_price',   'Exit'],
  ['pnl',          'PNL $'],
  ['pnl_pct',      'PNL %'],
  ['duration_minutes', 'Duration'],
  ['close_reason', 'Reason'],
]

function RowTooltip({ trade }) {
  return (
    <div className="card p-2 text-xs absolute z-50 w-52 shadow-xl" style={{ bottom: '110%', left: 0 }}>
      <div className="muted mb-1">{trade.symbol} — {trade.strategy}</div>
      <div>Entry: {formatMoney(trade.entry_price)}</div>
      <div>Exit: {formatMoney(trade.exit_price)}</div>
      <div className={getColorClass(trade.pnl)}>PNL: {formatMoney(trade.pnl)}</div>
      <div>Qty: {trade.quantity}</div>
      <div>Strike: {trade.strike} {trade.option_type?.toUpperCase()}</div>
      <div>Expiry: {trade.expiry}</div>
      {trade.close_reason && <div className="muted mt-1">{trade.close_reason}</div>}
    </div>
  )
}

export default function TradeHistory({ trades = [] }) {
  const [sortKey, setSortKey] = useState('closed_at')
  const [sortDir, setSortDir] = useState(-1)
  const [hovered, setHovered]  = useState(null)

  function handleSort(key) {
    if (sortKey === key) setSortDir(d => -d)
    else { setSortKey(key); setSortDir(-1) }
  }

  const sorted = [...trades].sort((a, b) => {
    const av = a[sortKey] ?? ''
    const bv = b[sortKey] ?? ''
    if (typeof av === 'number') return (av - bv) * sortDir
    return String(av).localeCompare(String(bv)) * sortDir
  })

  if (!trades.length) {
    return <div className="text-xs muted px-3 py-4">No closed trades yet.</div>
  }

  return (
    <div className="overflow-x-auto overflow-y-auto max-h-72">
      <table>
        <thead>
          <tr>
            {COLS.map(([k, label]) => (
              <th
                key={k}
                onClick={() => handleSort(k)}
                className="cursor-pointer select-none hover:text-accent"
              >
                {label}{sortKey === k ? (sortDir > 0 ? ' ↑' : ' ↓') : ''}
              </th>
            ))}
          </tr>
        </thead>
        <tbody>
          {sorted.slice(0, 20).map((t, i) => {
            const pnlClass = getColorClass(t.pnl)
            const rowBg = i % 2 === 0 ? 'transparent' : 'rgba(15,21,53,0.5)'
            const winnerBg = t.pnl > 0
              ? 'rgba(0,255,136,0.03)'
              : t.pnl < 0
              ? 'rgba(255,0,85,0.03)'
              : rowBg

            return (
              <tr
                key={t.id ?? i}
                style={{ background: winnerBg, position: 'relative' }}
                onMouseEnter={() => setHovered(t.id)}
                onMouseLeave={() => setHovered(null)}
              >
                <td className="muted">{t.closed_at?.slice(0, 10) ?? '—'}</td>
                <td className="accent font-semibold">{t.symbol}</td>
                <td className="muted">{t.strategy ?? '—'}</td>
                <td>{formatMoney(t.entry_price)}</td>
                <td>{formatMoney(t.exit_price)}</td>
                <td className={pnlClass}>{formatMoney(t.pnl)}</td>
                <td className={pnlClass}>{formatPct(t.pnl_pct)}</td>
                <td className="muted">{formatDuration(t.duration_minutes)}</td>
                <td className="muted text-xs">{t.close_reason ?? '—'}</td>
                {hovered === t.id && (
                  <td style={{ position: 'relative', overflow: 'visible' }}>
                    <RowTooltip trade={t} />
                  </td>
                )}
              </tr>
            )
          })}
        </tbody>
      </table>
    </div>
  )
}
