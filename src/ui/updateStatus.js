// Keeps the update chip current: checks at once, then whenever the window regains
// focus, so a `git pull` shows up on switching back. Fails open: the chip is only
// a hint, so anything uncertain means no chip. Returns a function that stops it.
export function watchUpdateStatus({ api, target, onChange }) {
  async function check() {
    try {
      onChange((await api.getUiUpdateStatus()).stale === true);
    } catch {
      onChange(false);
    }
  }

  check();
  target.addEventListener('focus', check);
  return () => target.removeEventListener('focus', check);
}
