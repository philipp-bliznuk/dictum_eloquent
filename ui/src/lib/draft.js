// Draft shape used by WordForm; pure helpers so they can be unit tested.
//
// A draft mirrors the v2 API: a word owns spellings, one lexeme per part of speech (each with its
// own pronunciation, audio and definitions) and loose examples that belong to no definition.
// Lexemes and definitions carry a client-only `uid` so async work (example generation, AI drafting)
// finds its target again after the user reorders or deletes rows. `toPayload` never sends it.

const key = (text) => text.trim().normalize('NFKC').toLowerCase()

export const posKey = (text) => text.trim().split(/\s+/).join(' ').toLowerCase()

let nextUid = 1

const newUid = () => nextUid++

const hasText = (item) => Boolean(item.text.trim())

const hasExample = (d) => d.examples.some(hasText)

const exampleCount = (d) => d.examples.filter(hasText).length

const hasContent = (d) => hasText(d) || hasExample(d)

/** How many examples every definition should end up with. */
export const TARGET_EXAMPLES = 2

/** Upper-case the first letter only: `galore` -> `Galore`, `'twixt` -> `'Twixt`, `élan` -> `Élan`. */
export const capitalize = (text) => text.replace(/\p{L}/u, (c) => c.toUpperCase())

const lexemeHasContent = (lx) =>
  lx.definitions.some(hasContent) || Boolean(lx.pronunciation.trim()) || Boolean(lx.audio_path)

export const blankExample = () => ({ text: '', source: 'user' })

export const blankDefinition = () => ({ uid: newUid(), text: '', source: 'user', examples: [] })

export const blankLexeme = (partOfSpeech = '') => ({
  uid: newUid(),
  part_of_speech: partOfSpeech,
  pronunciation: '',
  audio_path: null,
  definitions: [blankDefinition()],
})

export function emptyDraft(text = '') {
  return {
    spellings: [{ text, kind: 'primary' }],
    status: 'inbox',
    notes: '',
    lexemes: [blankLexeme('noun')],
    examples: [],
  }
}

const exampleFrom = (e) => ({ text: e.text, source: e.source })

export function fromWord(w) {
  return {
    spellings: w.spellings.map((s) => ({ text: s.text, kind: s.kind })),
    status: w.status,
    notes: w.notes ?? '',
    lexemes: w.lexemes.map((lx) => ({
      uid: newUid(),
      part_of_speech: lx.part_of_speech,
      pronunciation: lx.pronunciation ?? '',
      audio_path: lx.audio_path ?? null,
      definitions: lx.definitions.map((d) => ({
        uid: newUid(),
        text: d.text,
        source: d.source,
        examples: d.examples.map(exampleFrom),
      })),
    })),
    examples: w.examples.map(exampleFrom),
  }
}

const trimmedExamples = (examples) =>
  examples.filter(hasText).map((e) => ({ text: e.text.trim(), source: e.source }))

/** Lexemes worth sending: a part of speech, or anything typed into them. */
const keptLexemes = (draft) => draft.lexemes.filter((lx) => lx.part_of_speech.trim() || lexemeHasContent(lx))

export function toPayload(draft) {
  return {
    notes: draft.notes.trim() || null,
    status: draft.status,
    spellings: draft.spellings.filter(hasText).map((s) => ({ text: s.text.trim(), kind: s.kind })),
    lexemes: keptLexemes(draft).map((lx) => ({
      part_of_speech: lx.part_of_speech.trim(),
      pronunciation: lx.pronunciation.trim() || null,
      audio_path: lx.audio_path || null,
      definitions: lx.definitions
        .filter(hasText)
        .map((d) => ({ text: d.text.trim(), source: d.source, examples: trimmedExamples(d.examples) })),
    })),
    examples: trimmedExamples(draft.examples),
  }
}

/** Dictionary headwords are capitalised, so a lookup capitalises what the user typed and every variant. */
function mergeSpellings(current, entries) {
  const spellings = current.filter(hasText).map((s) => ({ text: capitalize(s.text.trim()), kind: s.kind }))
  const seen = new Set(spellings.map((s) => key(s.text)))
  for (const entry of entries) {
    for (const s of entry.spellings) {
      if (seen.has(key(s.text))) continue
      seen.add(key(s.text))
      spellings.push({ text: capitalize(s.text), kind: spellings.length ? 'variant' : 'primary' })
    }
  }
  return spellings.length ? spellings : [{ text: '', kind: 'primary' }]
}

const definitionFrom = (d) => ({
  uid: newUid(),
  text: d.text,
  source: d.source,
  examples: d.examples.map(exampleFrom),
})

/** Append an entry's definitions to a lexeme, filling pronunciation and audio where empty. */
function absorb(lexeme, entry) {
  return {
    ...lexeme,
    pronunciation: lexeme.pronunciation || entry.pronunciation || '',
    audio_path: lexeme.audio_path || entry.audio_path || null,
    definitions: [...lexeme.definitions.filter(hasContent), ...entry.definitions.map(definitionFrom)],
  }
}

/**
 * Fold a `/api/lookup` (or `/api/generate/definitions`) response into a draft. Each entry lands in
 * the lexeme with the same part of speech or opens a new one; untouched placeholder lexemes are
 * dropped, spellings are deduplicated and scalar fields are filled only where empty.
 */
