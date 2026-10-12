const ESCAPES = { '&': '&amp;', '<': '&lt;', '>': '&gt;', '"': '&quot;' }

export function escape(text) {
  return String(text).replace(/[&<>"]/g, (c) => ESCAPES[c])
}

// Markdown subset: **bold**, *em*, _em_. Escape first, then substitute.
export function render(text) {
  let html = escape(text ?? '')
  html = html.replace(/\*\*(.+?)\*\*/g, '<strong>$1</strong>')
  html = html.replace(/(^|[^\w*])\*(?!\s)(.+?)(?<!\s)\*(?![\w*])/g, '$1<em>$2</em>')
  html = html.replace(/(^|[^\w_])_(?!\s)(.+?)(?<!\s)_(?![\w_])/g, '$1<em>$2</em>')
  return html
}
