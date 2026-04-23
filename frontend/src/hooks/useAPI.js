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

  const post = useCallback(async (path) => {
    try {
      const res = await axios.post(path)
      return res.data
    } catch (err) {
      setError(err.message)
      return null
    }
  }, [])

  return { get, post, loading, error }
}
