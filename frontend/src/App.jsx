import React, { useEffect, useState, useCallback } from 'react'
import { useWebSocket } from './hooks/useWebSocket.js'
import { useAPI } from './hooks/useAPI.js'
import EquityCurve from './components/EquityCurve.jsx'
import PositionsTable from './components/PositionsTable.jsx'
import SignalFeed from './components/SignalFeed.jsx'
import PortfolioGreeks from './components/PortfolioGreeks.jsx'
import TradeHistory from './components/TradeHistory.jsx'
import PipelineStatus from './components/PipelineStatus.jsx'
import {
  formatMoney, formatPercent, formatPct, formatTime, formatNumber
} from './utils/formatting.js'

const VERSION = '0.2.0'

// ── Card ──────────────────────────────────────────────────────────────────────

function Card({ title, children, className = '', action }) {
  return (
    <div className={`card flex flex-col ${className}`}>
      <div className="flex items-center justify-between px-3 py-2 border-b border-border">
        <span className="text-xs uppercase tracking-widest muted">{title}</span>
        {action}
      </div>
      <div className="flex-1 min-h-0 overflow-hidden">{children}</div>
    </div>
  )
}

// ── Stat ──────────────────────────────────────────────────────────────────────

function Stat({ label, value, color, large = false }) {
  return (
    <div className="px-3 py-2 border-b border-border last:border-0">
      <div className="text-xs muted uppercase tracking-widest mb-0.5">{label}</div>
      <div
        className={`font-mono font-semibold ${large ? 'text-lg' : 'text-sm'}`}
        style={{ color: color || '#E8EAED' }}
      >
        {value}
      </div>
    </div>
  )
}

// ── App ───────────────────────────────────────────────────────────────────────

