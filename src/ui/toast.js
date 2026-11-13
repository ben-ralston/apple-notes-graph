import { writable } from 'svelte/store';

// Transient progress pills (bottom-right). Long async work (cluster naming,
// add-link) shows a spinner toast, then flips to a done/fail message that
// auto-expires.
export const toasts = writable([]);
let nextId = 1;

export function showToast(text, { spinner = false } = {}) {
  const id = nextId++;
  toasts.update((list) => [...list, { id, text, spinner }]);
  return id;
}

export function updateToast(id, text, { spinner = false, ttlMs = 4000 } = {}) {
  toasts.update((list) => list.map((t) => (t.id === id ? { ...t, text, spinner } : t)));
  if (ttlMs) setTimeout(() => dismissToast(id), ttlMs);
}

export function dismissToast(id) {
  toasts.update((list) => list.filter((t) => t.id !== id));
}
