// Elements that own their keystrokes, so single-key app shortcuts (F = focus
// mode) must not fire there. A <select> counts: typing a letter jumps to the
// matching option (the excluded-folders dropdown, #146).
const EDITABLE_TAGS = new Set(['INPUT', 'TEXTAREA', 'SELECT']);

export function isEditableTarget(target) {
  if (!target || typeof target.tagName !== 'string') return false;
  return EDITABLE_TAGS.has(target.tagName) || target.isContentEditable === true;
}
