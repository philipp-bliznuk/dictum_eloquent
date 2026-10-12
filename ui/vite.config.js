import { svelte } from '@sveltejs/vite-plugin-svelte'
import { defineConfig } from 'vite'

// changeOrigin rewrites the Host header to the target so a restrictive ALLOWED_HOSTS
// (which should always include 127.0.0.1) accepts proxied dev requests.
const backend = { target: 'http://127.0.0.1:3000', changeOrigin: true }

export default defineConfig({
  plugins: [svelte()],
  server: {
    port: 5173,
    proxy: {
      '/api': backend,
      '/audio': backend,
    },
  },
  build: {
    outDir: 'dist',
    emptyOutDir: true,
  },
})
