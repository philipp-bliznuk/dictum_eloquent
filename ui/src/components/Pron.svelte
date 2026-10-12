<script>
  import { renderPron } from '../lib/pron.js'

  let { text = '' } = $props()
</script>

<span class="pron">{@html renderPron(text)}</span>
