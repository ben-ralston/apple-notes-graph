// Node position continuity: refreshes keep notes where they were, and layout
// changes ease nodes to their new positions instead of snapping.

const MOVED_EPSILON = 0.5;

// A refresh brings fresh note objects. Start them where they were by id, so the
// map doesn't reshuffle when a link is added or a reorganisation is reloaded.
export function seedPositions(nextNotes, previousNotes) {
  const previous = new Map(previousNotes.map((n) => [n.id, n]));
  for (const n of nextNotes) {
    const old = previous.get(n.id);
    if (Number.isFinite(old?.x) && Number.isFinite(old?.y)) {
      n.x = old.x;
      n.y = old.y;
      if (old.onRing) n.onRing = true;
    }
  }
}

// Rewinds laid-out nodes to where they were and returns what applyMoves needs to
// bring them back. Notes with no earlier position or no real move are left alone.
export function planMoves(nodes, before) {
  const moves = [];
  for (const n of nodes) {
    const from = before.get(n.id);
    if (!from) continue;
    if (Math.hypot(n.x - from.x, n.y - from.y) < MOVED_EPSILON) continue;
    moves.push({ node: n, fromX: from.x, fromY: from.y, toX: n.x, toY: n.y });
    n.x = from.x;
    n.y = from.y;
  }
  return moves;
}

export function applyMoves(moves, t) {
  for (const m of moves) {
    m.node.x = m.fromX + (m.toX - m.fromX) * t;
    m.node.y = m.fromY + (m.toY - m.fromY) * t;
  }
}
