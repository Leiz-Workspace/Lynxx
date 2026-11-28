// Lynxx desktop: Electron main process
// - spawns the Python sidecar (python -m lynxx.server, or the bundled lynxx-core) for the library, file conversion,
//   reader data and exports (JSON-RPC over stdio)
// - runs vBook-format extensions in hidden Chromium sandboxes and the book download jobs (app/plugin)
// - owns settings (userData/settings.json), the cover thumbnail cache (lynxx-thumb://), appearance and the app icon
'use strict';

const {
  app, BrowserWindow, ipcMain, dialog, shell, nativeImage, protocol, net, session, Menu, Notification,
} = require('electron');
const path = require('node:path');
const fs = require('node:fs');
const fsp = fs.promises;
const os = require('node:os');
const crypto = require('node:crypto');
const readline = require('node:readline');
const { spawn } = require('node:child_process');
const { pathToFileURL } = require('node:url');
const netlib = require('node:net');
const dnsp = require('node:dns').promises;

// Linux: Electron derives the window WM_CLASS (X11) and app_id (Wayland) from the desktop name ('lynxx.desktop' -> 'lynxx'),
// so launchers must use StartupWMClass=lynxx (lowercase) for docks to group the window with its launcher
const DESKTOP_ID = 'lynxx';
const { pngsToIco } = require('./icon-utils');
const { registerLibProtocol, setupReader, IMPORT_EXTENSIONS } = require('./plugin/ipc');

app.setName('Lynxx');
if (process.env.LYNXX_USER_DATA) app.setPath('userData', path.resolve(process.env.LYNXX_USER_DATA));
if (process.platform === 'linux' && typeof app.setDesktopName === 'function') app.setDesktopName(`${DESKTOP_ID}.desktop`);
// <input type=date> and the PDF viewer follow the Chromium UI language: Vietnamese like the rest of the UI (before 'ready')
app.commandLine.appendSwitch('lang', 'vi');

const REPO_ROOT = path.resolve(__dirname, '..');
const CORE_NAME = process.platform === 'win32' ? 'lynxx-core.exe' : 'lynxx-core';
const PACKAGE_NAME = (() => { try { return require('./package.json').name; } catch { return ''; } })();
const ICON_DIR = path.join(__dirname, 'assets', 'icons');
const ICONS = [
  { id: 'exposed-jaw-cyan', name: 'Exposed jaw · Cyan' }, // the app icon (installers, launcher)
  { id: 'full-mask-cyan', name: 'Full mask · Cyan' },
  { id: 'exposed-jaw-monochrome-1024', name: 'Exposed jaw · Mono' },
  { id: 'full-mask-monochrome-1024', name: 'Full mask · Mono' },
];
const THEMES = ['abyss', 'graphite', 'paper', 'oled'];
const THUMB_CACHE_MAX = 512 * 1024 * 1024;
const THUMB_SIZE_MAX = 25 * 1024 * 1024;
const THUMB_TIMEOUT_MS = 20000;
const THUMB_REDIRECTS_MAX = 5; // cover images: redirects followed, each hop checked (see upstreamGet)
// v4: Lynxx without the booru downloader it was forked from (settings of those pages are dropped on load)
// v5: the exposed jaw icon is the app icon: a profile still on the former default (full mask) follows it
const SETTINGS_VERSION = 5;

const DEFAULT_SETTINGS = {
  version: SETTINGS_VERSION,
  appearance: {
    theme: 'abyss',
    icon: 'exposed-jaw-cyan',
    background: { mode: 'icon', image: '', blur: 4, opacity: 0.9, dim: 0.18 },
  },
  download: { notify: true }, // a system notification when a book download ends while Lynxx is not the focused window
  connection: { proxy: '', ignoreProxy: false },
  advanced: { pythonPath: '' },
  window: { width: 1360, height: 860 },
};

// ---------- utils ----------

const userData = () => app.getPath('userData');
const settingsFile = () => path.join(userData(), 'settings.json');
let settings = structuredClone(DEFAULT_SETTINGS);
let mainWindow = null;

const isPlainObject = (v) => Boolean(v) && typeof v === 'object' && !Array.isArray(v);
const escapeRe = (s) => String(s).replace(/[.*+?^${}()|[\]\\]/g, '\\$&');

function deepMerge(base, patch) {
  if (patch === null || typeof patch !== 'object' || Array.isArray(patch)) return patch;
  const out = { ...(isPlainObject(base) ? base : {}) };
  for (const [k, v] of Object.entries(patch)) out[k] = isPlainObject(v) ? deepMerge(out[k], v) : v;
  return out;
}

function knownOnly(shape, value) {
  // the keys of the defaults only, with the type of the default: what older versions stored (booru pages, API keys) goes away
  if (!isPlainObject(shape)) return typeof value === typeof shape ? value : shape;
  const out = {};
  for (const [k, def] of Object.entries(shape)) out[k] = isPlainObject(value) && k in value ? knownOnly(def, value[k]) : structuredClone(def);
  return out;
}

function writeJsonAtomic(file, data) {
  fs.mkdirSync(path.dirname(file), { recursive: true });
  const tmp = `${file}.tmp`;
  const fd = fs.openSync(tmp, 'w', 0o600);
  try {
    fs.writeFileSync(fd, JSON.stringify(data, null, 2));
    try { fs.fsyncSync(fd); } catch { /* not supported on this filesystem */ } // a crash must not leave a truncated settings file
  } finally {
    fs.closeSync(fd);
  }
  fs.renameSync(tmp, file);
  try { fs.chmodSync(file, 0o600); } catch { /* not supported on this filesystem */ }
}

function browserLikeUA(ua, appNames = []) {
  // Electron's own User-Agent names Electron and the app ('… lynxx-desktop/1.0.0 Chrome/142.0.0.0 Electron/45.2.1 Safari/537.36'):
  // both go (WAF rules aimed at Electron, the app advertised to every site). The Chrome version stays: the page's scripts run on
  // that engine
  const names = ['Electron', ...appNames].filter((n) => typeof n === 'string' && n.trim()).map((n) => escapeRe(n.trim()));
  const re = new RegExp(`(^|\\s)(?:${names.join('|')})/\\S*`, 'gi');
  return String(ua || '').replace(re, '$1').replace(/\s{2,}/g, ' ').trim();
}

// ---------- security helpers ----------

const approvedPaths = new Set(); // files picked by the user through native dialogs (the renderer cannot invent paths)

