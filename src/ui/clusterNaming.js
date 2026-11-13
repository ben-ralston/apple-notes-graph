import { get } from 'svelte/store';
import { assembleClusters, pruneCache, splitCached } from './graph/clusterNames.js';
import { clusterNames, graph } from './stores.js';
import { dismissToast, showToast, updateToast } from './toast.js';

const STORAGE_KEY = 'notes-graph:cluster-names:v1';

function loadCache() {
  try {
    return JSON.parse(localStorage.getItem(STORAGE_KEY)) ?? {};
  } catch {
    return {};
  }
}

let namingSeq = 0;

// Auto-fires after every graph load/refresh. Cache hits label immediately;
// only unnamed member-sets cost a claude -p call. Any failure = clusters
// simply stay unlabeled (degradation rule).
export async function refreshClusterNames() {
  const seq = ++namingSeq;
  const clusters = assembleClusters(get(graph).notes);
  const cache = loadCache();
  const presentKeys = clusters.map((c) => c.key);
  const { named, unnamed } = splitCached(clusters, cache);
  clusterNames.set(named);

  if (unnamed.length === 0) {
    localStorage.setItem(STORAGE_KEY, JSON.stringify(pruneCache(cache, presentKeys)));
    return;
  }

  const toastId = showToast('Naming clusters…', { spinner: true });
  const res = await window.notesAPI.nameClusters({
    clusters: unnamed.map(({ key, members }) => ({ key, members })),
  });
  if (seq !== namingSeq) { dismissToast(toastId); return; }
  if (res?.error || !res?.names) { dismissToast(toastId); return; }

  const pruned = pruneCache({ ...cache, ...res.names }, presentKeys);
  localStorage.setItem(STORAGE_KEY, JSON.stringify(pruned));
  const labels = { ...named };
  for (const c of unnamed) if (pruned[c.key]) labels[c.id] = pruned[c.key];
  clusterNames.set(labels);
  const n = Object.keys(res.names).length;
  updateToast(toastId, `Named ${n} cluster${n === 1 ? '' : 's'}`, { ttlMs: 3500 });
}
