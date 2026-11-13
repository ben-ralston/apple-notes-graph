// Health-lens derivations over the extract payload's health block.
// Pure functions — no store or canvas dependencies.

// Ghost ids are strings so they can never collide with real note pks.
export function ghostId(title) {
  return `ghost:${title}`;
}

export function healthSets(health) {
  return {
    orphans: new Set(health?.orphans ?? []),
    stale: new Set(health?.stale ?? []),
    dupCounts: new Map((health?.duplicate_titles ?? []).map((d) => [d.name, d.count])),
  };
}

// duplicate_titles names arrive lowercased (find_duplicate_names groups
// case-insensitively); nodes carry display casing — normalize on lookup.
export function duplicateCount(dupCounts, noteName) {
  return dupCounts.get(noteName.toLowerCase()) ?? 0;
}

// One ghost node per missing title, one edge per distinct source. Sources
// attach by source_pk; name resolution (first pk match) is a fallback for
// older payloads without it. Unresolvable entries are dropped.
export function deriveGhosts(notes, brokenLinks) {
  const pkByName = new Map();
  for (const n of notes) {
    if (!pkByName.has(n.name)) pkByName.set(n.name, n.id);
  }

  const ghosts = new Map(); // identity key -> ghost node
  const ghostLinks = [];
  for (const b of brokenLinks ?? []) {
    const sourcePk = b.source_pk ?? pkByName.get(b.source_name);
    if (sourcePk === undefined || sourcePk === null) continue;
    // Untitled broken links (no alt text) must not collapse into one
    // "ghost:null" — fall back to the target uuid for identity and label.
    const key = b.intended_target_title ?? `uuid:${b.target_uuid}`;
    let ghost = ghosts.get(key);
    if (!ghost) {
      ghost = {
        id: ghostId(key),
        name: b.intended_target_title
          ?? `Untitled (${String(b.target_uuid ?? 'unknown').slice(0, 8)})`,
        ghost: true,
        connections: 0,
        sources: [],
      };
      ghosts.set(key, ghost);
    }
    if (!ghost.sources.includes(sourcePk)) {
      ghost.sources.push(sourcePk);
      ghost.connections = ghost.sources.length;
      ghostLinks.push({ source: sourcePk, target: ghost.id, ghost: true });
    }
  }
  return { ghostNodes: [...ghosts.values()], ghostLinks };
}

// Summary-chip counts, scoped to the notes currently on screen so the chip
// agrees with what the lens actually lights up (the payload health block is
// computed over ALL notes, before folder filters).
export function visibleHealthCounts(visibleNotes, overlay) {
  const ids = new Set(visibleNotes.map((n) => n.id));
  return {
    orphans: visibleNotes.filter((n) => overlay.orphans.has(n.id)).length,
    broken: overlay.ghostLinks.filter((l) => ids.has(l.source.id ?? l.source)).length,
    stale: visibleNotes.filter((n) => overlay.stale.has(n.id)).length,
  };
}
