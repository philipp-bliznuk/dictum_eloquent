<script>
  import { onMount } from 'svelte'
  import { api } from '../lib/api.js'
  import { link, navigate, router } from '../lib/router.svelte.js'
  import Icon from '../components/Icon.svelte'

  const PAGE = 50

  let page = $state({ items: [], total: 0, offset: 0, limit: PAGE })
  let stats = $state(null)
  let error = $state('')
  let loading = $state(false)

  const status = $derived(router.query.get('status') ?? '')
  const headword = (w) => w.spellings.map((s) => s.text).join(' / ')
  const last = $derived(Math.min(page.offset + page.limit, page.total))

  async function loadList(offset = 0) {
    loading = true
    error = ''
    try {
      const query = { offset, limit: PAGE }
      if (status) query.status = status
      page = await api.listWords(query)
    } catch (e) {
      error = e.message
    } finally {
      loading = false
    }
  }

  function onStatus(e) {
    const value = e.currentTarget.value
    navigate(value ? `/?status=${value}` : '/', { replace: true })
  }

  $effect(() => {
    void status
    loadList(0)
  })

  onMount(async () => {
    try {
      stats = await api.stats()
    } catch (e) {
      error = e.message
    }
  })
</script>

<section class="hero">
  <h1>Dictum Eloquent</h1>
  <p class="muted">
    Your personal dictionary of words worth keeping.
    <span class="desktop-only">Press <kbd>/</kbd> to search, or</span>
    <span class="phone-only">Search above, or</span>
    <a href="/words/new" use:link>add a word or phrase</a>.
  </p>
</section>

{#if error}<p class="error" role="alert">{error}</p>{/if}

{#if stats}
  <div class="stats">
    <a class="stat" href="/" use:link><b>{stats.words}</b><span>words</span></a>
    <a class="stat" href="/?status=inbox" use:link><b>{stats.inbox}</b><span>inbox</span></a>
    <div class="stat"><b>{stats.definitions}</b><span>definitions</span></div>
    <div class="stat"><b>{stats.examples}</b><span>examples</span></div>
  </div>
{/if}

<section class="card">
  <div class="spread">
    <h2>{status === 'inbox' ? 'Inbox' : status === 'learned' ? 'Learned words' : 'All words'}</h2>
    <label class="inline-label">
      <span class="muted small">Status</span>
      <select value={status} onchange={onStatus} aria-label="Filter by status">
        <option value="">any</option>
        <option value="inbox">inbox</option>
        <option value="learned">learned</option>
      </select>
    </label>
  </div>

  {#if !page.items.length}
    <p class="muted">{loading ? 'Loading…' : 'Nothing here yet.'}</p>
  {:else}
    <ul class="word-list">
      {#each page.items as w (w.id)}
        <li>
          <a href="/words/{w.id}" use:link>
            <span class="hw">
              {headword(w)}
              {#if w.status === 'inbox'}<span class="tag inbox">inbox</span>{/if}
            </span>
            <span class="meta">
              {w.parts_of_speech.join(' / ') || 'no part of speech'} · {w.definition_count}
              {w.definition_count === 1 ? 'def' : 'defs'}
            </span>
          </a>
        </li>
      {/each}
    </ul>
    <div class="spread pager">
      <span class="muted small">{page.offset + 1}-{last} of {page.total}</span>
      <span class="row">
        <button
          class="btn sm"
          disabled={page.offset === 0 || loading}
          onclick={() => loadList(Math.max(0, page.offset - PAGE))}
        >
          <Icon name="back" size={16} />Prev
        </button>
        <button
          class="btn sm"
          disabled={last >= page.total || loading}
          onclick={() => loadList(page.offset + PAGE)}
        >
          Next
        </button>
      </span>
    </div>
  {/if}
</section>
