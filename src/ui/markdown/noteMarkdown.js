// Parser for the notation read_note_content emits (src/apple_notes/note_content.py).
// A general markdown library would misread this dialect ({color:…}, {#tag},
// [[links]]) and need a sanitiser. The output is plain data that NoteBody.svelte
// renders as text nodes, so note text can never become markup, and web links are
// data, never anchors.
//
// Inline parsing is a single scan with no backtracking regex: an unmatched marker
// is literal text, and closer lookups are cached so pathological input stays linear.

const MAX_NESTING = 8;
// Tags, colours and attachment labels are short. Capping them keeps many openers
// that share one far closer from each re-reading the text in between.
const MAX_NAME_LENGTH = 64;
const INDENT_SPACES = 3;

// Mirrors COLOR_MAP in src/apple_notes/colors.py. Anything else (hex values
// included) drops the colour and keeps the text.
const NAMED_COLORS = new Set(['red', 'orange', 'yellow', 'green', 'teal', 'blue', 'indigo', 'purple', 'pink']);

const ATTACHMENT_LABELS = new Set(['image', 'table', 'drawing', 'gallery', 'file', 'url', 'mention', 'attachment', 'hashtag']);
// Unmapped attachment types come through as a UTI: [com.apple.paper].
const UTI = /^[a-z]+(\.[a-z0-9-]+)+$/;

const URL_SCAN_MAX = 2048;
const URL_SCHEME = /^[a-z][a-z0-9+.-]*:/i;
const WORD_CHAR = /[\p{L}\p{N}]/u;

const MARKER_CHARS = new Set(['*', '~', '<', '[', '{', '`']);

const isSpace = (ch) => ch === undefined || ch === ' ' || ch === '\t';

function pushText(out, text) {
  if (!text) return;
  const last = out[out.length - 1];
  if (last?.type === 'text') last.text += text;
  else out.push({ type: 'text', text });
}

function pushAll(out, inlines) {
  for (const inline of inlines) {
    if (inline.type === 'text') pushText(out, inline.text);
    else out.push(inline);
  }
}

