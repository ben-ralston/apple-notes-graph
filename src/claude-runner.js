'use strict';
// Headless Claude Code runner (issue #117). One call = one `claude -p` spawn
// with all tools disabled. Failures come back as {error} data - callers
// degrade, never crash (intelligence is additive, not load-bearing).

const { execFile } = require('child_process');
const fs = require('fs');
const os = require('os');
const path = require('path');

// THE model switch: change to 'haiku' if Sonnet latency is unacceptable.
const CLAUDE_MODEL = 'sonnet';

// Verified live: without this, the headless session inherits the user's
// CLAUDE.md + hooks (~20k tokens of coding-assistant framing) and Sonnet
// refuses the "oddly formed" prompt instead of answering it. A dedicated
// system prompt + no setting sources + neutral cwd makes the call a pure
// LLM request (1-2s, clean JSON). Hooks are also why we must skip settings:
// hook children can inherit stdout and hold execFile's pipe open forever.
const SYSTEM_PROMPT =
  'You are a JSON-only service inside a personal notes app. You receive one ' +
  'task per invocation and reply with exactly the JSON object the task ' +
  'specifies - no prose, no markdown fences.';

function resolveClaudeBin() {
  // Finder-launched Electron gets a bare PATH; prefer the known install spot.
  const local = path.join(os.homedir(), '.local', 'bin', 'claude');
  return fs.existsSync(local) ? local : 'claude';
}

function runClaude(prompt, { timeoutMs }) {
  return new Promise((resolve) => {
    const child = execFile(
      resolveClaudeBin(),
      [
        '-p', '--model', CLAUDE_MODEL, '--output-format', 'json',
        '--tools', '', '--setting-sources', '', '--system-prompt', SYSTEM_PROMPT,
      ],
      { timeout: timeoutMs, maxBuffer: 10 * 1024 * 1024, cwd: os.tmpdir() },
      (error, stdout) => {
        if (error) return resolve({ error: String(error) });
        try {
          const envelope = JSON.parse(stdout);
          if (envelope.is_error) return resolve({ error: `claude: ${envelope.result ?? envelope.subtype}` });
          resolve({ text: envelope.result ?? '' });
        } catch (e) {
          resolve({ error: `claude: unparseable output (${e.message})` });
        }
      }
    );
    child.stdin.on('error', () => {}); // claude dying early EPIPEs stdin; the callback error covers it
    child.stdin.end(prompt);
  });
}

module.exports = { CLAUDE_MODEL, resolveClaudeBin, runClaude };
