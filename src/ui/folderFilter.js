// Excluded-folders filter (#146). The persisted form is unchanged: a list of
// folder-name strings in config.json's `exclude_folders`. The graph UI hides a
// note when its folder contains any entry (case-insensitive substring, entries
// trimmed). The Python side reads the same list with a similar substring match,
// but it doesn't trim entries and lets `include_folders` lift an exclusion;
// this UI has never applied `include_folders`.

function normalized(entries) {
  return (entries ?? []).map((f) => f.trim().toLowerCase()).filter(Boolean);
}

// Normalize once, then test many folders (visibleGraph runs this per note).
export function folderExcluder(excludeFolders) {
  const entries = normalized(excludeFolders);
  return (folder) => {
    const name = (folder ?? '').toLowerCase();
    return entries.some((f) => name.includes(f));
  };
}

export function isFolderExcluded(folder, excludeFolders) {
  return folderExcluder(excludeFolders)(folder);
}

// Dropdown options: every folder in the loaded graph, once, minus the ones the
// current exclusions already hide (so nothing can be added twice).
export function folderChoices(notes, excludeFolders) {
  const excluded = folderExcluder(excludeFolders);
  const names = new Set();
  for (const n of notes) if (n.folder) names.add(n.folder);
  return [...names]
    .filter((name) => !excluded(name))
    .sort((a, b) => a.localeCompare(b, undefined, { sensitivity: 'base' }));
}

// Append one folder name as-is (a dropdown pick: real folder names may contain
// commas). Skips blanks and case-insensitive duplicates; returns the same list
// if nothing changed.
export function addExcludedFolder(excludeFolders, rawName) {
  const name = rawName.trim();
  if (!name || normalized(excludeFolders).includes(name.toLowerCase())) return excludeFolders;
  return [...excludeFolders, name];
}

// The free-text fallback: a comma-separated list, added one name at a time.
export function addExcludedFolders(excludeFolders, text) {
  return text.split(',').reduce(addExcludedFolder, excludeFolders);
}

// A loaded or injected list, made safe to key chips by name: trimmed, no blanks,
// no case-insensitive duplicates (the old free-text box could save any of those).
export function cleanExcludedFolders(excludeFolders) {
  if (!Array.isArray(excludeFolders)) return [];
  return excludeFolders.filter((f) => typeof f === 'string').reduce(addExcludedFolder, []);
}

export function removeExcludedFolder(excludeFolders, name) {
  return excludeFolders.filter((f) => f !== name);
}
