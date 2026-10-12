<script>
  let { name, size = 20, label = '' } = $props()

  // 24x24 stroke icons, path data only (Lucide-style geometry).
  const PATHS = {
    search: ['M11 4a7 7 0 1 0 0 14a7 7 0 1 0 0-14', 'm20 20-3.5-3.5'],
    plus: ['M12 5v14', 'M5 12h14'],
    x: ['M18 6 6 18', 'm6 6 12 12'],
    speaker: ['M11 5 6 9H3v6h3l5 4z', 'M15.5 8.5a5 5 0 0 1 0 7', 'M18.5 5.5a9 9 0 0 1 0 13'],
    pencil: ['M12 20h9', 'M16.5 3.5a2.1 2.1 0 0 1 3 3L7 19l-4 1 1-4Z'],
    trash: ['M3 6h18', 'M8 6V4h8v2', 'm19 6-1 14H6L5 6', 'M10 11v6', 'M14 11v6'],
    check: ['m5 12 5 5L20 7'],
    inbox: ['M22 12h-6l-2 3h-4l-2-3H2', 'M5.5 5h13l3.5 7v7H2v-7z'],
    up: ['m18 15-6-6-6 6'],
    down: ['m6 9 6 6 6-6'],
    sparkles: [
      'M12 3l1.9 5.1L19 10l-5.1 1.9L12 17l-1.9-5.1L5 10l5.1-1.9z',
      'M19 16l.8 2.2L22 19l-2.2.8L19 22l-.8-2.2L16 19l2.2-.8z',
    ],
    book: [
      'M4 19.5A2.5 2.5 0 0 1 6.5 17H20',
      'M6.5 2H20v20H6.5A2.5 2.5 0 0 1 4 19.5v-15A2.5 2.5 0 0 1 6.5 2z',
    ],
    list: ['M8 6h13', 'M8 12h13', 'M8 18h13', 'M3 6h.01', 'M3 12h.01', 'M3 18h.01'],
    database: [
      'M3 5a9 3 0 1 0 18 0a9 3 0 1 0-18 0',
      'M21 12c0 1.7-4 3-9 3s-9-1.3-9-3',
      'M3 5v14c0 1.7 4 3 9 3s9-1.3 9-3V5',
    ],
    download: ['M21 15v4a2 2 0 0 1-2 2H5a2 2 0 0 1-2-2v-4', 'm7 10 5 5 5-5', 'M12 15V3'],
    upload: ['M21 15v4a2 2 0 0 1-2 2H5a2 2 0 0 1-2-2v-4', 'm17 8-5-5-5 5', 'M12 3v12'],
    loader: ['M21 12a9 9 0 1 1-6.2-8.6'],
    back: ['m12 19-7-7 7-7', 'M19 12H5'],
  }

  const paths = $derived(PATHS[name] ?? [])
</script>

<svg
  class="icon"
  class:spin={name === 'loader'}
  width={size}
  height={size}
  viewBox="0 0 24 24"
  fill="none"
  stroke="currentColor"
  stroke-width="2"
  stroke-linecap="round"
  stroke-linejoin="round"
  role={label ? 'img' : undefined}
  aria-label={label || undefined}
  aria-hidden={label ? undefined : 'true'}
>
  {#each paths as d (d)}
    <path {d} />
  {/each}
</svg>