export function parseInlines(text, depth = 0) {
  const out = [];
  if (!text) return out;
  if (depth >= MAX_NESTING) {
    pushText(out, text);
    return out;
  }

  // Next occurrence of `needle` at or after `from`. `from` only grows during a
  // scan, so each answer stays valid until the scan passes it.
  const cache = new Map();
  const nextIndex = (needle, from) => {
    const hit = cache.get(needle);
    if (hit && from >= hit.from && (hit.at === -1 || from <= hit.at)) return hit.at;
    const at = text.indexOf(needle, from);
    cache.set(needle, { from, at });
    return at;
  };

  // The star that would close an italic opened before `from`. Pairs of stars are
  // bold markers to step over. In an odd run of 3+ the lone star comes last if a
  // bold span is open (*a **b***), and first otherwise (*a***b**).
  let loneStar = null;
  const nextLoneStar = (from) => {
    if (loneStar && from >= loneStar.from && (loneStar.at === -1 || from <= loneStar.at)) return loneStar.at;
    let at = nextIndex('*', from);
    let boldOpen = false;
    while (at !== -1) {
      let end = at;
      while (text[end] === '*') end += 1;
      const run = end - at;
      if (run % 2 === 1) {
        if (run > 1 && boldOpen) at = end - 1;
        break;
      }
      if ((run / 2) % 2 === 1) boldOpen = !boldOpen;
      at = nextIndex('*', end);
    }
    loneStar = { from, at };
    return at;
  };

  const hasOddStars = (from, to) => {
    let count = 0;
    for (let j = from; j < to; j++) if (text[j] === '*') count += 1;
    return count % 2 === 1;
  };

  const nest = (from, to) => parseInlines(text.slice(from, to), depth + 1);

  // Each matcher returns [inlines, nextIndex], or null when the text at `i` isn't
  // that marker.
  const matchStars = (i) => {
    if (text.startsWith('***', i)) {
      const close = nextIndex('***', i + 3);
      if (close > i + 3 && !isSpace(text[i + 3]) && !isSpace(text[close - 1])) {
        return [[{ type: 'italic', children: [{ type: 'bold', children: nest(i + 3, close) }] }], close + 3];
      }
    }
    if (text.startsWith('**', i)) {
      let close = nextIndex('**', i + 2);
      if (close > i + 2 && !isSpace(text[i + 2]) && !isSpace(text[close - 1])) {
        // In **b *c*** the bold closes on the last two stars of the run, because an
        // italic is still open. In **a***b* it closes on the first two.
        while (text[close + 2] === '*' && hasOddStars(i + 2, close)) close += 1;
        return [[{ type: 'bold', children: nest(i + 2, close) }], close + 2];
      }
      return [[{ type: 'text', text: '**' }], i + 2];
    }
    // Notes have no escape for a typed star, so 3*4 or a*b must stay literal.
    if (i > 0 && WORD_CHAR.test(text[i - 1])) return null;
    const close = nextLoneStar(i + 1);
    if (close > i + 1 && !isSpace(text[i + 1]) && !isSpace(text[close - 1])) {
      return [[{ type: 'italic', children: nest(i + 1, close) }], close + 1];
    }
    return null;
  };

  const matchWrapped = (i, open, closer, make) => {
    if (!text.startsWith(open, i)) return null;
    const start = i + open.length;
    const close = nextIndex(closer, start);
    if (close <= start) return null;
    return [[make(start, close)], close + closer.length];
  };

  const matchColor = (i) => {
    if (!text.startsWith('{color:', i)) return null;
    const nameEnd = nextIndex('}', i + 7);
    if (nameEnd === -1 || nameEnd - (i + 7) > MAX_NAME_LENGTH) return null;
    const close = nextIndex('{/color}', nameEnd + 1);
    if (close === -1) return null;
    const name = text.slice(i + 7, nameEnd);
    const children = nest(nameEnd + 1, close);
    const inlines = NAMED_COLORS.has(name) ? [{ type: 'color', name, children }] : children;
    return [inlines, close + 8];
  };

  const matchTag = (i) => {
    if (!text.startsWith('{#', i)) return null;
    const close = nextIndex('}', i + 2);
    if (close <= i + 2 || close - (i + 2) > MAX_NAME_LENGTH) return null;
    const name = text.slice(i + 2, close);
    if (/\s/.test(name)) return null;
    return [[{ type: 'tag', name }], close + 1];
  };

  const matchNoteLink = (i) => {
    if (!text.startsWith('[[', i)) return null;
    const close = nextIndex(']]', i + 2);
    if (close <= i + 2) return null;
    const inner = text.slice(i + 2, close);
    let node;
    if (inner === 'unknown link') node = { type: 'unknownLink' };
    else if (inner.startsWith('broken link: ')) node = { type: 'brokenLink', name: inner.slice(13) };
    else node = { type: 'link', name: inner };
    return [[node], close + 2];
  };

  const matchBracket = (i) => {
    const close = nextIndex(']', i + 1);
    if (close <= i + 1) return null;
    // An attachment can sit right before typed text: [image](see above).
    const isLabel = close - (i + 1) <= MAX_NAME_LENGTH && ATTACHMENT_LABELS.has(text.slice(i + 1, close));
    if (text[close + 1] === '(' && (!isLabel || URL_SCHEME.test(text.slice(close + 2, close + 2 + MAX_NAME_LENGTH)))) {
      const firstClose = nextIndex(')', close + 2);
      if (firstClose <= close + 2) return null;
      // Balance parentheses so .../Foo_(bar) survives; a URL that never balances
      // ends at its first closing parenthesis.
      let urlEnd = firstClose;
      let depth = 1;
      const limit = Math.min(text.length, close + 2 + URL_SCAN_MAX);
      for (let j = close + 2; j < limit; j++) {
        if (text[j] === '(') depth += 1;
        else if (text[j] === ')') depth -= 1;
        if (depth === 0) {
          urlEnd = j;
          break;
        }
      }
      return [[{ type: 'webLink', text: text.slice(i + 1, close), url: text.slice(close + 2, urlEnd) }], urlEnd + 1];
    }
    if (close - (i + 1) > MAX_NAME_LENGTH) return null;
    const inner = text.slice(i + 1, close);
    if (ATTACHMENT_LABELS.has(inner)) return [[{ type: 'attachment', label: inner }], close + 1];
    if (UTI.test(inner)) return [[{ type: 'attachment', label: 'file' }], close + 1];
    return null;
  };

  const match = (i) => {
    switch (text[i]) {
      case '*':
        return matchStars(i);
      case '~':
        return matchWrapped(i, '~~', '~~', (a, b) => ({ type: 'strike', children: nest(a, b) }));
      case '`':
        return matchWrapped(i, '`', '`', (a, b) => ({ type: 'code', text: text.slice(a, b) }));
      case '<':
        return matchWrapped(i, '<u>', '</u>', (a, b) => ({ type: 'underline', children: nest(a, b) }))
          ?? matchWrapped(i, '<b>', '</b>', (a, b) => ({ type: 'bold', children: nest(a, b) }));
      case '{':
        return matchColor(i) ?? matchTag(i);
      case '[':
        return matchNoteLink(i) ?? matchBracket(i);
      default:
        return null;
    }
  };

  let i = 0;
  let textStart = 0;
  while (i < text.length) {
    if (!MARKER_CHARS.has(text[i])) {
      i += 1;
      continue;
    }
    const hit = match(i);
    if (!hit) {
      i += 1;
      continue;
    }
    pushText(out, text.slice(textStart, i));
    pushAll(out, hit[0]);
    i = hit[1];
    textStart = i;
  }
  pushText(out, text.slice(textStart));
  return out;
}

export function plainText(inlines) {
  return inlines.map((inline) => {
    switch (inline.type) {
      case 'text':
      case 'code':
      case 'webLink':
        return inline.text;
      case 'link':
      case 'brokenLink':
        return inline.name;
      case 'unknownLink':
        return 'unknown link';
      case 'tag':
        return `#${inline.name}`;
      case 'attachment':
        return inline.label;
      default:
        return plainText(inline.children ?? []);
    }
  }).join('');
}

