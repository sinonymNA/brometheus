import React, { useState, useEffect, useCallback, useRef } from 'react'
import {
  LineChart, Line, XAxis, YAxis, Tooltip, ResponsiveContainer,
  BarChart, Bar, PieChart, Pie, Cell, ReferenceLine, CartesianGrid,
} from 'recharts'
import { formatMoney, formatPercent, formatPct, formatNumber } from '../utils/formatting.js'

const COLORS = ['#00D9FF', '#00FF88', '#FFB800', '#FF0055', '#A855F7', '#F97316']
const STRAT_COLORS = { momentum: '#00D9FF', iv_rank: '#FFB800', flow: '#A855F7' }

// ── Shared sub-components ─────────────────────────────────────────────────────

function Card({ title, children, className = '' }) {
  return (
    <div className={`card flex flex-col ${className}`}>
      <div className="flex items-center px-3 py-2 border-b border-border">
        <span className="text-xs uppercase tracking-widest muted">{title}</span>
      </div>
      <div className="flex-1 min-h-0 overflow-hidden">{children}</div>
    </div>
  )
}

function StatBlock({ label, value, color, sub }) {
  return (
    <div className="flex flex-col gap-0.5 px-3 py-2 border-b border-border last:border-0">
      <span className="text-xs muted uppercase tracking-widest">{label}</span>
      <span className="font-mono font-bold text-sm" style={{ color: color || '#E8EAED' }}>
        {value}
      </span>
      {sub && <span className="text-xs muted">{sub}</span>}
    </div>
  )
}

function PctBar({ label, value, total, color }) {
  const pct = total > 0 ? (value / total) * 100 : 0
  return (
    <div className="flex items-center gap-2 text-xs py-1">
      <span className="w-20 muted capitalize">{label}</span>
      <div className="flex-1 h-1.5 rounded-full" style={{ background: 'rgba(255,255,255,0.08)' }}>
        <div className="h-full rounded-full" style={{ width: `${pct}%`, background: color }} />
      </div>
      <span className="w-10 text-right font-mono" style={{ color }}>{value}</span>
    </div>
  )
}

// ── Preset selector ───────────────────────────────────────────────────────────

function PresetSelector({ presets, selected, onSelect }) {
  return (
    <div className="flex flex-wrap gap-2">
      {presets.map(p => (
        <button
          key={p.id}
          onClick={() => onSelect(p)}
          className="text-xs px-3 py-1.5 rounded font-semibold uppercase tracking-wider transition-all"
          style={{
            background: selected?.id === p.id ? 'rgba(0,217,255,0.2)' : 'rgba(255,255,255,0.05)',
            color: selected?.id === p.id ? '#00D9FF' : '#9CA3AF',
            border: selected?.id === p.id ? '1px solid #00D9FF' : '1px solid rgba(255,255,255,0.1)',
          }}
        >
          {p.label}
        </button>
      ))}
    </div>
  )
}

// ── Custom date range form ────────────────────────────────────────────────────

function CustomDateForm({ value, onChange }) {
  return (
    <div className="flex flex-wrap gap-3 items-end mt-3">
      {[
        ['start_date', 'Start'],
        ['end_date', 'End'],
      ].map(([key, label]) => (
        <label key={key} className="flex flex-col gap-1">
          <span className="text-xs muted">{label}</span>
          <input
            type="date"
            value={value[key] || ''}
            onChange={e => onChange({ ...value, [key]: e.target.value })}
            className="font-mono text-xs px-2 py-1 rounded"
            style={{ background: 'rgba(255,255,255,0.07)', border: '1px solid rgba(255,255,255,0.15)', color: '#E8EAED' }}
          />
        </label>
      ))}
      <label className="flex items-center gap-2 text-xs muted cursor-pointer">
        <input
          type="checkbox"
          checked={value.walk_forward || false}
          onChange={e => onChange({ ...value, walk_forward: e.target.checked })}
        />
        Walk-forward
      </label>
      {value.walk_forward && (
        <>
          {[['test_start_date', 'Test Start'], ['test_end_date', 'Test End']].map(([key, label]) => (
            <label key={key} className="flex flex-col gap-1">
              <span className="text-xs muted">{label}</span>
              <input
                type="date"
                value={value[key] || ''}
                onChange={e => onChange({ ...value, [key]: e.target.value })}
                className="font-mono text-xs px-2 py-1 rounded"
                style={{ background: 'rgba(255,255,255,0.07)', border: '1px solid rgba(255,255,255,0.15)', color: '#E8EAED' }}
              />
            </label>
          ))}
        </>
      )}
    </div>
  )
}

