import { describe, expect, test } from 'bun:test'
import {
  TARGET_EXAMPLES,
  allDefinitions,
  attachExample,
  blankDefinition,
  blankLexeme,
  capitalize,
  describeAttempts,
  emptyDraft,
  examplesRequests,
  fromWord,
  mergeDefinitions,
  mergeExamples,
  mergeLookup,
  missingExamples,
  moveDefinition,
  moveLexeme,
  posKey,
  toPayload,
} from './draft.js'

const ex = (text, source = 'user') => ({ text, source })

const def = (text, overrides = {}) => ({ ...blankDefinition(), text, ...overrides })

const lex = (partOfSpeech, definitions, overrides = {}) => ({
  ...blankLexeme(partOfSpeech),
  definitions,
  ...overrides,
})

const entry = (overrides = {}) => ({
  spellings: [{ text: 'abandon', kind: 'primary' }],
  part_of_speech: 'verb',
  pronunciation: 'uh-BAN-dun',
  audio_path: '/audio/abando01.mp3',
  definitions: [
    { text: 'to give up completely', source: 'mw', examples: [ex('They *abandon* the ship.', 'mw')] },
    { text: 'to leave behind', source: 'mw', examples: [] },
  ],
  source: 'mw',
  ...overrides,
})

const lookup = (entries, extra = {}) => ({
  query: 'abandon',
  found: entries.length > 0,
  entries,
  suggestions: [],
  attempts: [],
  existing_word_id: null,
  ...extra,
})

/** Draft content without client uids, for structural comparison. */
const content = (draft) => ({
  ...draft,
  lexemes: draft.lexemes.map(({ uid, definitions, ...lx }) => ({
    ...lx,
    definitions: definitions.map(({ uid: _uid, ...d }) => d),
  })),
})

const word = {
  id: 7,
  status: 'learned',
  notes: 'stress shifts',
  created_at: '2026-10-10T10:00:00Z',
  updated_at: '2026-10-10T10:00:00Z',
  spellings: [
    { id: 1, text: 'Record', kind: 'primary' },
    { id: 2, text: 'Recorde', kind: 'variant' },
  ],
  lexemes: [
    {
      id: 3,
      position: 0,
      part_of_speech: 'noun',
      pronunciation: 'ˈre-kərd',
      audio_path: '/audio/record01.mp3',
      definitions: [
        {
          id: 10,
          position: 0,
          text: 'a written account',
          source: 'user',
          examples: [{ id: 20, position: 0, text: 'Keep a *record*.', source: 'ai' }],
        },
        { id: 11, position: 1, text: 'a disc', source: 'mw', examples: [] },
      ],
    },
    {
      id: 4,
      position: 1,
      part_of_speech: 'verb',
      pronunciation: 'ri-ˈkȯrd',
      audio_path: null,
      definitions: [{ id: 12, position: 0, text: 'to set down in writing', source: 'user', examples: [] }],
    },
  ],
  examples: [{ id: 21, position: 1, text: 'For the *record*, I disagree.', source: 'vault' }],
}

describe('posKey', () => {
  test('collapses whitespace and lowercases', () => {
    expect(posKey('  Noun  ')).toBe('noun')
    expect(posKey('Plural\t Noun')).toBe('plural noun')
  })
})

describe('capitalize', () => {
  test('upper-cases only the first letter, skipping leading punctuation', () => {
    expect(capitalize('galore')).toBe('Galore')
    expect(capitalize("'twixt")).toBe("'Twixt")
    expect(capitalize('élan')).toBe('Élan')
    expect(capitalize('put paid to')).toBe('Put paid to')
    expect(capitalize('Already')).toBe('Already')
    expect(capitalize('')).toBe('')
  })
})

