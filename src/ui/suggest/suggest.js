// Pure helpers behind LinkSuggestions.svelte (vitest-covered).

const REASON_LABELS = {
  forward_mention: 'you mention it',
  reverse_mention: 'mentions you',
  co_citation: 'co-cited',
  title_overlap: 'title overlap',
  same_folder: 'same folder',
  backlink_not_reciprocated: 'links to you',
};

export function reasonChips(reasons) {
  return (reasons ?? []).map((r) => REASON_LABELS[r.type] ?? r.type).filter(Boolean);
}

// LLM picks float to the top (in pick order, with explanations); everything
// else keeps recall order below them. LLM failure = picks undefined = pure
// recall order. Cards are never removed by ranking.
export function mergeRanking(candidates, picks) {
  const byName = new Map(candidates.map((c) => [c.name, c]));
  const picked = [];
  for (const p of picks ?? []) {
    const c = byName.get(p.name);
    if (!c || picked.some((x) => x.name === p.name)) continue;
    picked.push({ ...c, ranked: true, reason: p.reason });
  }
  const pickedNames = new Set(picked.map((c) => c.name));
  const rest = candidates
    .filter((c) => !pickedNames.has(c.name))
    .map((c) => ({ ...c, ranked: false }));
  return [...picked, ...rest];
}