// ── Progress bar ──────────────────────────────────────────────────────────────

function ProgressBar({ pct, message }) {
  return (
    <div className="flex flex-col gap-2">
      <div className="flex justify-between text-xs">
        <span className="muted">{message || 'Running…'}</span>
        <span className="accent font-mono">{pct}%</span>
      </div>
      <div className="h-2 rounded-full overflow-hidden" style={{ background: 'rgba(255,255,255,0.08)' }}>
        <div
          className="h-full rounded-full transition-all duration-300"
          style={{ width: `${pct}%`, background: 'linear-gradient(90deg, #00D9FF, #00FF88)' }}
        />
      </div>
    </div>
  )
}

// ── Results summary cards ─────────────────────────────────────────────────────

function ResultSummary({ result }) {
  const pnlColor = result.total_pnl >= 0 ? '#00FF88' : '#FF0055'
  const ddColor = result.max_drawdown_pct >= 0.1 ? '#FF0055' : result.max_drawdown_pct >= 0.05 ? '#FFB800' : '#6B7280'

  return (
    <div className="grid grid-cols-4 gap-0 border border-border rounded overflow-hidden">
      <div className="border-r border-border">
        <StatBlock
          label="Total Return"
          value={`${result.total_return_pct >= 0 ? '+' : ''}${formatPct(result.total_return_pct * 100)}`}
          color={pnlColor}
          sub={`${formatMoney(result.total_pnl)} PnL`}
        />
        <StatBlock label="Starting Balance" value={formatMoney(result.starting_balance)} />
        <StatBlock label="Ending Balance" value={formatMoney(result.ending_balance)} color={pnlColor} />
      </div>
      <div className="border-r border-border">
        <StatBlock
          label="Win Rate"
          value={formatPct(result.win_rate * 100)}
          color={result.win_rate >= 0.5 ? '#00FF88' : '#FF0055'}
          sub={`${result.winning_trades}W / ${result.losing_trades}L`}
        />
        <StatBlock label="Avg Win" value={formatMoney(result.avg_win_dollars)} color="#00FF88" />
        <StatBlock label="Avg Loss" value={formatMoney(result.avg_loss_dollars)} color="#FF0055" />
      </div>
      <div className="border-r border-border">
        <StatBlock
          label="Profit Factor"
          value={result.profit_factor > 0 ? formatNumber(result.profit_factor) : '—'}
          color={result.profit_factor >= 1.5 ? '#00FF88' : '#E8EAED'}
        />
        <StatBlock
          label="Sharpe Ratio"
          value={formatNumber(result.sharpe_ratio)}
          color={result.sharpe_ratio >= 1 ? '#00FF88' : '#E8EAED'}
        />
        <StatBlock label="Total Trades" value={result.total_trades.toLocaleString()} />
      </div>
      <div>
        <StatBlock
          label="Max Drawdown"
          value={formatPct(result.max_drawdown_pct * 100)}
          color={ddColor}
          sub={formatMoney(result.max_drawdown_dollars)}
        />
        <StatBlock label="Duration" value={`${result.duration_days}d`} />
        <StatBlock
          label="Signals Hit Rate"
          value={result.signals_generated > 0
            ? formatPct(result.signals_acted_on / result.signals_generated * 100)
            : '—'}
          sub={`${result.signals_acted_on} / ${result.signals_generated}`}
        />
      </div>
    </div>
  )
}

