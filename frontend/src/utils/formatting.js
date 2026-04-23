export function formatMoney(num, decimals = 2) {
  if (num == null || isNaN(num)) return '—'
  const abs = Math.abs(num)
  const sign = num < 0 ? '-' : ''
  return `${sign}$${abs.toLocaleString('en-US', { minimumFractionDigits: decimals, maximumFractionDigits: decimals })}`
}

export function formatPercent(num, decimals = 2) {
  if (num == null || isNaN(num)) return '—'
  const sign = num > 0 ? '+' : ''
  return `${sign}${(num * 100).toFixed(decimals)}%`
}

export function formatPct(num, decimals = 2) {
  if (num == null || isNaN(num)) return '—'
  const sign = num > 0 ? '+' : ''
  return `${sign}${Number(num).toFixed(decimals)}%`
}

export function formatTime(ts) {
  if (!ts) return '—'
  const diff = (Date.now() - new Date(ts).getTime()) / 1000
  if (diff < 5)   return 'just now'
  if (diff < 60)  return `${Math.floor(diff)}s ago`
  if (diff < 3600) return `${Math.floor(diff / 60)}m ago`
  if (diff < 86400) return `${Math.floor(diff / 3600)}h ago`
  return new Date(ts).toLocaleDateString()
}

export function formatGreek(val, decimals = 4) {
  if (val == null || isNaN(val)) return '—'
  return Number(val).toFixed(decimals)
}

export function getColorClass(pnl) {
  if (pnl == null) return 'neu'
  if (pnl > 0)  return 'pos'
  if (pnl < 0)  return 'neg'
  return 'neu'
}

export function formatDuration(minutes) {
  if (minutes == null) return '—'
  if (minutes < 60) return `${Math.round(minutes)}m`
  return `${Math.floor(minutes / 60)}h ${Math.round(minutes % 60)}m`
}

export function formatNumber(num, decimals = 2) {
  if (num == null || isNaN(num)) return '—'
  return Number(num).toLocaleString('en-US', { minimumFractionDigits: decimals, maximumFractionDigits: decimals })
}
