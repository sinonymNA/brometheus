import React, { useState, useEffect, useCallback, useRef } from 'react'
import {
  LineChart, Line, XAxis, YAxis, Tooltip, ResponsiveContainer, CartesianGrid,
} from 'recharts'
import { formatMoney, formatPct, formatNumber } from '../utils/formatting.js'

// ── Parameter definitions ─────────────────────────────────────────────────────

const PARAM_DEFS = [
  { key: 'rsi_bull_threshold', label: 'RSI Bull Threshold', min: 50, max: 75, step: 1,  default: 62,   format: v => v.toFixed(0) },
  { key: 'rsi_bear_threshold', label: 'RSI Bear Threshold', min: 25, max: 50, step: 1,  default: 38,   format: v => v.toFixed(0) },
  { key: 'volume_ratio_min',   label: 'Volume Ratio Min',   min: 1.0, max: 3.0, step: 0.1, default: 1.5, format: v => v.toFixed(1) },
  { key: 'iv_rank_max',        label: 'IV Rank Max (momentum)', min: 30, max: 80, step: 1, default: 60,  format: v => v.toFixed(0) + '%' },
  { key: 'iv_rank_min',        label: 'IV Rank Min (condor)',   min: 50, max: 90, step: 1, default: 70,  format: v => v.toFixed(0) + '%' },
  { key: 'signal_strength_min',label: 'Signal Strength Min', min: 0.30, max: 0.80, step: 0.05, default: 0.55, format: v => v.toFixed(2) },
  { key: 'stop_loss_pct',      label: 'Stop Loss %',         min: 0.05, max: 0.20, step: 0.01, default: 0.08, format: v => (v*100).toFixed(0) + '%' },
  { key: 'profit_target_pct',  label: 'Profit Target %',     min: 0.15, max: 0.60, step: 0.05, default: 0.30, format: v => (v*100).toFixed(0) + '%' },
  { key: 'min_dte',            label: 'Min DTE',             min: 2, max: 21, step: 1,  default: 5,    format: v => v.toFixed(0) + 'd' },
  { key: 'max_dte',            label: 'Max DTE',             min: 14, max: 60, step: 1, default: 45,   format: v => v.toFixed(0) + 'd' },
  { key: 'max_positions',      label: 'Max Concurrent Positions', min: 1, max: 8, step: 1, default: 3, format: v => v.toFixed(0) },
  { key: 'position_size_pct',  label: 'Position Size %',     min: 0.005, max: 0.05, step: 0.005, default: 0.02, format: v => (v*100).toFixed(1) + '%' },
]

const DEFAULTS = Object.fromEntries(PARAM_DEFS.map(p => [p.key, p.default]))

function randomizeParams() {
  const result = {}
  for (const d of PARAM_DEFS) {
    const steps = Math.round((d.max - d.min) / d.step)
    const pick = Math.floor(Math.random() * (steps + 1))
    result[d.key] = parseFloat((d.min + pick * d.step).toFixed(10))
  }
  return result
}

// ── Result table column definitions ──────────────────────────────────────────

const RESULT_COLS = [
  { key: 'run',           label: 'Run #',        fmt: v => v,                          better: null },
  { key: 'rsi',          label: 'RSI B/Bear',    fmt: v => v,                          better: null },
  { key: 'vol_ratio',    label: 'Vol Ratio',     fmt: v => v?.toFixed(1) ?? '—',       better: null },
  { key: 'strength_min', label: 'Strength',      fmt: v => v?.toFixed(2) ?? '—',       better: null },
  { key: 'stop_pct',     label: 'Stop %',        fmt: v => v != null ? (v*100).toFixed(0)+'%' : '—', better: null },
  { key: 'target_pct',   label: 'Target %',      fmt: v => v != null ? (v*100).toFixed(0)+'%' : '—', better: null },
  { key: 'win_rate',     label: 'Win Rate',      fmt: v => formatPct(v * 100),          better: 'high' },
  { key: 'total_return', label: 'Return',        fmt: v => formatPct(v * 100),          better: 'high' },
  { key: 'profit_factor',label: 'Profit Factor', fmt: v => formatNumber(v),             better: 'high' },
  { key: 'max_dd',       label: 'Max DD',        fmt: v => formatPct(v * 100),          better: 'low' },
  { key: 'total_trades', label: 'Trades',        fmt: v => v,                           better: 'high' },
  { key: 'sharpe',       label: 'Sharpe',        fmt: v => formatNumber(v),             better: 'high' },
]

