// Lynxx desktop: Electron main process
// - spawns the Python sidecar (python -m lynxx.server, JSON-RPC over stdio) for queries
// - runs downloads as separate CLI processes (LYNXX_IPC=1 python -m lynxx ...) with progress on stdout
// - owns settings (userData/settings.json), secrets (safeStorage), thumbnails cache (lynxx-thumb://), appearance
'use strict';

const {
  app, BrowserWindow, ipcMain, dialog, shell, nativeImage, protocol, net, safeStorage, session, Menu, Notification,
} = require('electron');
const path = require('node:path');
const fs = require('node:fs');
const fsp = fs.promises;
const os = require('node:os');
const crypto = require('node:crypto');
const readline = require('node:readline');
const { spawn, spawnSync } = require('node:child_process');
const { pathToFileURL } = require('node:url');
const netlib = require('node:net');
const dnsp = require('node:dns').promises;

// Linux: Electron derives the window WM_CLASS (X11) and app_id (Wayland) from the desktop name ('lynxx.desktop' -> 'lynxx'),
// so launchers must use StartupWMClass=lynxx (lowercase) for docks to group the window with its launcher
const DESKTOP_ID = 'lynxx';
const { pngsToIco } = require('./icon-utils');
const { shq, hasTagBlock, withoutTagBlock, withTagBlock, scheduleWhen } = require('./tag-schedule');
const {
  CLEARANCE_COOKIE, browserLikeUA, sameSite, siteBaseHost, cookieForHost, clearanceHosts, envProxyRules, ChallengeTracker, mergeHttpMaps,
  withSiteEntry,
} = require('./site-verify');

app.setName('Lynxx');
if (process.env.LYNXX_USER_DATA) app.setPath('userData', path.resolve(process.env.LYNXX_USER_DATA));
if (process.platform === 'linux' && typeof app.setDesktopName === 'function') app.setDesktopName(`${DESKTOP_ID}.desktop`);
// <input type=date> follows the Chromium UI language: Vietnamese shows dd/mm/yyyy like the rest of the UI (before 'ready')
app.commandLine.appendSwitch('lang', 'vi');

const REPO_ROOT = path.resolve(__dirname, '..');
const CORE_NAME = process.platform === 'win32' ? 'lynxx-core.exe' : 'lynxx-core';
const ICON_DIR = path.join(__dirname, 'assets', 'icons');
const ICONS = [
  { id: 'full-mask-orange', name: 'Full mask · Orange' },
  { id: 'exposed-jaw-orange', name: 'Exposed jaw · Orange' },
  { id: 'full-mask-monochrome', name: 'Full mask · Mono' },
  { id: 'exposed-jaw-monochrome', name: 'Exposed jaw · Mono' },
];
const THEMES = ['ember', 'graphite', 'paper', 'oled'];
const THUMB_CACHE_MAX = 512 * 1024 * 1024;
const THUMB_SIZE_MAX = 25 * 1024 * 1024;
const THUMB_TIMEOUT_MS = 20000;
const PROGRESS_PREFIX = '@@LYNXX_PROGRESS ';
const SETTINGS_VERSION = 3;
const RE_ABBR = /^[a-z0-9]{2,4}$/; // module abbreviation (extension manifest 'abbr')
const RE_PICK_ID = /^[A-Za-z0-9_-]{1,64}$/; // a post id of the Preview ("Tải đã chọn" without id search, see pickArgs)
const PICK_IDS_MAX = 1000;
const VIDEO_HEIGHT_MAX = 100000; // -max_vres
const MEDIA_REDIRECTS_MAX = 5; // thumbnails / lightbox media: redirects followed, each hop checked (see upstreamGet)
const HISTORY_MAX = 50;
const SAVED_MAX = 200;
const QUERY_TAGS_MAX = 2000;
const SAVED_NAME_MAX = 80;
const QUIT_GRACE_MS = 5000; // quitting with running jobs: time given to 'cancel' before the processes are killed

const DEFAULT_SETTINGS = {
  version: SETTINGS_VERSION,
  appearance: {
    theme: 'ember',
    icon: 'full-mask-orange',
    background: { mode: 'icon', image: '', blur: 4, opacity: 0.9, dim: 0.18 },
  },
  search: {
    module: 'rx', autocomplete: true, previewOnSearch: true, safePreview: false, filtersOpen: true,
    tagLimits: {}, // abbr -> max tags per search of the user's account (0 / missing = module default)
    history: [], // [{module, tags, at}] newest first
    saved: [], // [{name, module, tags, at}]
  },
  download: {
    path: path.join(os.homedir(), 'Downloads', 'Lynxx'), threads: 4, images: true, videos: true, audio: true, videoFormat: '',
    // op 'exclude': the picked labels are dropped; mode 'model': image model; verifyMedia: videos / animated pictures labeled by the
    // model are checked again on several frames once downloaded (-gender_verify_media), a mismatch is moved to _gender_mismatch/
    gender: { select: [], conf: 'medium', strict: false, op: 'keep', mode: 'tags', verifyMedia: false },
    // lowRes: the site's lighter image instead of the original (images only). maxVideoHeight: the best video file not higher than
    // this (-max_vres; 0 = the best one), for modules with the 'video_quality_choice' capability
    lowRes: false, maxVideoHeight: 0, prefix: true, appendInfo: false, reverse: false, limit: 0, parchi: false, preserveDate: false,
    skipKnown: false, // -skip_known: skip posts found in the local download index (core lynxx/dlindex.py)
    // -safety_blacklist: drop posts whose tags suggest minors (core lynxx/safety.py); its own filter, off by default,
    // independent of the gender filter (product decision 2026-11-18). Saved with each query (q.safetyBlacklist)
    safetyBlacklist: false,
    subfolderPerQuery: false, dumpTags: false, dumpSources: false, dumpComments: false, dumpHashes: false, infoMode: 'run',
    warnNonEmpty: true, maxConcurrentJobs: 1, notify: true,
  },
  connection: {
    proxy: '', ignoreProxy: false, proxyNoDownload: false, timeout: 0, retries: 0, headers: {}, cookies: {},
    // abbr -> {name: value}: sent to that website only, over the maps above for the same name (a browser User-Agent and the
    // cf_clearance cookie of a bot challenge the user passed in a browser must not reach other websites, see connectionArgs)
    siteHeaders: {}, siteCookies: {},
    // abbr -> requests a minute the site gave the user's API key (0 / missing: the limit the site publishes, manifest auth.rateLimit);
    // written to <data dir>/ratelimit/limits.json, read by every core process (see syncKeyRateLimits)
    keyRateLimits: {},
  },
  // schedule: the system scheduler updates the lists even while the app is closed (crontab / Task Scheduler, see tagScheduleStatus)
  tags: { autoUpdate: false, autoUpdateDays: 7, lastAutoUpdate: {}, schedule: { enabled: false, frequency: 'weekly' } },
  // the official signed catalog (tools/build_catalog.py, docs/operations.md 3d); the user may point it elsewhere
  advanced: { pythonPath: '', verbose: false, catalogUrl: 'https://github.com/Leiz-Workspace/Lynxx/releases/download/catalog/index.json' },
  ai: { model: 'claude-opus-5-5', maxUsd: 2 }, // extension generator (the Claude API key itself is a secret, see AI_SECRET)
  // gender image model: runs in a model server (python -m lynxx.model_server), the app only calls its API (token: MODEL_SECRET).
  // enabled: the user turned it on (the renderer checks the server's GPU / VRAM first); off: jobs filter by tags only
  genderModel: { url: 'http://127.0.0.1:8765', enabled: false },
  // "Đã tải" page: deleted files go to the trash (core lynxx/dlindex.py); trashDays: emptied of files older than this (0 = never)
  library: { trashDays: 30 },
  window: { width: 1360, height: 860 },
};

// ---------- utils ----------

const userData = () => app.getPath('userData');
const settingsFile = () => path.join(userData(), 'settings.json');
let settings = structuredClone(DEFAULT_SETTINGS);
let secrets = {}; // module abbr -> { enc: base64, plain?: string }
let mainWindow = null;