// ── Equity curve chart ────────────────────────────────────────────────────────

function BacktestEquityCurve({ curve, startBalance }) {
  const data = curve.map((v, i) => ({ day: i, value: v }))
  const min = Math.min(...curve, startBalance) * 0.98
  const max = Math.max(...curve, startBalance) * 1.02

  return (
    <ResponsiveContainer width="100%" height={180}>
      <LineChart data={data} margin={{ top: 4, right: 4, bottom: 0, left: 0 }}>
        <CartesianGrid strokeDasharray="3 3" stroke="rgba(255,255,255,0.05)" />
        <XAxis dataKey="day" tick={{ fill: '#6B7280', fontSize: 9 }} tickLine={false} />
        <YAxis domain={[min, max]} tick={{ fill: '#6B7280', fontSize: 9 }} tickLine={false}
          tickFormatter={v => `$${(v / 1000).toFixed(0)}k`} width={40} />
        <Tooltip
          contentStyle={{ background: '#13172F', border: '1px solid #1E2240', borderRadius: 4, fontSize: 11 }}
          formatter={(v) => [formatMoney(v), 'Balance']}
          labelFormatter={(l) => `Day ${l}`}
        />
        <ReferenceLine y={startBalance} stroke="rgba(255,255,255,0.2)" strokeDasharray="4 2" />
        <Line
          type="monotone" dataKey="value" stroke="#00D9FF"
          dot={false} strokeWidth={1.5} isAnimationActive={false}
        />
      </LineChart>
    </ResponsiveContainer>
  )
}

// ── Daily PnL histogram ───────────────────────────────────────────────────────

function DailyPnLChart({ dailyPnl }) {
  const data = dailyPnl.map((v, i) => ({ day: i, pnl: v }))
  return (
    <ResponsiveContainer width="100%" height={140}>
      <BarChart data={data} margin={{ top: 4, right: 4, bottom: 0, left: 0 }}>
        <XAxis dataKey="day" tick={false} />
        <YAxis tick={{ fill: '#6B7280', fontSize: 9 }} tickLine={false}
          tickFormatter={v => `$${v.toFixed(0)}`} width={45} />
        <Tooltip
          contentStyle={{ background: '#13172F', border: '1px solid #1E2240', borderRadius: 4, fontSize: 11 }}
          formatter={(v) => [formatMoney(v), 'Day PnL']}
          labelFormatter={(l) => `Day ${l}`}
        />
        <ReferenceLine y={0} stroke="rgba(255,255,255,0.2)" />
        <Bar dataKey="pnl" isAnimationActive={false} radius={[1, 1, 0, 0]}>
          {data.map((entry, i) => (
            <Cell key={i} fill={entry.pnl >= 0 ? '#00FF88' : '#FF0055'} fillOpacity={0.8} />
          ))}
        </Bar>
      </BarChart>
    </ResponsiveContainer>
  )
}

// ── Strategy breakdown ────────────────────────────────────────────────────────

function StrategyBreakdown({ tradesByStrategy, pnlByStrategy }) {
  const strategies = Object.keys(tradesByStrategy)
  const totalTrades = Object.values(tradesByStrategy).reduce((a, b) => a + b, 0)

  return (
    <div className="px-3 py-2">
      <div className="text-xs muted uppercase tracking-widest mb-2">Trades by strategy</div>
      {strategies.map((s, i) => (
        <PctBar key={s} label={s} value={tradesByStrategy[s]} total={totalTrades} color={STRAT_COLORS[s] || COLORS[i]} />
      ))}
      <div className="text-xs muted uppercase tracking-widest mt-3 mb-2">PnL by strategy</div>
      {strategies.map((s, i) => {
        const pnl = pnlByStrategy[s] || 0
        return (
          <div key={s} className="flex justify-between text-xs py-1">
            <span className="muted capitalize">{s}</span>
            <span className="font-mono" style={{ color: pnl >= 0 ? '#00FF88' : '#FF0055' }}>
              {pnl >= 0 ? '+' : ''}{formatMoney(pnl)}
            </span>
          </div>
        )
      })}
    </div>
  )
}