export function mergeLookup(draft, result) {
  let lexemes = draft.lexemes.filter(lexemeHasContent)
  for (const entry of result.entries) {
    const pos = posKey(entry.part_of_speech)
    const index = lexemes.findIndex((lx) => posKey(lx.part_of_speech) === pos)
    if (index >= 0) {
      lexemes = lexemes.map((lx, i) => (i === index ? absorb(lx, entry) : lx))
    } else {
      lexemes = [...lexemes, absorb({ ...blankLexeme(entry.part_of_speech), definitions: [] }, entry)]
    }
  }
  return {
    ...draft,
    spellings: mergeSpellings(draft.spellings, result.entries),
    lexemes: lexemes.length ? lexemes : [blankLexeme('noun')],
  }
}

/**
 * Append AI-drafted definitions (any entry of the response) to one lexeme, keeping what the user
 * typed there. Unknown uid leaves the draft unchanged.
 */
export function mergeDefinitions(draft, lexemeUid, entries) {
  if (!draft.lexemes.some((lx) => lx.uid === lexemeUid)) return draft
  const merged = { spellings: [], part_of_speech: '', pronunciation: '', audio_path: null, definitions: [] }
  for (const entry of entries) {
    merged.pronunciation ||= entry.pronunciation ?? ''
    merged.audio_path ||= entry.audio_path ?? null
    merged.definitions.push(...entry.definitions)
  }
  return {
    ...draft,
    lexemes: draft.lexemes.map((lx) => (lx.uid === lexemeUid ? absorb(lx, merged) : lx)),
  }
}

/** One-line summary of which providers answered, e.g. `mw: not found, free_dict: error (timeout)`. */
export function describeAttempts(attempts) {
  return attempts
    .map((a) => `${a.provider}: ${a.outcome.replace('_', ' ')}${a.detail ? ` (${a.detail})` : ''}`)
    .join(', ')
}

export const allDefinitions = (draft) => draft.lexemes.flatMap((lx) => lx.definitions)

/** Uids of definitions that have text but fewer than `TARGET_EXAMPLES` non-blank examples. */
export function missingExamples(draft) {
  return allDefinitions(draft)
    .filter((d) => hasText(d) && exampleCount(d) < TARGET_EXAMPLES)
    .map((d) => d.uid)
}

/**
 * Bodies for `/api/generate/examples`, one per lexeme that owns some of the given definitions
 * (the endpoint takes a single part of speech). Each request echoes the uids it covers, in draft
 * order, so `mergeExamples` can map the response back even if the user edits the list meanwhile.
 */
export function examplesRequests(draft, uids, perDefinition = 2) {
  const word = draft.spellings[0]?.text.trim() ?? ''
  if (!word) return []
  const wanted = new Set(uids)
  const requests = []
  for (const lx of draft.lexemes) {
    const selected = lx.definitions.filter((d) => wanted.has(d.uid) && hasText(d))
    if (!selected.length) continue
    requests.push({
      uids: selected.map((d) => d.uid),
      body: {
        word,
        part_of_speech: lx.part_of_speech.trim() || 'other',
        definitions: selected.map((d) => d.text.trim()),
        per_definition: perDefinition,
      },
    })
  }
  return requests
}

/**
 * Append generated example groups to the definitions they were requested for, skipping duplicates.
 * With `target`, a definition is only topped up to that many non-blank examples.
 */
export function mergeExamples(draft, uids, groups, { target = Infinity } = {}) {
  const byUid = new Map(uids.map((uid, i) => [uid, groups[i] ?? []]))
  const lexemes = draft.lexemes.map((lx) => ({
    ...lx,
    definitions: lx.definitions.map((d) => {
      const group = byUid.get(d.uid)
      if (!group) return d
      const seen = new Set(d.examples.map((e) => key(e.text)))
      const room = Math.max(0, target - exampleCount(d))
      const fresh = group.filter((e) => e.text.trim() && !seen.has(key(e.text))).slice(0, room)
      return fresh.length ? { ...d, examples: [...d.examples, ...fresh.map(exampleFrom)] } : d
    }),
  }))
  return { ...draft, lexemes }
}

function moveWithin(items, uid, delta) {
  const from = items.findIndex((item) => item.uid === uid)
  const to = from + delta
  if (from < 0 || to < 0 || to >= items.length) return null
  const moved = [...items]
  const [item] = moved.splice(from, 1)
  moved.splice(to, 0, item)
  return moved
}

/** Move the lexeme `uid` by `delta` positions (negative = up); no-op at the edges. */
export function moveLexeme(draft, uid, delta) {
  const lexemes = moveWithin(draft.lexemes, uid, delta)
  return lexemes ? { ...draft, lexemes } : draft
}

/** Move the definition `uid` by `delta` positions inside its lexeme; no-op at the edges. */
export function moveDefinition(draft, uid, delta) {
  const lexemes = draft.lexemes.map((lx) => {
    const definitions = moveWithin(lx.definitions, uid, delta)
    return definitions ? { ...lx, definitions } : lx
  })
  return { ...draft, lexemes }
}

/** Move loose example `index` under definition `uid`; unknown uid leaves the draft unchanged. */
export function attachExample(draft, index, uid) {
  const example = draft.examples[index]
  if (!example || !allDefinitions(draft).some((d) => d.uid === uid)) return draft
  return {
    ...draft,
    examples: draft.examples.filter((_, i) => i !== index),
    lexemes: draft.lexemes.map((lx) => ({
      ...lx,
      definitions: lx.definitions.map((d) =>
        d.uid === uid ? { ...d, examples: [...d.examples, example] } : d,
      ),
    })),
  }
}
