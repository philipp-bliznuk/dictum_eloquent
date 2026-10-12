// Svelte action: grow a textarea with its content. `use:autogrow={value}` re-measures whenever the
// bound value changes from outside (lookup merge, generated examples), not only on typing.
export function autogrow(node) {
  let width = 0
  const resize = () => {
    width = node.clientWidth
    node.style.height = 'auto'
    const chrome = node.offsetHeight - node.clientHeight
    node.style.height = `${node.scrollHeight + chrome}px`
  }
  // Width changes (viewport resize, late-arriving sibling columns, web fonts) rewrap the text.
  const observer =
    typeof ResizeObserver === 'undefined'
      ? null
      : new ResizeObserver(() => {
          if (node.clientWidth !== width) resize()
        })
  resize()
  node.addEventListener('input', resize)
  observer?.observe(node)
  return {
    // Microtask: let the pending bind:value write reach the DOM before measuring.
    update: () => queueMicrotask(resize),
    destroy: () => {
      node.removeEventListener('input', resize)
      observer?.disconnect()
    },
  }
}
