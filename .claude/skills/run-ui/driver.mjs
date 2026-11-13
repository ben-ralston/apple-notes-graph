// REPL driver for the Notes Graph Electron app (macOS, live GUI session).
// Designed for agents: wrap in tmux, send-keys commands, capture-pane output.
// See SKILL.md in this directory for usage.
import { _electron as electron } from 'playwright-core';
import * as readline from 'node:readline';
import * as fs from 'node:fs';
import * as path from 'node:path';

const APP_DIR = path.resolve(import.meta.dirname, '../../..');
const SHOT_DIR = process.env.SCREENSHOT_DIR || '/tmp/notes-graph-shots';
fs.mkdirSync(SHOT_DIR, { recursive: true });

let app = null;
let page = null;

// page.screenshot() crops the window when the page zoom level is non-zero
// (the app defaults to zoomLevel 1) — capture via Electron instead, which
// grabs the full window at any zoom.
async function capturePng(file) {
  const b64 = await app.evaluate(async ({ BrowserWindow }) => {
    const img = await BrowserWindow.getAllWindows()[0].webContents.capturePage();
    return img.toPNG().toString('base64');
  });
  fs.writeFileSync(file, Buffer.from(b64, 'base64'));
}

const electronBin = process.platform === 'darwin'
  ? path.join(APP_DIR, 'node_modules/electron/dist/Electron.app/Contents/MacOS/Electron')
  : path.join(APP_DIR, 'node_modules/electron/dist/electron');