function deepMerge(base, patch) {
  if (patch === null || typeof patch !== 'object' || Array.isArray(patch)) return patch;
  const out = { ...(base && typeof base === 'object' && !Array.isArray(base) ? base : {}) };
  for (const [k, v] of Object.entries(patch)) out[k] = (v && typeof v === 'object' && !Array.isArray(v)) ? deepMerge(out[k], v) : v;
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

const isPlainObject = (v) => Boolean(v) && typeof v === 'object' && !Array.isArray(v);

// ---------- security helpers ----------

const RE_EXT_ID = /^[a-z][a-z0-9_-]{1,31}$/;
const RE_SHA256 = /^[0-9a-fA-F]{64}$/;
const AI_SECRET = '@anthropic'; // secrets key of the Claude API key: never an extension abbr (those match RE_ABBR)
const MODEL_SECRET = '@gendermodel'; // token of the gender model server
const RE_MODEL_URL = /^https?:\/\/[^\s/?#@]+(\/[^\s?#]*)?$/; // the core checks the rest (plain http only on this computer / LAN)
const RE_GEN_SESSION = /^\d{8}-\d{6}-[0-9a-f]{6}$/; // extension generator session folder (lynxx/aigen/session.py)
const GEN_MODELS = ['claude-opus-5-5', 'claude-sonnet-5-5'];
const approvedPaths = new Set(); // files picked by the user through native dialogs (renderer cannot invent paths)

function redact(text) {
  return String(text)
    .replace(/([?&](?:api_key|login|user_id)=)[^&\s"]+/g, '$1<REDACTED>')
    .replace(/(-api_key|-cookies?|-headers?|-proxy)(\s+|=)(\S+)/g, '$1$2<REDACTED>');
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

function sanitizeSecrets(obj) {
  const out = {};
  if (!isPlainObject(obj)) return out;
  for (const [k, v] of Object.entries(obj)) {
    if (!(RE_ABBR.test(k) || k === AI_SECRET || k === MODEL_SECRET) || !isPlainObject(v)) continue;
    if (typeof v.enc === 'string' && v.enc) out[k] = { enc: v.enc };
    else if (k === AI_SECRET) continue; // the Claude API key is only ever stored encrypted
    else if (typeof v.plain === 'string' && v.plain) out[k] = { plain: v.plain };
  }
  return out;
}

function matchingBrace(text, start) {
  let depth = 0;
  let inString = false;
  for (let i = start; i < text.length; i++) {
    const c = text[i];
    if (inString) {
      if (c === '\\') i++;
      else if (c === '"') inString = false;
    } else if (c === '"') inString = true;
    else if (c === '{') depth++;
    else if (c === '}' && --depth === 0) return i;
  }
  return -1;
}

function recoverSecrets(text) {
  // corrupt settings file: the top-level "secrets" block is written last. Parse it as a whole, else entry by entry (truncated file)
  const starts = [...text.matchAll(/"secrets"\s*:\s*\{/g)];
  if (!starts.length) return {};
  const m = starts[starts.length - 1];
  const start = m.index + m[0].length - 1;
  const end = matchingBrace(text, start);
  if (end > 0) {
    try { return sanitizeSecrets(JSON.parse(text.slice(start, end + 1))); } catch { /* entry by entry */ }
  }
  const out = {};
  const re = /"([a-z0-9]{2,4})"\s*:\s*\{\s*"(enc|plain)"\s*:\s*("(?:[^"\\\n]|\\.)*")\s*\}/g;
  for (const e of text.slice(start).matchAll(re)) {
    try { out[e[1]] = { [e[2]]: JSON.parse(e[3]) }; } catch { /* broken entry */ }
  }
  return sanitizeSecrets(out);
}

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
  // -> { raw } for a valid file, {} when there is none, { error, recovered } when it is corrupt or unreadable
  let text;
  try {
    text = fs.readFileSync(settingsFile(), 'utf8');
  } catch (err) {
    return err.code === 'ENOENT' ? {} : { error: err, recovered: {} };
  }
  try {
    const raw = JSON.parse(text);
    if (!isPlainObject(raw)) throw new Error('not a JSON object');
    return { raw };
  } catch (err) {
    return { error: err, recovered: recoverSecrets(text) };
  }
}

function sanitizeQueryList(list, max, named) {
  // search history (named = false: deduplicated by module + tags, newest first) and saved queries (named = true)
  if (!Array.isArray(list)) return null;
  const out = [];
  const seen = new Set();
  for (const e of list) {
    if (out.length >= max) break;
    if (!isPlainObject(e) || typeof e.module !== 'string' || !RE_ABBR.test(e.module) || typeof e.tags !== 'string') continue;
    if (!e.tags.trim() || e.tags.length > QUERY_TAGS_MAX || !Number.isFinite(e.at) || e.at < 0) continue;
    const item = { module: e.module, tags: e.tags, at: Math.floor(e.at) };
    if (named) {
      if (typeof e.name !== 'string') continue;
      const auto = sanitizeAuto(e.auto);
      out.push({ name: e.name.slice(0, SAVED_NAME_MAX), ...item, ...(auto ? { auto } : {}) });
    } else {
      const key = `${e.module}\n${e.tags}`;
      if (seen.has(key)) continue;
      seen.add(key);
      out.push(item);
    }
  }
  return out;
}

// ---------- saved queries downloaded again on a schedule (only newer posts) ----------
const AUTO_EVERY_HOURS = [6, 12, 24, 72, 168];
const AUTO_CHECK_MS = 5 * 60 * 1000;
const autoKeyOf = (e) => `${e.module}\n${e.tags}`;

function sanitizeAuto(a) {
  if (!isPlainObject(a) || !AUTO_EVERY_HOURS.includes(a.every)) return null;
  const num = (v) => (Number.isFinite(v) && v >= 0 ? Math.floor(v) : 0);
  const lastStatus = ['', 'baseline', 'running', 'done', 'failed', 'cancelled'].includes(a.lastStatus) ? a.lastStatus : '';
  return {
    every: a.every, maxId: num(a.maxId), lastRun: num(a.lastRun), lastCount: num(a.lastCount), lastStatus,
    lastError: typeof a.lastError === 'string' ? a.lastError.slice(0, 200) : '',
    // "only newer posts from now": its baseline id was never passed by a finished run. Schedules saved before 2.0.11 have no such
    // field: a baseline id without any run that finished or downloaded something
    fromNow: typeof a.fromNow === 'boolean' ? a.fromNow : num(a.maxId) > 0 && lastStatus !== 'done' && !(num(a.lastCount) > 0),
  };
}

function mergeAutoRuntime(incoming) {
  // the renderer writes the whole saved list from its copy: main owns the run fields (last run, max id, status)
  const current = new Map((settings.search.saved || []).map((e) => [autoKeyOf(e), e.auto]));
  for (const e of incoming) {
    const cur = current.get(autoKeyOf(e));
    if (e.auto && cur) Object.assign(e.auto, { maxId: cur.maxId, lastRun: cur.lastRun, lastCount: cur.lastCount, lastStatus: cur.lastStatus, lastError: cur.lastError, fromNow: cur.fromNow });
  }
}

function coreMaxId(module) {
  // highest post id of the website right now (lynxx -get_maxid): the start point of "only posts newer than today"
  return new Promise((resolve, reject) => {
    connectionArgs(module).then((secretArgs) => {
      const proc = spawnCore('cli', ['-module', module, '-get_maxid'], { LYNXX_IPC: '1', LYNXX_SECRET_ARGS: JSON.stringify(secretArgs) });
      let maxId = 0;
      let error = '';
      const timer = setTimeout(() => { try { proc.kill(); } catch { /* gone */ } }, 90000);
      readline.createInterface({ input: proc.stdout }).on('line', (line) => {
        if (!line.startsWith(PROGRESS_PREFIX)) return;
        try {
          const p = JSON.parse(line.slice(PROGRESS_PREFIX.length));
          if (p.stateName === 'DONE') { maxId = Number(p.maxId) || 0; error = p.error || ''; }
        } catch { /* ignore */ }
      });
      proc.stdin.end();
      proc.on('error', (e) => { clearTimeout(timer); reject(e); });
      proc.on('close', () => {
        clearTimeout(timer);
        if (maxId > 0) resolve(maxId);
        else reject(new Error(error || 'không lấy được id mới nhất của website'));
      });
    }, reject);
  });
}

// A scheduled run looks for newer posts only with 'id:>N': where the module searches by id (manifest search.idSearch, true when
// absent) and the query does not select another site listing (a listing kind of search.kinds: its own order, no id filter there)
function autoIdSearch(info, tags) {
  if (!info || info.search?.idSearch === false) return false;
  const listing = (Array.isArray(info.search?.kinds) ? info.search.kinds : []).filter((k) => k && k.listing && k.token);
  return !String(tags || '').split(/\s+/).some((t) => listing.some((k) => t.replace(/^%/, '').startsWith(k.token)));
}
// ' id:>N' added to the saved query of a scheduled run ('' when the module cannot filter by id, or uses another metatag separator)
function autoIdFilter(info, tags, maxId) {
  return maxId > 0 && autoIdSearch(info, tags) && (info.syntax?.idSep || ':') === ':' ? ` id:>${maxId}` : '';
}
// A schedule set to "only newer posts from now" whose query cannot filter by id any more (an extension update set search.idSearch
// false: 2.0.10 sent 'id:>N' there and the run failed): run as is it would download the whole result. Held with this message
const AUTO_BASELINE_LOST = 'truy vấn này không còn lọc được theo id post nên “Chỉ post mới từ bây giờ” sẽ tải cả kết quả hiện có. '
  + 'Muốn vậy thì tắt tự tải lại rồi bật lại với “Tải cả kết quả hiện có” (file đã tải được bỏ qua)';
function autoHeld(info, e) {
  const a = e?.auto;
  return a?.fromNow && a.maxId > 0 && !autoIdSearch(info, e.tags) ? AUTO_BASELINE_LOST : '';
}

async function setSavedAuto(key, every, from) {
  const e = (settings.search.saved || []).find((x) => autoKeyOf(x) === key);
  if (!e) throw new Error('không tìm thấy truy vấn đã lưu');
  if (!every) {
    delete e.auto;
  } else {
    if (!AUTO_EVERY_HOURS.includes(every)) throw new Error('chu kỳ không hợp lệ');
    if (!e.auto && from === 'now') { // "only newer posts from now" needs an id filter
      const info = (await sidecar.call('ext.list', {}).catch(() => [])).find((m) => m.abbr === e.module && m.installed);
      if (info && !autoIdSearch(info, e.tags)) throw new Error('truy vấn này không lọc được theo id post: chọn “Tải cả kết quả hiện có”');
    }
    if (e.auto) e.auto.every = every; // keeps where it stopped
    else if (from === 'now') e.auto = sanitizeAuto({ every, maxId: await coreMaxId(e.module), lastRun: Date.now(), lastStatus: 'baseline', fromNow: true });
    else e.auto = sanitizeAuto({ every, maxId: 0, lastRun: 0, fromNow: false }); // first run: everything that matches (files already downloaded are skipped)
  }
  saveSettings();
  setTimeout(() => runDueSaved().catch(() => {}), 2000);
  return { settings, appearance: appearanceState(), secrets: secretsStatus() };
}

let autoRunning = false;
async function runDueSaved() {
  if (autoRunning || jobs.closing || process.env.LYNXX_SELFTEST_DIR) return;
  autoRunning = true;
  try {
    const now = Date.now();
    let modules = null;
    for (const e of settings.search.saved || []) {
      const a = e.auto;
      if (!a?.every || now - a.lastRun < a.every * 3600e3) continue;
      if (jobs.active().some((j) => j.autoKey === autoKeyOf(e))) continue;
      try {
        modules = modules || await sidecar.call('ext.list', {});
        const info = modules.find((m) => m.abbr === e.module && m.installed);
        if (!info) throw new Error(`module ${e.module} chưa được cài`);
        const held = autoHeld(info, e);
        if (held) throw new Error(held);
        // only newer posts: 'id:>N' where the site searches by id with ':' metatags, and files already in the download index are
        // skipped anyway (the only filter where it cannot: search.idSearch false, a site listing)
        const idFilter = autoIdFilter(info, e.tags, a.maxId);
        await jobs.start({ module: e.module, tags: `${e.tags}${idFilter}`, label: `Tự cập nhật: ${e.name}`, path: settings.download.path,
          options: { skipKnown: true }, autoKey: autoKeyOf(e), allowDuplicate: false });
        Object.assign(a, { lastRun: now, lastStatus: 'running', lastError: '' });
      } catch (err) {
        Object.assign(a, { lastRun: now, lastStatus: 'failed', lastError: String(err.message || err).slice(0, 200) });
      }
      saveSettings();
    }
  } finally {
    autoRunning = false;
  }
}

function autoJobFinished(job) {
  const e = (settings.search.saved || []).find((x) => autoKeyOf(x) === job.autoKey);
  if (!e?.auto) return;
  const p = job.progress || {};
  if (job.status === 'done') {
    Object.assign(e.auto, { lastStatus: 'done', lastCount: Number(p.success) || 0, lastError: '', maxId: Math.max(e.auto.maxId, Number(p.maxId) || 0), fromNow: false });
  } else {
    Object.assign(e.auto, { lastStatus: job.status === 'cancelled' ? 'cancelled' : 'failed', lastError: String(job.error || '').slice(0, 200) });
  }
  saveSettings();
}

function sanitizeKeyRateLimits(obj) {
  if (!isPlainObject(obj)) return null;
  const out = {};
  for (const [k, v] of Object.entries(obj)) {
    const n = typeof v === 'string' && v.trim() ? Number(v) : v;
    if (RE_ABBR.test(k) && Number.isInteger(n) && n >= 0 && n <= 100000) out[k] = n;
  }
  return out;
}

// The core reads the key limits from a file, not from argv: download jobs, Preview, the sidecar's tag updates and scheduled runs
// all pace one key together (lynxx.network.key_rate_limit / shared_rate_wait), and a long-running sidecar sees a new value at once
function keyRateLimitsFile() {
  return path.join(userData(), 'ratelimit', 'limits.json');
}

function syncKeyRateLimits() {
  const data = Object.fromEntries(Object.entries(settings.connection.keyRateLimits || {}).filter(([, n]) => n > 0));
  const file = keyRateLimitsFile();
  try {
    let current = null;
    try { current = fs.readFileSync(file, 'utf8'); } catch { /* none yet */ }
    if (current !== null && JSON.stringify(JSON.parse(current)) === JSON.stringify(data)) return;
    if (current === null && !Object.keys(data).length) return;
    writeJsonAtomic(file, data);
  } catch (err) {
    console.warn(`key rate limits: ${err.message}`);
  }
}

function sanitizeTagLimits(obj) {
  if (!isPlainObject(obj)) return null;
  const out = {};
  for (const [k, v] of Object.entries(obj)) {
    const n = typeof v === 'string' && v.trim() ? Number(v) : v;
    if (RE_ABBR.test(k) && Number.isInteger(n) && n >= 0 && n <= 1000) out[k] = n;
  }
  return out;
}

// RFC 9110 token; a leading '-' would look like a CLI option
const RE_HTTP_NAME = /^[!#$%&'*+.^_`|~0-9A-Za-z][!#$%&'*+.^_`|~0-9A-Za-z-]*$/;

function normalizeHttpMap(map, kind, strict) {
  // headers / cookies {name: value}. strict (settings:set): an invalid entry is an error shown to the user, otherwise dropped
  if (map === null || map === undefined) return {};
  if (!isPlainObject(map)) {
    if (strict) throw new Error(`${kind}: phải là JSON object`);
    return {};
  }
  const out = {};
  for (const [k, v] of Object.entries(map)) {
    const name = String(k).trim();
    const value = typeof v === 'string' ? v : (typeof v === 'number' || typeof v === 'boolean') ? String(v) : null;
    let problem = '';
    if (!RE_HTTP_NAME.test(name)) problem = 'tên không hợp lệ';
    else if (value === null) problem = 'giá trị phải là chuỗi';
    else if (/[\r\n\0]/.test(value)) problem = 'giá trị không được chứa ký tự xuống dòng';
    else if (kind === 'Cookie' && value.includes(';')) problem = 'giá trị không được chứa dấu ;';
    if (problem) {
      if (strict) throw new Error(`${kind} "${name.slice(0, 40)}": ${problem}`);
      continue;
    }
    out[name] = value;
  }
  return out;
}

function normalizeSiteHttpMaps(obj, kind, strict) {
  // per-website headers / cookies {abbr: {name: value}}; an abbr with no valid entry is dropped
  if (obj === null || obj === undefined) return {};
  if (!isPlainObject(obj)) {
    if (strict) throw new Error(`${kind} theo site: phải là JSON object {"mã site": {...}}`);
    return {};
  }
  const out = {};
  for (const [k, v] of Object.entries(obj)) {
    const abbr = String(k).trim();
    if (!RE_ABBR.test(abbr)) {
      if (strict) throw new Error(`${kind} theo site: "${abbr.slice(0, 20)}" không phải mã site (2-4 chữ thường hoặc số, ví dụ rn)`);
      continue;
    }
    const map = normalizeHttpMap(v, `${kind} của ${abbr}`, strict);
    if (Object.keys(map).length) out[abbr] = map;
  }
  return out;
}

// mergeHttpMaps: site-verify.js (a website's entries over the shared ones of the same name, letter case aside)

function sanitizeSettings() {
  // -> true when the time in the trash had to be turned off (an unreadable value: see the end)
  const days = isPlainObject(settings.library) ? settings.library.trashDays : settings.library;
  for (const [k, v] of Object.entries(DEFAULT_SETTINGS)) if (isPlainObject(v) && !isPlainObject(settings[k])) settings[k] = structuredClone(v);
  if (!isPlainObject(settings.appearance.background)) settings.appearance.background = structuredClone(DEFAULT_SETTINGS.appearance.background);
  if (!THEMES.includes(settings.appearance.theme)) settings.appearance.theme = 'ember';
  if (!ICONS.some((i) => i.id === settings.appearance.icon)) settings.appearance.icon = ICONS[0].id;
  const s = settings.search;
  s.history = sanitizeQueryList(s.history, HISTORY_MAX, false) || [];
  s.saved = sanitizeQueryList(s.saved, SAVED_MAX, true) || [];
  s.tagLimits = sanitizeTagLimits(s.tagLimits) || {};
  settings.connection.keyRateLimits = sanitizeKeyRateLimits(settings.connection.keyRateLimits) || {};
  settings.connection.headers = normalizeHttpMap(settings.connection.headers, 'Header', false);
  settings.connection.cookies = normalizeHttpMap(settings.connection.cookies, 'Cookie', false);
  settings.connection.siteHeaders = normalizeSiteHttpMaps(settings.connection.siteHeaders, 'Header', false);
  settings.connection.siteCookies = normalizeSiteHttpMaps(settings.connection.siteCookies, 'Cookie', false);
  if (!GEN_MODELS.includes(settings.ai.model)) settings.ai.model = DEFAULT_SETTINGS.ai.model;
  settings.ai.maxUsd = clampUsd(settings.ai.maxUsd, DEFAULT_SETTINGS.ai.maxUsd);
  settings.genderModel.url = sanitizeModelUrl(settings.genderModel.url) ?? DEFAULT_SETTINGS.genderModel.url;
  settings.genderModel.enabled = settings.genderModel.enabled === true;
  if (!validTrashDays(days)) {
    // not a value the app writes (a hand edit): the default could delete files the user meant to keep for longer, so the trash
    // is not emptied until the user picks a time again
    startupNotices.push({ level: 'warn', title: 'Thùng rác tạm không tự xoá',
      text: `Số ngày giữ thùng rác trong file cài đặt không hợp lệ (${String(JSON.stringify(days)).slice(0, 30)}). `
        + 'Thùng rác không tự xoá cho tới khi bạn chọn lại ở Cài đặt → Index các lần tải.' });
    settings.library.trashDays = 0;
    return true;
  }
  return false;
}

const TRASH_DAYS_MAX = 365;
const validTrashDays = (d) => Number.isInteger(d) && d >= 0 && d <= TRASH_DAYS_MAX;

// the image model is used only when it is turned on and has an address; otherwise the gender filter goes by tags
const genderModelOn = () => settings.genderModel.enabled === true && Boolean(settings.genderModel.url);

function sanitizeModelUrl(u) {
  // '' = no model server: the "tags + model" gender mode is off; null = invalid
  if (u === '') return '';
  return typeof u === 'string' && u.length <= 300 && RE_MODEL_URL.test(u.trim()) ? u.trim().replace(/\/+$/, '') : null;
}

function clampUsd(v, fallback) {
  const n = Number(v);
  return Number.isFinite(n) && n > 0 ? Math.min(Math.max(Math.round(n * 100) / 100, 0.1), 20) : fallback;
}

function migrateSettings(from) {
  // v1/v2 -> v3: earlier default backgrounds were too blurred, the icon outline must stay visible
  const bg = settings.appearance.background;
  const oldDefaults = [[48, 0.32, 0.55], [14, 0.9, 0.18]];
  if (from < 3 && oldDefaults.some(([b, o, d]) => bg.blur === b && bg.opacity === o && bg.dim === d)) {
    Object.assign(bg, { blur: 4, opacity: 0.9, dim: 0.18 });
  }
  settings.version = SETTINGS_VERSION;
}

function loadSettings() {
  const { raw, error, recovered } = readSettingsFile();
  // the file version, not the merged default: a v1 file has no "version" key (the original code read 3 from the defaults)
  const fileVersion = raw ? (Number.isInteger(raw.version) && raw.version > 0 ? raw.version : 1) : SETTINGS_VERSION;
  secrets = raw ? sanitizeSecrets(raw.secrets) : (recovered || {});
  if (raw) delete raw.secrets;
  settings = deepMerge(DEFAULT_SETTINGS, raw || {});
  const fixedTrashDays = sanitizeSettings();
  // a corrupt file lost the user's time in the trash: the default could delete what they meant to keep, nothing is emptied
  // until they pick a time again (a new profile, no file at all, keeps the default)
  if (error) settings.library.trashDays = 0;
  migrateSettings(fileVersion);
  syncKeyRateLimits();
  settingsLoadInfo = { fileVersion: raw ? fileVersion : null, corrupt: Boolean(error), migrated: Boolean(raw && fileVersion < SETTINGS_VERSION) };
  if (error) {
    // a corrupt file must not cost the user their API keys: keep a copy, salvage the secrets block, tell the user
    const backup = backupCorruptSettings();
    const names = Object.keys(secrets);
    const text = `File cài đặt bị hỏng (${String(error.message || error).slice(0, 200)}). `
      + (backup ? `Đã lưu bản sao: ${backup}. ` : 'Không sao lưu được file nên app sẽ không ghi đè lên nó. ')
      + 'Cài đặt được đặt lại mặc định; '
      + (names.length ? `đã khôi phục API key: ${names.join(', ')}.` : 'không khôi phục được API key nào.')
      + ' Thùng rác không tự xoá cho tới khi bạn chọn lại số ngày ở Cài đặt → Index các lần tải.';
    startupNotices.push({ level: 'warn', title: 'File cài đặt bị hỏng', text });
    Object.assign(settingsLoadInfo, { backup, recovered: names });
    console.warn(`settings: ${text}`);
    if (!backup) settingsWriteBlocked = true;
    else saveSettings();
  } else if (raw && fileVersion < SETTINGS_VERSION) {
    saveSettings();
    startupNotices.push({ level: 'info', text: `settings: đã chuyển từ phiên bản ${fileVersion} lên ${SETTINGS_VERSION}` });
  } else if (fixedTrashDays) saveSettings();
}

function saveSettings() {
  if (settingsWriteBlocked) return;
  writeJsonAtomic(settingsFile(), { ...settings, secrets });
}

function flushStartupNotices() {
  for (const n of startupNotices.splice(0)) {
    send('log', { source: 'settings', text: n.text });
    if (n.level !== 'warn') continue;
    send('sidecar:notify', { method: 'app.notice', params: { level: n.level, title: n.title, message: n.text } });
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

function coreCommand(kind, args = []) {
  // kind: 'server' (JSON-RPC sidecar) | 'cli' (download job). Packaged app: the bundled core (the Python path setting is
  // only a fallback when it is missing: the app's sources are not on disk); development: python -m lynxx[.server]
  const core = bundledCore();
  // 'aigen' (extension generator worker) and 'extgen' (draft tools: check / pack) are commands of the same core
  if (core) return { cmd: core, args: [kind, ...args], cwd: userData(), frozen: true };
  const mod = { server: 'lynxx.server', cli: 'lynxx', aigen: 'lynxx.aigen.worker', extgen: 'lynxx.extgen', index: 'lynxx.dlindex_tools',
    tags: 'lynxx.tags_updater' }[kind];
  if (!mod) throw new Error(`unknown core command ${kind}`);
  return { cmd: resolvePython(), args: ['-m', mod, ...args], cwd: REPO_ROOT, frozen: false };
}

function pythonEnv(extra = {}, frozen = false) {
  const sep = process.platform === 'win32' ? ';' : ':';
  const env = {
    ...process.env,
    PYTHONUNBUFFERED: '1',
    PYTHONIOENCODING: 'utf-8',
    LYNXX_DATA_DIR: userData(),
    LYNXX_CATALOG_AUTOLOAD: '0', // only builtin and installed extensions: catalog ones must be installed first
    PYTHONPATH: [REPO_ROOT, process.env.PYTHONPATH].filter(Boolean).join(sep),
    ...extra,
  };
  if (frozen) delete env.PYTHONPATH; // the frozen core carries its own modules
  return env;
}

function spawnCore(kind, args, extraEnv = {}) {
  fs.mkdirSync(userData(), { recursive: true });
  const c = coreCommand(kind, args);
  const proc = spawn(c.cmd, c.args, { cwd: c.cwd, env: pythonEnv(extraEnv, c.frozen), stdio: ['pipe', 'pipe', 'pipe'], windowsHide: true });
  proc.coreCmd = c.cmd;
  return proc;
}

// ---------- secrets (API keys) ----------

let encryptionAvailable = false;

async function initEncryption() {
  try { encryptionAvailable = await safeStorage.isAsyncEncryptionAvailable(); } catch { encryptionAvailable = false; }
  if (encryptionAvailable && process.platform === 'linux') {
    try { if (safeStorage.getSelectedStorageBackend() === 'basic_text') encryptionAvailable = false; } catch { /* older api */ }
  }
}

async function setSecret(module, value) {
  // an empty value never deletes a key (an untouched empty field saved by the UI): clearSecret does that
  if (!RE_ABBR.test(String(module)) || !value) return;
  if (encryptionAvailable) secrets[module] = { enc: (await safeStorage.encryptStringAsync(value)).toString('base64') };
  else secrets[module] = { plain: value }; // no OS keyring available: stored as plain text (reported to UI)
  saveSettings();
}

function clearSecret(module) {
  if (!RE_ABBR.test(String(module)) || !Object.hasOwn(secrets, module)) return;
  delete secrets[module];
  saveSettings();
}

async function getSecret(module) {
  if (!Object.hasOwn(secrets, module)) return '';
  const s = secrets[module];
  if (!s) return '';
  if (!s.enc) return s.plain || '';
  try {
    const { result, shouldReEncrypt } = await safeStorage.decryptStringAsync(Buffer.from(s.enc, 'base64'));
    if (shouldReEncrypt) await setSecret(module, result);
    return result;
  } catch {
    return '';
  }
}

// gender model server token: never sent to the renderer; given to the core as a secret argument (sidecar rpc params, job env)
function modelTokenStatus() {
  const s = secrets[MODEL_SECRET];
  return { set: Boolean(s), encrypted: Boolean(s?.enc), encryption: encryptionAvailable };
}

async function setModelToken(value) {
  value = typeof value === 'string' ? value.trim() : '';
  if (!value) return modelTokenStatus(); // an empty field never deletes the token: clearModelToken does that
  if (!/^[\x21-\x7e]{8,256}$/.test(value)) throw new Error('token: 8–256 ký tự ASCII, không có dấu cách');
  if (encryptionAvailable) secrets[MODEL_SECRET] = { enc: (await safeStorage.encryptStringAsync(value)).toString('base64') };
  else secrets[MODEL_SECRET] = { plain: value }; // no OS keyring: plain text in the 0600 settings file (reported to the UI)
  saveSettings();
  return modelTokenStatus();
}

function clearModelToken() {
  if (Object.hasOwn(secrets, MODEL_SECRET)) {
    delete secrets[MODEL_SECRET];
    saveSettings();
  }
  return modelTokenStatus();
}

async function genderSecretArgs(g) {
  if (g?.mode !== 'model' || !genderModelOn()) return [];
  const token = await getSecret(MODEL_SECRET);
  return token ? ['-gender_model_token', token] : [];
}

// ---------- extension generator (lynxx.aigen worker) ----------
// A session turns a website link into a draft folder (manifest.json, downloader.py, recorded test fixtures, report.json) under
// <userData>/extgen/drafts/<session>/. Nothing is installed by a session: "install" is a separate user action that re-checks
// the draft, packs it and asks for a native confirmation. The Claude API key never reaches the renderer: main decrypts it and
// writes it to the worker's stdin only.

function aiKeyStatus() {
  return { set: Boolean(secrets[AI_SECRET]?.enc), encryption: encryptionAvailable };
}

async function setAiKey(value) {
  value = typeof value === 'string' ? value.trim() : '';
  if (!value) return aiKeyStatus(); // an empty field never deletes the key: clearAiKey does that
  if (!/^[\x21-\x7e]{20,400}$/.test(value)) throw new Error('khoá Claude API không hợp lệ');
  if (!encryptionAvailable) {
    throw new Error('Hệ điều hành không có kho khoá mã hoá (Linux: gnome-keyring / KWallet): Lynxx không lưu khoá Claude API dạng chữ thường.');
  }
  secrets[AI_SECRET] = { enc: (await safeStorage.encryptStringAsync(value)).toString('base64') };
  saveSettings();
  return aiKeyStatus();
}

function clearAiKey() {
  if (Object.hasOwn(secrets, AI_SECRET)) {
    delete secrets[AI_SECRET];
    saveSettings();
  }
  return aiKeyStatus();
}

const draftsRoot = () => path.join(userData(), 'extgen', 'drafts');

function genSessionDir(session) {
  if (!RE_GEN_SESSION.test(String(session))) throw new Error('invalid draft session');
  return path.join(draftsRoot(), String(session));
}

async function readGenReport(session) {
  const dir = genSessionDir(session);
  let rep;
  try { rep = JSON.parse(await fsp.readFile(path.join(dir, 'report.json'), 'utf8')); } catch { rep = { status: 'unknown' }; }
  if (!isPlainObject(rep)) rep = { status: 'unknown' };
  // the draft folder comes from the session and the extension id, never from a path written in the file
  const extId = RE_EXT_ID.test(String(rep.extId || '')) ? rep.extId : '';
  const draft = extId ? path.join(dir, extId) : '';
  return { ...rep, session, extId, draftDir: draft && fs.existsSync(path.join(draft, 'manifest.json')) ? draft : '' };
}

async function listGenDrafts() {
  let names = [];
  try { names = (await fsp.readdir(draftsRoot())).filter((n) => RE_GEN_SESSION.test(n)).sort().reverse().slice(0, 100); } catch { /* none yet */ }
  const out = [];
  for (const n of names) {
    const { log, ...rep } = await readGenReport(n);
    out.push(rep);
  }
  return out;
}

function runExtgen(args, timeoutMs = 180000) {
  // python -m lynxx.extgen <args> (JSON on stdout): check / pack of a draft, no network
  return new Promise((resolve, reject) => {
    const proc = spawnCore('extgen', args);
    let out = '';
    let err = '';
    const timer = setTimeout(() => { try { proc.kill(); } catch { /* gone */ } }, timeoutMs);
    proc.stdout.on('data', (d) => { out += d; });
    proc.stderr.on('data', (d) => { err = (err + d).slice(-4000); });
    proc.on('error', (e) => { clearTimeout(timer); reject(e); });
    proc.on('close', (code) => {
      clearTimeout(timer);
      let data = null;
      try { data = JSON.parse(out); } catch { /* not json */ }
      if (!data) return reject(new Error(`extgen ${args[0]}: exit ${code} ${redact(err).split('\n').slice(-3).join(' ')}`));
      resolve({ code, data });
    });
    proc.stdin.end();
  });
}

class Generator {
  constructor() {
    this.proc = null;
    this.session = '';
    this.events = [];
    this.done = null;
    this.stderr = [];
    this.killTimer = null;
    this.confirm = confirmMain; // native confirmation (replaced by the self-test)
  }

  status() {
    return { running: Boolean(this.proc), session: this.session, events: this.events.slice(-300), key: aiKeyStatus(), models: GEN_MODELS,
      defaults: { model: settings.ai.model, maxUsd: settings.ai.maxUsd } };
  }

  async start(opts) {
    if (this.proc) throw new Error('một phiên tạo extension đang chạy');
    if (!isPlainObject(opts)) throw new Error('invalid options');
    let url = String(opts.url || '').trim();
    let repair = '';
    if (opts.repair !== undefined) { // fix a draft that did not pass: same site, ai mode, the old session stays untouched
      const old = await readGenReport(String(opts.repair));
      if (!old.draftDir || old.status === 'ready') throw new Error('chỉ sửa được bản nháp chưa đạt còn thư mục bản nháp');
      repair = old.session;
      url = String(old.url || old.site || '');
    }
    if (!url || url.length > 2000 || /\s/.test(url)) throw new Error('link website không hợp lệ');
    const mode = repair ? 'ai' : opts.mode === 'template' ? 'template' : 'ai';
    const media = ['image', 'video', 'audio'].filter((m) => Array.isArray(opts.media) && opts.media.includes(m));
    const cfg = {
      cmd: 'start', url, mode, media: media.length ? media : ['image', 'video'],
      query: typeof opts.query === 'string' ? opts.query.trim().slice(0, 200) : '',
      model: GEN_MODELS.includes(opts.model) ? opts.model : settings.ai.model,
      maxUsd: clampUsd(opts.maxUsd, settings.ai.maxUsd),
      ...(repair ? { repair } : {}),
    };
    let apiKey = '';
    if (mode === 'ai') {
      apiKey = await getSecret(AI_SECRET);
      if (!apiKey) throw new Error('Chưa có khoá Claude API: thêm trong Cài đặt → AI tạo extension, hoặc dùng chế độ không AI.');
    }
    this.events = [];
    this.done = null;
    this.stderr = [];
    this.session = '';
    const proc = spawnCore('aigen', []);
    this.proc = proc;
    readline.createInterface({ input: proc.stdout }).on('line', (line) => this._line(line));
    readline.createInterface({ input: proc.stderr }).on('line', (line) => {
      this.stderr.push(redact(line).slice(0, 500));
      if (this.stderr.length > 40) this.stderr.shift();
    });
    proc.on('error', (e) => this._exit(null, e));
    proc.on('exit', (code) => this._exit(code));
    proc.stdin.on('error', () => { /* worker gone */ });
    proc.stdin.write(`${JSON.stringify({ ...cfg, apiKey })}\n`); // the key goes through stdin only: not argv, env or files
    apiKey = '';
    return { started: true, mode, model: mode === 'ai' ? cfg.model : '' };
  }

  cancel() {
    if (!this.proc) return false;
    try { this.proc.stdin.write('{"cmd":"cancel"}\n'); } catch { /* gone */ }
    clearTimeout(this.killTimer);
    this.killTimer = setTimeout(() => this.kill(), 15000); // a request to the model can take a while to stop
    return true;
  }

  kill() {
    try { this.proc?.kill(); } catch { /* gone */ }
  }

  _line(line) {
    if (!line.startsWith('@@GEN ')) return;
    let ev;
    try { ev = JSON.parse(line.slice(6)); } catch { return; }
    if (!isPlainObject(ev) || typeof ev.type !== 'string') return;
    if (ev.type === 'start' && RE_GEN_SESSION.test(String(ev.session))) this.session = ev.session;
    if (ev.type === 'done') {
      this.done = ev;
      if (isPlainObject(ev.report)) delete ev.report.log; // the full log stays in report.json
    }
    this._event(ev);
  }

  _event(ev) {
    this.events.push(ev);
    if (this.events.length > 500) this.events.shift();
    send('gen:event', ev);
  }

  _exit(code, error) {
    if (!this.proc) return;
    this.proc = null;
    clearTimeout(this.killTimer);
    if (!this.done) {
      const why = error ? error.message : `mã thoát ${code}`;
      this._event({ type: 'done', status: 'error', session: this.session, summary: `Tiến trình tạo extension dừng bất thường (${why}). ${this.stderr.slice(-3).join(' ')}`.trim() });
    }
    send('gen:event', { type: 'exit', session: this.session });
  }

  async exportZip(session) {
    const rep = await readGenReport(session);
    if (!rep.draftDir) throw new Error('bản nháp không có manifest.json');
    const r = await dialog.showSaveDialog(mainWindow, {
      title: 'Xuất extension ra file .zip',
      defaultPath: path.join(app.getPath('downloads'), `${rep.extId}-${rep.version || '1.0.0'}.zip`),
      filters: [{ name: 'Lynxx extension', extensions: ['zip'] }],
    });
    if (r.canceled || !r.filePath) return null;
    const { code, data } = await runExtgen(['pack', rep.draftDir, '--out', r.filePath]);
    if (code !== 0 || !data.sha256) throw new Error(data.error || 'không đóng gói được bản nháp');
    return { path: data.path, sha256: data.sha256 };
  }

  async install(session) {
    // explicit user action only: offline re-check (the files may have been edited since), pack, native confirmation, install
    const rep = await readGenReport(session);
    if (this.proc && this.session === session) throw new Error('phiên này vẫn đang chạy');
    if (rep.status !== 'ready' || !rep.draftDir) throw new Error('chỉ cài được bản nháp đã chạy thử đạt (hoặc xuất zip rồi tự cài)');
    const check = await runExtgen(['check', rep.draftDir]);
    if (check.code !== 0) {
      const failedChecks = (check.data.checks || []).filter((c) => !c.ok && c.severity === 'error');
      const denied = failedChecks.find((c) => String(c.id).startsWith('domain:')); // domain denylist: its own clear message
      if (denied) throw new Error(String(denied.detail || 'site nằm trong danh sách chặn của Lynxx'));
      const failed = failedChecks.map((c) => c.id);
      throw new Error(`bản nháp không còn qua kiểm tra: ${failed.join(', ') || check.data.error || 'lỗi'}`);
    }
    const zip = path.join(genSessionDir(session), `${rep.extId}.zip`);
    const { code, data } = await runExtgen(['pack', rep.draftDir, '--out', zip]);
    if (code !== 0 || !RE_SHA256.test(String(data.sha256 || ''))) throw new Error(data.error || 'không đóng gói được bản nháp');
    const origin = rep.mode === 'ai' ? `AI (${rep.model || 'Claude'})` : 'template có sẵn của Lynxx';
    const ok = await this.confirm(`Cài extension "${rep.name || rep.extId}" (${rep.abbr || '?'}) vào Lynxx?`,
      `Site: ${rep.site || rep.url}\nTạo bởi: ${origin}. Chạy thử và kiểm tra lại: đạt.\nsha256: ${data.sha256}\n\n`
      + 'Extension là code Python chạy trên máy bạn với quyền của bạn. Hãy xem downloader.py trước khi cài (nút "Mở thư mục").',
      ['Huỷ', 'Cài']);
    if (!ok) throw new Error('cancelled');
    return installExtension({ archivePath: zip, sha256: data.sha256 });
  }

  async remove(session) {
    const dir = genSessionDir(session);
    if (this.proc && this.session === session) throw new Error('phiên này vẫn đang chạy');
    const rep = await readGenReport(session);
    const ok = await this.confirm(`Xoá bản nháp "${rep.name || rep.extId || session}"?`,
      'Thư mục bản nháp (manifest.json, downloader.py, dữ liệu chạy thử) sẽ bị xoá. Extension đã cài (nếu có) không bị ảnh hưởng.', ['Huỷ', 'Xoá']);
    if (!ok) return false;
    await fsp.rm(dir, { recursive: true, force: true });
    return true;
  }
}

const generator = new Generator();

// ---------- sidecar (JSON-RPC) ----------

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
    const py = coreCommand('server').cmd;
    this.info = null;
    this.status = { ok: false, error: 'starting' };
    this.ready = new Promise((resolve, reject) => {
      this._resolveReady = resolve;
      this._rejectReady = reject;
    });
    this.ready.catch(() => {}); // callers get the rejection through call(); avoid unhandled rejection warnings
    let proc;
    try {
      proc = spawnCore('server', []);
    } catch (err) {
      this._setStatus({ ok: false, error: `Không chạy được Python (${py}): ${err.message}` });
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
    proc.on('error', (err) => onEnd(`Không chạy được Python (${py}): ${err.message}`));
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
      send('log', { source: msg.params.scope || 'core', text: msg.params.text });
    } else if (msg.method) {
      if (msg.method === 'tags.updateDone') onTagsUpdateDone(msg.params || {});
      send('sidecar:notify', { method: msg.method, params: msg.params });
    }
  }

  async call(method, params = {}, timeoutMs = 120000) {
    const deadline = Date.now() + timeoutMs;
    // the timeout also covers waiting for the sidecar to start (the original code could wait forever)
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

// ---------- CLI arguments ----------

function toCliDate(iso) {
  // '2024-01-31' -> '31-01-2024'
  const m = /^(\d{4})-(\d{2})-(\d{2})$/.exec(iso || '');
  return m ? `${m[3]}-${m[2]}-${m[1]}` : '';
}

function siteHttpMap(module, kind) {
  // the headers or cookies of one website (siteHeaders / siteCookies), {} for none
  const all = kind === 'Header' ? settings.connection.siteHeaders : settings.connection.siteCookies;
  return RE_ABBR.test(module || '') && isPlainObject(all) ? normalizeHttpMap(all[module], kind, false) : {};
}

function siteHttpArgs(module) {
  // '-header' / '-cookie' arguments of one website only (library import: the shared ones are in its common arguments)
  const a = [];
  for (const [opt, kind] of [['-header', 'Header'], ['-cookie', 'Cookie']]) {
    for (const [name, value] of Object.entries(siteHttpMap(module, kind))) a.push(opt, `${name}=${value}`);
  }
  return a;
}

async function connectionArgs(module) {
  const c = settings.connection;
  const a = [];
  if (c.proxy) a.push('-proxy', c.proxy);
  if (c.ignoreProxy) a.push('-noproxy');
  if (c.proxyNoDownload) a.push('-proxynodown');
  if (c.timeout > 0) a.push('-timeout', String(c.timeout));
  if (c.retries > 0) a.push('-retries', String(c.retries));
  // one '-header name=value' per entry, not '-headers <json>': the core's JSON option strips every backslash (values with
  // '"' or '\' broke); name=value is split at the first '=' and token names never contain one. The website of this run gets
  // its own entries too (siteHeaders / siteCookies), no other website does
  for (const [opt, kind, map] of [['-header', 'Header', c.headers], ['-cookie', 'Cookie', c.cookies]]) {
    const merged = mergeHttpMaps(normalizeHttpMap(map, kind, false), siteHttpMap(module, kind));
    for (const [name, value] of Object.entries(merged)) a.push(opt, `${name}=${value}`);
  }
  const key = await getSecret(module);
  if (key) a.push('-api_key', key);
  if (settings.advanced.verbose) a.push('-verbose');
  return a;
}

function genderArgs(g) {
  // content filter (character gender); the safety blacklist is a separate option, see safetyArgs
  const labels = ['only_female', 'only_male', 'both', 'none', 'unknown'];
  const sel = [...new Set((g && Array.isArray(g.select) ? g.select : []).filter((x) => labels.includes(x)))];
  if (!sel.length) return [];
  const exclude = g.op === 'exclude';
  if (exclude && sel.length === labels.length) return []; // dropping every label would leave nothing: not a filter
  const a = exclude ? ['-gender_exclude', sel.join(',')] : ['-gender', sel.join(',')];
  if (['low', 'medium', 'high'].includes(g.conf)) a.push('-gender_conf', g.conf);
  if (g.mode === 'model' && genderModelOn()) {
    a.push('-gender_mode', 'model', '-gender_model_url', settings.genderModel.url);
    if (g.verifyMedia === true) a.push('-gender_verify_media'); // checks downloaded files: only with the model
  }
  if (g.strict) a.push('-gender_strict');
  return a;
}

function safetyArgs(q) {
  // '-safety_blacklist': only when the query asks for it (the check box of the filters panel); never implied by the gender filter
  return q && q.safetyBlacklist === true ? ['-safety_blacklist'] : [];
}

function searchArgs(q) {
  const a = [];
  if (q.dateMin) a.push('-mindate', toCliDate(q.dateMin));
  if (q.dateMax) a.push('-maxdate', toCliDate(q.dateMax));
  return a;
}

function tagLimitArgs(module) {
  // account tag limit (danbooru: 2 free, 6 Gold, 12 Platinum); 0 / missing = module default
  const n = settings.search.tagLimits?.[module];
  return Number.isInteger(n) && n > 0 ? ['-tags_limit', String(n)] : [];
}

function asQuery(q) {
  if (!isPlainObject(q) || typeof q.module !== 'string' || !RE_ABBR.test(q.module)) throw new Error('invalid query');
  return q;
}

function sanitizeFolderName(s) {
  return String(s).replace(/[<>:"/\\|?*\x00-\x1f]+/g, '_').replace(/\s+/g, ' ').trim().slice(0, 60) || 'query';
}

function resolveUserPath(p) {
  // '~/x' and relative paths are resolved against the home folder (lynxx.server resolve_user_path does the same)
  const s = String(p || '').trim();
  if (s === '~') return os.homedir();
  if (s.startsWith('~/') || s.startsWith('~\\')) return path.join(os.homedir(), s.slice(2));
  return path.isAbsolute(s) ? path.normalize(s) : path.join(os.homedir(), s);
}

function downloadArgs(q, capabilities = null) {
  // capabilities: the module's (ext.list); '-mp4' / '-webm' only with video_format_choice, whatever settings.download says (a
  // scheduled run or a multi-site job takes the saved format; the core would warn and wait 2 s on a module without the choice)
  const d = { ...settings.download, ...(q.options || {}) };
  if (!(Array.isArray(capabilities) && capabilities.includes('video_format_choice'))) d.videoFormat = '';
  let dest = resolveUserPath(q.path || d.path);
  // the renderer always sends the path field: the original '!q.path' made this option a no-op
  if (d.subfolderPerQuery) dest = path.join(dest, `${q.module}_${sanitizeFolderName(q.tags)}`);
  const a = ['-path', dest];
  if (!d.images) a.push('-skip_img');
  if (!d.videos) a.push('-skip_vid');
  if (d.audio === false) a.push('-skip_aud'); // only modules with audio media produce audio files
  if (d.videoFormat === 'mp4') a.push('-mp4');
  if (d.videoFormat === 'webm') a.push('-webm');
  if (d.lowRes) a.push('-lowres');
  const vres = Number(d.maxVideoHeight);
  if (Number.isInteger(vres) && vres > 0 && vres <= VIDEO_HEIGHT_MAX) a.push('-max_vres', String(vres));
  if (d.threads) a.push('-threads', String(Math.max(1, Math.min(8, d.threads))));
  if (d.prefix) a.push('-prefix');
  if (d.appendInfo) a.push('-append_info');
  if (d.reverse) a.push('-reverse');
  if (d.limit > 0) a.push('-dlimit', String(d.limit));
  if (d.parchi) a.push('-include_parchi');
  if (d.preserveDate) a.push('-preserve_date');
  if (d.skipKnown === true) a.push('-skip_known');
  if (d.dumpTags) a.push('-dump_tags');
  if (d.dumpSources) a.push('-dump_sources');
  if (d.dumpComments) a.push('-dump_comments');
  if (d.dumpHashes) a.push('-dump_hashes');
  if (d.infoMode === 'item') a.push('-dump_per_item');
  if (d.infoMode === 'merge') a.push('-merge_lists');
  return { args: a, dest };
}

function pickArgs(pick) {
  // "Tải đã chọn" on a site that cannot search by id: the job reads the Preview's page again (same query, page index and page
  // size) and downloads only the picked posts on it (core -pick_page / -pick_size / -pick_ids). [] without a pick. A page may hold
  // more posts than its size (a query of several sub-queries merges page N of each): only the core's own bound applies
  if (pick === undefined || pick === null) return [];
  if (!isPlainObject(pick)) throw new Error('invalid pick');
  const { page, size, ids } = pick;
  if (!Number.isSafeInteger(page) || page < 0) throw new Error('invalid pick page');
  if (!Number.isSafeInteger(size) || size < 1) throw new Error('invalid pick size');
  const list = Array.isArray(ids) ? [...new Set(ids)] : [];
  if (!list.length || list.length > PICK_IDS_MAX || !list.every((x) => typeof x === 'string' && RE_PICK_ID.test(x))) {
    throw new Error('invalid pick ids');
  }
  // a value that starts with '-' would be read as an option: such an id never leads the list
  list.sort((x, y) => Number(x.startsWith('-')) - Number(y.startsWith('-')));
  if (list[0].startsWith('-')) throw new Error('invalid pick ids');
  return ['-pick_page', String(page), '-pick_size', String(size), '-pick_ids', list.join(',')];
}

// ---------- download jobs (separate CLI processes) ----------

function notifyJob(job) {
  // a job ended while Lynxx is not the focused window: system notification (Settings: download.notify)
  if (!settings.download.notify || job.label === 'selftest' || !['done', 'failed'].includes(job.status)) return;
  if (mainWindow && !mainWindow.isDestroyed() && mainWindow.isFocused()) return;
  try {
    if (!Notification.isSupported()) return;
    const p = job.progress || {};
    const body = job.status === 'done'
      ? `${p.success || 0} file ${job.kind === 'import' ? 'đã nhập vào index' : 'đã tải'}${p.failed ? `, ${p.failed} lỗi` : ''}`
      : `Lỗi: ${String(job.error || '').slice(0, 120)}`;
    const n = new Notification({ title: `${job.status === 'done' ? 'Xong' : 'Lỗi'}: ${job.module} · ${job.label || job.tags}`.slice(0, 120), body });
    n.on('click', () => { if (mainWindow && !mainWindow.isDestroyed()) { mainWindow.show(); mainWindow.focus(); send('nav:page', 'downloads'); } });
    n.show();
  } catch { /* notifications are optional */ }
}

class Jobs {
  constructor() {
    this.jobs = new Map();
    this.seq = 0;
  }

  public(job) {
    const { proc, logTail, args, secretArgs, moduleSecretArgs, testCommand, query, importQuery, coreKind, ...rest } = job;
    return { ...rest, logTail: logTail.slice(-40) };
  }

  emit(job) {
    send('jobs:update', this.public(job));
  }

  async _build(q) {
    if (this.closing) throw new Error('app is closing');
    if (!q || !q.module || !String(q.tags || '').trim()) throw new Error('module and tags are required');
    asQuery(q);
    // kept for 'run again' (no secrets: they are added per run); a job of a multi-site group runs again on its own
    const query = structuredClone({ ...q, allowDuplicate: undefined, group: undefined });
    const id = `job-${Date.now()}-${++this.seq}`;
    // the module's capabilities are read only when a video format would be sent (cached ext.list; unknown: no format)
    const format = { ...settings.download, ...(q.options || {}) }.videoFormat;
    const capabilities = format === 'mp4' || format === 'webm' ? await moduleCapabilities(q.module) : null;
    const { args, dest } = downloadArgs(q, capabilities);
    const pick = pickArgs(q.pick);
    const job = {
      id, module: q.module, tags: String(q.tags).trim(), dest, status: 'queued', progress: {}, createdAt: Date.now(),
      startedAt: 0, endedAt: 0, exitCode: null, logTail: [], label: q.label || '',
      secretArgs: [...(await connectionArgs(q.module)), ...(await genderSecretArgs(q.gender))],
      // '--': tags like '-webm' or '-v' are never parsed as options
      args: ['-module', q.module, ...searchArgs(q), ...tagLimitArgs(q.module), ...genderArgs(q.gender), ...safetyArgs(q), ...args, ...pick,
        '--', ...String(q.tags).trim().split(/\s+/)],
      query,
      autoKey: typeof q.autoKey === 'string' ? q.autoKey : '',
      group: isPlainObject(q.group) ? q.group : null, // multi-site download (startMulti only)
    };
    return job;
  }

  _duplicate(job) {
    // the same download twice (double click, a query run again by mistake): the renderer asks, then retries with allowDuplicate
    const same = this.active().find((j) => j.args.join('\0') === job.args.join('\0'));
    return same ? new Error(`DUPLICATE: một job giống hệt đang ${same.status === 'queued' ? 'chờ' : 'chạy'}`) : null;
  }

  async start(q) {
    const job = await this._build(q);
    const dup = q.allowDuplicate === true ? null : this._duplicate(job);
    if (dup) throw dup;
    return this._add(job);
  }

  // one query on several sites: one job per module, each waits for the previous site's job to end (see _waiting). Forced options:
  // '-skip_known' (a post another site of the group already brought, same md5 in the index, is skipped) and module prefixed file
  // names (post 123 of two sites never share a file name). Tags are sent as typed to every site: their syntax may differ
  async startMulti(q, modules) {
    asQuery(q);
    const list = [...new Set([q.module, ...(Array.isArray(modules) ? modules : [])])];
    if (list.length < 2 || list.length > MULTI_SITE_MAX || !list.every((m) => typeof m === 'string' && RE_ABBR.test(m))) {
      throw new Error(`cần 2–${MULTI_SITE_MAX} module khác nhau`);
    }
    const group = { id: `group-${Date.now()}-${++this.seq}`, size: list.length, modules: list };
    const options = { ...(isPlainObject(q.options) ? q.options : {}), skipKnown: true, prefix: true };
    const built = [];
    // picked posts belong to one site's Preview page: never sent to a group
    for (const [index, module] of list.entries()) built.push(await this._build({ ...q, module, options, group: { ...group, index }, pick: undefined }));
    const dup = q.allowDuplicate === true ? null : built.map((j) => this._duplicate(j)).find(Boolean);
    if (dup) throw dup;
    return built.map((job) => this._add(job));
  }

  // library import into the download index (lynxx/dlindex_tools.py 'import'): a core job like a download (progress lines,
  // 'cancel'). The folder must come from the native dialog (index:pickFolder): the renderer cannot name an arbitrary path
  async startImport(opts) {
    if (this.closing) throw new Error('app is closing');
    if (!isPlainObject(opts)) throw new Error('invalid import options');
    const folder = String(opts.folder || '');
    if (!approvedImportFolders.has(folder)) throw new Error('choose the folder with the folder dialog first');
    if (!fs.existsSync(folder) || !fs.statSync(folder).isDirectory()) throw new Error('thư mục không còn');
    const module = String(opts.module || '');
    if (module && !RE_ABBR.test(module)) throw new Error('invalid module');
    const importQuery = { folder, module, recursive: opts.recursive !== false, verifyMd5: opts.verifyMd5 === true, dryRun: opts.dryRun === true };
    const moduleSecretArgs = {}; // the files may belong to any module: each one gets its own key (the core reads them, then drops them)
    for (const abbr of Object.keys(secrets).filter((k) => RE_ABBR.test(k))) {
      const key = await getSecret(abbr);
      if (key) moduleSecretArgs[abbr] = ['-api_key', key];
    }
    for (const abbr of new Set([...Object.keys(settings.connection.siteHeaders || {}), ...Object.keys(settings.connection.siteCookies || {})])) {
      const own = siteHttpArgs(abbr); // a website's own headers / cookies, after the shared ones: the core lets the later win
      if (own.length) moduleSecretArgs[abbr] = [...(moduleSecretArgs[abbr] || []), ...own];
    }
    const job = {
      id: `job-${Date.now()}-${++this.seq}`, kind: 'import', coreKind: 'index', module: module || '*', tags: folder, dest: folder,
      status: 'queued', progress: {}, createdAt: Date.now(), startedAt: 0, endedAt: 0, exitCode: null, logTail: [],
      label: importQuery.dryRun ? 'Kiểm tra thư mục (không gửi request)' : 'Nhập thư viện vào index',
      secretArgs: await connectionArgs(''), moduleSecretArgs, importQuery, autoKey: '', group: null,
      args: ['import', folder, ...(module ? ['--module', module] : []), ...(importQuery.recursive ? [] : ['--no-recursive']),
        ...(importQuery.verifyMd5 ? ['--verify-md5'] : []), ...(importQuery.dryRun ? ['--dry-run'] : [])],
    };
    const dup = opts.allowDuplicate === true ? null : this._duplicate(job);
    if (dup) throw dup;
    return this._add(job);
  }

  rerun(id, allowDuplicate = false) {
    const job = this.jobs.get(id);
    if (job?.importQuery) return this.startImport({ ...job.importQuery, allowDuplicate });
    if (!job?.query) throw new Error('job không còn thông tin để chạy lại');
    return this.start({ ...job.query, allowDuplicate });
  }

  _waiting(job) {
    // multi-site group: a site's job starts once every earlier site's job ended, so their files are in the index (-skip_known)
    if (!job.group) return false;
    for (const j of this.jobs.values()) {
      if (j.group?.id === job.group.id && j.group.index < job.group.index && ['queued', 'running', 'cancelling'].includes(j.status)) return true;
    }
    return false;
  }

  _add(job) {
    this.jobs.set(job.id, job);
    this.emit(job);
    this.pump();
    return this.public(job);
  }

  running() {
    return [...this.jobs.values()].filter((j) => j.status === 'running').length;
  }

  active() {
    return [...this.jobs.values()].filter((j) => ['queued', 'running', 'cancelling'].includes(j.status));
  }

  pump() {
    if (this.closing) return;
    const max = Math.max(1, settings.download.maxConcurrentJobs || 1);
    for (const job of this.jobs.values()) {
      if (this.running() >= max) break;
      if (job.status === 'queued' && !this._waiting(job)) this._spawn(job);
    }
  }

  _fail(job, error) {
    job.status = 'failed';
    job.error = error;
    job.endedAt = Date.now();
    delete job.proc;
    this.emit(job);
  }

  _spawn(job) {
    job.status = 'running';
    job.startedAt = Date.now();
    let proc;
    try {
      fs.mkdirSync(job.dest, { recursive: true });
      const env = { LYNXX_IPC: '1', LYNXX_SECRET_ARGS: JSON.stringify(job.secretArgs || []) };
      if (job.moduleSecretArgs) env.LYNXX_SECRET_MODULE_ARGS = JSON.stringify(job.moduleSecretArgs); // library import
      if (job.testCommand) { // self-test only: a stand-in process (never set from the renderer)
        proc = spawn(job.testCommand.cmd, job.testCommand.args, { env: { ...process.env, ...job.testCommand.env }, stdio: ['pipe', 'pipe', 'pipe'] });
      } else proc = spawnCore(job.coreKind || 'cli', job.args, env);
    } catch (err) {
      // a failed job must not block the queue (the original code threw here and the queue stalled forever)
      this._fail(job, err.message);
      setImmediate(() => this.pump());
      return;
    }
    job.proc = proc;
    proc.stdin.on('error', () => {});
    const onLine = (line) => {
      if (line.startsWith(PROGRESS_PREFIX)) {
        try { job.progress = { ...job.progress, ...JSON.parse(line.slice(PROGRESS_PREFIX.length)) }; } catch { /* ignore */ }
        this.emit(job);
        return;
      }
      const text = redact(line);
      job.logTail.push(text);
      if (job.logTail.length > 400) job.logTail.splice(0, job.logTail.length - 400);
      send('jobs:log', { id: job.id, text });
    };
    readline.createInterface({ input: proc.stdout }).on('line', onLine);
    readline.createInterface({ input: proc.stderr }).on('line', onLine);
    let finished = false;
    const finish = (code, spawnError) => {
      if (finished) return; // 'error' and 'close' can both fire
      finished = true;
      job.exitCode = code;
      job.endedAt = Date.now();
      // exit codes of lynxx_cmd.run_cmd: 0 ok, 3 some items failed, 130 cancelled, other = error (2 = invalid arguments)
      if (job.status === 'cancelling' || code === 130 || job.progress.cancelled) job.status = 'cancelled';
      else if (!spawnError && (code === 0 || code === 3)) job.status = 'done';
      else {
        job.status = 'failed';
        job.error = spawnError || job.progress.error || (job.progress.cfChallenge ? 'cloudflare challenge' : `exit code ${code}`);
      }
      delete job.proc;
      this.emit(job);
      this.pump();
      notifyJob(job);
      if (job.status === 'done' && !job.coreKind && RE_ABBR.test(String(job.module))) learnTagList(job.module);
      if (job.autoKey) autoJobFinished(job);
    };
    proc.on('error', (err) => { onLine(`ERROR: ${err.message}`); finish(null, err.message); });
    // 'close' (not 'exit'): stdout is fully read, the final DONE line is already parsed
    proc.on('close', (code, signal) => finish(code ?? (signal ? 130 : null)));
    this.emit(job);
  }

  cancel(id) {
    const job = this.jobs.get(id);
    if (!job) return false;
    if (job.status === 'queued') {
      job.status = 'cancelled';
      this.emit(job);
      this.pump();
      return true;
    }
    if (job.status === 'running' && job.proc) {
      job.status = 'cancelling';
      this.emit(job);
      try { job.proc.stdin.write('cancel\n'); } catch { /* ignore */ }
      const p = job.proc;
      setTimeout(() => { if (p.exitCode === null) p.kill(); }, 15000);
      return true;
    }
    return false;
  }

  clearFinished() {
    for (const [id, j] of this.jobs) if (['done', 'failed', 'cancelled'].includes(j.status)) this.jobs.delete(id);
    return this.list();
  }

  list() {
    return [...this.jobs.values()].map((j) => this.public(j));
  }

  async shutdown(graceMs = QUIT_GRACE_MS) {
    // quit / relaunch confirmed: queued jobs are dropped, running ones get 'cancel' (the core stops cleanly), processes
    // still alive after the grace period are killed (SIGTERM, then SIGKILL)
    this.closing = true;
    for (const j of this.jobs.values()) {
      if (j.status !== 'queued') continue;
      j.status = 'cancelled';
      j.endedAt = Date.now();
      this.emit(j);
    }
    const live = [...this.jobs.values()].filter((j) => j.proc && isAlive(j.proc));
    for (const j of live) {
      if (j.status === 'running') { j.status = 'cancelling'; this.emit(j); }
      try { j.proc.stdin.write('cancel\n'); } catch { /* ignore */ }
    }
    const procs = live.map((j) => j.proc);
    await Promise.all(procs.map((p) => waitExit(p, graceMs)));
    const stubborn = procs.filter(isAlive);
    for (const p of stubborn) { try { p.kill(); } catch { /* ignore */ } }
    await Promise.all(stubborn.map((p) => waitExit(p, 2000)));
    for (const p of stubborn.filter(isAlive)) { try { p.kill('SIGKILL'); } catch { /* ignore */ } }
    return { stopped: procs.length, killed: stubborn.length };
  }

  killAll() {
    // last resort at exit: shutdown() normally ran already
    for (const j of this.jobs.values()) {
      if (!j.proc || !isAlive(j.proc)) continue;
      try { j.proc.stdin.write('cancel\n'); } catch { /* ignore */ }
      try { j.proc.kill(); } catch { /* ignore */ }
    }
  }
}

function isAlive(p) {
  return p.exitCode === null && p.signalCode === null;
}

function waitExit(p, ms) {
  if (!isAlive(p)) return Promise.resolve(true);
  return new Promise((resolve) => {
    const onExit = () => { clearTimeout(timer); resolve(true); };
    const timer = setTimeout(() => { p.off('exit', onExit); resolve(false); }, ms);
    p.once('exit', onExit);
  });
}

const jobs = new Jobs();

// ---------- thumbnails ----------

const thumbDir = () => path.join(userData(), 'cache', 'thumbs');

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

const thumbInflight = new Map();

// module abbr -> {host, capabilities} of the installed rows of ext.list. host: its website (manifest 'site', without a leading
// 'www.'): a module's own headers and cookies (Settings → Kết nối, theo site) go with a thumbnail / media request to that host
// and its subdomains only, never to a CDN of another domain (a cf_clearance cookie must not leave the site that issued it).
// capabilities: a job gets '-mp4' / '-webm' only for a module with video_format_choice (moduleCapabilities)
const SITE_HOSTS_TTL_MS = 5 * 60 * 1000;
const SITE_HOSTS_MISS_MS = 30 * 1000; // an abbr not in the cache (an extension installed since) reads the list again, at most this often
let siteHosts = null;
let siteHostsAt = 0;
let siteHostsLoading = null;

function siteHostOf(site) {
  try {
    const u = new URL(String(site || ''));
    return u.protocol === 'https:' || u.protocol === 'http:' ? u.hostname.toLowerCase().replace(/\.$/, '').replace(/^www\./, '') : '';
  } catch {
    return '';
  }
}

function onSiteHost(host, siteHost) {
  const h = String(host || '').toLowerCase().replace(/\.$/, '');
  return Boolean(siteHost) && (h === siteHost || h.endsWith(`.${siteHost}`));
}

function rememberSiteHosts(rows) {
  const map = new Map();
  for (const r of Array.isArray(rows) ? rows : []) {
    if (!r?.installed || !RE_ABBR.test(r.abbr || '')) continue;
    const capabilities = Array.isArray(r.capabilities) ? r.capabilities.filter((c) => typeof c === 'string') : [];
    map.set(r.abbr, { host: siteHostOf(r.site), capabilities });
  }
  siteHosts = map;
  siteHostsAt = Date.now();
}

async function moduleRow(module) {
  // the cached ext.list row of an installed module (null: not installed, or the list could not be read)
  if (!RE_ABBR.test(module || '')) return null;
  const age = Date.now() - siteHostsAt;
  if (!siteHosts || age > SITE_HOSTS_TTL_MS || (!siteHosts.has(module) && age > SITE_HOSTS_MISS_MS)) {
    siteHostsLoading = siteHostsLoading || sidecar.call('ext.list', {}, 15000).then(rememberSiteHosts)
      .catch(() => { /* sidecar busy or not ready: nothing known this time, read again on the next request */ })
      .finally(() => { siteHostsLoading = null; });
    await siteHostsLoading;
  }
  return siteHosts?.get(module) || null;
}

const moduleSiteHost = async (module) => (await moduleRow(module))?.host || '';
const moduleCapabilities = async (module) => (await moduleRow(module))?.capabilities || null; // null: unknown

// a website's own header never replaces what the request itself sets, and never one the network stack refuses: Electron's
// net.request fails the whole request (net::ERR_INVALID_ARGUMENT) on TE, Trailer, Upgrade, Cookie2, Keep-Alive, Host,
// Content-Length, Transfer-Encoding, Proxy-* and a method override, and an Expect header gets a 417 (checked with Electron 45)
const UPSTREAM_HEADERS_SKIP = new Set(['host', 'cookie', 'cookie2', 'range', 'content-length', 'connection', 'transfer-encoding', 'origin',
  'te', 'trailer', 'upgrade', 'keep-alive', 'expect', 'x-http-method', 'x-http-method-override', 'x-method-override']);
const upstreamHeaderSkipped = (name) => UPSTREAM_HEADERS_SKIP.has(name) || name.startsWith('proxy-');

async function upstreamHeaders(target, referer, module, siteHttp = false) {
  // Lynxx's User-Agent and the site as referer; on the module's own website (or a subdomain), over https only, its per-site
  // headers (a browser User-Agent, the one its cf_clearance cookie was issued with) and cookies too, as the core sends them with
  // the listing. Never over plain http (a browser keeps a Secure cf_clearance off it too). siteHttp: the self-test's local site
  const h = { 'user-agent': `Lynxx/${app.getVersion()}` };
  if (referer && /^https?:\/\//i.test(referer)) h.referer = referer;
  const url = new URL(target);
  if (!module || !(url.protocol === 'https:' || siteHttp) || !onSiteHost(url.hostname, await moduleSiteHost(module))) return h;
  for (const [name, value] of Object.entries(siteHttpMap(module, 'Header'))) {
    if (!upstreamHeaderSkipped(name.toLowerCase())) h[name.toLowerCase()] = value;
  }
  const cookie = Object.entries(siteHttpMap(module, 'Cookie')).map(([name, value]) => `${name}=${value}`).join('; ');
  if (cookie) h.cookie = cookie;
  return h;
}

function requestOnce(url, headers, signal) {
  // one upstream GET that does not follow a redirect: {status, location} for one, else {status, header(name), body} with body a
  // web stream (cancelling it, or the signal, ends the request). No session cookies: only the ones in headers are sent
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

async function upstreamGet(target, { referer = '', module = '', range = '', signal = null, check = assertPublicHost, siteHttp = false } = {}) {
  // a site's file link may answer with a redirect to its CDN (a video's get_file link): followed by hand, at most
  // MEDIA_REDIRECTS_MAX times, each hop checked against private addresses and given the site's headers / cookies only on its host
  let url = target;
  for (let hop = 0; ; hop++) {
    if (!/^https?:$/.test(new URL(url).protocol)) throw new Error('unsupported scheme');
    await check(url);
    const own = async (m) => {
      const headers = await upstreamHeaders(url, referer, m, siteHttp);
      if (range) headers.range = range;
      return headers;
    };
    let r;
    try {
      r = await requestOnce(url, await own(module), signal);
    } catch (e) {
      // a per-site header the network stack still refuses (nothing was sent): this hop once more without the site's entries,
      // so one bad header does not break every thumbnail of the module
      if (!module || !/ERR_INVALID_ARGUMENT/.test(String(e?.message))) throw e;
      r = await requestOnce(url, await own(''), signal);
    }
    if (r.location === undefined) return { ...r, url };
    if (hop >= MEDIA_REDIRECTS_MAX) throw new Error('too many redirects');
    url = new URL(r.location, url).href;
  }
}

function upstreamError(r) {
  // a Cloudflare bot challenge (the site's cf_clearance cookie is missing, expired, or sent with another User-Agent) says so
  const challenge = r.status === 403 && /challenge/i.test(r.header('cf-mitigated'));
  return `upstream ${r.status}${challenge ? ': bot challenge' : ''}`;
}

async function fetchThumb(target, referer, module, check = assertPublicHost, siteHttp = false) {
  // check / siteHttp: the self-test's local site (loopback, plain http)
  const ctrl = new AbortController();
  const timer = setTimeout(() => ctrl.abort(), THUMB_TIMEOUT_MS);
  try {
    const r = await upstreamGet(target, { referer, module, signal: ctrl.signal, check, siteHttp });
    if (r.status < 200 || r.status > 299) throw new Error(upstreamError(r));
    const type = r.header('content-type').toLowerCase();
    if (!type.startsWith('image/') && !type.startsWith('video/')) throw new Error('not an image');
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

async function streamMedia(target, referer, range, module, check = assertPublicHost, siteHttp = false) {
  // in-app player (Preview lightbox): one upstream request per range, streamed through, not cached; video / audio only
  const r = await upstreamGet(target, { referer, module, range: range && /^bytes=\d*-\d*$/.test(range) ? range : '', check, siteHttp });
  const type = r.header('content-type').toLowerCase();
  const ok = r.status >= 200 && r.status <= 299;
  if (!ok || !(type.startsWith('video/') || type.startsWith('audio/') || type === 'application/ogg')) {
    try { await r.body.cancel(); } catch { /* ignore */ }
    return new Response(ok ? 'not a media file' : upstreamError(r), { status: ok ? 415 : 502 });
  }
  const out = { 'content-type': type, 'accept-ranges': r.header('accept-ranges') || 'none' };
  for (const h of ['content-length', 'content-range']) if (r.header(h)) out[h] = r.header(h);
  return new Response(r.body, { status: r.status, headers: out });
}

// a thumbnail fetched for a module may carry its cookie / headers: cached apart from the same address fetched without them
const thumbKey = (target, module) => crypto.createHash('sha1').update(module ? `${module}\n${target}` : target).digest('hex');

function registerThumbProtocol() {
  protocol.handle('lynxx-thumb', async (request) => {
    const u = new URL(request.url);
    const target = u.searchParams.get('u') || '';
    const referer = u.searchParams.get('r') || '';
    const module = u.searchParams.get('m') || ''; // the Preview's module: its per-site headers / cookies, see upstreamHeaders
    if (!/^https?:\/\//i.test(target)) return new Response('bad url', { status: 400 });
    if (module && !RE_ABBR.test(module)) return new Response('bad module', { status: 400 });
    if (u.host === 'm') {
      try {
        return await streamMedia(target, referer, request.headers.get('range') || '', module);
      } catch (err) {
        return new Response(String(err.message || err), { status: /blocked|scheme/.test(String(err.message)) ? 403 : 502 });
      }
    }
    const key = thumbKey(target, module);
    const ext = (path.extname(new URL(target).pathname) || '.img').slice(0, 6).toLowerCase();
    const file = path.join(thumbDir(), `${key}${ext}`);
    try {
      const buf = await fsp.readFile(file);
      return new Response(buf, { headers: { 'content-type': mimeOf(ext), 'cache-control': 'max-age=86400' } });
    } catch { /* not cached */ }
    try {
      if (!thumbInflight.has(key)) {
        thumbInflight.set(key, fetchThumb(target, referer, module).finally(() => thumbInflight.delete(key)));
      }
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

function mimeOf(ext) {
  return {
    '.jpg': 'image/jpeg', '.jpeg': 'image/jpeg', '.png': 'image/png', '.gif': 'image/gif', '.webp': 'image/webp', '.avif': 'image/avif',
    '.mp4': 'video/mp4', '.webm': 'video/webm',
  }[ext.toLowerCase()] || 'application/octet-stream';
}

function proxyConfig(c) {
  if (!c.proxy || c.ignoreProxy) return { mode: 'system' };
  // chromium proxy rules: http://host:port, socks5://host:port (socks5h behaves like socks5: dns is resolved by the proxy).
  // Chromium takes no credentials here: they are dropped (see verifySite for the window that needs the core's IP)
  return { proxyRules: c.proxy.replace(/^socks5h:/i, 'socks5:').replace(/\/\/[^@/]*@/, '//') };
}

const proxyHasLogin = (c) => Boolean(c.proxy && !c.ignoreProxy && /^[a-z0-9]+:\/\/[^@/]*@/i.test(c.proxy));

async function applyProxyToSession() {
  await session.defaultSession.setProxy(proxyConfig(settings.connection));
}

// ---------- site verification ("Xác minh trong Lynxx") ----------
// A website behind a bot challenge (Cloudflare) opens in a window of its own where the USER passes the check: the app never
// solves, clicks or scripts it (no preload, no executeJavaScript, no input events). The core then needs what that window used:
// its cf_clearance cookie and its exact User-Agent, saved as the module's own cookie / header (siteCookies / siteHeaders: sent
// to that website only). The renderer names an abbr, never an address: the address is the installed module's manifest 'site'
const VERIFY_TIMEOUT_MS = 5 * 60 * 1000;
const VERIFY_POLL_MS = 1000; // the cookie is read again every second: a missed 'changed' event costs at most this
const VERIFY_SETTLE_MS = 2000; // a page that loaded without a challenge: time for its scripts to start one after all
const verifyWindows = new Map(); // abbr -> {win, promise}: one window per module, a second call focuses it and gets the same answer
const verifySessions = new WeakSet(); // sessions already given their handlers (fromPartition gives the same object every time)
const PACKAGE_NAME = (() => { try { return require('./package.json').name; } catch { return ''; } })();
// never the default session (thumbnails and media go through it); persist: a later check of the same site is faster
const verifyPartition = (abbr) => `persist:lynxx-verify-${abbr}`;

function saveSiteClearance(abbr, value, ua) {
  // the module's other entries (a login cookie) and the other modules stay; 'user-agent' in another letter case is replaced.
  // Both maps are checked before either is written
  const c = settings.connection;
  const siteCookies = normalizeSiteHttpMaps(withSiteEntry(c.siteCookies, abbr, { [CLEARANCE_COOKIE]: value }), 'Cookie', true);
  const siteHeaders = normalizeSiteHttpMaps(withSiteEntry(c.siteHeaders, abbr, { 'User-Agent': ua }), 'Header', true);
  Object.assign(c, { siteCookies, siteHeaders });
  saveSettings();
}

async function verifyTarget(abbr) {
  const info = (await sidecar.call('ext.list', {}, 15000)).find((m) => m.abbr === abbr && m.installed);
  if (!info) throw new Error(`module ${abbr} chưa được cài`);
  const name = String(info.name || abbr);
  let url = null;
  try { url = new URL(String(info.site || '')); } catch { /* checked below */ }
  if (!url || url.protocol !== 'https:' || !siteBaseHost(url.href)) throw new Error(`${name}: địa chỉ website của module không phải https`);
  try {
    await assertPublicHost(url.href);
  } catch (e) {
    throw new Error(e.message === 'blocked host' ? `${name}: địa chỉ website trỏ vào mạng nội bộ, không mở`
      : `${name}: không tìm thấy ${url.hostname} (${e.code || e.message})`);
  }
  return { name, site: url.href };
}

const VERIFY_PROXY_LOGIN = 'Proxy có tên đăng nhập / mật khẩu: cửa sổ xác minh không dùng được loại proxy này, mà cookie chỉ dùng được từ IP của proxy';

async function verifyProxy(site, call = (m, p) => sidecar.call(m, p, 10000)) {
  // the window leaves where the core does (the cookie is bound to the IP it was issued to): Lynxx's proxy when one is set.
  // Otherwise the core's requests session takes the proxy of its environment (http(s)_proxy variables, the Windows registry,
  // macOS settings; never PAC / WPAD nor the GNOME / KDE settings that Chromium's 'system' mode reads): the sidecar names it for
  // the site (net.proxyFor), none = direct. An older core without that method: direct. call: the self-test's stand-in
  const c = settings.connection;
  if (c.proxy && !c.ignoreProxy) return proxyConfig(c);
  let proxy = '';
  try {
    const r = await call('net.proxyFor', { url: site });
    proxy = typeof r?.proxy === 'string' ? r.proxy : '';
  } catch { /* an older core: direct */ }
  const p = envProxyRules(proxy);
  if (p.login) throw new Error(VERIFY_PROXY_LOGIN.replace('Proxy có', 'Proxy của hệ thống (lõi dùng nó khi Lynxx không đặt proxy) có'));
  if (p.invalid) {
    throw new Error('Proxy của hệ thống (lõi dùng nó khi Lynxx không đặt proxy) không dùng được cho cửa sổ xác minh: đặt proxy trong Cài đặt → Kết nối');
  }
  return p.rules ? { proxyRules: p.rules } : { mode: 'direct' };
}

function verifySite(abbr) {
  // -> {ok: true, expires (seconds since 1970, null: a session cookie)} | {ok: false, reason, message}; the cookie value never
  // leaves main
  if (typeof abbr !== 'string' || !RE_ABBR.test(abbr)) throw new Error('mã site không hợp lệ');
  const open = verifyWindows.get(abbr);
  if (open) {
    if (open.win && !open.win.isDestroyed()) {
      if (open.win.isMinimized()) open.win.restore();
      open.win.show();
      open.win.focus();
    }
    return open.promise;
  }
  // Chromium drops proxy credentials (proxyConfig): the window would leave from another IP than the core, and the cookie is
  // bound to the IP it was issued to
  if (proxyHasLogin(settings.connection)) throw new Error(VERIFY_PROXY_LOGIN);
  const entry = { win: null, promise: null };
  entry.promise = (async () => {
    const target = await verifyTarget(abbr);
    const proxy = await verifyProxy(target.site);
    return runVerifyWindow({ abbr, ...target, proxy, partition: verifyPartition(abbr), onWindow: (w) => { entry.win = w; } });
  })().finally(() => { if (verifyWindows.get(abbr) === entry) verifyWindows.delete(abbr); });
  verifyWindows.set(abbr, entry);
  return entry.promise;
}

async function openVerifyWindow({ site, partition, title, show, proxy }) {
  const ses = session.fromPartition(partition);
  if (!verifySessions.has(ses)) {
    verifySessions.add(ses);
    ses.setPermissionRequestHandler((_wc, _perm, cb) => cb(false));
    ses.setPermissionCheckHandler(() => false);
    ses.on('will-download', (e) => e.preventDefault());
  }
  await ses.setProxy(proxy || { mode: 'direct' }); // the core's way out (verifyProxy): the cookie is bound to the IP it was issued to
  const ua = browserLikeUA(ses.getUserAgent(), [app.getName(), PACKAGE_NAME]);
  ses.setUserAgent(ua);
  const parent = mainWindow && !mainWindow.isDestroyed() ? mainWindow : undefined;
  const win = new BrowserWindow({
    parent, width: 1000, height: 760, show, title, autoHideMenuBar: true, icon: iconPath(settings.appearance.icon),
    webPreferences: {
      session: ses, sandbox: true, contextIsolation: true, nodeIntegration: false, nodeIntegrationInSubFrames: false, webviewTag: false,
      spellcheck: false, devTools: !app.isPackaged,
    },
  });
  win.setMenu(null);
  win.on('page-title-updated', (e) => e.preventDefault()); // the title says what the window is for
  const wc = win.webContents;
  wc.setUserAgent(ua);
  wc.setWindowOpenHandler(() => ({ action: 'deny' }));
  // only the site's own pages as the top page (a link or a redirect elsewhere stays out); the challenge's frame is a subframe
  const guard = (e, url, _inPlace, isMainFrame) => {
    const main = typeof e?.isMainFrame === 'boolean' ? e.isMainFrame : isMainFrame !== false;
    if (main && !sameSite(e?.url || url, site)) e.preventDefault();
  };
  wc.on('will-navigate', guard);
  wc.on('will-redirect', guard);
  wc.on('will-attach-webview', (e) => e.preventDefault());
  wc.on('will-prevent-unload', (e) => e.preventDefault()); // a page's "leave site?" never keeps the window open
  return { win, ses };
}

async function removeStoredClearance(ses, site) {
  // the cf_clearance of an earlier check (persist partition) on the site's host, apex or www. variant: it may be one the site no
  // longer takes (another IP since), and a check must never save it again
  const hosts = clearanceHosts(site);
  for (const c of await ses.cookies.get({ name: CLEARANCE_COOKIE }).catch(() => [])) {
    if (!hosts.some((h) => cookieForHost(c.domain, h))) continue;
    await ses.cookies.remove(`https://${String(c.domain).replace(/^\./, '')}${c.path || '/'}`, c.name).catch(() => {});
  }
}

function runVerifyWindow({ abbr, name, site, partition, proxy, load = site, show = true, timeoutMs = VERIFY_TIMEOUT_MS, onWindow }) {
  // load: the self-test opens about:blank in a test partition (no network) and feeds the tracker itself
  return openVerifyWindow({ site, partition, show, proxy, title: `Xác minh ${name}: tự qua bước kiểm tra của site, cửa sổ tự đóng khi xong` })
    .then(({ win, ses }) => new Promise((resolve, reject) => {
      const { origin, hostname } = new URL(site);
      const base = siteBaseHost(site);
      const wc = win.webContents;
      const tracker = new ChallengeTracker();
      let done = false;
      let loaded = false;
      let settle = null;
      let poll = null;
      let timer = null;
      const finish = (err, value) => {
        if (done) return;
        done = true;
        clearInterval(poll);
        clearTimeout(timer);
        clearTimeout(settle);
        ses.cookies.removeListener('changed', onCookie);
        ses.webRequest.onHeadersReceived(null);
        if (!win.isDestroyed()) win.destroy();
        if (err) reject(err);
        else resolve(value);
      };
      const check = () => {
        if (done || win.isDestroyed() || !tracker.passed) return;
        try {
          const { value, expires } = tracker.clearance;
          saveSiteClearance(abbr, value, wc.getUserAgent()); // the exact User-Agent the challenge saw
          finish(null, { ok: true, expires: expires || null });
        } catch (e) {
          finish(e);
        }
      };
      const readCookie = () => ses.cookies.get({ url: origin, name: CLEARANCE_COOKIE }).then((list) => tracker.cookie(list[0] || null), () => {});
      function onCookie(_e, cookie, _cause, removed) {
        if (cookie?.name !== CLEARANCE_COOKIE || !cookieForHost(cookie.domain, hostname)) return;
        tracker.cookieChanged(cookie, removed);
        setImmediate(check);
      }
      win.on('closed', () => finish(null, { ok: false, reason: 'cancelled', message: `Đã đóng cửa sổ xác minh ${name}: chưa lưu gì` }));
      ses.cookies.on('changed', onCookie);
      const urls = netlib.isIP(base) ? [`https://${base}/*`] : [`https://${base}/*`, `https://*.${base}/*`]; // an address has no subdomains
      ses.webRequest.onHeadersReceived({ urls }, (d, cb) => {
        if (d.resourceType === 'mainFrame' && (d.webContentsId === undefined || d.webContentsId === wc.id)) {
          tracker.mainFrame(d.statusCode, d.responseHeaders);
          setImmediate(check);
        }
        cb({});
      });
      wc.on('did-finish-load', () => {
        loaded = true;
        clearTimeout(settle);
        settle = setTimeout(() => {
          if (tracker.notNeeded) finish(null, { ok: false, reason: 'not-needed', message: `${name}: site không yêu cầu xác minh lúc này` });
        }, VERIFY_SETTLE_MS);
        check();
      });
      wc.on('did-fail-load', (_e, code, desc, _url, isMainFrame) => {
        // the first page did not load (connection, proxy, DNS): nothing to pass in this window. -3: replaced or stopped
        if (isMainFrame && code !== -3 && !loaded) finish(new Error(`Không mở được ${name}: ${desc || code}`));
      });
      timer = setTimeout(() => finish(null, { ok: false, reason: 'timeout',
        message: `${name}: hết ${Math.round(timeoutMs / 60000)} phút mà chưa qua bước kiểm tra, cửa sổ đã đóng` }), timeoutMs);
      // the cookie of an earlier check goes before the page loads, and the tracker is not given it: any cookie it sees was set
      // during this check. ready: the self-test sets its cookie after that
      const ready = removeStoredClearance(ses, site).then(() => {
        if (done) return;
        poll = setInterval(() => { readCookie().then(check); }, VERIFY_POLL_MS);
        win.loadURL(load).catch(() => { /* did-fail-load reports it */ });
      });
      onWindow?.(win, { ses, tracker, ready });
    }));
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
    '[Desktop Entry]', 'Type=Application', 'Name=Lynxx', 'Comment=Booru downloader', `Exec=${exec} %U`,
    `Icon=${iconFile}`, 'Terminal=false', 'Categories=Network;Graphics;', `StartupWMClass=${DESKTOP_ID}`, LAUNCHER_MARKER, '',
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
  // 'missing' | 'ours' | 'foreign': a launcher written by something else (ex. AppImage integration tools) is never touched
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

// ---------- tag lists auto update ----------

function onTagsUpdateDone(params) {
  // tags.update only starts a background job: the date is recorded when it finished without error
  if (params.ok && params.module) {
    settings.tags.lastAutoUpdate[params.module] = Date.now();
    saveSettings();
  }
}

async function maybeAutoUpdateTags() {
  if (!settings.tags.autoUpdate) return;
  const days = Math.max(1, settings.tags.autoUpdateDays || 7) * 86400000;
  for (const module of ['rx', 'db']) {
    const last = settings.tags.lastAutoUpdate[module] || 0;
    if (Date.now() - last < days) continue;
    if (module === 'rx' && !(await getSecret('rx'))) continue; // rx needs the user's own key: nothing to try without it
    try {
      await sidecar.call('tags.update', { module, apiKey: await getSecret(module) });
    } catch (err) {
      send('log', { source: 'tags', text: `auto update ${module} failed: ${err.message}` });
    }
  }
}

// ---------- every extension has a tag list at once ----------
// The generator puts the site's list into the draft (lynxx/extgen/taglist.py), the core uses it as soon as it is installed.
// A list too big for the draft (or one that needs the user's key) is read right after the install. A site without a tag listing,
// or whose robots.txt forbids it, gets its list learned from the posts previewed (core, search.preview) and downloaded (below):
// no request is made for it (lynxx/tags_learned.py).
async function installExtension(params) {
  const m = await sidecar.call('ext.install', params);
  if (RE_ABBR.test(String(m?.abbr || ''))) {
    fetchNewTagList(m.abbr).catch((err) => send('log', { source: 'tags', text: `tag list of ${m.abbr} not started: ${err.message}` }));
  }
  return m;
}

async function fetchNewTagList(abbr) {
  const status = await sidecar.call('tags.status', {});
  if ((Array.isArray(status) ? status : []).some((r) => r.module === abbr && r.size > 0 && !r.learned)) return; // shipped with it
  const src = ((await sidecar.call('tags.sources', {})) || []).find((x) => x.module === abbr);
  if (!src) return; // no tag listing: learned from the posts previewed and downloaded
  if (src.onDemand) return; // the extension's own update (manifest tagUpdate) runs only when the user asks: Tags A-Z button
  const apiKey = await getSecret(abbr);
  if (src.key === 'required' && !apiKey) return; // read once the user saves a key (Tags A-Z button, auto update)
  await sidecar.call('tags.update', { module: abbr, apiKey });
  send('log', { source: 'tags', text: `${abbr}: đang tải danh sách tag của extension vừa cài` });
}

function learnTagList(module) {
  // after a download job: the module's downloaded posts teach its list when it has none from a source (no request). written: the
  // list served for it changed, also the tags learned on top of a list (rn, rs; rp, whose own update merges: 'learned' stays false
  // for those)
  return sidecar.call('tags.learn', { module }, 60000).then((r) => {
    if (r?.written) send('sidecar:notify', { method: 'tags.learned', params: r });
  }).catch(() => { /* sidecar not running: the next job or preview learns it */ });
}

// ---------- scheduled tag list update (system scheduler) ----------
// The system scheduler runs 'lynxx-core tags' (development: python -m lynxx.tags_updater) even while the app is closed, for the
// modules that need no key; the lists go to <userData>/tags, where the app looks for the newest list of each module. A scheduled
// run cannot read the app's keys (OS keyring): rx keeps the in-app update. Linux / macOS: a marked block of the user's crontab
// (the rest of the crontab is left as it is); Windows: a Task Scheduler task; AppImage: unsupported (its core only exists while
// the app runs). The entry is written again at every start, so a new install path or version is picked up.
// 'auto': the core picks the modules when it runs (lynxx/tags_updater.py tag_sources): every installed module with a tag source
// that needs no key of the user, the reviewed ones (db, xb, bb, en) and extensions made from a known engine. An extension
// installed later is covered without writing the entry again.
const TAG_SCHEDULE_MODULES = 'auto';
const WIN_TASK = 'Lynxx tag lists';
const tagScheduleLog = () => path.join(userData(), 'logs', 'tags-schedule.log');

function tagScheduleSupport() {
  if (process.env.APPIMAGE) {
    return { ok: false, reason: 'Bản AppImage không hỗ trợ lịch hệ thống: lõi của app chỉ tồn tại khi app đang chạy. Dùng bản .deb, hoặc cron trong tools/cron/.' };
  }
  if (process.platform === 'win32') return { ok: true, kind: 'schtasks' };
  const r = spawnSync('crontab', ['-l'], { encoding: 'utf8' });
  if (r.error) return { ok: false, reason: 'Máy không có lệnh crontab (cài gói cron hoặc cronie).' };
  return { ok: true, kind: 'crontab' };
}

function tagScheduleArgs() {
  return ['--modules', TAG_SCHEDULE_MODULES, '--out', path.join(userData(), 'tags'), '--min-posts', '2', '--log', tagScheduleLog()];
}

function tagScheduleCommand() {
  // POSIX shell line for crontab: absolute paths only (cron has a minimal PATH), no secret in it
  const c = coreCommand('tags', tagScheduleArgs());
  const env = `LYNXX_DATA_DIR=${shq(userData())}${c.frozen ? '' : ` PYTHONPATH=${shq(REPO_ROOT)}`}`;
  return `cd ${shq(c.cwd)} && ${env} ${[c.cmd, ...c.args].map(shq).join(' ')}`;
}

const tagScheduleWhen = (frequency) => scheduleWhen(userData(), frequency);

function readCrontab() {
  const r = spawnSync('crontab', ['-l'], { encoding: 'utf8' });
  if (r.status === 0) return r.stdout;
  if (/no crontab/i.test(r.stderr || '')) return '';
  throw new Error(`không đọc được crontab: ${(r.stderr || r.error?.message || '').trim()}`); // never overwrite a crontab we could not read
}

function writeCrontab(text) {
  const r = spawnSync('crontab', ['-'], { input: text.endsWith('\n') ? text : `${text}\n`, encoding: 'utf8' });
  if (r.status !== 0) throw new Error(`không ghi được crontab: ${(r.stderr || r.error?.message || '').trim()}`);
}

function tagScheduleStatus() {
  const sup = tagScheduleSupport();
  const sched = settings.tags.schedule || {};
  let lastRun = 0;
  try { lastRun = fs.statSync(tagScheduleLog()).mtimeMs; } catch { /* never ran */ }
  let installed = false;
  if (sup.ok && sup.kind === 'crontab') {
    try { installed = hasTagBlock(readCrontab()); } catch { /* unreadable: not ours */ }
  }
  if (sup.ok && sup.kind === 'schtasks') installed = spawnSync('schtasks', ['/Query', '/TN', WIN_TASK], { windowsHide: true }).status === 0;
  const frequency = sched.frequency === 'daily' ? 'daily' : 'weekly';
  return {
    ...sup, installed, enabled: sched.enabled === true, frequency, time: tagScheduleWhen(frequency).time, log: tagScheduleLog(), lastRun,
  };
}

function applyTagSchedule(enabled, frequency) {
  const sup = tagScheduleSupport();
  if (!sup.ok) throw new Error(sup.reason);
  const freq = frequency === 'daily' ? 'daily' : 'weekly';
  fs.mkdirSync(path.dirname(tagScheduleLog()), { recursive: true });
  const when = tagScheduleWhen(freq);
  if (sup.kind === 'crontab') {
    const current = readCrontab();
    if (enabled) writeCrontab(withTagBlock(current, `${when.cron} ${tagScheduleCommand()}`));
    else if (hasTagBlock(current)) writeCrontab(withoutTagBlock(current));
  } else if (enabled) {
    const c = coreCommand('tags', tagScheduleArgs());
    if (!c.frozen) throw new Error('Trên Windows, lịch hệ thống chỉ dùng được với bản cài đặt (lõi đóng gói)');
    const tr = [c.cmd, ...c.args].map((a) => (/[\s"]/.test(a) ? `"${a}"` : a)).join(' ');
    const r = spawnSync('schtasks', ['/Create', '/F', '/TN', WIN_TASK, '/SC', freq === 'daily' ? 'DAILY' : 'WEEKLY',
      ...(freq === 'daily' ? [] : ['/D', 'MON']), '/ST', when.time, '/TR', tr], { encoding: 'utf8', windowsHide: true });
    if (r.status !== 0) throw new Error(`schtasks: ${(r.stderr || r.stdout || r.error?.message || '').trim()}`);
  } else {
    spawnSync('schtasks', ['/Delete', '/F', '/TN', WIN_TASK], { windowsHide: true });
  }
  settings.tags.schedule = { enabled, frequency: freq };
  saveSettings();
  return tagScheduleStatus();
}

async function scheduledTagModules() {
  // what 'auto' covers now (shown in Tags A-Z): installed modules with a tag source that needs no key, except the extensions' own
  // updates (onDemand: run only when the user asks); [] while the core is not up
  try {
    const rows = await sidecar.call('tags.sources', {}, 5000);
    return (Array.isArray(rows) ? rows : []).filter((s) => s.key !== 'required' && !s.onDemand).map((s) => ({ module: s.module, name: s.name }));
  } catch { return []; }
}

function refreshTagSchedule() {
  // at start: write the entry again, so it runs the core of the version installed now
  if (settings.tags.schedule?.enabled !== true) return;
  try { applyTagSchedule(true, settings.tags.schedule.frequency); } catch (err) {
    send('log', { source: 'tags', text: `scheduled tag update not refreshed: ${err.message}` });
  }
}

// ---------- IPC ----------

const RENDERER_URL = pathToFileURL(path.join(__dirname, 'renderer', 'index.html')).href;

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

function registerIpc() {
  handle('sidecar:getStatus', () => sidecar.status || { ok: false, error: 'starting' });
  handle('rpc', async (_e, method, params) => {
    // installs never go through the generic rpc channel (see ext:* handlers): they need main-side checks and confirmation
    const allowed = ['app.info', 'ext.list', 'ext.uninstall', 'ext.remoteCatalog', 'tags.autocomplete', 'tags.browse',
      'tags.validate', 'tags.status', 'tags.sources', 'tags.learn', 'tags.resolve', 'tags.fuzzy', 'tags.types', 'files.nonEmpty', 'files.identify',
      'index.search', 'index.stats'];
    if (!allowed.includes(method)) throw new Error(`method not allowed: ${method}`);
    params = params && typeof params === 'object' ? params : {};
    if (method === 'ext.uninstall' && !RE_EXT_ID.test(String(params.id || ''))) throw new Error('invalid extension id');
    if (method === 'ext.remoteCatalog') {
      const url = String(settings.advanced.catalogUrl || '');
      if (!/^https:\/\//i.test(url)) throw new Error('catalog url must start with https://');
      params = { url };
    }
    const r = await sidecar.call(method, params);
    if (method === 'ext.list') rememberSiteHosts(r); // the renderer reads the list at start and after every install / removal
    return r;
  });
  handle('ext:installCatalog', async (_e, id) => {
    if (!RE_EXT_ID.test(String(id || ''))) throw new Error('invalid extension id');
    return installExtension({ id });
  });
  // remote catalog: only the id comes from the renderer. The entry (archive url, sha256) is taken from the catalog the core
  // verified (Ed25519 signature, lynxx/trust.py); the install verifies it again and pins the sha256 the user confirmed
  handle('ext:installRemote', async (_e, item) => {
    const id = String(item?.id || '');
    if (!RE_EXT_ID.test(id)) throw new Error('invalid extension id');
    const catalogUrl = String(settings.advanced.catalogUrl || '');
    if (!/^https:\/\//i.test(catalogUrl)) throw new Error('catalog url must start with https://');
    const entries = await sidecar.call('ext.remoteCatalog', { url: catalogUrl });
    const entry = (Array.isArray(entries) ? entries : []).find((x) => isPlainObject(x) && x.id === id);
    if (!entry || !RE_SHA256.test(String(entry.sha256 || ''))) throw new Error(`extension "${id}" không có trong catalog`);
    if (entry.denied) throw new Error(String(entry.denied.message || 'site nằm trong danh sách chặn của Lynxx'));
    const trust = entry.signed ? `Catalog có chữ ký hợp lệ (khoá ${String(entry.keyId || '?').slice(0, 16)}).`
      : 'CẢNH BÁO: catalog CHƯA KÝ, chỉ được nhận vì đang bật LYNXX_CATALOG_ALLOW_UNSIGNED (chế độ nhà phát triển).';
    const ok = await confirmMain(`Cài extension "${String(entry.name || id).slice(0, 80)}"?`,
      `Extension là code Python sẽ chạy trên máy bạn với quyền của bạn.\nNguồn: ${String(entry.url || '').slice(0, 300)}\n`
      + `sha256: ${entry.sha256}\n${trust}\nChỉ cài từ nguồn bạn tin cậy.`);
    if (!ok) throw new Error('cancelled');
    return installExtension({ catalogUrl, remoteId: id, sha256: entry.sha256 });
  });
  // opts.full: read the whole list where the last pages would do (Rule34, lynxx/tags_updater.py update_tag_list)
  handle('tags:update', async (_e, module, opts) => sidecar.call('tags.update', { module, apiKey: await getSecret(module), full: opts?.full === true }));
  handle('tags:scheduleStatus', async () => ({ ...tagScheduleStatus(), modules: await scheduledTagModules() }));
  handle('tags:schedule', async (_e, o) => ({ ...applyTagSchedule(o?.enabled === true, o?.frequency), modules: await scheduledTagModules() }));
  handle('search:count', async (_e, q) => sidecar.call('search.count', {
    module: asQuery(q).module, tags: q.tags, args: [...(await connectionArgs(q.module)), ...searchArgs(q), ...tagLimitArgs(q.module)],
  }));
  handle('search:preview', async (_e, q, page) => {
    const params = {
      module: asQuery(q).module, tags: q.tags, page: page || 0,
      // the preview downloads nothing: no media check (a model server older than /v1/frames would fail it)
      args: [...(await connectionArgs(q.module)), ...searchArgs(q), ...tagLimitArgs(q.module),
        ...genderArgs(isPlainObject(q.gender) ? { ...q.gender, verifyMedia: false } : q.gender), ...safetyArgs(q),
        ...(await genderSecretArgs(q.gender))],
    };
    if (Number.isInteger(q.pageSize) && q.pageSize > 0) params.pageSize = q.pageSize; // the sidecar caps it to the module page size
    return sidecar.call('search.preview', params, 180000);
  });
  handle('search:related', async (_e, q) => {
    // tags co-occurring in the first results page: same connection / date args as the preview, no content filter
    const params = {
      module: asQuery(q).module, tags: q.tags, args: [...(await connectionArgs(q.module)), ...searchArgs(q), ...tagLimitArgs(q.module)],
    };
    if (Number.isInteger(q.limit) && q.limit > 0) params.limit = Math.min(q.limit, 200);
    return sidecar.call('search.related', params, 180000);
  });

  handle('settings:get', () => ({ settings, appearance: appearanceState(), secrets: secretsStatus() }));
  handle('settings:set', async (_e, patch) => {
    if (!isPlainObject(patch)) throw new Error('invalid settings patch');
    delete patch.secrets;
    delete patch.version;
    const py = patch.advanced?.pythonPath;
    if (py !== undefined && py !== settings.advanced.pythonPath) {
      // the python path is executed by the app: changing it requires a native confirmation
      const valid = py === '' || (typeof py === 'string' && path.isAbsolute(py) && fs.existsSync(py) && fs.statSync(py).isFile());
      if (!valid || (py && !(await confirmMain('Dùng chương trình này làm Python cho Lynxx?', py)))) delete patch.advanced.pythonPath;
    }
    if (isPlainObject(patch.search)) {
      // history / saved queries are written whole by the renderer: drop malformed entries, cap sizes
      for (const [key, max, named] of [['history', HISTORY_MAX, false], ['saved', SAVED_MAX, true]]) {
        if (patch.search[key] === undefined) continue;
        const v = sanitizeQueryList(patch.search[key], max, named);
        if (v === null) delete patch.search[key];
        else {
          if (named) mergeAutoRuntime(v);
          patch.search[key] = v;
        }
      }
      if (patch.search.tagLimits !== undefined) {
        const v = sanitizeTagLimits(patch.search.tagLimits);
        if (v === null) delete patch.search.tagLimits;
        else patch.search.tagLimits = v;
      }
    }
    const replaceMaps = {}; // headers / cookies are edited as a whole: replaced, not merged (a removed entry must go away)
    if (isPlainObject(patch.connection)) {
      for (const [key, kind] of [['headers', 'Header'], ['cookies', 'Cookie']]) {
        if (patch.connection[key] === undefined) continue;
        replaceMaps[key] = normalizeHttpMap(patch.connection[key], kind, true);
        delete patch.connection[key];
      }
      for (const [key, kind] of [['siteHeaders', 'Header'], ['siteCookies', 'Cookie']]) {
        if (patch.connection[key] === undefined) continue;
        replaceMaps[key] = normalizeSiteHttpMaps(patch.connection[key], kind, true);
        delete patch.connection[key];
      }
      if (patch.connection.keyRateLimits !== undefined) {
        const v = sanitizeKeyRateLimits(patch.connection.keyRateLimits);
        if (v === null) delete patch.connection.keyRateLimits;
        else patch.connection.keyRateLimits = v;
      }
    }
    if (isPlainObject(patch.genderModel) && patch.genderModel.url !== undefined) {
      const u = sanitizeModelUrl(patch.genderModel.url);
      if (u === null) throw new Error('Địa chỉ model server phải có dạng http://máy:cổng hoặc https://…');
      patch.genderModel.url = u;
    }
    if (patch.library !== undefined && !isPlainObject(patch.library)) throw new Error('invalid settings patch');
    if (patch.library?.trashDays !== undefined && !validTrashDays(patch.library.trashDays)) {
      throw new Error(`Số ngày giữ thùng rác phải là số nguyên từ 0 đến ${TRASH_DAYS_MAX}`);
    }
    const trashDaysBefore = settings.library.trashDays;
    settings = deepMerge(settings, patch);
    settings.genderModel.enabled = settings.genderModel.enabled === true;
    Object.assign(settings.connection, replaceMaps);
    if (!GEN_MODELS.includes(settings.ai.model)) settings.ai.model = DEFAULT_SETTINGS.ai.model;
    settings.ai.maxUsd = clampUsd(settings.ai.maxUsd, DEFAULT_SETTINGS.ai.maxUsd);
    saveSettings();
    // a shorter time in the trash (or none before): what is older than it now goes at once, not at the next 12-hour check
    const trashDays = settings.library.trashDays;
    if (trashDays > 0 && (trashDaysBefore === 0 || trashDays < trashDaysBefore)) purgeOldTrash('setting').catch(() => {});
    if (patch?.connection?.keyRateLimits) syncKeyRateLimits();
    if (patch?.connection) await applyProxyToSession();
    let iconSync;
    if (patch?.appearance) {
      if (patch.appearance.icon) iconSync = await syncIcon();
      send('appearance:changed', appearanceState());
    }
    return { settings, appearance: appearanceState(), secrets: secretsStatus(), iconSync };
  });
  handle('secrets:set', async (_e, module, value) => {
    await setSecret(module, typeof value === 'string' ? value.trim() : ''); // empty: no change
    return secretsStatus();
  });
  handle('secrets:clear', (_e, module) => { clearSecret(module); return secretsStatus(); });
  // "Xác minh trong Lynxx": only an abbr (main reads the module's website itself), answered once the window is done
  handle('site:verify', (_e, abbr) => verifySite(abbr));

  handle('jobs:start', (_e, q) => jobs.start(isPlainObject(q) ? { ...q, autoKey: undefined, group: undefined } : q)); // only the scheduler sets autoKey
  // gender image model: runs in a model server (lynxx/model_server.py); the app only calls its API, the token stays in main
  handle('gmodel:status', async () => {
    if (!settings.genderModel.url) return { url: '', reachable: false, local: false, error: 'off' };
    return sidecar.call('gender.modelStatus', { url: settings.genderModel.url, token: await getSecret(MODEL_SECRET) }, 20000);
  });
  // turning the model on loads it on the server now (status after: the device it really runs on); off frees its memory
  handle('gmodel:load', async () => {
    if (!settings.genderModel.url) throw new Error('Chưa có địa chỉ model server');
    return sidecar.call('gender.modelLoad', { url: settings.genderModel.url, token: await getSecret(MODEL_SECRET) }, 200000);
  });
  handle('gmodel:unload', async () => {
    if (!settings.genderModel.url) return { url: '', reachable: false, local: false, error: 'off' };
    return sidecar.call('gender.modelUnload', { url: settings.genderModel.url, token: await getSecret(MODEL_SECRET) }, 20000);
  });
  handle('gmodel:setToken', (_e, value) => setModelToken(value));
  handle('gmodel:clearToken', () => clearModelToken());
  handle('saved:auto', (_e, key, every, from) => setSavedAuto(String(key || ''), Number(every) || 0, from === 'now' ? 'now' : 'all'));
  handle('jobs:cancel', (_e, id) => jobs.cancel(id));
  handle('jobs:list', () => jobs.list());
  handle('jobs:clear', () => jobs.clearFinished());
  handle('jobs:rerun', (_e, id, allowDuplicate) => jobs.rerun(String(id || ''), allowDuplicate === true));

  handle('dialog:folder', async (_e, current) => {
    const r = await dialog.showOpenDialog(mainWindow, { properties: ['openDirectory', 'createDirectory'], defaultPath: current || undefined });
    return r.canceled ? '' : r.filePaths[0];
  });
  handle('dialog:image', async () => {
    const r = await dialog.showOpenDialog(mainWindow, {
      properties: ['openFile'], filters: [{ name: 'Images', extensions: ['png', 'jpg', 'jpeg', 'webp', 'gif'] }],
    });
    if (r.canceled) return '';
    approvedPaths.add(r.filePaths[0]);
    return r.filePaths[0];
  });
  handle('dialog:zip', async () => {
    const r = await dialog.showOpenDialog(mainWindow, { properties: ['openFile'], filters: [{ name: 'Lynxx extension', extensions: ['zip'] }] });
    if (r.canceled) return '';
    approvedPaths.add(r.filePaths[0]);
    return r.filePaths[0];
  });
  handle('ext:installZip', async (_e, file, sha256) => {
    if (!approvedPaths.has(file)) throw new Error('choose the archive with the file dialog first');
    if (!RE_SHA256.test(String(sha256 || '').trim())) throw new Error('sha256 must be 64 hex characters');
    const ok = await confirmMain(`Cài extension từ ${path.basename(file)}?`,
      'Extension là code Python sẽ chạy trên máy bạn với quyền của bạn. Chỉ cài từ nguồn bạn tin cậy.');
    if (!ok) throw new Error('cancelled');
    return installExtension({ archivePath: file, sha256: String(sha256).trim() });
  });

  // extension generator: drafts only; installing one is its own confirmed action (Generator.install)
  handle('gen:status', () => generator.status());
  handle('gen:start', (_e, opts) => generator.start(opts));
  handle('gen:cancel', () => generator.cancel());
  handle('gen:list', () => listGenDrafts());
  handle('gen:get', (_e, session) => readGenReport(session));
  handle('gen:open', async (_e, session) => {
    const rep = await readGenReport(session);
    return shell.openPath(rep.draftDir || genSessionDir(session));
  });
  handle('gen:export', (_e, session) => generator.exportZip(session));
  handle('gen:install', (_e, session) => generator.install(session));
  handle('gen:remove', (_e, session) => generator.remove(session));
  handle('gen:setKey', (_e, value) => setAiKey(value));
  handle('gen:clearKey', () => clearAiKey());

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
    const core = coreCommand('server');
    return {
      version: app.getVersion(), electron: process.versions.electron, platform: process.platform, userData: userData(),
      python: core.cmd, core: core.frozen ? 'bundled' : 'python', packaged: app.isPackaged, repo: REPO_ROOT, sidecar: sidecar.info,
      encryption: encryptionAvailable, launcherInstalled: fs.existsSync(desktopEntryPath()),
    };
  });
  handle('launcher:install', () => installLauncher());
  handle('shell:open', (_e, p) => {
    // folders only (download destinations): files could be executables or .desktop launchers
    const target = String(p || '');
    if (!path.isAbsolute(target) || !fs.existsSync(target) || !fs.statSync(target).isDirectory()) return 'not a folder';
    return shell.openPath(target);
  });
  handle('shell:external', (_e, url) => (/^https?:\/\//.test(url) ? shell.openExternal(url) : false));
  // library page: a downloaded media file is opened with the default app; anything else (an executable, a .desktop file a
  // website could have named) is refused. 'reveal' only shows the file in its folder
  handle('library:open', (_e, p) => {
    const target = String(p || '');
    const ext = path.extname(target).slice(1).toLowerCase();
    if (!path.isAbsolute(target) || !LIBRARY_MEDIA_EXT.has(ext)) return 'chỉ mở được file ảnh / video / âm thanh';
    try { if (!fs.statSync(target).isFile()) return 'không phải file'; } catch { return 'file không còn trên đĩa'; }
    return shell.openPath(target);
  });
  handle('library:reveal', (_e, p) => {
    const target = String(p || '');
    if (!path.isAbsolute(target)) return false;
    if (fs.existsSync(target)) shell.showItemInFolder(target);
    else if (fs.existsSync(path.dirname(target)) && fs.statSync(path.dirname(target)).isDirectory()) shell.openPath(path.dirname(target));
    return true;
  });
}

// ---------- download index tools: clean (sidecar), library import (job), multi-site downloads (jobs) ----------

const MULTI_SITE_MAX = 8;
const approvedImportFolders = new Set(); // folders picked with the native dialog for a library import (renderer paths are refused)

// native confirmation of an index clean; the self-test replaces it
let confirmIndexClean = async () => {
  const r = await dialog.showMessageBox(mainWindow, {
    type: 'question', buttons: ['Huỷ', 'Dọn index'], defaultId: 1, cancelId: 0, noLink: true,
    message: 'Dọn index các lần tải?',
    detail: 'Xoá khỏi index những dòng có file không còn trên đĩa. Chỉ xoá dòng trong index (CSDL của Lynxx): không xoá, '
      + 'di chuyển hay đọc file nào. Có thể chạy trong lúc đang tải.\n\nDòng mà cả thư mục chứa file cũng không còn (ổ cứng ngoài '
      + 'chưa cắm, thư mục mạng chưa kết nối) được giữ lại, trừ khi bạn đánh dấu ô bên dưới. Hãy cắm ổ / kết nối thư mục trước khi dọn.',
    checkboxLabel: 'Xoá cả dòng có thư mục không còn', checkboxChecked: false,
  });
  return { ok: r.response === 1, missingFolders: r.checkboxChecked === true };
};

function registerIndexIpc() {
  // clean: rows only, after a native confirmation; a dry run (count only) needs none. The direct rpc methods are not allowlisted
  handle('index:clean', async (_e, opts) => {
    if (opts !== undefined && opts !== null && !isPlainObject(opts)) throw new Error('invalid options');
    if (opts?.dryRun === true) return sidecar.call('index.clean', { dryRun: true, missingFolders: opts.missingFolders === true });
    const answer = await confirmIndexClean();
    if (!answer.ok) throw new Error('cancelled');
    return sidecar.call('index.clean', { missingFolders: answer.missingFolders === true });
  });
  handle('index:cleanCancel', () => sidecar.call('index.cleanCancel', {}));
  handle('index:pickFolder', async () => {
    const r = await dialog.showOpenDialog(mainWindow, { title: 'Chọn thư mục thư viện để nhập vào index', properties: ['openDirectory'] });
    if (r.canceled || !r.filePaths[0]) return '';
    approvedImportFolders.add(r.filePaths[0]);
    return r.filePaths[0];
  });
  handle('index:import', (_e, opts) => jobs.startImport(opts));
  handle('jobs:startMulti', (_e, q, modules) => jobs.startMulti(isPlainObject(q) ? { ...q, autoKey: undefined, group: undefined, pick: undefined } : q, modules));
}

const LIBRARY_MEDIA_EXT = new Set(['jpg', 'jpeg', 'png', 'gif', 'webp', 'mp4', 'webm', 'mp3', 'ogg', 'oga', 'opus', 'flac', 'm4a', 'wav']);

// ---------- library: delete and the trash ("Đã tải" page; core: lynxx/dlindex.py trash / restore / purge_trash / forget) ----------
// Moving to the trash and back is reversible: no dialog. Purging files and forgetting rows cannot be undone: a native confirmation.
// The core refuses any path that is not a row of the index, so the renderer cannot name an arbitrary file

const LIBRARY_PATHS_MAX = 1000;
const TRASH_PURGE_EVERY_MS = 12 * 3600e3;
let trashPurgedAt = 0; // last automatic purge of the files older than settings.library.trashDays

function libraryPaths(paths) {
  if (!Array.isArray(paths) || !paths.length || paths.length > LIBRARY_PATHS_MAX) throw new Error('invalid paths');
  const out = [...new Set(paths)];
  if (out.some((p) => typeof p !== 'string' || !p || p.length > 4096 || p.includes('\0') || !path.isAbsolute(p))) throw new Error('invalid paths');
  return out;
}

const trashRoots = () => [resolveUserPath(settings.download.path)].filter((p) => path.isAbsolute(p));

function trashSizeText(bytes) {
  const b = Number(bytes) || 0;
  return b >= 1073741824 ? `${(b / 1073741824).toFixed(1)} GB` : b >= 1048576 ? `${(b / 1048576).toFixed(1)} MB` : `${Math.round(b / 1024)} KB`;
}

// native confirmation of what cannot be undone; the self-test replaces it
let confirmLibrary = async (message, detail, okLabel) => {
  const r = await dialog.showMessageBox(mainWindow, {
    type: 'warning', buttons: ['Huỷ', okLabel], defaultId: 0, cancelId: 0, noLink: true, message, detail,
  });
  return r.response === 1;
};

async function purgeOldTrash(reason) {
  // files that stayed in the trash longer than settings.library.trashDays are deleted for good. reason 'timer': at most every 12 h
  // (60 s after the core is up, then on the 5-minute tick); 'setting': the user shortened the time
  const days = settings.library.trashDays;
  if (!validTrashDays(days) || days === 0) return null;
  if (reason === 'timer' && (process.env.LYNXX_SELFTEST_DIR || Date.now() - trashPurgedAt < TRASH_PURGE_EVERY_MS)) return null;
  trashPurgedAt = Date.now();
  try {
    const r = await sidecar.call('index.purgeTrash', { days }, 600000);
    if (r.purged || r.failed?.length) {
      // purged counts rows (files already gone and files kept outside a trash folder too): the log says what was deleted
      const gone = Math.max(0, r.purged - r.files - (r.kept || 0));
      send('log', { source: 'library', text: `thùng rác: xoá vĩnh viễn ${r.files} file quá ${days} ngày (${trashSizeText(r.bytes)})`
        + `${gone ? `, bỏ ${gone} mục không còn file` : ''}${r.failed?.length ? `, ${r.failed.length} file không xoá được` : ''}` });
      send('sidecar:notify', { method: 'library.trashPurged', params: { ...r, days, auto: true } });
    }
    return r;
  } catch (err) {
    send('log', { source: 'library', text: `thùng rác: không tự dọn được: ${err.message}` });
    return null;
  }
}

function registerLibraryIpc() {
  handle('library:trash', (_e, paths) => sidecar.call('index.trash', { paths: libraryPaths(paths), roots: trashRoots() }, 600000));
  handle('library:restore', (_e, paths) => sidecar.call('index.restore', { paths: libraryPaths(paths) }, 600000));
  handle('library:forget', async (_e, paths) => {
    const list = libraryPaths(paths);
    const ok = await confirmLibrary(`Xoá ${list.length} mục khỏi thư viện?`,
      'File của các mục này không còn trên đĩa. Chỉ xoá dòng trong index của Lynxx, không thể hoàn tác.', 'Xoá khỏi thư viện');
    if (!ok) throw new Error('cancelled');
    return sidecar.call('index.forget', { paths: list });
  });
  handle('library:purge', async (_e, paths) => {
    const list = libraryPaths(paths);
    const ok = await confirmLibrary(`Xoá vĩnh viễn ${list.length} file?`,
      'Các file này bị xoá khỏi ổ đĩa và khỏi thư viện. Không thể hoàn tác.', 'Xoá vĩnh viễn');
    if (!ok) throw new Error('cancelled');
    return sidecar.call('index.purgeTrash', { paths: list }, 600000);
  });
  handle('library:emptyTrash', async () => {
    const st = await sidecar.call('index.stats', {});
    const n = st.trash?.files || 0;
    if (!n) return { purged: 0, files: 0, bytes: 0, failed: [], kept: 0 };
    const ok = await confirmLibrary('Dọn sạch thùng rác?',
      `${n} file (${trashSizeText(st.trash.bytes)}) bị xoá khỏi ổ đĩa và khỏi thư viện. Không thể hoàn tác.`, 'Dọn sạch');
    if (!ok) throw new Error('cancelled');
    return sidecar.call('index.purgeTrash', { all: true }, 600000);
  });
}

function secretsStatus() {
  return {
    encryption: encryptionAvailable,
    modules: Object.fromEntries(Object.entries(secrets).filter(([k]) => k !== AI_SECRET && k !== MODEL_SECRET)
      .map(([k, v]) => [k, { set: true, encrypted: Boolean(v.enc) }])),
    ai: aiKeyStatus(),
    genderModel: modelTokenStatus(),
  };
}

// ---------- quitting ----------

let quitApproved = false; // the user confirmed (or nothing needed confirming): before-quit / window close pass through
let quitFlow = null;
// native confirmation, replaced by the self-test
let askQuit = (message, detail, okLabel) => confirmMain(message, detail, ['Ở lại', okLabel]);

function jobsNeedingConfirm() {
  return jobs.active().filter((j) => j.status === 'queued' || j.status === 'running');
}

async function confirmQuit(relaunch) {
  const pending = jobsNeedingConfirm();
  const running = pending.filter((j) => j.status === 'running').length;
  const parts = [running && `${running} job đang tải`, pending.length - running && `${pending.length - running} job đang chờ`].filter(Boolean);
  return askQuit(relaunch ? 'Khởi động lại Lynxx?' : 'Thoát Lynxx?',
    `Đang có ${parts.join(' và ')}. Nếu tiếp tục, các job này sẽ bị huỷ.`,
    relaunch ? 'Huỷ job và khởi động lại' : 'Huỷ job và thoát');
}

function requestQuit(relaunch = false) {
  // quit / relaunch with download jobs queued or running: ask first, then cancel them gracefully before exiting
  if (!quitFlow) {
    quitFlow = (async () => {
      try {
        if (!quitApproved && jobsNeedingConfirm().length && !(await confirmQuit(relaunch))) return false;
        quitApproved = true;
        if (jobs.active().length) await jobs.shutdown();
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
    title: 'Lynxx', icon: iconPath(settings.appearance.icon), backgroundColor: '#0d0b0a', show: false,
    autoHideMenuBar: true,
    webPreferences: { preload: path.join(__dirname, 'preload.js'), contextIsolation: true, nodeIntegration: false, sandbox: true },
  });
  mainWindow.loadFile(path.join(__dirname, 'renderer', 'index.html'));
  mainWindow.once('ready-to-show', () => mainWindow.show());
  mainWindow.webContents.once('did-finish-load', () => flushStartupNotices());
  mainWindow.on('close', (e) => {
    if (!quitApproved && jobs.active().length) {
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
  // dropping a file/link or any script navigation must never replace the app page (preload would expose window.lynxx)
  mainWindow.webContents.on('will-navigate', (e, url) => { if (url !== RENDERER_URL) e.preventDefault(); });
  mainWindow.webContents.on('will-attach-webview', (e) => e.preventDefault());
  mainWindow.webContents.session.setPermissionRequestHandler((_wc, _perm, cb) => cb(false));
  if (process.argv.includes('--lynxx-devtools')) mainWindow.webContents.openDevTools({ mode: 'detach' });
}

// ---------- self test (dev only: LYNXX_SELFTEST_DIR=<dir> LYNXX_USER_DATA=<scratch folder> electron .) ----------
// Without LYNXX_USER_DATA the self-test runs on the real profile: the steps that would delete files of the user's trash skip

// the trash steps empty the trash of the profile they run on: a scratch profile only (LYNXX_USER_DATA)
const SELFTEST_REAL_PROFILE = 'the real profile: these steps empty its trash (set LYNXX_USER_DATA to a scratch folder)';
const selfTestOnScratchProfile = () => Boolean(process.env.LYNXX_USER_DATA);

async function runSelfTest(dir) {
  const wait = (ms) => new Promise((r) => setTimeout(r, ms));
  const shot = async (name) => {
    const img = await mainWindow.webContents.capturePage();
    await fsp.mkdir(dir, { recursive: true });
    await fsp.writeFile(path.join(dir, `${name}.png`), img.toPNG());
  };
  const js = (code) => mainWindow.webContents.executeJavaScript(code, true);
  const quick = Boolean(process.env.LYNXX_SELFTEST_QUICK); // no network steps: contract, settings and quit checks only
  const report = { settingsLoad: settingsLoadInfo, core: coreCommand('server') };
  try {
    await sidecar.ready;
    await wait(1500);
    if (!quick) { // UI walkthrough (needs network to danbooru)
      await shot('01-search-empty');
      report.preview = await js(`(async () => {
        document.querySelector('#module-btn').click();
        [...document.querySelectorAll('#module-menu button')].find((b) => b.textContent.includes('Danbooru'))?.click();
        const inp = document.querySelector('#tag-text');
        for (const t of ${JSON.stringify((process.env.LYNXX_SELFTEST_TAGS || 'scenery sky cloud rating:g').split(' '))}) {
          inp.value = t + ' '; inp.dispatchEvent(new Event('input', { bubbles: true }));
        }
        inp.value = 'sun'; inp.dispatchEvent(new Event('input', { bubbles: true }));
        // first use loads the module tag list in the sidecar (~1 s for danbooru's 766k tags)
        for (let i = 0; i < 20 && !document.querySelector('#ac .ac-item'); i++) await new Promise((r) => setTimeout(r, 250));
        const ac = [...document.querySelectorAll('#ac .ac-item .nm')].map((e) => e.textContent);
        return { ac, query: document.querySelector('#query-text').textContent };
      })()`);
      await shot('02-autocomplete');
      report.previewResult = await js(`(async () => {
        const inp = document.querySelector('#tag-text'); inp.value = ''; inp.dispatchEvent(new Event('input', { bubbles: true }));
        document.querySelector('#btn-count').click();
        document.querySelector('#btn-preview').click();
        for (let i = 0; i < 60 && document.querySelector('#btn-preview').disabled !== false; i++) await new Promise((r) => setTimeout(r, 500));
        await new Promise((r) => setTimeout(r, 1500));
        for (let i = 0; i < 40 && document.querySelector('#btn-count').textContent !== 'Đếm'; i++) await new Promise((r) => setTimeout(r, 500));
        return { info: document.querySelector('#results-info').textContent, cards: document.querySelectorAll('#grid .card').length,
          status: document.querySelector('#query-status').textContent };
      })()`);
      await wait(4000);
      await shot('03-preview');
      // search by file name / post link: dialog -> files.identify -> query -> preview
      report.filesSearch = await js(`(async () => {
        document.querySelector('#btn-files').click();
        const area = document.querySelector('.files-input');
        area.value = 'db_5000.jpg\\nhttps://danbooru.donmai.us/posts/5001\\nIMG_1.jpg';
        area.dispatchEvent(new Event('input', { bubbles: true }));
        for (let i = 0; i < 20 && !document.querySelector('.files-queries .btn'); i++) await new Promise((r) => setTimeout(r, 250));
        return { items: [...document.querySelectorAll('.files-item')].map((e) => e.textContent) };
      })()`);
      await shot('03a-files-dialog');
      report.filesSearch = { ...report.filesSearch, ...await js(`(async () => {
        const btn = document.querySelector('.files-queries .btn');
        const label = btn ? btn.textContent : null;
        if (btn) btn.click();
        await new Promise((r) => setTimeout(r, 300));
        for (let i = 0; i < 60 && document.querySelector('#btn-preview').disabled !== false; i++) await new Promise((r) => setTimeout(r, 500));
        await new Promise((r) => setTimeout(r, 1500));
        return { label, query: document.querySelector('#query-text').textContent, cards: document.querySelectorAll('#grid .card').length };
      })()`) };
      await wait(1500);
      await shot('03b-files-search');
      await js(`document.querySelector('#btn-az').click()`);
      await wait(2500);
      await shot('04-az-drawer');
      await js(`document.querySelector('#az-close').click(); document.querySelector('[data-page="extensions"]').click()`);
      await wait(1500);
      await shot('05-extensions');
      await js(`document.querySelector('[data-page="settings"]').click()`);
      await wait(800);
      await shot('06-settings');
      await js(`document.querySelector('.theme-sw:nth-child(3)').click()`);
      await wait(800);
      await shot('07-settings-paper');
      await js(`document.querySelector('.theme-sw:nth-child(1)').click(); document.querySelector('[data-page="tags"]').click()`);
      await wait(2500);
      await shot('08-tags');
      if (process.env.LYNXX_SELFTEST_DOWNLOAD) {
        report.download = await js(`(async () => {
          const job = await window.lynxx.jobs.start({ module: 'db', tags: ${JSON.stringify(process.env.LYNXX_SELFTEST_DOWNLOAD)},
            path: ${JSON.stringify(path.join(dir, 'dl'))}, options: { limit: 2, threads: 1 } });
          document.querySelector('[data-page="downloads"]').click();
          for (let i = 0; i < 120; i++) {
            const j = (await window.lynxx.jobs.list()).find((x) => x.id === job.id);
            if (j && ['done', 'failed', 'cancelled'].includes(j.status)) return { status: j.status, progress: j.progress, exit: j.exitCode };
            await new Promise((r) => setTimeout(r, 500));
          }
          return { status: 'timeout' };
        })()`);
        // batch B: a module that is not installed must fail with a reason, not hang the queue
        report.badModuleJob = await js(`(async () => {
          const job = await window.lynxx.jobs.start({ module: 'rs', tags: 'scenery -webm', path: ${JSON.stringify(path.join(dir, 'dl2'))} });
          for (let i = 0; i < 60; i++) {
            const j = (await window.lynxx.jobs.list()).find((x) => x.id === job.id);
            if (j && ['done', 'failed', 'cancelled'].includes(j.status)) return { status: j.status, exit: j.exitCode, error: j.error };
            await new Promise((r) => setTimeout(r, 500));
          }
          return { status: 'timeout' };
        })()`);
        await wait(800);
        await shot('09-downloads');
      }
    }
    report.coreStatus = await js('window.lynxx.sidecarStatus()');
    report.contract = await selfTestContract(js, quick);
    if (process.env.LYNXX_SELFTEST_SECURITY) {
      report.security = await js(`(async () => {
        const jobs = await window.lynxx.jobs.list();
        const loadImg = (src) => new Promise((resolve) => {
          const img = new Image();
          img.onload = () => resolve('loaded');
          img.onerror = () => resolve('blocked');
          img.src = src;
        });
        let bypass = 'denied';
        try { await window.lynxx.ext.installZip('/etc/passwd', '0'.repeat(64)); bypass = 'ALLOWED'; } catch (e) { bypass = 'denied: ' + e.message; }
        let shellOpen = '';
        try { shellOpen = await window.lynxx.app.openPath('/etc/passwd'); } catch (e) { shellOpen = 'err: ' + e.message; }
        return {
          jobKeys: jobs[0] ? Object.keys(jobs[0]).sort() : [],
          thumbLoopback: await loadImg(window.lynxx.thumbUrl('http://127.0.0.1:9/x.jpg', '')),
          thumbMetadata: await loadImg(window.lynxx.thumbUrl('http://169.254.169.254/latest/meta-data', '')),
          zipWithoutDialog: bypass,
          openFile: shellOpen,
        };
      })()`);
      const before = mainWindow.webContents.getURL();
      await js(`location.href = 'https://example.com/'`).catch(() => {});
      await wait(1500);
      report.security.navigationBlocked = mainWindow.webContents.getURL() === before;
      report.security.stillHasBridge = await js('typeof window.lynxx === "object"');
    }
    report.quitGuard = await selfTestQuitGuard(dir);
    report.generator = await selfTestGenerator(js);
    report.indexTools = await selfTestIndexTools(js, shot);
    if (process.env.LYNXX_SELFTEST_ICONS) {
      // writes ~/.local/share/applications/lynxx.desktop: run it with a scratch HOME only
      const read = () => fs.readFileSync(desktopEntryPath(), 'utf8');
      const iconLine = () => (read().match(/^Icon=(.*)$/m) || [])[1] || '';
      const setIcon = (id) => js(`window.lynxx.settings.set({ appearance: { icon: '${id}' } }).then((r) => r.iconSync)`);
      const r = { home: os.homedir() };
      try { fs.unlinkSync(desktopEntryPath()); } catch { /* none */ }
      r.missing = await setIcon('exposed-jaw-orange');
      r.install = await js('window.lynxx.app.installLauncher()');
      const firstIcon = iconLine();
      r.changed = await setIcon('full-mask-monochrome');
      const secondIcon = iconLine();
      r.unchanged = await setIcon('full-mask-monochrome');
      Object.assign(r, {
        firstIcon: path.basename(firstIcon), secondIcon: path.basename(secondIcon), iconFiles: fs.readdirSync(outsideIconDir()),
        oldRemoved: !fs.existsSync(firstIcon), newExists: fs.existsSync(secondIcon), marker: read().includes(LAUNCHER_MARKER),
        wmClass: /^StartupWMClass=lynxx$/m.test(read()),
      });
      fs.writeFileSync(desktopEntryPath(), '[Desktop Entry]\nName=Other\nIcon=x\n');
      r.foreign = await setIcon('exposed-jaw-orange');
      r.foreignUntouched = read().includes('Name=Other');
      fs.unlinkSync(desktopEntryPath());
      await setIcon(DEFAULT_SETTINGS.appearance.icon);
      r.ok = r.missing?.launcher === 'missing' && r.install?.ok === true && r.changed?.launcher === 'updated'
        && r.unchanged?.launcher === 'unchanged' && r.oldRemoved && r.newExists && r.marker && r.wmClass
        && r.foreign?.launcher === 'foreign' && r.foreignUntouched;
      report.iconSync = r;
    }
    report.sidecar = sidecar.info;
  } catch (err) {
    report.error = String(err && err.stack || err);
  }
  await fsp.writeFile(path.join(dir, 'report.json'), JSON.stringify(report, null, 2));
  quitApproved = true; // a job left over by a failed step must not open the quit confirmation
  await jobs.shutdown(3000);
  app.quit();
}

async function selfTestContract(js, quick) {
  // batch D wiring: allowlisted sidecar methods, related / pageSize, secrets, settings validation, headers, tag limits, locale
  const out = await js(`(async () => {
    const r = {};
    const t = async (name, fn) => {
      try { r[name] = { ok: true, sample: JSON.stringify(await fn()).slice(0, 400) }; } catch (e) { r[name] = { ok: false, error: String(e.message || e).slice(0, 300) }; }
    };
    await t('resolve', () => window.lynxx.rpc('tags.resolve', { module: 'db', tags: ['scenery', 'sky', 'skyy', 'rating:g'] }));
    await t('fuzzy', () => window.lynxx.rpc('tags.fuzzy', { module: 'db', text: 'scenrey', limit: 5 }));
    await t('notAllowed', () => window.lynxx.rpc('ext.install', { id: 'x' }));
    await t('indexStats', () => window.lynxx.rpc('index.stats'));
    await t('indexSearch', () => window.lynxx.rpc('index.search', { tags: 'scenery', pageSize: 5 }));
    if (!${quick}) {
      await t('related', () => window.lynxx.search.related({ module: 'db', tags: 'scenery rating:g', limit: 10 }));
      await t('previewPageSize', async () => {
        const p = await window.lynxx.search.preview({ module: 'db', tags: 'scenery rating:g', pageSize: 5 }, 0);
        return { items: (p.items || []).length, pageSize: p.pageSize, hasMore: p.hasMore };
      });
    }
    let s = await window.lynxx.secrets.set('zz', 'selftest-key');
    const setOk = Boolean(s.modules.zz);
    s = await window.lynxx.secrets.set('zz', '   ');
    const emptyKeeps = Boolean(s.modules.zz);
    s = await window.lynxx.secrets.clear('zz');
    r.secrets = { setOk, emptyKeeps, cleared: !s.modules.zz, ok: setOk && emptyKeeps && !s.modules.zz };
    const prev = (await window.lynxx.settings.get()).settings.search;
    const history = [{ module: 'db', tags: 'a b', at: 2 }, { module: 'db', tags: 'a b', at: 1 }, { module: 'DB!', tags: 'x', at: 1 },
      { module: 'db', tags: 'x'.repeat(2001), at: 1 }, 'junk', { module: 'rx', tags: 'c', at: 3 }, { module: 'rx', tags: 'd', at: 'x' }];
    const saved = Array.from({ length: 205 }, (_, i) => ({ name: 'n'.repeat(100), module: 'db', tags: 't' + i, at: i }));
    const v = (await window.lynxx.settings.set({ search: { history, saved, tagLimits: { db: 6, rx: 'x', 'bad key': 3 } } })).settings.search;
    r.settingsValidation = { history: v.history.length, saved: v.saved.length, nameLen: v.saved[0].name.length, tagLimits: v.tagLimits };
    r.settingsValidation.ok = v.history.length === 2 && v.saved.length === 200 && v.saved[0].name.length === 80 && v.tagLimits.db === 6 && !('rx' in v.tagLimits);
    await window.lynxx.settings.set({ search: { history: prev.history || [], saved: prev.saved || [], tagLimits: { db: (prev.tagLimits || {}).db || 0 } } });
    r.headersSave = await window.lynxx.settings.set({ connection: { headers: ${JSON.stringify({ 'X-Test': 'a"b\\c' })}, cookies: ${JSON.stringify({ sid: '"q\\z"' })} } })
      .then(() => 'saved', (e) => e.message);
    r.badHeaderName = await window.lynxx.settings.set({ connection: { headers: { 'bad name': 'v' } } }).then(() => 'ACCEPTED', () => 'rejected');
    r.siteHttpSave = await window.lynxx.settings.set({ connection: { siteHeaders: { zz: { 'user-agent': 'UA-site' } }, siteCookies: { zz: { cf_clearance: 'cf1' } } } })
      .then(() => 'saved', (e) => e.message);
    r.badSiteKey = await window.lynxx.settings.set({ connection: { siteCookies: { 'RN!': { a: 'b' } } } }).then(() => 'ACCEPTED', () => 'rejected');
    // a higher limit the site gave the user's key (Settings > API keys): main restores the previous value after checking the file
    const prevKeyLimits = (await window.lynxx.settings.get()).settings.connection.keyRateLimits || {};
    const kv = (await window.lynxx.settings.set({ connection: { keyRateLimits: { rx: 120, 'bad key': 3, xb: -1, bb: 'x' } } })).settings.connection.keyRateLimits;
    r.keyRateLimits = { saved: kv, prev: prevKeyLimits, ok: kv.rx === 120 && !('bad key' in kv) && kv.xb !== -1 && kv.bb !== 'x' };
    r.lang = { navigator: navigator.language, dateInput: (() => { const d = document.createElement('input'); d.type = 'date'; return d.type; })() };
    return r;
  })()`);
  // main side: what the core receives (secrets go through LYNXX_SECRET_ARGS, see Jobs._spawn)
  const args = await connectionArgs('zz');
  out.httpArgs = args.filter((_a, i) => args[i - 1] === '-header' || args[i - 1] === '-cookie');
  out.httpArgsOk = out.httpArgs.includes('X-Test=a"b\\c') && out.httpArgs.includes('sid="q\\z"');
  {
    // per-website maps: the website of the run gets its entries over the shared ones of the same name (any letter case),
    // no other website sees them; a library import passes them as that module's arguments
    settings.connection.headers = { ...settings.connection.headers, 'User-Agent': 'UA-shared' };
    const http = (a) => a.filter((_x, i) => a[i - 1] === '-header' || a[i - 1] === '-cookie');
    const own = http(await connectionArgs('zz'));
    const other = http(await connectionArgs('yy'));
    out.siteHttp = { saved: out.siteHttpSave, badKey: out.badSiteKey, own, other, importArgs: siteHttpArgs('zz') };
    out.siteHttp.ok = out.siteHttpSave === 'saved' && out.badSiteKey === 'rejected' && own.includes('user-agent=UA-site')
      && !own.includes('User-Agent=UA-shared') && own.includes('cf_clearance=cf1') && own.includes('X-Test=a"b\\c')
      && other.includes('User-Agent=UA-shared') && !other.some((x) => /UA-site|cf_clearance/.test(x))
      && out.siteHttp.importArgs.join(' ') === '-header user-agent=UA-site -cookie cf_clearance=cf1';
    settings.connection.siteHeaders = {};
    settings.connection.siteCookies = {};
  }
  settings.connection.headers = {};
  settings.connection.cookies = {};
  const limits = settings.search.tagLimits;
  settings.search.tagLimits = { db: 6 };
  out.tagLimitArgs = tagLimitArgs('db');
  settings.search.tagLimits = limits;
  {
    // key limits: written where every core process reads them (lynxx.network.key_rate_limit), then the user's value is back
    let written = null;
    try { written = JSON.parse(fs.readFileSync(keyRateLimitsFile(), 'utf8')); } catch { /* checked below */ }
    settings.connection.keyRateLimits = sanitizeKeyRateLimits(out.keyRateLimits?.prev) || {};
    saveSettings();
    syncKeyRateLimits();
    out.keyRateLimitsFile = { written, ok: written?.rx === 120 };
  }
  // download index: '-skip_known' only when the option is on (off by default, old behavior unchanged)
  const hasSkip = (options) => downloadArgs({ module: 'db', tags: 'x', options }).args.includes('-skip_known');
  out.skipKnownArgs = { on: hasSkip({ skipKnown: true }), off: hasSkip({ skipKnown: false }), default: hasSkip({}) };
  out.skipKnownArgs.ok = out.skipKnownArgs.on && !out.skipKnownArgs.off && out.skipKnownArgs.default === Boolean(settings.download.skipKnown);
  // scheduled tag update: the command line only (a self-test never writes the user's crontab / tasks)
  {
    const cmd = tagScheduleCommand();
    out.tagSchedule = {
      cmd, when: tagScheduleWhen('weekly').cron,
      ok: (cmd.includes("'tags' '--modules' 'auto'") || cmd.includes("'lynxx.tags_updater' '--modules' 'auto'"))
        && cmd.includes(shq(path.join(userData(), 'tags'))) && cmd.includes(shq(tagScheduleLog())) && !/api.?key/i.test(cmd)
        && /^\d+ 4 \* \* 1$/.test(tagScheduleWhen('weekly').cron) && /^\d+ 4 \* \* \*$/.test(tagScheduleWhen('daily').cron),
    };
  }
  // audio: '-skip_aud' only when the audio option is off (default on)
  const hasSkipAud = (options) => downloadArgs({ module: 'db', tags: 'x', options }).args.includes('-skip_aud');
  out.audioArgs = { off: hasSkipAud({ audio: false }), on: hasSkipAud({ audio: true }), default: hasSkipAud({}) };
  out.audioArgs.ok = out.audioArgs.off && !out.audioArgs.on && !out.audioArgs.default;
  // gender: 'keep' picks -> -gender, 'exclude' picks -> -gender_exclude, excluding every label is no filter
  const ga = (g) => genderArgs(g).join(' ');
  out.genderArgs = {
    keep: ga({ select: ['only_female', 'both'], op: 'keep' }), exclude: ga({ select: ['only_male'], op: 'exclude', conf: 'high' }),
    all: ga({ select: ['only_female', 'only_male', 'both', 'none', 'unknown'], op: 'exclude' }),
  };
  // the image model only when it is turned on: off, a 'model' query filters by tags
  const wasOn = settings.genderModel.enabled;
  settings.genderModel.enabled = true;
  out.genderArgs.model = ga({ select: ['only_male'], op: 'exclude', mode: 'model' });
  settings.genderModel.enabled = false;
  out.genderArgs.modelOff = ga({ select: ['only_male'], op: 'exclude', mode: 'model' });
  // -gender_verify_media: only with the model turned on and 'Tag + model' picked
  out.genderArgs.verifyOff = ga({ select: ['only_female'], mode: 'model', verifyMedia: true });
  settings.genderModel.enabled = true;
  out.genderArgs.verify = ga({ select: ['only_female'], mode: 'model', verifyMedia: true });
  out.genderArgs.verifyTags = ga({ select: ['only_female'], mode: 'tags', verifyMedia: true });
  out.genderArgs.verifyNo = ga({ select: ['only_female'], mode: 'model', verifyMedia: false });
  settings.genderModel.enabled = wasOn;
  out.genderArgs.ok = out.genderArgs.keep === '-gender only_female,both' && out.genderArgs.exclude === '-gender_exclude only_male -gender_conf high'
    && out.genderArgs.all === '' && out.genderArgs.model === `-gender_exclude only_male -gender_mode model -gender_model_url ${settings.genderModel.url}`
    && out.genderArgs.modelOff === '-gender_exclude only_male'
    && out.genderArgs.verify === `-gender only_female -gender_mode model -gender_model_url ${settings.genderModel.url} -gender_verify_media`
    && out.genderArgs.verifyOff === '-gender only_female' && out.genderArgs.verifyTags === '-gender only_female'
    && out.genderArgs.verifyNo === `-gender only_female -gender_mode model -gender_model_url ${settings.genderModel.url}`;
  // safety blacklist: its own option; the gender filter never implies it
  out.safetyArgs = {
    on: safetyArgs({ safetyBlacklist: true }).join(' '), off: safetyArgs({ safetyBlacklist: false }).join(' '), missing: safetyArgs({}).join(' '),
    genderOnly: [...genderArgs({ select: ['only_female'] }), ...safetyArgs({ gender: { select: ['only_female'] } })].join(' '),
  };
  out.safetyArgs.ok = out.safetyArgs.on === '-safety_blacklist' && out.safetyArgs.off === '' && out.safetyArgs.missing === ''
    && out.safetyArgs.genderOnly === '-gender only_female' && settings.download.safetyBlacklist !== undefined;
  // the media check box: hidden while 'Tag + model' cannot be picked, disabled with 'Chỉ dựa vào tag', saved in download.gender
  try {
    const savedGender = structuredClone(settings.download.gender);
    const ui = await js(`(() => {
      const was = { on: S.settings.genderModel.enabled, g: { ...genderState() } };
      const read = () => ({ hidden: $('#f-gender-verify-wrap').hidden, disabled: $('#f-gender-verify').disabled, checked: $('#f-gender-verify').checked });
      S.settings.genderModel.enabled = false; S.settings.download.gender = { ...was.g, mode: 'tags', verifyMedia: false }; renderGenderFilter();
      const off = read();
      S.settings.genderModel.enabled = true; renderGenderFilter();
      const tags = read();
      S.settings.download.gender = { ...was.g, mode: 'model', verifyMedia: false }; renderGenderFilter();
      const box = $('#f-gender-verify');
      box.checked = true;
      box.dispatchEvent(new Event('change'));
      const model = read();
      S.settings.genderModel.enabled = was.on; renderGenderFilter();
      return { off, tags, model };
    })()`);
    await new Promise((r) => setTimeout(r, 300)); // the renderer saves through settings:set
    ui.saved = settings.download.gender.verifyMedia === true && settings.download.gender.mode === 'model';
    settings.download.gender = savedGender;
    saveSettings();
    await js(`(() => { S.settings.download.gender = ${JSON.stringify(savedGender)}; renderGenderFilter(); })()`);
    out.genderVerifyUi = { ...ui, ok: ui.off.hidden && !ui.tags.hidden && ui.tags.disabled && !ui.model.hidden && !ui.model.disabled
      && ui.model.checked && ui.saved };
  } catch (e) {
    out.genderVerifyUi = { ok: false, error: String(e.message || e) };
  }
  // gender model server: status through main, the token never reaches the renderer, the core gets it as a secret argument
  try {
    const fakeToken = 'selftest-model-token-0123';
    const st = await js('window.lynxx.genderModel.status()');
    const tok = await js(`window.lynxx.genderModel.setToken(${JSON.stringify(fakeToken)})`);
    const view = JSON.stringify(await js('window.lynxx.settings.get()'));
    const wasOn = settings.genderModel.enabled;
    settings.genderModel.enabled = false;
    const secretOff = (await genderSecretArgs({ mode: 'model' })).length === 0; // model off: the token is not handed out
    settings.genderModel.enabled = true;
    const secretArgs = (await genderSecretArgs({ mode: 'model' })).join(' ');
    settings.genderModel.enabled = wasOn;
    const blocked = async (m) => js(`window.lynxx.rpc(${JSON.stringify(m)}, {url: 'http://127.0.0.1:1'}).then(() => 'allowed', (e) => 'blocked')`);
    const rpcBlocked = [await blocked('gender.modelStatus'), await blocked('gender.modelLoad'), await blocked('gender.modelUnload')];
    const cleared = await js('window.lynxx.genderModel.clearToken()');
    // unload through main -> sidecar -> server (a no-op when nothing is loaded; load is the same path, not run: ~550 MB)
    const un = await js('window.lynxx.genderModel.unload()');
    out.genderModel = {
      url: st.url, reachable: st.reachable, error: st.error, check: st.check?.code, unload: un.reachable ? un.model?.loaded === false : un.error,
      tokenSet: tok.set, rpcBlocked, cleared: !cleared.set,
      rendererHasNoToken: !view.includes(fakeToken), secretArgs: secretArgs === `-gender_model_token ${fakeToken}` && secretOff,
    };
    // a reachable server reports its hardware: the renderer warns before turning the model on without a GPU
    out.genderModel.ok = typeof st.reachable === 'boolean' && st.url === settings.genderModel.url && tok.set
      && rpcBlocked.every((x) => x === 'blocked') && (!st.reachable || (typeof st.check?.code === 'string' && un.model?.loaded === false))
      && out.genderModel.cleared && out.genderModel.rendererHasNoToken && out.genderModel.secretArgs;
  } catch (e) {
    out.genderModel = { ok: false, error: String(e.message || e) };
  }
  // library page: the renderer (file:// page) shows a downloaded image from disk; 'open' refuses non media files
  try {
    const dir = fs.mkdtempSync(path.join(os.tmpdir(), 'lynxx-selftest-lib-'));
    const img = path.join(dir, 'db_1 test.png');
    fs.copyFileSync(path.join(__dirname, 'assets', 'icons', 'full-mask-orange-64.png'), img);
    const launcher = path.join(dir, 'evil.desktop');
    fs.writeFileSync(launcher, '[Desktop Entry]\n');
    const loaded = await js(`new Promise((resolve) => { const i = new Image(); i.onload = () => resolve(i.naturalWidth); i.onerror = () => resolve(0);
      i.src = fileUrl(${JSON.stringify(img)}); })`);
    const refused = await js(`window.lynxx.library.open(${JSON.stringify(launcher)})`);
    out.libraryFiles = { loaded, refused, ok: loaded === 64 && /chỉ mở được/.test(String(refused)) };
    fs.rmSync(dir, { recursive: true, force: true });
  } catch (e) {
    out.libraryFiles = { ok: false, error: String(e.message || e) };
  }
  // saved query schedule: set from the renderer, run fields owned by main survive a stale whole-list write (no run: self-test)
  try {
    const entry = { name: 'selftest-auto', module: 'db', tags: 'selftest_auto', at: Date.now() };
    const keep = settings.search.saved;
    settings.search.saved = [entry];
    const key = `${entry.module}\n${entry.tags}`;
    let r = await js(`window.lynxx.saved.setAuto(${JSON.stringify(key)}, 24, 'all').then((x) => x.settings.search.saved, (e) => 'ERR ' + e.message)`);
    const on = Array.isArray(r) && r[0]?.auto?.every === 24;
    settings.search.saved[0].auto.lastRun = 123456;
    settings.search.saved[0].auto.maxId = 777;
    r = await js(`window.lynxx.settings.set({ search: { saved: [{ name: 'selftest-auto', module: 'db', tags: 'selftest_auto', at: 1, auto: { every: 72 } }] } }).then((x) => x.settings.search.saved)`);
    const merged = r[0]?.auto?.every === 72 && r[0]?.auto?.lastRun === 123456 && r[0]?.auto?.maxId === 777;
    r = await js(`window.lynxx.saved.setAuto(${JSON.stringify(key)}, 0, 'all').then((x) => x.settings.search.saved)`);
    const off = !r[0]?.auto;
    const bad = await js(`window.lynxx.saved.setAuto(${JSON.stringify(key)}, 5, 'all').then(() => 'SET', (e) => e.message)`);
    out.savedAuto = { on, merged, off, bad, ok: on && merged && off && /chu kỳ/.test(bad) };
    settings.search.saved = keep;
  } catch (e) {
    out.savedAuto = { ok: false, error: String(e.message || e) };
  }
  // identical active job refused unless asked on purpose, 'run again' reuses the query (nothing is spawned: pump is off)
  const realPump = jobs.pump;
  const made = [];
  try {
    jobs.pump = () => {};
    const q = { module: 'db', tags: 'selftest_duplicate', path: path.join(os.tmpdir(), 'lynxx-selftest-dup') };
    const first = await jobs.start(q);
    made.push(first.id);
    const dup = await jobs.start(q).then(() => 'ADDED', (e) => e.message);
    const forced = await jobs.start({ ...q, allowDuplicate: true });
    made.push(forced.id);
    const rerunDup = await jobs.rerun(first.id).then(() => 'ADDED', (e) => e.message);
    made.forEach((id) => jobs.cancel(id));
    const again = await jobs.rerun(first.id);
    made.push(again.id);
    out.duplicateJobs = { dup, forced: forced.status, rerunDup, rerun: again.tags === q.tags && again.status, hidden: !('query' in again) };
    out.duplicateJobs.ok = /^DUPLICATE/.test(dup) && forced.status === 'queued' && /^DUPLICATE/.test(rerunDup)
      && again.status === 'queued' && out.duplicateJobs.hidden;
  } catch (e) {
    out.duplicateJobs = { ok: false, error: String(e.message || e) };
  } finally {
    made.forEach((id) => { jobs.cancel(id); jobs.jobs.delete(id); });
    jobs.pump = realPump;
  }
  out.selectedDownload = await selfTestSelectedDownload(js);
  out.searchKinds = await selfTestSearchKinds(js);
  out.upstream = await selfTestUpstream();
  out.quality = await selfTestQuality(js);
  out.siteVerify = await selfTestSiteVerify(js);
  // posts without a preview address, or whose preview does not load (here a loopback address, refused by lynxx-thumb without
  // a request): a placeholder tile instead of the broken image icon, and the card still selects. The grid's images load lazily,
  // and a window the system reports hidden or occluded (the Windows and macOS CI runners) never lays them out in view: the test
  // asks for them now (loading = eager), the error path is the app's own
  try {
    out.thumbPlaceholder = await js(`(async () => {
      const pause = (ms) => new Promise((r) => setTimeout(r, ms));
      document.querySelector('[data-page="search"]').click();
      Object.assign(S.preview, { module: 'db', tags: 'selftest_thumbs', site: '', page: 0, hasMore: false, dateFilter: 'native',
        items: [{ id: '1', excludedBy: [] }, { id: '2', isVideo: true, mediaKind: 'video', excludedBy: [] },
          { id: '3', preview: 'http://127.0.0.1:9/x.jpg', excludedBy: [] }] });
      S.preview.key = previewKey();
      S.preview.selected.clear();
      renderGrid();
      document.querySelectorAll('#grid img').forEach((img) => { img.loading = 'eager'; });
      for (let i = 0; i < 50 && document.querySelectorAll('#grid .thumb-ph').length < 3; i++) await pause(100);
      const tiles = [...document.querySelectorAll('#grid .thumb-ph')].map((e) => e.dataset.kind + ':' + e.textContent);
      const first = document.querySelector('#grid .card');
      first.click();
      const r = { tiles, selected: S.preview.selected.has('1') && first.classList.contains('sel'), images: document.querySelectorAll('#grid img').length };
      S.preview.selected.clear();
      clearPreview();
      return r;
    })()`);
    out.thumbPlaceholder.ok = out.thumbPlaceholder.tiles.join('|')
      === 'image:không có ảnh xem trước|video:không có ảnh xem trước|image:không tải được ảnh xem trước'
      && out.thumbPlaceholder.selected === true && out.thumbPlaceholder.images === 0;
  } catch (e) {
    out.thumbPlaceholder = { ok: false, error: String(e.message || e) };
  }
  // a core that exits during a tag update never sends tags.updateDone: once it is back, the Tags A-Z row is usable again
  try {
    out.tagRestart = await js(`(async () => {
      const pause = (ms) => new Promise((r) => setTimeout(r, ms));
      document.querySelector('[data-page="tags"]').click();
      for (let i = 0; i < 50 && !document.querySelector('#tag-status td'); i++) await pause(100);
      const m = 'zz_selftest';
      showTagRunning(m, 5, '');
      const running = tagUpdates.has(m);
      onSidecarStatus({ ok: false, error: 'selftest' });
      onSidecarStatus(await window.lynxx.sidecarStatus());
      await pause(300);
      return { running, cleared: !tagUpdates.has(m), rows: document.querySelectorAll('#tag-status tr').length,
        runningButtons: document.querySelectorAll('#tag-status [data-running]').length };
    })()`);
    out.tagRestart.ok = out.tagRestart.running === true && out.tagRestart.cleared === true && out.tagRestart.rows > 1
      && out.tagRestart.runningButtons === 0;
  } catch (e) {
    out.tagRestart = { ok: false, error: String(e.message || e) };
  }
  out.tagLearned = await selfTestTagLearned(js);
  await js(`document.querySelector('[data-page="search"]').click()`).catch(() => {});
  saveSettings();
  return out;
}

async function selfTestTagLearned(js) {
  // after a download job, tags learned on top of a list (rn, rs; rp, whose own update merges: tags.learn says learned: false) reach
  // the window
  // (tags.learned), and only when the list changed (written). The chip shown keeps its hints while it is asked again (no blink),
  // the module's other cached answers are dropped. tags.learn is faked here; tags.resolve is the real core's
  const out = {};
  const wc = mainWindow.webContents;
  const realCall = sidecar.call;
  const realSend = wc.send;
  const told = [];
  try {
    const m = await js('S.module');
    const answers = [{ module: m, newPosts: 2, learned: false, bundled: true, added: 1, written: true },
      { module: m, newPosts: 1, learned: false, bundled: true, added: 1, written: false }];
    sidecar.call = function (method, params, ...rest) {
      if (method === 'tags.learn' && params?.module === m && answers.length) return Promise.resolve(answers.shift());
      return realCall.call(this, method, params, ...rest);
    };
    wc.send = function (channel, payload, ...rest) {
      if (channel === 'sidecar:notify' && payload?.method === 'tags.learned') told.push(Boolean(payload.params?.written));
      return realSend.call(this, channel, payload, ...rest);
    };
    await js(`(() => {
      document.querySelector('[data-page="search"]').click();
      window.__selftestChips = S.chips;
      const m = S.module;
      S.chips = [{ kind: 'tag', text: 'zz_selftest_learned', id: ++S.chipSeq }];
      window.__selftestRow = { tag: 'zz_selftest_learned', known: false, posts: 0, type: 'general' };
      resolveCache.set(rkey(m, 'zz_selftest_learned'), window.__selftestRow);
      resolveCache.set(rkey(m, 'zz_selftest_hidden'), { tag: 'zz_selftest_hidden', known: true, posts: 3, type: 'general' });
      renderChips();
    })()`);
    await learnTagList(m);
    out.shown = await js(`(async () => {
      const pause = (ms) => new Promise((r) => setTimeout(r, ms));
      const m = S.module;
      for (let i = 0; i < 100 && resolveCache.has(rkey(m, 'zz_selftest_hidden')); i++) await pause(5);
      const r = { dropped: !resolveCache.has(rkey(m, 'zz_selftest_hidden')), kept: resolveCache.get(rkey(m, 'zz_selftest_learned')) === window.__selftestRow };
      for (let i = 0; i < 60 && resolveCache.get(rkey(m, 'zz_selftest_learned')) === window.__selftestRow; i++) await pause(50);
      r.askedAgain = resolveCache.get(rkey(m, 'zz_selftest_learned')) !== window.__selftestRow;
      return r;
    })()`);
    await learnTagList(m); // newPosts but nothing written: the window is not told
    await new Promise((r) => setTimeout(r, 100));
    out.told = told;
    out.ok = told.join(',') === 'true' && out.shown.dropped === true && out.shown.kept === true && out.shown.askedAgain === true;
  } catch (e) {
    out.ok = false;
    out.error = String(e.message || e);
  } finally {
    sidecar.call = realCall;
    wc.send = realSend;
    await js(`(() => {
      if (!window.__selftestChips) return;
      resolveCache.delete(rkey(S.module, 'zz_selftest_learned'));
      S.chips = window.__selftestChips;
      delete window.__selftestChips;
      delete window.__selftestRow;
      renderChips();
    })()`).catch(() => {});
  }
  return out;
}

async function selfTestUpstream() {
  // thumbnails and lightbox media (lynxx-thumb): a module's own headers / cookies reach its website and its subdomains only, a
  // redirect is followed (at most MEDIA_REDIRECTS_MAX hops) without them once it leaves the site, a bot challenge says so, and the
  // module is part of the thumbnail cache key. A local server stands for the site (127.0.0.1) and for a CDN (localhost); the
  // check given here lets its loopback address through (the app's own check refuses it, see the security self-test), and the
  // site's entries go to it over plain http only because the test says so (siteHttp; the app sends them over https only).
  // Headers the network stack refuses are left out, and a redirect to a private address (IPv4-mapped IPv6 too) is refused
  const http = require('node:http');
  const out = {};
  const seen = [];
  const keep = { hosts: siteHosts, at: siteHostsAt, headers: settings.connection.siteHeaders, cookies: settings.connection.siteCookies };
  const srv = http.createServer((req, res) => {
    seen.push({ host: String(req.headers.host || '').replace(/:\d+$/, ''), path: req.url, cookie: req.headers.cookie || '',
      ua: req.headers['user-agent'] || '', range: req.headers.range || '', ok: req.headers['x-ok'] || '' });
    const port = srv.address().port;
    if (req.url === '/to-cdn.mp4') { res.writeHead(302, { location: `http://localhost:${port}/media.mp4` }); res.end(); return; }
    if (req.url === '/to-private.jpg') { res.writeHead(302, { location: `http://127.0.0.1:${port}/private.jpg` }); res.end(); return; }
    if (req.url === '/to-mapped.jpg') { res.writeHead(302, { location: `http://[::ffff:127.0.0.1]:${port}/mapped.jpg` }); res.end(); return; }
    if (req.url === '/loop.mp4') { res.writeHead(302, { location: '/loop.mp4' }); res.end(); return; }
    if (req.url === '/challenge.jpg') { res.writeHead(403, { 'content-type': 'text/html', 'cf-mitigated': 'challenge' }); res.end('<html></html>'); return; }
    if (req.url === '/page.jpg') { res.writeHead(200, { 'content-type': 'text/html' }); res.end('<html></html>'); return; }
    if (req.url.endsWith('.mp4')) {
      res.writeHead(206, { 'content-type': 'video/mp4', 'content-range': 'bytes 0-3/4', 'content-length': '4', 'accept-ranges': 'bytes' });
      res.end('mp4!');
      return;
    }
    res.writeHead(200, { 'content-type': 'image/jpeg' });
    res.end('jpeg');
  });
  const allow = async () => {};
  const failure = (p) => p.then(() => 'LOADED', (e) => String(e.message || e));
  try {
    await new Promise((resolve, reject) => { srv.once('error', reject); srv.listen(0, '127.0.0.1', resolve); });
    const site = `http://127.0.0.1:${srv.address().port}`;
    const cdn = `http://localhost:${srv.address().port}`;
    const row = { host: '127.0.0.1', capabilities: [] };
    siteHosts = new Map([['zt', row], ['zu', row], ['zv', row]]);
    siteHostsAt = Date.now();
    settings.connection.siteHeaders = { zt: { 'User-Agent': 'selftest-browser', Range: 'bytes=9-' },
      // refused by the network stack (each one alone fails the request): left out, the rest still goes
      zu: { 'User-Agent': 'selftest-zu', TE: 'trailers', 'Keep-Alive': '300', 'Proxy-Foo': '1', Expect: '100-continue', Upgrade: 'h2c',
        Trailer: 'x', Cookie2: '$Version=1', 'X-HTTP-Method-Override': 'TRACE', 'X-Ok': 'yes' },
      zv: { 'User-Agent': 'selftest-zv', 'X-Bad': 'đ' } }; // a value that is not Latin-1: the thumbnail still loads
    settings.connection.siteCookies = { zt: { cf_clearance: 'selftest-cookie' } };
    const at = (p) => seen.find((x) => x.path === p) || {};
    const lynxxUa = (x) => String(x.ua).startsWith('Lynxx/');
    const thumb = await fetchThumb(`${site}/a.jpg`, '', 'zt', allow, true);
    await fetchThumb(`${cdn}/b.jpg`, '', 'zt', allow, true);
    await fetchThumb(`${site}/c.jpg`, '', '', allow, true);
    await fetchThumb(`${site}/d.jpg`, '', 'zt', allow); // plain http: no cookie, no site header
    out.thumbs = {
      body: thumb.buf.toString(), type: thumb.type,
      site: at('/a.jpg').cookie === 'cf_clearance=selftest-cookie' && at('/a.jpg').ua === 'selftest-browser' && at('/a.jpg').range === '',
      cdn: at('/b.jpg').host === 'localhost' && at('/b.jpg').cookie === '' && lynxxUa(at('/b.jpg')),
      noModule: at('/c.jpg').cookie === '' && lynxxUa(at('/c.jpg')),
      plainHttp: at('/d.jpg').path === '/d.jpg' && at('/d.jpg').cookie === '' && lynxxUa(at('/d.jpg')),
      challenge: await failure(fetchThumb(`${site}/challenge.jpg`, '', 'zt', allow, true)),
      notImage: await failure(fetchThumb(`${site}/page.jpg`, '', 'zt', allow, true)),
      refusedHeaders: await failure(fetchThumb(`${site}/e.jpg`, '', 'zu', allow, true)),
      badValue: await failure(fetchThumb(`${site}/f.jpg`, '', 'zv', allow, true)),
    };
    out.thumbs.skipped = at('/e.jpg').ua === 'selftest-zu' && at('/e.jpg').ok === 'yes';
    const media = await streamMedia(`${site}/to-cdn.mp4`, '', 'bytes=0-', 'zt', allow, true);
    out.media = {
      status: media.status, type: media.headers.get('content-type'), range: media.headers.get('content-range'), body: await media.text(),
      first: at('/to-cdn.mp4').cookie === 'cf_clearance=selftest-cookie' && at('/to-cdn.mp4').range === 'bytes=0-',
      cdn: at('/media.mp4').host === 'localhost' && at('/media.mp4').cookie === '' && lynxxUa(at('/media.mp4')) && at('/media.mp4').range === 'bytes=0-',
      loop: await failure(streamMedia(`${site}/loop.mp4`, '', '', 'zt', allow, true)),
      loopHops: seen.filter((x) => x.path === '/loop.mp4').length,
    };
    // the first hop stands for a public site (stub), every later one gets the app's own check: a redirect into the machine or
    // the LAN is refused before anything is sent there
    const firstHopOnly = () => { let n = 0; return async (u) => { if (n++ > 0) await assertPublicHost(u); }; };
    out.redirects = {
      private: await failure(fetchThumb(`${site}/to-private.jpg`, '', '', firstHopOnly())),
      mapped: await failure(fetchThumb(`${site}/to-mapped.jpg`, '', '', firstHopOnly())),
      media: (await streamMedia(`${site}/to-private.jpg`, '', '', '', firstHopOnly()).then((r) => r.status, (e) => String(e.message || e))),
    };
    out.redirects.reached = seen.filter((x) => ['/private.jpg', '/mapped.jpg'].includes(x.path)).length;
    // literals as WHATWG URL writes them ([::ffff:127.0.0.1] -> [::ffff:7f00:1]) and IPv4 inside IPv6
    const literal = (u) => assertPublicHost(u).then(() => 'public', (e) => e.message);
    out.literals = (await Promise.all(['http://[::ffff:127.0.0.1]:8765/', 'http://[::ffff:192.168.1.1]/', 'http://[::127.0.0.1]/',
      'http://[::ffff:0:a00:1]/', 'http://[64:ff9b::a9fe:a9fe]/', 'http://[fe80::1]/', 'http://[ff02::1]/', 'http://[::1]/', 'http://0x7f.1/',
      'http://[::ffff:8.8.8.8]/', 'http://[2001:4860:4860::8888]/', 'http://8.8.8.8/'].map(literal))).join();
    const u = 'https://cdn.example.com/x.jpg';
    out.keys = {
      module: thumbKey(u, 'zt') !== thumbKey(u, ''), plain: thumbKey(u, '') === crypto.createHash('sha1').update(u).digest('hex'),
      avif: mimeOf('.avif'),
      hosts: [siteHostOf('https://www.zerochan.net/'), siteHostOf('ftp://x.net/'), onSiteHost('s3.zerochan.net', 'zerochan.net'),
        onSiteHost('zerochan.net', 'zerochan.net'), onSiteHost('evilzerochan.net', 'zerochan.net'), onSiteHost('zerochan.net.evil.com', 'zerochan.net'),
        onSiteHost('cdn.other.net', '')].join(),
    };
    const bad = await net.fetch(`lynxx-thumb://t/?u=${encodeURIComponent('https://example.com/x.jpg')}&m=${encodeURIComponent('BAD!')}`);
    out.badModule = bad.status;
  } catch (e) {
    out.error = String(e.message || e);
  } finally {
    siteHosts = keep.hosts;
    siteHostsAt = keep.at;
    settings.connection.siteHeaders = keep.headers;
    settings.connection.siteCookies = keep.cookies;
    srv.close();
  }
  const t = out.thumbs || {};
  const m = out.media || {};
  const rd = out.redirects || {};
  out.ok = Boolean(t.body === 'jpeg' && t.type === 'image/jpeg' && t.site && t.cdn && t.noModule && t.plainHttp && t.challenge === 'upstream 403: bot challenge'
    && t.notImage === 'not an image' && t.refusedHeaders === 'LOADED' && t.skipped && t.badValue === 'LOADED'
    && rd.private === 'blocked host' && rd.mapped === 'blocked host' && rd.media === 'blocked host' && rd.reached === 0
    && out.literals === `${'blocked host,'.repeat(9)}public,public,public`
    && m.status === 206 && m.type === 'video/mp4' && m.range === 'bytes 0-3/4' && m.body === 'mp4!' && m.first && m.cdn
    && m.loop === 'too many redirects' && m.loopHops === MEDIA_REDIRECTS_MAX + 1 && out.keys?.module && out.keys.plain && out.keys.avif === 'image/avif'
    && out.keys.hosts === 'zerochan.net,,true,true,false,false,false' && out.badModule === 400);
  return out;
}

async function selfTestSiteVerify(js) {
  // "Xác minh trong Lynxx" without a network: the renderer names only an abbr (a bad or uninstalled one opens no window), one
  // window per module, the window (sandboxed, no preload, no node, a session of its own, a User-Agent without Electron/), the
  // tracker fed by a cookie set into a test partition (the challenge itself is the user's: the test stands for the pages), the
  // save (the module's other entries and the other modules kept), a closed window and a site that asks nothing, and the cookie
  // value in no log. The user's per-site maps are given back exactly
  const out = {};
  const wait = (ms) => new Promise((r) => setTimeout(r, ms));
  const c = settings.connection;
  const keep = structuredClone({ siteHeaders: c.siteHeaders, siteCookies: c.siteCookies });
  const value = `selftest-clearance-${crypto.randomBytes(8).toString('hex')}`;
  const site = 'https://zz.invalid/';
  const captured = [];
  const wc = mainWindow.webContents;
  const realSend = wc.send;
  const realConsole = { log: console.log, info: console.info, warn: console.warn, error: console.error };
  const opened = [];
  const open = (partition, onWindow) => {
    let hooks = null;
    const run = runVerifyWindow({ abbr: 'zz', name: 'Selftest', site, partition, load: 'about:blank', show: false, timeoutMs: 60000,
      onWindow: (win, h) => { hooks = { win, ...h }; opened.push(win); onWindow?.(hooks); } })
      .catch((e) => ({ error: String(e.message || e) }));
    return { run, hooks: async () => { for (let i = 0; i < 100 && !hooks; i++) await wait(50); return hooks; } };
  };
  wc.send = function (channel, ...a) { captured.push(`${channel} ${JSON.stringify(a)}`); return realSend.call(this, channel, ...a); };
  for (const k of Object.keys(realConsole)) console[k] = (...a) => { captured.push(a.map(String).join(' ')); realConsole[k](...a); };
  try {
    const windowsBefore = BrowserWindow.getAllWindows().length;
    out.renderer = await js(`(async () => {
      const res = (p) => Promise.resolve(p).then((x) => 'OPENED ' + JSON.stringify(x), (e) => 'rejected: ' + String(e.message || e));
      return {
        api: typeof window.lynxx.site?.verify,
        badAbbr: await res(window.lynxx.site.verify('RN!')),
        notString: await res(window.lynxx.site.verify({ abbr: 'rx' })),
        notInstalled: await res(window.lynxx.site.verify('zz')),
        help: CHALLENGE_HELP.includes('Xác minh trong Lynxx'),
        options: [...document.querySelectorAll('#c-verify-module option')].map((o) => o.value).join(),
        installed: S.modules.filter((m) => m.installed && /^https:/.test(m.site || '')).map((m) => m.abbr).join(),
        merged: mergeSiteHttpText('{"zz": {"user-agent": "old", "x": "1"}, "yy": {"a": "b"}}', 'zz', { 'User-Agent': 'new' }),
        mergedEmpty: mergeSiteHttpText('', 'zz', { cf_clearance: 'c' }),
        mergedBad: mergeSiteHttpText('{not json', 'zz', { 'User-Agent': 'new' }),
      };
    })()`);
    const r = out.renderer;
    out.renderer.ok = r.api === 'function' && /^rejected: .*mã site không hợp lệ/.test(r.badAbbr) && /^rejected: .*mã site không hợp lệ/.test(r.notString)
      && /^rejected: .*chưa được cài/.test(r.notInstalled) && r.help && r.options === r.installed
      && r.merged === JSON.stringify({ zz: { x: '1', 'User-Agent': 'new' }, yy: { a: 'b' } }, null, 1)
      && r.mergedEmpty === JSON.stringify({ zz: { cf_clearance: 'c' } }, null, 1) && r.mergedBad === null;
    out.noWindowOnReject = BrowserWindow.getAllWindows().length === windowsBefore;
    // one window per module: a second call while one is open gets the same answer, no second window
    const pending = { win: null, promise: Promise.resolve({ ok: false, reason: 'selftest' }) };
    verifyWindows.set('zz', pending);
    out.oneWindowPerModule = verifySite('zz') === pending.promise;
    verifyWindows.delete('zz');
    // the user's other entries: kept by the save; 'yy' stands for another module
    c.siteHeaders = { zz: { 'user-agent': 'old-ua', 'X-Other': '1' }, yy: { 'User-Agent': 'yy-ua' } };
    c.siteCookies = { zz: { shm_session: 's1' }, yy: { cf_clearance: 'yy-cookie' } };
    const first = open('lynxx-verify-selftest');
    const h = await first.hooks();
    if (!h) throw new Error('the verification window did not open');
    const prefs = h.win.webContents.getLastWebPreferences() || {};
    const preloads = typeof h.ses.getPreloadScripts === 'function' ? h.ses.getPreloadScripts() : [];
    const ua = h.win.webContents.getUserAgent();
    out.window = {
      sandbox: prefs.sandbox === true, contextIsolation: prefs.contextIsolation === true,
      node: Boolean(prefs.nodeIntegration || prefs.nodeIntegrationInSubFrames || prefs.nodeIntegrationInWorker),
      preload: Boolean(prefs.preload) || preloads.length > 0, webview: prefs.webviewTag === true,
      ownSession: h.ses !== session.defaultSession && h.ses !== wc.session, ua,
      browserUa: !/Electron\//.test(ua) && !new RegExp(`(^|\\s)(${app.getName()}|${PACKAGE_NAME})/`, 'i').test(ua) && /Chrome\/\d/.test(ua),
      sameAsSession: ua === h.ses.getUserAgent(),
    };
    out.window.ok = out.window.sandbox && out.window.contextIsolation && !out.window.node && !out.window.preload && !out.window.webview
      && out.window.ownSession && out.window.browserUa && out.window.sameAsSession;
    // the cookie appears (as the site's answer to the user would set it): seen, but no page without a challenge yet
    await h.ready;
    await h.ses.cookies.set({ url: site, name: CLEARANCE_COOKIE, value, secure: true, httpOnly: true, expirationDate: Math.floor(Date.now() / 1000) + 900 });
    for (let i = 0; i < 40 && h.tracker.clearance?.value !== value; i++) await wait(50);
    out.tracker = { cookieSeen: h.tracker.clearance?.value === value, passedWithoutPage: h.tracker.passed };
    h.tracker.mainFrame(403, { 'CF-Mitigated': ['challenge'] }); // the challenge page is still the latest
    await wait(VERIFY_POLL_MS + 400);
    out.tracker.waitsOnChallenge = !h.win.isDestroyed();
    h.tracker.mainFrame(200, {}); // the page reloaded after the check
    const result = await Promise.race([first.run, wait(5000).then(() => ({ error: 'no answer' }))]);
    out.result = result;
    out.tracker.ok = out.tracker.cookieSeen && !out.tracker.passedWithoutPage && out.tracker.waitsOnChallenge;
    out.saved = {
      cookie: c.siteCookies.zz?.cf_clearance === value, keptCookie: c.siteCookies.zz?.shm_session === 's1',
      ua: c.siteHeaders.zz?.['User-Agent'] === ua, oldUaGone: !('user-agent' in (c.siteHeaders.zz || {})), keptHeader: c.siteHeaders.zz?.['X-Other'] === '1',
      otherModule: JSON.stringify(c.siteHeaders.yy) === '{"User-Agent":"yy-ua"}' && JSON.stringify(c.siteCookies.yy) === '{"cf_clearance":"yy-cookie"}',
      windowClosed: h.win.isDestroyed(),
      inFile: (() => { try { return JSON.parse(fs.readFileSync(settingsFile(), 'utf8')).connection.siteCookies.zz.cf_clearance === value; } catch { return false; } })(),
    };
    const own = (await connectionArgs('zz')).join(' ');
    const other = (await connectionArgs('yy')).join(' ');
    out.saved.args = own.includes(`-cookie ${CLEARANCE_COOKIE}=${value}`) && own.includes('-cookie shm_session=s1') && own.includes(`-header User-Agent=${ua}`)
      && !other.includes(value) && other.includes('-header User-Agent=yy-ua');
    out.saved.ok = result?.ok === true && typeof result.expires === 'number' && Object.values(out.saved).every((x) => x === true);
    // a second check of the same site: the cookie the first one saved (still in the persist partition, maybe one the site no
    // longer takes) is removed before the page loads and never seen by the tracker, and an edge redirect is no page. Then the
    // user closes the window: nothing saved
    const before = JSON.stringify(c.siteCookies);
    const kept = (await session.fromPartition('lynxx-verify-selftest').cookies.get({ name: CLEARANCE_COOKIE })).length;
    const second = open('lynxx-verify-selftest');
    const h2 = await second.hooks();
    await h2?.ready;
    h2?.tracker.mainFrame(301, {});
    await wait(VERIFY_POLL_MS + 400);
    out.stale = { kept, left: (await h2.ses.cookies.get({ name: CLEARANCE_COOKIE })).length, clearance: h2.tracker.clearance, open: !h2.win.isDestroyed() };
    out.stale.ok = out.stale.kept === 1 && out.stale.left === 0 && out.stale.clearance === null && out.stale.open === true;
    h2?.win.close();
    out.cancelled = await Promise.race([second.run, wait(5000).then(() => ({ error: 'no answer' }))]);
    out.cancelled.ok = out.cancelled.ok === false && out.cancelled.reason === 'cancelled' && JSON.stringify(c.siteCookies) === before;
    // a page without a challenge and no cookie: the site asks nothing now (a fresh partition: no cookie of the runs above)
    const third = open('lynxx-verify-selftest-2', (x) => x.tracker.mainFrame(200, {}));
    out.notNeeded = await Promise.race([third.run, wait(VERIFY_SETTLE_MS + 5000).then(() => ({ error: 'no answer' }))]);
    out.notNeeded.ok = out.notNeeded.ok === false && out.notNeeded.reason === 'not-needed' && /không yêu cầu xác minh/.test(out.notNeeded.message || '');
    out.windowsClosed = opened.every((w) => w.isDestroyed()) && BrowserWindow.getAllWindows().length === windowsBefore;
    // the window's way out: Lynxx's proxy, else the proxy the core takes from its environment (the sidecar names it), else
    // direct, never Chromium's 'system' mode; a proxy with a login is refused, an older core (no net.proxyFor) means direct
    const keepProxy = { proxy: c.proxy, ignoreProxy: c.ignoreProxy };
    const answer = (r) => async (m, p) => { if (m !== 'net.proxyFor' || p?.url !== site) throw new Error('unexpected call'); return r; };
    const mode = (p) => p.then((x) => JSON.stringify(x), (e) => `refused: ${e.message}`);
    try {
      c.proxy = '';
      c.ignoreProxy = false;
      out.proxy = {
        none: await mode(verifyProxy(site, answer({ proxy: '' }))),
        env: await mode(verifyProxy(site, answer({ proxy: 'socks5h://127.0.0.1:1080' }))),
        login: await mode(verifyProxy(site, answer({ proxy: 'http://u:selftest-pw@127.0.0.1:3128' }))),
        invalid: await mode(verifyProxy(site, answer({ proxy: 'ftp://127.0.0.1:21' }))),
        oldCore: await mode(verifyProxy(site, async () => { throw new Error('unknown method'); })),
      };
      c.proxy = 'http://127.0.0.1:3128';
      out.proxy.lynxx = await mode(verifyProxy(site, answer({ proxy: 'http://127.0.0.1:9' })));
      c.ignoreProxy = true;
      out.proxy.ignored = await mode(verifyProxy(site, answer({ proxy: '' })));
      out.proxy.real = await mode(verifyProxy(site)); // this machine's core, whatever it answers (recorded only)
    } finally {
      Object.assign(c, keepProxy);
    }
    const px = out.proxy;
    px.ok = px.none === '{"mode":"direct"}' && px.env === '{"proxyRules":"socks5://127.0.0.1:1080"}' && /^refused: .*tên đăng nhập/.test(px.login)
      && !px.login.includes('selftest-pw') && /^refused: .*không dùng được/.test(px.invalid) && px.oldCore === '{"mode":"direct"}'
      && px.lynxx === '{"proxyRules":"http://127.0.0.1:3128"}' && px.ignored === '{"mode":"direct"}' && !/"system"/.test(JSON.stringify(px));
  } catch (e) {
    out.error = String(e.message || e);
  } finally {
    wc.send = realSend;
    Object.assign(console, realConsole);
    opened.forEach((w) => { if (!w.isDestroyed()) w.destroy(); });
    for (const p of ['lynxx-verify-selftest', 'lynxx-verify-selftest-2']) await session.fromPartition(p).clearStorageData().catch(() => {});
    c.siteHeaders = keep.siteHeaders;
    c.siteCookies = keep.siteCookies;
    saveSettings();
  }
  out.restored = JSON.stringify({ siteHeaders: c.siteHeaders, siteCookies: c.siteCookies }) === JSON.stringify(keep);
  // the value went to the settings file and to the core's arguments only: no log line, no message to the window, not the answer
  out.noValueInLogs = !captured.some((l) => l.includes(value)) && !JSON.stringify(out).includes(value);
  out.ok = Boolean(out.renderer?.ok && out.noWindowOnReject && out.oneWindowPerModule && out.window?.ok && out.tracker?.ok && out.saved?.ok
    && out.stale?.ok && out.cancelled?.ok && out.notNeeded?.ok && out.windowsClosed && out.proxy?.ok && out.restored && out.noValueInLogs && !out.error);
  return out;
}

async function selfTestQuality(js) {
  // "Bộ lọc & tuỳ chọn tải": "Chất lượng video" only for a module with the video_quality_choice capability (hidden while the
  // module list is unknown), "Định dạng video" only with video_format_choice, "Chất lượng ảnh" original / light. The choices are
  // remembered (settings.download) and sent as -max_vres / -lowres. Fake modules, the panel and the settings are restored
  const out = {};
  const argsOf = (options) => downloadArgs({ module: 'db', tags: 'x', options }).args;
  out.args = {
    none: argsOf({ maxVideoHeight: 0 }).includes('-max_vres'),
    h720: argsOf({ maxVideoHeight: 720 }).join(' ').includes('-max_vres 720'),
    bad: [-1, 1.5, 'abc', null, VIDEO_HEIGHT_MAX + 1].map((v) => argsOf({ maxVideoHeight: v }).includes('-max_vres')).join(),
    lowRes: argsOf({ lowRes: true }).includes('-lowres'),
    defaults: DEFAULT_SETTINGS.download.maxVideoHeight === 0 && argsOf({}).includes('-max_vres') === false,
  };
  out.args.ok = out.args.none === false && out.args.h720 === true && out.args.bad === 'false,false,false,false,false' && out.args.lowRes === true
    && out.args.defaults === true;
  // '-mp4' / '-webm' only for a module whose capabilities (the cached ext.list row) have video_format_choice, whatever
  // settings.download says: a scheduled run takes the saved format, every job of a multi-site group the form's. Unknown: none
  const keepRows = { hosts: siteHosts, at: siteHostsAt, format: settings.download.videoFormat };
  try {
    siteHosts = new Map([['zr', { host: '', capabilities: ['preview', 'video_format_choice'] }], ['zq', { host: '', capabilities: ['preview'] }]]);
    siteHostsAt = Date.now();
    const fmt = (a) => a.filter((x) => x === '-mp4' || x === '-webm').join() || 'none';
    const built = async (module, options) => fmt((await jobs._build({ module, tags: 'x', options })).args);
    settings.download.videoFormat = 'webm'; // the saved choice, as a scheduled run (options {skipKnown: true}) gets it
    out.format = {
      capable: await built('zr', { skipKnown: true }), notCapable: await built('zq', { skipKnown: true }), unknown: await built('zy', {}),
      sent: await built('zr', { videoFormat: 'mp4' }), sentNotCapable: await built('zq', { videoFormat: 'mp4' }),
      noCapabilities: fmt(downloadArgs({ module: 'zr', tags: 'x', options: {} }).args),
    };
    out.format.ok = out.format.capable === '-webm' && out.format.notCapable === 'none' && out.format.unknown === 'none'
      && out.format.sent === '-mp4' && out.format.sentNotCapable === 'none' && out.format.noCapabilities === 'none';
  } catch (e) {
    out.format = { ok: false, error: String(e.message || e) };
  } finally {
    siteHosts = keepRows.hosts;
    siteHostsAt = keepRows.at;
    settings.download.videoFormat = keepRows.format;
  }
  try {
    out.ui = await js(`(async () => {
      const pause = (ms) => new Promise((r) => setTimeout(r, ms));
      document.querySelector('[data-page="search"]').click();
      const keep = { modules: S.modules, module: S.module, vres: $('#f-vres').value, lowres: $('#f-lowres').value, vformat: $('#f-vformat').value,
        d: { maxVideoHeight: S.settings.download.maxVideoHeight, lowRes: S.settings.download.lowRes, videoFormat: S.settings.download.videoFormat } };
      const r = {};
      try {
        const base = { source: 'user', default: false, installed: true, loaded: true, media: ['image', 'video'], syntax: { idSep: ':' },
          search: { idSearch: true, metaNames: [], kinds: [] } };
        const fake = [{ ...base, id: 'selftest-vres', name: 'Selftest vres', abbr: 'zq', capabilities: ['preview', 'video_quality_choice'] },
          { ...base, id: 'selftest-vformat', name: 'Selftest vformat', abbr: 'zr', capabilities: ['preview', 'video_format_choice'] }];
        S.modules = [...keep.modules.filter((m) => !['zq', 'zr'].includes(m.abbr)), ...fake];
        const use = (abbr) => { S.module = abbr; renderModuleButton(); };
        const shown = () => ({ vres: !$('#f-vres-wrap').hidden, vformat: !$('#f-vformat-wrap').hidden, summary: $('#filters-sum').textContent });
        use('zq');
        r.vres = shown();
        for (const [id, value] of [['#f-vres', '720'], ['#f-lowres', '1']]) {
          $(id).value = value;
          $(id).dispatchEvent(new Event('change', { bubbles: true }));
        }
        for (let i = 0; i < 40 && !(S.settings.download.maxVideoHeight === 720 && S.settings.download.lowRes === true); i++) await pause(50);
        r.saved = [S.settings.download.maxVideoHeight, S.settings.download.lowRes];
        const o = downloadOptions();
        r.options = [o.maxVideoHeight, o.lowRes];
        r.summary = $('#filters-sum').textContent;
        use('zr');
        r.vformat = shown();
        r.hiddenVres = downloadOptions().maxVideoHeight; // 720 is still the field's value, but the field is hidden
        S.modules = [];
        use('zq');
        r.unknown = shown();
      } finally {
        S.modules = keep.modules; S.module = keep.module;
        $('#f-vres').value = keep.vres; $('#f-lowres').value = keep.lowres; $('#f-vformat').value = keep.vformat;
        await saveSettings({ download: keep.d });
        renderModuleButton();
      }
      return r;
    })()`);
    const u = out.ui;
    u.ok = Boolean(u.vres.vres === true && u.vres.vformat === false && u.saved.join() === '720,true' && u.options.join() === '720,true'
      && /video ≤ 720p/.test(u.summary) && /ảnh nhẹ/.test(u.summary) && u.vformat.vres === false && u.vformat.vformat === true
      && !/video ≤/.test(u.vformat.summary) && u.hiddenVres === 0 && u.unknown.vres === false && settings.download.maxVideoHeight === 0);
  } catch (e) {
    out.ui = { ok: false, error: String(e.message || e) };
  }
  out.ok = Boolean(out.args.ok && out.format.ok && out.ui.ok);
  return out;
}

async function selfTestSelectedDownload(js) {
  // "Tải đã chọn" downloads exactly the picked posts: the panel's post limit, dates and gender filter are not sent (limit 1 with
  // two picked posts downloaded one). "Tải xuống" with posts picked in a current preview asks first; a stale preview (a date
  // changed since) does not. A fake preview of danbooru post 12579374, nothing is spawned (pump is off), the panel is restored.
  // On a site without id search (fake module zk) the job reads the Preview's page again: pick arguments, checked by main
  const out = {};
  const realPump = jobs.pump;
  const before = new Set(jobs.jobs.keys());
  const newJobs = () => [...jobs.jobs.values()].filter((j) => !before.has(j.id));
  const waitJob = async (n) => {
    for (let i = 0; i < 50 && newJobs().length < n; i++) await new Promise((r) => setTimeout(r, 100));
    return newJobs()[n - 1] || null;
  };
  const clean = (job) => job && {
    tail: job.args.slice(-2).join(' '), label: job.label,
    dropped: job.args.filter((a) => ['-dlimit', '-mindate', '-maxdate'].includes(a) || a.startsWith('-gender')),
  };
  const argsOk = (c) => Boolean(c && c.tail === '-- id:12579374' && c.dropped.length === 0);
  const waitFor = async (pred) => {
    for (let i = 0; i < 50 && !newJobs().some(pred); i++) await new Promise((r) => setTimeout(r, 100));
    return newJobs().find(pred) || null;
  };
  try {
    jobs.pump = () => {};
    out.setup = await js(`(async () => {
      document.querySelector('[data-page="search"]').click();
      const keep = { limit: $('#f-limit').value, dateMin: $('#f-datemin').value, path: $('#f-path').value, gender: S.settings.download.gender,
        warn: S.settings.download.warnNonEmpty };
      window.__selftestKeep = keep;
      $('#f-limit').value = '1';
      $('#f-datemin').value = '2020-01-01';
      $('#f-path').value = ${JSON.stringify(path.join(os.tmpdir(), 'lynxx-selftest-selected'))};
      S.settings.download.gender = { select: ['only_female'], conf: 'medium', op: 'keep' };
      S.settings.download.warnNonEmpty = false; // no folder question in the middle of the test
      Object.assign(S.preview, { module: 'db', tags: 'selftest_selected', site: '', page: 0, hasMore: false, dateFilter: 'native',
        items: [{ id: '12579374', excludedBy: [], excluded: false }, { id: '12579375', excludedBy: [], excluded: false }] });
      S.preview.key = previewKey();
      renderGrid();
      S.preview.selected.clear();
      S.preview.selected.add('12579374');
      syncSelection();
      // what the panel would send with a whole query download
      const sent = { limit: downloadOptions().limit, dates: currentQuery().dateMin, gender: genderState().select.join(',') };
      document.querySelector('#dl-selected').click();
      return { button: document.querySelector('#dl-selected').textContent, stale: previewStale(), sent };
    })()`);
    out.selected = clean(await waitJob(1));
    newJobs().forEach((j) => jobs.cancel(j.id)); // the same job again below must not be refused as a duplicate
    out.ask = await js(`(async () => {
      document.querySelector('#btn-download').click();
      await new Promise((r) => setTimeout(r, 50));
      const buttons = [...document.querySelectorAll('#modal-card .modal-actions button')].map((b) => b.textContent);
      const open = !document.querySelector('#modal').hidden && Boolean(document.querySelector('#dl-ask-selected'));
      document.querySelector('#dl-ask-selected')?.click();
      return { open, buttons, closed: document.querySelector('#modal').hidden };
    })()`);
    out.askSelected = clean(await waitJob(2));
    // the next page did not load (timeout, Cloudflare): the grid on screen is still a page of this query, its picked posts
    // stay picked, and "Tải xuống" still asks which posts are meant
    out.failedPage = await js(`(async () => {
      previewFailed(new Error('selftest: the next page did not load'));
      const dimmed = document.querySelector('#grid').classList.contains('stale');
      document.querySelector('#btn-download').click();
      await new Promise((r) => setTimeout(r, 50));
      const asked = !document.querySelector('#modal').hidden && Boolean(document.querySelector('#dl-ask-selected'));
      closeModal();
      return { dimmed, stale: previewStale(), asked, picked: S.preview.selected.size, button: document.querySelector('#dl-selected').textContent };
    })()`);
    // a date changed since the preview: the grid is stale, "Tải xuống" means the query (no question; with the quick self-test the
    // query is empty and refused, after the UI walkthrough it is queued like any download and removed below)
    out.stale = await js(`(async () => {
      const d = document.querySelector('#f-datemin');
      d.value = '2021-01-01';
      d.dispatchEvent(new Event('input', { bubbles: true }));
      const dimmed = document.querySelector('#grid').classList.contains('stale');
      document.querySelector('#btn-download').click();
      await new Promise((r) => setTimeout(r, 50));
      return { dimmed, stale: previewStale(), asked: !document.querySelector('#modal').hidden };
    })()`);
    await new Promise((r) => setTimeout(r, 300));
    out.staleJobs = newJobs().filter((j) => j.tags.startsWith('id:')).length; // no third job of the picked posts
    // no id search: the Preview's own query (its tags and dates, not the form's), its page index, page size and the picked ids;
    // the panel's post limit and gender filter are left out like above
    out.pickUi = await js(`(async () => {
      const fake = ${JSON.stringify(SELFTEST_KIND_MODULES[0])};
      window.__selftestKeep.modules = S.modules;
      S.modules = [...S.modules.filter((m) => m.abbr !== fake.abbr), fake];
      clearPreview();
      const query = { module: 'zk', tags: 'selftest_pick', dateMin: '2020-01-01', dateMax: '', gender: genderState(), safetyBlacklist: false, pageSize: 24 };
      Object.assign(S.preview, { module: 'zk', tags: 'selftest_pick', site: '', page: 2, pageSize: 24, query, hasMore: true, dateFilter: '',
        items: ['501', '502', '503'].map((id) => ({ id, excludedBy: [], excluded: false })) });
      S.preview.key = previewKey(query);
      renderGrid();
      S.preview.selected.clear();
      S.preview.selected.add('502');
      S.preview.selected.add('501');
      syncSelection();
      const b = document.querySelector('#dl-selected');
      const r = { enabled: !b.disabled, title: b.title, form: currentQuery().dateMin };
      b.click();
      return r;
    })()`);
    const pj = await waitFor((j) => j.tags === 'selftest_pick');
    if (pj) {
      const a = pj.args;
      const at = (opt) => (a.includes(opt) ? a[a.indexOf(opt) + 1] : null);
      const pickAt = a.indexOf('-pick_page');
      out.pick = { pick: a.slice(pickAt, pickAt + 6).join(' '), beforeTags: pickAt >= 0 && a.indexOf('-pick_ids') < a.indexOf('--'),
        tail: a.slice(a.indexOf('--')).join(' '), mindate: at('-mindate'), label: pj.label,
        dropped: a.filter((x) => ['-dlimit', '-maxdate'].includes(x) || x.startsWith('-gender')) };
      // run again keeps the pick; a multi-site group never gets one; a pick main cannot trust is refused
      const again = await jobs.rerun(pj.id, true);
      out.pick.rerunKeeps = (jobs.jobs.get(again.id)?.args || []).join(' ').includes('-pick_ids 502,501');
    }
    // a query of two sub-queries (an OR group the site cannot search): its page merges page N of both, 2 x pageSize posts. All of
    // them go; one more than that is refused by the renderer
    out.pickMerged = await js(`(async () => {
      const query = { module: 'zk', tags: 'selftest_pick_merged', dateMin: '', dateMax: '', gender: genderState(), safetyBlacklist: false, pageSize: 2 };
      Object.assign(S.preview, { module: 'zk', tags: 'selftest_pick_merged', site: '', page: 0, pageSize: 2, query, hasMore: true, dateFilter: '',
        tasks: ['a', 'b'], items: ['601', '602', '603', '604', '605'].map((id) => ({ id, excludedBy: [], excluded: false })) });
      S.preview.key = previewKey(query);
      renderGrid();
      S.preview.selected.clear();
      ['601', '602', '603', '604', '605'].forEach((id) => S.preview.selected.add(id));
      syncSelection();
      document.querySelector('#dl-selected').click(); // 5 > 2 x 2: refused
      await new Promise((r) => setTimeout(r, 50));
      S.preview.selected.delete('605');
      syncSelection();
      document.querySelector('#dl-selected').click();
      return true;
    })()`);
    const mj = await waitFor((j) => j.tags === 'selftest_pick_merged');
    if (mj) {
      const a = mj.args;
      out.pickMerged = { count: newJobs().filter((j) => j.tags === 'selftest_pick_merged').length,
        pick: a.slice(a.indexOf('-pick_page'), a.indexOf('-pick_page') + 6).join(' ') };
    }
    const group = await jobs.startMulti({ module: 'zk', tags: 'selftest_pick_group', path: path.join(os.tmpdir(), 'lynxx-selftest-selected'),
      pick: { page: 0, size: 24, ids: ['1'] } }, ['zm']);
    out.pickGroup = group.map((g) => (jobs.jobs.get(g.id)?.args || []).some((x) => x.startsWith('-pick'))).join();
    const bad = [{ page: -1, size: 24, ids: ['1'] }, { page: 0, size: 0, ids: ['1'] }, { page: 0.5, size: 24, ids: ['1'] },
      { page: '1', size: 24, ids: ['1'] }, { page: 0, size: 24, ids: [] }, { page: 0, size: 24, ids: ['1;x'] }, { page: 0, size: 24, ids: [1] },
      { page: 0, size: 5000, ids: Array.from({ length: 1001 }, (_, i) => String(i)) },
      { page: 0, size: 24, ids: ['-1'] }, { page: 0, size: 24, ids: ['x'.repeat(65)] }, [1], 'x'];
    out.pickRefused = [];
    for (const pick of bad) out.pickRefused.push(await jobs._build({ module: 'zk', tags: 'x', pick }).then(() => 'BUILT', (e) => e.message));
    out.pickOrder = pickArgs({ page: 0, size: 24, ids: ['-a', '7', '7'] }).join(' ');
    out.pickOverSize = pickArgs({ page: 0, size: 1, ids: ['1', '2'] }).join(' '); // more ids than the page size: a merged page
  } catch (e) {
    out.error = String(e.message || e);
  } finally {
    newJobs().forEach((j) => { jobs.cancel(j.id); jobs.jobs.delete(j.id); });
    jobs.pump = realPump;
    await js(`(() => {
      const k = window.__selftestKeep;
      if (k) {
        $('#f-limit').value = k.limit; $('#f-datemin').value = k.dateMin; $('#f-path').value = k.path;
        S.settings.download.gender = k.gender; S.settings.download.warnNonEmpty = k.warn;
        if (k.modules) S.modules = k.modules;
        delete window.__selftestKeep;
      }
      closeModal();
      clearPreview();
      document.querySelector('#jobs-clear').click(); // the renderer drops the jobs main no longer has
    })()`).catch(() => {});
  }
  out.ok = Boolean(out.setup?.sent?.limit === 1 && out.setup.sent.dates === '2020-01-01' && out.setup.sent.gender === 'only_female'
    && out.setup.stale === false && argsOk(out.selected) && out.ask?.open && out.ask.closed && out.ask.buttons.join('|') === 'Huỷ|Tải cả truy vấn|Tải 1 post đã chọn'
    && argsOk(out.askSelected) && out.failedPage?.dimmed === true && out.failedPage.stale === true && out.failedPage.asked === true
    && out.failedPage.picked === 1 && out.failedPage.button === 'Tải đã chọn (1)' && out.stale?.dimmed === true && out.stale.stale === true && out.stale.asked === false && out.staleJobs === 2
    && out.pickUi?.enabled === true && out.pickUi.title === '' && out.pickUi.form === '2021-01-01'
    && out.pick?.pick === '-pick_page 2 -pick_size 24 -pick_ids 502,501' && out.pick.beforeTags === true && out.pick.tail === '-- selftest_pick'
    && out.pick.mindate === '01-01-2020' && out.pick.dropped.length === 0 && out.pick.label === '2 post đã chọn từ: selftest_pick'
    && out.pick.rerunKeeps === true && out.pickGroup === 'false,false' && out.pickRefused.length === 12
    && out.pickRefused.every((m) => /^invalid pick/.test(m)) && out.pickOrder === '-pick_page 0 -pick_size 24 -pick_ids 7,-a'
    && out.pickOverSize === '-pick_page 0 -pick_size 1 -pick_ids 1,2'
    && out.pickMerged?.count === 1 && out.pickMerged.pick === '-pick_page 0 -pick_size 2 -pick_ids 601,602,603,604');
  return out;
}

// Search kinds (a tag '<token><value>' of a kind the module's manifest declares; typed by hand or added from Tags A-Z, the app
// has no kind picker): the core's autocomplete by type with a token; in the renderer, with fake modules (no request): no picker
// element, tokens typed by hand as chips (label, escape kept, no ± on a listing), the ± of a kind chip, no exclusion of a listing by
// the app, the autocomplete request of a typed token, the multi-site guard, the date fields of a site listing, the idSearch rules,
// the Preview hint; main's scheduled runs add 'id:>N' only where it works. Everything shown is restored
const SELFTEST_KIND_MODULES = (() => {
  const base = { source: 'user', default: false, installed: true, loaded: true, media: ['image', 'video'], capabilities: ['preview'], syntax: { idSep: ':' } };
  const kind = (k) => ({ token: '', value: '', input: 'tag', tagType: '', listing: false, pageSize: 0, example: '', label: '', use: '', order: 'id', ...k });
  const uploader = kind({ id: 'uploader', token: 'user:', value: '[^\\s:]+', input: 'text' });
  return [
    { ...base, id: 'selftest-kinds', name: 'Selftest kinds', abbr: 'zk', search: { idSearch: false, metaNames: ['vote'], kinds: [
      kind({ id: 'artist', token: 'artist:', value: '[a-z0-9_-]+', tagType: 'artist', listing: true, pageSize: 24, example: 'some_artist' }),
      uploader, kind({ id: 'keyword', token: 'search:', input: 'text', value: '\\S+', listing: true })] } },
    { ...base, id: 'selftest-nokinds', name: 'Selftest no kinds', abbr: 'zl', search: { idSearch: true, metaNames: [], kinds: [] } },
    { ...base, id: 'selftest-uploader', name: 'Selftest uploader', abbr: 'zm', search: { idSearch: true, metaNames: [], kinds: [uploader] } },
    // the same kind (id, token) as zm, but a listing there: a second site that takes 'user:x' in its own order (no dates)
    { ...base, id: 'selftest-uploader-listing', name: 'Selftest uploader listing', abbr: 'zn', search: { idSearch: true, metaNames: [],
      kinds: [{ ...uploader, listing: true }] } },
  ];
})();
const SELFTEST_KIND_ABBRS = SELFTEST_KIND_MODULES.map((m) => m.abbr);

async function selfTestSearchKinds(js) {
  const out = {};
  // main: 'id:>N' only where the module searches by id, the query is not a site listing and metatags use ':'
  const listingInfo = SELFTEST_KIND_MODULES[0];
  const f = {
    plain: autoIdFilter({ syntax: { idSep: ':' } }, 'scenery', 5),
    noIdSearch: autoIdFilter(listingInfo, 'scenery', 5),
    listing: autoIdFilter({ ...listingInfo, search: { ...listingInfo.search, idSearch: true } }, 'artist:x solo', 5),
    escapedListing: autoIdFilter({ ...listingInfo, search: { ...listingInfo.search, idSearch: true } }, '%artist:X.y% solo', 5),
    otherKind: autoIdFilter({ ...listingInfo, search: { ...listingInfo.search, idSearch: true } }, 'user:Some.Name', 5),
    otherSep: autoIdFilter({ syntax: { idSep: '=' } }, 'scenery', 5),
    noBaseline: autoIdFilter({ syntax: { idSep: ':' } }, 'scenery', 0),
  };
  out.autoIdFilter = { ...f, ok: f.plain === ' id:>5' && f.noIdSearch === '' && f.listing === '' && f.escapedListing === ''
    && f.otherKind === ' id:>5' && f.otherSep === '' && f.noBaseline === '' };
  // a "from now" schedule saved before its module stopped searching by id is held (not run on the whole result)
  const legacy = (a) => sanitizeAuto({ every: 24, maxId: 5, lastRun: 1, ...a });
  const held = {
    legacyNow: legacy({ lastStatus: 'failed' }).fromNow, legacyDone: legacy({ lastStatus: 'done' }).fromNow,
    legacyCounted: legacy({ lastStatus: 'failed', lastCount: 3 }).fromNow, kept: legacy({ fromNow: false, lastStatus: 'failed' }).fromNow,
    noId: autoHeld(listingInfo, { tags: 'scenery', auto: legacy({ lastStatus: 'failed' }) }),
    byId: autoHeld({ syntax: { idSep: ':' } }, { tags: 'scenery', auto: legacy({ lastStatus: 'failed' }) }),
    all: autoHeld(listingInfo, { tags: 'scenery', auto: legacy({ fromNow: false }) }),
    noBaseline: autoHeld(listingInfo, { tags: 'scenery', auto: sanitizeAuto({ every: 24, maxId: 0, fromNow: true }) }),
  };
  out.autoHeld = { ...held, ok: held.legacyNow === true && held.legacyDone === false && held.legacyCounted === false && held.kept === false
    && /Tải cả kết quả hiện có/.test(held.noId) && held.byId === '' && held.all === '' && held.noBaseline === '' };
  // the core: suggestions of one type, with a kind's token (a packaged core may ship no db list: then only the shape is checked)
  try {
    const r = await js(`(async () => {
      const list = await window.lynxx.rpc('tags.types', { module: 'db' });
      const ac = await window.lynxx.rpc('tags.autocomplete', { module: 'db', prefix: 'sa', limit: 8, types: ['artist'] });
      const tok = await window.lynxx.rpc('tags.autocomplete', { module: 'db', prefix: '-sa', limit: 3, types: ['artist'], token: 'user:' });
      return { types: list.types, hasList: list.hasList, acTypes: [...new Set(ac.map((x) => x.type))], ac: ac.length, tokInsert: tok.map((x) => x.insert) };
    })()`);
    r.ok = typeof r.types === 'object' && r.tokInsert.every((x) => x.startsWith('-user:'))
      && (!(r.types.artist > 0) || (r.ac > 0 && r.acTypes.join() === 'artist' && r.tokInsert.length > 0));
    out.server = r;
  } catch (e) {
    out.server = { ok: false, error: String(e.message || e) };
  }
  try {
    out.ui = await js(`(async () => {
      const pause = (ms) => new Promise((r) => setTimeout(r, ms));
      document.querySelector('[data-page="search"]').click();
      const fake = ${JSON.stringify(SELFTEST_KIND_MODULES)};
      const keep = { modules: S.modules, module: S.module, chips: S.chips, multi: [...multi], dmin: $('#f-datemin').value,
        ac: S.settings.search.autocomplete, L, input: tagInput.value };
      const r = {};
      const use = (abbr) => { S.module = abbr; renderModuleButton(); renderChips(); onQueryChanged(); };
      const typed = (text, exclude = false) => { tagInput.value = text; return commitTyped(exclude); };
      const kindChips = () => [...document.querySelectorAll('#chips .chip.kind')].map((e) => e.querySelector('.k').textContent + e.querySelector('.t').textContent);
      try {
        const fakeAbbrs = ${JSON.stringify(SELFTEST_KIND_ABBRS)};
        S.modules = [...keep.modules.filter((m) => !fakeAbbrs.includes(m.abbr)), ...fake];
        S.chips = [];
        // no kind picker, on a module with kinds or without: the search bar holds no select, the tag input reads the same
        use('zl');
        const placeholder = tagInput.placeholder;
        use('zk');
        let stored = null;
        try { stored = localStorage.getItem('lynxx.searchKind'); } catch { /* no storage */ }
        r.noPicker = !document.querySelector('#kind-picker, #kind-select') && !document.querySelector('.searchbar select')
          && tagInput.placeholder === placeholder && /^Nhập tag/.test(placeholder) && stored === null;
        r.meta = [parseToken('vote:3:bob').kind, parseToken('artist:x').kind, parseToken('solo').kind];
        // a kind typed with its token: added as typed (case kept, the escape written by hand kept), a space ends the tag
        typed('user:Some.Name');
        typed('%artist:Ab.c%');
        typed('solo');
        typed('search:night city');
        r.chips = S.chips.map(chipToString);
        r.chipLabels = kindChips();
        r.pm = [...document.querySelectorAll('#chips .chip')].map((e) => (e.querySelector('.pm') ? 1 : 0)); // none on a listing
        // a kind chip is edited as its whole tag: Enter gives it back
        const escaped = S.chips.find((c) => c.text === '%artist:Ab.c%');
        editChip(escaped);
        r.edit = [tagInput.value, S.chips.map(chipToString).includes('%artist:Ab.c%')];
        typed(tagInput.value);
        r.edit.push(S.chips.map(chipToString).includes('%artist:Ab.c%'));
        S.chips = S.chips.filter((c) => !['search:night', 'city'].includes(c.text));
        renderChips(); onQueryChanged();
        // tokens typed on the real core's module (db, builtin manifest): its chips, and what tags.validate says
        const dbKind = (id) => declaredKinds('db').find((k) => k.id === id);
        r.dbKinds = ['uploader', 'favorites', 'pool'].every((id) => dbKind(id)?.token);
        r.typedValid = [];
        for (const t of ['user:Some.Name', 'favorited_by:1', 'pool:5']) r.typedValid.push((await keep.L.rpc('tags.validate', { module: 'db', tags: t })).valid);
        // ± of a kind chip: built again from its value ('-' inside an escape), a listing is never excluded. Favorites / pool kinds
        // ('use') follow the core's tag check, not their regex: lower case, %…% for other characters; the real core takes them
        const poolId = { id: 'pool', token: 'pool:', input: 'id', value: '\\\\d+' };
        r.kindTag = [kindTag(poolId, '1234').tag, kindTag(poolId, '1234', true).tag, kindTag(poolId, 'abc').error || '', kindTag(poolId, '').error || ''];
        r.useKinds = r.dbKinds ? [kindTag(dbKind('favorites'), 'Some_Name').tag, kindTag(dbKind('pool'), 'My_Pool').tag, kindTag(dbKind('favorites'), 'a:b').tag] : [];
        r.useValid = [];
        for (const t of r.useKinds) r.useValid.push((await keep.L.rpc('tags.validate', { module: 'db', tags: t })).valid);
        const pm = { kind: 'meta', text: '%user:a:b%', exclude: false, id: ++S.chipSeq };
        const pmBefore = toggleChipExclude(pm);
        r.toggle = { escaped: [pmBefore, chipToString(pm)] };
        toggleChipExclude(pm);
        r.toggle.back = chipToString(pm);
        const plain = { kind: 'meta', text: 'user:Bob', exclude: false, id: ++S.chipSeq };
        toggleChipExclude(plain);
        r.toggle.plain = chipToString(plain);
        r.toggle.listing = toggleChipExclude({ kind: 'meta', text: 'artist:x', exclude: false, id: ++S.chipSeq });
        // what the app adds itself as an exclusion (a suggestion, the − of a Tags A-Z row): never a listing
        r.excludeRefused = [excludeRefused('artist:x'), excludeRefused('user:x'), excludeRefused('solo')];
        // typed by hand (no picker since 2.0.12): Shift+Enter on a listing adds nothing; a favorites / pool tag is written the way
        // the core's tag check takes it (lower case, %…% for other characters), and the real core takes it
        const nChips = S.chips.length;
        typed('artist:x', true);
        r.typedExclude = [S.chips.length === nChips, tagInput.value];
        tagInput.value = '';
        r.typedUse = r.dbKinds ? ['favorited_by:Some_Name', '-pool:My_Pool', 'favorited_by:a:b', 'user:Some.Name'].map((t) => typedKindTag(t, 'db')) : [];
        r.typedUseValid = [];
        for (const t of r.typedUse.slice(0, 3)) r.typedUseValid.push((await keep.L.rpc('tags.validate', { module: 'db', tags: t })).valid);
        // date fields: off with a site listing in the query, the typed range is kept for later
        $('#f-datemin').value = '2026-01-01';
        renderDateFields();
        r.dates = { disabled: $('#f-datemin').disabled, sent: currentQuery().dateMin, title: Boolean($('#f-datemin').closest('.field').title) };
        S.chips = S.chips.filter((c) => !tagKind(c.text)?.kind.listing);
        renderChips(); onQueryChanged();
        r.dates.after = { disabled: $('#f-datemin').disabled, sent: currentQuery().dateMin };
        // a picked second site that lists the same kind in its own order: the dates go to every site, so they are off too
        use('zm');
        S.chips = [{ kind: 'meta', text: 'user:x', exclude: false, id: ++S.chipSeq }];
        renderChips(); onQueryChanged();
        r.dates.multi = { alone: $('#f-datemin').disabled };
        multi.clear(); multi.add('zn'); renderMultiSites();
        Object.assign(r.dates.multi, { picked: $('#f-datemin').disabled, sent: currentQuery().dateMin, title: /Selftest uploader listing/.test($('#f-datemin').closest('.field').title),
          guard: multiKindConflict('user:x', 'zm', ['zn']) });
        multi.clear(); renderMultiSites();
        r.dates.multi.cleared = $('#f-datemin').disabled;
        use('zk');
        S.chips = [];
        // multi-site: a kind another picked site does not declare (same id, same token) is refused; a plain tag passes
        r.multi = { listing: multiKindConflict('artist:x solo', 'zk', ['zm']), uploader: multiKindConflict('user:x', 'zk', ['zm']),
          missing: multiKindConflict('(user:a~user:b)', 'zk', ['zl']), plain: multiKindConflict('some_artist', 'zk', ['zl']) };
        S.chips = [{ kind: 'meta', text: 'artist:x', exclude: false, id: ++S.chipSeq }];
        multi.clear(); multi.add('zm');
        renderMultiSites();
        r.multi.warn = $('#f-multi-warn .kind-conflict')?.textContent || '';
        multi.clear();
        renderMultiSites();
        // idSearch false: no "Theo tên file", no "only newer posts" schedule; "Tải đã chọn" reads the Preview's page again (pick
        // mode, see selfTestSelectedDownload) once the Preview reported its page size, not without one
        Object.assign(S.preview, { module: 'zk', items: [{ id: '1', excludedBy: [] }], tags: 'artist:x', site: '', page: 0, hasMore: false,
          dateFilter: '', localUnchecked: true, pageSize: 0, query: null });
        S.preview.key = previewKey();
        S.preview.selected.clear(); S.preview.selected.add('1');
        renderGrid();
        r.preview = $('#results-info').textContent.includes('chưa lọc cục bộ trong Preview');
        r.ids = { files: $('#btn-files').disabled, noSize: $('#dl-selected').disabled, noSizeTitle: $('#dl-selected').title === PICK_OFF };
        Object.assign(S.preview, { pageSize: 24, query: { ...currentQuery(), pageSize: 24 } });
        updateSelButton();
        Object.assign(r.ids, { selected: $('#dl-selected').disabled, title: $('#dl-selected').title,
          autoListing: autoIdFilterOk({ module: 'zm', tags: 'user:x' }), autoNoId: autoIdFilterOk({ module: 'zk', tags: 'solo' }) });
        use('zm');
        S.preview.module = 'zm'; updateSelButton();
        r.ids.otherFiles = $('#btn-files').disabled; r.ids.otherSelected = $('#dl-selected').disabled;
        S.preview.selected.clear(); clearPreview();
        // autocomplete: text that starts with a kind's token asks for that kind's values (its tag type, its token), rows are shown
        // without the token and inserted with it; a kind of ids or free words suggests nothing; plain text asks for every type
        use('zk');
        S.settings.search.autocomplete = true;
        const calls = [];
        L = { ...keep.L, rpc: async (m, p) => { calls.push([m, p]); return m === 'tags.autocomplete'
          ? [{ tag: 'artist:abc', posts: 3, type: 'artist' }, { tag: 'abd', posts: 2, type: 'artist' }] : keep.L.rpc(m, p); } };
        S.chips = []; renderChips();
        const type = async (text) => {
          hideAc(); calls.length = 0;
          tagInput.value = text;
          tagInput.dispatchEvent(new Event('input', { bubbles: true }));
          for (let i = 0; i < 40 && !document.querySelector('#ac .ac-item'); i++) await pause(25);
          const call = calls.find((c) => c[0] === 'tags.autocomplete')?.[1] || {};
          return { types: call.types, token: call.token, prefix: call.prefix, shown: [...document.querySelectorAll('#ac .ac-item .nm')].map((e) => e.textContent) };
        };
        r.ac = await type('artist:ab');
        r.ac.eligible = ['artist:ab', '-artist:ab', 'artist:a', 'user:bo', 'search:night', 'vote:3', 'ab'].map((t) => (acEligible(t) ? 1 : 0));
        pickAc(0, true); // a listing is not excluded
        r.ac.noExclude = S.chips.length === 0;
        pickAc(1);
        r.ac.picked = S.chips.map(chipToString);
        r.ac.inputCleared = tagInput.value === '';
        S.chips = []; renderChips();
        r.ac.negated = await type('-artist:ab');
        r.ac.plain = await type('ab');
        pickAc(0); // a list row that carries a token ('artist:abc', as the + of Tags A-Z adds it) is that kind's chip
        r.ac.plainPicked = [S.chips.map(chipToString).join(' '), kindChips().join('|')];
        r.errorVi = parseErrorVi('one site listing per search: artist:a search:b');
        r.errorVi2 = [parseErrorVi("artist:a is a site listing: 'or' groups cannot be used with it"),
          parseErrorVi("pool:12 lists posts in the site's order: 'or' groups cannot be used with it"),
          parseErrorVi("'-%user:a:b%': write the '-' inside the escape: '%-user:a:b%'")];
      } finally {
        L = keep.L;
        S.settings.search.autocomplete = keep.ac;
        multi.clear(); keep.multi.forEach((m) => multi.add(m));
        $('#f-datemin').value = keep.dmin;
        S.modules = keep.modules; S.module = keep.module; S.chips = keep.chips;
        hideAc(); tagInput.value = keep.input;
        clearPreview(); renderModuleButton(); renderChips(); onQueryChanged();
      }
      return r;
    })()`);
    const u = out.ui;
    u.ok = Boolean(u.noPicker === true && u.meta.join() === 'meta,meta,tag'
      && u.chips.join(' ') === 'user:Some.Name %artist:Ab.c% solo search:night city'
      && u.chipLabels.join('|') === 'Người đăng:Some.Name|Nghệ sĩ:Ab.c|Từ khoá:night' && u.pm.join('') === '10101'
      && u.edit.join('|') === '%artist:Ab.c%|false|true'
      && u.dbKinds === true && u.typedValid.every((v) => v === true)
      && u.kindTag.join('|') === 'pool:1234|-pool:1234|Pool: giá trị phải là số (id)|Pool: chưa có giá trị'
      && u.useKinds.join(' ') === 'favorited_by:some_name pool:my_pool %favorited_by:a:b%' && u.useValid.every((v) => v === true)
      && u.toggle.escaped.join('|') === '|%-user:a:b%' && u.toggle.back === '%user:a:b%' && u.toggle.plain === '-user:Bob' && /không loại trừ được/.test(u.toggle.listing)
      && /^Nghệ sĩ .*không loại trừ được$/.test(u.excludeRefused[0]) && u.excludeRefused[1] === '' && u.excludeRefused[2] === ''
      && u.typedExclude.join('|') === 'true|artist:x'
      && u.typedUse.join(' ') === 'favorited_by:some_name -pool:my_pool %favorited_by:a:b% user:Some.Name' && u.typedUseValid.every((v) => v === true)
      && u.dates.disabled === true && u.dates.sent === '' && u.dates.title && u.dates.after.disabled === false && u.dates.after.sent === '2026-01-01'
      && u.dates.multi.alone === false && u.dates.multi.picked === true && u.dates.multi.sent === '' && u.dates.multi.title && u.dates.multi.guard === ''
      && u.dates.multi.cleared === false
      && /Nghệ sĩ/.test(u.multi.listing) && u.multi.uploader === '' && /Người đăng/.test(u.multi.missing) && u.multi.plain === '' && /Nghệ sĩ/.test(u.multi.warn)
      && u.preview && u.ids.files === true && u.ids.noSize === true && u.ids.noSizeTitle === true && u.ids.selected === false && u.ids.title === ''
      && u.ids.autoListing === true && u.ids.autoNoId === false
      && u.ids.otherFiles === false && u.ids.otherSelected === false
      && u.ac.types?.join() === 'artist' && u.ac.token === 'artist:' && u.ac.prefix === 'ab' && u.ac.shown.join() === 'abc,abd'
      && u.ac.eligible.join('') === '1100001' && u.ac.noExclude === true && u.ac.picked.join() === 'artist:abd' && u.ac.inputCleared === true
      && u.ac.negated.types?.join() === 'artist' && u.ac.negated.token === 'artist:' && u.ac.negated.prefix === '-ab' && u.ac.negated.shown.join() === 'abc,abd'
      && u.ac.plain.types === undefined && u.ac.plain.token === undefined && u.ac.plain.prefix === 'ab' && u.ac.plain.shown.join() === 'artist:abc,abd'
      && u.ac.plainPicked.join('|') === 'artist:abc|Nghệ sĩ:abc' && /^mỗi lần tìm/.test(u.errorVi)
      && /nhóm OR/.test(u.errorVi2[0]) && /thứ tự của site, không dùng nhóm OR/.test(u.errorVi2[1]) && /dấu - phải nằm trong/.test(u.errorVi2[2]));
  } catch (e) {
    out.ui = { ok: false, error: String(e.message || e) };
  }
  out.ok = Boolean(out.autoIdFilter.ok && out.autoHeld.ok && out.server.ok && out.ui.ok);
  return out;
}

async function selfTestGenerator(js) {
  // extension generator: write-only key, a refused session (no network), no install without a ready draft and a confirmation
  const out = {};
  const realConfirm = generator.confirm;
  const waitIdle = async () => { for (let i = 0; i < 100 && generator.proc; i++) await new Promise((r) => setTimeout(r, 100)); };
  try {
    const fake = `sk-ant-selftest-${'k'.repeat(32)}`;
    out.setKey = await js(`window.lynxx.gen.setKey(${JSON.stringify(fake)}).then((s) => s, (e) => 'rejected: ' + e.message)`);
    out.keyStored = encryptionAvailable ? Boolean(secrets[AI_SECRET]?.enc) : !secrets[AI_SECRET];
    out.keyNotPlain = !fs.readFileSync(settingsFile(), 'utf8').includes(fake);
    const status = JSON.stringify(await js('window.lynxx.gen.status()')) + JSON.stringify(await js('window.lynxx.settings.get()'));
    out.rendererHasNoKey = !status.includes(fake) && !(secrets[AI_SECRET]?.enc && status.includes(secrets[AI_SECRET].enc));
    out.refused = await js(`(async () => {
      const done = new Promise((resolve) => { const off = window.lynxx.on('gen:event', (ev) => { if (ev.type === 'done') { off(); resolve(ev); } }); });
      await window.lynxx.gen.start({ url: 'http://example.com/', mode: 'template', media: ['image'] });
      const ev = await Promise.race([done, new Promise((r) => setTimeout(() => r({ status: 'timeout' }), 60000))]);
      return { status: ev.status, ruleIds: ev.ruleIds, session: ev.session };
    })()`);
    await waitIdle();
    const refusedSession = out.refused.session;
    out.listed = (await listGenDrafts()).some((d) => d.session === refusedSession && d.status === 'refused');
    out.installNotReady = await js(`window.lynxx.gen.install(${JSON.stringify(refusedSession)}).then(() => 'INSTALLED', (e) => e.message)`);
    out.badSession = await js('window.lynxx.gen.get("../../etc").then(() => "READ", (e) => e.message)');
    await js('window.lynxx.gen.clearKey()');
    out.aiWithoutKey = await js(`window.lynxx.gen.start({ url: 'https://example.com/', mode: 'ai' }).then(() => 'STARTED', (e) => e.message)`);
    // domain denylist (lynxx/safety_data/denied_domains.txt): refused before any request, with the Vietnamese reason
    out.denied = await js(`(async () => {
      const done = new Promise((resolve) => { const off = window.lynxx.on('gen:event', (ev) => { if (ev.type === 'done') { off(); resolve(ev); } }); });
      await window.lynxx.gen.start({ url: 'https://www.youtube.com/watch?v=1', mode: 'template', media: ['video'] });
      const ev = await Promise.race([done, new Promise((r) => setTimeout(() => r({ status: 'timeout' }), 60000))]);
      return { status: ev.status, ruleIds: ev.ruleIds, session: ev.session, summary: String(ev.summary || '').slice(0, 300) };
    })()`);
    await waitIdle();
    // signed remote catalog: the archive url / sha256 never come from the renderer (main reads the verified catalog)
    const realCatalogUrl = settings.advanced.catalogUrl;
    settings.advanced.catalogUrl = '';
    out.remoteBadId = await js(`window.lynxx.ext.installRemote({ id: '../x' }).then(() => 'INSTALLED', (e) => e.message)`);
    out.remoteNoCatalog = await js(`window.lynxx.ext.installRemote({ id: 'demo', url: 'https://evil.example/x.zip', sha256: '${'0'.repeat(64)}' })
      .then(() => 'INSTALLED', (e) => e.message)`);
    settings.advanced.catalogUrl = realCatalogUrl;
    out.remoteOk = /invalid extension id/.test(out.remoteBadId) && /catalog url must start with https/.test(out.remoteNoCatalog);
    if (!bundledCore()) {
      // a ready draft of the offline fake site of lynxx/tests/test_extgen.py, then the confirmed install path of main
      const py = 'import pathlib, sys\nfrom lynxx.tests.test_extgen import SITE, GenRequest, GenSession, fake_fetcher\n'
        + "r = GenSession(GenRequest(url=SITE, mode='template'), drafts_root=pathlib.Path(sys.argv[1]), fetcher=fake_fetcher()).run()\n"
        + "print('@@' + r['session'] + ' ' + r['status'] + ' ' + r['extId'])";
      const res = spawnSync(resolvePython(), ['-c', py, draftsRoot()], { cwd: REPO_ROOT, env: pythonEnv(), encoding: 'utf8', timeout: 180000 });
      const m = /@@(\S+) (\S+) (\S+)/.exec(res.stdout || '');
      out.readyDraft = m ? m[2] : `failed: ${redact(res.stderr || '').slice(-300)}`;
      if (m && m[2] === 'ready') {
        const [, session, , extId] = m;
        const asked = [];
        generator.confirm = async (msg) => { asked.push(msg); return false; };
        out.installDeclined = await js(`window.lynxx.gen.install('${session}').then(() => 'INSTALLED', (e) => e.message)`);
        const before = await sidecar.call('ext.list', {});
        out.notInstalledYet = !before.some((x) => x.id === extId && x.installed && x.source !== 'catalog');
        generator.confirm = async (msg) => { asked.push(msg); return true; };
        out.installed = await js(`window.lynxx.gen.install('${session}').then((r) => r && r.id, (e) => 'error: ' + e.message)`);
        out.asked = asked.length;
        out.inList = (await sidecar.call('ext.list', {})).some((x) => x.id === extId && x.installed);
        await sidecar.call('ext.uninstall', { id: extId }).catch(() => {});
        out.removed = await js(`window.lynxx.gen.remove('${session}')`);
        out.removedGone = !fs.existsSync(genSessionDir(session));
      }
    }
    generator.confirm = async () => true;
    await js(`window.lynxx.gen.remove(${JSON.stringify(refusedSession)})`);
    if (out.denied.session) await js(`window.lynxx.gen.remove(${JSON.stringify(out.denied.session)})`);
    out.ok = out.keyNotPlain && out.rendererHasNoKey && out.keyStored && out.refused.status === 'refused'
      && (out.refused.ruleIds || []).includes('R10') && out.listed && /chỉ cài được/.test(out.installNotReady)
      && /invalid draft session/.test(out.badSession) && /Chưa có khoá/.test(out.aiWithoutKey)
      && out.denied.status === 'refused' && (out.denied.ruleIds || []).includes('R7') && /danh sách chặn/.test(out.denied.summary)
      && out.remoteOk
      && (bundledCore() ? true : out.readyDraft === 'ready' && /cancelled$/.test(out.installDeclined) && out.notInstalledYet
        && out.installed === 'fakebooru' && out.asked === 2 && out.inList && out.removed === true && out.removedGone);
  } catch (err) {
    out.error = String(err && err.stack || err);
  } finally {
    generator.confirm = realConfirm;
  }
  return out;
}

async function selfTestIndexTools(js, shot) {
  // download index tools: clean only through main (confirmation), import only from a dialog-picked folder (a dry run job runs the
  // real core command), multi-site groups (validation, forced options, a site waits for the previous one; nothing is spawned)
  const out = {};
  const blocked = async (m) => js(`window.lynxx.rpc(${JSON.stringify(m)}, {}).then(() => 'allowed', (e) => 'blocked: ' + e.message)`);
  out.rpcBlocked = [await blocked('index.clean'), await blocked('index.cleanCancel')];
  const realConfirm = confirmIndexClean;
  const waitDone = `new Promise((resolve) => {
    const off = window.lynxx.on('sidecar:notify', ({ method, params }) => { if (method === 'index.cleanDone') { off(); resolve(params); } });
    setTimeout(() => { off(); resolve({ timeout: true }); }, 20000);
  })`;
  try {
    let asked = 0;
    confirmIndexClean = async () => { asked += 1; return { ok: false }; };
    out.cleanDeclined = await js(`window.lynxx.index.clean().then(() => 'STARTED', (e) => e.message)`);
    out.cleanBadOptions = await js(`window.lynxx.index.clean('x').then(() => 'STARTED', (e) => e.message)`);
    const askedDeclined = asked;
    out.cleanDryRun = await js(`(async () => { const done = ${waitDone}; const r = await window.lynxx.index.clean({ dryRun: true }); return { r, done: await done }; })()`);
    confirmIndexClean = async () => { asked += 1; return { ok: true, missingFolders: false }; };
    out.clean = await js(`(async () => { const done = ${waitDone}; const r = await window.lynxx.index.clean(); return { r, done: await done }; })()`);
    out.cleanAsked = asked;
    out.cleanOk = /cancelled/.test(out.cleanDeclined) && /invalid options/.test(out.cleanBadOptions) && askedDeclined === 1 && asked === 2
      && out.cleanDryRun.done?.ok === true && out.cleanDryRun.done?.dryRun === true && out.clean.done?.ok === true
      && out.clean.done?.dryRun === false && typeof out.clean.done?.removed === 'number';
  } catch (e) {
    out.cleanOk = false;
    out.cleanError = String(e.message || e);
  } finally {
    confirmIndexClean = realConfirm;
  }
  const dir = fs.mkdtempSync(path.join(os.tmpdir(), 'lynxx-selftest-import-'));
  try {
    for (const name of ['db_1.jpg', 'rx_2.png', 'IMG_3.jpg', 'notes.txt']) fs.writeFileSync(path.join(dir, name), 'x');
    out.importUnpicked = await js(`window.lynxx.index.importLibrary({ folder: ${JSON.stringify(dir)} }).then(() => 'STARTED', (e) => e.message)`);
    approvedImportFolders.add(dir); // what index:pickFolder does after the native dialog
    out.importBadModule = await js(`window.lynxx.index.importLibrary({ folder: ${JSON.stringify(dir)}, module: 'DB!' }).then(() => 'STARTED', (e) => e.message)`);
    const startedAt = Date.now();
    const job = await js(`window.lynxx.index.importLibrary({ folder: ${JSON.stringify(dir)}, dryRun: true })`);
    let j = jobs.jobs.get(job.id);
    for (let i = 0; i < 120 && j && !['done', 'failed', 'cancelled'].includes(j.status); i++) {
      await new Promise((r) => setTimeout(r, 250));
      j = jobs.jobs.get(job.id);
    }
    const p = j?.progress || {};
    out.importJob = { kind: job.kind, publicHidesSecrets: !('moduleSecretArgs' in job) && !('importQuery' in job), status: j?.status,
      exit: j?.exitCode, scanned: p.scanned, totalAll: p.totalAll, unknown: p.unknown, unknownNames: p.unknownNames, error: j?.error,
      stateName: p.stateName, ms: Date.now() - startedAt };
    // the job card: import texts, the lists of names (renderer). Cards are drawn on the next animation frame after each update:
    // wait for the card's final state (the "Danh sách" button of an ended import), not only for the card, which a slow machine
    // may still show as running
    await js(`document.querySelector('[data-page="downloads"]').click()`);
    // under a virtual X server (xvfb-run, no screenshot taken for a while: quick self-test) the page may draw no frame, so
    // requestAnimationFrame does not run and the card never appears; a capture makes it draw again (seen 2026-11-20)
    await mainWindow.webContents.capturePage().catch(() => {});
    const cardFrom = Date.now();
    out.importCard = await js(`(async () => {
      const listsOf = (card) => [...card.querySelectorAll('.job-actions .btn')].find((b) => b.textContent === 'Danh sách');
      let card = null;
      for (let i = 0; i < 100; i++) {
        card = document.querySelector('.job[data-id="${job.id}"]');
        const lists = card && listsOf(card);
        if (lists && !lists.hidden) break;
        await new Promise((r) => setTimeout(r, 100));
      }
      if (!card) return { card: false };
      const lists = listsOf(card);
      const text = card.textContent;
      if (lists && !lists.hidden) lists.click();
      for (let i = 0; i < 20 && !document.querySelector('#modal-card .import-list'); i++) await new Promise((r) => setTimeout(r, 100));
      const modal = document.querySelector('#modal-card').textContent;
      return { card: true, text, listsShown: Boolean(lists && !lists.hidden), modal };
    })()`);
    out.importCard.ms = Date.now() - cardFrom;
    await shot('24-import-job-lists');
    await js('closeModal()');
    out.importCardOk = out.importCard.card && /Nhập thư viện/.test(out.importCard.text) && /2 file sẽ được tra/.test(out.importCard.text)
      && out.importCard.listsShown && /Không nhận ra tên \(1\)/.test(out.importCard.modal) && /IMG_3\.jpg/.test(out.importCard.modal);
    out.importOk = /folder dialog/.test(out.importUnpicked) && /invalid module/.test(out.importBadModule) && job.kind === 'import'
      && out.importJob.publicHidesSecrets && j?.status === 'done' && p.scanned === 3 && p.totalAll === 2 && p.unknown === 1 && out.importCardOk;
    jobs.jobs.delete(job.id);
  } catch (e) {
    out.importOk = false;
    out.importError = String(e.message || e);
  } finally {
    approvedImportFolders.delete(dir);
    fs.rmSync(dir, { recursive: true, force: true });
  }
  const realPump = jobs.pump;
  const made = [];
  try {
    jobs.pump = () => {};
    const q = { module: 'db', tags: 'selftest_multi', path: path.join(os.tmpdir(), 'lynxx-selftest-multi'), options: { skipKnown: false } };
    const tryMulti = (mods) => js(`window.lynxx.jobs.startMulti(${JSON.stringify(q)}, ${JSON.stringify(mods)}).then((r) => 'ADDED ' + r.length, (e) => e.message)`);
    out.multiRefused = [await tryMulti([]), await tryMulti(['db']), await tryMulti(['rx', 'BAD!']), await tryMulti(Array.from({ length: 9 }, (_, i) => `m${i}`))];
    const list = await js(`window.lynxx.jobs.startMulti(${JSON.stringify(q)}, ['rx', 'db', 'rx'])`);
    made.push(...list.map((x) => x.id));
    const [a, b] = list.map((x) => jobs.jobs.get(x.id));
    const waitsBefore = jobs._waiting(b);
    jobs.cancel(a.id);
    const waitsAfter = jobs._waiting(b);
    const forged = await js(`window.lynxx.jobs.start({ ...${JSON.stringify(q)}, tags: 'selftest_multi_forged', group: { id: 'x', index: 9 } })`);
    made.push(forged.id);
    out.multi = {
      modules: list.map((x) => x.module), sameGroup: list[0].group?.id === list[1].group?.id && list[1].group?.index === 1,
      forcedArgs: [a, b].every((x) => x.args.includes('-skip_known') && x.args.includes('-prefix')), waitsBefore, waitsAfter,
      forgedGroup: forged.group, rerunAlone: (await jobs.rerun(a.id, true)).group,
    };
    made.push(...jobs.list().filter((x) => x.tags === 'selftest_multi' && !made.includes(x.id)).map((x) => x.id));
    out.multiOk = out.multiRefused.every((x) => /module khác nhau/.test(x)) && out.multi.modules.join() === 'db,rx' && out.multi.sameGroup
      && out.multi.forcedArgs && waitsBefore === true && waitsAfter === false && forged.group === null && out.multi.rerunAlone === null;
  } catch (e) {
    out.multiOk = false;
    out.multiError = String(e.message || e);
  } finally {
    made.forEach((id) => { jobs.cancel(id); jobs.jobs.delete(id); });
    jobs.pump = realPump;
    await js(`document.querySelector('#jobs-clear').click()`).catch(() => {}); // the renderer drops the jobs main no longer has
  }
  out.trash = await selfTestLibraryTrash(js);
  out.ui = await selfTestIndexUi(js, shot);
  out.ok = out.rpcBlocked.every((x) => /^blocked: .*not allowed/.test(x)) && out.cleanOk && out.importOk && out.multiOk && out.trash.ok
    && out.ui.ok;
  return out;
}

function selfTestSeedIndex(rows) {
  // rows of the download index for files the self-test made, written by the real core (lynxx/dlindex.py add_many) into the
  // index the sidecar reads (LYNXX_DATA_DIR = userData)
  const py = 'import json, sys\nfrom lynxx.dlindex import DownloadIndex, IndexRecord\n'
    + "with DownloadIndex(create=True) as index:\n    print(index.add_many([IndexRecord(module=r['module'], post_id=r['id'], path=r['path'], "
    + "tags=r['tags'], size=r['size']) for r in json.loads(sys.argv[1])]))";
  const res = spawnSync(resolvePython(), ['-c', py, JSON.stringify(rows)], { cwd: REPO_ROOT, env: pythonEnv(), encoding: 'utf8', timeout: 60000 });
  if (res.status !== 0) throw new Error(`index seed failed: ${redact(res.stderr || res.error?.message || '').slice(-300)}`);
}

async function selfTestLibraryTrash(js) {
  // the trash of the "Đã tải" page: only through main (the rpc channel refuses the core methods), paths checked, trash -> restore ->
  // purge with the native confirmation stubbed (declined: nothing deleted), forget, empty, settings.library.trashDays
  if (bundledCore()) return { ok: true, skipped: 'frozen core: no python to seed the index' };
  if (!selfTestOnScratchProfile()) return { ok: true, skipped: SELFTEST_REAL_PROFILE };
  const out = {};
  const dir = fs.mkdtempSync(path.join(os.tmpdir(), 'lynxx-selftest-trash-'));
  const realConfirm = confirmLibrary;
  const realPath = settings.download.path;
  const realDays = settings.library.trashDays;
  const asked = [];
  const call = (expr) => js(`${expr}.then((r) => r, (e) => 'ERR ' + e.message)`);
  const files = ['db_901.png', 'db_902.png', 'db_903.png'].map((n) => path.join(dir, n));
  try {
    settings.download.path = dir; // the download folder: its root takes '.lynxx-trash' when the data dir is on another drive
    const icon = path.join(__dirname, 'assets', 'icons', 'full-mask-orange-64.png');
    files.forEach((f) => fs.copyFileSync(icon, f));
    const gone = path.join(dir, 'db_904.png'); // a row whose file is not on disk
    const stranger = path.join(dir, 'stranger.png'); // a file the index does not know
    fs.copyFileSync(icon, stranger);
    selfTestSeedIndex([...files, gone].map((p, i) => ({ module: 'db', id: String(901 + i), path: p, tags: 'selftest_trash', size: 100 })));
    out.rpcBlocked = [];
    for (const m of ['index.trash', 'index.restore', 'index.purgeTrash', 'index.forget']) {
      out.rpcBlocked.push(await call(`window.lynxx.rpc('${m}', { paths: ${JSON.stringify(files)}, all: true })`));
    }
    out.badPaths = [await call(`window.lynxx.library.trash('x')`), await call(`window.lynxx.library.restore(['relative.png'])`)];
    const t = await call(`window.lynxx.library.trash(${JSON.stringify([files[0], files[1], stranger])})`);
    const moved = (t.trashed || []).map((x) => x.trashPath);
    out.trash = { trashed: moved.length, refused: t.refused, gone: !fs.existsSync(files[0]) && !fs.existsSync(files[1]),
      inTrash: moved.length === 2 && moved.every((p) => fs.existsSync(p)), strangerKept: fs.existsSync(stranger) };
    out.listed = (await call(`window.lynxx.rpc('index.search', { tags: 'selftest_trash', trash: true })`)).total;
    const r = await call(`window.lynxx.library.restore(${JSON.stringify(moved.slice(0, 1))})`);
    out.restore = { restored: r.restored?.length, back: fs.existsSync(files[0]) };
    confirmLibrary = async (message) => { asked.push(message); return false; };
    out.purgeDeclined = await call(`window.lynxx.library.purge(${JSON.stringify(moved.slice(1))})`);
    out.keptAfterDecline = fs.existsSync(moved[1]);
    confirmLibrary = async (message) => { asked.push(message); return true; };
    const p = await call(`window.lynxx.library.purge(${JSON.stringify(moved.slice(1))})`);
    out.purge = { purged: p.purged, files: p.files, deleted: !fs.existsSync(moved[1]) };
    const f = await call(`window.lynxx.library.forget(${JSON.stringify([gone, files[0]])})`);
    out.forget = { removed: f.removed, refused: f.refused };
    const t2 = await call(`window.lynxx.library.trash(${JSON.stringify([files[2]])})`);
    const e = await call('window.lynxx.library.emptyTrash()');
    out.empty = { purged: e.purged, deleted: Boolean(t2.trashed?.[0]) && !fs.existsSync(t2.trashed[0].trashPath) };
    out.asked = asked;
    out.autoTimer = await purgeOldTrash('timer'); // the self-test never purges on the timer
    out.autoSetting = await purgeOldTrash('setting');
  } catch (err) {
    out.error = String(err && err.stack || err);
  } finally {
    confirmLibrary = realConfirm;
    settings.download.path = realPath;
    fs.rmSync(dir, { recursive: true, force: true });
    await sidecar.call('index.forget', { paths: files }).catch(() => {}); // the rows left of the self-test's files
  }
  try {
    out.days = {
      tooMany: await call('window.lynxx.settings.set({ library: { trashDays: 400 } })'),
      notInt: await call('window.lynxx.settings.set({ library: { trashDays: 2.5 } })'),
      badPatch: await call("window.lynxx.settings.set({ library: 'x' })"),
      seven: await call('window.lynxx.settings.set({ library: { trashDays: 7 } }).then((x) => x.settings.library.trashDays)'),
      never: await call('window.lynxx.settings.set({ library: { trashDays: 0 } }).then((x) => x.settings.library.trashDays)'),
      back: await call('window.lynxx.settings.set({ library: { trashDays: 30 } }).then((x) => x.settings.library.trashDays)'),
    };
  } catch (err) {
    out.daysError = String(err && err.message || err);
  } finally {
    settings.library.trashDays = realDays; // the profile's own choice, set back without a purge
    saveSettings();
  }
  // a time in the trash the app did not write (the settings file was corrupt, a hand edit): never the default 30 days, which
  // could delete files the user meant to keep longer; nothing is emptied until the user picks a time again
  const file = settingsFile();
  const keep = { text: fs.readFileSync(file, 'utf8'), notices: startupNotices.length, blocked: settingsWriteBlocked,
    files: new Set(fs.readdirSync(path.dirname(file))) };
  try {
    fs.writeFileSync(file, keep.text.slice(0, Math.floor(keep.text.length / 2))); // cut short: not JSON
    loadSettings();
    out.lostDays = { corrupt: settings.library.trashDays, purge: await purgeOldTrash('setting') };
    fs.writeFileSync(file, JSON.stringify({ ...JSON.parse(keep.text), library: { trashDays: 400 } }));
    loadSettings();
    out.lostDays.handEdit = settings.library.trashDays;
    out.lostDays.saved = JSON.parse(fs.readFileSync(file, 'utf8')).library?.trashDays;
    out.lostDays.notices = startupNotices.slice(keep.notices).map((n) => n.title);
  } catch (err) {
    out.lostDays = { error: String(err && err.message || err) };
  } finally {
    fs.writeFileSync(file, keep.text);
    settingsWriteBlocked = keep.blocked;
    loadSettings();
    startupNotices.splice(keep.notices);
    for (const f of fs.readdirSync(path.dirname(file))) if (!keep.files.has(f) && f.includes('.corrupt-')) fs.rmSync(path.join(path.dirname(file), f), { force: true });
  }
  const lost = out.lostDays || {};
  const d = out.days || {};
  out.ok = Boolean(!out.error && out.rpcBlocked.every((x) => /^ERR .*not allowed/.test(x)) && out.badPaths.every((x) => /invalid paths/.test(x))
    && out.trash.trashed === 2 && out.trash.refused?.length === 1 && out.trash.gone && out.trash.inTrash && out.trash.strangerKept
    && out.listed === 2 && out.restore.restored === 1 && out.restore.back && /cancelled/.test(out.purgeDeclined) && out.keptAfterDecline
    && out.purge.purged === 1 && out.purge.files === 1 && out.purge.deleted && out.forget.removed === 1 && out.forget.refused?.length === 1
    && out.empty.purged === 1 && out.empty.deleted && out.asked.length === 4 && /Dọn sạch/.test(out.asked[3])
    && out.autoTimer === null && out.autoSetting?.purged === 0
    && [d.tooMany, d.notInt].every((x) => /từ 0 đến 365/.test(x)) && /invalid settings patch/.test(d.badPatch)
    && d.seven === 7 && d.never === 0 && d.back === 30 && settings.library.trashDays === realDays
    && lost.corrupt === 0 && lost.purge === null && lost.handEdit === 0 && lost.saved === 0
    && lost.notices?.join('|') === 'File cài đặt bị hỏng|Thùng rác tạm không tự xoá');
  return out;
}

async function selfTestIndexUi(js, shot) {
  // renderer side of the index tools: settings controls (a dry run fills the status line), the import dialog, the multi-site picker
  // and a multi-site group box in the job list (nothing is spawned: pump is off)
  const out = {};
  const pause = (ms) => new Promise((r) => setTimeout(r, ms));
  try {
    await js(`document.querySelector('[data-page="settings"]').click()`);
    await pause(300);
    out.dryRun = await js(`(async () => {
      document.querySelector('#index-clean-check').click();
      const st = document.querySelector('#index-clean-status');
      for (let i = 0; i < 60 && !(!st.hidden && /Đã kiểm/.test(st.textContent)); i++) await new Promise((r) => setTimeout(r, 250));
      st.scrollIntoView({ block: 'center' });
      return { text: st.textContent, buttons: ['#index-clean', '#index-clean-check', '#index-import'].every((s) => document.querySelector(s)) };
    })()`);
    await pause(300);
    await shot('20-index-settings');
    out.importDialog = await js(`(async () => {
      document.querySelector('#index-import').click();
      await new Promise((r) => setTimeout(r, 300));
      const m = document.querySelector('.import-modal');
      return { open: Boolean(m), modules: m ? m.querySelectorAll('select option').length : 0, startDisabled: m ? m.querySelector('.btn.primary').disabled : null };
    })()`);
    await shot('21-import-dialog');
    await js('closeModal()');
    out.multi = await js(`(async () => {
      document.querySelector('[data-page="search"]').click();
      document.querySelector('#filters').open = true;
      const pills = [...document.querySelectorAll('#f-multi-pills .pill')];
      pills[0]?.click();
      await new Promise((r) => setTimeout(r, 150));
      document.querySelector('#f-multi').scrollIntoView({ block: 'center' });
      return { visible: !document.querySelector('#f-multi').hidden, pills: pills.length, label: document.querySelector('#btn-download').textContent,
        warn: !document.querySelector('#f-multi-warn').hidden, summary: document.querySelector('#filters-sum').textContent };
    })()`);
    await pause(200);
    await shot('22-multi-site');
    out.multiOff = await js(`(async () => { document.querySelector('#f-multi-pills .pill.on')?.click(); await new Promise((r) => setTimeout(r, 50));
      return document.querySelector('#btn-download').textContent; })()`);
  } catch (e) {
    out.error = String(e.message || e);
  }
  const realPump = jobs.pump;
  let made = [];
  try {
    jobs.pump = () => {};
    const list = await js(`window.lynxx.jobs.startMulti({ module: 'db', tags: 'selftest_group_ui', path: ${JSON.stringify(path.join(os.tmpdir(), 'lynxx-selftest-group'))} }, ['rx'])`);
    made = list.map((x) => x.id);
    jobs.jobs.get(made[0]).progress = { stateName: 'DOWNLOADING', totalAll: 10, processed: 4, success: 4 };
    jobs.emit(jobs.jobs.get(made[0]));
    await js(`document.querySelector('[data-page="downloads"]').click()`);
    out.group = await js(`(async () => {
      const find = () => [...document.querySelectorAll('.job-group')].find((x) => x.textContent.includes('selftest_group_ui'));
      for (let i = 0; i < 20 && !find(); i++) await new Promise((r) => setTimeout(r, 100));
      await new Promise((r) => setTimeout(r, 100));
      const g = find();
      return g ? { cards: g.querySelectorAll('.job').length, head: g.querySelector('.job-group-head').textContent,
        order: [...g.querySelectorAll('.job')].map((c) => c.textContent.includes('site 1/2') ? 1 : c.textContent.includes('site 2/2') ? 2 : 0),
        waiting: g.textContent.includes('Chờ site trước tải xong') } : null;
    })()`);
    await pause(200);
    await shot('23-job-group');
  } catch (e) {
    out.groupError = String(e.message || e);
  } finally {
    made.forEach((id) => { jobs.cancel(id); });
    jobs.pump = realPump;
    await js(`document.querySelector('#jobs-clear').click()`).catch(() => {});
    await pause(200);
    out.groupRemoved = await js(`![...document.querySelectorAll('.job-group')].some((x) => x.textContent.includes('selftest_group_ui'))`).catch(() => null);
  }
  out.trash = await selfTestTrashUi(js, shot);
  out.ok = Boolean(out.trash.ok && out.dryRun?.buttons && /Đã kiểm/.test(out.dryRun?.text) && out.importDialog?.open && out.importDialog.modules >= 3
    && out.importDialog.startDisabled === true && out.multi?.visible && out.multi.pills >= 1 && /\(2 site\)/.test(out.multi.label) && out.multi.warn
    && /\+1 site/.test(out.multi.summary) && out.multiOff === 'Tải xuống' && out.group?.cards === 2 && out.group.order.join() === '1,2'
    && out.group.waiting && /db → rx/.test(out.group.head) && out.groupRemoved === true);
  return out;
}

async function selfTestTrashUi(js, shot) {
  // the "Đã tải" page with real files: select (click, shift + click), Xoá -> toast "Hoàn tác" -> files back; the trash view (badges
  // "còn X ngày", restore / purge / empty buttons), its lightbox, the settings row (select, trash size)
  if (bundledCore()) return { ok: true, skipped: 'frozen core: no python to seed the index' };
  if (!selfTestOnScratchProfile()) return { ok: true, skipped: SELFTEST_REAL_PROFILE };
  const out = {};
  const dir = fs.mkdtempSync(path.join(os.tmpdir(), 'lynxx-selftest-trashui-'));
  const files = Array.from({ length: 4 }, (_, i) => path.join(dir, `db_${951 + i}.png`));
  try {
    const icon = path.join(__dirname, 'assets', 'icons', 'full-mask-orange-64.png');
    files.forEach((f) => fs.copyFileSync(icon, f));
    selfTestSeedIndex(files.map((p, i) => ({ module: 'db', id: String(951 + i), path: p, tags: 'selftest_trash_ui', size: 1500 })));
    await sidecar.call('index.trash', { paths: files.slice(2), roots: [] }); // two files already in the trash
    await js(`document.querySelector('[data-page="library"]').click()`);
    out.library = await js(`(async () => {
      const pause = (ms) => new Promise((r) => setTimeout(r, ms));
      const cards = () => [...document.querySelectorAll('#lib-grid .card')];
      document.querySelector('#lib-q').value = 'selftest_trash_ui';
      document.querySelector('#lib-search').click();
      for (let i = 0; i < 50 && cards().length !== 2; i++) await pause(100);
      cards()[0].click();
      cards()[1].dispatchEvent(new MouseEvent('click', { bubbles: true, shiftKey: true }));
      const label = document.querySelector('#lib-delete').textContent;
      const selected = cards().filter((c) => c.classList.contains('sel')).length;
      document.querySelector('#lib-delete').click();
      let undo = null;
      for (let i = 0; i < 50 && !undo; i++) {
        await pause(100);
        undo = [...document.querySelectorAll('#toasts .toast button')].find((b) => b.textContent === 'Hoàn tác');
      }
      const count = () => document.querySelector('#lib-trash-count').textContent;
      for (let i = 0; i < 30 && (cards().length || count() !== '(4)'); i++) await pause(100);
      const afterDelete = { cards: cards().length, count: document.querySelector('#lib-trash-count').textContent };
      undo?.click();
      for (let i = 0; i < 50 && cards().length !== 2; i++) await pause(100);
      return { label, selected, undo: Boolean(undo), afterDelete, afterUndo: cards().length };
    })()`);
    out.backOnDisk = files.slice(0, 2).every((f) => fs.existsSync(f));
    out.trashView = await js(`(async () => {
      const pause = (ms) => new Promise((r) => setTimeout(r, ms));
      const cards = () => [...document.querySelectorAll('#lib-grid .card')];
      document.querySelector('#lib-view button[data-view="trash"]').click();
      for (let i = 0; i < 50 && !(cards().length === 2 && cards().every((c) => c.querySelector('.b.left'))); i++) await pause(100);
      const shown = (s) => !document.querySelector(s).hidden;
      return { cards: cards().length, badges: cards().map((c) => c.querySelector('.b.left')?.textContent || ''),
        info: document.querySelector('#lib-info').textContent, count: document.querySelector('#lib-trash-count').textContent,
        order: document.querySelector('#lib-order').value,
        tools: { delete: shown('#lib-delete'), restore: shown('#lib-restore'), purge: shown('#lib-purge'), empty: shown('#lib-empty') } };
    })()`);
    await new Promise((r) => setTimeout(r, 400));
    await shot('25-library-trash');
    // the thumbnails are read from the trash folder (lazy images: loaded once the page is drawn)
    out.trashView.thumbs = await js(`(async () => {
      const imgs = () => [...document.querySelectorAll('#lib-grid .card img')];
      for (let i = 0; i < 50 && imgs().filter((x) => x.naturalWidth === 64).length < 2; i++) await new Promise((r) => setTimeout(r, 100));
      return imgs().filter((x) => x.naturalWidth === 64).length;
    })()`);
    out.lightbox = await js(`(async () => {
      document.querySelector('#lib-grid .card').dispatchEvent(new MouseEvent('dblclick', { bubbles: true }));
      await new Promise((r) => setTimeout(r, 300));
      return { text: document.querySelector('#modal-card').textContent,
        buttons: [...document.querySelectorAll('#modal-card .modal-actions .btn')].map((b) => b.textContent) };
    })()`);
    await new Promise((r) => setTimeout(r, 400));
    await shot('26-library-trash-item');
    await js('closeModal()');
    // the trash view with a search that keeps none of its files: says so, not "Thùng rác trống."
    out.trashFiltered = await js(`(async () => {
      const pause = (ms) => new Promise((r) => setTimeout(r, ms));
      const q = document.querySelector('#lib-q');
      q.value = 'selftest_no_such_tag';
      document.querySelector('#lib-search').click();
      for (let i = 0; i < 50 && document.querySelectorAll('#lib-grid .card').length; i++) await pause(100);
      await pause(300);
      const info = document.querySelector('#lib-info').textContent;
      q.value = 'selftest_trash_ui';
      return info;
    })()`);
    // "Hoàn tác" (the toast sits above the lightbox) while a lightbox shows the file it brings back: the lightbox closes first
    out.undoLightbox = await js(`(async () => {
      const pause = (ms) => new Promise((r) => setTimeout(r, ms));
      const cards = () => [...document.querySelectorAll('#lib-grid .card')];
      document.querySelector('#lib-view button[data-view="library"]').click();
      for (let i = 0; i < 50 && cards().length !== 2; i++) await pause(100);
      cards()[0].click();
      document.querySelector('#lib-delete').click();
      let undo = null;
      for (let i = 0; i < 50 && !undo; i++) {
        await pause(100);
        undo = [...document.querySelectorAll('#toasts .toast button')].find((b) => b.textContent === 'Hoàn tác');
      }
      document.querySelector('#lib-view button[data-view="trash"]').click();
      for (let i = 0; i < 50 && cards().length !== 3; i++) await pause(100);
      const card = cards()[0]; // newest deletion first
      card.dispatchEvent(new MouseEvent('dblclick', { bubbles: true }));
      await pause(300);
      const shown = document.querySelector('#modal-card .lightbox')?.dataset.path === card.dataset.path;
      undo?.click();
      for (let i = 0; i < 50 && !document.querySelector('#modal').hidden; i++) await pause(100);
      const closed = document.querySelector('#modal').hidden;
      for (let i = 0; i < 50 && cards().length !== 2; i++) await pause(100);
      return { undo: Boolean(undo), shown, closed, trashCards: cards().length };
    })()`);
    out.undoLightbox.back = files.slice(0, 2).every((f) => fs.existsSync(f));
    await js(`document.querySelector('#lib-view button[data-view="library"]').click(); document.querySelector('#lib-q').value = ''`);
    await js(`document.querySelector('[data-page="settings"]').click()`);
    out.settings = await js(`(async () => {
      const info = document.querySelector('#index-trash-info');
      for (let i = 0; i < 50 && !/file/.test(info.textContent); i++) await new Promise((r) => setTimeout(r, 100));
      const sel = document.querySelector('#s-trash-days');
      sel.scrollIntoView({ block: 'center' });
      return { value: sel.value, options: sel.options.length, info: info.textContent, emptyEnabled: !document.querySelector('#index-empty-trash').disabled,
        row: [...document.querySelectorAll('#index-stats tr')].map((r) => r.textContent).find((t) => t.startsWith('Thùng rác')) || '' };
    })()`);
    await new Promise((r) => setTimeout(r, 400));
    await shot('27-trash-settings');
  } catch (err) {
    out.error = String(err && err.stack || err);
  } finally {
    // its own files only, never the rest of the trash
    const own = await sidecar.call('index.search', { tags: 'selftest_trash_ui', trash: true, pageSize: 50 }).catch(() => ({ items: [] }));
    if (own.items.length) await sidecar.call('index.purgeTrash', { paths: own.items.map((it) => it.path) }).catch(() => {});
    fs.rmSync(dir, { recursive: true, force: true });
    await sidecar.call('index.forget', { paths: files }).catch(() => {});
  }
  const lib = out.library || {};
  const tv = out.trashView || {};
  out.ok = Boolean(!out.error && lib.label === 'Xoá (2)' && lib.selected === 2 && lib.undo && lib.afterDelete?.cards === 0
    && lib.afterDelete?.count === '(4)' && lib.afterUndo === 2 && out.backOnDisk && tv.cards === 2 && tv.badges?.every((b) => b === 'còn 30 ngày')
    && tv.thumbs === 2 && /2 file trong thùng rác/.test(tv.info) && tv.count === '(2)' && tv.order === 'trashed'
    && !tv.tools?.delete && tv.tools?.restore && tv.tools?.purge && tv.tools?.empty
    && /Vị trí cũ: /.test(out.lightbox?.text) && out.lightbox?.buttons?.join() === 'Khôi phục,Xoá vĩnh viễn,Đóng'
    && /^Không có file nào trong thùng rác khớp bộ lọc \(thùng rác có 2 file\)/.test(out.trashFiltered || '')
    && out.undoLightbox?.undo && out.undoLightbox.shown && out.undoLightbox.closed && out.undoLightbox.trashCards === 2
    && out.undoLightbox.back
    && out.settings?.value === '30' && out.settings?.options === 6 && /2 file/.test(out.settings?.info) && out.settings?.emptyEnabled
    && /2 file/.test(out.settings?.row));
  return out;
}

async function selfTestQuitGuard(dir) {
  // quit / relaunch with jobs: stand-in processes (Electron as node) for a job that honours 'cancel' and one that ignores it
  const graceful = "process.stdin.on('data', (d) => { if (String(d).includes('cancel')) process.exit(130); }); setInterval(() => {}, 1000);";
  const stubborn = "process.on('SIGTERM', () => {}); process.stdin.on('data', () => {}); setInterval(() => {}, 1000);";
  const make = (name, script) => ({
    id: `selftest-${name}-${Date.now()}`, module: 'zz', tags: name, dest: path.join(dir, 'quit'), status: 'queued', progress: {},
    createdAt: Date.now(), startedAt: 0, endedAt: 0, exitCode: null, logTail: [], label: 'selftest', secretArgs: [], args: [],
    testCommand: { cmd: process.execPath, args: ['-e', script], env: { ELECTRON_RUN_AS_NODE: '1' } },
  });
  const maxJobs = settings.download.maxConcurrentJobs;
  const realAsk = askQuit;
  const asked = [];
  const out = {};
  let ids = [];
  try {
    settings.download.maxConcurrentJobs = 2;
    ids = [make('graceful', graceful), make('stubborn', stubborn), make('queued', graceful)].map((j) => jobs._add(j).id);
    await new Promise((r) => setTimeout(r, 1000));
    out.needsConfirm = jobsNeedingConfirm().length;
    askQuit = async (message, detail) => { asked.push(`${message} ${detail}`); return false; };
    out.declined = await requestQuit(true);
    out.asked = asked;
    out.afterDecline = ids.map((id) => jobs.jobs.get(id).status);
    const t0 = Date.now();
    out.shutdown = await jobs.shutdown(1500);
    out.shutdownMs = Date.now() - t0;
    await new Promise((r) => setTimeout(r, 300)); // 'close' events
    out.final = ids.map((id) => { const j = jobs.jobs.get(id); return `${j.status}:${j.exitCode}`; });
    out.ok = out.needsConfirm === 3 && out.declined === false && asked.length === 1
      && out.afterDecline.join() === 'running,running,queued' && out.shutdown.stopped === 2 && out.shutdown.killed === 1
      && out.final.join() === 'cancelled:130,cancelled:130,cancelled:null';
  } finally {
    askQuit = realAsk;
    jobs.closing = false;
    settings.download.maxConcurrentJobs = maxJobs;
    for (const id of ids) jobs.jobs.delete(id);
  }
  return out;
}

protocol.registerSchemesAsPrivileged([
  { scheme: 'lynxx-thumb', privileges: { standard: true, secure: true, supportFetchAPI: true, corsEnabled: true, stream: true } },
]);

if (!app.requestSingleInstanceLock()) {
  app.quit();
} else {
  app.on('second-instance', () => {
    if (mainWindow) { if (mainWindow.isMinimized()) mainWindow.restore(); mainWindow.focus(); }
  });
  app.whenReady().then(async () => {
    loadSettings();
    await initEncryption();
    registerThumbProtocol();
    registerIpc();
    registerIndexIpc();
    registerLibraryIpc();
    await applyProxyToSession();
    sidecar.start();
    setupMenu();
    createWindow();
    syncIcon({ startup: true }).catch(() => {}); // macOS dock, launcher / shortcuts written by an older version or another icon
    pruneThumbCache();
    if (process.env.LYNXX_SELFTEST_DIR) mainWindow.webContents.once('did-finish-load', () => runSelfTest(process.env.LYNXX_SELFTEST_DIR));
    sidecar.ready.then(() => maybeAutoUpdateTags()).catch(() => {});
    if (!process.env.LYNXX_SELFTEST_DIR) refreshTagSchedule(); // a self-test never touches the user's crontab
    sidecar.ready.then(() => {
      setTimeout(() => { // saved queries due while the app was closed, the trash past its time
        runDueSaved().catch(() => {});
        purgeOldTrash('timer').catch(() => {});
      }, 60000);
      setInterval(() => {
        runDueSaved().catch(() => {});
        purgeOldTrash('timer').catch(() => {});
      }, AUTO_CHECK_MS);
    }).catch(() => {});
  });
  app.on('window-all-closed', () => app.quit());
  app.on('before-quit', (e) => {
    // Cmd+Q, the macOS app menu, OS logout, app.quit(): jobs still queued / running need the user's confirmation first
    if (!quitApproved && jobs.active().length) {
      e.preventDefault();
      requestQuit(false);
      return;
    }
    quitApproved = true;
    jobs.killAll();
    generator.kill(); // an unfinished generation leaves its draft folder and report behind
    sidecar.stop();
  });
}
