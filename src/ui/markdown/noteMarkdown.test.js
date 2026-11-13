import { describe, expect, test } from 'vitest';
import { parseInlines, parseNote, plainText, resolvePreviewLink } from './noteMarkdown.js';

const text = (t) => ({ type: 'text', text: t });
const para = (...inlines) => ({ type: 'paragraph', inlines });

describe('parseNote blocks', () => {
  test('empty string gives no blocks', () => {
    expect(parseNote('')).toEqual([]);
    expect(parseNote('\n\n  \n')).toEqual([]);
    expect(parseNote(undefined)).toEqual([]);
  });

  test.each([
    ['# One', 1, 'One'],
    ['## Two', 2, 'Two'],
    ['### Three', 3, 'Three'],
  ])('%s is a heading', (line, level, content) => {
    expect(parseNote(line)).toEqual([{ type: 'heading', level, inlines: [text(content)] }]);
  });

  test('more than three hashes or no space stays a paragraph', () => {
    expect(parseNote('#### Four')).toEqual([para(text('#### Four'))]);
    expect(parseNote('#tag')).toEqual([para(text('#tag'))]);
  });

  test('dash and star bullets form one list', () => {
    expect(parseNote('- a\n* b')).toEqual([{
      type: 'list',
      ordered: false,
      items: [
        { inlines: [text('a')], depth: 0 },
        { inlines: [text('b')], depth: 0 },
      ],
    }]);
  });

  test('numbered lines group into one ordered list and keep their numbers', () => {
    expect(parseNote('1. a\n2. b\n3. c')).toEqual([{
      type: 'list',
      ordered: true,
      items: [
        { inlines: [text('a')], depth: 0, number: 1 },
        { inlines: [text('b')], depth: 0, number: 2 },
        { inlines: [text('c')], depth: 0, number: 3 },
      ],
    }]);
  });

  test('checklist lines group into one list with their state', () => {
    expect(parseNote('- [x] done\n- [ ] open')).toEqual([{
      type: 'list',
      ordered: false,
      items: [
        { inlines: [text('done')], depth: 0, checked: true },
        { inlines: [text('open')], depth: 0, checked: false },
      ],
    }]);
  });

  test('a change between ordered and unordered starts a new list', () => {
    const blocks = parseNote('1. a\n- b');
    expect(blocks.map((b) => [b.type, b.ordered])).toEqual([['list', true], ['list', false]]);
  });

  test('indented items carry a depth', () => {
    const [list] = parseNote('* top\n   * nested\n      * deeper');
    expect(list.items.map((i) => i.depth)).toEqual([0, 1, 2]);
  });

  test('tab-indented literal bullets are list items', () => {
    const [list] = parseNote('\t• one\n\t∙ two');
    expect(list.type).toBe('list');
    expect(list.items.map((i) => plainText(i.inlines))).toEqual(['one', 'two']);
  });

  test('> is a quote', () => {
    expect(parseNote('> said')).toEqual([{ type: 'quote', inlines: [text('said')] }]);
  });

  test('a whole line in backticks is code, and consecutive lines merge', () => {
    expect(parseNote('`a = 1`\n`b = 2`')).toEqual([{ type: 'code', text: 'a = 1\nb = 2' }]);
  });

  test('inline code at both ends of a line stays a paragraph', () => {
    expect(parseNote('`foo` and `bar`')).toEqual([para(
      { type: 'code', text: 'foo' }, text(' and '), { type: 'code', text: 'bar' },
    )]);
  });

  test.each([
    ['`npm test`: runs `vitest`'],
    ['`foo`, and `bar`'],
    ['`foo` and `bar`'],
  ])('inline code spans followed by punctuation stay a paragraph: %s', (line) => {
    const [block] = parseNote(line);
    expect(block.type).toBe('paragraph');
    expect(block.inlines.filter((n) => n.type === 'code')).toHaveLength(2);
  });

  test.each([
    ['`a=`./script.sh``', 'a=`./script.sh`'],
    ['`b=`/usr/bin/env``', 'b=`/usr/bin/env`'],
  ])('a nested command starting with punctuation stays code: %s', (line, code) => {
    expect(parseNote(line)).toEqual([{ type: 'code', text: code }]);
  });

  test('a code line with an assignment from a nested command stays code', () => {
    expect(parseNote('`a=`b``')).toEqual([{ type: 'code', text: 'a=`b`' }]);
  });

  test('a code line can contain backticks', () => {
    expect(parseNote('`echo `date``')).toEqual([{ type: 'code', text: 'echo `date`' }]);
  });

  test('a line of dashes is a rule', () => {
    expect(parseNote('---')).toEqual([{ type: 'rule' }]);
    expect(parseNote('-----')).toEqual([{ type: 'rule' }]);
  });

  test('pipe table with a separator row', () => {
    expect(parseNote('| A | B |\n| --- | --- |\n| 1 | 2 |\n| 3 |  |')).toEqual([{
      type: 'table',
      header: [[text('A')], [text('B')]],
      rows: [[[text('1')], [text('2')]], [[text('3')], []]],
    }]);
  });

  test('a lone pipe line without a separator stays a paragraph', () => {
    expect(parseNote('| not a table')).toEqual([para(text('| not a table'))]);
  });

  test('table cells parse inline formatting, including literal tags from the table parser', () => {
    const [table] = parseNote('| <b><u>Head</u></b> |\n| --- |\n| ~~gone~~ |');
    expect(table.header[0]).toEqual([
      { type: 'bold', children: [{ type: 'underline', children: [text('Head')] }] },
    ]);
    expect(table.rows[0][0]).toEqual([{ type: 'strike', children: [text('gone')] }]);
  });

  test('blank line runs collapse into one gap on the next block', () => {
    expect(parseNote('a\n\n\n\nb')).toEqual([para(text('a')), { ...para(text('b')), gapBefore: true }]);
  });

  test('adjacent lines stay separate without a gap', () => {
    expect(parseNote('a\nb')).toEqual([para(text('a')), para(text('b'))]);
  });

  test('leading whitespace in a paragraph is preserved', () => {
    expect(parseNote('    indented')).toEqual([para(text('    indented'))]);
  });
});