describe('emptyDraft', () => {
  test('starts with one noun lexeme holding one blank definition', () => {
    const draft = emptyDraft('put paid to')
    expect(draft.spellings).toEqual([{ text: 'put paid to', kind: 'primary' }])
    expect(draft.status).toBe('inbox')
    expect(draft.lexemes).toHaveLength(1)
    expect(draft.lexemes[0].part_of_speech).toBe('noun')
    expect(draft.lexemes[0].definitions).toHaveLength(1)
    expect(draft.lexemes[0].uid).toBeGreaterThan(0)
    expect(draft.lexemes[0].definitions[0].uid).toBeGreaterThan(0)
    expect(draft.examples).toEqual([])
  })

  test('hands out distinct uids', () => {
    const a = emptyDraft()
    const b = emptyDraft()
    expect(a.lexemes[0].uid).not.toBe(b.lexemes[0].uid)
    expect(a.lexemes[0].definitions[0].uid).not.toBe(b.lexemes[0].definitions[0].uid)
  })
})

describe('fromWord / toPayload', () => {
  test('round trips a nested word', () => {
    const draft = fromWord(word)
    expect(draft.lexemes.map((lx) => lx.uid)).toHaveLength(2)
    expect(toPayload(draft)).toEqual({
      notes: 'stress shifts',
      status: 'learned',
      spellings: [
        { text: 'Record', kind: 'primary' },
        { text: 'Recorde', kind: 'variant' },
      ],
      lexemes: [
        {
          part_of_speech: 'noun',
          pronunciation: 'ˈre-kərd',
          audio_path: '/audio/record01.mp3',
          definitions: [
            { text: 'a written account', source: 'user', examples: [ex('Keep a *record*.', 'ai')] },
            { text: 'a disc', source: 'mw', examples: [] },
          ],
        },
        {
          part_of_speech: 'verb',
          pronunciation: 'ri-ˈkȯrd',
          audio_path: null,
          definitions: [{ text: 'to set down in writing', source: 'user', examples: [] }],
        },
      ],
      examples: [ex('For the *record*, I disagree.', 'vault')],
    })
  })

  test('drops blank rows, trims text and nulls empty scalars', () => {
    const draft = emptyDraft('  abandon ')
    draft.spellings.push({ text: '   ', kind: 'variant' })
    draft.lexemes[0].definitions[0].text = '  to give up  '
    draft.lexemes[0].definitions[0].examples = [ex('  They *abandon* ship. '), ex('   ')]
    draft.lexemes[0].definitions.push(def('   '))
    draft.lexemes.push(blankLexeme(''))
    draft.examples = [ex(' loose '), ex('')]
    expect(toPayload(draft)).toEqual({
      notes: null,
      status: 'inbox',
      spellings: [{ text: 'abandon', kind: 'primary' }],
      lexemes: [
        {
          part_of_speech: 'noun',
          pronunciation: null,
          audio_path: null,
          definitions: [{ text: 'to give up', source: 'user', examples: [ex('They *abandon* ship.')] }],
        },
      ],
      examples: [ex('loose')],
    })
  })

  test('keeps a lexeme that has content but no part of speech yet', () => {
    const draft = emptyDraft('abandon')
    draft.lexemes = [lex('', [def('a sense without a label')])]
    expect(toPayload(draft).lexemes).toEqual([
      {
        part_of_speech: '',
        pronunciation: null,
        audio_path: null,
        definitions: [{ text: 'a sense without a label', source: 'user', examples: [] }],
      },
    ])
  })
})

