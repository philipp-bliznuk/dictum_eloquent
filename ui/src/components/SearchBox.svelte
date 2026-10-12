<script>
  import Icon from './Icon.svelte'
  import Rich from './Rich.svelte'
  import { api } from '../lib/api.js'
  import { navigate, router } from '../lib/router.svelte.js'

  let { placeholder = 'Search words and phrases' } = $props()

  let q = $state('')
  let hits = $state([])
  let open = $state(false)
  let active = $state(-1)
  let input = $state(null)
  let timer
  let seq = 0

  const headword = (w) => w.spellings.map((s) => s.text).join(' / ')

  const items = $derived.by(() => {
    const list = hits.map((h) => ({
      kind: 'word',
      key: `w${h.word.id}`,
      id: h.word.id,
      label: headword(h.word),
      meta: h.word.parts_of_speech.join(' / '),
      // Definition hits show the matching definition so the user sees why the word turned up.
      snippet: h.kind === 'definition' ? h.matched : '',
    }))
    const text = q.trim()
    const exact = hits.some((h) => h.kind === 'spelling' && h.matched.toLowerCase() === text.toLowerCase())
    if (text && !exact) list.push({ kind: 'add', key: 'add', label: `Add "${text}"`, text })
    return list
  })

  async function search(text) {
    const mine = ++seq
    if (!text.trim()) {
      hits = []
      return
    }
    try {
      const res = await api.search(text, 12)
      if (mine === seq) hits = res.hits
    } catch {
      if (mine === seq) hits = []
    }
  }

  function onInput() {
    open = true
    active = -1
    clearTimeout(timer)
    timer = setTimeout(() => search(q), 120)
  }

  function close() {
    open = false
    active = -1
  }

  function reset() {
    close()
    q = ''
    hits = []
  }

  function choose(item) {
    if (!item) return
    const target =
      item.kind === 'word' ? `/words/${item.id}` : `/words/new?text=${encodeURIComponent(item.text)}`
    reset()
    input?.blur()
    navigate(target)
  }

  function onKeydown(e) {
    if (e.key === 'ArrowDown') {
      e.preventDefault()
      open = true
      active = Math.min(active + 1, items.length - 1)
    } else if (e.key === 'ArrowUp') {
      e.preventDefault()
      active = Math.max(active - 1, -1)
    } else if (e.key === 'Enter') {
      e.preventDefault()
      choose(items[active >= 0 ? active : 0])
    } else if (e.key === 'Escape') {
      if (open) close()
      else reset()
    }
  }

  function onFocusOut(e) {
    if (!e.currentTarget.contains(e.relatedTarget)) close()
  }

  function onGlobalKey(e) {
    if (e.key !== '/' || e.metaKey || e.ctrlKey || e.altKey) return
    const t = e.target
    if (
      t instanceof HTMLInputElement ||
      t instanceof HTMLTextAreaElement ||
      t instanceof HTMLSelectElement ||
      t?.isContentEditable
    ) {
      return
    }
    e.preventDefault()
    input?.focus()
    input?.select()
  }

  $effect(() => {
    // Any navigation closes the dropdown and clears the query.
    void router.path
    void router.query
    reset()
  })

  const expanded = $derived(open && items.length > 0)
  const activeId = $derived(active >= 0 && items[active] ? `search-opt-${items[active].key}` : undefined)
</script>

<svelte:window onkeydown={onGlobalKey} />

<div class="searchbox" onfocusout={onFocusOut}>
  <span class="icon-lead"><Icon name="search" size={18} /></span>
  <input
    bind:this={input}
    bind:value={q}
    type="search"
    role="combobox"
    aria-label="Search"
    aria-expanded={expanded}
    aria-controls="search-listbox"
    aria-autocomplete="list"
    aria-activedescendant={activeId}
    autocomplete="off"
    autocapitalize="off"
    spellcheck="false"
    enterkeyhint="go"
    {placeholder}
    oninput={onInput}
    onfocus={() => (open = true)}
    onkeydown={onKeydown}
  />
  {#if q}
    <button type="button" class="icon-btn sm clear" aria-label="Clear search" onclick={reset}>
      <Icon name="x" size={16} />
    </button>
  {/if}
  {#if expanded}
    <div class="dropdown" id="search-listbox" role="listbox" aria-label="Search results">
      {#each items as item, i (item.key)}
        <button
          type="button"
          role="option"
          id="search-opt-{item.key}"
          class="option"
          class:add={item.kind === 'add'}
          aria-selected={i === active}
          tabindex="-1"
          onmousedown={(e) => e.preventDefault()}
          onclick={() => choose(item)}
        >
          {#if item.kind === 'word'}
            <span class="hw">{item.label}</span>
            <span class="meta">{item.meta}</span>
            {#if item.snippet}
              <span class="snippet"><Rich text={item.snippet} /></span>
            {/if}
          {:else}
            <span class="row"><Icon name="plus" size={16} />{item.label}</span>
            <span class="muted-inline">new entry</span>
          {/if}
        </button>
      {/each}
    </div>
  {/if}
</div>