describe('parseNote title handling', () => {
  test('first line equal to the title is dropped, with #', () => {
    expect(parseNote('# Food\nbody', { title: 'Food' })).toEqual([para(text('body'))]);
  });

  test('first line equal to the title is dropped, without #', () => {
    expect(parseNote('Food\nbody', { title: 'Food' })).toEqual([para(text('body'))]);
  });

  test('first line equal to the title is dropped when bold', () => {
    expect(parseNote('# **★ Tea**\nbody', { title: '★ Tea' })).toEqual([para(text('body'))]);
  });

  test('a first line that differs is kept', () => {
    expect(parseNote('# Other\nbody', { title: 'Food' })).toHaveLength(2);
  });

  test('a later line equal to the title is kept', () => {
    expect(parseNote('first\nFood', { title: 'Food' })).toHaveLength(2);
  });

  test('the gap after a dropped title does not count', () => {
    expect(parseNote('Food\n\nbody', { title: 'Food' })).toEqual([para(text('body'))]);
  });
});

describe('parseInlines', () => {
  test.each([
    ['**b**', [{ type: 'bold', children: [text('b')] }]],
    ['*i*', [{ type: 'italic', children: [text('i')] }]],
    ['~~s~~', [{ type: 'strike', children: [text('s')] }]],
    ['<u>u</u>', [{ type: 'underline', children: [text('u')] }]],
    ['<b>b</b>', [{ type: 'bold', children: [text('b')] }]],
    ['`c`', [{ type: 'code', text: 'c' }]],
    ['{#tag}', [{ type: 'tag', name: 'tag' }]],
    ['{color:purple}p{/color}', [{ type: 'color', name: 'purple', children: [text('p')] }]],
    ['[[Note Name]]', [{ type: 'link', name: 'Note Name' }]],
    ['[[broken link: Gone]]', [{ type: 'brokenLink', name: 'Gone' }]],
    ['[[unknown link]]', [{ type: 'unknownLink' }]],
    ['[site](https://example.com)', [{ type: 'webLink', text: 'site', url: 'https://example.com' }]],
    ['[image]', [{ type: 'attachment', label: 'image' }]],
    ['[mention]', [{ type: 'attachment', label: 'mention' }]],
  ])('%s', (input, expected) => {
    expect(parseInlines(input)).toEqual(expected);
  });

  test('nesting: bold italic, formatted text around links', () => {
    expect(parseInlines('***x***')).toEqual([
      { type: 'italic', children: [{ type: 'bold', children: [text('x')] }] },
    ]);
    expect(parseInlines('a **b [[N]]** c')).toEqual([
      text('a '),
      { type: 'bold', children: [text('b '), { type: 'link', name: 'N' }] },
      text(' c'),
    ]);
  });

  test.each([
    ['**a***b*', [{ type: 'bold', children: [text('a')] }, { type: 'italic', children: [text('b')] }]],
    ['*a***b**', [{ type: 'italic', children: [text('a')] }, { type: 'bold', children: [text('b')] }]],
    ['**a****{color:red}b{/color}**', [
      { type: 'bold', children: [text('a')] },
      { type: 'bold', children: [{ type: 'color', name: 'red', children: [text('b')] }] },
    ]],
    ['**b *c***', [{ type: 'bold', children: [text('b '), { type: 'italic', children: [text('c')] }] }]],
    ['*a **b***', [{ type: 'italic', children: [text('a '), { type: 'bold', children: [text('b')] }] }]],
  ])('adjacent and nested star runs: %s', (input, expected) => {
    expect(parseInlines(input)).toEqual(expected);
  });

  test('a star inside a word or number is literal, not italic', () => {
    expect(parseInlines('3*4 = 12 and 5*6 = 30')).toEqual([text('3*4 = 12 and 5*6 = 30')]);
    expect(parseInlines('a*b and c*d')).toEqual([text('a*b and c*d')]);
  });

  test('italics after punctuation or at a word start still work', () => {
    expect(parseInlines('(*x*) and *y*')).toEqual([
      text('('), { type: 'italic', children: [text('x')] }, text(') and '), { type: 'italic', children: [text('y')] },
    ]);
  });

  test('web link URLs keep balanced parentheses', () => {
    expect(parseInlines('[x](https://en.wikipedia.org/wiki/Foo_(bar)) tail')).toEqual([
      { type: 'webLink', text: 'x', url: 'https://en.wikipedia.org/wiki/Foo_(bar)' },
      text(' tail'),
    ]);
  });

  test('a web link with an unbalanced URL ends at the first closing parenthesis', () => {
    expect(parseInlines('[x](https://a.example/(b) c')).toEqual([
      { type: 'webLink', text: 'x', url: 'https://a.example/(b' },
      text(' c'),
    ]);
  });

  test('an attachment followed by parenthetical text keeps both', () => {
    expect(parseInlines('[image](see above)')).toEqual([
      { type: 'attachment', label: 'image' }, text('(see above)'),
    ]);
  });

  test('a web link whose text happens to be an attachment word is still a link', () => {
    expect(parseInlines('[image](https://example.com/a.png)')).toEqual([
      { type: 'webLink', text: 'image', url: 'https://example.com/a.png' },
    ]);
  });

  test('UTI-style attachment labels read as a file', () => {
    expect(parseInlines('[com.apple.paper]')).toEqual([{ type: 'attachment', label: 'file' }]);
    expect(parseInlines('[com.adobe.pdf]')).toEqual([{ type: 'attachment', label: 'file' }]);
  });

  test('ordinary bracketed words stay literal', () => {
    expect(parseInlines('[main] and [list tickers]')).toEqual([text('[main] and [list tickers]')]);
  });

  test('every named colour survives; an unknown colour is dropped, children kept', () => {
    for (const name of ['red', 'orange', 'yellow', 'green', 'teal', 'blue', 'indigo', 'purple', 'pink']) {
      expect(parseInlines(`{color:${name}}x{/color}`)[0].name).toBe(name);
    }
    expect(parseInlines('{color:#EBEBF5}x{/color}')).toEqual([text('x')]);
    expect(parseInlines('{color:evil}**x**{/color}')).toEqual([{ type: 'bold', children: [text('x')] }]);
  });
});