describe('mergeLookup', () => {
  test('replaces the blank placeholder lexeme and fills scalars', () => {
    const draft = emptyDraft('abandon')
    const merged = mergeLookup(draft, lookup([entry()]))
    expect(content(merged).lexemes).toEqual([
      {
        part_of_speech: 'verb',
        pronunciation: 'uh-BAN-dun',
        audio_path: '/audio/abando01.mp3',
        definitions: [
          { text: 'to give up completely', source: 'mw', examples: [ex('They *abandon* the ship.', 'mw')] },
          { text: 'to leave behind', source: 'mw', examples: [] },
        ],
      },
    ])
    expect(merged.lexemes[0].definitions.every((d) => d.uid > 0)).toBe(true)
  })

  test('lands each entry in the lexeme with the same part of speech, case-insensitively', () => {
    const draft = emptyDraft('abandon')
    const mine = def('my own verb sense')
    draft.lexemes = [lex(' Verb ', [mine], { uid: 500 })]
    const merged = mergeLookup(
      draft,
      lookup([entry(), entry({ part_of_speech: 'noun', pronunciation: null })]),
    )
    expect(merged.lexemes.map((lx) => lx.part_of_speech)).toEqual([' Verb ', 'noun'])
    expect(merged.lexemes[0].uid).toBe(500)
    expect(merged.lexemes[0].definitions[0]).toBe(mine)
    expect(merged.lexemes[0].definitions).toHaveLength(3)
    expect(merged.lexemes[1].pronunciation).toBe('')
    expect(merged.lexemes[1].definitions).toHaveLength(2)
  })

  test('capitalises user text and adds new spellings as capitalised variants', () => {
    const draft = emptyDraft('aggrandise')
    const merged = mergeLookup(
      draft,
      lookup([
        entry({
          spellings: [
            { text: 'aggrandize', kind: 'primary' },
            { text: 'aggrandise', kind: 'variant' },
          ],
        }),
      ]),
    )
    expect(merged.spellings).toEqual([
      { text: 'Aggrandise', kind: 'primary' },
      { text: 'Aggrandize', kind: 'variant' },
    ])
  })

  test('fills an empty headword from the first entry, capitalised', () => {
    const merged = mergeLookup(emptyDraft(''), lookup([entry()]))
    expect(merged.spellings).toEqual([{ text: 'Abandon', kind: 'primary' }])
  })

  test('does not fill pronunciation or audio the user already has', () => {
    const draft = emptyDraft('abandon')
    draft.lexemes = [lex('verb', [def('x')], { pronunciation: 'mine', audio_path: '/audio/mine.mp3' })]
    const merged = mergeLookup(draft, lookup([entry()]))
    expect(merged.lexemes[0].pronunciation).toBe('mine')
    expect(merged.lexemes[0].audio_path).toBe('/audio/mine.mp3')
  })

  test('not found leaves a usable draft with the headword capitalised', () => {
    const draft = emptyDraft('abandn')
    const merged = mergeLookup(draft, lookup([]))
    expect(merged.lexemes).toHaveLength(1)
    expect(merged.lexemes[0].part_of_speech).toBe('noun')
    expect(merged.lexemes[0].definitions).toHaveLength(1)
    expect(merged.spellings).toEqual([{ text: 'Abandn', kind: 'primary' }])
  })
})

describe('mergeDefinitions', () => {
  test('appends every entry definition to the chosen lexeme and fills scalars', () => {
    const draft = emptyDraft('nitpick')
    const target = lex('noun', [def('')], { uid: 42 })
    draft.lexemes = [lex('verb', [def('to find fault')]), target]
    const merged = mergeDefinitions(draft, 42, [
      entry({ part_of_speech: 'noun', pronunciation: '/ˈnɪtpɪk/', audio_path: null }),
    ])
    expect(merged.lexemes[0]).toBe(draft.lexemes[0])
    expect(content(merged).lexemes[1]).toEqual({
      part_of_speech: 'noun',
      pronunciation: '/ˈnɪtpɪk/',
      audio_path: null,
      definitions: [
        { text: 'to give up completely', source: 'mw', examples: [ex('They *abandon* the ship.', 'mw')] },
        { text: 'to leave behind', source: 'mw', examples: [] },
      ],
    })
  })

  test('unknown uid is a no-op', () => {
    const draft = emptyDraft('nitpick')
    expect(mergeDefinitions(draft, 999, [entry()])).toBe(draft)
  })
})

describe('describeAttempts', () => {
  test('formats outcomes and details', () => {
    expect(
      describeAttempts([
        { provider: 'mw', outcome: 'not_found', detail: null },
        { provider: 'free_dict', outcome: 'error', detail: 'timeout' },
      ]),
    ).toBe('mw: not found, free_dict: error (timeout)')
  })
})

