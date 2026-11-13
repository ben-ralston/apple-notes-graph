// Merges async semantic hits under the sync fuzzy results (⌘K palette).
// Semantic is additive: dedup against fuzzy, drop pks no longer in the graph.
export function mergeSemantic(fuzzyResults, semanticResults, notes) {
  const byId = new Map(notes.map((n) => [n.id, n]));
  const fuzzyIds = new Set(
    fuzzyResults.map((r) => r.item.id).filter((id) => id !== undefined)
  );
  const out = [];
  for (const s of semanticResults ?? []) {
    if (fuzzyIds.has(s.pk)) continue;
    const item = byId.get(s.pk);
    if (!item) continue;
    out.push({ item, semantic: true, score: s.score, positions: [] });
  }
  return out;
}
