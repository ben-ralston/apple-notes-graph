'use strict';
// Content fingerprint of everything packaged into the .app (issue #238).
// `./run ui-app-install` bakes it in; the running app compares it against the
// checkout to show the update chip. Based on file contents rather than a commit
// id, so a squash merge of the same code doesn't read as an update.
//
// Also a CLI, so install and runtime can't disagree:
//   node src/ui-fingerprint.js <repo-root>

const { execFile } = require('child_process');
const { createHash } = require('crypto');
const { promisify } = require('util');

const execFileAsync = promisify(execFile);

function isUiPath(file) {
  if (file.endsWith('.test.js')) return false;
  if (['package.json', 'package-lock.json', 'vite.config.js'].includes(file)) return true;
  if (file.startsWith('src/ui/') || file.startsWith('assets/')) return true;
  return /^src\/[^/]+\.js$/.test(file);
}

// Takes `git ls-tree -r -z` output: NUL-separated "<mode> <type> <blob>\t<path>".
function fingerprintFromLsTree(output) {
  const entries = output
    .split('\0')
    .filter((entry) => entry && isUiPath(entry.slice(entry.indexOf('\t') + 1)));
  return createHash('sha256').update(entries.join('\n')).digest('hex');
}

async function computeFingerprint(repoRoot) {
  const { stdout } = await execFileAsync('git', ['ls-tree', '-r', '-z', 'HEAD'], {
    cwd: repoRoot,
    maxBuffer: 10 * 1024 * 1024,
  });
  return fingerprintFromLsTree(stdout);
}

// Fails open: the chip is a hint, so anything uncertain means no chip.
async function uiUpdateStatus({ isPackaged, baked, repoRoot, compute = computeFingerprint }) {
  if (!isPackaged || !baked) return { stale: false };
  try {
    return { stale: (await compute(repoRoot)) !== baked };
  } catch {
    return { stale: false };
  }
}

if (require.main === module) {
  computeFingerprint(process.argv[2] ?? '.').then(
    (fingerprint) => console.log(fingerprint),
    (error) => {
      console.error(String(error));
      process.exit(1);
    }
  );
}

module.exports = { isUiPath, fingerprintFromLsTree, computeFingerprint, uiUpdateStatus };
