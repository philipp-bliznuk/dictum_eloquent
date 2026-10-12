import { escape } from './markup.js'

// Merriam-Webster marks the voiced "th" of "the" by tying the letters with U+035F
// (combining double macron below). Most fonts draw that mark badly, so it becomes an
// underlined "th" instead.
const VOICED_TH = /t\u035fh/g

export function renderPron(text) {
  return escape(text ?? '').replace(VOICED_TH, '<span class="voiced">th</span>')
}
