const STATUS_PATH = /^\/(?:automation\/status|stream-checker\/(?:status|progress)|teamarr-preflight\/status|shadow-blank-monitor\/status|stream-sessions(?:\/[^/]+)?|viewer-activity\/status|job-arbiter\/status|scheduling\/(?:processor|epg-refresh|udi-refresh)\/status|dispatcharr\/initialization-status|udi\/(?:status|stats))$/

export class ConditionalStatusCache {
  constructor(limit = 32) {
    this.limit = limit
    this.entries = new Map()
    this.generation = 0
  }

  clear() {
    this.generation += 1
    this.entries.clear()
  }

  prepare(config) {
    const method = (config.method || 'get').toLowerCase()
    if (!['get', 'head', 'options'].includes(method)) this.clear()
    const path = String(config.url || '').replace(/^\/api/, '').split('?')[0]
    if (method !== 'get' || !STATUS_PATH.test(path)) return config
    const auth = config.headers?.get?.('Authorization') || config.headers?.Authorization || ''
    const key = JSON.stringify([config.baseURL, config.url, config.params || null, auth])
    config._statusCache = { key, generation: this.generation }
    const entry = this.entries.get(key)
    if (entry && !config._statusUnconditional) {
      config.headers = config.headers || {}
      config.headers['If-None-Match'] = entry.etag
      config._statusCache.etag = entry.etag
    }
    const validate = config.validateStatus
    config.validateStatus = status => status === 304 || (validate ? validate(status) : status >= 200 && status < 300)
    return config
  }

  accept(response) {
    const request = response.config?._statusCache
    if (!request) return response
    if (request.generation !== this.generation) return null
    const entry = this.entries.get(request.key)
    if (response.status === 304) {
      if (request.generation !== this.generation || !entry || entry.etag !== request.etag) return null
      return { ...response, status: 200, data: entry.data }
    }
    const etag = response.headers?.get?.('etag') || response.headers?.etag
    if (response.status === 200 && etag && request.generation === this.generation) {
      this.entries.delete(request.key)
      this.entries.set(request.key, { etag, data: response.data })
      while (this.entries.size > this.limit) this.entries.delete(this.entries.keys().next().value)
    }
    return response
  }
}