// ── Sub-components ────────────────────────────────────────────────────────────

function ParamSlider({ def: d, value, onChange }) {
  return (
    <div className="flex flex-col gap-1">
      <div className="flex justify-between items-baseline">
        <label className="text-xs muted">{d.label}</label>
        <span className="text-xs font-mono accent font-bold">{d.format(value)}</span>
      </div>
      <input
        type="range"
        min={d.min} max={d.max} step={d.step}
        value={value}
        onChange={e => onChange(d.key, parseFloat(e.target.value))}
        className="w-full h-1 rounded-full appearance-none cursor-pointer"
        style={{ accentColor: '#00D9FF' }}
      />
      <div className="flex justify-between text-xs" style={{ color: 'rgba(255,255,255,0.2)' }}>
        <span>{d.format(d.min)}</span>
        <span>{d.format(d.max)}</span>
      </div>
    </div>
  )
}

function ProgressBar({ pct, message }) {
  return (
    <div className="flex flex-col gap-2">
      <div className="flex justify-between text-xs">
        <span className="muted">{message || 'Running…'}</span>
        <span className="accent font-mono">{pct}%</span>
      </div>
      <div className="h-2 rounded-full overflow-hidden" style={{ background: 'rgba(255,255,255,0.08)' }}>
        <div className="h-full rounded-full transition-all duration-300"
          style={{ width: `${pct}%`, background: 'linear-gradient(90deg, #00D9FF, #00FF88)' }} />
      </div>
    </div>
  )
}

function PresetBtn({ label, active, onClick }) {
  return (
    <button onClick={onClick} className="text-xs px-2 py-1 rounded font-semibold uppercase tracking-wider"
      style={{
        background: active ? 'rgba(0,217,255,0.2)' : 'rgba(255,255,255,0.05)',
        color: active ? '#00D9FF' : '#6B7280',
        border: active ? '1px solid #00D9FF' : '1px solid rgba(255,255,255,0.1)',
      }}>
      {label}
    </button>
  )
}

// ── Results table ─────────────────────────────────────────────────────────────

function ResultsTable({ runs, onClear, onExport }) {
  const [sortKey, setSortKey] = useState('run')
  const [sortAsc, setSortAsc] = useState(true)

  if (runs.length === 0) return null

  // Compute best/worst per metric column
  const metricCols = RESULT_COLS.filter(c => c.better)
  const bests = {}, worsts = {}
  for (const col of metricCols) {
    const vals = runs.map(r => r[col.key]).filter(v => v != null && !isNaN(v))
    if (!vals.length) continue
    if (col.better === 'high') { bests[col.key] = Math.max(...vals); worsts[col.key] = Math.min(...vals) }
    else                       { bests[col.key] = Math.min(...vals); worsts[col.key] = Math.max(...vals) }
  }

  const sorted = [...runs].sort((a, b) => {
    const av = a[sortKey], bv = b[sortKey]
    if (av == null) return 1
    if (bv == null) return -1
    return sortAsc ? (av > bv ? 1 : -1) : (av < bv ? 1 : -1)
  })

  const handleSort = key => {
    if (sortKey === key) setSortAsc(a => !a)
    else { setSortKey(key); setSortAsc(false) }
  }

  return (
    <div className="flex flex-col gap-2">
      <div className="flex justify-between items-center">
        <span className="text-xs muted uppercase tracking-widest">{runs.length} run{runs.length !== 1 ? 's' : ''}</span>
        <div className="flex gap-2">
          <button onClick={onExport} className="text-xs px-2 py-1 rounded"
            style={{ background: 'rgba(0,217,255,0.1)', color: '#00D9FF', border: '1px solid rgba(0,217,255,0.3)' }}>
            ↓ CSV
          </button>
          <button onClick={onClear} className="text-xs px-2 py-1 rounded"
            style={{ background: 'rgba(255,0,85,0.1)', color: '#FF0055', border: '1px solid rgba(255,0,85,0.3)' }}>
            Clear
          </button>
        </div>
      </div>
      <div className="overflow-x-auto">
        <table className="w-full text-xs border-collapse">
          <thead>
            <tr className="border-b border-border">
              {RESULT_COLS.map(col => (
                <th key={col.key} onClick={() => handleSort(col.key)}
                  className="text-left px-2 py-1.5 muted font-normal uppercase tracking-wider cursor-pointer hover:text-white whitespace-nowrap">
                  {col.label}{sortKey === col.key ? (sortAsc ? ' ↑' : ' ↓') : ''}
                </th>
              ))}
            </tr>
          </thead>
          <tbody>
            {sorted.map(row => (
              <tr key={row.run} className="border-b border-border last:border-0 hover:bg-white/5">
                {RESULT_COLS.map(col => {
                  const val = row[col.key]
                  const isBest  = col.better && bests[col.key]  != null && val === bests[col.key]
                  const isWorst = col.better && worsts[col.key] != null && val === worsts[col.key]
                  return (
                    <td key={col.key} className="px-2 py-1.5 font-mono whitespace-nowrap"
                      style={{ color: isBest ? '#00FF88' : isWorst ? '#FF0055' : '#E8EAED',
                               fontWeight: isBest || isWorst ? 'bold' : 'normal' }}>
                      {col.fmt(val)}
                    </td>
                  )
                })}
              </tr>
            ))}
          </tbody>
        </table>
      </div>
    </div>
  )
}

