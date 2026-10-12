<script>
  import { onMount } from 'svelte'
  import { api } from '../lib/api.js'
  import { link, navigate } from '../lib/router.svelte.js'
  import { play } from '../lib/audio.js'
  import Icon from '../components/Icon.svelte'
  import Pron from '../components/Pron.svelte'
  import Rich from '../components/Rich.svelte'

  let { id } = $props()

  let word = $state(null)
  let error = $state('')
  let busy = $state(false)

  const headword = $derived(word ? word.spellings.map((s) => s.text).join(' / ') : '')
  const primary = $derived(word?.spellings.find((s) => s.kind === 'primary') ?? word?.spellings[0])
  const prons = $derived(word ? distinct(word.lexemes.map((lx) => lx.pronunciation)) : [])
  const audios = $derived(word ? distinct(word.lexemes.map((lx) => lx.audio_path)) : [])
  // One pronunciation shared by every part of speech is shown once, in the header.
  const hoisted = $derived(prons.length <= 1)
  const needsAudio = $derived(word ? word.lexemes.some((lx) => !lx.audio_path) : false)

  function distinct(values) {
    return [...new Set(values.filter(Boolean))]
  }

  function pronounce(audioPath = audios[0] ?? null) {
    play({ audioPath, text: primary?.text })
  }

  async function load() {
    error = ''
    try {
      word = await api.getWord(id)
    } catch (e) {
      error = e.message
    }
  }

  async function toggleStatus() {
    busy = true
    try {
      const status = word.status === 'inbox' ? 'learned' : 'inbox'
      word = await api.patchWord(id, { status })
    } catch (e) {
      error = e.message
    } finally {
      busy = false
    }
  }

  async function fetchAudio() {
    busy = true
    error = ''
    try {
      word = await api.fetchAudio(id)
      pronounce()
    } catch (e) {
      error = e.message
    } finally {
      busy = false
    }
  }

  async function remove() {
    if (!window.confirm(`Delete "${headword}"? This cannot be undone.`)) return
    busy = true
    try {
      await api.deleteWord(id)
      navigate('/')
    } catch (e) {
      error = e.message
      busy = false
    }
  }

  onMount(load)
</script>

{#if error}<div class="error">{error}</div>{/if}

{#if word}
  <article class="card entry">
    <header class="entry-head">
      <div>
        <h1 class="headword">{headword}</h1>
        <div class="row">
          {#if hoisted && prons[0]}<Pron text={prons[0]} />{/if}
          <span class="tag {word.status}">{word.status}</span>
        </div>
      </div>
      <button
        type="button"
        class="icon-btn"
        title={audios.length ? 'Play pronunciation' : 'Speak with the browser voice'}
        aria-label="Pronounce"
        onclick={() => pronounce()}
      >
        <Icon name="speaker" size={22} />
      </button>
    </header>

    {#each word.lexemes as lx (lx.id)}
      <section class="lexeme">
        <h2 class="pos-head">
          <span class="pos">{lx.part_of_speech}</span>
          {#if !hoisted && lx.pronunciation}<Pron text={lx.pronunciation} />{/if}
          {#if lx.audio_path && audios.length > 1}
            <button
              type="button"
              class="icon-btn sm"
              title="Play the {lx.part_of_speech} pronunciation"
              aria-label="Pronounce as {lx.part_of_speech}"
              onclick={() => pronounce(lx.audio_path)}
            >
              <Icon name="speaker" size={18} />
            </button>
          {/if}
        </h2>

        {#if lx.definitions.length}
          <ol class="defs">
            {#each lx.definitions as d (d.id)}
              <li>
                <p class="def-text">
                  <Rich text={d.text} />
                  {#if d.source !== 'user'}<span class="tag {d.source}">{d.source}</span>{/if}
                </p>
                {#if d.examples.length}
                  <ul class="examples">
                    {#each d.examples as ex (ex.id)}
                      <li><Rich text={ex.text} /></li>
                    {/each}
                  </ul>
                {/if}
              </li>
            {/each}
          </ol>
        {:else}
          <p class="empty-lexeme">No definitions yet.</p>
        {/if}
      </section>
    {/each}

    {#if word.examples.length}
      <section class="defs-tail">
        <h2 class="eyebrow">More examples</h2>
        <ul class="examples">
          {#each word.examples as ex (ex.id)}
            <li><Rich text={ex.text} /></li>
          {/each}
        </ul>
      </section>
    {/if}

    {#if word.notes}
      <section class="defs-tail">
        <h2 class="eyebrow">Notes</h2>
        <p class="notes"><Rich text={word.notes} /></p>
      </section>
    {/if}

    <footer class="actions">
      <a class="btn primary" href="/words/{id}/edit" use:link>
        <Icon name="pencil" size={18} />
        Edit
      </a>
      {#if needsAudio}
        <button
          type="button"
          class="ghost"
          onclick={fetchAudio}
          disabled={busy}
          title="Download the dictionary pronunciation"
        >
          <Icon name="download" size={18} />
          Fetch audio
        </button>
      {/if}
      <button type="button" class="ghost" onclick={toggleStatus} disabled={busy}>
        <Icon name={word.status === 'inbox' ? 'check' : 'inbox'} size={18} />
        {word.status === 'inbox' ? 'Mark learned' : 'Back to inbox'}
      </button>
      <button type="button" class="danger" onclick={remove} disabled={busy}>
        <Icon name="trash" size={18} />
        Delete
      </button>
    </footer>
  </article>
  <p class="muted small">Added {new Date(word.created_at).toLocaleDateString()}</p>
{:else if !error}
  <p class="muted">Loading…</p>
{/if}
