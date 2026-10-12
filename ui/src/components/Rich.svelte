<script>
  import { render } from '../lib/markup.js'

  let { text = '' } = $props()
</script>

<span>{@html render(text)}</span>