// ── Best combo finder ─────────────────────────────────────────────────────────

function BestComboCard({ runs, onApply }) {
  if (runs.length < 5) return (
    <div className="p-3 rounded text-xs muted text-center"
      style={{ background: 'rgba(255,255,255,0.03)', border: '1px solid rgba(255,255,255,0.08)' }}>
      Run {5 - runs.length} more combination{5 - runs.length !== 1 ? 's' : ''} to unlock Best Combo Finder
    </div>
  )

  const candidates = runs.filter(r => r.win_rate > 0.5 && r.total_trades > 30)
  if (!candidates.length) return (
    <div className="p-3 rounded text-xs muted text-center"
      style={{ background: 'rgba(255,255,255,0.03)', border: '1px solid rgba(255,255,255,0.08)' }}>
      No run yet has win rate &gt; 50% with &gt; 30 trades
    </div>
  )

  const best = candidates.reduce((a, b) => (b.profit_factor ?? 0) > (a.profit_factor ?? 0) ? b : a)
  const params = best._params || {}

  return (
    <div className="rounded p-3 flex flex-col gap-3"
      style={{ background: 'rgba(0,255,136,0.05)', border: '1px solid rgba(0,255,136,0.3)' }}>
      <div className="flex justify-between items-center">
        <span className="text-xs uppercase tracking-widest font-bold" style={{ color: '#00FF88' }}>
          ★ Best Combo — Run #{best.run}
        </span>
        <button onClick={() => onApply(params)}
          className="text-xs px-3 py-1 rounded font-bold uppercase"
          style={{ background: 'rgba(0,255,136,0.2)', color: '#00FF88', border: '1px solid #00FF88' }}>
          Apply to Live Bot
        </button>
      </div>
      <div className="grid grid-cols-3 gap-2 text-xs">
        <div><span className="muted">Win Rate </span><span className="pos font-bold">{formatPct(best.win_rate * 100)}</span></div>
        <div><span className="muted">Return </span><span className="pos font-bold">{formatPct(best.total_return * 100)}</span></div>
        <div><span className="muted">Profit Factor </span><span className="pos font-bold">{formatNumber(best.profit_factor)}</span></div>
        <div><span className="muted">Sharpe </span><span className="accent font-bold">{formatNumber(best.sharpe)}</span></div>
        <div><span className="muted">Max DD </span><span className="neg font-bold">{formatPct(best.max_dd * 100)}</span></div>
        <div><span className="muted">Trades </span><span className="font-bold">{best.total_trades}</span></div>
      </div>
      <div className="grid grid-cols-3 gap-x-4 gap-y-1 text-xs border-t border-border pt-2">
        {PARAM_DEFS.map(d => (
          <div key={d.key}>
            <span className="muted">{d.label.split('(')[0].trim()}: </span>
            <span className="accent font-mono">{d.format(params[d.key] ?? d.default)}</span>
          </div>
        ))}
      </div>
    </div>
  )
}

// ── Main component ────────────────────────────────────────────────────────────

const DATE_PRESETS = [
  { id: '2023_full', label: '2023 Full Year', start: '2023-01-03', end: '2023-12-29' },
  { id: '2022_bear', label: '2022 Bear',      start: '2022-01-03', end: '2022-12-30' },
  { id: 'ytd',       label: 'YTD',            start: `${new Date().getFullYear()}-01-02`, end: new Date().toISOString().slice(0,10) },
]

