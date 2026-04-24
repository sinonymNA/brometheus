import React, { useState, useEffect, useRef, useCallback } from 'react'

// ── Constants ─────────────────────────────────────────────────────────────────

const QUICK_PROMPTS = [
  "What went wrong in the last run?",
  "Which parameters should I change?",
  "Which strategy is performing best?",
  "Should I apply these to the live bot?",
  "Compare my last 3 runs",
  "What's working in this market regime?",
  "Explain parameter importance",
]

const REGIME_COLORS = {
  BULL:    '#00FF88',
  BEAR:    '#FF0055',
  NEUTRAL: '#FFB800',
}

// ── Helpers ───────────────────────────────────────────────────────────────────

let msgId = 0
function nextId() { return ++msgId }

/**
 * Render AI message content with rudimentary markdown-like formatting:
 * - **text** → <strong>
 * - Newlines → <br />
 */
function renderContent(text) {
  // Split on **...** boundaries
  const parts = text.split(/(\*\*[^*]+\*\*)/g)
  return parts.map((part, i) => {
    if (part.startsWith('**') && part.endsWith('**')) {
      return <strong key={i} style={{ color: '#E8EAED', fontWeight: 700 }}>{part.slice(2, -2)}</strong>
    }
    // Split on newlines within plain text segments
    const lines = part.split('\n')
    return lines.map((line, j) => (
      <React.Fragment key={`${i}-${j}`}>
        {line}
        {j < lines.length - 1 && <br />}
      </React.Fragment>
    ))
  })
}

// ── Sub-components ────────────────────────────────────────────────────────────

function RegimeBadge({ regime }) {
  const label = regime || 'UNKNOWN'
  const color = REGIME_COLORS[label] || '#6B7280'
  return (
    <span
      className="text-xs font-bold px-2 py-0.5 rounded uppercase tracking-wider"
      style={{
        background: `${color}18`,
        color,
        border: `1px solid ${color}44`,
      }}
    >
      {label}
    </span>
  )
}

function BestRunCard({ best }) {
  if (!best) return null
  const pf = best.profit_factor != null ? best.profit_factor.toFixed(2) : '—'
  const wr = best.win_rate      != null ? (best.win_rate * 100).toFixed(1) + '%' : '—'
  const rt = best.total_return_pct != null ? (best.total_return_pct * 100).toFixed(1) + '%' : '—'
  return (
    <div
      className="rounded p-2 flex flex-col gap-1 text-xs"
      style={{
        background: 'rgba(0,255,136,0.05)',
        border: '1px solid rgba(0,255,136,0.2)',
      }}
    >
      <div className="flex justify-between items-center mb-0.5">
        <span className="uppercase tracking-widest font-bold" style={{ color: '#00FF88', fontSize: '0.65rem' }}>
          Best Run
        </span>
        {best.run_id && (
          <span className="font-mono muted" style={{ fontSize: '0.65rem' }}>#{best.run_id}</span>
        )}
      </div>
      <div className="flex justify-between">
        <span className="muted">PF</span>
        <span className="font-mono font-bold" style={{ color: '#00FF88' }}>{pf}</span>
      </div>
      <div className="flex justify-between">
        <span className="muted">Win Rate</span>
        <span className="font-mono font-bold" style={{ color: '#00FF88' }}>{wr}</span>
      </div>
      <div className="flex justify-between">
        <span className="muted">Return</span>
        <span className="font-mono font-bold" style={{ color: '#00FF88' }}>{rt}</span>
      </div>
    </div>
  )
}

function UserMessage({ message }) {
  return (
    <div className="flex justify-end">
      <div
        className="max-w-xs rounded px-3 py-2 text-xs leading-relaxed"
        style={{
          background: 'rgba(0,217,255,0.08)',
          border: '1px solid rgba(0,217,255,0.2)',
          color: '#E8EAED',
          wordBreak: 'break-word',
        }}
      >
        {message.content}
      </div>
    </div>
  )
}

