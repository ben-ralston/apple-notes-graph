// BFS neighborhood for focus mode. Links may be raw ({source: id}) or
// d3-force-mutated ({source: {id}}), so normalize both.
const endpointId = (endpoint) => endpoint.id ?? endpoint;

export function neighborhood(links, rootId, hops) {
  const noteIds = new Set([rootId]);
  let frontier = new Set([rootId]);
  for (let hop = 0; hop < hops; hop++) {
    const next = new Set();
    for (const l of links) {
      const s = endpointId(l.source), t = endpointId(l.target);
      if (frontier.has(s) && !noteIds.has(t)) next.add(t);
      if (frontier.has(t) && !noteIds.has(s)) next.add(s);
    }
    next.forEach((id) => noteIds.add(id));
    frontier = next;
  }
  return { noteIds };
}
