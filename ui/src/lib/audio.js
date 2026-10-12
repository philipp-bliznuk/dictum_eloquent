const SYNTH = typeof window !== 'undefined' ? window.speechSynthesis : null

export function canSpeak() {
  return Boolean(SYNTH)
}

export function play({ audioPath, text }) {
  if (audioPath) {
    const audio = new Audio(audioPath)
    audio.play().catch(() => speak(text))
    return
  }
  speak(text)
}

function speak(text) {
  if (!SYNTH || !text) return
  SYNTH.cancel()
  const utterance = new SpeechSynthesisUtterance(text)
  utterance.lang = 'en-US'
  utterance.rate = 0.9
  SYNTH.speak(utterance)
}
