// Deterministic synthetic graph for layout and label tests. Shape follows real
// data: each cluster is a hub with spokes, every hub links to one root hub, and
// `bridges` extra links cross clusters at random. No network, no real notes.

function lcg(seed) {
  let s = seed >>> 0;
  return () => {
    s = (Math.imul(s, 1664525) + 1013904223) >>> 0;
    return s / 2 ** 32;
  };
}

export function makeGraph({ clusters = 12, size = 20, bridges = 30, unlinked = 40, seed = 7 } = {}) {
  const rand = lcg(seed);
  const nodes = [];
  const links = [];
  let nextId = 1;
  const node = (cluster, name) => {
    const n = { id: nextId++, name, cluster, connections: 0 };
    nodes.push(n);
    return n;
  };
  const link = (a, b) => {
    links.push({ source: a.id, target: b.id });
    a.connections += 1;
    b.connections += 1;
  };

  const root = clusters > 0 ? node(0, 'Root hub') : null;
  const members = [];
  for (let c = 0; c < clusters; c++) {
    const hub = c === 0 ? root : node(c, `Hub ${c}`);
    if (c !== 0) link(root, hub);
    const group = [hub];
    for (let i = 0; i < size - 1; i++) {
      const m = node(c, `Cluster ${c} note ${i}`);
      link(hub, m);
      group.push(m);
    }
    members.push(group);
  }
  for (let i = 0; i < bridges && clusters > 1; i++) {
    const a = Math.floor(rand() * clusters);
    const b = (a + 1 + Math.floor(rand() * (clusters - 1))) % clusters;
    const from = members[a][Math.floor(rand() * members[a].length)];
    const to = members[b][Math.floor(rand() * members[b].length)];
    link(from, to);
  }
  for (let i = 0; i < unlinked; i++) node(-1, `Unlinked note ${i}`);
  return { nodes, links };
}