export default function App() {
  const { data: ws, connected, lastUpdate } = useWebSocket()
  const { get, post } = useAPI()

  // Derived from WebSocket
  const balance      = ws?.balance      ?? 50000
  const dailyPnl     = ws?.daily_pnl    ?? 0
  const marketOpen   = ws?.market_open  ?? false
  const botRunning   = ws?.bot_running  ?? false
  const emergency    = ws?.emergency_stop ?? false
  const positions    = ws?.open_positions   ?? []
  const signals      = ws?.recent_signals   ?? []
  const greeks       = ws?.portfolio_greeks ?? null
  const equityCurve  = ws?.equity_curve     ?? []
  const pipeline     = ws?.pipeline_state   ?? {}
  const uptimeSec    = ws?.uptime_seconds   ?? 0

  // Performance stats (polled, slower TTL)
  const [perf, setPerf] = useState(null)
  const [health, setHealth] = useState(null)

  useEffect(() => {
    get('/api/performance', { ttl: 30000 }).then(d => d && setPerf(d))
    get('/health').then(d => d && setHealth(d))
  }, [])

  useEffect(() => {
    const iv = setInterval(() => {
      get('/api/performance', { ttl: 30000 }).then(d => d && setPerf(d))
    }, 30_000)
    return () => clearInterval(iv)
  }, [])

  const handleEmergencyStop = useCallback(async () => {
    if (!window.confirm('Activate emergency stop? All new trading will halt.')) return
    await post('/api/emergency-stop')
  }, [post])

  const handleResume = useCallback(async () => {
    await post('/api/resume')
  }, [post])

  const totalPnl     = perf?.total_pnl     ?? 0
  const winRate      = perf?.win_rate      ?? 0
  const profitFactor = perf?.profit_factor ?? null
  const drawdownPct  = perf?.current_drawdown_pct ?? 0
  const sharpe       = perf?.sharpe_estimate ?? null
  const totalTrades  = perf?.total_trades  ?? 0
  const redisOk      = health?.db_connected  // proxy for redis

  const formatUptime = (s) => {
    if (!s) return '—'
    const h = Math.floor(s / 3600)
    const m = Math.floor((s % 3600) / 60)
    return h > 0 ? `${h}h ${m}m` : `${m}m`
  }

  return (
    <div className="min-h-screen flex flex-col" style={{ background: '#0A0E27' }}>

      {/* ── Header ───────────────────────────────────────────────────────────── */}
      <header
        className="fixed top-0 left-0 right-0 z-50 flex items-center gap-4 px-4 h-12 border-b border-border"
        style={{ background: '#0A0E27' }}
      >
        {/* Logo */}
        <div className="flex items-center gap-2 mr-4">
          <span
            className="font-bold text-sm tracking-widest uppercase glow-accent px-2 py-0.5 rounded"
            style={{ color: '#00D9FF', border: '1px solid rgba(0,217,255,0.3)' }}
          >
            APEX CRUSHER
          </span>
          <span className="text-xs muted">v{VERSION}</span>
        </div>

        {/* Balance */}
        <div className="flex items-center gap-1.5">
          <span className="text-xs muted">Balance</span>
          <span className="font-mono font-bold text-sm" style={{ color: '#E8EAED' }}>
            {formatMoney(balance)}
          </span>
        </div>

        {/* Daily PnL */}
        <div className="flex items-center gap-1.5">
          <span className="text-xs muted">Day</span>
          <span
            className="font-mono font-bold"
            style={{
              color: dailyPnl > 0 ? '#00FF88' : dailyPnl < 0 ? '#FF0055' : '#6B7280',
              fontSize: Math.max(0.75, Math.min(1, 0.75 + Math.abs(dailyPnl) / 5000)) + 'rem',
            }}
          >
            {dailyPnl >= 0 ? '+' : ''}{formatMoney(dailyPnl)}
          </span>
        </div>

        {/* Market status */}
        <div className="flex items-center gap-1.5">
          <span className={`dot ${marketOpen ? 'dot-green' : 'dot-gray'}`} />
          <span className={`text-xs ${marketOpen ? 'pos' : 'muted'}`}>
            {marketOpen ? 'MARKET OPEN' : 'CLOSED'}
          </span>
        </div>

        {/* Bot status */}
        <div className="flex items-center gap-1.5">
          <span className={`dot ${botRunning ? 'dot-green' : 'dot-red'}`} />
          <span className={`text-xs ${botRunning ? 'pos' : 'neg'}`}>
            {botRunning ? 'BOT LIVE' : 'BOT OFFLINE'}
          </span>
        </div>

        <div className="flex-1" />

        {/* Emergency controls */}
        {emergency ? (
          <button
            onClick={handleResume}
            className="text-xs px-3 py-1 rounded font-bold uppercase tracking-wider"
            style={{ background: 'rgba(0,255,136,0.15)', color: '#00FF88', border: '1px solid #00FF88' }}
          >
            ▶ Resume Trading
          </button>
        ) : (
          <button
            onClick={handleEmergencyStop}
            className="text-xs px-3 py-1 rounded font-bold uppercase tracking-wider"
            style={{ background: 'rgba(255,0,85,0.15)', color: '#FF0055', border: '1px solid #FF0055' }}
          >
            ⬛ Emergency Stop
          </button>
        )}
      </header>

      {/* ── Body: sidebar + main ─────────────────────────────────────────────── */}
      <div className="flex flex-1 pt-12">

        {/* ── Sidebar ────────────────────────────────────────────────────────── */}
        <aside
          className="fixed top-12 left-0 bottom-8 w-52 border-r border-border flex flex-col overflow-y-auto"
          style={{ background: '#0A0E27' }}
        >
          <div className="px-3 pt-3 pb-1">
            <span className="text-xs muted uppercase tracking-widest">Performance</span>
          </div>

          <Stat
            label="Net P&L All-Time"
            value={formatMoney(totalPnl)}
            color={totalPnl >= 0 ? '#00FF88' : '#FF0055'}
            large
          />
          <Stat
            label="Win Rate"
            value={formatPct(winRate * 100)}
            color={winRate >= 0.5 ? '#00FF88' : winRate >= 0.4 ? '#FFB800' : '#FF0055'}
          />
          <Stat
            label="Profit Factor"
            value={profitFactor != null ? formatNumber(profitFactor) : '—'}
            color={profitFactor != null && profitFactor >= 1.5 ? '#00FF88' : '#E8EAED'}
          />
          <Stat
            label="Drawdown"
            value={formatPct(drawdownPct * 100)}
            color={drawdownPct >= 0.05 ? '#FF0055' : drawdownPct >= 0.03 ? '#FFB800' : '#6B7280'}
          />
          <Stat
            label="Sharpe (est)"
            value={sharpe != null ? formatNumber(sharpe) : '—'}
            color={sharpe != null && sharpe >= 1 ? '#00FF88' : '#E8EAED'}
          />
          <Stat
            label="Total Trades"
            value={totalTrades.toLocaleString()}
          />
          <Stat
            label="Open Positions"
            value={positions.length}
            color={positions.length > 0 ? '#00D9FF' : '#6B7280'}
          />
        </aside>

        {/* ── Main content ─────────────────────────────────────────────────── */}
        <main className="ml-52 flex-1 p-3 pb-10 min-w-0">
          <div className="grid grid-cols-3 gap-3">

            {/* ── Column 1: Positions + Signals ──────────────────────────── */}
            <div className="flex flex-col gap-3">
              <Card title={`Active Positions (${positions.length})`}>
                <PositionsTable positions={positions} />
              </Card>

              <Card title="Live Signal Feed">
                <SignalFeed signals={signals} />
              </Card>
            </div>

            {/* ── Column 2: Equity Curve + Greeks ────────────────────────── */}
            <div className="flex flex-col gap-3">
              <Card title="Equity Curve">
                <div className="p-2">
                  <div className="flex justify-between items-baseline mb-2 px-1">
                    <span className="text-xs muted">60-day running balance</span>
                    <span
                      className="font-mono font-bold text-sm"
                      style={{ color: balance >= 50000 ? '#00FF88' : '#FF0055' }}
                    >
                      {formatMoney(balance)}
                    </span>
                  </div>
                  <EquityCurve curve={equityCurve} balance={balance} />
                </div>
              </Card>

              <Card title="Portfolio Greeks">
                <PortfolioGreeks greeks={greeks} />
              </Card>
            </div>

            {/* ── Column 3: Trade History + Pipeline ─────────────────────── */}
            <div className="flex flex-col gap-3">
              <Card title="Trade History (last 20)">
                <TradeHistory trades={ws ? [] : []} />
                <HistoryLoader get={get} />
              </Card>

              <Card title="Pipeline Status">
                <PipelineStatus
                  pipelineState={pipeline}
                  alpacaConnected={health?.alpaca_connected ?? false}
                  redisConnected={health?.db_connected ?? false}
                  marketOpen={marketOpen}
                />
              </Card>
            </div>

          </div>
        </main>
      </div>

      {/* ── Footer ───────────────────────────────────────────────────────────── */}
      <footer
        className="fixed bottom-0 left-0 right-0 z-50 flex items-center gap-4 px-4 h-8 border-t border-border text-xs muted"
        style={{ background: '#0A0E27' }}
      >
        <span className={`dot ${connected ? 'dot-green' : 'dot-red'} mr-1`} />
        <span className={connected ? 'pos' : 'neg'}>
          {connected ? 'WebSocket connected' : 'WebSocket reconnecting…'}
        </span>
        {lastUpdate && (
          <span className="muted">Updated {formatTime(lastUpdate)}</span>
        )}
        <span className="muted">Uptime {formatUptime(uptimeSec)}</span>
        <div className="flex-1" />
        <span className={`dot ${health?.db_connected ? 'dot-green' : 'dot-red'} mr-1`} />
        <span>{health?.db_connected ? 'DB ok' : 'DB ?'}</span>
      </footer>

    </div>
  )
}

// Separate component to load trade history without re-rendering App
function HistoryLoader({ get }) {
  const [trades, setTrades] = useState([])

  useEffect(() => {
    async function load() {
      const data = await get('/api/trades/history', { ttl: 10000 })
      if (data?.trades) setTrades(data.trades)
    }
    load()
    const iv = setInterval(load, 10_000)
    return () => clearInterval(iv)
  }, [])

  return <TradeHistory trades={trades} />
}