function MonthlyPnLTable({ monthlyPnl }) {
  if (!monthlyPnl || Object.keys(monthlyPnl).length === 0) {
    return <div className="px-3 py-2 text-xs muted">No monthly data</div>
  }

  const months = Object.entries(monthlyPnl).sort(([a], [b]) => a.localeCompare(b))
  const totalMonths = months.length
  const avgMonthly = months.reduce((sum, [, pnl]) => sum + pnl, 0) / totalMonths

  return (
    <div className="px-3 py-2">
      <div className="text-xs muted uppercase tracking-widest mb-2">Monthly P&L</div>
      <table className="w-full text-xs mb-3">
        <tbody>
          {months.map(([month, pnl]) => (
            <tr key={month} className="border-b border-border last:border-0">
              <td className="px-2 py-1 muted">{month}</td>
              <td className="px-2 py-1 font-mono text-right" style={{ color: pnl >= 0 ? '#00FF88' : '#FF0055' }}>
                {pnl >= 0 ? '+' : ''}{formatMoney(pnl)}
              </td>
            </tr>
          ))}
        </tbody>
      </table>
      <div className="flex justify-between text-xs border-t border-border pt-1">
        <span className="muted">Avg/Month</span>
        <span className="font-mono" style={{ color: avgMonthly >= 0 ? '#00FF88' : '#FF0055' }}>
          {avgMonthly >= 0 ? '+' : ''}{formatMoney(avgMonthly)}
        </span>
      </div>
    </div>
  )
}

// ── Win/Loss pie ──────────────────────────────────────────────────────────────

function WinLossPie({ wins, losses }) {
  const data = [
    { name: 'Wins', value: wins },
    { name: 'Losses', value: losses },
  ]
  return (
    <ResponsiveContainer width="100%" height={140}>
      <PieChart>
        <Pie data={data} cx="50%" cy="50%" innerRadius={35} outerRadius={55}
          dataKey="value" isAnimationActive={false}>
          <Cell fill="#00FF88" />
          <Cell fill="#FF0055" />
        </Pie>
        <Tooltip
          contentStyle={{ background: '#13172F', border: '1px solid #1E2240', borderRadius: 4, fontSize: 11 }}
        />
      </PieChart>
    </ResponsiveContainer>
  )
}

// ── Trade table ───────────────────────────────────────────────────────────────

function TradeTable({ equity_curve, daily_pnl_history, ...result }) {
  const [page, setPage] = useState(0)
  // We don't have individual trade rows in the result, so show daily PnL
  const dailyData = (daily_pnl_history || []).map((pnl, i) => ({ day: i + 1, pnl }))
  const PAGE_SIZE = 20
  const total = dailyData.length
  const pages = Math.ceil(total / PAGE_SIZE)
  const slice = dailyData.slice(page * PAGE_SIZE, (page + 1) * PAGE_SIZE)

  return (
    <div>
      <table className="w-full text-xs">
        <thead>
          <tr className="border-b border-border">
            <th className="text-left px-3 py-1.5 muted font-normal uppercase tracking-wider">Day</th>
            <th className="text-right px-3 py-1.5 muted font-normal uppercase tracking-wider">PnL</th>
            <th className="text-right px-3 py-1.5 muted font-normal uppercase tracking-wider">Cumulative</th>
          </tr>
        </thead>
        <tbody>
          {slice.map(({ day, pnl }) => {
            const cumIdx = (page * PAGE_SIZE + day - 1)
            const cumPnl = dailyData.slice(0, cumIdx + 1).reduce((a, b) => a + b.pnl, 0)
            return (
              <tr key={day} className="border-b border-border last:border-0 hover:bg-white/5">
                <td className="px-3 py-1.5 font-mono muted">{day}</td>
                <td className={`px-3 py-1.5 font-mono text-right ${pnl >= 0 ? 'pos' : 'neg'}`}>
                  {pnl >= 0 ? '+' : ''}{formatMoney(pnl)}
                </td>
                <td className={`px-3 py-1.5 font-mono text-right ${cumPnl >= 0 ? 'pos' : 'neg'}`}>
                  {cumPnl >= 0 ? '+' : ''}{formatMoney(cumPnl)}
                </td>
              </tr>
            )
          })}
        </tbody>
      </table>
      {pages > 1 && (
        <div className="flex justify-between items-center px-3 py-1.5 border-t border-border text-xs muted">
          <button onClick={() => setPage(p => Math.max(0, p - 1))} disabled={page === 0}
            className="hover:text-white disabled:opacity-30">← Prev</button>
          <span>Page {page + 1} / {pages}</span>
          <button onClick={() => setPage(p => Math.min(pages - 1, p + 1))} disabled={page === pages - 1}
            className="hover:text-white disabled:opacity-30">Next →</button>
        </div>
      )}
    </div>
  )
}

