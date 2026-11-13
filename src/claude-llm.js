'use strict';
// Prompt builders + response parsers for the headless claude -p calls.
// Parsers validate against the input set so hallucinated names/keys are dropped.

const MAX_PICKS = 5;
const MAX_LABEL_CHARS = 40;
const MAX_REASON_CHARS = 200;

function extractJson(text) {
  if (typeof text !== 'string') return null;
  const fenced = text.match(/```(?:json)?\s*([\s\S]*?)```/);
  const source = fenced ? fenced[1] : text;
  const start = source.indexOf('{');
  if (start === -1) return null;
  let depth = 0, inString = false, escape = false;
  for (let i = start; i < source.length; i++) {
    const ch = source[i];
    if (escape) { escape = false; continue; }
    if (ch === '\\') { escape = inString; continue; }
    if (ch === '"') { inString = !inString; continue; }
    if (inString) continue;
    if (ch === '{') depth++;
    else if (ch === '}') {
      depth--;
      if (depth === 0) {
        try { return JSON.parse(source.slice(start, i + 1)); } catch { return null; }
      }
    }
  }
  return null;
}

function buildRankPrompt({ noteName, noteFolder, noteContent }, candidates) {
  const slim = candidates.map((c) => ({
    name: c.name,
    folder: c.folder,
    reasons: (c.reasons ?? []).map((r) => r.type ?? String(r)),
  }));
  return [
    'You are helping link related notes in a personal Apple Notes knowledge base.',
    '',
    `SUBJECT NOTE: ${JSON.stringify(noteName)} (folder: ${JSON.stringify(noteFolder ?? '')})`,
    'CONTENT (may be truncated):',
    noteContent ?? '',
    '',
    'CANDIDATES (JSON array; "reasons" are mechanical recall signals, not judgments):',
    JSON.stringify(slim),
    '',
    `Pick up to ${MAX_PICKS} candidates genuinely worth linking from the subject note, best first.`,
    'Respond with ONLY this JSON object - no markdown fences, no commentary:',
    '{"picks":[{"name":"<exact candidate name>","reason":"<why, one sentence, max 120 chars>"}]}',
    'Use only names that appear in CANDIDATES. If nothing is worth linking: {"picks":[]}',
  ].join('\n');
}

function parseRankResponse(text, candidateNames) {
  const parsed = extractJson(text);
  if (!parsed || !Array.isArray(parsed.picks)) return null;
  const valid = new Set(candidateNames);
  const picks = [];
  for (const p of parsed.picks) {
    if (!p || typeof p.name !== 'string' || !valid.has(p.name)) continue;
    if (picks.some((q) => q.name === p.name)) continue;
    picks.push({
      name: p.name,
      reason: typeof p.reason === 'string' ? p.reason.slice(0, MAX_REASON_CHARS) : '',
    });
    if (picks.length === MAX_PICKS) break;
  }
  return { picks };
}

function buildClusterPrompt(clusters) {
  return [
    "These are groups of connected notes from one person's Apple Notes.",
    'For each group, produce a short descriptive label: 2-4 words, Title Case,',
    'no punctuation, no quotes, each label distinct from the others.',
    '',
    'GROUPS (JSON; members listed most-connected first):',
    JSON.stringify(clusters.map((c) => ({ key: c.key, members: c.members }))),
    '',
    'Respond with ONLY this JSON object - no markdown fences, no commentary:',
    '{"names":{"<key>":"<label>", ...}}',
    'Include every key exactly once.',
  ].join('\n');
}

function parseClusterResponse(text, validKeys) {
  const parsed = extractJson(text);
  if (!parsed || typeof parsed.names !== 'object' || parsed.names === null) return null;
  const names = {};
  for (const key of validKeys) {
    const v = parsed.names[key];
    if (typeof v === 'string' && v.trim()) names[key] = v.trim().slice(0, MAX_LABEL_CHARS);
  }
  return { names };
}

module.exports = {
  extractJson, buildRankPrompt, parseRankResponse, buildClusterPrompt, parseClusterResponse,
};
