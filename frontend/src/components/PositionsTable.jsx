import React, { useState } from 'react'
import { formatMoney, formatPct, getColorClass } from '../utils/formatting.js'

function GreeksRow({ pos }) {
  return (
    <tr>
      <td colSpan={8} className="bg-bg">
        <div className="flex gap-6 px-2 py-2 text-xs muted">
          <span>Symbol <span className="accent">{pos.symbol}</span></span>
          <span>Strike <span className="text">{pos.strike}</span></span>
          <span>Type <span className="text uppercase">{pos.option_type}</span></span>
          <span>Exp <span className="text">{pos.expiry}</span></span>
          <span>Qty <span className="text">{pos.quantity}</span></span>
          <span>Strategy <span className="accent">{pos.strategy || '—'}</span></span>
        </div>
      </td>
    </tr>
  )
}

export default function PositionsTable({ positions = [] }) {
  const [expanded, setExpanded] = useState(null)
  const [sortKey, setSortKey] = useState('unrealized_pnl')
  const [sortDir, setSortDir] = useState(-1)

  function handleSort(key) {
    if (sortKey === key) setSortDir(d => -d)
    else { setSortKey(key); setSortDir(-1) }
  }

  const sorted = [...positions].sort((a, b) => {
    const av = a[sortKey] ?? -Infinity
    const bv = b[sortKey] ?? -Infinity
    return (av - bv) * sortDir
  })

  if (!positions.length) {
    return <div className="text-xs muted px-3 py-4">No open positions.</div>
  }

  return (
    <div className="overflow-x-auto">
      <table>
        <thead>
          <tr>
            {[
              ['symbol',        'Symbol'],
              ['strike',        'Strike'],
              ['entry_price',   'Entry'],
              ['current_price', 'Current'],
              ['unrealized_pnl','PNL $'],
              ['dte',           'DTE'],
              ['strategy',      'Strategy'],
            ].map(([k, label]) => (
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
          {sorted.map((pos) => {
            const pnlClass = getColorClass(pos.unrealized_pnl)
            const isOpen = expanded === pos.id
            const rowBg = pos.unrealized_pnl > 0
              ? 'rgba(0,255,136,0.04)'
              : pos.unrealized_pnl < 0
              ? 'rgba(255,0,85,0.04)'
              : 'transparent'

            return (
              <React.Fragment key={pos.id}>
                <tr
                  style={{ background: rowBg, cursor: 'pointer' }}
                  onClick={() => setExpanded(isOpen ? null : pos.id)}
                >
                  <td className="accent font-semibold">{pos.symbol}</td>
                  <td>{pos.strike ?? '—'}</td>
                  <td>{formatMoney(pos.entry_price)}</td>
                  <td>{pos.current_price ? formatMoney(pos.current_price) : '—'}</td>
                  <td className={pnlClass}>{formatMoney(pos.unrealized_pnl)}</td>
                  <td className={pos.dte != null && pos.dte <= 3 ? 'neg' : 'neu'}>
                    {pos.dte ?? '—'}
                  </td>
                  <td className="muted">{pos.strategy || '—'}</td>
                </tr>
                {isOpen && <GreeksRow pos={pos} />}
              </React.Fragment>
            )
          })}
        </tbody>
      </table>
    </div>
  )
}
