import React, { useState, useEffect, useCallback, useRef, useMemo } from 'react'
import {
  LineChart, Line, BarChart, Bar, XAxis, YAxis, Tooltip, CartesianGrid,
  ResponsiveContainer, PieChart, Pie, Cell, ReferenceLine,
} from 'recharts'
import TheLabChat from './TheLabChat.jsx'
import { formatMoney, formatPct, formatNumber } from '../utils/formatting.js'

// ── Constants ─────────────────────────────────────────────────────────────────

const PARAM_DEFS = [
  { key: 'rsi_bull_threshold', label: 'RSI Bull',       min: 50,   max: 75,  step: 1,     default: 62,   fmt: v => v.toFixed(0) },
  { key: 'rsi_bear_threshold', label: 'RSI Bear',       min: 25,   max: 50,  step: 1,     default: 38,   fmt: v => v.toFixed(0) },
  { key: 'volume_ratio_min',   label: 'Vol Ratio Min',  min: 1.0,  max: 3.0, step: 0.1,   default: 1.5,  fmt: v => v.toFixed(1) },
  { key: 'iv_rank_max',        label: 'IV Rank Max',    min: 30,   max: 80,  step: 1,     default: 60,   fmt: v => v.toFixed(0) + '%' },
  { key: 'iv_rank_min',        label: 'IV Rank Min',    min: 50,   max: 90,  step: 1,     default: 70,   fmt: v => v.toFixed(0) + '%' },
  { key: 'signal_strength_min',label: 'Strength Min',   min: 0.30, max: 0.80,step: 0.05,  default: 0.55, fmt: v => v.toFixed(2) },
  { key: 'stop_loss_pct',      label: 'Stop Loss',      min: 0.05, max: 0.20,step: 0.01,  default: 0.08, fmt: v => (v*100).toFixed(0)+'%' },
  { key: 'profit_target_pct',  label: 'Profit Target',  min: 0.15, max: 0.60,step: 0.05,  default: 0.30, fmt: v => (v*100).toFixed(0)+'%' },
  { key: 'min_dte',            label: 'Min DTE',        min: 2,    max: 21,  step: 1,     default: 5,    fmt: v => v.toFixed(0) + 'd' },
  { key: 'max_dte',            label: 'Max DTE',        min: 14,   max: 60,  step: 1,     default: 45,   fmt: v => v.toFixed(0) + 'd' },
  { key: 'max_positions',      label: 'Max Positions',  min: 1,    max: 8,   step: 1,     default: 3,    fmt: v => v.toFixed(0) },
  { key: 'position_size_pct',  label: 'Position Size',  min: 0.005,max: 0.05,step: 0.005, default: 0.02, fmt: v => (v*100).toFixed(1)+'%' },
]
const DEFAULTS = Object.fromEntries(PARAM_DEFS.map(p => [p.key, p.default]))
const SYMBOLS_ALL = ['SPY','QQQ','AAPL','NVDA','TSLA','MSFT','AMD','META']
const STRATEGIES_ALL = ['momentum','iv_rank','flow']
const DATE_PRESETS = [
  { id:'2024',  label:'2024',       start:'2024-01-02', end:'2024-12-31' },
  { id:'2023',  label:'2023',       start:'2023-01-03', end:'2023-12-29' },
  { id:'2022',  label:'2022 Bear',  start:'2022-01-03', end:'2022-12-30' },
  { id:'2y',    label:'2023–2024',  start:'2023-01-03', end:'2024-12-31' },
]
const PIE_COLORS = ['#00FF88','#FF0055','#00D9FF','#FFB800','#9B59B6']
const TABS = [
  { id:'runner',    label:'▶ Backtest Runner' },
  { id:'analyzer',  label:'◈ Results Analyzer' },
  { id:'optimizer', label:'⚡ Optimizer Engine' },
  { id:'chat',      label:'✦ The Chat' },
]

// ── Helpers ───────────────────────────────────────────────────────────────────

function pearson(xs, ys) {
  const n = xs.length
  if (n < 3) return 0
  const mx = xs.reduce((a,b) => a+b,0)/n, my = ys.reduce((a,b) => a+b,0)/n
  const num = xs.reduce((s,x,i) => s+(x-mx)*(ys[i]-my), 0)
  const dx = Math.sqrt(xs.reduce((s,x) => s+(x-mx)**2,0))
  const dy = Math.sqrt(ys.reduce((s,y) => s+(y-my)**2,0))
  return dx===0||dy===0 ? 0 : num/(dx*dy)
}

function computeImportance(runs) {
  const valid = runs.filter(r => r.summary && r.params)
  if (valid.length < 3) return []
  return PARAM_DEFS.map(d => {
    const xs = valid.map(r => r.params[d.key] ?? d.default)
    const ys = valid.map(r => r.summary.profit_factor ?? 0)
    const c  = pearson(xs, ys)
    return { key:d.key, label:d.label, correlation:c, abs:Math.abs(c) }
  }).sort((a,b) => b.abs-a.abs)
}

function loadConfigs()        { try { return JSON.parse(localStorage.getItem('lab_configs')||'{}') } catch { return {} } }
function saveConfigs(c)       { localStorage.setItem('lab_configs', JSON.stringify(c)) }
function regimeFromRuns(runs) {
  const last = [...runs].find(r => r.end_date)
  const yr = parseInt(last?.end_date?.slice(0,4)||'0')
  if (yr === 2022) return 'BEAR'
  if (yr >= 2023)  return 'BULL'
  return 'NEUTRAL'
}

// ── Shared small components ───────────────────────────────────────────────────

function Slider({ def:d, value, onChange }) {
  return (
    <div className="flex flex-col gap-0.5">
      <div className="flex justify-between items-baseline">
        <span className="text-xs muted">{d.label}</span>
        <span className="text-xs font-mono font-bold" style={{color:'#00D9FF'}}>{d.fmt(value)}</span>
      </div>
      <input type="range" min={d.min} max={d.max} step={d.step} value={value}
        onChange={e => onChange(d.key, parseFloat(e.target.value))}
        className="w-full h-1 rounded-full appearance-none cursor-pointer"
        style={{accentColor:'#00D9FF'}} />
      <div className="flex justify-between" style={{color:'rgba(255,255,255,0.18)', fontSize:'0.6rem'}}>
        <span>{d.fmt(d.min)}</span><span>{d.fmt(d.max)}</span>
      </div>
    </div>
  )
}

