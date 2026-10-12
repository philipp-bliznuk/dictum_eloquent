<script>
  import { link, router } from './lib/router.svelte.js'
  import TopBar from './components/TopBar.svelte'
  import BottomNav from './components/BottomNav.svelte'
  import Home from './pages/Home.svelte'
  import WordView from './pages/WordView.svelte'
  import WordForm from './pages/WordForm.svelte'
  import Transfer from './pages/Transfer.svelte'

  // "Add" picked from the search box while already on /words/new must restart the form.
  const newKey = $derived(router.query.get('text') ?? '')
</script>

<TopBar />

<main>
  {#if router.name === 'home'}
    <Home />
  {:else if router.name === 'new'}
    {#key newKey}
      <WordForm />
    {/key}
  {:else if router.name === 'edit'}
    {#key router.params.id}
      <WordForm id={Number(router.params.id)} />
    {/key}
  {:else if router.name === 'word'}
    {#key router.params.id}
      <WordView id={Number(router.params.id)} />
    {/key}
  {:else if router.name === 'transfer'}
    <Transfer />
  {:else}
    <div class="card">
      <h2>Not found</h2>
      <p class="muted">No page at <code>{router.path}</code>.</p>
      <p><a href="/" use:link>Back to words</a></p>
    </div>
  {/if}
</main>

<BottomNav />
