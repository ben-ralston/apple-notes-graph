// Escape a value for embedding inside an AppleScript double-quoted string
// literal. Backslashes must be escaped before quotes — matches the
// backslash/quote handling of _escape_applescript_string in
// src/apple_notes/applescript.py. Note names are single-line, so the Python
// helper's additional \n/\r/\t escaping is not needed here.
function escapeAppleScriptString(value) {
  return String(value).replace(/\\/g, '\\\\').replace(/"/g, '\\"');
}

module.exports = { escapeAppleScriptString };