function PBar({ pct, msg }) {
  return (
    <div className="flex flex-col gap-1">
      <div className="flex justify-between text-xs">
        <span className="muted">{msg||'Running…'}</span>
        <span className="font-mono" style={{color:'#00D9FF'}}>{pct}%</span>
      </div>
      <div className="h-1.5 rounded-full overflow-hidden" style={{background:'rgba(255,255,255,0.08)'}}>
        <div className="h-full rounded-full transition-all duration-300"
          style={{width:`${pct}%`, background:'linear-gradient(90deg,#00D9FF,#00FF88)'}} />
      </div>
    </div>
  )
}

function MetricCard({ label, value, prev, fmt, better }) {
  const delta = prev != null ? value - prev : null
  const up = delta > 0
  const improved = better==='high' ? up : better==='low' ? !up : null
  return (
    <div className="card p-3 flex flex-col gap-1">
      <span className="text-xs muted uppercase tracking-widest">{label}</span>
      <span className="text-xl font-mono font-bold" style={{color:'#E8EAED'}}>{fmt ? fmt(value) : value}</span>
      {delta!=null && Math.abs(delta)>0.0001 && (
        <span className="text-xs font-mono font-bold"
          style={{color: improved===true?'#00FF88':improved===false?'#FF0055':'#6B7280'}}>
          {up?'▲':'▼'} {fmt ? fmt(Math.abs(delta)) : Math.abs(delta).toFixed(2)}
        </span>
      )}
    </div>
  )
}

function RunItem({ run, selected, onClick }) {
  const s = run.summary||{}
  const pos = (s.total_return_pct??0) > 0
  return (
    <div onClick={onClick} className="p-2 rounded cursor-pointer flex flex-col gap-1"
      style={{
        background: selected ? 'rgba(0,217,255,0.1)' : 'rgba(255,255,255,0.03)',
        border: selected ? '1px solid #00D9FF' : `1px solid ${pos?'rgba(0,255,136,0.2)':'rgba(255,0,85,0.2)'}`,
      }}>
      <div className="flex justify-between items-center">
        <span className="text-xs font-mono muted">{run.start_date?.slice(0,7)} → {run.end_date?.slice(0,7)}</span>
        <span className="text-xs font-bold font-mono" style={{color:pos?'#00FF88':'#FF0055'}}>
          {((s.total_return_pct??0)>=0?'+':'')}{((s.total_return_pct??0)*100).toFixed(1)}%
        </span>
      </div>
      <div className="flex gap-3 text-xs muted">
        <span>WR {((s.win_rate??0)*100).toFixed(0)}%</span>
        <span>PF {(s.profit_factor??0).toFixed(2)}</span>
        <span>{s.total_trades??0}T</span>
      </div>
    </div>
  )
}

function TradeTable({ trades }) {
  const [sortKey, setSort] = useState('pnl')
  const [asc, setAsc]      = useState(false)
  const cols = [
    {key:'entry_time', lbl:'Date',     f:v=>v?.slice(0,10)||'—'},
    {key:'symbol',     lbl:'Symbol',   f:v=>v||'—'},
    {key:'strategy',   lbl:'Strategy', f:v=>v||'—'},
    {key:'action',     lbl:'Act',      f:v=>v||'—'},
    {key:'option_type',lbl:'Type',     f:v=>v||'—'},
    {key:'entry_price',lbl:'Entry',    f:v=>v!=null?'$'+v.toFixed(2):'—'},
    {key:'exit_price', lbl:'Exit',     f:v=>v!=null?'$'+v.toFixed(2):'—'},
    {key:'pnl',        lbl:'PnL',      f:v=>v!=null?(v>=0?'+$':'-$')+Math.abs(v).toFixed(0):'—'},
    {key:'exit_reason',lbl:'Reason',   f:v=>v||'—'},
  ]
  const sorted = useMemo(() => {
    if (!trades?.length) return []
    return [...trades].sort((a,b) => {
      const av=a[sortKey]??'', bv=b[sortKey]??''
      return (asc?1:-1)*(av<bv?-1:av>bv?1:0)
    })
  }, [trades, sortKey, asc])
  const toggle = k => { if(k===sortKey) setAsc(a=>!a); else { setSort(k); setAsc(false) } }
  if (!trades?.length) return <div className="text-xs muted p-4 text-center">No trade data</div>
  return (
    <div className="overflow-auto max-h-64">
      <table className="w-full text-xs">
        <thead>
          <tr className="border-b border-border sticky top-0" style={{background:'#0D1220'}}>
            {cols.map(c=>(
              <th key={c.key} onClick={()=>toggle(c.key)}
                className="px-2 py-1.5 text-left muted uppercase tracking-wider cursor-pointer hover:text-white whitespace-nowrap">
                {c.lbl}{sortKey===c.key?(asc?' ▲':' ▼'):''}
              </th>
            ))}
          </tr>
        </thead>
        <tbody>
          {sorted.map((t,i)=>(
            <tr key={i} className="border-b border-border last:border-0"
              style={{background:(t.pnl??0)>0?'rgba(0,255,136,0.04)':(t.pnl??0)<0?'rgba(255,0,85,0.04)':'transparent'}}>
              {cols.map(c=>(
                <td key={c.key} className="px-2 py-1.5 font-mono whitespace-nowrap"
                  style={{color:c.key==='pnl'?((t.pnl??0)>0?'#00FF88':(t.pnl??0)<0?'#FF0055':'#E8EAED'):'#E8EAED'}}>
                  {c.f(t[c.key])}
                </td>
              ))}
            </tr>
          ))}
        </tbody>
      </table>
    </div>
  )
}

// ── RunnerTab ─────────────────────────────────────────────────────────────────

