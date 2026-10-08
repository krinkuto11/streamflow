import { describe, expect, it } from 'vitest'
import { ConditionalStatusCache } from './conditional-status.js'

const request = (cache, extra = {}) => cache.prepare({ url: '/stream-checker/status', headers: {}, ...extra })
const response = (config, data, etag = '"one"') => ({ config, data, headers: { etag }, status: 200 })

describe('conditional status revalidation', () => {
  it('keeps polling and reuses the same payload only after a matching 304', () => {
    const cache = new ConditionalStatusCache()
    const data = { queue: { queued: 2 } }
    cache.accept(response(request(cache), data))
    const next = request(cache)
    expect(next.headers['If-None-Match']).toBe('"one"')
    expect(cache.accept({ config: next, status: 304 }).data).toBe(data)
  })

  it('rejects a 304 after a mutation or eviction and prevents old reads from repopulating', () => {
    const cache = new ConditionalStatusCache()
    const first = request(cache)
    cache.accept(response(first, { old: true }))
    const pending = request(cache)
    cache.prepare({ method: 'post', url: '/stream-checker/queue/clear' })
    expect(cache.accept({ config: pending, status: 304 })).toBeNull()
    cache.accept(response(first, { old: true }))
    expect(request(cache).headers['If-None-Match']).toBeUndefined()
  })

  it('supports older endpoints and separates auth contexts with bounded storage', () => {
    const cache = new ConditionalStatusCache(1)
    cache.accept(response(request(cache), { value: 1 }))
    const auth = request(cache, { headers: { Authorization: 'Bearer fixture' } })
    expect(auth.headers['If-None-Match']).toBeUndefined()
    cache.accept(response(auth, { value: 2 }, '"two"'))
    expect(request(cache).headers['If-None-Match']).toBeUndefined()
    const unsupported = cache.prepare({ url: '/channels', headers: {} })
    expect(unsupported._statusCache).toBeUndefined()
    const older = request(new ConditionalStatusCache())
    expect(new ConditionalStatusCache().accept(response(older, {}, null)).status).toBe(200)
  })
})
