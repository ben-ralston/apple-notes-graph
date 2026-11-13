import { readdirSync, readFileSync, statSync } from 'node:fs';
import { join } from 'node:path';
import { fileURLToPath } from 'node:url';
import { describe, expect, test } from 'vitest';

const root = fileURLToPath(new URL('.', import.meta.url));

function svelteFiles(dir) {
  return readdirSync(dir).flatMap((name) => {
    const path = join(dir, name);
    if (statSync(path).isDirectory()) return svelteFiles(path);
    return name.endsWith('.svelte') ? [path] : [];
  });
}

const stripped = (source) => source
  .replace(/<script[\s\S]*?<\/script>/g, '')
  .replace(/<style[\s\S]*?<\/style>/g, '')
  .replace(/<!--[\s\S]*?-->/g, '');

const files = svelteFiles(root).map((path) => ({
  name: path.slice(root.length),
  source: readFileSync(path, 'utf8'),
}));

describe('one icon set', () => {
  test.each(files.map((f) => [f.name, f.source]))('%s has no text-glyph icons or raw SVGs', (_name, source) => {
    const markup = stripped(source);
    expect(markup).not.toMatch(/[×⨯⊙≈→←↗✨⚙]/u);
    expect(markup).not.toMatch(/<svg/);
  });
});

describe('note text can never become markup', () => {
  test.each(files.map((f) => [f.name, f.source]))('%s does not use {@html}', (_name, source) => {
    expect(source).not.toContain('{@html');
  });

  test('NoteBody renders no anchors', () => {
    const body = files.find((f) => f.name.endsWith('NoteBody.svelte'));
    expect(body).toBeDefined();
    expect(stripped(body.source)).not.toMatch(/<a[\s>]/);
  });
});