describe('hostile and malformed text', () => {
  test('<script> stays literal text', () => {
    expect(parseInlines('<script>alert(1)</script>')).toEqual([text('<script>alert(1)</script>')]);
  });

  test('other HTML stays literal; <u> only pairs exactly', () => {
    expect(parseInlines('<img src=x onerror=alert(1)>')).toEqual([text('<img src=x onerror=alert(1)>')]);
    expect(parseInlines('<u>no close')).toEqual([text('<u>no close')]);
    expect(parseInlines('<U>x</U>')).toEqual([text('<U>x</U>')]);
    expect(parseInlines('<u onclick=x>y</u>')).toEqual([text('<u onclick=x>y</u>')]);
  });

  test('a javascript: URL is only ever data on a webLink node', () => {
    expect(parseInlines('[x](javascript:alert(1))')[0]).toEqual({
      type: 'webLink', text: 'x', url: 'javascript:alert(1)',
    });
  });

  test('a colour name cannot carry CSS', () => {
    expect(parseInlines('{color:red;background:url(x)}y{/color}')).toEqual([text('y')]);
  });

  test('unterminated markers stay literal and never throw', () => {
    for (const input of ['****', '**bold', '*it', '~~s', '`c', '[[x', '[x](y', '{#t', '{color:red}x', '**', '*', '***']) {
      expect(() => parseInlines(input)).not.toThrow();
      expect(plainText(parseInlines(input))).toBe(input);
    }
  });

  test('a star surrounded by spaces is not italic', () => {
    expect(parseInlines('2 * 3 * 4')).toEqual([text('2 * 3 * 4')]);
  });

  test.each([
    ['stars', '*'.repeat(200000)],
    ['openers with no closer', '*a '.repeat(70000)],
    ['bold openers with no closer', '**a '.repeat(50000)],
    ['colour openers', '{color:red}'.repeat(18000)],
    ['link openers', '[['.repeat(100000)],
    ['brackets', '['.repeat(200000)],
    ['tags', '<u>'.repeat(66000)],
    ['web link openers with open parentheses', `${'[a]('.repeat(25000)}${'('.repeat(100000)})`],
    ['tag openers sharing one far closer', `${'{#'.repeat(100000)} }`],
    ['bracket openers sharing one far closer', `${'[a'.repeat(100000)}]`],
    ['colour openers sharing far closers', `${'{color:'.repeat(28000)}}x{/color}`],
  ])('200k chars of %s parses in under 200ms', (_name, input) => {
    const start = performance.now();
    parseNote(`x${input}`); // the x keeps a run of stars from being a rule line
    expect(performance.now() - start).toBeLessThan(200);
  });

  test('200k chars across many lines parses in under 200ms', () => {
    const input = Array.from({ length: 8000 }, (_, i) => `- [x] item **${i}** with [[Link ${i}]] and text`).join('\n');
    const start = performance.now();
    expect(parseNote(input)).toHaveLength(1);
    expect(performance.now() - start).toBeLessThan(200);
  });

  test('deep nesting is bounded and keeps the text', () => {
    const depth = 500;
    const input = `${'*'.repeat(depth)}x${'*'.repeat(depth)}`;
    expect(() => parseInlines(input)).not.toThrow();
    expect(plainText(parseInlines(input))).toContain('x');
  });
});