function redact(text) {
  return String(text)
    .replace(/([?&](?:api_key|apikey|token|access_token|key|auth|sig|signature|password)=)[^&\s"']+/gi, '$1<REDACTED>')
    .replace(/((?:authorization|cookie|x-api-key)\s*[:=]\s*)[^\n"']+/gi, '$1<REDACTED>');
}

async function confirmMain(message, detail, buttons = ['Huỷ', 'Đồng ý']) {
  const opts = { type: 'warning', buttons, defaultId: 0, cancelId: 0, noLink: true, message, detail };
  const parent = mainWindow && !mainWindow.isDestroyed() ? mainWindow : null;
  const r = await (parent ? dialog.showMessageBox(parent, opts) : dialog.showMessageBox(opts));
  return r.response === 1;
}

function ipv6Words(ip) {
  // the 8 16-bit words of an IPv6 address ('::' expanded, a trailing dotted IPv4 part read as two words, a zone id dropped);
  // null for anything else
  let v = String(ip || '').toLowerCase().replace(/%.*$/, '');
  const dotted = v.match(/^(.*:)([^:]+\.[^:]*)$/);
  if (dotted) {
    if (!netlib.isIPv4(dotted[2])) return null;
    const [a, b, c, d] = dotted[2].split('.').map(Number);
    v = `${dotted[1]}${((a << 8) | b).toString(16)}:${((c << 8) | d).toString(16)}`;
  }
  const halves = v.split('::');
  if (halves.length > 2) return null;
  const head = halves[0] ? halves[0].split(':') : [];
  const tail = halves.length === 2 && halves[1] ? halves[1].split(':') : [];
  const fill = 8 - head.length - tail.length;
  if (halves.length === 2 ? fill < 1 : fill !== 0) return null;
  const words = [...head, ...Array(halves.length === 2 ? fill : 0).fill('0'), ...tail];
  return words.every((w) => /^[0-9a-f]{1,4}$/.test(w)) ? words.map((w) => parseInt(w, 16)) : null;
}

function isPrivateAddress(ip) {
  if (netlib.isIPv4(ip)) {
    const [a, b] = ip.split('.').map(Number);
    return a === 0 || a === 10 || a === 127 || a >= 224 || (a === 169 && b === 254) || (a === 172 && b >= 16 && b <= 31)
      || (a === 192 && b === 168) || (a === 100 && b >= 64 && b <= 127);
  }
  // IPv6 in any spelling: WHATWG URL writes [::ffff:127.0.0.1] as [::ffff:7f00:1], so an IPv4 address inside one is read back
  // and judged by the IPv4 rules. Anything that is not an address is refused
  const w = ipv6Words(ip);
  if (!w) return true;
  const v4 = () => `${w[6] >> 8}.${w[6] & 255}.${w[7] >> 8}.${w[7] & 255}`;
  const zero = (from, to) => w.slice(from, to).every((x) => x === 0);
  if (zero(0, 5) && w[5] === 0xffff) return isPrivateAddress(v4()); // ::ffff:a.b.c.d (IPv4-mapped)
  if (zero(0, 4) && w[4] === 0xffff && w[5] === 0) return isPrivateAddress(v4()); // ::ffff:0:a.b.c.d (IPv4-translated)
  if (w[0] === 0x64 && w[1] === 0xff9b && zero(2, 6)) return isPrivateAddress(v4()); // 64:ff9b::a.b.c.d (NAT64)
  if (w[0] === 0x64 && w[1] === 0xff9b && w[2] === 1) return true; // 64:ff9b:1::/48 (local NAT64)
  if (zero(0, 6)) return true; // '::', '::1' and ::a.b.c.d (IPv4-compatible, deprecated): never a public host
  // unique local fc00::/7, link-local fe80::/10, site-local fec0::/10, multicast ff00::/8
  return (w[0] & 0xfe00) === 0xfc00 || (w[0] & 0xffc0) === 0xfe80 || (w[0] & 0xffc0) === 0xfec0 || (w[0] & 0xff00) === 0xff00;
}

async function assertPublicHost(urlString) {
  const url = new URL(urlString);
  if (url.protocol !== 'https:' && url.protocol !== 'http:') throw new Error('unsupported scheme');
  const host = url.hostname.replace(/^\[|\]$/g, '');
  if (!host || host === 'localhost' || /\.(local|localhost|internal|lan|home)$/i.test(host)) throw new Error('blocked host');
  const addrs = netlib.isIP(host) ? [{ address: host }] : await dnsp.lookup(host, { all: true });
  if (!addrs.length || addrs.some((a) => isPrivateAddress(a.address))) throw new Error('blocked host');
}

// ---------- settings ----------

const startupNotices = []; // problems found before the window existed: shown once the renderer has loaded
let settingsWriteBlocked = false; // a corrupt settings file that could not be backed up is never overwritten
let settingsLoadInfo = {}; // reported by the self-test

function backupCorruptSettings() {
  const file = settingsFile();
  const backup = `${file}.corrupt-${new Date().toISOString().replace(/[:.]/g, '-')}`;
  try {
    fs.copyFileSync(file, backup);
  } catch {
    try { fs.renameSync(file, backup); } catch { return ''; } // unreadable file: move it aside instead
  }
  try { fs.chmodSync(backup, 0o600); } catch { /* not supported on this filesystem */ }
  return backup;
}

function readSettingsFile() {
  // -> { raw } for a valid file, {} when there is none, { error } when it is corrupt or unreadable
  let text;
  try {
    text = fs.readFileSync(settingsFile(), 'utf8');
  } catch (err) {
    return err.code === 'ENOENT' ? {} : { error: err };
  }
  try {
    const raw = JSON.parse(text);
    if (!isPlainObject(raw)) throw new Error('not a JSON object');
    return { raw };
  } catch (err) {
    return { error: err };
  }
}

function sanitizeSettings() {
  settings = knownOnly(DEFAULT_SETTINGS, settings);
  if (!THEMES.includes(settings.appearance.theme)) settings.appearance.theme = 'abyss'; // 'ember' (the orange theme of Lustra) included
  if (!ICONS.some((i) => i.id === settings.appearance.icon)) settings.appearance.icon = ICONS[0].id;
  if (!['icon', 'image', 'none'].includes(settings.appearance.background.mode)) settings.appearance.background.mode = 'icon';
  const w = settings.window;
  if (!(w.width >= 800 && w.width <= 10000)) w.width = DEFAULT_SETTINGS.window.width;
  if (!(w.height >= 600 && w.height <= 10000)) w.height = DEFAULT_SETTINGS.window.height;
  settings.version = SETTINGS_VERSION;
}

function migrateSettings(from) {
  // v1/v2 -> v3: earlier default backgrounds were too blurred, the icon outline must stay visible
  const bg = settings.appearance.background;
  const oldDefaults = [[48, 0.32, 0.55], [14, 0.9, 0.18]];
  if (from < 3 && oldDefaults.some(([b, o, d]) => bg.blur === b && bg.opacity === o && bg.dim === d)) {
    Object.assign(bg, { blur: 4, opacity: 0.9, dim: 0.18 });
  }
  if (from < 5 && settings.appearance.icon === 'full-mask-cyan') settings.appearance.icon = 'exposed-jaw-cyan';
}

function loadSettings() {
  const { raw, error } = readSettingsFile();
  // the file version, not the merged default: a v1 file has no "version" key
  const fileVersion = raw ? (Number.isInteger(raw.version) && raw.version > 0 ? raw.version : 1) : SETTINGS_VERSION;
  settings = deepMerge(DEFAULT_SETTINGS, raw || {});
  migrateSettings(fileVersion);
  sanitizeSettings();
  settingsLoadInfo = { fileVersion: raw ? fileVersion : null, corrupt: Boolean(error), migrated: Boolean(raw && fileVersion < SETTINGS_VERSION) };
  if (error) {
    const backup = backupCorruptSettings();
    const text = `File cài đặt bị hỏng (${String(error.message || error).slice(0, 200)}). `
      + (backup ? `Đã lưu bản sao: ${backup}. ` : 'Không sao lưu được file nên app sẽ không ghi đè lên nó. ')
      + 'Cài đặt được đặt lại mặc định.';
    startupNotices.push({ level: 'warn', title: 'File cài đặt bị hỏng', text });
    Object.assign(settingsLoadInfo, { backup });
    console.warn(`settings: ${text}`);
    if (!backup) settingsWriteBlocked = true;
    else saveSettings();
  } else if (raw && fileVersion < SETTINGS_VERSION) {
    saveSettings();
    startupNotices.push({ level: 'info', text: `settings: đã chuyển từ phiên bản ${fileVersion} lên ${SETTINGS_VERSION}` });
  }
}

function saveSettings() {
  if (settingsWriteBlocked) return;
  writeJsonAtomic(settingsFile(), settings);
}

function flushStartupNotices() {
  for (const n of startupNotices.splice(0)) {
    send('log', { source: 'settings', text: n.text });
    if (n.level !== 'warn') continue;
    if (!process.env.LYNXX_SELFTEST_DIR && mainWindow && !mainWindow.isDestroyed()) {
      dialog.showMessageBox(mainWindow, { type: 'warning', message: n.title, detail: n.text, buttons: ['OK'], noLink: true }).catch(() => {});
    }
  }
}

function send(channel, payload) {
  if (mainWindow && !mainWindow.isDestroyed()) mainWindow.webContents.send(channel, payload);
}

function iconPath(id) {
  return path.join(ICON_DIR, `${id}.png`);
}

// ---------- core (Python sidecar) ----------

function resolvePython() {
  const candidates = [
    settings.advanced.pythonPath,
    process.env.LYNXX_PYTHON,
    path.join(REPO_ROOT, '.venv', process.platform === 'win32' ? 'Scripts\\python.exe' : 'bin/python'),
  ].filter(Boolean);
  for (const c of candidates) if (fs.existsSync(c)) return c;
  return process.platform === 'win32' ? 'python' : 'python3';
}

function bundledCore() {
  // frozen core (PyInstaller one-dir, see tools/build_core.py): resources/core in the packaged app, LYNXX_CORE to test one in development
  if (process.env.LYNXX_CORE) return path.resolve(process.env.LYNXX_CORE);
  if (!app.isPackaged) return '';
  const core = path.join(process.resourcesPath, 'core', CORE_NAME);
  return fs.existsSync(core) ? core : '';
}

function coreCommand() {
  // packaged app: the bundled core (the Python path setting is only a fallback when it is missing: the app's sources are not on
  // disk); development: python -m lynxx.server
  const core = bundledCore();
  if (core) return { cmd: core, args: ['server'], cwd: userData(), frozen: true };
  return { cmd: resolvePython(), args: ['-m', 'lynxx.server'], cwd: REPO_ROOT, frozen: false };
}

function pythonEnv(frozen) {
  const sep = process.platform === 'win32' ? ';' : ':';
  const env = {
    ...process.env,
    PYTHONUNBUFFERED: '1',
    PYTHONIOENCODING: 'utf-8',
    LYNXX_DATA_DIR: userData(),
    PYTHONPATH: [REPO_ROOT, process.env.PYTHONPATH].filter(Boolean).join(sep),
  };
  if (frozen) delete env.PYTHONPATH; // the frozen core carries its own modules
  return env;
}

class Sidecar {
  constructor() {
    this.proc = null;
    this.pending = new Map();
    this.nextId = 1;
    this.ready = null;
    this.info = null;
    this.restarts = 0;
    this.stopping = false;
  }

  start() {
    const c = coreCommand();
    this.info = null;
    this.status = { ok: false, error: 'starting' };
    this.ready = new Promise((resolve, reject) => {
      this._resolveReady = resolve;
      this._rejectReady = reject;
    });
    this.ready.catch(() => {}); // callers get the rejection through call(); avoid unhandled rejection warnings
    let proc;
    try {
      fs.mkdirSync(userData(), { recursive: true });
      proc = spawn(c.cmd, c.args, { cwd: c.cwd, env: pythonEnv(c.frozen), stdio: ['pipe', 'pipe', 'pipe'], windowsHide: true });
    } catch (err) {
      this._setStatus({ ok: false, error: `Không chạy được lõi Lynxx (${c.cmd}): ${err.message}` });
      this._rejectReady(err);
      return;
    }
    this.proc = proc;
    proc.stdin.on('error', () => {}); // EPIPE after the process died must not crash the main process
    let ended = false;
    const onEnd = (reason) => {
      if (ended) return; // 'error' and 'exit' can both fire
      ended = true;
      if (this.proc === proc) this.proc = null;
      for (const { reject, timer } of this.pending.values()) { clearTimeout(timer); reject(new Error(`sidecar: ${reason}`)); }
      this.pending.clear();
      this._rejectReady(new Error(`sidecar: ${reason}`)); // no-op when already ready
      this._setStatus({ ok: false, error: reason });
      if (!this.stopping && this.restarts < 5) {
        this.restarts += 1;
        setTimeout(() => this.start(), 1000 * this.restarts);
      }
    };
    proc.on('error', (err) => onEnd(`Không chạy được lõi Lynxx (${c.cmd}): ${err.message}`));
    readline.createInterface({ input: proc.stdout }).on('line', (line) => this._onLine(line));
    readline.createInterface({ input: proc.stderr }).on('line', (line) => send('log', { source: 'sidecar', text: redact(line) }));
    proc.on('exit', (code, signal) => onEnd(`sidecar exited (${code ?? signal})`));
  }

  _setStatus(st) {
    this.status = st;
    send('sidecar:status', st);
  }

  _onLine(line) {
    let msg;
    try { msg = JSON.parse(line); } catch { send('log', { source: 'sidecar', text: line }); return; }
    if (msg.id !== undefined && msg.id !== null && this.pending.has(msg.id)) {
      const { resolve, reject, timer } = this.pending.get(msg.id);
      clearTimeout(timer);
      this.pending.delete(msg.id);
      if (msg.error) {
        const err = new Error(msg.error.message);
        err.code = msg.error.code;
        err.data = msg.error.data;
        reject(err);
      } else resolve(msg.result);
      return;
    }
    if (msg.method === 'ready') {
      this.info = msg.params;
      this.restarts = 0;
      this._setStatus({ ok: true, info: msg.params });
      this._resolveReady(msg.params);
    } else if (msg.method === 'log') {
      send('log', { source: msg.params?.scope || 'core', text: String(msg.params?.text ?? '') });
    } else if (msg.method) {
      send('sidecar:notify', { method: msg.method, params: msg.params });
    }
  }

  async call(method, params = {}, timeoutMs = 120000) {
    const deadline = Date.now() + timeoutMs;
    // the timeout also covers waiting for the sidecar to start
    let waitTimer;
    await Promise.race([
      this.ready,
      new Promise((_, reject) => { waitTimer = setTimeout(() => reject(new Error(`timeout: ${method} (sidecar not ready)`)), timeoutMs); }),
    ]).finally(() => clearTimeout(waitTimer));
    if (!this.proc) throw new Error('sidecar is not running');
    const id = this.nextId++;
    return new Promise((resolve, reject) => {
      const timer = setTimeout(() => {
        this.pending.delete(id);
        reject(new Error(`timeout: ${method}`));
      }, Math.max(1000, deadline - Date.now()));
      this.pending.set(id, { resolve, reject, timer });
      this.proc.stdin.write(`${JSON.stringify({ jsonrpc: '2.0', id, method, params })}\n`);
    });
  }

  stop() {
    this.stopping = true;
    try { this.proc?.stdin.write(`${JSON.stringify({ jsonrpc: '2.0', method: 'app.shutdown' })}\n`); } catch { /* ignore */ }
    setTimeout(() => { try { this.proc?.kill(); } catch { /* ignore */ } }, 1500);
  }
}

const sidecar = new Sidecar();

function notifyBook(job) {
  // a book download ended while Lynxx is not the focused window: system notification (Settings: download.notify)
  if (!settings.download.notify || !['done', 'failed'].includes(job.status)) return;
  if (mainWindow && !mainWindow.isDestroyed() && mainWindow.isFocused()) return;
  try {
    if (!Notification.isSupported()) return;
    const p = job.progress || {};
    const body = job.status === 'done' ? `${p.chaptersDone || 0} chương đã tải${p.chaptersFailed ? `, ${p.chaptersFailed} lỗi` : ''}` : `Lỗi: ${String(job.error || '').slice(0, 120)}`;
    const n = new Notification({ title: `${job.status === 'done' ? 'Xong' : 'Lỗi'}: ${job.title || job.label || job.url}`.slice(0, 120), body });
    n.on('click', () => { if (mainWindow && !mainWindow.isDestroyed()) { mainWindow.show(); mainWindow.focus(); send('nav:page', 'downloads'); } });
    n.show();
  } catch { /* notifications are optional */ }
}

// ---------- cover thumbnails (lynxx-thumb://t/?u=<image url>&r=<referer>) ----------
// Covers of the "Nguồn truyện" pages come from the sites: fetched by main (public hosts only, size capped) and cached on disk.

const thumbDir = () => path.join(userData(), 'cache', 'thumbs');
const thumbInflight = new Map();

async function pruneThumbCache() {
  try {
    const dir = thumbDir();
    const files = await fsp.readdir(dir);
    const stats = await Promise.all(files.map(async (f) => ({ f, s: await fsp.stat(path.join(dir, f)) })));
    let total = stats.reduce((acc, x) => acc + x.s.size, 0);
    stats.sort((a, b) => a.s.atimeMs - b.s.atimeMs);
    for (const { f, s } of stats) {
      if (total <= THUMB_CACHE_MAX) break;
      await fsp.unlink(path.join(dir, f)).catch(() => {});
      total -= s.size;
    }
  } catch { /* no cache yet */ }
}

function requestOnce(url, headers, signal) {
  // one upstream GET that does not follow a redirect: {status, location} for one, else {status, header(name), body} with body a
  // web stream (cancelling it, or the signal, ends the request). No session cookies
  return new Promise((resolve, reject) => {
    if (signal?.aborted) { reject(new Error('timeout')); return; }
    const req = net.request({ url, method: 'GET', redirect: 'manual', useSessionCookies: false });
    for (const [name, value] of Object.entries(headers)) {
      try { req.setHeader(name, value); } catch { /* a value Electron refuses (not Latin-1): that header is left out */ }
    }
    let settled = false;
    const fail = (e) => { if (!settled) { settled = true; reject(e); } };
    signal?.addEventListener('abort', () => { try { req.abort(); } catch { /* ended */ } fail(new Error('timeout')); }, { once: true });
    req.on('redirect', (status, _method, location) => {
      settled = true;
      req.abort();
      resolve({ status, location });
    });
    req.on('response', (res) => {
      settled = true;
      const it = res[Symbol.asyncIterator]();
      const body = new ReadableStream({
        async pull(c) {
          try {
            const { done, value } = await it.next();
            if (done) c.close(); else c.enqueue(value);
          } catch (e) { c.error(e); }
        },
        cancel() { try { req.abort(); } catch { /* ended */ } },
      });
      const header = (name) => { const v = res.headers[name]; return Array.isArray(v) ? v.join(', ') : String(v ?? ''); };
      resolve({ status: res.statusCode, header, body });
    });
    req.on('error', fail);
    req.end();
  });
}

async function upstreamGet(target, { referer = '', signal = null, check = assertPublicHost } = {}) {
  // redirects are followed by hand, at most THUMB_REDIRECTS_MAX times, each hop checked against private addresses
  let url = target;
  const headers = { 'user-agent': browserLikeUA(session.defaultSession.getUserAgent(), [PACKAGE_NAME, app.getName()]) };
  if (referer && /^https?:\/\//i.test(referer)) headers.referer = referer;
  for (let hop = 0; ; hop++) {
    if (!/^https?:$/.test(new URL(url).protocol)) throw new Error('unsupported scheme');
    await check(url);
    const r = await requestOnce(url, headers, signal);
    if (r.location === undefined) return { ...r, url };
    if (hop >= THUMB_REDIRECTS_MAX) throw new Error('too many redirects');
    url = new URL(r.location, url).href;
  }
}

async function fetchThumb(target, referer, check = assertPublicHost) {
  const ctrl = new AbortController();
  const timer = setTimeout(() => ctrl.abort(), THUMB_TIMEOUT_MS);
  try {
    const r = await upstreamGet(target, { referer, signal: ctrl.signal, check });
    if (r.status < 200 || r.status > 299) throw new Error(`upstream ${r.status}`);
    const type = r.header('content-type').toLowerCase();
    if (!type.startsWith('image/')) throw new Error('not an image');
    if (Number(r.header('content-length') || 0) > THUMB_SIZE_MAX) throw new Error('too large');
    const reader = r.body.getReader();
    const chunks = [];
    let total = 0;
    for (;;) {
      const { done, value } = await reader.read();
      if (done) break;
      total += value.length;
      if (total > THUMB_SIZE_MAX) throw new Error('too large');
      chunks.push(value);
    }
    return { buf: Buffer.concat(chunks), type };
  } catch (err) {
    const timedOut = ctrl.signal.aborted;
    ctrl.abort(); // ends a request whose answer was refused
    throw timedOut ? new Error('timeout') : err;
  } finally {
    clearTimeout(timer);
  }
}

function mimeOf(ext) {
  return {
    '.jpg': 'image/jpeg', '.jpeg': 'image/jpeg', '.png': 'image/png', '.gif': 'image/gif', '.webp': 'image/webp', '.avif': 'image/avif',
  }[ext.toLowerCase()] || 'application/octet-stream';
}

function registerThumbProtocol() {
  protocol.handle('lynxx-thumb', async (request) => {
    const u = new URL(request.url);
    const target = u.searchParams.get('u') || '';
    const referer = u.searchParams.get('r') || '';
    if (u.host !== 't' || !/^https?:\/\//i.test(target)) return new Response('bad url', { status: 400 });
    const key = crypto.createHash('sha1').update(`${referer}\n${target}`).digest('hex');
    const ext = (path.extname(new URL(target).pathname) || '.img').slice(0, 6).toLowerCase();
    const file = path.join(thumbDir(), `${key}${ext}`);
    try {
      const buf = await fsp.readFile(file);
      return new Response(buf, { headers: { 'content-type': mimeOf(ext), 'cache-control': 'max-age=86400' } });
    } catch { /* not cached */ }
    try {
      if (!thumbInflight.has(key)) thumbInflight.set(key, fetchThumb(target, referer).finally(() => thumbInflight.delete(key)));
      const { buf, type } = await thumbInflight.get(key);
      const tmp = `${file}.${process.pid}.${crypto.randomUUID()}.tmp`;
      fsp.mkdir(thumbDir(), { recursive: true })
        .then(() => fsp.writeFile(tmp, buf)).then(() => fsp.rename(tmp, file))
        .catch(() => fsp.unlink(tmp).catch(() => {}));
      return new Response(buf, { headers: { 'content-type': type } });
    } catch (err) {
      return new Response(String(err.message || err), { status: /blocked|scheme/.test(String(err.message)) ? 403 : 502 });
    }
  });
}

// ---------- proxy ----------

function proxyConfig(c) {
  if (!c.proxy || c.ignoreProxy) return { mode: 'system' };
  // chromium proxy rules: http://host:port, socks5://host:port (socks5h behaves like socks5: dns is resolved by the proxy).
  // Chromium takes no credentials here: they are dropped
  return { proxyRules: c.proxy.replace(/^socks5h:/i, 'socks5:').replace(/\/\/[^@/]*@/, '//') };
}

async function applyProxyToSession() {
  await session.defaultSession.setProxy(proxyConfig(settings.connection));
  // extensions' own session (persist:lynxx-plugins): their requests and page downloads follow the same proxy
  await reader?.host?.setProxy(proxyConfig(settings.connection));
}

// ---------- appearance ----------

function backgroundUrl() {
  const bg = settings.appearance.background;
  if (bg.mode === 'image' && bg.image && fs.existsSync(bg.image)) return pathToFileURL(bg.image).href;
  return pathToFileURL(iconPath(settings.appearance.icon)).href;
}

function appearanceState() {
  return {
    ...settings.appearance,
    iconUrl: pathToFileURL(iconPath(settings.appearance.icon)).href,
    backgroundUrl: backgroundUrl(),
    icons: ICONS.map((i) => ({ ...i, url: pathToFileURL(iconPath(i.id)).href })),
    themes: THEMES,
    // window icon changes live on Windows/X11/macOS; Wayland compositors read the icon from the .desktop entry only
    liveTaskbarIcon: !isWaylandLinux(),
    platform: process.platform,
  };
}

// ---------- app icon outside the window (dock, launcher, shortcuts) ----------
// The window / taskbar icon changes live on Windows and X11, the macOS dock icon can be set while running. Elsewhere the icon is
// read from files: the Linux launcher (Wayland docks use nothing else) and the shortcuts made by the Windows installer. They are
// rewritten when the icon changes. The icon of the app file itself (AppImage, .exe, .app bundle) is fixed at build time.
const LAUNCHER_MARKER = 'X-Lynxx-Launcher=true';
const ICON_FILE_PREFIX = 'lynxx-icon-';
const isWaylandLinux = () => process.platform === 'linux' && !!(process.env.WAYLAND_DISPLAY || process.env.XDG_SESSION_TYPE === 'wayland');

function applyWindowIcon() {
  const img = nativeImage.createFromPath(iconPath(settings.appearance.icon));
  if (img.isEmpty()) return;
  if (process.platform === 'darwin') app.dock?.setIcon(img);
  else mainWindow?.setIcon(img);
}

const outsideIconDir = () => path.join(userData(), 'icons');

function desktopEntryPath() {
  return path.join(os.homedir(), '.local', 'share', 'applications', `${DESKTOP_ID}.desktop`);
}

function launcherEntry(iconFile) {
  // AppImage: process.execPath points into the temporary mount, $APPIMAGE is the file the user runs
  const quote = (s) => `"${String(s).replace(/(["`$\\])/g, '\\$1')}"`;
  const exec = process.env.APPIMAGE ? quote(process.env.APPIMAGE)
    : app.isPackaged ? quote(process.execPath) : `${quote(process.execPath)} ${quote(__dirname)}`;
  return [
    '[Desktop Entry]', 'Type=Application', 'Name=Lynxx', 'Comment=Comic, novel and book reader', `Exec=${exec} %U`,
    `Icon=${iconFile}`, 'Terminal=false', 'Categories=Office;Viewer;Graphics;', `StartupWMClass=${DESKTOP_ID}`, LAUNCHER_MARKER, '',
  ].join('\n');
}

const launcherIconFile = () => path.join(outsideIconDir(), `${ICON_FILE_PREFIX}${settings.appearance.icon}.png`);

async function writeLauncher() {
  const iconFile = launcherIconFile();
  await fsp.mkdir(outsideIconDir(), { recursive: true });
  // a new file name per icon: desktop shells cache icons by path, an overwritten file is often not picked up
  await fsp.copyFile(iconPath(settings.appearance.icon), iconFile);
  const file = desktopEntryPath();
  await fsp.mkdir(path.dirname(file), { recursive: true });
  const tmp = `${file}.tmp-${process.pid}`;
  await fsp.writeFile(tmp, launcherEntry(iconFile));
  await fsp.rename(tmp, file); // the shell watches this folder: it only ever sees a complete file
  for (const f of await fsp.readdir(outsideIconDir()).catch(() => [])) {
    const old = path.join(outsideIconDir(), f);
    if (old !== iconFile && f.endsWith('.png') && (f.startsWith(ICON_FILE_PREFIX) || f === 'lynxx.png')) await fsp.unlink(old).catch(() => {});
  }
  return file;
}

async function installLauncher() {
  if (process.platform !== 'linux') return { ok: false, message: 'Chỉ cần trên Linux' };
  return { ok: true, path: await writeLauncher(), restartForDock: isWaylandLinux() };
}

async function launcherState() {
  // 'missing' | 'ours' | 'foreign': a launcher written by something else (ex. AppImage integration tools) is never touched.
  // Ours: the marker, or the entry older versions wrote without it
  let text;
  try { text = await fsp.readFile(desktopEntryPath(), 'utf8'); } catch { return 'missing'; }
  const ours = text.includes(LAUNCHER_MARKER) || (/^Name=Lynxx$/m.test(text) && /^Comment=Booru downloader$/m.test(text));
  return ours ? 'ours' : 'foreign';
}

async function syncLauncher() {
  // -> 'unsupported' | 'missing' | 'foreign' | 'unchanged' | 'updated'
  if (process.platform !== 'linux') return 'unsupported';
  const state = await launcherState();
  if (state !== 'ours') return state;
  const current = await fsp.readFile(desktopEntryPath(), 'utf8').catch(() => '');
  if (current === launcherEntry(launcherIconFile()) && fs.existsSync(launcherIconFile())) return 'unchanged';
  await writeLauncher();
  return 'updated';
}

async function writeIco(iconId) {
  const img = nativeImage.createFromPath(iconPath(iconId));
  if (img.isEmpty()) throw new Error(`icon ${iconId} not found`);
  const pngs = [256, 48, 32, 16].map((size) => ({ size, data: img.resize({ width: size, height: size, quality: 'best' }).toPNG() }));
  const file = path.join(outsideIconDir(), `${ICON_FILE_PREFIX}${iconId}.ico`);
  await fsp.mkdir(outsideIconDir(), { recursive: true });
  await fsp.writeFile(file, pngsToIco(pngs));
  return file;
}

async function listShortcuts(root, depth = 1) {
  const out = [];
  for (const d of await fsp.readdir(root, { withFileTypes: true }).catch(() => [])) {
    const full = path.join(root, d.name);
    if (d.isFile() && d.name.toLowerCase().endsWith('.lnk')) out.push(full);
    else if (d.isDirectory() && depth > 0) out.push(...await listShortcuts(full, depth - 1));
  }
  return out;
}

async function syncWindowsShortcuts({ startup = false } = {}) {
  // shortcuts of the installer (Start menu, desktop, pinned taskbar) pointing at this exe get the selected icon
  if (process.platform !== 'win32' || !app.isPackaged) return 0;
  const appData = app.getPath('appData');
  const roots = [path.join(appData, 'Microsoft', 'Windows', 'Start Menu', 'Programs'), app.getPath('desktop'),
    path.join(appData, 'Microsoft', 'Internet Explorer', 'Quick Launch', 'User Pinned', 'TaskBar')];
  const exe = path.resolve(process.execPath).toLowerCase();
  const ours = [];
  for (const root of roots) {
    for (const lnk of await listShortcuts(root)) {
      try {
        const d = shell.readShortcutLink(lnk);
        if (path.resolve(d.target || '').toLowerCase() === exe) ours.push([lnk, d]);
      } catch { /* not a readable shortcut */ }
    }
  }
  // at startup with the default icon and untouched shortcuts there is nothing to do (the exe icon is the default one)
  if (startup && settings.appearance.icon === DEFAULT_SETTINGS.appearance.icon && ours.every(([, d]) => !d.icon)) return 0;
  if (!ours.length) return 0;
  const ico = await writeIco(settings.appearance.icon);
  let n = 0;
  for (const [lnk, d] of ours) {
    if (d.icon && path.resolve(d.icon).toLowerCase() === ico.toLowerCase()) continue;
    if (shell.writeShortcutLink(lnk, 'update', { icon: ico, iconIndex: 0 })) n++;
  }
  return n;
}

async function syncIcon({ startup = false } = {}) {
  // -> { launcher, shortcuts, restartForDock }: what was updated outside the window
  const r = { launcher: 'unsupported', shortcuts: 0, restartForDock: false };
  try {
    if (!startup || process.platform === 'darwin') applyWindowIcon(); // the window got its icon at creation, the dock did not
    r.launcher = await syncLauncher();
    r.shortcuts = await syncWindowsShortcuts({ startup });
  } catch (err) {
    r.error = err.message;
    send('log', { source: 'icon', text: `icon sync failed: ${err.message}` });
  }
  // Wayland docks read the launcher when the app starts: the running window keeps its icon until the app is reopened
  r.restartForDock = isWaylandLinux() && ['updated', 'unchanged'].includes(r.launcher) && !startup;
  return r;
}

// ---------- IPC ----------

const RENDERER_URL = pathToFileURL(path.join(__dirname, 'renderer', 'index.html')).href;
// sidecar methods the renderer may call directly; everything else goes through a main-side handler (app/plugin/ipc.js)
const RENDERER_RPC = ['app.info', 'formats.supported'];

function trustedSender(e) {
  const url = e.senderFrame?.url || '';
  return url === RENDERER_URL || url.startsWith(`${RENDERER_URL}#`) || url.startsWith(`${RENDERER_URL}?`);
}

function handle(channel, fn) {
  ipcMain.handle(channel, (e, ...a) => {
    if (!trustedSender(e)) throw new Error('untrusted sender');
    return fn(e, ...a);
  });
}

function settingsView() {
  return { settings, appearance: appearanceState() };
}

function registerIpc() {
  handle('sidecar:getStatus', () => sidecar.status || { ok: false, error: 'starting' });
  handle('rpc', async (_e, method, params) => {
    if (!RENDERER_RPC.includes(method)) throw new Error(`method not allowed: ${method}`);
    return sidecar.call(method, isPlainObject(params) ? params : {});
  });

  handle('settings:get', () => settingsView());
  handle('settings:set', async (_e, patch) => {
    if (!isPlainObject(patch)) throw new Error('invalid settings patch');
    delete patch.version;
    const py = patch.advanced?.pythonPath;
    if (py !== undefined && py !== settings.advanced.pythonPath) {
      // the python path is executed by the app: changing it requires a native confirmation
      const valid = py === '' || (typeof py === 'string' && path.isAbsolute(py) && fs.existsSync(py) && fs.statSync(py).isFile());
      if (!valid || (py && !(await confirmMain('Dùng chương trình này làm Python cho Lynxx?', py)))) delete patch.advanced.pythonPath;
    }
    if (isPlainObject(patch.connection) && patch.connection.proxy !== undefined) {
      const proxy = String(patch.connection.proxy || '').trim();
      if (proxy && !/^(https?|socks4|socks5h?):\/\/[^\s/]+(:\d+)?\/?$/i.test(proxy)) throw new Error('Proxy phải có dạng http://máy:cổng hoặc socks5://máy:cổng');
      patch.connection.proxy = proxy;
    }
    if (isPlainObject(patch.appearance)) delete patch.appearance.background?.image; // only through the image dialog
    settings = deepMerge(settings, patch);
    sanitizeSettings();
    saveSettings();
    if (patch.connection) await applyProxyToSession();
    let iconSync;
    if (patch.appearance) {
      if (patch.appearance.icon) iconSync = await syncIcon();
      send('appearance:changed', appearanceState());
    }
    return { ...settingsView(), iconSync };
  });

  handle('dialog:folder', async (_e, current) => {
    const r = await dialog.showOpenDialog(mainWindow, { properties: ['openDirectory', 'createDirectory'], defaultPath: current || undefined });
    return r.canceled ? '' : r.filePaths[0];
  });
  handle('dialog:image', async () => {
    const r = await dialog.showOpenDialog(mainWindow, {
      properties: ['openFile'], filters: [{ name: 'Ảnh', extensions: ['png', 'jpg', 'jpeg', 'webp', 'gif'] }],
    });
    if (r.canceled) return '';
    approvedPaths.add(r.filePaths[0]);
    return r.filePaths[0];
  });
  handle('dialog:document', async () => {
    const r = await dialog.showOpenDialog(mainWindow, {
      properties: ['openFile'], filters: [{ name: 'Sách, truyện, tài liệu', extensions: IMPORT_EXTENSIONS }],
    });
    if (r.canceled) return '';
    approvedPaths.add(r.filePaths[0]);
    return r.filePaths[0];
  });

  handle('appearance:setBackgroundImage', async (_e, file) => {
    if (!file) {
      settings.appearance.background.mode = 'icon';
    } else {
      if (!approvedPaths.has(file)) throw new Error('choose the image with the file dialog first');
      const ext = path.extname(file).toLowerCase();
      if (!['.png', '.jpg', '.jpeg', '.webp', '.gif'].includes(ext)) throw new Error('unsupported image type');
      const dest = path.join(userData(), 'backgrounds', `${crypto.randomUUID()}${ext}`);
      await fsp.mkdir(path.dirname(dest), { recursive: true });
      await fsp.copyFile(file, dest);
      const old = settings.appearance.background.image;
      if (old && old.startsWith(path.join(userData(), 'backgrounds'))) fsp.unlink(old).catch(() => {});
      settings.appearance.background = { ...settings.appearance.background, mode: 'image', image: dest };
    }
    saveSettings();
    const st = appearanceState();
    send('appearance:changed', st);
    return st;
  });

  handle('app:relaunch', () => requestQuit(true)); // false: the user kept the app running (download jobs)
  handle('app:info', () => {
    const core = coreCommand();
    return {
      version: app.getVersion(), electron: process.versions.electron, platform: process.platform, userData: userData(),
      python: core.cmd, core: core.frozen ? 'bundled' : 'python', packaged: app.isPackaged, repo: REPO_ROOT, sidecar: sidecar.info,
      launcherInstalled: fs.existsSync(desktopEntryPath()),
    };
  });
  handle('launcher:install', () => installLauncher());
  handle('shell:open', (_e, p) => {
    // folders only: files could be executables or .desktop launchers
    const target = String(p || '');
    if (!path.isAbsolute(target) || !fs.existsSync(target) || !fs.statSync(target).isDirectory()) return 'not a folder';
    return shell.openPath(target);
  });
  handle('shell:external', (_e, url) => (/^https?:\/\//.test(url) ? shell.openExternal(url) : false));
  handle('reader:import', async (_e, p, title) => {
    const target = String(p || '');
    if (!approvedPaths.has(target)) return { ok: false, error: 'file phải được chọn bằng hộp thoại Lynxx' };
    const ext = path.extname(target).slice(1).toLowerCase();
    if (!IMPORT_EXTENSIONS.includes(ext)) return { ok: false, error: 'định dạng file chưa được hỗ trợ' };
    const imported = await sidecar.call('library.import', { source: target, title: String(title || '') }, 1800000);
    return { ok: true, ...imported };
  });
}

// ---------- quitting ----------

let quitApproved = false; // the user confirmed (or nothing needed confirming): before-quit / window close pass through
let quitFlow = null;
// native confirmation, replaced by the self-test
let askQuit = (message, detail, okLabel) => confirmMain(message, detail, ['Ở lại', okLabel]);

let reader = null; // extensions, book downloads, library, exports (app/plugin/ipc.js)

function jobsNeedingConfirm() {
  return reader ? reader.jobs.active().filter((j) => j.status === 'queued' || j.status === 'running') : [];
}

async function confirmQuit(relaunch) {
  const pending = jobsNeedingConfirm();
  const running = pending.filter((j) => j.status === 'running').length;
  const parts = [running && `${running} truyện đang tải`, pending.length - running && `${pending.length - running} truyện đang chờ`].filter(Boolean);
  return askQuit(relaunch ? 'Khởi động lại Lynxx?' : 'Thoát Lynxx?',
    `Đang có ${parts.join(' và ')}. Nếu tiếp tục, các lượt tải này sẽ dừng (chương đã tải xong vẫn được giữ).`,
    relaunch ? 'Dừng tải và khởi động lại' : 'Dừng tải và thoát');
}

function requestQuit(relaunch = false) {
  // quit / relaunch with book downloads queued or running: ask first, then stop them before exiting
  if (!quitFlow) {
    quitFlow = (async () => {
      try {
        if (!quitApproved && jobsNeedingConfirm().length && !(await confirmQuit(relaunch))) return false;
        quitApproved = true;
        reader?.shutdown();
        // AppImage: the running binary lives in a mount that goes away with this process, start the AppImage file again
        if (relaunch) app.relaunch(process.env.APPIMAGE ? { execPath: process.env.APPIMAGE, args: process.argv.slice(1) } : undefined);
        app.quit();
        return true;
      } finally {
        quitFlow = null;
      }
    })();
  }
  return quitFlow;
}

// ---------- window ----------

function setupMenu() {
  if (process.platform !== 'darwin') {
    Menu.setApplicationMenu(null); // no menu bar on Windows / Linux (text fields handle their own shortcuts)
    return;
  }
  // macOS: Cmd+C / Cmd+V / Cmd+A / Cmd+Z only reach text fields through menu roles
  Menu.setApplicationMenu(Menu.buildFromTemplate([
    { role: 'appMenu' },
    { role: 'editMenu', label: 'Sửa' },
    { role: 'windowMenu', label: 'Cửa sổ' },
  ]));
}

function createWindow() {
  mainWindow = new BrowserWindow({
    width: settings.window.width, height: settings.window.height, minWidth: 1080, minHeight: 680,
    title: 'Lynxx', icon: iconPath(settings.appearance.icon), backgroundColor: '#061015', show: false,
    autoHideMenuBar: true,
    // plugins: Chromium's built-in PDF viewer for the reader (PDF files of the managed library)
    webPreferences: { preload: path.join(__dirname, 'preload.js'), contextIsolation: true, nodeIntegration: false, sandbox: true, plugins: true },
  });
  mainWindow.loadFile(path.join(__dirname, 'renderer', 'index.html'));
  mainWindow.once('ready-to-show', () => mainWindow.show());
  mainWindow.webContents.once('did-finish-load', () => flushStartupNotices());
  mainWindow.on('close', (e) => {
    if (!quitApproved && jobsNeedingConfirm().length) {
      e.preventDefault();
      requestQuit(false);
      return;
    }
    const [width, height] = mainWindow.getSize();
    settings.window = { width, height };
    saveSettings();
  });
  mainWindow.webContents.setWindowOpenHandler(({ url }) => {
    if (/^https?:\/\//.test(url)) shell.openExternal(url);
    return { action: 'deny' };
  });
  // the extension sandbox keeps hidden windows: without this, closing the app window would never fire window-all-closed
  mainWindow.on('closed', () => {
    reader?.shutdown();
    for (const w of BrowserWindow.getAllWindows()) if (!w.isDestroyed()) w.destroy();
    mainWindow = null;
  });
  // dropping a file/link or any script navigation must never replace the app page (preload would expose window.lynxx)
  mainWindow.webContents.on('will-navigate', (e, url) => { if (url !== RENDERER_URL) e.preventDefault(); });
  mainWindow.webContents.on('will-attach-webview', (e) => e.preventDefault());
  mainWindow.webContents.session.setPermissionRequestHandler((_wc, _perm, cb) => cb(false));
  if (process.argv.includes('--lynxx-devtools')) mainWindow.webContents.openDevTools({ mode: 'detach' });
}

// ---------- self test (LYNXX_SELFTEST_DIR=<dir> [LYNXX_USER_DATA=<scratch folder>] electron .) ----------
// Report: <dir>/report.json (tools/check_selftest.py fails on any step with ok: false). LYNXX_SELFTEST_QUICK: no screenshots.
// Nothing needs the network: the library steps import a file made here, the extension step runs scripts that fetch nothing.
// What a step adds to the profile (a collection, an extension) is removed by the same step.

async function runSelfTest(dir) {
  const wait = (ms) => new Promise((r) => setTimeout(r, ms));
  const quick = Boolean(process.env.LYNXX_SELFTEST_QUICK);
  const shot = async (name) => {
    if (quick) return;
    const img = await mainWindow.webContents.capturePage();
    await fsp.mkdir(dir, { recursive: true });
    await fsp.writeFile(path.join(dir, `${name}.png`), img.toPNG());
  };
  const js = (code) => mainWindow.webContents.executeJavaScript(code, true);
  const step = async (name, fn) => {
    try { report[name] = await fn(); } catch (err) { report[name] = { ok: false, error: String(err?.stack || err) }; }
  };
  const report = { settingsLoad: settingsLoadInfo, core: coreCommand() };
  const scratch = await fsp.mkdtemp(path.join(os.tmpdir(), 'lynxx-selftest-'));
  try {
    await sidecar.ready;
    await wait(1200);
    await step('coreStatus', async () => {
      const st = await js('window.lynxx.sidecarStatus()');
      return { ...st, ok: st.ok === true && /^\d+\.\d+/.test(String(st.info?.version || '')) };
    });
    // what the renderer may and may not ask: the generic rpc channel is limited to RENDERER_RPC
    await step('contract', async () => {
      const info = await js('window.lynxx.rpc("app.info")');
      const formats = await js('window.lynxx.rpc("formats.supported")');
      const refused = await js(`Promise.all(['library.collection.delete', 'plugins.unpack', 'export.build'].map((m) =>
        window.lynxx.rpc(m, {}).then(() => 'allowed', (e) => String(e.message || e))))`);
      const out = {
        appInfo: { version: info.version, ok: info.name === 'Lynxx' && Boolean(info.version) },
        formats: { count: formats.extensions?.length, ok: ['epub', 'pdf', 'mobi', 'docx', 'cbz'].every((x) => formats.extensions?.includes(x)) },
        // a refused call is the expected outcome: tools/check_selftest.py knows this step as ok: false on purpose
        notAllowed: { refused, ok: false },
      };
      out.ok = out.appInfo.ok && out.formats.ok && refused.every((r) => /not allowed/.test(r));
      return out;
    });
    // the shell: only the Lynxx pages, the theme of the icon, nothing left of the booru app it was forked from
    await step('ui', async () => {
      const r = await js(`(() => {
        const pages = [...document.querySelectorAll('#nav button[data-page]')].map((b) => b.dataset.page);
        const text = document.body.innerText;
        const accent = getComputedStyle(document.documentElement).getPropertyValue('--accent').trim();
        return { pages, theme: document.documentElement.dataset.theme, accent, booru: /booru|rule ?34|danbooru|gelbooru|e621|tags a–z/i.test(text) };
      })()`);
      r.ok = r.pages.join() === 'shelf,sources,reader,downloads,settings,log' && r.theme === 'abyss' && r.accent === '#2fd4d8' && !r.booru;
      return r;
    });
    await shot('01-shelf');
    // library: import a file (main-side, as after the file dialog), read it in the reader, export it, delete it
    await step('library', async () => {
      const src = path.join(scratch, 'Sách thử.txt');
      await fsp.writeFile(src, 'Chương 1: Mở đầu\n\nMột đoạn văn.\n\nChương 2: Tiếp theo\n\nĐoạn văn khác.\n', 'utf8');
      const imported = await sidecar.call('library.import', { source: src, title: 'Lynxx selftest' }, 120000);
      const r = { chapters: imported.chapters, kind: imported.kind };
      try {
        const shelf = await js('window.lynxx.shelf.collections()');
        r.listed = shelf.some((c) => c.id === imported.collectionId);
        const chapter = await js(`window.lynxx.reader.chapter(${JSON.stringify(imported.chapterId)})`);
        r.reader = { kind: chapter.kind, hasText: chapter.kind === 'text' && /Một đoạn văn/.test(String(chapter.html || '')) };
        const out = path.join(scratch, 'out.epub');
        const built = await reader.exportCollection({ collectionId: imported.collectionId, format: 'epub3', options: { scope: 'all' }, output: out });
        const head = (await fsp.readFile(out)).subarray(0, 58).toString('latin1');
        r.export = { size: built.size, zip: head.startsWith('PK') && head.includes('application/epub+zip') };
      } finally {
        r.deleted = (await sidecar.call('library.collection.delete', { collectionId: imported.collectionId })).ok === true;
      }
      r.ok = r.chapters === 2 && r.listed && r.reader?.hasText && r.export?.zip && r.deleted;
      return r;
    });
    // extensions: the packaged sandbox page loads, the synchronous bridge answers, CryptoJS and storage work
    await step('extensions', async () => {
      const ext = path.join(scratch, 'ext');
      await fsp.mkdir(path.join(ext, 'src'), { recursive: true });
      await fsp.writeFile(path.join(ext, 'plugin.json'), JSON.stringify({
        metadata: { name: 'Lynxx selftest', author: 'lynxx', version: 1, source: 'https://selftest.invalid', regexp: 'selftest\\.invalid', type: 'novel' },
        script: { toc: 'toc.js', chap: 'chap.js' },
      }));
      await fsp.writeFile(path.join(ext, 'src', 'toc.js'), 'function execute(url) { return Response.success([{ name: "Chương 1", url: url + "/c1" }]); }');
      await fsp.writeFile(path.join(ext, 'src', 'chap.js'), `load('crypto.js');
function execute(url) {
  localStorage.setItem('k', 'v');
  return Response.success('<p>' + [CryptoJS.MD5('abc').toString(), localStorage.getItem('k'), Html.parse('<b>x</b>').select('b').text()].join('|') + '</p>');
}`);
      const info = await reader.host.installFromDir(ext, { origin: 'folder' });
      try {
        const { svc } = reader.host.service(info.id);
        const toc = await svc.toc('https://selftest.invalid/book');
        const chap = await svc.chap('https://selftest.invalid/book/c1');
        const r = { id: info.id, toc: toc.length, chap: String(chap.html || '').replace(/<\/?p>/g, '') };
        r.ok = r.toc === 1 && r.chap === '900150983cd24fb0d6963f7d28e17f72|v|x';
        return r;
      } finally {
        await reader.host.uninstall(info.id).catch(() => {});
      }
    });
    await step('pages', async () => {
      const r = {};
      for (const page of ['sources', 'downloads', 'settings', 'log', 'shelf']) {
        await js(`document.querySelector('#nav button[data-page="${page}"]').click()`);
        await wait(300);
        r[page] = await js(`document.querySelector('#page-${page}')?.classList.contains('active') === true`);
        await shot(`02-${page}`);
      }
      r.ok = Object.values(r).every(Boolean);
      return r;
    });
    // quit with a book download running: asked once, a refusal keeps the app open
    await step('quitGuard', async () => {
      const fake = { id: 'selftest-job', status: 'running' };
      reader.jobs.jobs.set(fake.id, fake);
      const realAsk = askQuit;
      const asked = [];
      try {
        askQuit = async (message, detail) => { asked.push(`${message} ${detail}`); return false; };
        const declined = await requestQuit(false);
        return { declined, asked, ok: declined === false && asked.length === 1 && /Thoát Lynxx/.test(asked[0]) };
      } finally {
        askQuit = realAsk;
        reader.jobs.jobs.delete(fake.id);
      }
    });
    report.sidecar = sidecar.info;
  } catch (err) {
    report.error = String(err && err.stack || err);
  } finally {
    await fsp.rm(scratch, { recursive: true, force: true }).catch(() => {});
  }
  await fsp.mkdir(dir, { recursive: true });
  await fsp.writeFile(path.join(dir, 'report.json'), JSON.stringify(report, null, 2));
  quitApproved = true; // a job left over by a failed step must not open the quit confirmation
  app.quit();
}

// ---------- start ----------

protocol.registerSchemesAsPrivileged([
  { scheme: 'lynxx-thumb', privileges: { standard: true, secure: true, supportFetchAPI: true, corsEnabled: true, stream: true } },
  { scheme: 'lynxx-lib', privileges: { standard: true, secure: true, supportFetchAPI: true, stream: true } },
]);

if (!app.requestSingleInstanceLock()) {
  app.quit();
} else {
  app.on('second-instance', () => {
    if (mainWindow && !mainWindow.isDestroyed()) { if (mainWindow.isMinimized()) mainWindow.restore(); mainWindow.focus(); }
  });
  app.whenReady().then(async () => {
    loadSettings();
    registerThumbProtocol();
    registerLibProtocol(userData());
    registerIpc();
    reader = setupReader({
      handle, sidecar, send, approvedPaths, userData: userData(), assertPublicHost, isDev: !app.isPackaged,
      confirm: (message, detail) => confirmMain(message, detail),
      getWindow: () => (mainWindow && !mainWindow.isDestroyed() ? mainWindow : null),
      browserUA: (ua) => browserLikeUA(ua, [PACKAGE_NAME, app.getName()]),
      log: (text) => send('log', { source: 'plugins', text: redact(String(text)) }),
      notifyBook: (job) => notifyBook(job),
    });
    await applyProxyToSession();
    sidecar.start();
    setupMenu();
    createWindow();
    syncIcon({ startup: true }).catch(() => {}); // macOS dock, launcher / shortcuts written by an older version or another icon
    pruneThumbCache();
    if (process.env.LYNXX_SELFTEST_DIR) mainWindow.webContents.once('did-finish-load', () => runSelfTest(process.env.LYNXX_SELFTEST_DIR));
  });
  app.on('window-all-closed', () => app.quit());
  app.on('before-quit', (e) => {
    // Cmd+Q, the macOS app menu, OS logout, app.quit(): downloads still queued / running need the user's confirmation first
    if (!quitApproved && jobsNeedingConfirm().length) {
      e.preventDefault();
      requestQuit(false);
      return;
    }
    quitApproved = true;
    reader?.shutdown();
    sidecar.stop();
  });
}