function RunnerTab({ params, onParamChange, runs, selectedRun, onSelectRun, onRun, running, progress, progressMsg, error }) {
  const [datePreset, setDatePreset]     = useState('2024')
  const [customStart, setCustomStart]   = useState('')
  const [customEnd, setCustomEnd]       = useState('')
  const [symbols, setSymbols]           = useState(['SPY','QQQ'])
  const [strategies, setStrategies]     = useState(['momentum','iv_rank'])
  const [configName, setConfigName]     = useState('')
  const [configs, setConfigs]           = useState(loadConfigs)

  const preset   = DATE_PRESETS.find(d => d.id === datePreset)
  const startDate = datePreset === 'custom' ? customStart : preset?.start
  const endDate   = datePreset === 'custom' ? customEnd   : preset?.end

  function toggleSymbol(s) {
    setSymbols(prev => prev.includes(s) ? (prev.length>1?prev.filter(x=>x!==s):prev) : [...prev,s])
  }
  function toggleStrategy(s) {
    setStrategies(prev => prev.includes(s) ? (prev.length>1?prev.filter(x=>x!==s):prev) : [...prev,s])
  }
  function handleSaveConfig() {
    if (!configName.trim()) return
    const next = { ...configs, [configName.trim()]: { params, symbols, strategies, datePreset, customStart, customEnd } }
    setConfigs(next); saveConfigs(next); setConfigName('')
  }
  function handleLoadConfig(name) {
    const c = configs[name]; if (!c) return
    Object.entries(c.params||{}).forEach(([k,v]) => onParamChange(k,v))
    if (c.symbols)    setSymbols(c.symbols)
    if (c.strategies) setStrategies(c.strategies)
    if (c.datePreset) setDatePreset(c.datePreset)
    if (c.customStart) setCustomStart(c.customStart)
    if (c.customEnd)   setCustomEnd(c.customEnd)
  }
  function handleDeleteConfig(name) {
    const next = { ...configs }; delete next[name]; setConfigs(next); saveConfigs(next)
  }
  function handleRun() {
    onRun({ params, symbols, strategies, start_date: startDate, end_date: endDate })
  }

  return (
    <div className="flex gap-3 h-full">
      {/* Left: sliders */}
      <div className="w-64 flex flex-col gap-3 overflow-y-auto flex-shrink-0">
        <div className="card p-3 flex flex-col gap-3">
          <span className="text-xs muted uppercase tracking-widest">Parameters</span>
          {PARAM_DEFS.map(d => (
            <Slider key={d.key} def={d} value={params[d.key]??d.default} onChange={onParamChange} />
          ))}
        </div>
        {/* Config save/load */}
        <div className="card p-3 flex flex-col gap-2">
          <span className="text-xs muted uppercase tracking-widest">Configs</span>
          <div className="flex gap-1">
            <input value={configName} onChange={e=>setConfigName(e.target.value)}
              placeholder="Name…" className="flex-1 text-xs px-2 py-1 rounded"
              style={{background:'rgba(255,255,255,0.06)',border:'1px solid rgba(255,255,255,0.12)',color:'#E8EAED'}}
              onKeyDown={e=>e.key==='Enter'&&handleSaveConfig()} />
            <button onClick={handleSaveConfig} className="text-xs px-2 py-1 rounded font-bold"
              style={{background:'rgba(0,217,255,0.15)',color:'#00D9FF',border:'1px solid rgba(0,217,255,0.3)'}}>
              Save
            </button>
          </div>
          {Object.keys(configs).length > 0 && (
            <div className="flex flex-col gap-1 max-h-32 overflow-y-auto">
              {Object.keys(configs).map(name => (
                <div key={name} className="flex items-center gap-1">
                  <button onClick={()=>handleLoadConfig(name)} className="flex-1 text-left text-xs px-2 py-1 rounded truncate"
                    style={{background:'rgba(255,255,255,0.05)',color:'#E8EAED',border:'1px solid rgba(255,255,255,0.1)'}}>
                    {name}
                  </button>
                  <button onClick={()=>handleDeleteConfig(name)} className="text-xs px-1.5 py-1 rounded"
                    style={{color:'#FF0055',border:'1px solid rgba(255,0,85,0.3)'}}>✕</button>
                </div>
              ))}
            </div>
          )}
        </div>
      </div>

      {/* Middle: run config */}
      <div className="flex-1 flex flex-col gap-3 min-w-0">
        {/* Date presets */}
        <div className="card p-3 flex flex-col gap-2">
          <span className="text-xs muted uppercase tracking-widest">Date Range</span>
          <div className="flex gap-1 flex-wrap">
            {DATE_PRESETS.map(d => (
              <button key={d.id} onClick={()=>setDatePreset(d.id)}
                className="text-xs px-3 py-1 rounded font-bold"
                style={{
                  background: datePreset===d.id ? 'rgba(0,217,255,0.2)' : 'rgba(255,255,255,0.05)',
                  color: datePreset===d.id ? '#00D9FF' : '#6B7280',
                  border: `1px solid ${datePreset===d.id?'rgba(0,217,255,0.4)':'rgba(255,255,255,0.1)'}`,
                }}>
                {d.label}
              </button>
            ))}
            <button onClick={()=>setDatePreset('custom')}
              className="text-xs px-3 py-1 rounded font-bold"
              style={{
                background: datePreset==='custom' ? 'rgba(0,217,255,0.2)' : 'rgba(255,255,255,0.05)',
                color: datePreset==='custom' ? '#00D9FF' : '#6B7280',
                border: `1px solid ${datePreset==='custom'?'rgba(0,217,255,0.4)':'rgba(255,255,255,0.1)'}`,
              }}>
              Custom
            </button>
          </div>
          {datePreset === 'custom' && (
            <div className="flex gap-2">
              <input type="date" value={customStart} onChange={e=>setCustomStart(e.target.value)}
                className="text-xs px-2 py-1 rounded flex-1"
                style={{background:'rgba(255,255,255,0.06)',border:'1px solid rgba(255,255,255,0.12)',color:'#E8EAED'}} />
              <input type="date" value={customEnd} onChange={e=>setCustomEnd(e.target.value)}
                className="text-xs px-2 py-1 rounded flex-1"
                style={{background:'rgba(255,255,255,0.06)',border:'1px solid rgba(255,255,255,0.12)',color:'#E8EAED'}} />
            </div>
          )}
          {datePreset !== 'custom' && preset && (
            <span className="text-xs muted font-mono">{preset.start} → {preset.end}</span>
          )}
        </div>

        {/* Symbols */}
        <div className="card p-3 flex flex-col gap-2">
          <span className="text-xs muted uppercase tracking-widest">Symbols</span>
          <div className="flex gap-1 flex-wrap">
            {SYMBOLS_ALL.map(s => (
              <button key={s} onClick={()=>toggleSymbol(s)}
                className="text-xs px-2.5 py-1 rounded font-bold font-mono"
                style={{
                  background: symbols.includes(s) ? 'rgba(0,255,136,0.15)' : 'rgba(255,255,255,0.05)',
                  color: symbols.includes(s) ? '#00FF88' : '#6B7280',
                  border: `1px solid ${symbols.includes(s)?'rgba(0,255,136,0.4)':'rgba(255,255,255,0.1)'}`,
                }}>
                {s}
              </button>
            ))}
          </div>
        </div>

        {/* Strategies */}
        <div className="card p-3 flex flex-col gap-2">
          <span className="text-xs muted uppercase tracking-widest">Strategies</span>
          <div className="flex gap-1 flex-wrap">
            {STRATEGIES_ALL.map(s => (
              <button key={s} onClick={()=>toggleStrategy(s)}
                className="text-xs px-3 py-1 rounded font-bold capitalize"
                style={{
                  background: strategies.includes(s) ? 'rgba(155,89,182,0.2)' : 'rgba(255,255,255,0.05)',
                  color: strategies.includes(s) ? '#C39BD3' : '#6B7280',
                  border: `1px solid ${strategies.includes(s)?'rgba(155,89,182,0.5)':'rgba(255,255,255,0.1)'}`,
                }}>
                {s}
              </button>
            ))}
          </div>
        </div>

        {/* Run button */}
        <button onClick={handleRun} disabled={running}
          className="w-full py-3 rounded font-bold text-sm uppercase tracking-widest transition-all"
          style={{
            background: running ? 'rgba(255,255,255,0.05)' : 'linear-gradient(135deg,#00D9FF,#00FF88)',
            color: running ? '#6B7280' : '#0A0E27',
            cursor: running ? 'not-allowed' : 'pointer',
          }}>
          {running ? '⏳ Running…' : '▶ Run Backtest'}
        </button>

        {running && <PBar pct={progress} msg={progressMsg} />}
        {error && <div className="text-xs p-2 rounded" style={{background:'rgba(255,0,85,0.1)',color:'#FF0055',border:'1px solid rgba(255,0,85,0.3)'}}>{error}</div>}
      </div>

      {/* Right: run history */}
      <div className="w-64 flex flex-col gap-2 flex-shrink-0">
        <span className="text-xs muted uppercase tracking-widest px-1">Run History ({runs.length})</span>
        {runs.length === 0 && (
          <div className="card p-3 flex flex-col gap-1.5">
            <div className="text-xs muted">No runs yet. Configure parameters and date range, then hit <strong style={{color:'#00D9FF'}}>Run Backtest</strong>.</div>
          </div>
        )}
        <div className="flex flex-col gap-1.5 overflow-y-auto">
          {[...runs].reverse().map(r => (
            <RunItem key={r.run_id} run={r} selected={selectedRun?.run_id===r.run_id} onClick={()=>onSelectRun(r)} />
          ))}
        </div>
      </div>
    </div>
  )
}