// ── Walk-forward comparison table ─────────────────────────────────────────────

function WalkForwardComparison({ train, test, degradation }) {
  const rows = [
    { label: 'Total Return', train: formatPct(train.total_return_pct * 100), test: formatPct(test.total_return_pct * 100), delta: degradation.return_delta },
    { label: 'Win Rate', train: formatPct(train.win_rate * 100), test: formatPct(test.win_rate * 100), delta: degradation.win_rate_delta },
    { label: 'Sharpe', train: formatNumber(train.sharpe_ratio), test: formatNumber(test.sharpe_ratio), delta: degradation.sharpe_delta },
    { label: 'Max Drawdown', train: formatPct(train.max_drawdown_pct * 100), test: formatPct(test.max_drawdown_pct * 100), delta: degradation.max_dd_delta },
    { label: 'Total Trades', train: train.total_trades, test: test.total_trades, delta: null },
    { label: 'Profit Factor', train: formatNumber(train.profit_factor), test: formatNumber(test.profit_factor), delta: null },
  ]

  return (
    <div className="overflow-x-auto">
      <table className="w-full text-xs">
        <thead>
          <tr className="border-b border-border">
            <th className="text-left px-3 py-1.5 muted font-normal uppercase tracking-wider">Metric</th>
            <th className="text-right px-3 py-1.5 muted font-normal uppercase tracking-wider">Train</th>
            <th className="text-right px-3 py-1.5 muted font-normal uppercase tracking-wider">Test</th>
            <th className="text-right px-3 py-1.5 muted font-normal uppercase tracking-wider">Delta</th>
          </tr>
        </thead>
        <tbody>
          {rows.map(row => (
            <tr key={row.label} className="border-b border-border last:border-0">
              <td className="px-3 py-1.5 muted">{row.label}</td>
              <td className="px-3 py-1.5 font-mono text-right accent">{row.train}</td>
              <td className="px-3 py-1.5 font-mono text-right" style={{ color: '#E8EAED' }}>{row.test}</td>
              <td className="px-3 py-1.5 font-mono text-right">
                {row.delta != null ? (
                  <span style={{ color: row.delta >= 0 ? '#00FF88' : '#FF0055' }}>
                    {row.delta >= 0 ? '+' : ''}{formatNumber(row.delta * 100)}%
                  </span>
                ) : '—'}
              </td>
            </tr>
          ))}
        </tbody>
      </table>
    </div>
  )
}

// ── Main BacktestUI component ─────────────────────────────────────────────────

