import { describe, expect, test } from 'vitest';
import {
  addExcludedFolder, addExcludedFolders, cleanExcludedFolders, folderChoices, folderExcluder,
  isFolderExcluded,
  removeExcludedFolder,
} from './folderFilter.js';

describe('isFolderExcluded: the one exclusion rule (case-insensitive substring)', () => {
  test('matches a folder containing an entry, ignoring case and padding', () => {
    expect(isFolderExcluded('🗄️ Archive', [' archive '])).toBe(true);
    expect(isFolderExcluded('Archive', ['Arch'])).toBe(true);
  });

  test('blank entries exclude nothing', () => {
    expect(isFolderExcluded('Notes', ['', '  '])).toBe(false);
  });

  test('a note with no folder is never excluded', () => {
    expect(isFolderExcluded(null, ['Archive'])).toBe(false);
  });
});

describe('folderChoices: dropdown options from the graph already fetched', () => {
  const notes = [
    { id: 1, folder: 'Work' },
    { id: 2, folder: 'archive' },
    { id: 3, folder: 'Work' },
    { id: 4, folder: 'Personal' },
    { id: 5, folder: null },
    { id: 6, folder: '' },
  ];

  test('lists each folder once, sorted case-insensitively, skipping blanks', () => {
    expect(folderChoices(notes, [])).toEqual(['archive', 'Personal', 'Work']);
  });

  test('leaves out folders the current exclusions already hide', () => {
    expect(folderChoices(notes, ['WORK'])).toEqual(['archive', 'Personal']);
    expect(folderChoices(notes, ['arch', 'work'])).toEqual(['Personal']);
  });

  test('offers graph folders only, never names from the exclusion list', () => {
    expect(folderChoices([], ['Archive'])).toEqual([]);
  });
});

describe('addExcludedFolders: dedupe on append', () => {
  test('appends a new folder name, trimmed', () => {
    expect(addExcludedFolders(['Work'], ' Archive ')).toEqual(['Work', 'Archive']);
  });

  test('ignores a case-insensitive duplicate and blanks', () => {
    const list = ['Work'];
    expect(addExcludedFolders(list, 'work')).toBe(list);
    expect(addExcludedFolders(list, '   ')).toBe(list);
  });

  test('free-text fallback: comma-separated names add one by one', () => {
    expect(addExcludedFolders(['Work'], 'Archive, work,, Trash')).toEqual(['Work', 'Archive', 'Trash']);
  });

  test('never mutates the input list', () => {
    const list = ['Work'];
    addExcludedFolders(list, 'Archive');
    expect(list).toEqual(['Work']);
  });
});

describe('removeExcludedFolder', () => {
  test('removes exactly that entry', () => {
    expect(removeExcludedFolder(['Work', 'Archive', 'Trash'], 'Archive')).toEqual(['Work', 'Trash']);
  });
});

describe('addExcludedFolder: one name from the dropdown, never split', () => {
  test('keeps a folder name that contains a comma whole', () => {
    expect(addExcludedFolder(['Work'], 'Errands, Misc')).toEqual(['Work', 'Errands, Misc']);
  });

  test('ignores a case-insensitive duplicate', () => {
    const list = ['Work'];
    expect(addExcludedFolder(list, ' WORK ')).toBe(list);
  });
});

describe('cleanExcludedFolders: a loaded config list made safe to key by name', () => {
  test('trims, drops blanks and case-insensitive duplicates, keeping the first spelling', () => {
    expect(cleanExcludedFolders([' Archive', 'archive', '', 'Trash ', 'Archive', '  '])).toEqual(['Archive', 'Trash']);
  });

  test('tolerates a missing list', () => {
    expect(cleanExcludedFolders(undefined)).toEqual([]);
  });
});

describe('folderExcluder: normalize the list once, test many notes', () => {
  test('returns a predicate with the same rule as isFolderExcluded', () => {
    const excluded = folderExcluder([' archive ', '']);
    expect(excluded('🗄️ Archive')).toBe(true);
    expect(excluded('Notes')).toBe(false);
    expect(excluded(undefined)).toBe(false);
  });
});

describe('cleanExcludedFolders: hand-edited config', () => {
  test('drops entries that are not strings', () => {
    expect(cleanExcludedFolders(['Archive', null, 7, { a: 1 }])).toEqual(['Archive']);
  });
});
