const { app, BrowserWindow, Menu, dialog, ipcMain, screen, shell } = require('electron');
const path = require('path');
const fs = require('fs');
const os = require('os');
const { exec, execFile } = require('child_process');
const { promisify } = require('util');
const { escapeAppleScriptString } = require('./applescript-escape.js');
const { resolveAppPaths } = require('./app-paths.js');
const {
  blockNavigation,
  claimSingleInstance,
  focusOrCreateWindow,
  startOrQuit,
  devUserDataPath,
} = require('./app-lifecycle.js');
const { runStartupChecks } = require('./startup-checks.js');
const { parseWindowState, resolveBounds, trackWindowBounds } = require('./window-state.js');
const { uiUpdateStatus } = require('./ui-fingerprint.js');
const { buildRankPrompt, parseRankResponse, buildClusterPrompt, parseClusterResponse } = require('./claude-llm.js');
const { runClaude } = require('./claude-runner.js');
const { demoRefusal } = require('./demo-mode.js');

const execAsync = promisify(exec);
const execFileAsync = promisify(execFile);

const metadata = require('../package.json');
const paths = resolveAppPaths({ isPackaged: app.isPackaged, baseDir: __dirname, metadata });

// PYTHONUTF8: a Finder launch has no LANG, and the scripts print non-ASCII JSON.
function scriptOptions(options) {
  return { cwd: paths.repoRoot, env: { ...process.env, PYTHONUTF8: '1' }, ...options };
}

function runScript(name, args, options) {
  return execFileAsync(paths.venvPython, [paths.script(name), ...args], scriptOptions(options));
}

// Config file location for shared settings between UI and MCP server
const CONFIG_DIR = path.join(os.homedir(), 'Library', 'Application Support', 'notes-graph');
const CONFIG_PATH = path.join(CONFIG_DIR, 'config.json');

const DEFAULT_CONFIG = {
  exclude_folders: [],
  include_folders: []
};

// Comfortable reading scale (two Cmd+Plus steps above Electron's 100%).
// Cmd+Plus/Minus still adjust dynamically; Cmd+0 returns here, not to 100%.
const DEFAULT_ZOOM_LEVEL = 1;

const DEFAULT_WINDOW_SIZE = { width: 1400, height: 900 };

const NOTE_STORE_PATH = path.join(
  os.homedir(), 'Library', 'Group Containers', 'group.com.apple.notes', 'NoteStore.sqlite'
);

let mainWindow;
// Stays false while a startup dialog is open, so a Dock click can't open a
// window behind it.
let startupChecksPassed = false;

function windowStatePath() {
  return path.join(app.getPath('userData'), 'window-state.json');
}

function loadWindowBounds() {
  let saved = null;
  try {
    saved = parseWindowState(fs.readFileSync(windowStatePath(), 'utf-8'));
  } catch {
    // No saved state yet: first launch.
  }
  return resolveBounds(saved, screen.getAllDisplays(), DEFAULT_WINDOW_SIZE);
}

function saveWindowState(state) {
  try {
    fs.writeFileSync(windowStatePath(), JSON.stringify(state));
  } catch (error) {
    console.error('Failed to save window state:', error);
  }
}

function createWindow() {
  // Window memory is packaged-only: dev and the run-ui driver expect a fixed size.
  const { isMaximized, ...bounds } = app.isPackaged
    ? loadWindowBounds()
    : { ...DEFAULT_WINDOW_SIZE, isMaximized: false };

  mainWindow = new BrowserWindow({
    ...bounds,
    title: 'Notes Graph',
    webPreferences: {
      nodeIntegration: false,
      contextIsolation: true,
      preload: paths.preload
    }
  });

  blockNavigation(mainWindow.webContents);
  if (isMaximized) mainWindow.maximize();
  if (app.isPackaged) trackWindowBounds(mainWindow, saveWindowState);

  mainWindow.webContents.once('did-finish-load', () => {
    mainWindow.webContents.setZoomLevel(DEFAULT_ZOOM_LEVEL);
  });

  if (process.env.VITE_DEV_SERVER_URL) {
    mainWindow.loadURL(process.env.VITE_DEV_SERVER_URL);
  } else {
    mainWindow.loadFile(paths.distUiIndex);
  }
}

function buildMenu() {
  const template = [
    ...(process.platform === 'darwin' ? [{ role: 'appMenu' }] : []),
    { role: 'fileMenu' },
    { role: 'editMenu' },
    {
      label: 'View',
      submenu: [
        { role: 'reload' },
        { role: 'forceReload' },
        { role: 'toggleDevTools' },
        { type: 'separator' },
        {
          label: 'Actual Size',
          accelerator: 'CmdOrCtrl+0',
          click: (_item, focusedWindow) => {
            focusedWindow?.webContents.setZoomLevel(DEFAULT_ZOOM_LEVEL);
          }
        },
        { role: 'zoomIn' },
        { role: 'zoomOut' },
        { type: 'separator' },
        { role: 'togglefullscreen' }
      ]
    },
    { role: 'windowMenu' }
  ];
  Menu.setApplicationMenu(Menu.buildFromTemplate(template));
}

