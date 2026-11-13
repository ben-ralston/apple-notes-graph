import { describe, it, expect, beforeEach } from 'vitest';
import { execFileSync } from 'child_process';
import fs from 'fs';
import os from 'os';
import path from 'path';
import {
  isUiPath,
  fingerprintFromLsTree,
  computeFingerprint,
  uiUpdateStatus,
} from './ui-fingerprint.js';

const entry = (file, blob) => `100644 blob ${blob}\t${file}`;

describe('isUiPath', () => {
  it.each(['src/main.js', 'src/ui/App.svelte', 'src/ui/graph/view.js', 'package.json', 'package-lock.json', 'vite.config.js', 'assets/icon.png'])(
    'counts %s',
    (file) => expect(isUiPath(file)).toBe(true)
  );

  it.each([
    'src/apple_notes/database.py',
    'src/ui/toast.test.js',
    'src/main.test.js',
    'src/apple_notes_mcp/thing.js',
    'scripts/read_note.py',
    'README.md',
  ])('ignores %s', (file) => expect(isUiPath(file)).toBe(false));
});

describe('fingerprintFromLsTree', () => {
  const base = [entry('src/main.js', 'aaa'), entry('README.md', 'bbb')];

  it('ignores changes to non-UI files', () => {
    const changed = [entry('src/main.js', 'aaa'), entry('README.md', 'ccc')];
    expect(fingerprintFromLsTree(changed.join('\0'))).toBe(fingerprintFromLsTree(base.join('\0')));
  });

  it('changes when a UI file changes', () => {
    const changed = [entry('src/main.js', 'zzz'), entry('README.md', 'bbb')];
    expect(fingerprintFromLsTree(changed.join('\0'))).not.toBe(fingerprintFromLsTree(base.join('\0')));
  });
});

describe('computeFingerprint', () => {
  let repo;
  const git = (...args) =>
    execFileSync('git', ['-c', 'user.name=t', '-c', 'user.email=t@example.com', ...args], {
      cwd: repo,
      stdio: 'pipe',
    });
  const commit = (file, content) => {
    const full = path.join(repo, file);
    fs.mkdirSync(path.dirname(full), { recursive: true });
    fs.writeFileSync(full, content);
    git('add', '.');
    git('commit', '-m', `change ${file}`);
  };

  beforeEach(() => {
    repo = fs.mkdtempSync(path.join(os.tmpdir(), 'ui-fingerprint-'));
    git('init', '-q');
    commit('src/ui/App.svelte', 'one');
  });

  it('is stable across an empty commit and a test-only commit', async () => {
    const before = await computeFingerprint(repo);
    git('commit', '--allow-empty', '-m', 'empty');
    commit('src/ui/App.test.js', 'test');
    expect(await computeFingerprint(repo)).toBe(before);
  });

  it('changes after a commit to a UI file', async () => {
    const before = await computeFingerprint(repo);
    commit('src/ui/App.svelte', 'two');
    expect(await computeFingerprint(repo)).not.toBe(before);
  });

  it('changes after a lockfile-only commit', async () => {
    const before = await computeFingerprint(repo);
    commit('package-lock.json', '{"lockfileVersion":3}');
    expect(await computeFingerprint(repo)).not.toBe(before);
  });

  it('rejects outside a git repository', async () => {
    const notRepo = fs.mkdtempSync(path.join(os.tmpdir(), 'ui-fingerprint-none-'));
    await expect(computeFingerprint(notRepo)).rejects.toBeTruthy();
  });
});

describe('uiUpdateStatus', () => {
  const compute = async () => 'current';
  const args = { isPackaged: true, baked: 'old', repoRoot: '/checkout', compute };

  it('is stale when the baked fingerprint differs', async () => {
    expect(await uiUpdateStatus(args)).toEqual({ stale: true });
  });

  it('is not stale when they match', async () => {
    expect(await uiUpdateStatus({ ...args, baked: 'current' })).toEqual({ stale: false });
  });

  it('is never stale in dev', async () => {
    expect(await uiUpdateStatus({ ...args, isPackaged: false })).toEqual({ stale: false });
  });

  it('is not stale with no baked fingerprint', async () => {
    expect(await uiUpdateStatus({ ...args, baked: undefined })).toEqual({ stale: false });
  });

  it('is not stale when git fails', async () => {
    const failing = async () => {
      throw new Error('git broke');
    };
    expect(await uiUpdateStatus({ ...args, compute: failing })).toEqual({ stale: false });
  });
});