// ── AnalyzerTab ───────────────────────────────────────────────────────────────

function AnalyzerTab({ run, prevRun, runs, onSelectRun, onAsk, aiResponse, aiLoading, post }) {
  const [question, setQuestion] = useState('')
  const [chatHistory, setChatHistory] = useState([])
  const [streaming, setStreaming]     = useState(false)
  const [streamText, setStreamText]   = useState('')
  const chatRef = useRef(null)

  const s   = run?.summary   || {}
  const ps  = prevRun?.summary || {}
  const eq  = run?.equity_curve || []
  const daily = run?.daily_pnl  || []

  const winData = s.total_trades
    ? [{ name:'Win', value: Math.round((s.win_rate??0)*s.total_trades) },
       { name:'Loss',value: Math.round((1-(s.win_rate??0))*s.total_trades) }]
    : []
  const stratData = Object.entries(s.strategy_breakdown||{}).map(([k,v])=>({name:k,value:v}))

  async function handleAsk(q) {
    const text = q || question.trim()
    if (!text || streaming) return
    setQuestion('')
    setChatHistory(h => [...h, { role:'user', text }])
    setStreaming(true); setStreamText('')
    try {
      const ctrl = new AbortController()
      const res = await fetch('/api/lab/chat', {
        method:'POST',
        headers:{'Content-Type':'application/json'},
        body: JSON.stringify({ question: text }),
        signal: ctrl.signal,
      })
      const reader = res.body.getReader()
      const dec    = new TextDecoder()
      let buf = '', full = ''
      while (true) {
        const { done, value } = await reader.read()
        if (done) break
        buf += dec.decode(value, { stream:true })
        const lines = buf.split('\n')
        buf = lines.pop()
        for (const line of lines) {
          if (!line.startsWith('data:')) continue
          const payload = line.slice(5).trim()
          if (payload === '[DONE]') { reader.cancel(); break }
          try { const { text: chunk } = JSON.parse(payload); full += chunk; setStreamText(full) } catch {}
        }
      }
      setChatHistory(h => [...h, { role:'ai', text: full }])
    } catch (e) {
      setChatHistory(h => [...h, { role:'ai', text:'[Error reaching AI]' }])
    }
    setStreaming(false); setStreamText('')
    setTimeout(() => chatRef.current?.scrollTo(0, chatRef.current.scrollHeight), 50)
  }

  function renderContent(text) {
    return text.split(/(\*\*[^*]+\*\*)/).map((part,i) =>
      part.startsWith('**') && part.endsWith('**')
        ? <strong key={i} style={{color:'#00FF88'}}>{part.slice(2,-2)}</strong>
        : <span key={i}>{part}</span>
    )
  }

  if (!run) return (
    <div className="flex flex-col items-center justify-center gap-4 h-64">
      <div className="text-xs muted text-center">
        No run selected. Go to <strong style={{color:'#00D9FF'}}>▶ Backtest Runner</strong> to run your first backtest, then come back here.
      </div>
      {runs?.length > 0 && (
        <div className="flex flex-col gap-1 w-72">
          <span className="text-xs muted uppercase tracking-widest text-center">Or pick a previous run:</span>
          {[...runs].reverse().slice(0, 5).map(r => (
            <RunItem key={r.run_id} run={r} selected={false} onClick={() => onSelectRun(r)} />
          ))}
        </div>
      )}
    </div>
  )

  return (
    <div className="flex flex-col gap-4 overflow-y-auto">
      {/* Run selector header */}
      {runs?.length > 1 && (
        <div className="flex items-center gap-2 p-2 rounded" style={{background:'rgba(255,255,255,0.03)',border:'1px solid rgba(255,255,255,0.08)'}}>
          <span className="text-xs muted flex-shrink-0">Viewing:</span>
          <select
            value={run.run_id}
            onChange={e => {
              const r = runs.find(x => x.run_id === e.target.value)
              if (r) onSelectRun(r)
            }}
            className="text-xs flex-1 px-2 py-1 rounded"
            style={{background:'rgba(255,255,255,0.06)',border:'1px solid rgba(255,255,255,0.15)',color:'#E8EAED'}}>
            {[...runs].reverse().map(r => {
              const ret = ((r.summary?.total_return_pct??0)*100).toFixed(1)
              const sign = parseFloat(ret) >= 0 ? '+' : ''
              return (
                <option key={r.run_id} value={r.run_id} style={{background:'#0D1220'}}>
                  {r.start_date?.slice(0,7)} → {r.end_date?.slice(0,7)} | {sign}{ret}% | WR {((r.summary?.win_rate??0)*100).toFixed(0)}% | PF {(r.summary?.profit_factor??0).toFixed(2)}
                </option>
              )
            })}
          </select>
          {run.ai_analysis && <span className="text-xs" style={{color:'#00FF88'}}>✦ AI Ready</span>}
        </div>
      )}
      {/* Metric cards */}
      <div className="grid grid-cols-6 gap-2">
        <MetricCard label="Total Return"  value={(s.total_return_pct??0)*100}  prev={ps.total_return_pct!=null?(ps.total_return_pct)*100:null} fmt={v=>v.toFixed(1)+'%'} better="high"/>
        <MetricCard label="Win Rate"      value={(s.win_rate??0)*100}           prev={ps.win_rate!=null?ps.win_rate*100:null}                   fmt={v=>v.toFixed(1)+'%'} better="high"/>
        <MetricCard label="Profit Factor" value={s.profit_factor??0}            prev={ps.profit_factor??null}                                   fmt={v=>v.toFixed(2)}     better="high"/>
        <MetricCard label="Max Drawdown"  value={(s.max_drawdown_pct??0)*100}   prev={ps.max_drawdown_pct!=null?ps.max_drawdown_pct*100:null}   fmt={v=>v.toFixed(1)+'%'} better="low"/>
        <MetricCard label="Sharpe"        value={s.sharpe_ratio??0}             prev={ps.sharpe_ratio??null}                                    fmt={v=>v.toFixed(2)}     better="high"/>
        <MetricCard label="Total Trades"  value={s.total_trades??0}             prev={ps.total_trades??null}                                    fmt={v=>v.toFixed(0)}/>
      </div>

      {/* Charts row */}
      <div className="grid grid-cols-2 gap-3">
        {/* Equity curve */}
        <div className="card p-3">
          <span className="text-xs muted uppercase tracking-widest">Equity Curve</span>
          <ResponsiveContainer width="100%" height={160}>
            <LineChart data={eq} margin={{top:8,right:4,left:0,bottom:0}}>
              <CartesianGrid strokeDasharray="3 3" stroke="rgba(255,255,255,0.06)" />
              <XAxis dataKey="date" tick={{fontSize:9,fill:'#6B7280'}} tickLine={false} axisLine={false}
                tickFormatter={v=>v?.slice(5)||v} interval="preserveStartEnd"/>
              <YAxis tick={{fontSize:9,fill:'#6B7280'}} tickLine={false} axisLine={false}
                tickFormatter={v=>formatMoney(v)} width={55}/>
              <Tooltip contentStyle={{background:'#0D1220',border:'1px solid #1E2A3A',borderRadius:6,fontSize:11}}
                formatter={v=>[formatMoney(v),'Equity']} labelStyle={{color:'#6B7280'}}/>
              <ReferenceLine y={eq[0]?.equity??50000} stroke="rgba(255,255,255,0.15)" strokeDasharray="4 4"/>
              <Line type="monotone" dataKey="equity" stroke="#00D9FF" strokeWidth={2} dot={false}/>
            </LineChart>
          </ResponsiveContainer>
        </div>

        {/* Daily PnL */}
        <div className="card p-3">
          <span className="text-xs muted uppercase tracking-widest">Daily PnL</span>
          <ResponsiveContainer width="100%" height={160}>
            <BarChart data={daily} margin={{top:8,right:4,left:0,bottom:0}}>
              <CartesianGrid strokeDasharray="3 3" stroke="rgba(255,255,255,0.06)" />
              <XAxis dataKey="date" tick={{fontSize:9,fill:'#6B7280'}} tickLine={false} axisLine={false}
                tickFormatter={v=>v?.slice(5)||v} interval="preserveStartEnd"/>
              <YAxis tick={{fontSize:9,fill:'#6B7280'}} tickLine={false} axisLine={false}
                tickFormatter={v=>formatMoney(v)} width={55}/>
              <Tooltip contentStyle={{background:'#0D1220',border:'1px solid #1E2A3A',borderRadius:6,fontSize:11}}
                formatter={v=>[formatMoney(v),'PnL']} labelStyle={{color:'#6B7280'}}/>
              <ReferenceLine y={0} stroke="rgba(255,255,255,0.2)"/>
              <Bar dataKey="pnl" radius={[2,2,0,0]}>
                {(daily||[]).map((d,i)=>(
                  <Cell key={i} fill={(d.pnl??0)>=0?'#00FF88':'#FF0055'}/>
                ))}
              </Bar>
            </BarChart>
          </ResponsiveContainer>
        </div>

        {/* Win/Loss pie */}
        <div className="card p-3">
          <span className="text-xs muted uppercase tracking-widest">Win / Loss Split</span>
          {winData.length > 0 ? (
            <ResponsiveContainer width="100%" height={140}>
              <PieChart>
                <Pie data={winData} cx="50%" cy="50%" innerRadius={35} outerRadius={55}
                  paddingAngle={3} dataKey="value">
                  {winData.map((_, i) => <Cell key={i} fill={i===0?'#00FF88':'#FF0055'}/>)}
                </Pie>
                <Tooltip contentStyle={{background:'#0D1220',border:'1px solid #1E2A3A',borderRadius:6,fontSize:11}}/>
                <text x="50%" y="50%" textAnchor="middle" dominantBaseline="middle"
                  style={{fill:'#E8EAED',fontSize:13,fontWeight:'bold'}}>
                  {((s.win_rate??0)*100).toFixed(0)}%
                </text>
              </PieChart>
            </ResponsiveContainer>
          ) : <div className="text-xs muted p-4 text-center">No data</div>}
        </div>

        {/* Strategy breakdown */}
        <div className="card p-3">
          <span className="text-xs muted uppercase tracking-widest">Strategy Mix</span>
          {stratData.length > 0 ? (
            <ResponsiveContainer width="100%" height={140}>
              <PieChart>
                <Pie data={stratData} cx="50%" cy="50%" innerRadius={35} outerRadius={55}
                  paddingAngle={3} dataKey="value">
                  {stratData.map((_, i) => <Cell key={i} fill={PIE_COLORS[i%PIE_COLORS.length]}/>)}
                </Pie>
                <Tooltip contentStyle={{background:'#0D1220',border:'1px solid #1E2A3A',borderRadius:6,fontSize:11}}/>
              </PieChart>
            </ResponsiveContainer>
          ) : <div className="text-xs muted p-4 text-center">No data</div>}
        </div>
      </div>

      {/* Trade table */}
      <div className="card p-3">
        <span className="text-xs muted uppercase tracking-widest mb-2 block">Trade Log ({run?.all_trades?.length||0})</span>
        <TradeTable trades={run?.all_trades||[]} />
      </div>

      {/* AI analysis */}
      <div className="card p-3 flex flex-col gap-3">
        <span className="text-xs muted uppercase tracking-widest">AI Analysis</span>
        {run?.ai_analysis && (
          <div className="text-xs p-3 rounded" style={{background:'rgba(0,255,136,0.06)',border:'1px solid rgba(0,255,136,0.15)',color:'#E8EAED',lineHeight:1.6,whiteSpace:'pre-wrap'}}>
            {run.ai_analysis}
          </div>
        )}
        <div className="flex gap-1 flex-wrap">
          {['What drove performance?','Where did we lose money?','How does this compare?','What to optimize?'].map(q=>(
            <button key={q} onClick={()=>handleAsk(q)} disabled={streaming}
              className="text-xs px-2 py-1 rounded"
              style={{background:'rgba(0,217,255,0.08)',color:'#00D9FF',border:'1px solid rgba(0,217,255,0.2)',cursor:streaming?'not-allowed':'pointer'}}>
              {q}
            </button>
          ))}
        </div>
        {/* Chat history */}
        {(chatHistory.length > 0 || streaming) && (
          <div ref={chatRef} className="flex flex-col gap-2 max-h-48 overflow-y-auto">
            {chatHistory.map((m,i)=>(
              <div key={i} className={`text-xs p-2 rounded`}
                style={m.role==='user'
                  ? {background:'rgba(0,217,255,0.08)',color:'#00D9FF',alignSelf:'flex-end',maxWidth:'80%',border:'1px solid rgba(0,217,255,0.2)'}
                  : {background:'rgba(0,255,136,0.06)',color:'#E8EAED',border:'1px solid rgba(0,255,136,0.15)',lineHeight:1.5}
                }>
                {renderContent(m.text)}
              </div>
            ))}
            {streaming && (
              <div className="text-xs p-2 rounded" style={{background:'rgba(0,255,136,0.06)',color:'#E8EAED',border:'1px solid rgba(0,255,136,0.15)',lineHeight:1.5}}>
                {streamText}<span className="animate-pulse">▊</span>
              </div>
            )}
          </div>
        )}
        {/* Custom question */}
        <div className="flex gap-2">
          <input value={question} onChange={e=>setQuestion(e.target.value)}
            onKeyDown={e=>e.key==='Enter'&&handleAsk()}
            placeholder="Ask the AI anything about this run…"
            className="flex-1 text-xs px-3 py-2 rounded"
            style={{background:'rgba(255,255,255,0.06)',border:'1px solid rgba(255,255,255,0.12)',color:'#E8EAED'}}/>
          <button onClick={()=>handleAsk()} disabled={streaming||!question.trim()}
            className="text-xs px-3 py-2 rounded font-bold"
            style={{background:'linear-gradient(135deg,#00D9FF,#00FF88)',color:'#0A0E27',cursor:streaming||!question.trim()?'not-allowed':'pointer',opacity:streaming||!question.trim()?0.5:1}}>
            Ask
          </button>
        </div>
      </div>
    </div>
  )
}

