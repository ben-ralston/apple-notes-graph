import { showToast, updateToast } from './toast.js';

// Open a note in the Notes app. A refusal (demo mode) comes back as {error}
// and is shown as a toast instead of failing silently.
export async function openInNotes(name) {
  const result = await window.notesAPI.openNote(name);
  if (result?.error) updateToast(showToast(result.error), result.error, { ttlMs: 5000 });
  return result;
}
