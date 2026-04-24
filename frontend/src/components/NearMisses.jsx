import React, { useState, useEffect, useCallback } from 'react'

function timeAgo(isoStr) {
  const diff = Math.floor((Date.now() - new Date(isoStr).getTime()) / 1000)
  if (diff < 60)  return `${diff}s ago`
  if (diff < 3600) return `${Math.floor(diff / 60)}m ago`
  return `${Math.floor(diff / 3600)}h ago`
}

function CondBadge({ label, value, threshold, pass, fmt }) {
  const gap = pass ? null : Math.abs(value - threshold)
  return (
    <div className="flex flex-col items-center gap-0.5 min-w-[56px]">
      <span className="text-xs font-mono font-bold"
        style={{ color: pass ? '#00FF88' : '#FF0055' }}>
        {fmt ? fmt(value) : value}
      </span>
      <span className="text-xs muted">{label}</span>
      {!pass && gap != null && (
        <span className="text-xs" style={{ color: '#FFB800', fontSize: '0.6rem' }}>
          {gap.toFixed(1)} short
        </span>
      )}
      <span style={{ fontSize: '0.65rem', color: pass ? '#00FF88' : '#FF0055' }}>
        {pass ? '✓' : '✗'}
      </span>
    </div>
  )
}

function NearMissRow({ miss }) {
  const passed = miss.conditions_passed ?? 0
  const pctBar = (passed / 4) * 100

  return (
    <div className="flex flex-col gap-2 p-2 rounded"
      style={{ background: 'rgba(255,184,0,0.05)', border: '1px solid rgba(255,184,0,0.2)' }}>
      <div className="flex items-center justify-between">
        <div className="flex items-center gap-2">
          <span className="font-mono font-bold text-sm" style={{ color: '#E8EAED' }}>
            {miss.symbol}
          </span>
          <span className="text-xs px-1.5 py-0.5 rounded font-bold uppercase"
            style={{
              background: miss.direction === 'bull' ? 'rgba(0,255,136,0.15)' : 'rgba(255,0,85,0.15)',
              color: miss.direction === 'bull' ? '#00FF88' : '#FF0055',
              border: miss.direction === 'bull' ? '1px solid rgba(0,255,136,0.4)' : '1px solid rgba(255,0,85,0.4)',
            }}>
            {miss.direction === 'bull' ? '▲ Bull' : '▼ Bear'}
          </span>
          <span className="text-xs muted">{passed}/4 conditions</span>
        </div>
        <span className="text-xs muted">{timeAgo(miss.timestamp)}</span>
      </div>

      {/* Proximity bar */}
      <div className="h-1 rounded-full overflow-hidden" style={{ background: 'rgba(255,255,255,0.08)' }}>
        <div className="h-full rounded-full"
          style={{ width: `${pctBar}%`, background: passed >= 3 ? '#FFB800' : '#FF6B35' }} />
      </div>

      {/* Condition badges */}
      <div className="flex gap-3 flex-wrap">
        <CondBadge
          label="RSI"
          value={miss.rsi}
          threshold={miss.rsi_threshold}
          pass={miss.rsi_pass}
          fmt={v => v.toFixed(1)}
        />
        <CondBadge
          label="Vol Ratio"
          value={miss.volume_ratio}
          threshold={1.5}
          pass={miss.volume_pass}
          fmt={v => v.toFixed(2)}
        />
        <CondBadge
          label="IV Rank"
          value={miss.iv_rank}
          threshold={60}
          pass={miss.iv_pass}
          fmt={v => v.toFixed(0) + '%'}
        />
        <CondBadge
          label="Price Brk"
          value={miss.price_pct}
          threshold={0}
          pass={miss.price_pass}
          fmt={v => (v >= 0 ? '+' : '') + v.toFixed(2) + '%'}
        />
      </div>
    </div>
  )
}

export default function NearMisses({ get }) {
  const [misses, setMisses]     = useState([])
  const [loading, setLoading]   = useState(true)
  const [filter, setFilter]     = useState('all')   // 'all' | 'bull' | 'bear'

  const load = useCallback(async () => {
    const data = await get('/api/signals/near-misses', { ttl: 0 })
    if (data?.near_misses) {
      setMisses(data.near_misses)
      setLoading(false)
    }
  }, [get])

  useEffect(() => {
    load()
    const iv = setInterval(load, 15_000)
    return () => clearInterval(iv)
  }, [load])

  const visible = filter === 'all' ? misses : misses.filter(m => m.direction === filter)

  return (
    <div className="flex flex-col h-full">
      {/* Header bar */}
      <div className="flex items-center justify-between px-3 py-2 border-b border-border">
        <div className="flex items-center gap-2">
          <span className="text-xs" style={{ color: '#FFB800' }}>◉</span>
          <span className="text-xs muted uppercase tracking-widest">Near Misses</span>
          {misses.length > 0 && (
            <span className="text-xs px-1.5 py-0.5 rounded-full font-mono"
              style={{ background: 'rgba(255,184,0,0.15)', color: '#FFB800' }}>
              {misses.length}
            </span>
          )}
        </div>
        <div className="flex items-center gap-1">
          {['all', 'bull', 'bear'].map(f => (
            <button key={f} onClick={() => setFilter(f)}
              className="text-xs px-2 py-0.5 rounded capitalize"
              style={{
                background: filter === f ? 'rgba(0,217,255,0.15)' : 'transparent',
                color: filter === f ? '#00D9FF' : '#6B7280',
              }}>
              {f}
            </button>
          ))}
        </div>
      </div>

      {/* Body */}
      <div className="flex-1 overflow-y-auto p-2 flex flex-col gap-2">
        {loading ? (
          <div className="text-xs muted text-center py-4">Loading…</div>
        ) : visible.length === 0 ? (
          <div className="flex flex-col items-center justify-center py-6 gap-2">
            <span className="text-2xl">◎</span>
            <span className="text-xs muted text-center">
              {misses.length === 0
                ? 'No near misses yet — bot is scanning every 60s during market hours'
                : `No ${filter} near misses`}
            </span>
          </div>
        ) : (
          visible.map((m, i) => <NearMissRow key={i} miss={m} />)
        )}
      </div>
    </div>
  )
}
