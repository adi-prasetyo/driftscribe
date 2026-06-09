import { defineConfig } from 'vitest/config';
import { svelte } from '@sveltejs/vite-plugin-svelte';
import { svelteTesting } from '@testing-library/svelte/vite';

export default defineConfig({
  // `svelteTesting` adds the `browser` resolve condition so Svelte's client
  // (not SSR) build is used under vitest — required to render components in
  // jsdom with @testing-library/svelte (e.g. DecisionsRail.test.ts).
  //
  // `preprocess: []` overrides svelte.config.js's `vitePreprocess()`: the latter
  // calls Vite's `preprocessCSS`, which throws "Cannot create proxy with a
  // non-object" under vitest's PartialEnvironment. Our `<style>` blocks are
  // plain CSS (no Sass/PostCSS-lang), so skipping preprocessing in tests is a
  // no-op for correctness while letting components compile in jsdom.
  plugins: [svelte({ hot: false, preprocess: [] }), svelteTesting()],
  test: {
    environment: 'jsdom',
    globals: true,
    include: ['tests/unit/**/*.test.ts'],
    setupFiles: ['tests/unit/setup.ts'],
    passWithNoTests: true,
  },
});
