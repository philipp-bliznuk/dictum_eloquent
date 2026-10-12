const BASE = '/api'

export class ApiError extends Error {
  constructor(status, detail, headers) {
    super(detail || `HTTP ${status}`)
    this.status = status
    this.detail = detail
    this.location = headers?.get('location') ?? null
  }
}

async function request(method, path, { body, query } = {}) {
  const url = new URL(BASE + path, window.location.origin)
  for (const [key, value] of Object.entries(query ?? {})) {
    if (value !== undefined && value !== null && value !== '') url.searchParams.set(key, value)
  }
  const init = { method, headers: {} }
  if (body !== undefined) {
    init.headers['content-type'] = 'application/json'
    init.body = JSON.stringify(body)
  }
  const res = await fetch(url, init)
  if (res.status === 204) return null
  const isJson = res.headers.get('content-type')?.includes('application/json')
  const data = isJson ? await res.json() : await res.text()
  if (!res.ok) {
    const detail = typeof data === 'object' ? formatDetail(data.detail) : data
    throw new ApiError(res.status, detail, res.headers)
  }
  return data
}

function formatDetail(detail) {
  if (typeof detail === 'string') return detail
  if (Array.isArray(detail)) {
    return detail.map((e) => `${(e.loc ?? []).slice(1).join('.')}: ${e.msg}`).join('; ')
  }
  return JSON.stringify(detail)
}

export const api = {
  health: () => request('GET', '/health'),
  stats: () => request('GET', '/words/stats'),
  listWords: (query) => request('GET', '/words', { query }),
  getWord: (id) => request('GET', `/words/${id}`),
  bySpelling: (text) => request('GET', '/words/by-spelling', { query: { text } }),
  createWord: (body) => request('POST', '/words', { body }),
  replaceWord: (id, body) => request('PUT', `/words/${id}`, { body }),
  patchWord: (id, body) => request('PATCH', `/words/${id}`, { body }),
  deleteWord: (id) => request('DELETE', `/words/${id}`),
  search: (q, limit = 20) => request('GET', '/search', { query: { q, limit } }),
  lookup: (text) => request('POST', '/lookup', { body: { text } }),
  fetchAudio: (id) => request('POST', `/words/${id}/audio`),
  generateExamples: (body) => request('POST', '/generate/examples', { body }),
  generateDefinitions: (body) => request('POST', '/generate/definitions', { body }),
  importWords: (body, onConflict) => request('POST', '/import', { body, query: { on_conflict: onConflict } }),
  exportJsonUrl: () => `${BASE}/export.json`,
  exportMdUrl: () => `${BASE}/export.md`,
}