export default function ParameterOptimizer({ get, post }) {
  const [params, setParams]         = useState({ ...DEFAULTS })
  const [datePreset, setDatePreset] = useState(DATE_PRESETS[0])
  const [customDates, setCustomDates] = useState({ start: '', end: '' })
  const [useCustom, setUseCustom]   = useState(false)
  const [runCount, setRunCount]     = useState(0)
  const [runs, setRuns]             = useState([])
  const [jobId, setJobId]           = useState(null)
  const [jobStatus, setJobStatus]   = useState(null)
  const [applyMsg, setApplyMsg]     = useState('')
  const [batchCount, setBatchCount] = useState(10)
  const [batchProgress, setBatchProgress] = useState(null)
  const pollRef    = useRef(null)
  const batchAbort = useRef(false)

  const isRunning = jobStatus?.status === 'running'

  const handleParam = useCallback((key, val) => {
    setParams(p => ({ ...p, [key]: val }))
  }, [])

  const handleReset = useCallback(() => {
    setParams({ ...DEFAULTS })
  }, [])

  // Run one backtest and return result (polls until done, no state side-effects)
  const runOne = useCallback(async (runParams) => {
    const start = useCustom ? customDates.start : datePreset.start
    const end   = useCustom ? customDates.end   : datePreset.end
    const body  = { start_date: start, end_date: end, symbols: ['SPY', 'QQQ', 'AAPL'], parameters: runParams }
    const resp  = await post('/api/backtest', body)
    if (!resp?.job_id) {
      console.warn('[Batch] POST failed')
      return null
    }
    let status, maxPolls = 300, polls = 0
    while (polls < maxPolls) {
      await new Promise(r => setTimeout(r, 2000))
      status = await get(`/api/backtest/${resp.job_id}`, { ttl: 0 })
      polls++
      if (!status) {
        console.warn('[Batch] Poll #' + polls + ' returned null')
        continue
      }
      if (status.status === 'done') {
        console.log('[Batch] Done after ' + polls + ' polls')
        setJobStatus(status)
        return status.result
      }
      if (status.status === 'error') {
        console.error('[Batch] Job error: ' + status.error)
        setJobStatus(status)
        return null
      }
      setJobStatus(status)
    }
    console.warn('[Batch] Max polls reached')
    return null
  }, [useCustom, customDates, datePreset, post, get])

  const handleBatchRun = useCallback(async () => {
    batchAbort.current = false
    let localCount = runCount
    for (let i = 0; i < batchCount; i++) {
      if (batchAbort.current) break
      setBatchProgress({ current: i + 1, total: batchCount })
      const rp = randomizeParams()
      setParams(rp)
      setJobStatus({ status: 'running', progress: 0, message: `Batch ${i + 1}/${batchCount} — submitting…` })
      const result = await runOne(rp)
      if (result) {
        localCount++
        const runNum = localCount
        setRunCount(localCount)
        setRuns(prev => [...prev, {
          run:          runNum,
          rsi:          `${rp.rsi_bull_threshold}/${rp.rsi_bear_threshold}`,
          vol_ratio:    rp.volume_ratio_min,
          strength_min: rp.signal_strength_min,
          stop_pct:     rp.stop_loss_pct,
          target_pct:   rp.profit_target_pct,
          win_rate:     result.win_rate ?? 0,
          total_return: result.total_return_pct ?? 0,
          profit_factor:result.profit_factor ?? 0,
          max_dd:       result.max_drawdown_pct ?? 0,
          total_trades: result.total_trades ?? 0,
          sharpe:       result.sharpe_ratio ?? 0,
          _params:      { ...rp },
        }])
      }
    }
    setBatchProgress(null)
    setJobStatus(null)
  }, [batchCount, runOne, runCount])

  const startPoll = useCallback((id) => {
    if (pollRef.current) clearInterval(pollRef.current)
    pollRef.current = setInterval(async () => {
      const status = await get(`/api/backtest/${id}`, { ttl: 0 })
      if (!status) return
      setJobStatus(status)
      if (status.status === 'done' || status.status === 'error') {
        clearInterval(pollRef.current)
        pollRef.current = null
        if (status.status === 'done' && status.result) {
          const r = status.result
          const runNum = runCount + 1
          setRunCount(runNum)
          setRuns(prev => [...prev, {
            run: runNum,
            rsi:          `${params.rsi_bull_threshold}/${params.rsi_bear_threshold}`,
            vol_ratio:    params.volume_ratio_min,
            strength_min: params.signal_strength_min,
            stop_pct:     params.stop_loss_pct,
            target_pct:   params.profit_target_pct,
            win_rate:     r.win_rate ?? 0,
            total_return: r.total_return_pct ?? 0,
            profit_factor:r.profit_factor ?? 0,
            max_dd:       r.max_drawdown_pct ?? 0,
            total_trades: r.total_trades ?? 0,
            sharpe:       r.sharpe_ratio ?? 0,
            _params:      { ...params },
          }])
        }
      }
    }, 2000)
  }, [get, params, runCount])

  useEffect(() => () => { if (pollRef.current) clearInterval(pollRef.current) }, [])

  const handleRun = useCallback(async () => {
    const start = useCustom ? customDates.start : datePreset.start
    const end   = useCustom ? customDates.end   : datePreset.end
    if (!start || !end) return

    setJobStatus({ status: 'running', progress: 0, message: 'Submitting…' })
    const body = {
      start_date: start, end_date: end,
      symbols: ['SPY', 'QQQ', 'AAPL'],
      parameters: { ...params },
    }
    const resp = await post('/api/backtest', body)
    if (resp?.job_id) {
      setJobId(resp.job_id)
      startPoll(resp.job_id)
    } else {
      setJobStatus({ status: 'error', error: resp?.error || 'Failed to start' })
    }
  }, [useCustom, customDates, datePreset, params, post, startPoll])

  const handleApply = useCallback(async (applyParams) => {
    setApplyMsg('')
    const resp = await post('/api/bot/apply-parameters', applyParams)
    if (resp?.status === 'applied') {
      setApplyMsg('✓ Parameters applied to live bot')
      setTimeout(() => setApplyMsg(''), 4000)
    } else {
      setApplyMsg('✗ Failed: ' + (resp?.error || 'unknown error'))
    }
  }, [post])

  const handleExport = useCallback(() => {
    if (!runs.length) return
    const headers = RESULT_COLS.map(c => c.label).join(',')
    const rows = runs.map(r => RESULT_COLS.map(c => r[c.key] ?? '').join(','))
    const csv = [headers, ...rows].join('\n')
    const blob = new Blob([csv], { type: 'text/csv' })
    const url = URL.createObjectURL(blob)
    const a = document.createElement('a')
    a.href = url; a.download = 'optimizer_results.csv'; a.click()
    URL.revokeObjectURL(url)
  }, [runs])

  return (
    <div className="flex flex-col gap-4 p-3">

      {/* ── Section 1: Sliders ─────────────────────────────────────────────── */}
      <div className="card p-3">
        <div className="flex justify-between items-center mb-3">
          <span className="text-xs uppercase tracking-widest muted">Parameters</span>
          <button onClick={handleReset} className="text-xs px-2 py-1 rounded"
            style={{ background: 'rgba(255,255,255,0.06)', color: '#9CA3AF', border: '1px solid rgba(255,255,255,0.1)' }}>
            Reset to Defaults
          </button>
        </div>
        <div className="grid grid-cols-2 gap-x-8 gap-y-4">
          {PARAM_DEFS.map(d => (
            <ParamSlider key={d.key} def={d} value={params[d.key]} onChange={handleParam} />
          ))}
        </div>
      </div>

      {/* ── Section 2: Date range ──────────────────────────────────────────── */}
      <div className="card p-3">
        <span className="text-xs uppercase tracking-widest muted block mb-3">Date Range</span>
        <div className="flex flex-wrap gap-2 mb-3">
          {DATE_PRESETS.map(p => (
            <PresetBtn key={p.id} label={p.label}
              active={!useCustom && datePreset.id === p.id}
              onClick={() => { setDatePreset(p); setUseCustom(false) }} />
          ))}
          <PresetBtn label="Custom" active={useCustom} onClick={() => setUseCustom(true)} />
        </div>
        {useCustom && (
          <div className="flex gap-3">
            {[['start', 'Start'], ['end', 'End']].map(([k, label]) => (
              <label key={k} className="flex flex-col gap-1">
                <span className="text-xs muted">{label}</span>
                <input type="date" value={customDates[k] || ''}
                  onChange={e => setCustomDates(d => ({ ...d, [k]: e.target.value }))}
                  className="font-mono text-xs px-2 py-1 rounded"
                  style={{ background: 'rgba(255,255,255,0.07)', border: '1px solid rgba(255,255,255,0.15)', color: '#E8EAED' }} />
              </label>
            ))}
          </div>
        )}
      </div>

      {/* ── Section 3: Run button + progress ──────────────────────────────── */}
      <div className="card p-3 flex flex-col gap-3">
        <div className="flex items-center gap-3">
          <button onClick={handleRun} disabled={isRunning}
            className="text-xs px-5 py-2 rounded font-bold uppercase tracking-wider disabled:opacity-50"
            style={{ background: isRunning ? 'rgba(0,217,255,0.1)' : 'rgba(0,217,255,0.2)',
                     color: '#00D9FF', border: '1px solid #00D9FF' }}>
            {isRunning ? '⟳ Running…' : '▶ Run with These Parameters'}
          </button>
          {applyMsg && <span className="text-xs" style={{ color: applyMsg.startsWith('✓') ? '#00FF88' : '#FF0055' }}>{applyMsg}</span>}
        </div>
        {isRunning && (
          <ProgressBar pct={jobStatus?.progress || 0} message={jobStatus?.message} />
        )}
        {jobStatus?.status === 'error' && (
          <div className="p-2 rounded text-xs" style={{ background: 'rgba(255,0,85,0.1)', border: '1px solid #FF0055', color: '#FF0055' }}>
            {jobStatus.error}
          </div>
        )}

        {/* ── Batch runner ─────────────────────────────────────────────── */}
        <div className="border-t border-border pt-3 flex flex-col gap-2">
          <div className="flex items-center gap-2 flex-wrap">
            <span className="text-xs muted uppercase tracking-wider">Auto-run</span>
            {[5, 10, 20, 50].map(n => (
              <button key={n} onClick={() => setBatchCount(n)}
                className="text-xs px-2 py-0.5 rounded font-mono"
                style={{
                  background: batchCount === n ? 'rgba(0,217,255,0.2)' : 'rgba(255,255,255,0.05)',
                  color: batchCount === n ? '#00D9FF' : '#6B7280',
                  border: batchCount === n ? '1px solid #00D9FF' : '1px solid rgba(255,255,255,0.1)',
                }}>
                {n}
              </button>
            ))}
            <input
              type="number" min={1} max={200} value={batchCount}
              onChange={e => setBatchCount(Math.max(1, parseInt(e.target.value) || 1))}
              className="w-16 text-xs font-mono text-center rounded px-1 py-0.5"
              style={{ background: 'rgba(255,255,255,0.05)', border: '1px solid rgba(255,255,255,0.15)', color: '#E8EAED' }}
            />
            <span className="text-xs muted">simulations</span>
            {batchProgress ? (
              <button onClick={() => { batchAbort.current = true }}
                className="text-xs px-3 py-1 rounded font-bold uppercase tracking-wider ml-auto"
                style={{ background: 'rgba(255,0,85,0.15)', color: '#FF0055', border: '1px solid #FF0055' }}>
                ■ Stop
              </button>
            ) : (
              <button onClick={handleBatchRun} disabled={isRunning}
                className="text-xs px-4 py-1 rounded font-bold uppercase tracking-wider ml-auto disabled:opacity-50"
                style={{ background: 'rgba(255,184,0,0.15)', color: '#FFB800', border: '1px solid #FFB800' }}>
                ⚡ Run {batchCount} Random Combos
              </button>
            )}
          </div>
          {batchProgress && (
            <div className="flex flex-col gap-1">
              <div className="flex justify-between text-xs">
                <span className="muted">Simulation {batchProgress.current} of {batchProgress.total}</span>
                <span className="accent font-mono">{Math.round(batchProgress.current / batchProgress.total * 100)}%</span>
              </div>
              <div className="h-1.5 rounded-full overflow-hidden" style={{ background: 'rgba(255,255,255,0.08)' }}>
                <div className="h-full rounded-full transition-all duration-500"
                  style={{ width: `${batchProgress.current / batchProgress.total * 100}%`, background: '#FFB800' }} />
              </div>
            </div>
          )}
        </div>
      </div>

      {/* ── Section 4: Results table ───────────────────────────────────────── */}
      {runs.length > 0 && (
        <div className="card p-3">
          <div className="text-xs uppercase tracking-widest muted mb-3">Results Comparison</div>
          <ResultsTable runs={runs} onClear={() => { setRuns([]); setRunCount(0) }} onExport={handleExport} />
        </div>
      )}

      {/* ── Section 5: Best combo finder ──────────────────────────────────── */}
      <div className="card p-3">
        <div className="text-xs uppercase tracking-widest muted mb-3">Best Combination Finder</div>
        <BestComboCard runs={runs} onApply={handleApply} />
      </div>

    </div>
  )
}