function AssistantMessage({ message, isStreaming }) {
  return (
    <div className="flex gap-2 items-start">
      {/* Avatar */}
      <div
        className="flex-shrink-0 w-6 h-6 rounded flex items-center justify-center text-xs font-bold"
        style={{
          background: 'rgba(0,255,136,0.1)',
          border: '1px solid rgba(0,255,136,0.3)',
          color: '#00FF88',
        }}
      >
        AI
      </div>
      <div
        className="flex-1 min-w-0 rounded px-3 py-2 text-xs leading-relaxed"
        style={{
          background: 'rgba(0,255,136,0.05)',
          border: '1px solid rgba(0,255,136,0.15)',
          color: '#E8EAED',
          wordBreak: 'break-word',
        }}
      >
        {/* AI label */}
        <div
          className="text-xs font-bold uppercase tracking-widest mb-1.5"
          style={{
            color: '#00FF88',
            animation: isStreaming ? 'glow-pulse 1s ease-in-out infinite' : 'none',
          }}
        >
          {isStreaming ? '⟳ THE LAB AI' : 'THE LAB AI'}
        </div>
        <div>
          {message.content
            ? renderContent(message.content)
            : isStreaming
              ? <span className="muted" style={{ animation: 'glow-pulse 1s ease-in-out infinite', display: 'inline-block' }}>▌</span>
              : null}
        </div>
      </div>
    </div>
  )
}

// ── Main component ────────────────────────────────────────────────────────────

