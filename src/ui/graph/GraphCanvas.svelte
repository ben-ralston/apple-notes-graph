<script>
  import { onMount } from 'svelte';
  import { get } from 'svelte/store';
  import { openInNotes } from '../openNote.js';
  import { quadtree as d3quadtree } from 'd3-quadtree';
  import { select as d3select } from 'd3-selection';
  import { zoom as d3zoom, zoomIdentity } from 'd3-zoom';
  import { drag as d3drag } from 'd3-drag';
  import 'd3-transition'; // registers selection.transition (smooth fly-to)
  import { visibleGraph, selection, selectNode, clearSelection, viewCommand, healthLens, healthOverlay, clusterNames, focus } from '../stores.js';
  import { createSimulation } from './simulation.js';
  import { CLUSTER_PALETTE, drawRadius, nodeBaseAlpha, nodeColor } from './colors.js';
  import { applyMoves, planMoves } from './tween.js';
  import { duplicateCount } from './health.js';
  import { placeLabels, placeRegionLabels } from './labels.js';
  import { centerTransform, fitTransform } from './view.js';
  import { inspectorWidth } from '../layout.js';
  import { extendTestHook } from '../testhook.js';

  const BG = '#0b0e1a';
  const LINK = 'rgba(76, 92, 138, 0.38)';
  const LINK_LIT = 'rgba(125, 211, 252, 0.7)';
  const DIM_ALPHA = 0.12;
  const LABEL = '#8b9bbd';
  const LABEL_LIT = '#e2e8f0';
  const LABEL_HALO = 'rgba(9, 12, 24, 0.85)';
  const REGION_ALPHA = 0.9;
  // Glow budget: strong glow is reserved for the selected node and idle hubs;
  // lit neighbors get a whisper so a 75-neighbor hub doesn't blow out.
  const GLOW_SELECTED = 20;
  const GLOW_NEIGHBOR = 5;
  const GLOW_HUB = 14;

  // Health lens styling
  const GHOST_LINK = 'rgba(248, 113, 113, 0.55)';
  const ORPHAN_PULSE = '#fbbf24';
  const STALE_COLOR = '#5c6f94'; // cooler, recedes
  const STALE_ALPHA = 0.5;
  const BADGE_BG = '#fbbf24';
  const BADGE_TEXT = '#1c1917';
  const PULSE_PERIOD_MS = 1600;

  let canvas;
  let ctx;
  let sim = null;
  let transform = zoomIdentity;
  let zoomBehavior = null;
  let hovered = null;
  let quadtree = null;
  let nodes = [], links = [];
  let needsDraw = false;

  // Selection dim/light transition: dimStrength eases 0 (nothing dimmed) <-> 1
  // (non-lit elements fully dimmed to DIM_ALPHA) over TRANSITION_MS, driven by
  // its own rAF loop so it animates independent of simulation ticks. `visualSel`
  // is what draw() actually lights/dims — it holds the outgoing selection during
  // a clear-out ease so links/nodes fade rather than snapping to "everything lit".
  const TRANSITION_MS = 250;
  let dimStrength = 0;
  let visualSel = { id: null, neighborIds: new Set() };
  let transitionFrom = 0, transitionTo = 0, transitionStart = 0;
  let transitioning = false, pendingClear = false;

  const easeCubicOut = (t) => 1 - (1 - t) ** 3;

  function startSelectionTransition(sel) {
    const target = sel.id !== null ? 1 : 0;
    if (sel.id !== null) visualSel = sel;
    pendingClear = sel.id === null;
    // Always repaint: switching A->B keeps dimStrength at 1 (no tween runs),
    // but visualSel changed and the frozen sim won't redraw for us.
    scheduleDraw();
    if (target === dimStrength && !transitioning) {
      if (pendingClear) visualSel = { id: null, neighborIds: new Set() };
      return;
    }
    transitionFrom = dimStrength;
    transitionTo = target;
    transitionStart = performance.now();
    if (!transitioning) {
      transitioning = true;
      requestAnimationFrame(stepSelectionTransition);
    }
  }

  function stepSelectionTransition(now) {
    const t = Math.min(1, (now - transitionStart) / TRANSITION_MS);
    dimStrength = transitionFrom + (transitionTo - transitionFrom) * easeCubicOut(t);
    draw();
    if (t < 1) {
      requestAnimationFrame(stepSelectionTransition);
    } else {
      transitioning = false;
      if (pendingClear) {
        // The draw() above still used the outgoing visualSel (needed for the
        // fade to look right) — clearing it then redraw once more so the
        // final on-screen frame doesn't retain a stale lit edge/label.
        visualSel = { id: null, neighborIds: new Set() };
        pendingClear = false;
        draw();
      }
    }
  }

  function scheduleDraw() {
    if (needsDraw) return;
    needsDraw = true;
    requestAnimationFrame(() => { needsDraw = false; draw(); });
  }

  // Positions move every tick (simulation forces, drag-perturbed neighbors via
  // collision), so the quadtree must be rebuilt every tick to stay accurate for
  // hit-testing (click/dblclick/hover/drag subject). Hundreds of nodes, O(n log n) — fine
  // at 60fps. `end` is a belt-and-suspenders rebuild for when the sim settles.
  function onTick() {
    quadtree = d3quadtree(nodes, (d) => d.x, (d) => d.y);
    scheduleDraw();
  }

  function rebuild(data) {
    nodes = data.notes;
    links = data.links;
    if (!canvas) return;
    sim?.stop();
    finishLayoutEase();
    if (nodes.length === 0) {
      sim = null;
      quadtree = null;
      scheduleDraw();
      return;
    }
    const before = new Map();
    for (const n of nodes) if (Number.isFinite(n.x)) before.set(n.id, { x: n.x, y: n.y });
    sim = createSimulation(nodes, links, canvas.clientWidth, canvas.clientHeight);
    sim.on('tick', onTick).on('end', onTick);
    // Fit against the final positions, then rewind for the ease toward them.
    if (pendingFit) fitToWindow();
    startLayoutEase(before);
    quadtree = d3quadtree(nodes, (d) => d.x, (d) => d.y);
    scheduleDraw();
  }

  // Layout changes (focus, filters, refresh) ease nodes to their new positions.
  const LAYOUT_EASE_MS = 450;
  const easeInOut = (t) => (t < 0.5 ? 4 * t ** 3 : 1 - (-2 * t + 2) ** 3 / 2);
  let easing = null;

  function startLayoutEase(before) {
    if (window.matchMedia?.('(prefers-reduced-motion: reduce)').matches) return;
    const moves = planMoves(nodes, before);
    if (moves.length === 0) return;
    const ease = { moves, start: performance.now() };
    easing = ease;
    const step = (now) => {
      if (easing !== ease) return;
      const t = Math.min(1, (now - ease.start) / LAYOUT_EASE_MS);
      applyMoves(moves, easeInOut(t));
      if (t < 1) {
        // Hit-testing has to follow the nodes, as it does for simulation ticks.
        quadtree = d3quadtree(nodes, (d) => d.x, (d) => d.y);
        draw();
        requestAnimationFrame(step);
      } else {
        finishLayoutEase();
        draw();
      }
    };
    requestAnimationFrame(step);
  }

  function finishLayoutEase() {
    if (!easing) return;
    applyMoves(easing.moves, 1);
    easing = null;
    quadtree = d3quadtree(nodes, (d) => d.x, (d) => d.y);
  }

  // The launch view and every focus enter/exit frame the whole visible graph.
  // Filter changes and refreshes keep the user's camera.
  const FIT_MS = 450;
  const PANEL_MARGIN = 16;
  let pendingFit = true;
  let hasFitted = false;
  let lastFocusRoot = null;

  function fitToWindow() {
    if (!zoomBehavior || !canvas) return;
    pendingFit = false;
    const width = canvas.clientWidth;
    // get(), not $selection: this runs inside the rebuild effect, and subscribing
    // would re-run the whole layout on every selection change.
    const rightInset = get(selection).id !== null ? inspectorWidth(width) : 0;
    const t = fitTransform(nodes, width, canvas.clientHeight, { rightInset });
    const target = zoomIdentity.translate(t.x, t.y).scale(t.k);
    if (hasFitted) {
      d3select(canvas).transition().duration(FIT_MS).call(zoomBehavior.transform, target);
    } else {
      d3select(canvas).call(zoomBehavior.transform, target);
    }
    hasFitted = true;
  }

  // Runs before the rebuild effect below, so the rebuild it triggers does the fit.
  $effect(() => {
    if ($focus.rootId !== lastFocusRoot) {
      lastFocusRoot = $focus.rootId;
      pendingFit = true;
    }
  });

  function rebuildWithGhostSeeding(data) {
    // New ghost nodes have no position yet; seed them beside their first
    // source so they fade in next to the note that references them instead
    // of flying across the field from the origin.
    for (const n of data.notes) {
      if (n.ghost && n.x === undefined) {
        const src = data.notes.find((s) => s.id === n.sources[0]);
        if (src?.x !== undefined) { n.x = src.x + 24; n.y = src.y + 24; }
      }
    }
    rebuild(data);
  }

  $effect(() => rebuildWithGhostSeeding($visibleGraph));
  $effect(() => startSelectionTransition($selection));
  // LLM cluster region labels (issue #117) — repaint when names arrive.
  let regionNames = {};
  $effect(() => { regionNames = $clusterNames; scheduleDraw(); });

  // Orphan pulse: while the lens is on, a dedicated rAF loop repaints so the
  // amber glow can breathe even with the simulation frozen.
  let lensActive = false;
  let overlay = { orphans: new Set(), stale: new Set(), dupCounts: new Map() };
  let pulsing = false;
  function pulseLoop() {
    if (!lensActive) { pulsing = false; return; }
    draw();
    requestAnimationFrame(pulseLoop);
  }
  $effect(() => {
    lensActive = $healthLens;
    overlay = $healthOverlay;
    if (lensActive && !pulsing) { pulsing = true; requestAnimationFrame(pulseLoop); }
    if (!lensActive) scheduleDraw(); // repaint once to drop lens styling
  });

  // Fly the viewport to a node (⌘K jump). Goes through the zoom behavior so
  // its internal state stays in sync — a later wheel/pan continues from here.
  function centerOn(id) {
    const n = nodes.find((x) => x.id === id);
    if (!n || !canvas || !zoomBehavior) return; // e.g. note hidden by filters
    const width = canvas.clientWidth;
    const t = centerTransform(n, transform.k, width, canvas.clientHeight, inspectorWidth(width));
    d3select(canvas)
      .transition()
      .duration(500)
      .call(zoomBehavior.transform, zoomIdentity.translate(t.x, t.y).scale(t.k));
  }

  $effect(() => {
    const cmd = $viewCommand;
    if (cmd?.type === 'center') centerOn(cmd.id);
  });

  // The visible part of the graph, in graph units. `rightInset` screen px are
  // hidden behind the Inspector.
  function graphViewport(rightInset = 0) {
    const [x0, y0] = transform.invert([0, 0]);
    const [x1, y1] = transform.invert([canvas.clientWidth - rightInset, canvas.clientHeight]);
    return { x0, y0, x1, y1 };
  }

  // Background vignette: a faint lift at the center falling to darker edges
  // gives the field depth. Cached per canvas size — no per-frame allocation.
  let bgGradient = null, bgGradientKey = '';
  function backgroundGradient(w, h) {
    const key = `${w}x${h}`;
    if (bgGradientKey !== key) {
      bgGradient = ctx.createRadialGradient(w / 2, h * 0.42, 0, w / 2, h * 0.42, Math.hypot(w, h) * 0.6);
      bgGradient.addColorStop(0, '#0e1226');
      bgGradient.addColorStop(0.55, BG);
      bgGradient.addColorStop(1, '#070910');
      bgGradientKey = key;
    }
    return bgGradient;
  }

  // Text widths in screen px, cached per string: measuring every label on every
  // frame would dominate the frame budget.
  let lastLabelIds = new Map();
  const widthCache = new Map();
  function textWidth(font, text) {
    const key = `${font}|${text}`;
    let width = widthCache.get(key);
    if (width === undefined) {
      ctx.save();
      ctx.setTransform(1, 0, 0, 1, 0, 0);
      ctx.font = font;
      width = ctx.measureText(text).width;
      ctx.restore();
      widthCache.set(key, width);
    }
    return width;
  }
  const measureLabel = (text) => textWidth('11px -apple-system, sans-serif', text);
  const measureRegion = (text) => textWidth('600 15px -apple-system, sans-serif', text);

  function draw() {
    if (!ctx) return;
    const dpr = window.devicePixelRatio || 1;
    const w = canvas.clientWidth, h = canvas.clientHeight;
    if (canvas.width !== w * dpr || canvas.height !== h * dpr) { canvas.width = w * dpr; canvas.height = h * dpr; }
    ctx.setTransform(dpr, 0, 0, dpr, 0, 0);
    ctx.fillStyle = backgroundGradient(w, h);
    ctx.fillRect(0, 0, w, h);
    ctx.translate(transform.x, transform.y);
    ctx.scale(transform.k, transform.k);

    const sel = visualSel;
    const hasSelection = sel.id !== null;
    const isLit = (id) => id === sel.id || sel.neighborIds.has(id);
    const nonDimAlpha = 1 - dimStrength * (1 - DIM_ALPHA);

    // links (broken links render red + dashed under the health lens)
    for (const l of links) {
      const lit = hasSelection && (isLit(l.source.id) && isLit(l.target.id))
        && (l.source.id === sel.id || l.target.id === sel.id);
      ctx.globalAlpha = hasSelection && !lit ? nonDimAlpha : 1;
      if (l.ghost) {
        ctx.strokeStyle = GHOST_LINK;
        ctx.setLineDash([5 / transform.k, 4 / transform.k]);
      } else {
        ctx.strokeStyle = lit ? LINK_LIT : LINK;
      }
      ctx.lineWidth = (lit ? 1.6 : 0.8) / transform.k;
      ctx.beginPath();
      ctx.moveTo(l.source.x, l.source.y);
      ctx.lineTo(l.target.x, l.target.y);
      ctx.stroke();
      if (l.ghost) ctx.setLineDash([]);
    }

    // nodes (glow pass for lit/hub nodes, flat pass otherwise)
    const pulse = lensActive
      ? 0.5 + 0.5 * Math.sin(((performance.now() % PULSE_PERIOD_MS) / PULSE_PERIOD_MS) * 2 * Math.PI)
      : 0;
    for (const n of nodes) {
      const r = drawRadius(n, transform.k);
      const lit = !hasSelection || isLit(n.id);
      let base = nodeBaseAlpha(n);
      let color = nodeColor(n);
      const isOrphan = lensActive && overlay.orphans.has(n.id);
      const isStale = lensActive && !isOrphan && overlay.stale.has(n.id);
      if (isOrphan) { color = ORPHAN_PULSE; base = 0.55 + 0.35 * pulse; }
      else if (isStale) { color = STALE_COLOR; base *= STALE_ALPHA; }
      // Lit nodes ease from their resting alpha to full luminance with the dim.
      ctx.globalAlpha = lit ? base + (1 - base) * dimStrength : base * nonDimAlpha;
      const isSelected = n.id === sel.id;
      // Selection-driven glow/enlargement scale with dimStrength so they ease
      // in and out with the dim instead of snapping when visualSel empties.
      let blur = 0;
      if (hasSelection && isLit(n.id)) blur = (isSelected ? GLOW_SELECTED : GLOW_NEIGHBOR) * dimStrength;
      else if (!hasSelection && n.connections >= 8) blur = GLOW_HUB;
      if (isOrphan) blur = Math.max(blur, 4 + 10 * pulse);
      ctx.shadowBlur = blur;
      ctx.shadowColor = color;
      const drawR = isSelected ? r * (1 + 0.25 * dimStrength) : r;
      ctx.beginPath();
      ctx.arc(n.x, n.y, drawR, 0, 2 * Math.PI);
      if (n.ghost) {
        // Ghosts are hollow dashed rings: a placeholder, not a note.
        ctx.strokeStyle = color;
        ctx.lineWidth = 1.4 / transform.k;
        ctx.setLineDash([3 / transform.k, 2.5 / transform.k]);
        ctx.stroke();
        ctx.setLineDash([]);
      } else {
        ctx.fillStyle = color;
        ctx.fill();
      }
      ctx.shadowBlur = 0;
      if (isSelected && dimStrength > 0) {
        // Focus ring: a thin halo just outside the selected node.
        ctx.globalAlpha = 0.45 * dimStrength;
        ctx.strokeStyle = color;
        ctx.lineWidth = 1.5 / transform.k;
        ctx.beginPath();
        ctx.arc(n.x, n.y, r * (1 + 0.25 * dimStrength) + 3.5 / transform.k, 0, 2 * Math.PI);
        ctx.stroke();
      }
    }

    // duplicate-title badges (health lens): a small amber count pinned to
    // the node's upper right.
    if (lensActive && overlay.dupCounts.size > 0) {
      const br = 5.5 / transform.k;
      ctx.font = `700 ${8 / transform.k}px -apple-system, sans-serif`;
      ctx.textAlign = 'center';
      ctx.textBaseline = 'middle';
      for (const n of nodes) {
        const dupCount = n.ghost ? 0 : duplicateCount(overlay.dupCounts, n.name);
        if (dupCount === 0) continue;
        ctx.globalAlpha = hasSelection && !isLit(n.id) ? nonDimAlpha : 1;
        const r = drawRadius(n, transform.k);
        const bx = n.x + r * 0.8 + br, by = n.y - r * 0.8 - br;
        ctx.fillStyle = BADGE_BG;
        ctx.beginPath();
        ctx.arc(bx, by, br, 0, 2 * Math.PI);
        ctx.fill();
        // Dark ring so the badge stays legible on amber (pulsing orphan) nodes.
        ctx.strokeStyle = BG;
        ctx.lineWidth = 1.5 / transform.k;
        ctx.stroke();
        ctx.fillStyle = BADGE_TEXT;
        ctx.fillText(String(dupCount), bx, by + 0.5 / transform.k);
      }
      ctx.textAlign = 'start';
      ctx.textBaseline = 'alphabetic';
    }

    // Cluster region labels (LLM-named). Zoomed out they name the map's
    // continents; they fade as node labels take over (k -> 1.3) and with a selection.
    const REGION_LABEL_MAX_K = 1.3;
    const regionFade = Math.max(0, Math.min(1, (REGION_LABEL_MAX_K - transform.k) / 0.5));
    let regions = [];
    if (regionFade > 0 && Object.keys(regionNames).length > 0) {
      regions = placeRegionLabels(nodes, regionNames, transform.k, { measure: measureRegion });
      ctx.textAlign = 'center';
      ctx.textBaseline = 'middle';
      ctx.font = `600 ${15 / transform.k}px -apple-system, sans-serif`;
      ctx.strokeStyle = LABEL_HALO;
      ctx.lineWidth = 4 / transform.k;
      ctx.lineJoin = 'round';
      ctx.globalAlpha = REGION_ALPHA * regionFade * nonDimAlpha;
      for (const r of regions) {
        ctx.strokeText(r.name, r.x, r.y);
        ctx.fillStyle = CLUSTER_PALETTE[r.clusterId % CLUSTER_PALETTE.length];
        ctx.fillText(r.name, r.x, r.y);
      }
      ctx.globalAlpha = 1;
      ctx.textAlign = 'start';
      ctx.textBaseline = 'alphabetic';
    }

    // Node labels (dark halo keeps them legible over links). With a selection,
    // only lit notes are candidates, so all of its neighbours get a chance.
    const pinned = [];
    if (hovered) pinned.push(hovered.id);
    if (sel.id !== null) pinned.push(sel.id);
    const showRegions = regionFade > 0.05 && !hasSelection;
    const placed = placeLabels(
      hasSelection ? nodes.filter((n) => isLit(n.id) || n.id === hovered?.id) : nodes,
      transform.k,
      graphViewport(sel.id !== null ? inspectorWidth(w) + PANEL_MARGIN : 0),
      {
        measure: measureLabel,
        pinned,
        reserved: showRegions ? regions.map((r) => r.rect) : [],
        minConnections: hasSelection ? 0 : undefined,
        radius: (n) => drawRadius(n, transform.k),
      },
    );
    lastLabelIds = placed;
    ctx.globalAlpha = 1;
    ctx.strokeStyle = LABEL_HALO;
    ctx.lineWidth = 3 / transform.k;
    ctx.lineJoin = 'round';
    const drawLabel = (l) => {
      ctx.textAlign = l.side === 'left' ? 'right' : 'left';
      ctx.strokeText(l.text, l.x, l.y);
      ctx.fillText(l.text, l.x, l.y);
    };
    ctx.font = `${11 / transform.k}px -apple-system, sans-serif`;
    ctx.fillStyle = LABEL;
    for (const [id, l] of placed) if (!pinned.includes(id)) drawLabel(l);
    if (pinned.length > 0) {
      ctx.font = `600 ${11 / transform.k}px -apple-system, sans-serif`;
      ctx.fillStyle = LABEL_LIT;
      for (const id of pinned) if (placed.has(id)) drawLabel(placed.get(id));
    }
    ctx.textAlign = 'start';
    ctx.setTransform(1, 0, 0, 1, 0, 0);
  }

  function nodeAt(clientX, clientY) {
    if (!quadtree) return null;
    const rect = canvas.getBoundingClientRect();
    const [gx, gy] = transform.invert([clientX - rect.left, clientY - rect.top]);
    const found = quadtree.find(gx, gy, 18 / transform.k);
    return found ?? null;
  }

  onMount(() => {
    ctx = canvas.getContext('2d');
    const selection_ = d3select(canvas);

    zoomBehavior = d3zoom().scaleExtent([0.15, 5])
      .filter((e) => !e.button && (e.type === 'wheel' || !nodeAt(e.clientX ?? 0, e.clientY ?? 0)))
      .on('zoom', (e) => { transform = e.transform; scheduleDraw(); });
    selection_.call(zoomBehavior);

    selection_.call(
      d3drag()
        .subject((e) => nodeAt(e.sourceEvent.clientX, e.sourceEvent.clientY))
        .on('start', (e) => { finishLayoutEase(); if (sim) sim.alphaTarget(0.3).restart(); e.subject.fx = e.subject.x; e.subject.fy = e.subject.y; })
        .on('drag', (e) => {
          const rect = canvas.getBoundingClientRect();
          const [gx, gy] = transform.invert([e.sourceEvent.clientX - rect.left, e.sourceEvent.clientY - rect.top]);
          e.subject.fx = gx; e.subject.fy = gy;
        })
        .on('end', (e) => { e.subject.fx = null; e.subject.fy = null; if (sim) sim.alphaTarget(0); })
    );

    canvas.addEventListener('click', (e) => {
      const n = nodeAt(e.clientX, e.clientY);
      if (n) selectNode(n.id); else clearSelection();
    });
    canvas.addEventListener('dblclick', (e) => {
      const n = nodeAt(e.clientX, e.clientY);
      // Ghosts are missing notes — there is nothing to open in Apple Notes.
      if (n && !n.ghost) openInNotes(n.name);
    });
    canvas.addEventListener('pointermove', (e) => {
      const n = nodeAt(e.clientX, e.clientY);
      if (n !== hovered) { hovered = n; canvas.style.cursor = n ? 'pointer' : 'default'; scheduleDraw(); }
    });
    window.addEventListener('resize', scheduleDraw);

    extendTestHook({
      nodes: () => nodes.map((n) => ({ id: n.id, name: n.name, x: n.x, y: n.y, connections: n.connections, cluster: n.cluster, ghost: n.ghost ?? false })),
      select: (name) => {
        const n = nodes.find((x) => x.name === name) ?? nodes.find((x) => x.name.includes(name));
        if (!n) return false;
        selectNode(n.id);
        return true;
      },
      selection: () => {
        const s = $selection;
        return { id: s.id, neighborIds: [...s.neighborIds] };
      },
      view: () => ({ x: transform.x, y: transform.y, k: transform.k }),
      clusterLabels: () => ({ ...regionNames }),
      labels: () => [...lastLabelIds.keys()],
    });
  });
</script>

<canvas bind:this={canvas}></canvas>

<style>
  canvas { position: fixed; inset: 0; width: 100vw; height: 100vh; display: block; }
</style>
