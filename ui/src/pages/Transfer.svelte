<script>
  import { api } from '../lib/api.js'
  import Icon from '../components/Icon.svelte'

  let file = $state(null)
  let policy = $state('skip')
  let report = $state(null)
  let error = $state('')
  let busy = $state(false)

  async function runImport(event) {
    event.preventDefault()
    if (!file) return
    error = ''
    report = null
    busy = true
    try {
      const data = JSON.parse(await file.text())
      report = await api.importWords(data, policy)
    } catch (e) {
      error = e.message
    } finally {
      busy = false
    }
  }

  function onFile(event) {
    file = event.currentTarget.files?.[0] ?? null
  }
</script>

<div class="page-head">
  <h1>Data</h1>
</div>

{#if error}<div class="error">{error}</div>{/if}

<section class="card">
  <h2>Export</h2>
  <p class="hint">
    JSON is the full backup format and can be imported again. The vault zip holds one Markdown file per
    letter.
  </p>
  <div class="row">
    <a class="btn" href={api.exportJsonUrl()} download="dictum-export.json">
      <Icon name="download" size={18} />
      Download JSON
    </a>
    <a class="btn" href={api.exportMdUrl()} download="dictum-vault.zip">
      <Icon name="download" size={18} />
      Download vault (A-Z .md)
    </a>
  </div>
</section>

<form class="card" onsubmit={runImport}>
  <h2>Import</h2>
  <div class="field">
    <label for="file">JSON file (<code>{'{"version": 1, "words": [...]}'}</code>)</label>
    <input id="file" type="file" accept="application/json,.json" onchange={onFile} required />
  </div>
  <div class="field">
    <label for="policy">On spelling conflict</label>
    <select id="policy" bind:value={policy}>
      <option value="skip">skip - keep existing word</option>
      <option value="replace">replace - overwrite existing word</option>
      <option value="fail">fail - abort whole import</option>
    </select>
  </div>
  <button type="submit" class="primary" disabled={busy || !file}>
    <Icon name={busy ? 'loader' : 'upload'} size={18} />
    {busy ? 'Importing…' : 'Import'}
  </button>
</form>

{#if report}
  <section class="card">
    <h2>Report</h2>
    <div class="stats">
      <div class="stat"><b>{report.created}</b><span>created</span></div>
      <div class="stat"><b>{report.replaced}</b><span>replaced</span></div>
      <div class="stat"><b>{report.skipped}</b><span>skipped</span></div>
      <div class="stat">
        <b class:danger-text={report.failures.length}>{report.failures.length}</b><span>failed</span>
      </div>
    </div>
    {#if report.failures.length}
      <pre class="report">{report.failures
          .map((f) => `#${f.index} ${f.spellings.join(' / ')}: ${f.error}`)
          .join('\n')}</pre>
    {/if}
  </section>
{/if}