app.setName('Notes Graph');
app.setAboutPanelOptions({
  applicationName: 'Notes Graph',
  applicationVersion: app.getVersion(),
  copyright: 'Ben Ralston'
});

if (!app.isPackaged) {
  app.setPath('userData', devUserDataPath(app.getPath('appData')));
}

function showAppWindow() {
  focusOrCreateWindow({
    ready: startupChecksPassed,
    windows: BrowserWindow.getAllWindows(),
    createWindow,
  });
}

// The app has no window yet, so it isn't frontmost and the dialog would open
// behind whatever is, which looks like the launch did nothing.
function showStartupDialog(options) {
  app.focus({ steal: true });
  return dialog.showMessageBox(options);
}

if (claimSingleInstance({ isPackaged: app.isPackaged, requestLock: () => app.requestSingleInstanceLock() })) {
  app.on('second-instance', showAppWindow);
  startOrQuit({
    whenReady: () => app.whenReady(),
    start,
    quit: () => app.quit(),
    logError: console.error,
  });
} else {
  app.quit();
}

async function start() {
  // In dev the binary is Electron.app; give the Dock our icon at runtime
  // (the packaged app carries assets/icon.icns via electron-builder).
  if (process.platform === 'darwin' && !app.isPackaged && app.dock) {
    if (fs.existsSync(paths.dockIcon)) app.dock.setIcon(paths.dockIcon);
  }
  buildMenu();
  const canStart = !app.isPackaged || await runStartupChecks({
    paths,
    noteStorePath: NOTE_STORE_PATH,
    fsApi: fs,
    showDialog: showStartupDialog,
    openSettings: (url) => shell.openExternal(url),
  });
  if (!canStart) {
    app.quit();
    return;
  }
  startupChecksPassed = true;
  createWindow();
}

app.on('window-all-closed', () => {
  if (process.platform !== 'darwin') app.quit();
});

app.on('activate', () => {
  if (startupChecksPassed && BrowserWindow.getAllWindows().length === 0) createWindow();
});

// Semantic-search embeddings build in the background after each graph load;
// incremental (modified-timestamp) so steady-state is cheap. Never blocks
// the payload - failure just means the palette stays fuzzy-only.
let embeddingBuildInFlight = false;
function scheduleEmbeddingBuild() {
  if (embeddingBuildInFlight) return;
  embeddingBuildInFlight = true;
  execFile(paths.venvPython, [paths.script('build_embeddings.py')],
    scriptOptions({ timeout: 300000, maxBuffer: 1024 * 1024 }),
    (error, stdout) => {
      embeddingBuildInFlight = false;
      if (error) console.error('Embedding build failed:', String(error));
      else console.log('Embedding build:', String(stdout).trim());
    });
}

// Fetch all notes and links via Python script
ipcMain.handle('fetch-notes', async () => {
  try {
    const { stdout, stderr } = await runScript('extract_notes.py', [], {
      timeout: 60000,
      maxBuffer: 10 * 1024 * 1024 // 10MB buffer for large note collections
    });

    if (stderr) {
      console.error('Python script stderr:', stderr);
    }

    const data = JSON.parse(stdout);

    if (data.error) {
      console.error('Extraction error:', data.error);
      return { notes: [], links: [], health: null, stats: null, error: data.error };
    }

    scheduleEmbeddingBuild();
    return data;
  } catch (error) {
    console.error('Failed to fetch notes:', error);
    return { notes: [], links: [], health: null, stats: null, error: String(error) };
  }
});

// Semantic search against the embedding cache (reloads ONNX model per call,
// ~1-3s - acceptable behind the palette debounce)
ipcMain.handle('semantic-search', async (_event, query) => {
  try {
    const { stdout } = await runScript(
      'semantic_search.py',
      ['--query', String(query)],
      { timeout: 30000, maxBuffer: 1024 * 1024 }
    );
    return JSON.parse(stdout);
  } catch (error) {
    console.error('Semantic search failed:', error);
    return { error: String(error) };
  }
});

// Read a single note's content for the inspector preview
ipcMain.handle('read-note', async (_event, notePk) => {
  try {
    const { stdout } = await runScript(
      'read_note.py',
      ['--pk', String(notePk)],
      { timeout: 30000, maxBuffer: 10 * 1024 * 1024 }
    );
    return JSON.parse(stdout);
  } catch (error) {
    console.error('Failed to read note:', error);
    return { error: String(error) };
  }
});

