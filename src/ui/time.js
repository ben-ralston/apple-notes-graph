// Relative-date formatting for note timestamps (issue #15).
// Fail-open: returns null for missing/unparseable input so callers can
// simply omit the segment.

const MS_PER_DAY = 24 * 60 * 60 * 1000;
const ABSOLUTE_AFTER_DAYS = 30;

function startOfLocalDay(d) {
  return new Date(d.getFullYear(), d.getMonth(), d.getDate());
}

export function formatModified(iso, now = new Date()) {
  if (!iso) return null;
  const date = new Date(iso);
  if (Number.isNaN(date.getTime())) return null;

  // Local calendar-day difference — a note edited yesterday 8 PM is
  // "yesterday" this morning, not "today". Math.round absorbs DST-shifted
  // 23/25-hour days.
  const days = Math.round(
    (startOfLocalDay(now) - startOfLocalDay(date)) / MS_PER_DAY
  );
  if (days <= 0) return 'today';
  if (days === 1) return 'yesterday';
  if (days < ABSOLUTE_AFTER_DAYS) return `${days} days ago`;
  return date.toLocaleDateString('en-US', {
    month: 'short',
    day: 'numeric',
    year: 'numeric',
  });
}
