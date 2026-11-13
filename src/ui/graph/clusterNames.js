// Cluster identity + naming-prompt assembly (issue #117).
//
// Cluster ids from Python are dense positional indexes - deterministic for a
// fixed graph but renumbered whenever membership shifts. The name cache
// therefore keys on a hash of the member pk set, which survives renumbering.

export function clusterKey(memberPks) {
  const joined = [...memberPks].sort((a, b) => a - b).join(',');
  let h = 0x811c9dc5; // FNV-1a 32-bit
  for (let i = 0; i < joined.length; i++) {
    h ^= joined.charCodeAt(i);
    h = Math.imul(h, 0x01000193) >>> 0;
  }
  return h.toString(16).padStart(8, '0');
}

export function assembleClusters(notes, { minSize = 3, maxClusters = 12, maxMembers = 15 } = {}) {
  const byCluster = new Map();
  for (const n of notes) {
    if (n.cluster === -1 || n.cluster == null) continue;
    if (!byCluster.has(n.cluster)) byCluster.set(n.cluster, []);
    byCluster.get(n.cluster).push(n);
  }
  return [...byCluster.entries()]
    .filter(([, members]) => members.length >= minSize)
    .sort((a, b) => b[1].length - a[1].length)
    .slice(0, maxClusters)
    .map(([id, members]) => ({
      id,
      key: clusterKey(members.map((m) => m.id)),
      size: members.length,
      members: [...members]
        .sort((a, b) => (b.connections - a.connections) || a.name.localeCompare(b.name))
        .slice(0, maxMembers)
        .map((m) => m.name),
    }));
}

export function clusterCentroids(nodes) {
  const acc = new Map();
  for (const n of nodes) {
    if (n.cluster === -1 || n.cluster == null || n.ghost) continue;
    if (typeof n.x !== 'number' || typeof n.y !== 'number') continue;
    const a = acc.get(n.cluster) ?? { x: 0, y: 0, count: 0 };
    a.x += n.x; a.y += n.y; a.count += 1;
    acc.set(n.cluster, a);
  }
  for (const a of acc.values()) { a.x /= a.count; a.y /= a.count; }
  return acc;
}

export function splitCached(clusters, cache) {
  const named = {};
  const unnamed = [];
  for (const c of clusters) {
    if (cache[c.key]) named[c.id] = cache[c.key];
    else unnamed.push(c);
  }
  return { named, unnamed };
}

export function pruneCache(cache, presentKeys) {
  const out = {};
  for (const k of presentKeys) if (cache[k]) out[k] = cache[k];
  return out;
}
