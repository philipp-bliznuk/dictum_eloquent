<script>
  import { onMount, untrack } from 'svelte'
  import { api, ApiError } from '../lib/api.js'
  import { play } from '../lib/audio.js'
  import { autogrow } from '../lib/autogrow.js'
  import {
    allDefinitions,
    attachExample,
    blankDefinition,
    blankExample,
    blankLexeme,
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
    TARGET_EXAMPLES,
    toPayload,
  } from '../lib/draft.js'
  import { link, navigate, router } from '../lib/router.svelte.js'
  import Icon from '../components/Icon.svelte'

  let { id = null } = $props()

  const POS = [
    'noun',
    'verb',
    'adjective',
    'adverb',
    'pronoun',
    'preposition',
    'conjunction',
    'interjection',
    'phrase',
    'other',
  ]

  let draft = $state(emptyDraft(router.query.get('text') ?? ''))
  let error = $state('')
  let info = $state('')
  let conflict = $state(null)
  let busy = $state(false)
  // Edit mode starts in the loading state so the empty draft never flashes before the entry arrives.
  // App keys this component by id, so capturing the initial value is intentional.
  let loading = $state(untrack(() => id !== null))
  let looking = $state(false)
  let lookup = $state(null)
  let ai = $state(false)
  let asking = $state(false)
  let aiMiss = $state('')
  let generating = $state([])
  let drafting = $state([])
  let form = $state(null)

  const headword = $derived(draft.spellings[0]?.text.trim() ?? '')
  const missing = $derived(missingExamples(draft))
  const filled = $derived(allDefinitions(draft).filter((d) => d.text.trim()).length)
  const numbered = $derived(
    draft.lexemes.flatMap((lx) =>
      lx.definitions
        .map((d, i) => ({
          uid: d.uid,
          label: `${lx.part_of_speech.trim() || 'other'} ${i + 1}. ${d.text.trim()}`,
        }))
        .filter((d, i) => lx.definitions[i].text.trim()),
    ),
  )
  const isMac = typeof navigator !== 'undefined' && /Mac|iPhone|iPad/.test(navigator.platform)

  const missingIn = (lx) => lx.definitions.filter((d) => missing.includes(d.uid)).map((d) => d.uid)

  async function submit(event) {
    event.preventDefault()
    error = ''
    conflict = null
    busy = true
    try {
      const payload = toPayload(draft)
      const saved = id ? await api.replaceWord(id, payload) : await api.createWord(payload)
      navigate(`/words/${saved.id}`)
    } catch (e) {
      if (e instanceof ApiError && e.status === 409 && e.location) {
        conflict = { detail: e.detail, href: e.location.replace(/^\/api/, '') }
      } else {
        error = e.message
      }
    } finally {
      busy = false
    }
  }

  /** Request examples for `uids`; `target` tops definitions up instead of appending everything. */
  async function generate(uids, { target = Infinity } = {}) {
    const requests = examplesRequests(
      draft,
      uids.filter((uid) => !generating.includes(uid)),
    )
    if (!requests.length) return
    const sent = requests.flatMap((r) => r.uids)
    error = ''
    generating = [...generating, ...sent]
    const failures = []
    await Promise.all(
      requests.map(async (r) => {
        try {
          const result = await api.generateExamples(r.body)
          draft = mergeExamples(draft, r.uids, result.examples, { target })
        } catch (e) {
          failures.push(`${r.body.part_of_speech}: ${e.message}`)
        }
      }),
    )
    generating = generating.filter((uid) => !sent.includes(uid))
    if (failures.length) error = failures.join('; ')
  }

  const fillMissing = (uids) => generate(uids, { target: TARGET_EXAMPLES })

  const generateMissing = () => fillMissing(missingExamples(draft))

  async function lookUp(text = headword) {
    if (!text || looking) return
    error = ''
    info = ''
    conflict = null
    aiMiss = ''
    looking = true
    let result = null
    try {
      result = await api.lookup(text)
    } catch (e) {
      error = e.message
    } finally {
      looking = false
    }
    if (!result) return
    lookup = result
    if (result.existing_word_id && result.existing_word_id !== id) {
      conflict = {
        detail: `"${result.query}" is already in your dictionary.`,
        href: `/words/${result.existing_word_id}`,
      }
    }
    if (!result.found) return
    draft = mergeLookup(draft, result)
    if (ai) await generateMissing()
  }

  async function askAI() {
    const text = lookup?.query ?? headword
    if (!text || asking) return
    error = ''
    aiMiss = ''
    asking = true
    let result = null
    try {
      result = await api.generateDefinitions({ text })
    } catch (e) {
      error = e.message
    } finally {
      asking = false
    }
    if (!result) return
    if (!result.found) {
      aiMiss = `The AI does not recognise "${result.query}" either. You can still fill in the entry by hand.`
      return
    }
    draft = mergeLookup(draft, result)
    await generateMissing()
  }

  async function draftLexeme(lx) {
    const pos = lx.part_of_speech.trim()
    if (!headword || !pos || drafting.includes(lx.uid)) return
    error = ''
    info = ''
    drafting = [...drafting, lx.uid]
    let result = null
    try {
      result = await api.generateDefinitions({
        text: headword,
        parts_of_speech: [pos],
        hints: lx.definitions.map((d) => d.text.trim()).filter(Boolean),
      })
    } catch (e) {
      error = e.message
    } finally {
      drafting = drafting.filter((uid) => uid !== lx.uid)
    }
    if (!result) return
    if (!result.found || !result.entries.length) {
      info = `The AI knows no ${pos} sense of "${headword}".`
      return
    }
    draft = mergeDefinitions(draft, lx.uid, result.entries)
    await generateMissing()
  }

  function suggest(text) {
    draft.spellings[0].text = text
    lookUp(text)
  }

  function addSpelling() {
    draft.spellings.push({ text: '', kind: 'variant' })
  }

  function addLexeme() {
    draft.lexemes.push(blankLexeme(''))
  }

  function addDefinition(lx) {
    lx.definitions.push(blankDefinition())
  }

  function addExample(def) {
    def.examples.push(blankExample())
  }

  function remove(list, index) {
    list.splice(index, 1)
  }

  function move(uid, delta) {
    draft = moveDefinition(draft, uid, delta)
  }

  function moveLex(uid, delta) {
    draft = moveLexeme(draft, uid, delta)
  }

  function attach(index, event) {
    const uid = Number(event.currentTarget.value)
    event.currentTarget.value = ''
    if (uid) draft = attachExample(draft, index, uid)
  }

  function pronounce(lx) {
    play({ audioPath: lx.audio_path, text: headword })
  }

  function onKey(event) {
    if (!(event.metaKey || event.ctrlKey) || event.key !== 'Enter') return
    if (!form || busy || !form.contains(document.activeElement)) return
    event.preventDefault()
    form.requestSubmit()
  }

  onMount(async () => {
    try {
      ai = Boolean((await api.health()).ai)
    } catch {
      ai = false
    }
    if (!id) {
      if (headword) await lookUp()
      return
    }
    loading = true
    try {
      draft = fromWord(await api.getWord(id))
    } catch (e) {
      error = e.message
    } finally {
      loading = false
    }
  })
