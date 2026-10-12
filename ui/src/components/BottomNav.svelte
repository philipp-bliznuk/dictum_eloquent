<script>
  import Icon from './Icon.svelte'
  import { NAV_ITEMS } from '../lib/nav.js'
  import { link, router } from '../lib/router.svelte.js'
</script>

<nav class="bottom-nav" aria-label="Primary">
  {#each NAV_ITEMS as item (item.href)}
    <a
      href={item.href}
      use:link
      class:active={item.routes.includes(router.name)}
      aria-current={item.routes.includes(router.name) ? 'page' : undefined}
    >
      <Icon name={item.icon} size={22} />
      {item.label}
    </a>
  {/each}
</nav>