describe('missingExamples / examplesRequests', () => {
  function sample() {
    const draft = emptyDraft('record')
    const n1 = def('a written account', { examples: [ex('Keep a *record*.'), ex('A *record* of events.')] })
    const n2 = def('a disc')
    const v1 = def('to set down', { examples: [ex('She *recorded* the call.')] })
    const v2 = def('')
    draft.lexemes = [lex('noun', [n1, n2]), lex('verb', [v1, v2])]
    return { draft, n1, n2, v1, v2 }
  }

  test(`missingExamples lists definitions with text but fewer than ${TARGET_EXAMPLES} examples`, () => {
    const { draft, n2, v1 } = sample()
    expect(TARGET_EXAMPLES).toBe(2)
    expect(missingExamples(draft)).toEqual([n2.uid, v1.uid])
  })

  test('blank examples do not count', () => {
    const { draft, n1 } = sample()
    n1.examples = [ex('Keep a *record*.'), ex('   ')]
    expect(missingExamples(draft)).toContain(n1.uid)
  })

  test('one request per lexeme with the lexeme part of speech', () => {
    const { draft, n1, n2, v1, v2 } = sample()
    const requests = examplesRequests(draft, [v1.uid, n2.uid, n1.uid, v2.uid, 999], 3)
    expect(requests).toEqual([
      {
        uids: [n1.uid, n2.uid],
        body: {
          word: 'record',
          part_of_speech: 'noun',
          definitions: ['a written account', 'a disc'],
          per_definition: 3,
        },
      },
      {
        uids: [v1.uid],
        body: { word: 'record', part_of_speech: 'verb', definitions: ['to set down'], per_definition: 3 },
      },
    ])
  })

  test('defaults to two examples and falls back to "other" for an unlabelled lexeme', () => {
    const { draft, v1 } = sample()
    draft.lexemes[1].part_of_speech = '  '
    const [request] = examplesRequests(draft, [v1.uid])
    expect(request.body.part_of_speech).toBe('other')
    expect(request.body.per_definition).toBe(2)
  })

  test('no headword or nothing selected gives no requests', () => {
    const { draft, n2 } = sample()
    expect(examplesRequests(draft, [])).toEqual([])
    draft.spellings[0].text = '  '
    expect(examplesRequests(draft, [n2.uid])).toEqual([])
  })
})

describe('mergeExamples', () => {
  test('appends by uid across lexemes, skips duplicates, leaves other rows untouched', () => {
    const draft = emptyDraft('record')
    const n1 = def('a written account', { examples: [ex('Keep a *record*.')] })
    const v1 = def('to set down')
    draft.lexemes = [lex('noun', [n1]), lex('verb', [v1])]
    const merged = mergeExamples(
      draft,
      [n1.uid, v1.uid],
      [
        [ex('keep a *RECORD*.', 'ai'), ex('A *record* of events.', 'ai')],
        [ex('  ', 'ai'), ex('She *recorded* the call.', 'ai')],
      ],
    )
    expect(merged.lexemes[0].definitions[0].examples).toEqual([
      ex('Keep a *record*.'),
      ex('A *record* of events.', 'ai'),
    ])
    expect(merged.lexemes[1].definitions[0].examples).toEqual([ex('She *recorded* the call.', 'ai')])
    expect(draft.lexemes[0].definitions[0].examples).toHaveLength(1)
  })

  test('survives reorder and deletion between request and response', () => {
    const draft = emptyDraft('record')
    const a = def('a')
    const b = def('b')
    const c = def('c')
    draft.lexemes = [lex('noun', [a, b, c])]
    const uids = [a.uid, b.uid, c.uid]
    draft.lexemes = [lex('noun', [c, a])]
    const merged = mergeExamples(draft, uids, [[ex('A.')], [ex('B.')], [ex('C.')]])
    expect(merged.lexemes[0].definitions.map((d) => d.examples)).toEqual([[ex('C.')], [ex('A.')]])
  })

  test('fewer groups than uids leaves the tail untouched', () => {
    const draft = emptyDraft('record')
    const a = def('a')
    const b = def('b')
    draft.lexemes = [lex('noun', [a, b])]
    const merged = mergeExamples(draft, [a.uid, b.uid], [[ex('A.')]])
    expect(merged.lexemes[0].definitions[1]).toBe(b)
  })

  test('target caps each definition at that many non-blank examples in total', () => {
    const draft = emptyDraft('record')
    const full = def('a', { examples: [ex('One.'), ex('Two.')] })
    const half = def('b', { examples: [ex('One.'), ex('  ')] })
    const empty = def('c')
    draft.lexemes = [lex('noun', [full, half, empty])]
    const groups = [
      [ex('X.', 'ai'), ex('Y.', 'ai')],
      [ex('X.', 'ai'), ex('Y.', 'ai')],
      [ex('X.', 'ai'), ex('Y.', 'ai'), ex('Z.', 'ai')],
    ]
    const merged = mergeExamples(draft, [full.uid, half.uid, empty.uid], groups, { target: TARGET_EXAMPLES })
    const [a, b, c] = merged.lexemes[0].definitions
    expect(a.examples).toEqual([ex('One.'), ex('Two.')])
    expect(b.examples).toEqual([ex('One.'), ex('  '), ex('X.', 'ai')])
    expect(c.examples).toEqual([ex('X.', 'ai'), ex('Y.', 'ai')])
  })

  test('without a target every fresh example is appended', () => {
    const draft = emptyDraft('record')
    const full = def('a', { examples: [ex('One.'), ex('Two.')] })
    draft.lexemes = [lex('noun', [full])]
    const merged = mergeExamples(draft, [full.uid], [[ex('X.', 'ai'), ex('Y.', 'ai')]])
    expect(merged.lexemes[0].definitions[0].examples).toHaveLength(4)
  })
})