</script>

<svelte:window onkeydown={onKey} />

<div class="page-head">
  <h1>{id ? 'Edit entry' : 'Add word or phrase'}</h1>
  {#if id}<a class="small" href="/words/{id}" use:link>View entry</a>{/if}
</div>

{#if error}<div class="error">{error}</div>{/if}
{#if info}<div class="notice">{info}</div>{/if}
{#if conflict}
  <div class="notice">
    {conflict.detail}
    <a href={conflict.href} use:link>Open existing entry</a>
  </div>
{/if}

{#if loading}
  <p class="muted">Loading…</p>
{:else}
  <form bind:this={form} onsubmit={submit}>
    <section class="card">
      <h2>Headword</h2>
      {#each draft.spellings as s, i (i)}
        <div class="row-edit">
          <input
            type="text"
            bind:value={s.text}
            placeholder={i === 0 ? 'Word or phrase, e.g. put paid to' : 'Variant spelling'}
            aria-label={i === 0 ? 'Headword' : `Variant spelling ${i}`}
            autocapitalize="sentences"
            autocomplete="off"
            required={i === 0}
          />
          {#if i === 0}
            {#if !id}
              <button type="button" class="ghost" onclick={() => lookUp()} disabled={!headword || looking}>
                <Icon name={looking ? 'loader' : 'book'} size={18} />
                Look up
              </button>
            {/if}
          {:else}
            <button
              type="button"
              class="icon-btn danger"
              aria-label="Remove variant"
              onclick={() => remove(draft.spellings, i)}
            >
              <Icon name="x" size={18} />
            </button>
          {/if}
        </div>
      {/each}
      <div class="row spread">
        <button type="button" class="ghost sm" onclick={addSpelling}>
          <Icon name="plus" size={16} />
          Add spelling variant
        </button>
        <label class="inline-label">
          <span class="muted small">Status</span>
          <select bind:value={draft.status} aria-label="Status">
            <option value="inbox">inbox</option>
            <option value="learned">learned</option>
          </select>
        </label>
      </div>

      {#if lookup && !lookup.found}
        <div class="sub">
          <p class="muted">No dictionary entry for "{lookup.query}".</p>
          {#if lookup.suggestions.length}
            <p class="muted small">Did you mean:</p>
            <div class="chips">
              {#each lookup.suggestions as text (text)}
                <button type="button" class="chip" onclick={() => suggest(text)}>{text}</button>
              {/each}
            </div>
          {/if}
          {#if ai}
            <div class="row">
              <button type="button" class="ghost" onclick={askAI} disabled={asking}>
                <Icon name={asking ? 'loader' : 'sparkles'} size={18} />
                {asking ? 'Asking AI…' : 'Ask AI to draft it'}
              </button>
              <span class="muted small">Check AI drafts carefully before saving.</span>
            </div>
          {/if}
          {#if aiMiss}<p class="muted">{aiMiss}</p>{/if}
        </div>
      {/if}
      {#if lookup}
        <p class="muted small">{describeAttempts(lookup.attempts)}</p>
      {/if}
    </section>

    <div class="section-head">
      <h2>Senses by part of speech</h2>
      <p class="hint">
        Markup: <code>**bold**</code>, <code>*italic*</code>. Wrap the target word or phrase in examples with
        <code>*word*</code>.
      </p>
    </div>
    <datalist id="pos-list">
      {#each POS as p (p)}<option value={p}></option>{/each}
    </datalist>

    {#each draft.lexemes as lx, li (lx.uid)}
      <section class="card lex-card">
        <div class="lex-head">
          <span class="eyebrow">Part of speech {li + 1}</span>
          <div class="tools">
            <button
              type="button"
              class="icon-btn sm"
              aria-label="Move part of speech up"
              disabled={li === 0}
              onclick={() => moveLex(lx.uid, -1)}
            >
              <Icon name="up" size={16} />
            </button>
            <button
              type="button"
              class="icon-btn sm"
              aria-label="Move part of speech down"
              disabled={li === draft.lexemes.length - 1}
              onclick={() => moveLex(lx.uid, 1)}
            >
              <Icon name="down" size={16} />
            </button>
            <button
              type="button"
              class="icon-btn sm danger"
              aria-label="Remove part of speech"
              onclick={() => remove(draft.lexemes, li)}
            >
              <Icon name="trash" size={16} />
            </button>
          </div>
        </div>
        <div class="grid-2">
          <div class="field">
            <label for="pos-{lx.uid}">Part of speech</label>
            <input
              id="pos-{lx.uid}"
              type="text"
              bind:value={lx.part_of_speech}
              list="pos-list"
              placeholder="noun, verb, phrase…"
              autocomplete="off"
              required
            />
          </div>
          <div class="field">
            <label for="pron-{lx.uid}">Pronunciation</label>
            <div class="row-edit">
              <input
                id="pron-{lx.uid}"
                type="text"
                bind:value={lx.pronunciation}
                placeholder="ə-ˈban-dən"
                autocomplete="off"
              />
              {#if lx.audio_path}
                <button
                  type="button"
                  class="icon-btn"
                  aria-label="Play pronunciation"
                  onclick={() => pronounce(lx)}
                >
                  <Icon name="speaker" size={20} />
                </button>
              {/if}
            </div>
          </div>
        </div>

        <div class="def-list">
          {#each lx.definitions as d, i (d.uid)}
            <div class="def-panel">
              <div class="def-head">
                <span class="num">{i + 1}</span>
                {#if d.source !== 'user'}<span class="tag {d.source}">{d.source}</span>{/if}
                <div class="tools">
                  <button
                    type="button"
                    class="icon-btn sm"
                    aria-label="Move definition up"
                    disabled={i === 0}
                    onclick={() => move(d.uid, -1)}
                  >
                    <Icon name="up" size={16} />
                  </button>
                  <button
                    type="button"
                    class="icon-btn sm"
                    aria-label="Move definition down"
                    disabled={i === lx.definitions.length - 1}
                    onclick={() => move(d.uid, 1)}
                  >
                    <Icon name="down" size={16} />
                  </button>
                  <button
                    type="button"
                    class="icon-btn sm danger"
                    aria-label="Remove definition"
                    onclick={() => remove(lx.definitions, i)}
                  >
                    <Icon name="trash" size={16} />
                  </button>
                </div>
              </div>
              <textarea
                class="autogrow"
                bind:value={d.text}
                use:autogrow={d.text}
                placeholder="Definition {i + 1}"
                aria-label="Definition {i + 1} ({lx.part_of_speech || 'part of speech ' + (li + 1)})"
                rows="2"></textarea>

              {#if d.examples.length}
                <div class="ex-list">
                  {#each d.examples as ex, j (j)}
                    <div class="ex-row">
                      <textarea
                        class="autogrow"
                        bind:value={ex.text}
                        use:autogrow={ex.text}
                        placeholder="Example sentence"
                        aria-label="Example {j + 1} for definition {i + 1}"
                        rows="1"></textarea>
                      <div class="ex-tools">
                        {#if ex.source !== 'user'}<span class="tag {ex.source}">{ex.source}</span>{/if}
                        <button
                          type="button"
                          class="icon-btn sm danger"
                          aria-label="Remove example"
                          onclick={() => remove(d.examples, j)}
                        >
                          <Icon name="x" size={16} />
                        </button>
                      </div>
                    </div>
                  {/each}
                </div>
              {/if}

              <div class="row">
                <button type="button" class="ghost sm" onclick={() => addExample(d)}>
                  <Icon name="plus" size={16} />
                  Example
                </button>
                {#if ai}
                  <button
                    type="button"
                    class="ghost sm"
                    onclick={() => generate([d.uid])}
                    disabled={!headword || !d.text.trim() || generating.includes(d.uid)}
                    title="Generate example sentences for this definition"
                  >
                    <Icon name={generating.includes(d.uid) ? 'loader' : 'sparkles'} size={16} />
                    {generating.includes(d.uid) ? 'Generating…' : 'Generate examples'}
                  </button>
                {/if}
              </div>
            </div>
          {/each}
        </div>

        <div class="row">
          <button type="button" onclick={() => addDefinition(lx)}>
            <Icon name="plus" size={18} />
            Add definition
          </button>
          {#if ai}
            <button
              type="button"
              class="ghost"
              onclick={() => draftLexeme(lx)}
              disabled={!headword || !lx.part_of_speech.trim() || drafting.includes(lx.uid)}
              title="Ask the AI for the senses of this part of speech; your definitions are kept as hints"
            >
              <Icon name={drafting.includes(lx.uid) ? 'loader' : 'sparkles'} size={18} />
              {drafting.includes(lx.uid) ? 'Drafting…' : 'Draft definitions with AI'}
            </button>
            {#if missingIn(lx).length}
              <button
                type="button"
                class="ghost"
                onclick={() => fillMissing(missingIn(lx))}
                disabled={!headword || generating.length > 0}
                title="Top every definition up to {TARGET_EXAMPLES} examples"
              >
                <Icon name={generating.length ? 'loader' : 'sparkles'} size={18} />
                Generate missing examples ({missingIn(lx).length})
              </button>
            {/if}
          {/if}
        </div>
      </section>
    {/each}

    <button type="button" class="ghost add-lexeme" onclick={addLexeme}>
      <Icon name="plus" size={18} />
      Add part of speech
    </button>

    {#if draft.examples.length}
      <section class="card">
        <h2>Unattached examples</h2>
        <p class="hint">
          These examples are shown after the definitions. Attach them to a definition if one fits.
        </p>
        <div class="ex-list plain">
          {#each draft.examples as ex, j (j)}
            <div class="ex-row">
              <textarea
                class="autogrow"
                bind:value={ex.text}
                use:autogrow={ex.text}
                aria-label="Unattached example {j + 1}"
                rows="1"></textarea>
              <div class="ex-tools">
                {#if ex.source !== 'user'}<span class="tag {ex.source}">{ex.source}</span>{/if}
                {#if numbered.length}
                  <select aria-label="Attach example to a definition" onchange={(event) => attach(j, event)}>
                    <option value="">Attach to…</option>
                    {#each numbered as d (d.uid)}
                      <option value={d.uid}
                        >{d.label.length > 40 ? `${d.label.slice(0, 40)}…` : d.label}</option
                      >
                    {/each}
                  </select>
                {/if}
                <button
                  type="button"
                  class="icon-btn sm danger"
                  aria-label="Remove example"
                  onclick={() => remove(draft.examples, j)}
                >
                  <Icon name="x" size={16} />
                </button>
              </div>
            </div>
          {/each}
        </div>
      </section>
    {/if}

    <section class="card">
      <div class="field">
        <label for="notes">Notes</label>
        <textarea id="notes" class="autogrow" bind:value={draft.notes} use:autogrow={draft.notes} rows="2"
        ></textarea>
      </div>
    </section>

    <div class="form-bar">
      <span class="status muted small">
        {filled} definition{filled === 1 ? '' : 's'} · {isMac ? '⌘' : 'Ctrl'}+Enter to save
      </span>
      <a class="btn ghost" href={id ? `/words/${id}` : '/'} use:link>Cancel</a>
      <button type="submit" class="primary" disabled={busy}>
        <Icon name={busy ? 'loader' : 'check'} size={18} />
        {id ? 'Save' : 'Create'}
      </button>
    </div>
  </form>
{/if}
