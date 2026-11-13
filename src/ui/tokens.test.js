import { readFileSync } from 'node:fs';
import { describe, expect, test } from 'vitest';

const css = readFileSync(new URL('./tokens.css', import.meta.url), 'utf8');

function token(name) {
  const m = css.match(new RegExp(`--${name}:\\s*([^;]+);`));
  if (!m) throw new Error(`missing token --${name}`);
  return m[1].trim();
}

function parseColor(value) {
  const hex = value.match(/^#([0-9a-f]{6})$/i);
  if (hex) {
    const n = parseInt(hex[1], 16);
    return { r: n >> 16, g: (n >> 8) & 255, b: n & 255, a: 1 };
  }
  const rgba = value.match(/^rgba\((\d+),\s*(\d+),\s*(\d+),\s*([\d.]+)\)$/);
  if (rgba) return { r: +rgba[1], g: +rgba[2], b: +rgba[3], a: +rgba[4] };
  throw new Error(`unparsed colour ${value}`);
}

function over(top, bottom) {
  const mix = (t, b) => t * top.a + b * (1 - top.a);
  return { r: mix(top.r, bottom.r), g: mix(top.g, bottom.g), b: mix(top.b, bottom.b), a: 1 };
}

function luminance({ r, g, b }) {
  const channel = (c) => {
    const s = c / 255;
    return s <= 0.03928 ? s / 12.92 : ((s + 0.055) / 1.055) ** 2.4;
  };
  return 0.2126 * channel(r) + 0.7152 * channel(g) + 0.0722 * channel(b);
}

function contrastRatio(a, b) {
  const [hi, lo] = [luminance(a), luminance(b)].sort((x, y) => y - x);
  return (hi + 0.05) / (lo + 0.05);
}

describe('design tokens', () => {
  const surface = () => over(parseColor(token('surface')), parseColor(token('bg')));

  test.each(['text-strong', 'text-body', 'text-muted'])('%s reaches 4.5:1 on the panel surface', (name) => {
    expect(contrastRatio(parseColor(token(name)), surface())).toBeGreaterThanOrEqual(4.5);
  });

  test.each(['link', 'danger', 'warn'])('%s reaches 4.5:1 on the panel surface', (name) => {
    expect(contrastRatio(parseColor(token(name)), surface())).toBeGreaterThanOrEqual(4.5);
  });

  test.each(['text-xs', 'text-sm', 'text-md', 'text-lg', 'text-xl'])('%s is at least 11px', (name) => {
    expect(parseInt(token(name), 10)).toBeGreaterThanOrEqual(11);
  });

  test('the solid surface matches the translucent surface over the page, so a footer can hide what scrolls under it', () => {
    const solid = parseColor(token('surface-solid'));
    const composite = surface();
    expect(Math.abs(solid.r - composite.r)).toBeLessThan(2);
    expect(Math.abs(solid.g - composite.g)).toBeLessThan(2);
    expect(Math.abs(solid.b - composite.b)).toBeLessThan(2);
    expect(solid.a).toBe(1);
  });
});