export default function TheLabChat({ currentParams, labRuns, get }) {
  const [messages, setMessages]     = useState([])
  const [inputText, setInputText]   = useState('')
  const [isStreaming, setIsStreaming] = useState(false)
  const [stats, setStats]           = useState(null)

  const messagesEndRef = useRef(null)
  const textareaRef    = useRef(null)
  const abortRef       = useRef(null)

  // ── Fetch stats on mount ──────────────────────────────────────────────────

  useEffect(() => {
    async function fetchStats() {
      const data = await get('/api/lab/stats', { ttl: 0 })
      if (data) setStats(data)
    }
    fetchStats()
  }, [get])

  // ── Auto-scroll to bottom ─────────────────────────────────────────────────

  useEffect(() => {
    messagesEndRef.current?.scrollIntoView({ behavior: 'smooth' })
  }, [messages])

  // ── Streaming send ────────────────────────────────────────────────────────

  const sendQuestion = useCallback(async (question) => {
    if (!question.trim() || isStreaming) return

    // Abort any ongoing stream
    abortRef.current?.abort()
    abortRef.current = new AbortController()

    // Add user message
    const userMsg = { role: 'user', content: question, id: nextId() }
    // Add empty assistant placeholder
    const assistantId = nextId()
    const assistantMsg = { role: 'assistant', content: '', id: assistantId }

    setMessages(prev => [...prev, userMsg, assistantMsg])
    setIsStreaming(true)

    try {
      const response = await fetch('/api/lab/chat', {
        method: 'POST',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify({ question, current_params: currentParams }),
        signal: abortRef.current.signal,
      })

      if (!response.ok) {
        const errText = await response.text().catch(() => 'Unknown error')
        setMessages(prev =>
          prev.map(m =>
            m.id === assistantId
              ? { ...m, content: `Error: ${response.status} — ${errText}` }
              : m
          )
        )
        setIsStreaming(false)
        return
      }

      const reader  = response.body.getReader()
      const decoder = new TextDecoder()

      // eslint-disable-next-line no-constant-condition
      while (true) {
        const { done, value } = await reader.read()
        if (done) break

        const chunk = decoder.decode(value, { stream: true })
        const lines = chunk.split('\n')

        for (const line of lines) {
          const trimmed = line.trim()
          if (!trimmed.startsWith('data:')) continue

          const payload = trimmed.slice(5).trim()
          if (payload === '[DONE]') {
            // Stream complete
            setIsStreaming(false)
            return
          }

          try {
            const parsed = JSON.parse(payload)
            if (parsed.text !== undefined) {
              setMessages(prev =>
                prev.map(m =>
                  m.id === assistantId
                    ? { ...m, content: m.content + parsed.text }
                    : m
                )
              )
            }
          } catch {
            // Malformed JSON line — skip
          }
        }
      }
    } catch (err) {
      if (err.name !== 'AbortError') {
        setMessages(prev =>
          prev.map(m =>
            m.id === assistantId
              ? { ...m, content: `Stream error: ${err.message}` }
              : m
          )
        )
      }
    } finally {
      setIsStreaming(false)
    }
  }, [currentParams, isStreaming])

  // ── Input handling ────────────────────────────────────────────────────────

  const handleSend = useCallback(() => {
    const q = inputText.trim()
    if (!q) return
    setInputText('')
    sendQuestion(q)
  }, [inputText, sendQuestion])

  const handleKeyDown = useCallback((e) => {
    if (e.key === 'Enter' && !e.shiftKey) {
      e.preventDefault()
      handleSend()
    }
  }, [handleSend])

  const handleQuickPrompt = useCallback((prompt) => {
    if (isStreaming) return
    sendQuestion(prompt)
  }, [isStreaming, sendQuestion])

  // ── Derived ───────────────────────────────────────────────────────────────

  const aiAvailable  = stats?.ai_available ?? false
  const regime       = stats?.market_regime ?? null
  const totalBt      = stats?.total_backtests ?? 0
  const combosTested = stats?.combos_tested ?? 0
  const bestRun      = stats?.best_run ?? null

  // ── Render ────────────────────────────────────────────────────────────────

  return (
    <div
      className="flex h-full min-h-0"
      style={{ background: '#0A0E27', color: '#E8EAED' }}
    >

      {/* ── Left sidebar ───────────────────────────────────────────────────── */}
      <aside
        className="flex-shrink-0 flex flex-col gap-3 p-3 border-r overflow-y-auto"
        style={{
          width: 200,
          borderColor: '#1E2A4A',
          background: '#0A0E27',
        }}
      >

        {/* Header */}
        <div className="flex items-center gap-2">
          <span
            className={`dot ${aiAvailable ? 'dot-green' : 'dot-gray'}`}
            style={{ flexShrink: 0 }}
          />
          <span
            className="text-xs font-bold uppercase tracking-widest"
            style={{ color: aiAvailable ? '#00FF88' : '#6B7280' }}
          >
            THE LAB AI
          </span>
        </div>

        {/* Market regime */}
        {regime && (
          <div className="flex flex-col gap-1">
            <span className="text-xs muted uppercase tracking-wider" style={{ fontSize: '0.6rem' }}>Market</span>
            <RegimeBadge regime={regime} />
          </div>
        )}

        {/* Stats */}
        <div
          className="flex flex-col gap-1 p-2 rounded text-xs"
          style={{ background: 'rgba(255,255,255,0.03)', border: '1px solid #1E2A4A' }}
        >
          <div className="flex justify-between">
            <span className="muted">Total Backtests</span>
            <span className="font-mono accent">{totalBt.toLocaleString()}</span>
          </div>
          <div className="flex justify-between">
            <span className="muted">Combos Tested</span>
            <span className="font-mono accent">{combosTested.toLocaleString()}</span>
          </div>
        </div>

        {/* Best run card */}
        <BestRunCard best={bestRun} />

        {/* Divider */}
        <div style={{ height: 1, background: '#1E2A4A' }} />

        {/* Quick prompts */}
        <div className="flex flex-col gap-1.5">
          <span
            className="text-xs muted uppercase tracking-widest"
            style={{ fontSize: '0.6rem' }}
          >
            Quick prompts
          </span>
          {QUICK_PROMPTS.map((prompt) => (
            <button
              key={prompt}
              onClick={() => handleQuickPrompt(prompt)}
              disabled={isStreaming}
              className="text-left rounded px-2 py-1.5 transition-all disabled:opacity-40"
              style={{
                background: 'rgba(255,255,255,0.04)',
                border: '1px solid rgba(255,255,255,0.08)',
                color: '#9CA3AF',
                fontSize: '0.65rem',
                lineHeight: '1.4',
                cursor: isStreaming ? 'not-allowed' : 'pointer',
              }}
              onMouseEnter={e => {
                if (!isStreaming) {
                  e.currentTarget.style.background = 'rgba(0,217,255,0.08)'
                  e.currentTarget.style.borderColor = 'rgba(0,217,255,0.25)'
                  e.currentTarget.style.color = '#00D9FF'
                }
              }}
              onMouseLeave={e => {
                e.currentTarget.style.background = 'rgba(255,255,255,0.04)'
                e.currentTarget.style.borderColor = 'rgba(255,255,255,0.08)'
                e.currentTarget.style.color = '#9CA3AF'
              }}
            >
              {prompt}
            </button>
          ))}
        </div>
      </aside>

      {/* ── Right chat area ─────────────────────────────────────────────────── */}
      <div className="flex-1 flex flex-col min-w-0 min-h-0">

        {/* AI not configured banner */}
        {!aiAvailable && stats !== null && (
          <div
            className="flex-shrink-0 flex items-center gap-2 px-4 py-2 text-xs"
            style={{
              background: 'rgba(255,0,85,0.08)',
              border: '1px solid rgba(255,0,85,0.25)',
              borderLeft: 'none',
              borderRight: 'none',
              color: '#FF0055',
            }}
          >
            <span>⚠</span>
            <span>AI not configured — add <code className="font-mono" style={{ background: 'rgba(255,0,85,0.15)', padding: '0 4px', borderRadius: 2 }}>OPENAI_API_KEY</code> to enable chat</span>
          </div>
        )}

        {/* Message history */}
        <div className="flex-1 overflow-y-auto p-4 flex flex-col gap-3 min-h-0">
          {messages.length === 0 && (
            <div
              className="flex flex-col items-center justify-center h-full gap-3"
              style={{ color: '#6B7280' }}
            >
              <div
                className="text-3xl font-bold uppercase tracking-widest"
                style={{ color: 'rgba(0,255,136,0.15)' }}
              >
                THE LAB AI
              </div>
              <div className="text-xs text-center" style={{ maxWidth: 280 }}>
                Ask about your backtest results, parameter tuning, or strategy performance. Use the quick prompts on the left to get started.
              </div>
            </div>
          )}

          {messages.map((msg, idx) => {
            const isLast = idx === messages.length - 1
            const isLastAssistant = msg.role === 'assistant' && isLast

            return msg.role === 'user'
              ? <UserMessage key={msg.id} message={msg} />
              : <AssistantMessage
                  key={msg.id}
                  message={msg}
                  isStreaming={isLastAssistant && isStreaming}
                />
          })}

          <div ref={messagesEndRef} />
        </div>

        {/* Input row */}
        <div
          className="flex-shrink-0 p-3 border-t"
          style={{ borderColor: '#1E2A4A' }}
        >
          <div className="flex gap-2 items-end">
            <textarea
              ref={textareaRef}
              value={inputText}
              onChange={e => setInputText(e.target.value)}
              onKeyDown={handleKeyDown}
              placeholder={
                !aiAvailable && stats !== null
                  ? 'AI not configured…'
                  : 'Ask about your lab results… (Enter to send, Shift+Enter for newline)'
              }
              disabled={isStreaming || (!aiAvailable && stats !== null)}
              rows={2}
              className="flex-1 rounded px-3 py-2 text-xs resize-none transition-all disabled:opacity-40"
              style={{
                background: 'rgba(255,255,255,0.05)',
                border: '1px solid #1E2A4A',
                color: '#E8EAED',
                outline: 'none',
                fontFamily: 'inherit',
                lineHeight: '1.5',
              }}
              onFocus={e => { e.currentTarget.style.borderColor = 'rgba(0,217,255,0.4)' }}
              onBlur={e => { e.currentTarget.style.borderColor = '#1E2A4A' }}
            />
            <button
              onClick={handleSend}
              disabled={isStreaming || !inputText.trim() || (!aiAvailable && stats !== null)}
              className="flex-shrink-0 px-4 py-2 rounded text-xs font-bold uppercase tracking-wider transition-all disabled:opacity-40"
              style={{
                background: 'rgba(0,217,255,0.15)',
                border: '1px solid #00D9FF',
                color: '#00D9FF',
                cursor: 'pointer',
                height: 56, // align with 2-row textarea
              }}
            >
              {isStreaming ? '⟳' : 'Send'}
            </button>
          </div>
          <div className="text-xs muted mt-1" style={{ fontSize: '0.6rem' }}>
            Enter to send · Shift+Enter for newline
          </div>
        </div>
      </div>
    </div>
  )
}