// Link-suggestion recall (Python; parses every note body - slow, hence 120s)
ipcMain.handle('suggest-links', async (_event, notePk) => {
  try {
    const { stdout } = await runScript(
      'suggest_links.py',
      ['--pk', String(notePk)],
      { timeout: 120000, maxBuffer: 10 * 1024 * 1024 }
    );
    return JSON.parse(stdout);
  } catch (error) {
    console.error('Failed to get link suggestions:', error);
    return { error: String(error) };
  }
});

// Append a [[link]] to a note (UI-scripting write; 120s covers lock waits)
ipcMain.handle('add-link', async (_event, payload) => {
  const refused = demoRefusal('Adding a link');
  if (refused) return refused;
  try {
    const { sourcePk, targetName } = payload ?? {};
    const { stdout } = await runScript(
      'add_link.py',
      ['--pk', String(sourcePk), '--target', String(targetName)],
      { timeout: 120000, maxBuffer: 1024 * 1024 }
    );
    return JSON.parse(stdout);
  } catch (error) {
    console.error('Failed to add link:', error);
    return { error: String(error) };
  }
});

// Delete a note - moves to Recently Deleted (pure AppleScript, no focus steal)
ipcMain.handle('delete-note', async (_event, notePk) => {
  const refused = demoRefusal('Deleting a note');
  if (refused) return refused;
  try {
    const { stdout } = await runScript(
      'delete_note.py',
      ['--pk', String(notePk)],
      { timeout: 60000, maxBuffer: 1024 * 1024 }
    );
    return JSON.parse(stdout);
  } catch (error) {
    console.error('Failed to delete note:', error);
    return { error: String(error) };
  }
});

// Open a note in Apple Notes
// Resolves true on success, false on failure, or {error} when refused.
ipcMain.handle('open-note', async (_event, noteName) => {
  const refused = demoRefusal('Opening a note in Notes');
  if (refused) return refused;
  try {
    const script = `
      tell application "Notes"
        activate
        set theNote to first note whose name is "${escapeAppleScriptString(noteName)}"
        show theNote
      end tell
    `;
    await execAsync(`osascript -e '${script.replace(/'/g, "'\\''")}'`);
    return true;
  } catch (error) {
    console.error('Failed to open note:', error);
    return false;
  }
});

// Load config from shared config file
ipcMain.handle('load-config', async () => {
  try {
    if (fs.existsSync(CONFIG_PATH)) {
      const data = await fs.promises.readFile(CONFIG_PATH, 'utf-8');
      const loaded = JSON.parse(data);
      // Merge with defaults to ensure all keys exist
      return { ...DEFAULT_CONFIG, ...loaded };
    }
  } catch (error) {
    console.error('Failed to load config:', error);
  }
  return { ...DEFAULT_CONFIG };
});

// Save config to shared config file
ipcMain.handle('save-config', async (_event, config) => {
  try {
    await fs.promises.mkdir(CONFIG_DIR, { recursive: true });
    await fs.promises.writeFile(CONFIG_PATH, JSON.stringify(config, null, 2));
    return true;
  } catch (error) {
    console.error('Failed to save config:', error);
    return false;
  }
});

// Whether the checkout's UI has moved on since this app was installed.
ipcMain.handle('get-ui-update-status', () => uiUpdateStatus({
  isPackaged: app.isPackaged,
  baked: metadata.notesGraphUiFingerprint,
  repoRoot: paths.repoRoot,
}));

// Rank link-suggestion candidates via headless Claude (degrades to {error}).
ipcMain.handle('rank-suggestions', async (_event, payload) => {
  try {
    const candidates = payload?.candidates ?? [];
    const res = await runClaude(buildRankPrompt(payload ?? {}, candidates), { timeoutMs: 60000 });
    if (res.error) return { error: res.error };
    const parsed = parseRankResponse(res.text, candidates.map((c) => c.name));
    return parsed ?? { error: 'unparseable ranking response' };
  } catch (error) {
    return { error: String(error) };
  }
});

// Name graph clusters via headless Claude (degrades to {error}).
ipcMain.handle('name-clusters', async (_event, payload) => {
  try {
    const clusters = payload?.clusters ?? [];
    if (clusters.length === 0) return { names: {} };
    const res = await runClaude(buildClusterPrompt(clusters), { timeoutMs: 90000 });
    if (res.error) return { error: res.error };
    const parsed = parseClusterResponse(res.text, clusters.map((c) => c.key));
    return parsed ?? { error: 'unparseable cluster-naming response' };
  } catch (error) {
    return { error: String(error) };
  }
});