describe('plainText', () => {
  test('flattens nested formatting', () => {
    expect(plainText(parseInlines('a **b *c*** [[N]] {#t}'))).toBe('a b c N #t');
  });
});

describe('resolvePreviewLink', () => {
  const a = { id: 1, name: 'A' };
  test('one match gives its id', () => {
    expect(resolvePreviewLink('A', [a, { id: 2, name: 'B' }])).toBe(1);
  });

  test('two links to the same note are still one match', () => {
    expect(resolvePreviewLink('A', [a, a])).toBe(1);
  });

  test('duplicate titles give null, not a guess', () => {
    expect(resolvePreviewLink('A', [a, { id: 9, name: 'A' }])).toBeNull();
  });

  test('no match gives null', () => {
    expect(resolvePreviewLink('Hidden', [a])).toBeNull();
    expect(resolvePreviewLink('A', [])).toBeNull();
  });

  test('a duplicate title stays ambiguous when one of the notes is hidden', () => {
    const hidden = { id: 9, name: 'A' };
    expect(resolvePreviewLink('A', [a, hidden], new Set([1]))).toBeNull();
  });

  test('a unique title resolves only if that note is visible', () => {
    expect(resolvePreviewLink('A', [a], new Set([1]))).toBe(1);
    expect(resolvePreviewLink('A', [a], new Set([2]))).toBeNull();
  });

  test('matching is exact', () => {
    expect(resolvePreviewLink('a', [a])).toBeNull();
  });
});