function listItem(line) {
  const indent = line.match(/^[\t ]*/)[0];
  const rest = line.slice(indent.length);
  const tabs = indent.split('\t').length - 1;
  const depth = tabs + Math.floor((indent.length - tabs) / INDENT_SPACES);

  const check = rest.match(/^- \[( |x)\] /);
  if (check) return { ordered: false, depth, checked: check[1] === 'x', body: rest.slice(check[0].length) };
  if (rest.startsWith('- ') || rest.startsWith('* ')) return { ordered: false, depth, body: rest.slice(2) };
  if (tabs > 0 && (rest.startsWith('• ') || rest.startsWith('∙ '))) return { ordered: false, depth, body: rest.slice(2) };
  const numbered = rest.match(/^(\d{1,9})\. /);
  if (numbered) return { ordered: true, depth, number: Number(numbered[1]), body: rest.slice(numbered[0].length) };
  return null;
}

const cells = (line) => line.trim().replace(/^\|/, '').replace(/\|$/, '').split('|').map((c) => parseInlines(c.trim()));

function isTableSeparator(line) {
  if (!line?.startsWith('|')) return false;
  const parts = line.trim().replace(/^\|/, '').replace(/\|$/, '').split('|');
  return parts.length > 0 && parts.every((p) => /^:?-+:?$/.test(p.trim()));
}

// The notation wraps a whole monospace line in backticks without escaping, so
// `echo `date`` is one code line, while `foo` and `bar` is two inline spans. The
// first inner backtick tells them apart: a closer follows text and isn't followed
// by a word character. A line ending in two backticks ends with a nested command,
// so it's code whatever the command starts with.
function isCodeLine(line) {
  if (line.length <= 2 || !line.startsWith('`') || !line.endsWith('`')) return false;
  const inner = line.indexOf('`', 1);
  if (inner === line.length - 1 || line.endsWith('``')) return true;
  const closesSpan = !isSpace(line[inner - 1]) && !WORD_CHAR.test(line[inner + 1]);
  return !closesSpan;
}

function titleLine(line) {
  const heading = line.match(/^#{1,3} /);
  return plainText(parseInlines(heading ? line.slice(heading[0].length) : line)).trim();
}

export function parseNote(text, { title } = {}) {
  if (!text) return [];
  const lines = text.split('\n');
  const blocks = [];
  let gap = false;

  const add = (block) => {
    if (gap && blocks.length > 0) block.gapBefore = true;
    gap = false;
    blocks.push(block);
    return block;
  };

  let start = 0;
  while (start < lines.length && !lines[start].trim()) start += 1;
  if (title && start < lines.length && titleLine(lines[start]) === title.trim()) start += 1;

  for (let i = start; i < lines.length; i++) {
    const line = lines[i];
    if (!line.trim()) {
      gap = true;
      continue;
    }

    const heading = line.match(/^(#{1,3}) (.*)$/);
    if (heading) {
      add({ type: 'heading', level: heading[1].length, inlines: parseInlines(heading[2]) });
      continue;
    }

    if (/^(-{3,}|\*{3,})\s*$/.test(line)) {
      add({ type: 'rule' });
      continue;
    }

    if (line.startsWith('|') && isTableSeparator(lines[i + 1])) {
      const table = add({ type: 'table', header: cells(line), rows: [] });
      i += 2;
      while (i < lines.length && lines[i].startsWith('|')) {
        table.rows.push(cells(lines[i]));
        i += 1;
      }
      i -= 1;
      continue;
    }

    const item = listItem(line);
    if (item) {
      let list = gap ? null : blocks[blocks.length - 1];
      if (list?.type !== 'list' || list.ordered !== item.ordered) {
        list = add({ type: 'list', ordered: item.ordered, items: [] });
      }
      const { body, ordered, ...rest } = item;
      list.items.push({ inlines: parseInlines(body), ...rest });
      continue;
    }

    if (line.startsWith('> ')) {
      add({ type: 'quote', inlines: parseInlines(line.slice(2)) });
      continue;
    }

    if (isCodeLine(line)) {
      const code = line.slice(1, -1);
      const last = blocks[blocks.length - 1];
      if (!gap && last?.type === 'code') last.text += `\n${code}`;
      else add({ type: 'code', text: code });
      continue;
    }

    add({ type: 'paragraph', inlines: parseInlines(line) });
  }
  return blocks;
}

// The note id a [[name]] in the preview should walk to, or null when the title
// doesn't match exactly one outgoing note. Duplicate titles exist in real data, so
// an ambiguous link must not guess, even when only one of the notes is visible. A
// note outside `visibleIds` (hidden by filters or focus) can't be walked to.
export function resolvePreviewLink(name, outgoingNotes, visibleIds) {
  const ids = new Set(outgoingNotes.filter((n) => n.name === name).map((n) => n.id));
  if (ids.size !== 1) return null;
  const [id] = ids;
  return !visibleIds || visibleIds.has(id) ? id : null;
}
