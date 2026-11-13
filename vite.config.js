import { defineConfig } from 'vite';
import { svelte } from '@sveltejs/vite-plugin-svelte';

export default defineConfig({
  root: 'src/ui',
  base: './',
  plugins: [svelte()],
  build: {
    outDir: '../../dist-ui',
    emptyOutDir: true,
  },
  test: {
    environment: 'node',
    // Scan all of src/ (not just the src/ui root) so main-process
    // module tests like src/applescript-escape.test.js are collected.
    // Relative to the working directory, not `root`: anything above src/
    // would also collect every sibling worktree's tests.
    dir: 'src',
    include: ['**/*.test.js'],
  },
});