describe('moveLexeme / moveDefinition', () => {
  test('moveLexeme swaps neighbours and ignores edges and unknown uids', () => {
    const draft = emptyDraft('record')
    const noun = lex('noun', [def('n')])
    const verb = lex('verb', [def('v')])
    draft.lexemes = [noun, verb]
    expect(moveLexeme(draft, verb.uid, -1).lexemes).toEqual([verb, noun])
    expect(moveLexeme(draft, noun.uid, -1)).toBe(draft)
    expect(moveLexeme(draft, verb.uid, 1)).toBe(draft)
    expect(moveLexeme(draft, 999, 1)).toBe(draft)
    expect(draft.lexemes).toEqual([noun, verb])
  })

  test('moveDefinition stays inside its lexeme', () => {
    const draft = emptyDraft('record')
    const a = def('a')
    const b = def('b')
    const v = def('v')
    draft.lexemes = [lex('noun', [a, b]), lex('verb', [v])]
    const moved = moveDefinition(draft, b.uid, -1)
    expect(moved.lexemes[0].definitions).toEqual([b, a])
    expect(moved.lexemes[1]).toBe(draft.lexemes[1])
    expect(moveDefinition(draft, v.uid, 1).lexemes[1].definitions).toEqual([v])
    expect(moveDefinition(draft, a.uid, -1).lexemes[0].definitions).toEqual([a, b])
  })
})

describe('attachExample / allDefinitions', () => {
  test('moves a loose example under a definition in any lexeme', () => {
    const draft = emptyDraft('record')
    const n = def('n')
    const v = def('v')
    draft.lexemes = [lex('noun', [n]), lex('verb', [v])]
    draft.examples = [ex('first', 'vault'), ex('second', 'vault')]
    const attached = attachExample(draft, 1, v.uid)
    expect(attached.examples).toEqual([ex('first', 'vault')])
    expect(attached.lexemes[1].definitions[0].examples).toEqual([ex('second', 'vault')])
    expect(attached.lexemes[0].definitions[0].examples).toEqual([])
    expect(draft.examples).toHaveLength(2)
  })

  test('unknown uid or index is a no-op', () => {
    const draft = emptyDraft('record')
    draft.examples = [ex('loose')]
    expect(attachExample(draft, 0, 999)).toBe(draft)
    expect(attachExample(draft, 5, draft.lexemes[0].definitions[0].uid)).toBe(draft)
  })

  test('allDefinitions flattens lexemes in order', () => {
    const draft = emptyDraft('record')
    const n = def('n')
    const v = def('v')
    draft.lexemes = [lex('noun', [n]), lex('verb', [v])]
    expect(allDefinitions(draft)).toEqual([n, v])
  })
})