const COMMANDS = {
  async launch() {
    if (app) return console.log('already launched');
    // NOTE: recordVideo is NOT supported for Electron — with it set, the
    // window never finishes loading and no webm is written. Use `frames`
    // for recordings instead.
    app = await electron.launch({
      executablePath: electronBin,
      args: [APP_DIR],
      timeout: 30_000,
    });
    page = await app.firstWindow();
    // Ready = the app's test hook reports mounted + data fetched.
    await page.waitForFunction(() => window.__test?.ready === true,
      null, { timeout: 60_000 });
    const status = await page.evaluate(() =>
      window.__test.status?.() ?? document.querySelector('[data-status]')?.textContent ?? 'ready');
    console.log('launched.', status);
  },

  async ss(name) {
    if (!page) return console.log('ERROR: launch first');
    const f = path.join(SHOT_DIR, (name || `ss-${Date.now()}`) + '.png');
    await capturePng(f);
    console.log('screenshot:', f);
  },

  // The canvas graph has no DOM node elements — selection goes through the
  // window.__test hook (see App.svelte / GraphCanvas.svelte extendTestHook
  // calls). select() tries an exact name match, then a substring fallback.
  async 'click-node'(name) {
    if (!page) return console.log('ERROR: launch first');
    const r = await page.evaluate(n => window.__test.select(n), name);
    console.log('click-node', JSON.stringify(name), '→', r ? 'OK' : 'NOT_FOUND');
  },

  // Opens the note in Apple Notes.app — a real side effect. No test hook for
  // this (it's a deliberate one-way action), so it drives the IPC directly.
  async 'open-node'(name) {
    if (!page) return console.log('ERROR: launch first');
    const r = await page.evaluate(async n => {
      const found = window.__test.nodes().find(x => x.name === n)
        ?? window.__test.nodes().find(x => x.name.includes(n));
      if (!found) return 'NOT_FOUND';
      await window.notesAPI.openNote(found.name);
      return 'OK: ' + found.name;
    }, name);
    console.log('open-node', JSON.stringify(name), '→', r);
  },

  async nodes(filter) {
    if (!page) return console.log('ERROR: launch first');
    const list = await page.evaluate(() => window.__test.nodes());
    const shown = filter ? list.filter(n => n.name?.includes(filter)) : list;
    console.log(`${list.length} nodes${filter ? `, ${shown.length} matching` : ''}:`);
    for (const n of shown.slice(0, 40))
      console.log(`  ${n.name} (${n.connections} connections, cluster ${n.cluster})`);
    if (shown.length > 40) console.log(`  … ${shown.length - 40} more (pass a filter)`);
  },

  async stats() {
    if (!page) return console.log('ERROR: launch first');
    console.log(await page.evaluate(() => ({
      stats: window.__test.stats(),
      status: window.__test.status(),
    })));
  },

  async click(sel) {
    if (!page) return console.log('ERROR: launch first');
    const r = await page.evaluate(s => {
      const el = document.querySelector(s);
      if (!el) return 'NOT_FOUND';
      el.click(); return 'OK';
    }, sel);
    console.log('click', sel, '→', r);
  },

  async fill(arg) {
    if (!page) return console.log('ERROR: launch first');
    const [sel, ...rest] = arg.split(/\s+/);
    await page.fill(sel, rest.join(' '));
    // The filter inputs re-render on 'change', which fill() fires on blur
    await page.dispatchEvent(sel, 'change');
    console.log('filled', sel);
  },

  async type(text)  { if (page) await page.keyboard.type(text, { delay: 30 }); },
  async press(key)  { if (page) await page.keyboard.press(key); },

  async wait(sel) {
    if (!page) return console.log('ERROR: launch first');
    try { await page.waitForSelector(sel, { timeout: 10_000 }); console.log('found:', sel); }
    catch { console.log('TIMEOUT:', sel); }
  },

  // Deliberate arbitrary-JS escape hatch: this REPL is a local dev/debug
  // tool driven only by the operator's own typed commands, never by
  // untrusted input.
  async eval(expr) {
    if (!page) return console.log('ERROR: launch first');
    try { console.log(JSON.stringify(await page.evaluate(expr))); }
    catch (e) { console.log('ERROR:', e.message); }
  },

  async text(sel) {
    if (!page) return console.log('ERROR: launch first');
    console.log(await page.evaluate(
      s => (s ? document.querySelector(s) : document.body)?.innerText ?? '(null)',
      sel || null));
  },

  // Burst of screenshots for assembling a GIF/video of animation
  // (e.g. the force simulation settling after a refresh or drag).
  async frames(arg) {
    if (!page) return console.log('ERROR: launch first');
    const [name = 'frames', count = '12', intervalMs = '250'] = arg.split(/\s+/);
    const dir = path.join(SHOT_DIR, name);
    fs.mkdirSync(dir, { recursive: true });
    for (let i = 0; i < Number(count); i++) {
      await capturePng(path.join(dir, `${String(i).padStart(3, '0')}.png`));
      await new Promise(r => setTimeout(r, Number(intervalMs)));
    }
    console.log(`frames: ${count} × ${intervalMs}ms →`, dir);
  },

  // Evaluate an expression in the Electron MAIN process. The electron module
  // is in scope as `electron` — e.g.
  //   meval electron.BrowserWindow.getAllWindows()[0].webContents.getZoomLevel()
  async meval(expr) {
    if (!app) return console.log('ERROR: launch first');
    try {
      // eval of operator-typed input is the point of this REPL command (same
      // trust model as the renderer-side `eval` command above) — dev tool
      // driving the operator's own local app, never fed untrusted input.
      const result = await app.evaluate(
        (electron, e) => Promise.resolve(eval(e)), expr);
      console.log(JSON.stringify(result));
    } catch (e) {
      console.log('meval ERROR:', e.message.split('\n')[0]);
    }
  },

  async quit() {
    if (app) await app.close().catch(() => {});
    app = null; page = null;
  },
  help() { console.log('commands:', Object.keys(COMMANDS).join(', ')); },
};

// Stop Electron from stealing stdin — use the raw fd.
const stdin = fs.createReadStream(null, { fd: fs.openSync('/dev/stdin', 'r') });
const rl = readline.createInterface({ input: stdin, output: process.stdout, prompt: 'driver> ' });

rl.on('line', async line => {
  const [cmd, ...rest] = line.trim().split(/\s+/);
  if (!cmd) return rl.prompt();
  const fn = COMMANDS[cmd];
  if (!fn) { console.log('unknown:', cmd, '— try: help'); return rl.prompt(); }
  try { await fn(rest.join(' ')); } catch (e) { console.log('ERROR:', e.message); }
  if (cmd === 'quit') { rl.close(); process.exit(0); }
  rl.prompt();
});
rl.on('close', async () => { await COMMANDS.quit(); process.exit(0); });

console.log('notes-graph driver — "help" for commands, "launch" to start');
rl.prompt();
