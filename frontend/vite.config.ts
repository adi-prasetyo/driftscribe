import { defineConfig } from 'vite';
import { svelte } from '@sveltejs/vite-plugin-svelte';
import { resolve } from 'node:path';

// Backend-integration build (Vite docs §Backend Integration): emit a manifest
// and a single `src/main.ts` entry; FastAPI reads the manifest to resolve the
// hashed JS/CSS for the shell + approval pages. Assets are served by FastAPI at
// `/static/`, so `base` is set accordingly (manifest `file` stays relative; the
// Python helper prepends `/static/`).
export default defineConfig({
  plugins: [svelte()],
  base: '/static/',
  build: {
    outDir: resolve(__dirname, '../agent/static'),
    emptyOutDir: true,
    manifest: true,
    rollupOptions: {
      input: resolve(__dirname, 'src/main.ts'),
      output: {
        entryFileNames: 'transparency-[hash].js',
        chunkFileNames: 'chunks/[name]-[hash].js',
        // CSS (the only emitted asset) → driftscribe-[hash].css
        assetFileNames: 'driftscribe-[hash][extname]',
      },
    },
  },
});