// ── OptimizerTab ──────────────────────────────────────────────────────────────

function OptimizerTab({ runs, params, onParamChange, post }) {
  const [recs, setRecs]           = useState(null)
  const [recsLoading, setRecsLoading] = useState(false)
  const [prediction, setPrediction]   = useState(null)
  const [predLoading, setPredLoading] = useState(false)
  const [predDebounce, setPredDebounce] = useState(null)

  const importance = useMemo(() => computeImportance(runs), [runs])
  const regime     = useMemo(() => regimeFromRuns(runs),    [runs])

  async function handleGetRecs() {
    setRecsLoading(true)
    try {
      const data = await fetch('/api/lab/recommendations').then(r=>r.json())
      setRecs(data)
    } catch {}
    setRecsLoading(false)
  }

  function handleParamChangeWithPredict(key, val) {
    onParamChange(key, val)
    if (predDebounce) clearTimeout(predDebounce)
    const t = setTimeout(async () => {
      if (runs.length < 3) return
      setPredLoading(true)
      try {
        const res = await fetch('/api/lab/predict', {
          method:'POST', headers:{'Content-Type':'application/json'},
          body: JSON.stringify({ parameters: { ...params, [key]: val } })
        })
        const d = await res.json(); setPrediction(d)
      } catch {}
      setPredLoading(false)
    }, 2500)
    setPredDebounce(t)
  }

  function applyRecs() {
    if (!recs?.parameters) return
    Object.entries(recs.parameters).forEach(([k,v]) => onParamChange(k,v))
  }

  const regimeColor = regime==='BULL'?'#00FF88':regime==='BEAR'?'#FF0055':'#FFB800'

  return (
    <div className="flex gap-3 h-full">
      {/* Left: sliders with live prediction */}
      <div className="w-64 flex flex-col gap-3 overflow-y-auto flex-shrink-0">
        <div className="card p-3 flex flex-col gap-3">
          <span className="text-xs muted uppercase tracking-widest">Tune Parameters</span>
          <span className="text-xs muted">Adjust sliders — AI predicts performance after 2.5s pause</span>
          {PARAM_DEFS.map(d => (
            <Slider key={d.key} def={d} value={params[d.key]??d.default}
              onChange={handleParamChangeWithPredict} />
          ))}
        </div>
        {/* Prediction card */}
        {(prediction || predLoading) && (
          <div className="card p-3 flex flex-col gap-2">
            <span className="text-xs muted uppercase tracking-widest">AI Prediction</span>
            {predLoading ? (
              <div className="text-xs muted text-center py-2">Predicting…</div>
            ) : prediction && (
              <>
                <div className="grid grid-cols-2 gap-2">
                  <div className="text-center">
                    <div className="text-xs muted">Win Rate</div>
                    <div className="text-lg font-bold font-mono" style={{color:'#00FF88'}}>
                      {((prediction.win_rate??0)*100).toFixed(1)}%
                    </div>
                  </div>
                  <div className="text-center">
                    <div className="text-xs muted">Prof. Factor</div>
                    <div className="text-lg font-bold font-mono" style={{color:'#00D9FF'}}>
                      {(prediction.profit_factor??0).toFixed(2)}
                    </div>
                  </div>
                </div>
                <div className="flex justify-between text-xs">
                  <span className="muted">Confidence</span>
                  <span className="font-mono" style={{color:'#FFB800'}}>{((prediction.confidence??0)*100).toFixed(0)}%</span>
                </div>
                {prediction.percentile != null && (
                  <div className="flex justify-between text-xs">
                    <span className="muted">Percentile vs history</span>
                    <span className="font-mono" style={{color:'#9B59B6'}}>Top {(100-prediction.percentile).toFixed(0)}%</span>
                  </div>
                )}
              </>
            )}
          </div>
        )}
      </div>

      {/* Middle: param importance + regime */}
      <div className="flex-1 flex flex-col gap-3 min-w-0">
        {/* Market regime */}
        <div className="card p-3 flex items-center gap-3">
          <div>
            <div className="text-xs muted uppercase tracking-widest mb-1">Detected Market Regime</div>
            <div className="text-2xl font-bold font-mono" style={{color:regimeColor}}>{regime}</div>
          </div>
          <div className="flex-1 text-xs muted">
            {regime==='BULL' && 'Your runs span 2023–2024 bull markets. Momentum and IV strategies tend to outperform.'}
            {regime==='BEAR' && 'Your runs include 2022 bear conditions. Consider tighter stops and lower position sizes.'}
            {regime==='NEUTRAL' && 'Mixed market conditions. Balanced parameter tuning recommended.'}
          </div>
        </div>

        {/* Param importance */}
        <div className="card p-3 flex-1">
          <span className="text-xs muted uppercase tracking-widest mb-3 block">
            Parameter Importance (Pearson r vs Profit Factor, {runs.length} runs)
          </span>
          {importance.length === 0 ? (
            <div className="text-xs muted text-center py-8">Need at least 3 runs to compute importance</div>
          ) : (
            <ResponsiveContainer width="100%" height={Math.max(200, importance.length * 32)}>
              <BarChart data={importance} layout="vertical" margin={{top:0,right:16,left:80,bottom:0}}>
                <CartesianGrid strokeDasharray="3 3" stroke="rgba(255,255,255,0.06)" horizontal={false}/>
                <XAxis type="number" domain={[-1,1]} tick={{fontSize:9,fill:'#6B7280'}} tickLine={false} axisLine={false}
                  tickFormatter={v=>v.toFixed(1)}/>
                <YAxis type="category" dataKey="label" tick={{fontSize:10,fill:'#9CA3AF'}} tickLine={false} axisLine={false} width={80}/>
                <Tooltip contentStyle={{background:'#0D1220',border:'1px solid #1E2A3A',borderRadius:6,fontSize:11}}
                  formatter={(v,n,p)=>[v.toFixed(3),'Correlation']}/>
                <ReferenceLine x={0} stroke="rgba(255,255,255,0.2)"/>
                <Bar dataKey="correlation" radius={[0,3,3,0]}>
                  {importance.map((d,i)=><Cell key={i} fill={d.correlation>=0?'#00FF88':'#FF0055'}/>)}
                </Bar>
              </BarChart>
            </ResponsiveContainer>
          )}
        </div>
      </div>

      {/* Right: AI recommendations */}
      <div className="w-72 flex flex-col gap-3 flex-shrink-0">
        <div className="card p-3 flex flex-col gap-3 flex-1">
          <div className="flex items-center justify-between">
            <span className="text-xs muted uppercase tracking-widest">AI Recommendations</span>
            <button onClick={handleGetRecs} disabled={recsLoading||runs.length<3}
              className="text-xs px-2 py-1 rounded font-bold"
              style={{background:'rgba(0,217,255,0.15)',color:'#00D9FF',border:'1px solid rgba(0,217,255,0.3)',
                cursor:recsLoading||runs.length<3?'not-allowed':'pointer',opacity:runs.length<3?0.4:1}}>
              {recsLoading ? '…' : '⚡ Analyze'}
            </button>
          </div>
          {runs.length < 3 && <div className="text-xs muted">Need 3+ runs to generate recommendations</div>}
          {recs ? (
            <div className="flex flex-col gap-3 overflow-y-auto">
              {recs.reasoning && (
                <div className="text-xs p-2 rounded" style={{background:'rgba(0,255,136,0.06)',border:'1px solid rgba(0,255,136,0.15)',color:'#E8EAED',lineHeight:1.6}}>
                  {recs.reasoning}
                </div>
              )}
              {recs.parameters && (
                <div className="flex flex-col gap-1">
                  <span className="text-xs muted">Suggested values:</span>
                  {Object.entries(recs.parameters).map(([k,v])=>{
                    const d = PARAM_DEFS.find(p=>p.key===k)
                    return (
                      <div key={k} className="flex justify-between text-xs">
                        <span className="muted">{d?.label||k}</span>
                        <span className="font-mono" style={{color:'#00D9FF'}}>{d ? d.fmt(v) : v}</span>
                      </div>
                    )
                  })}
                  <button onClick={applyRecs} className="mt-1 text-xs py-1 rounded font-bold"
                    style={{background:'linear-gradient(135deg,#00D9FF,#00FF88)',color:'#0A0E27'}}>
                    Apply All
                  </button>
                </div>
              )}
              {recs.confidence != null && (
                <div className="flex justify-between text-xs">
                  <span className="muted">AI confidence</span>
                  <span className="font-mono" style={{color:'#FFB800'}}>{((recs.confidence??0)*100).toFixed(0)}%</span>
                </div>
              )}
            </div>
          ) : !recsLoading && runs.length>=3 && (
            <div className="text-xs muted text-center py-4">Click Analyze to get AI-powered parameter recommendations</div>
          )}
        </div>
      </div>
    </div>
  )
}

