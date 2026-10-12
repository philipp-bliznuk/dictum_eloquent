<script>
  import Icon from './Icon.svelte'
  import SearchBox from './SearchBox.svelte'
  import { NAV_ITEMS } from '../lib/nav.js'
  import { link, router } from '../lib/router.svelte.js'
</script>

<header class="topbar">
  <div class="topbar-inner">
    <a class="brand" href="/" use:link aria-label="Dictum Eloquent, home">
      <Icon name="book" size={22} />
      <span>Dictum</span>
    </a>
    <SearchBox />
    <nav class="topnav" aria-label="Primary">
      {#each NAV_ITEMS as item (item.href)}
        <a
          href={item.href}
          use:link
          class:active={item.routes.includes(router.name)}
          aria-current={item.routes.includes(router.name) ? 'page' : undefined}
        >
          <Icon name={item.icon} size={18} />{item.label}
        </a>
      {/each}
    </nav>
  </div>
</header>
