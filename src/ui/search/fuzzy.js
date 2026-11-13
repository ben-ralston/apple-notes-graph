// Subsequence fuzzy matcher for the ⌘K palette. Scoring rewards prefix
// matches, consecutive runs, and word-start hits; no dependencies.

const SCORE_CONSECUTIVE = 11;
const SCORE_WORD_START = 10;
const SCORE_PREFIX_BONUS = 15;
const PENALTY_GAP = -1;

function isWordStart(text, i) {
  if (i === 0) return true;
  const prev = text[i - 1];
  return prev === ' ' || prev === '-' || prev === '_' || prev === '/';
}

export function fuzzyMatch(query, text) {
  if (!query) return { score: 0, positions: [] };
  const q = query.toLowerCase();
  const t = text.toLowerCase();

  const positions = [];
  let score = 0;
  let ti = 0;

  for (let qi = 0; qi < q.length; qi++) {
    const found = t.indexOf(q[qi], ti);
    if (found === -1) return null;
    if (positions.length > 0) {
      const gap = found - positions[positions.length - 1] - 1;
      score += gap === 0 ? SCORE_CONSECUTIVE : PENALTY_GAP * Math.min(gap, 5);
    }
    if (isWordStart(text, found)) score += SCORE_WORD_START;
    positions.push(found);
    ti = found + 1;
  }

  if (positions[0] === 0) score += SCORE_PREFIX_BONUS;
  score -= positions[0]; // earlier first match is better
  return { score, positions };
}

export function fuzzySearch(query, items, key, limit = 8) {
  const results = [];
  for (const item of items) {
    const match = fuzzyMatch(query, key(item));
    if (match) results.push({ item, score: match.score, positions: match.positions });
  }
  results.sort((a, b) => b.score - a.score);
  return results.slice(0, limit);
}