// ── TheLab (main export) ──────────────────────────────────────────────────────

export default function TheLab({ get, post }) {
  const [activeTab, setActiveTab]   = useState('runner')
  const [params, setParams]         = useState(DEFAULTS)
  const [runs, setRuns]             = useState([])
  const [selectedRun, setSelectedRun] = useState(null)
  const [running, setRunning]       = useState(false)
  const [progress, setProgress]     = useState(0)
  const [progressMsg, setProgressMsg] = useState('')
  const [error, setError]           = useState('')
  const [aiResponse, setAiResponse] = useState('')
  const [aiLoading, setAiLoading]   = useState(false)
  const pollRef = useRef(null)

  function onParamChange(key, val) {
    setParams(prev => ({ ...prev, [key]: val }))
  }

  async function handleRun({ params: runParams, symbols, strategies, start_date, end_date }) {
    setRunning(true); setProgress(0); setProgressMsg('Starting…'); setError('')
    try {
      const res = await fetch('/api/lab/backtest', {
        method:'POST', headers:{'Content-Type':'application/json'},
        body: JSON.stringify({ ...runParams, symbols, strategies, start_date, end_date })
      })
      if (!res.ok) { setError('Failed to start backtest'); setRunning(false); return }
      const { job_id } = await res.json()
      if (!job_id) { setError('No job ID returned'); setRunning(false); return }

      let polls = 0
      while (polls < 300) {
        await new Promise(r => setTimeout(r, 1000))
        polls++
        const status = await get(`/api/backtest/${job_id}`)
        if (!status) continue
        if (status.progress != null) { setProgress(status.progress || 0); setProgressMsg(status.message||'Running…') }
        if (status.status === 'done') {
          const runId = status.run_id || job_id
          const full = await get(`/api/lab/runs/${runId}`)
          if (full && full.run_id) {
            const addRun = r => setRuns(prev => {
              const exists = prev.find(x => x.run_id === r.run_id)
              return exists ? prev.map(x => x.run_id === r.run_id ? r : x) : [...prev, r]
            })
            addRun(full)
            setSelectedRun(full)
            setActiveTab('analyzer')
            // Re-fetch after delay to pick up AI analysis which runs async after done
            setTimeout(async () => {
              const refreshed = await get(`/api/lab/runs/${runId}`)
              if (refreshed?.run_id) { addRun(refreshed); setSelectedRun(refreshed) }
            }, 8000)
          }
          break
        }
        if (status.status === 'error') { setError(status.error || 'Backtest failed'); break }
      }
    } catch (e) {
      setError(e.message || 'Error running backtest')
    }
    setRunning(false); setProgress(0); setProgressMsg('')
  }

  // Poll for lab runs on mount
  useEffect(() => {
    get('/api/lab/runs').then(d => { if (d?.runs) setRuns(d.runs) })
  }, [])

  const prevRun = useMemo(() => {
    if (!selectedRun || runs.length < 2) return null
    const idx = runs.findIndex(r => r.run_id === selectedRun.run_id)
    return idx > 0 ? runs[idx - 1] : null
  }, [selectedRun, runs])

  return (
    <div className="flex flex-col h-full gap-3">
      {/* Tab bar */}
      <div className="flex items-center gap-1 border-b border-border pb-3">
        {TABS.map(t => (
          <button key={t.id} onClick={() => setActiveTab(t.id)}
            className="text-xs px-4 py-1.5 rounded font-bold uppercase tracking-wider transition-all"
            style={{
              background: activeTab===t.id ? 'rgba(0,217,255,0.15)' : 'transparent',
              color: activeTab===t.id ? '#00D9FF' : '#6B7280',
              border: `1px solid ${activeTab===t.id?'rgba(0,217,255,0.35)':'transparent'}`,
            }}>
            {t.label}
          </button>
        ))}
        <div className="flex-1"/>
        <span className="text-xs muted">{runs.length} run{runs.length!==1?'s':''} stored</span>
      </div>

      {/* Tab content */}
      <div className="flex-1 min-h-0 overflow-hidden">
        {activeTab === 'runner' && (
          <div className="h-full overflow-y-auto">
            <RunnerTab
              params={params} onParamChange={onParamChange}
              runs={runs} selectedRun={selectedRun} onSelectRun={setSelectedRun}
              onRun={handleRun} running={running} progress={progress}
              progressMsg={progressMsg} error={error}
            />
          </div>
        )}
        {activeTab === 'analyzer' && (
          <div className="h-full overflow-y-auto pr-1">
            <AnalyzerTab
              run={selectedRun} prevRun={prevRun}
              runs={runs} onSelectRun={setSelectedRun}
              aiResponse={aiResponse} aiLoading={aiLoading} post={post}
            />
          </div>
        )}
        {activeTab === 'optimizer' && (
          <div className="h-full overflow-y-auto">
            <OptimizerTab runs={runs} params={params} onParamChange={onParamChange} post={post} />
          </div>
        )}
        {activeTab === 'chat' && (
          <TheLabChat get={get} labRuns={runs} currentParams={params} />
        )}
      </div>
    </div>
  )
}
