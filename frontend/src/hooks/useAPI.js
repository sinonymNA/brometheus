import { useState, useCallback, useRef } from 'react'
import axios from 'axios'

const TTL = 5000
const cache = new Map()

export function useAPI() {
  const [loading, setLoading] = useState(false)
  const [error, setError]     = useState(null)
  const abortRef = useRef(null)

  const get = useCallback(async (path, options = {}) => {
    const cacheKey = path
    const cached = cache.get(cacheKey)
    if (cached && Date.now() - cached.ts < (options.ttl ?? TTL)) {
      return cached.data
    }

    abortRef.current?.abort()
    abortRef.current = new AbortController()

    setLoading(true)
    setError(null)
    try {
      const res = await axios.get(path, { signal: abortRef.current.signal })
      cache.set(cacheKey, { data: res.data, ts: Date.now() })
      setLoading(false)
      return res.data
    } catch (err) {
      if (!axios.isCancel(err)) {
        setError(err.message)
        setLoading(false)
      }
      return null
    }
  }, [])

  const post = useCallback(async (path, data = {}) => {
    setLoading(true)
    setError(null)
    try {
      console.log('[API POST]', path, 'body:', JSON.stringify(data, null, 2))
      const res = await axios.post(path, data, {
        headers: { 'Content-Type': 'application/json' },
      })
      console.log('[API POST] response:', res.data)
      setLoading(false)
      return res.data
    } catch (err) {
      if (!axios.isCancel(err)) {
        console.error('[API POST] error:', err.response?.data || err.message)
        setError(err.message)
        setLoading(false)
      }
      return null
    }
  }, [])

  return { get, post, loading, error }
}
