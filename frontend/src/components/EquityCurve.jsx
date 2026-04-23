import React, { useMemo } from 'react'
import {
  ResponsiveContainer, LineChart, Line, XAxis, YAxis,
  CartesianGrid, Tooltip, ReferenceLine, ReferenceArea,
} from 'recharts'
import { formatMoney } from '../utils/formatting.js'

const STARTING = 50000

function CustomTooltip({ active, payload, label }) {
  if (!active || !payload?.length) return null
  const val = payload[0].value
  const diff = val - STARTING
  return (
    <div className="card p-2 text-xs">
      <div className="muted mb-1">{label}</div>
      <div className={diff >= 0 ? 'pos' : 'neg'}>{formatMoney(val)}</div>
      <div className={`text-xs ${diff >= 0 ? 'pos' : 'neg'}`}>
        {diff >= 0 ? '+' : ''}{formatMoney(diff)}
      </div>
    </div>
  )
}

export default function EquityCurve({ curve = [], balance }) {
  const data = useMemo(() => {
    const pts = [...curve]
    if (balance != null) {
      const today = new Date().toISOString().slice(0, 10)
      const last = pts[pts.length - 1]
      if (!last || last.date !== today) {
        pts.push({ date: today, balance })
      } else {
        pts[pts.length - 1] = { ...last, balance }
      }
    }
    return pts.map(p => ({ ...p, label: p.date?.slice(5) }))
  }, [curve, balance])

  // Find drawdown zones: where balance dips below starting
  const drawdownAreas = useMemo(() => {
    const areas = []
    let start = null
    for (let i = 0; i < data.length; i++) {
      if (data[i].balance < STARTING) {
        if (!start) start = data[i].label
      } else if (start) {
        areas.push({ x1: start, x2: data[i - 1]?.label || start })
        start = null
      }
    }
    if (start) areas.push({ x1: start, x2: data[data.length - 1]?.label })
    return areas
  }, [data])

  const yMin = useMemo(() => {
    if (!data.length) return 45000
    const min = Math.min(...data.map(d => d.balance))
    return Math.floor((min - 500) / 1000) * 1000
  }, [data])

  const yMax = useMemo(() => {
    if (!data.length) return 55000
    const max = Math.max(...data.map(d => d.balance))
    return Math.ceil((max + 500) / 1000) * 1000
  }, [data])

  if (!data.length) {
    return (
      <div className="flex items-center justify-center h-40 muted text-xs">
        No trade history yet
      </div>
    )
  }

  return (
    <ResponsiveContainer width="100%" height={180}>
      <LineChart data={data} margin={{ top: 4, right: 8, bottom: 0, left: 4 }}>
        <CartesianGrid stroke="#1E2A4A" strokeDasharray="3 3" vertical={false} />

        {drawdownAreas.map((a, i) => (
          <ReferenceArea key={i} x1={a.x1} x2={a.x2} fill="#FF0055" fillOpacity={0.06} />
        ))}

        <ReferenceLine y={STARTING} stroke="#6B7280" strokeDasharray="4 4" />

        <XAxis
          dataKey="label"
          tick={{ fill: '#6B7280', fontSize: 10, fontFamily: 'inherit' }}
          tickLine={false}
          axisLine={false}
          interval="preserveStartEnd"
        />
        <YAxis
          domain={[yMin, yMax]}
          tick={{ fill: '#6B7280', fontSize: 10, fontFamily: 'inherit' }}
          tickLine={false}
          axisLine={false}
          tickFormatter={v => `$${(v / 1000).toFixed(0)}k`}
          width={42}
        />
        <Tooltip content={<CustomTooltip />} />
        <Line
          type="monotone"
          dataKey="balance"
          stroke="#00D9FF"
          strokeWidth={2}
          dot={false}
          activeDot={{ r: 4, fill: '#00D9FF', strokeWidth: 0 }}
          isAnimationActive={false}
        />
      </LineChart>
    </ResponsiveContainer>
  )
}