export default function BacktestUI({ get, post }) {
  const [presets, setPresets] = useState([])
  const [selected, setSelected] = useState(null)
  const [customParams, setCustomParams] = useState({
    start_date: '2023-01-03',
    end_date: '2023-12-29',
    walk_forward: false,
  })
  const [symbols, setSymbols] = useState('SPY,QQQ,AAPL,NVDA,MSFT,TSLA,AMZN,META')
  const [strategies, setStrategies] = useState({ momentum: true, iv_rank: true, flow: true, ma_cross: true, bb: false })
  const [jobId, setJobId] = useState(null)
  const [jobStatus, setJobStatus] = useState(null)
  const pollRef = useRef(null)

  useEffect(() => {
    get('/api/backtest/presets').then(d => {
      if (d?.presets) setPresets(d.presets)
    })
  }, [])

  const handlePresetSelect = useCallback((preset) => {
    setSelected(preset)
    setCustomParams({
      start_date: preset.start_date,
      end_date: preset.end_date,
      walk_forward: preset.walk_forward || false,
      test_start_date: preset.test_start_date || '',
      test_end_date: preset.test_end_date || '',
    })
  }, [])

  const startPoll = useCallback((id) => {
    if (pollRef.current) clearInterval(pollRef.current)
    pollRef.current = setInterval(async () => {
      const status = await get(`/api/backtest/${id}`, { ttl: 0 })
      if (status) {
        setJobStatus(status)
        if (status.status === 'done' || status.status === 'error') {
          clearInterval(pollRef.current)
          pollRef.current = null
        }
      }
    }, 2000)
  }, [get])

  useEffect(() => () => { if (pollRef.current) clearInterval(pollRef.current) }, [])

  const handleRun = useCallback(async () => {
    const params = customParams.start_date && customParams.end_date ? customParams : selected
    if (!params) {
      setJobStatus({ status: 'error', error: 'Please select a preset or enter start/end dates' })
      return
    }

    const body = {
      start_date: params.start_date || customParams.start_date,
      end_date: params.end_date || customParams.end_date,
      symbols: symbols.split(',').map(s => s.trim().toUpperCase()).filter(Boolean),
      strategies: Object.entries(strategies).filter(([, v]) => v).map(([k]) => k),
      walk_forward: customParams.walk_forward || false,
    }
    if (body.walk_forward) {
      body.test_start_date = customParams.test_start_date || null
      body.test_end_date = customParams.test_end_date || null
    }

    console.log('[BacktestUI] Submitting request:', JSON.stringify(body, null, 2))
    setJobStatus({ status: 'running', progress: 0, message: 'Submitting…' })

    const resp = await post('/api/backtest', body)
    console.log('[BacktestUI] Response:', resp)

    if (resp?.job_id) {
      setJobId(resp.job_id)
      startPoll(resp.job_id)
    } else if (resp?.error) {
      setJobStatus({ status: 'error', error: resp.error })
    } else {
      setJobStatus({ status: 'error', error: resp?.detail || 'Failed to start backtest' })
    }
  }, [customParams, selected, symbols, strategies, post, startPoll])

  const isRunning = jobStatus?.status === 'running'
  const isDone = jobStatus?.status === 'done'
  const isError = jobStatus?.status === 'error'
  const result = isDone ? jobStatus.result : null
  const isWalkForward = result && result.train && result.test

  return (
    <div className="flex flex-col gap-4 p-3">

      {/* ── Config panel ─────────────────────────────────────────────────────── */}
      <Card title="Backtest Configuration">
        <div className="p-3 flex flex-col gap-3">
          <PresetSelector presets={presets} selected={selected} onSelect={handlePresetSelect} />
          <CustomDateForm value={customParams} onChange={setCustomParams} />

          <div className="flex flex-wrap gap-4 mt-1">
            <label className="flex flex-col gap-1">
              <span className="text-xs muted">Symbols (comma-separated)</span>
              <input
                value={symbols}
                onChange={e => setSymbols(e.target.value)}
                className="font-mono text-xs px-2 py-1 rounded w-52"
                style={{ background: 'rgba(255,255,255,0.07)', border: '1px solid rgba(255,255,255,0.15)', color: '#E8EAED' }}
              />
            </label>
            <div className="flex flex-col gap-1">
              <span className="text-xs muted">Strategies</span>
              <div className="flex gap-3">
                {Object.keys(strategies).map(s => (
                  <label key={s} className="flex items-center gap-1.5 text-xs cursor-pointer" style={{ color: STRAT_COLORS[s] }}>
                    <input type="checkbox" checked={strategies[s]}
                      onChange={e => setStrategies(prev => ({ ...prev, [s]: e.target.checked }))} />
                    {s}
                  </label>
                ))}
              </div>
            </div>
          </div>

          <div className="flex items-center gap-3 mt-1">
            <button
              onClick={handleRun}
              disabled={isRunning}
              className="text-xs px-5 py-2 rounded font-bold uppercase tracking-wider disabled:opacity-50"
              style={{ background: isRunning ? 'rgba(0,217,255,0.1)' : 'rgba(0,217,255,0.2)', color: '#00D9FF', border: '1px solid #00D9FF' }}
            >
              {isRunning ? '⟳ Running…' : '▶ Run Backtest'}
            </button>
          </div>

          {isRunning && (
            <ProgressBar pct={jobStatus.progress || 0} message={jobStatus.message} />
          )}

          {isError && (
            <div className="p-2 rounded" style={{ background: 'rgba(255, 0, 85, 0.1)', border: '1px solid #FF0055' }}>
              <div className="text-xs neg font-semibold mb-1">Backtest Error</div>
              <div className="text-xs muted whitespace-pre-wrap break-words">{jobStatus.error}</div>
            </div>
          )}
        </div>
      </Card>

      {/* ── Results ──────────────────────────────────────────────────────────── */}
      {isDone && result && !isWalkForward && (
        <>
          <ResultSummary result={result} />

          <div className="grid grid-cols-3 gap-3">
            <Card title="Equity Curve" className="col-span-2">
              <div className="p-2">
                <BacktestEquityCurve curve={result.equity_curve} startBalance={result.starting_balance} />
              </div>
            </Card>
            <Card title="Win / Loss">
              <WinLossPie wins={result.winning_trades} losses={result.losing_trades} />
            </Card>
          </div>

          <div className="grid grid-cols-3 gap-3">
            <Card title="Daily PnL Distribution" className="col-span-2">
              <div className="p-2">
                <DailyPnLChart dailyPnl={result.daily_pnl_history} />
              </div>
            </Card>
            <Card title="Strategy Breakdown">
              <StrategyBreakdown
                tradesByStrategy={result.trades_by_strategy}
                pnlByStrategy={result.pnl_by_strategy}
              />
            </Card>
          </div>

          <Card title="Monthly P&L Analysis">
            <MonthlyPnLTable monthlyPnl={result.monthly_pnl} />
          </Card>

          <Card title="Daily PnL Log">
            <TradeTable {...result} />
          </Card>
        </>
      )}

      {/* ── Walk-forward results ──────────────────────────────────────────────── */}
      {isDone && isWalkForward && (
        <>
          <Card title="Walk-Forward Comparison">
            <WalkForwardComparison
              train={result.train}
              test={result.test}
              degradation={result.degradation}
            />
          </Card>

          <div className="grid grid-cols-2 gap-3">
            {['train', 'test'].map(period => (
              <div key={period} className="flex flex-col gap-3">
                <div className="text-xs uppercase tracking-widest muted px-1">
                  {period === 'train' ? `Train (${result.train.start_date} → ${result.train.end_date})` : `Test (${result.test.start_date} → ${result.test.end_date})`}
                </div>
                <ResultSummary result={result[period]} />
                <Card title="Equity Curve">
                  <div className="p-2">
                    <BacktestEquityCurve curve={result[period].equity_curve} startBalance={result[period].starting_balance} />
                  </div>
                </Card>
              </div>
            ))}
          </div>
        </>
      )}
    </div>
  )
}
