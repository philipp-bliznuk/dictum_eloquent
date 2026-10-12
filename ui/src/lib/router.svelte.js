const ROUTES = [
  { name: 'home', pattern: /^\/$/ },
  { name: 'new', pattern: /^\/words\/new$/ },
  { name: 'edit', pattern: /^\/words\/(\d+)\/edit$/, params: ['id'] },
  { name: 'word', pattern: /^\/words\/(\d+)$/, params: ['id'] },
  { name: 'transfer', pattern: /^\/transfer$/ },
]

function match(pathname) {
  for (const route of ROUTES) {
    const m = route.pattern.exec(pathname)
    if (!m) continue
    const params = {}
    ;(route.params ?? []).forEach((key, i) => (params[key] = m[i + 1]))
    return { name: route.name, params }
  }
  return { name: 'notfound', params: {} }
}

function read() {
  const url = new URL(window.location.href)
  return { ...match(url.pathname), path: url.pathname, query: url.searchParams }
}

export const router = $state(read())

function sync() {
  Object.assign(router, read())
}

export function navigate(path, { replace = false } = {}) {
  if (replace) window.history.replaceState(null, '', path)
  else window.history.pushState(null, '', path)
  sync()
}

export function link(node) {
  const onClick = (event) => {
    if (event.defaultPrevented || event.button !== 0 || event.metaKey || event.ctrlKey) return
    const href = node.getAttribute('href')
    if (!href || href.startsWith('http') || node.hasAttribute('download')) return
    event.preventDefault()
    navigate(href)
  }
  node.addEventListener('click', onClick)
  return { destroy: () => node.removeEventListener('click', onClick) }
}

window.addEventListener('popstate', sync)
