// Viewport math for programmatic navigation (⌘K jump-to-note, fit to window).

// Minimum zoom when flying to a note: close enough that the note and its
// immediate neighborhood read clearly. If the user is already zoomed in past
// this, keep their zoom.
export const JUMP_ZOOM = 1.5;

// d3-zoom transform {k, x, y} that puts `node` at the centre of the canvas area
// left of `rightInset` (the Inspector, when open).
// screen = graph * k + (x, y)  ⇒  x = w/2 - node.x * k
export function centerTransform(node, currentK, width, height, rightInset = 0) {
  const k = Math.max(currentK, JUMP_ZOOM);
  return {
    k,
    x: (width - rightInset) / 2 - node.x * k,
    y: height / 2 - node.y * k,
  };
}

// d3-zoom transform that shows every positioned node inside the padded canvas
// area left of `rightInset`, centred there.
export function fitTransform(nodes, width, height, { padding = 64, minK = 0.15, maxK = 1.5, rightInset = 0 } = {}) {
  const placed = nodes.filter((n) => Number.isFinite(n.x) && Number.isFinite(n.y));
  if (placed.length === 0) return { k: 1, x: 0, y: 0 };

  const xs = placed.map((n) => n.x);
  const ys = placed.map((n) => n.y);
  const minX = Math.min(...xs);
  const maxX = Math.max(...xs);
  const minY = Math.min(...ys);
  const maxY = Math.max(...ys);

  const areaWidth = Math.max(width - rightInset, 0);
  const availableX = Math.max(areaWidth - padding * 2, 1);
  const availableY = Math.max(height - padding * 2, 1);
  const scaleX = maxX > minX ? availableX / (maxX - minX) : maxK;
  const scaleY = maxY > minY ? availableY / (maxY - minY) : maxK;
  const k = Math.min(maxK, Math.max(minK, Math.min(scaleX, scaleY)));

  return {
    k,
    x: areaWidth / 2 - ((minX + maxX) / 2) * k,
    y: height / 2 - ((minY + maxY) / 2) * k,
  };
}
