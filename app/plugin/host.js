// Lynxx plugin host (Electron main): installed extensions, the sandbox that runs their scripts, the
// synchronous bridge (HTTP, sleep, storage, cookies, headless browser, crypto, Graphics) and the high-level service per extension.
//
// Isolation: extension scripts run in hidden sandboxed renderers (Chromium sandbox, context isolation, no Node) whose
// page has a CSP without connect-src and whose session cancels every network request: the only way out is the bridge
// below, which checks the sender and applies the HTTP rules of app/plugin/http.js.
'use strict';

const fs = require('node:fs');
const fsp = fs.promises;
const path = require('node:path');
const crypto = require('node:crypto');
const { pathToFileURL } = require('node:url');
const { BrowserWindow, ipcMain, session } = require('electron');
const { PluginHttp, sleep } = require('./http.js');
const { cryptoOp } = require('./host-crypto.js');
const { GraphicsWorker } = require('./graphics.js');
const { readPackageDir, capabilities, regexMatches, hostMatches, pluginId: idOf, RE_SCRIPT_FILE } = require('./package.js');
const { createService } = require('./service.js');

const RUN_TIMEOUT_MS = 120000;
const POOL_MAX = 3;
const RUNS_PER_WINDOW = 200; // a sandbox window is recycled after this many runs (memory of long download jobs)
const BROWSER_MAX = 2;
const RE_ID = /^[a-z0-9-]{1,48}$/;
const STORAGE_MAX_BYTES = 2 * 1024 * 1024;
const PLUGIN_PARTITION = 'persist:lynxx-plugins';
const SANDBOX_PARTITION = 'lynxx-plugin-sandbox';
// extension repositories listed in "Kho extension" from the first start: the community catalog of vbookext.me (several hundred
// comic / novel sources) and the reference repository. Added once: a repository the user removes stays removed
const DEFAULT_REPOSITORIES = [
  'https://www.vbookext.me/api/plugin.json',
  'https://raw.githubusercontent.com/Darkrai9x/vbook-extensions/master/plugin.json',
];
// Lynxx's default sources, installed at the first start: nHentai downloaded from the reference repository (the release files do
// not carry it), Cứu Truyện from app/extensions (Lynxx's own extension, updated with the app). Each is installed once: one the
// user uninstalls stays uninstalled, one that could not be installed is tried at the next start
const BUNDLED_DIR = path.join(__dirname, '..', 'extensions');
const DEFAULT_EXTENSIONS = [
  { key: 'nhentai', name: 'nHentai', source: 'https://nhentai.net', url: 'https://raw.githubusercontent.com/Darkrai9x/vbook-extensions/master/nhentai/plugin.zip' },
  { key: 'cuutruyen', name: 'Cứu Truyện', source: 'https://cuutruyen.net', bundled: 'cuutruyen' },
];

class PluginHost {
  /**
   * opts: {dataDir, assertPublicHost, userAgent(session) -> string, unpackZip(zip, dest) -> Promise, log(text), isDev}
   */
  constructor(opts) {
    this.opts = opts;
    this.root = path.join(opts.dataDir, 'plugins');
    this.installedDir = path.join(this.root, 'installed');
    this.storageDir = path.join(this.root, 'storage');
    this.stateFile = path.join(this.root, 'state.json');
    this.state = { plugins: {}, repositories: [] };
    this.cache = new Map(); // id -> package (manifest + files)
    this.pool = []; // {win, busy, runs, ready: Promise}
    this.queue = [];
    this.pending = new Map(); // run id -> {resolve, timer, slot}
    this.active = new Map(); // sandbox webContents id -> {pluginId, runId}
    this.browsers = new Map(); // browser id -> {win, owner (webContents id), urls: [], block: [RegExp]}
    this.seq = 0;
  }

  // ------------------------------------------------------------ setup

  init() {
    fs.mkdirSync(this.installedDir, { recursive: true });
    fs.mkdirSync(this.storageDir, { recursive: true });
    try { this.state = { plugins: {}, repositories: [], ...JSON.parse(fs.readFileSync(this.stateFile, 'utf8')) }; } catch { /* first run */ }
    if (!this.state.defaultRepositories) {
      this.state.repositories = [...new Set([...DEFAULT_REPOSITORIES, ...(this.state.repositories || [])])];
      this.state.defaultRepositories = 1;
      this.saveState();
    }
    this.pluginSession = session.fromPartition(PLUGIN_PARTITION);
    const ua = this.opts.userAgent(this.pluginSession);
    this.pluginSession.setUserAgent(ua);
    this.pluginSession.setPermissionRequestHandler((_wc, _p, cb) => cb(false));
    this.pluginSession.on('will-download', (e) => e.preventDefault());
    // headless browsers of Engine.newBrowser(): record requested URLs per browser, apply its block list, keep LAN out
    this.pluginSession.webRequest.onBeforeRequest((details, cb) => {
      let host = '';
      try { host = new URL(details.url).hostname.replace(/^\[|\]$/g, ''); } catch { /* not a URL */ }
      const v6 = host.includes(':');
      if (host === 'localhost' || /\.localhost$/i.test(host) || /^(127|10|0)\.\d/.test(host) || /^192\.168\./.test(host) || /^169\.254\./.test(host)
        || /^172\.(1[6-9]|2\d|3[01])\./.test(host) || (v6 && /^(::1?$|f[cd][0-9a-f]{0,2}:|fe[89ab][0-9a-f]:)/i.test(host))) { cb({ cancel: true }); return; }
      for (const b of this.browsers.values()) {
        if (b.win.isDestroyed() || b.win.webContents.id !== details.webContentsId) continue;
        if (b.urls.length < 5000) b.urls.push(details.url);
        if (b.block.some((re) => re.test(details.url))) { cb({ cancel: true }); return; }
      }
      cb({});
    });
    this.http = new PluginHttp({ session: this.pluginSession, assertPublicHost: this.opts.assertPublicHost, userAgent: ua });
    // the sandbox page itself never reaches the network (only its own files)
    const sandboxSession = session.fromPartition(SANDBOX_PARTITION);
    sandboxSession.webRequest.onBeforeRequest((details, cb) => cb({ cancel: !details.url.startsWith('file://') && !details.url.startsWith('devtools://') }));
    sandboxSession.setPermissionRequestHandler((_wc, _p, cb) => cb(false));
    this.sandboxUrl = pathToFileURL(path.join(__dirname, 'sandbox.html')).href;
    this.graphics = new GraphicsWorker({ BrowserWindow, partition: SANDBOX_PARTITION, isDev: this.opts.isDev });

    ipcMain.on('plugin-sandbox:sync', (event, op, payload) => {
      const ctx = this.active.get(event.sender.id);
      if (!ctx) { event.returnValue = { error: 'untrusted sender' }; return; }
      const reply = (value) => {
        try { event.returnValue = value; } catch (err) { event.returnValue = { error: `bridge reply: ${err.message}` }; }
      };
      this.bridge(ctx, op, payload).then((v) => reply(v === undefined ? null : v), (err) => reply({ error: String(err && err.message ? err.message : err) }))
        .catch(() => { /* the sandbox is gone */ });
    });
    ipcMain.on('plugin-sandbox:log', (event, level, text) => {
      const ctx = this.active.get(event.sender.id);
      if (ctx) this.opts.log(`[${ctx.pluginId}] ${level}: ${String(text).slice(0, 2000)}`);
    });
    ipcMain.on('plugin-sandbox:done', (event, id, result) => {
      const p = this.pending.get(id);
      if (!p || p.slot.win.webContents.id !== event.sender.id) return;
      this.finishRun(id, result);
    });
    ipcMain.on('plugin-sandbox:ready', (event) => {
      const slot = this.pool.find((s) => !s.win.isDestroyed() && s.win.webContents.id === event.sender.id);
      if (slot) slot.markReady();
    });
  }

  saveState() {
    const tmp = `${this.stateFile}.${process.pid}.tmp`;
    fs.writeFileSync(tmp, JSON.stringify(this.state, null, 2));
    fs.renameSync(tmp, this.stateFile);
  }

  // ------------------------------------------------------------ installed extensions

  package(id) {
    if (!RE_ID.test(String(id))) throw new Error('extension id không hợp lệ');
    let pkg = this.cache.get(id);
    if (!pkg) {
      const dir = path.join(this.installedDir, id);
      if (!fs.existsSync(dir)) throw new Error('extension chưa được cài');
      pkg = readPackageDir(dir);
      pkg.id = id;
      this.cache.set(id, pkg);
    }
    return pkg;
  }

  info(pkg) {
    const st = this.state.plugins[pkg.id] || {};
    const caps = capabilities(pkg.manifest, pkg.files);
    return {
      id: pkg.id, ...pkg.manifest.metadata, capabilities: caps, enabled: st.enabled !== false, installedAt: st.installedAt || 0,
      origin: st.origin || '', repository: st.repository || '', missing: pkg.missing,
      icon: pkg.icon ? `data:image/png;base64,${fs.readFileSync(pkg.icon).toString('base64')}` : '',
      config: Object.entries(pkg.manifest.config).map(([key, c]) => ({ key, ...c, value: (st.config || {})[key] ?? c.default })),
      supported: caps.content && !pkg.manifest.metadata.encrypt,
      unsupportedReason: pkg.manifest.metadata.encrypt ? 'Extension được mã hoá (encrypt): Lynxx không có khoá giải mã'
        : !caps.content ? `Loại "${pkg.manifest.metadata.type}" chưa được hỗ trợ (chỉ novel / comic)` : '',
    };
  }

  list() {
    const out = [];
    let names = [];
    try { names = fs.readdirSync(this.installedDir); } catch { names = []; }
    for (const id of names) {
      if (!RE_ID.test(id)) continue;
      try { out.push(this.info(this.package(id))); } catch (err) { out.push({ id, name: id, broken: String(err.message || err) }); }
    }
    return out.sort((a, b) => String(a.name).localeCompare(String(b.name), 'vi'));
  }

  matchUrl(url) {
    const hits = [];
    for (const p of this.list()) {
      if (p.broken || !p.enabled || !p.capabilities?.downloadable) continue;
      const pkg = this.package(p.id);
      if (regexMatches(pkg.manifest, url)) hits.unshift(p); else if (hostMatches(pkg.manifest, url)) hits.push(p);
    }
    return hits;
  }

  async installFromDir(dir, origin = {}) {
    // copy the validated package (plugin.json, icon.png, src/*.js only) into staging, then swap it in
    const pkg = readPackageDir(dir);
    if (!Object.keys(pkg.files).length) throw new Error('extension không có file script nào trong src/');
    const id = idOf(pkg.manifest.metadata);
    const staging = path.join(this.root, `.staging-${id}-${crypto.randomBytes(4).toString('hex')}`);
    await fsp.mkdir(path.join(staging, 'src'), { recursive: true });
    try {
      // read + write rather than copyFile: a bundled extension lives inside app.asar
      await fsp.writeFile(path.join(staging, 'plugin.json'), await fsp.readFile(path.join(pkg.root, 'plugin.json')));
      if (pkg.icon) await fsp.writeFile(path.join(staging, 'icon.png'), await fsp.readFile(pkg.icon));
      for (const [name, source] of Object.entries(pkg.files)) {
        if (!RE_SCRIPT_FILE.test(name)) continue;
        await fsp.writeFile(path.join(staging, 'src', name), source, 'utf8');
      }
      const target = path.join(this.installedDir, id);
      const old = `${target}.old-${Date.now()}`;
      if (fs.existsSync(target)) await fsp.rename(target, old);
      try {
        await fsp.rename(staging, target);
      } catch (err) {
        if (fs.existsSync(old) && !fs.existsSync(target)) await fsp.rename(old, target).catch(() => {});
        throw err;
      }
      if (fs.existsSync(old)) await fsp.rm(old, { recursive: true, force: true });
    } catch (err) {
      await fsp.rm(staging, { recursive: true, force: true });
      throw err;
    }
    this.cache.delete(id);
    const prev = this.state.plugins[id] || {};
    this.state.plugins[id] = { ...prev, enabled: prev.enabled !== false, installedAt: Date.now(), origin: origin.origin || '', repository: origin.repository || prev.repository || '', version: pkg.manifest.metadata.version };
    this.saveState();
    return this.info(this.package(id));
  }

  async installFromZip(zipPath, origin = {}) {
    const tmp = path.join(this.root, `.unpack-${crypto.randomBytes(6).toString('hex')}`);
    try {
      await this.opts.unpackZip(zipPath, tmp);
      // the package may sit in a single top-level folder of the archive
      let dir = tmp;
      if (!fs.existsSync(path.join(dir, 'plugin.json'))) {
        const entries = (await fsp.readdir(tmp, { withFileTypes: true })).filter((e) => e.isDirectory());
        if (entries.length === 1 && fs.existsSync(path.join(tmp, entries[0].name, 'plugin.json'))) dir = path.join(tmp, entries[0].name);
      }
      return await this.installFromDir(dir, origin);
    } finally {
      await fsp.rm(tmp, { recursive: true, force: true });
    }
  }

  async download(url, maxBytes) {
    const r = await this.http.request({ url, method: 'GET', timeout: 60000, headers: { Accept: '*/*' } });
    if (!r.ok) throw new Error(r.error || `HTTP ${r.status}`);
    const bytes = Buffer.from(r.body, 'base64');
    if (bytes.length > maxBytes) throw new Error('file quá lớn');
    return bytes;
  }

  async installFromUrl(url, origin = {}) {
    if (!/^https:\/\//i.test(url) && !/^http:\/\//i.test(url)) throw new Error('chỉ nhận link http(s)');
    const bytes = await this.download(url, 20 * 1024 * 1024);
    if (bytes.subarray(0, 2).toString('latin1') !== 'PK') throw new Error('link không trả về file .zip của extension');
    const tmp = path.join(this.root, `.dl-${crypto.randomBytes(6).toString('hex')}.zip`);
    await fsp.writeFile(tmp, bytes);
    try { return await this.installFromZip(tmp, { origin: url, ...origin }); } finally { await fsp.rm(tmp, { force: true }); }
  }

  async installDefaults() {
    // -> the extensions installed or updated by this call (see DEFAULT_EXTENSIONS)
    const done = () => new Set(this.state.defaultExtensions || []);
    const changed = [];
    for (const d of DEFAULT_EXTENSIONS) {
      if (this.closed) break;
      const have = this.list().find((p) => !p.broken && p.name === d.name && String(p.source).replace(/\/+$/, '') === d.source);
      let bundled = null;
      if (d.bundled) {
        try { bundled = readPackageDir(path.join(BUNDLED_DIR, d.bundled)); } catch (err) { this.opts.log(`extension đi kèm ${d.name}: ${err.message}`); continue; }
      }
      const version = bundled ? bundled.manifest.metadata.version : 0;
      let install = false;
      if (!done().has(d.key)) install = !have || (bundled && have.version < version); // first start (an older copy is replaced)
      else if (bundled && have && have.origin === 'bundled' && have.version < version) install = true; // the app brought a newer one
      if (install) {
        try {
          const info = bundled ? await this.installFromDir(bundled.root, { origin: 'bundled' }) : await this.installFromUrl(d.url, { repository: DEFAULT_REPOSITORIES[1] });
          changed.push(info);
          this.opts.log(`đã cài extension mặc định ${info.name} v${info.version}`);
        } catch (err) {
          this.opts.log(`chưa cài được extension mặc định ${d.name}: ${err.message} (thử lại ở lần mở sau)`);
          continue;
        }
      }
      if (!done().has(d.key)) {
        this.state.defaultExtensions = [...done(), d.key];
        this.saveState();
      }
    }
    return changed;
  }

  async fetchRepository(url) {
    // repository index: {metadata, data: [{name, author, version, type, locale, source, description, path, icon, tag}]}
    const bytes = await this.download(url, 8 * 1024 * 1024);
    let json;
    try { json = JSON.parse(bytes.toString('utf8').replace(/^﻿/, '')); } catch { throw new Error('kho không trả về JSON hợp lệ'); }
    const data = Array.isArray(json) ? json : Array.isArray(json.data) ? json.data : null;
    if (!data) throw new Error('JSON không đúng định dạng kho extension (thiếu "data")');
    const installed = new Map(this.list().filter((p) => !p.broken).map((p) => [`${p.name}\n${p.source}`, p]));
    return {
      url,
      author: String(json.metadata?.author || ''),
      description: String(json.metadata?.description || ''),
      items: data.slice(0, 3000).filter((e) => e && typeof e === 'object' && e.name && /^https?:\/\//i.test(String(e.path || ''))).map((e) => {
        const key = `${String(e.name).trim()}\n${String(e.source || '').trim()}`;
        const have = installed.get(key);
        const type = String(e.type || 'novel').toLowerCase();
        return {
          name: String(e.name).slice(0, 120), author: String(e.author || '').slice(0, 120), version: Number(e.version) || 0, type,
          locale: String(e.locale || ''), source: String(e.source || ''), description: String(e.description || '').slice(0, 500),
          path: String(e.path), icon: /^https?:\/\//i.test(String(e.icon || '')) ? String(e.icon) : '', nsfw: e.nsfw === true || String(e.tag || '').toLowerCase() === 'nsfw',
          installedVersion: have ? have.version : null, installedId: have ? have.id : '', supportedType: ['novel', 'comic', 'chinese_novel'].includes(type),
        };
      }),
    };
  }

  addRepository(url) {
    const list = this.state.repositories || [];
    if (!list.includes(url)) list.push(url);
    this.state.repositories = list.slice(-30);
    this.saveState();
    return this.state.repositories;
  }

  removeRepository(url) {
    this.state.repositories = (this.state.repositories || []).filter((u) => u !== url);
    this.saveState();
    return this.state.repositories;
  }

  async uninstall(id) {
    this.package(id);
    await fsp.rm(path.join(this.installedDir, id), { recursive: true, force: true });
    await fsp.rm(path.join(this.storageDir, `${id}.json`), { force: true });
    this.cache.delete(id);
    delete this.state.plugins[id];
    this.saveState();
    return true;
  }

  setEnabled(id, enabled) {
    this.package(id);
    this.state.plugins[id] = { ...(this.state.plugins[id] || {}), enabled: Boolean(enabled) };
    this.saveState();
    return this.info(this.package(id));
  }

  setConfig(id, values) {
    const pkg = this.package(id);
    const config = { ...((this.state.plugins[id] || {}).config || {}) };
    for (const [k, v] of Object.entries(values || {})) if (k in pkg.manifest.config) config[k] = String(v ?? '').slice(0, 2000);
    this.state.plugins[id] = { ...(this.state.plugins[id] || {}), config };
    this.saveState();
    return this.info(pkg);
  }

  configValues(pkg) {
    const st = (this.state.plugins[pkg.id] || {}).config || {};
    const out = {};
    for (const [k, c] of Object.entries(pkg.manifest.config)) out[k] = st[k] ?? c.default;
    return out;
  }

  service(id) {
    const pkg = this.package(id);
    const st = this.state.plugins[id] || {};
    if (st.enabled === false) throw new Error('extension đang tắt');
    if (pkg.manifest.metadata.encrypt) throw new Error('extension được mã hoá: Lynxx không chạy được loại này');
    const caps = capabilities(pkg.manifest, pkg.files);
    return { pkg, caps, svc: createService({ manifest: pkg.manifest, capabilities: caps, runner: (entry, args) => this.run(id, entry, args) }) };
  }

  // ------------------------------------------------------------ sandbox pool

  newSlot() {
    const win = new BrowserWindow({
      show: false, width: 400, height: 300,
      webPreferences: {
        preload: path.join(__dirname, 'sandbox-preload.js'), sandbox: true, contextIsolation: true, nodeIntegration: false,
        partition: SANDBOX_PARTITION, images: false, webgl: false, spellcheck: false, backgroundThrottling: false, devTools: Boolean(this.opts.isDev),
      },
    });
    let markReady;
    const slot = { win, wcId: win.webContents.id, busy: false, dead: false, runs: 0, ready: new Promise((ok) => { markReady = ok; }) };
    slot.markReady = markReady;
    win.webContents.on('will-navigate', (e) => e.preventDefault());
    win.webContents.setWindowOpenHandler(() => ({ action: 'deny' }));
    win.webContents.on('render-process-gone', () => this.dropSlot(slot, 'sandbox crashed'));
    win.on('closed', () => this.dropSlot(slot, 'sandbox closed'));
    win.loadURL(this.sandboxUrl).catch((err) => this.dropSlot(slot, `sandbox did not load: ${err.message}`));
    this.pool.push(slot);
    return slot;
  }

  dropSlot(slot, reason) {
    slot.dead = true;
    const i = this.pool.indexOf(slot);
    if (i >= 0) this.pool.splice(i, 1);
    for (const [id, p] of this.pending) if (p.slot === slot) this.finishRun(id, { ok: false, error: reason });
    this.active.delete(slot.wcId);
    this.closeBrowsers(slot.wcId);
    if (!slot.win.isDestroyed()) slot.win.destroy();
    this.pump();
  }

  run(pluginId, entry, args, { timeoutMs = RUN_TIMEOUT_MS } = {}) {
    if (this.closed) return Promise.resolve({ ok: false, error: 'Lynxx is closing' });
    const pkg = this.package(pluginId);
    if (!(entry in pkg.files)) return Promise.resolve({ ok: false, error: `script ${entry} không có trong extension` });
    return new Promise((resolve) => {
      this.queue.push({ pkg, entry, args, timeoutMs, resolve });
      this.pump();
    });
  }

  pump() {
    if (this.closed) return;
    while (this.queue.length) {
      let slot = this.pool.find((s) => !s.busy && !s.dead && !s.win.isDestroyed());
      if (!slot) {
        if (this.pool.length >= POOL_MAX) return;
        slot = this.newSlot();
      }
      const job = this.queue.shift();
      this.dispatch(slot, job);
    }
  }

  async dispatch(slot, job) {
    slot.busy = true;
    slot.runs += 1;
    const id = `run-${++this.seq}`;
    const timer = setTimeout(() => {
      // an endless loop or a stuck bridge call: the whole sandbox renderer goes. The slot leaves the pool first, so the next
      // queued script is never handed to the renderer being destroyed
      slot.dead = true;
      const at = this.pool.indexOf(slot);
      if (at >= 0) this.pool.splice(at, 1);
      this.finishRun(id, { ok: false, error: `extension chạy quá ${Math.round(job.timeoutMs / 1000)} giây` });
      this.dropSlot(slot, 'timeout');
    }, job.timeoutMs);
    this.pending.set(id, { resolve: job.resolve, timer, slot });
    await slot.ready;
    if (!this.pending.has(id) || slot.win.isDestroyed()) return;
    this.active.set(slot.win.webContents.id, { pluginId: job.pkg.id, runId: id, source: job.pkg.manifest.metadata.source });
    slot.win.webContents.send('plugin-sandbox:run', {
      id, pluginId: job.pkg.id, files: job.pkg.files, entry: job.entry, args: job.args,
      ctx: { source: job.pkg.manifest.metadata.source, config: this.configValues(job.pkg) },
    });
  }

  finishRun(id, result) {
    const p = this.pending.get(id);
    if (!p) return;
    this.pending.delete(id);
    clearTimeout(p.timer);
    const slot = p.slot;
    this.active.delete(slot.wcId);
    this.closeBrowsers(slot.wcId);
    slot.busy = false;
    if (slot.runs >= RUNS_PER_WINDOW && !slot.dead && !slot.win.isDestroyed()) this.dropSlot(slot, 'recycled');
    p.resolve(result && typeof result === 'object' ? result : { ok: false, error: 'kết quả không hợp lệ' });
    this.pump();
  }

  // ------------------------------------------------------------ bridge (sync calls from the sandbox)

  async bridge(ctx, op, payload) {
    switch (op) {
      case 'request': return this.http.request(payload || {});
      case 'sleep': await sleep(Math.max(0, Math.min(30000, Number(payload) || 0))); return null;
      case 'crypto': return cryptoOp(payload?.op, payload?.args || {});
      case 'storage': return this.storage(ctx.pluginId, payload || {});
      case 'cookie': return this.cookie(ctx, payload || {});
      case 'browser': return this.browser(ctx, payload || {});
      case 'graphics': return this.graphics.handle(payload || {});
      default: return { error: `unknown bridge op ${op}` };
    }
  }

  storage(pluginId, { op, key, value }) {
    const file = path.join(this.storageDir, `${pluginId}.json`);
    let data = {};
    try { data = JSON.parse(fs.readFileSync(file, 'utf8')); } catch { data = {}; }
    if (op === 'get') return Object.prototype.hasOwnProperty.call(data, key) ? data[key] : null;
    if (op === 'set') data[String(key)] = value === null ? null : String(value);
    else if (op === 'remove') delete data[String(key)];
    else if (op === 'clear') data = {};
    else return null;
    const text = JSON.stringify(data);
    if (text.length > STORAGE_MAX_BYTES) return { error: 'localStorage của extension đã đầy' };
    fs.writeFileSync(file, text);
    return null;
  }

  async cookie(ctx, { op, url, value }) {
    const target = /^https?:\/\//i.test(String(url || '')) ? String(url) : ctx.source;
    if (!/^https?:\/\//i.test(String(target || ''))) return '';
    if (op === 'get') return this.http.cookieHeader(target);
    if (op === 'set') {
      for (const part of String(value || '').split(';').slice(0, 50)) {
        const i = part.indexOf('=');
        if (i <= 0) continue;
        try { await this.pluginSession.cookies.set({ url: target, name: part.slice(0, i).trim(), value: part.slice(i + 1).trim(), expirationDate: Date.now() / 1000 + 30 * 86400 }); } catch { /* refused */ }
      }
      return null;
    }
    if (op === 'clear') {
      const list = await this.pluginSession.cookies.get({ url: target });
      for (const c of list) await this.pluginSession.cookies.remove(target, c.name).catch(() => {});
    }
    return null;
  }

  closeBrowsers(ownerId) {
    for (const [id, b] of this.browsers) {
      if (ownerId !== null && b.owner !== ownerId && !b.win.isDestroyed()) continue;
      if (!b.win.isDestroyed()) b.win.destroy();
      this.browsers.delete(id);
    }
  }

  async pageHtml(win) {
    try {
      const html = await win.webContents.executeJavaScript('document.documentElement ? document.documentElement.outerHTML : ""', true);
      return { html: String(html || ''), url: win.webContents.getURL() };
    } catch { return { html: '', url: win.webContents.getURL() }; }
  }

  async browser(ctx, p) {
    const owner = [...this.active].find(([, v]) => v === ctx)?.[0];
    const found = this.browsers.get(String(p.id));
    const b = found && found.owner === owner ? found : null; // a script only drives the browsers it opened
    switch (p.op) {
      case 'open': {
        if ([...this.browsers.values()].filter((x) => x.owner === owner).length >= BROWSER_MAX) return { error: 'quá nhiều trình duyệt ẩn' };
        const win = new BrowserWindow({ show: false, width: 1280, height: 900, webPreferences: { partition: PLUGIN_PARTITION, sandbox: true, contextIsolation: true, nodeIntegration: false, backgroundThrottling: false, devTools: false } });
        win.webContents.setWindowOpenHandler(() => ({ action: 'deny' }));
        win.webContents.on('will-navigate', (e, url) => { if (!/^https?:\/\//i.test(url)) e.preventDefault(); });
        win.webContents.setAudioMuted(true);
        const id = `b${++this.seq}`;
        this.browsers.set(id, { win, owner, urls: [], block: [] });
        return { id };
      }
      case 'launch': {
        if (!b) return { error: 'trình duyệt đã đóng' };
        const url = String(p.url || '');
        await this.opts.assertPublicHost(url);
        const timeout = Math.min(Math.max(Number(p.timeout) || 15000, 1), 60000);
        const wc = b.win.webContents;
        const loaded = new Promise((ok) => {
          let timer = null;
          const finish = () => { clearTimeout(timer); wc.removeListener('did-finish-load', finish); wc.removeListener('did-fail-load', failed); ok(); };
          // a sub-frame (an ad cancelled by block()) or an aborted navigation is not the end of the page load
          const failed = (_e, code, _desc, _url, isMainFrame) => { if (isMainFrame && code !== -3) finish(); };
          wc.on('did-finish-load', finish);
          wc.on('did-fail-load', failed);
          timer = setTimeout(finish, timeout);
        });
        b.win.loadURL(url).catch(() => {});
        if (p.async) return { html: '', url };
        await loaded;
        return this.pageHtml(b.win);
      }
      case 'html': {
        if (!b) return { error: 'trình duyệt đã đóng' };
        if (p.timeout) await sleep(Math.min(Number(p.timeout), 60000));
        return this.pageHtml(b.win);
      }
      case 'callJs': {
        if (!b) return { error: 'trình duyệt đã đóng' };
        let value = '';
        try { value = await b.win.webContents.executeJavaScript(String(p.script || ''), true); } catch (err) { value = ''; this.opts.log(`[${ctx.pluginId}] callJs: ${err.message}`); }
        if (p.wait) await sleep(Math.min(Number(p.wait), 60000));
        const text = typeof value === 'string' ? value : value === undefined || value === null ? '' : JSON.stringify(value);
        return { html: text, url: b.win.webContents.getURL() };
      }
      case 'waitUrl': {
        if (!b) return { error: 'trình duyệt đã đóng' };
        const res = (p.patterns || []).map((s) => { try { return new RegExp(s); } catch { return null; } }).filter(Boolean);
        const until = Date.now() + Math.min(Number(p.timeout) || 15000, 60000);
        while (Date.now() < until) {
          const hit = b.urls.filter((u) => res.some((re) => re.test(u)));
          if (hit.length) return { urls: hit };
          await sleep(200);
        }
        return { urls: [] };
      }
      case 'urls': return { urls: b ? b.urls.slice(-2000) : [] };
      case 'block': if (b) b.block = (p.patterns || []).map((s) => { try { return new RegExp(s); } catch { return null; } }).filter(Boolean); return null;
      case 'loadHtml': {
        if (!b) return { error: 'trình duyệt đã đóng' };
        const base = /^https?:\/\//i.test(String(p.base || '')) ? String(p.base) : 'about:blank';
        await b.win.loadURL(`data:text/html;charset=utf-8;base64,${Buffer.from(String(p.html || '')).toString('base64')}`, { baseURLForDataURL: base }).catch(() => {});
        return this.pageHtml(b.win);
      }
      case 'userAgent': if (b && p.ua) b.win.webContents.setUserAgent(String(p.ua)); return null;
      case 'close': if (b) { if (!b.win.isDestroyed()) b.win.destroy(); this.browsers.delete(String(p.id)); } return null;
      default: return { error: `unknown browser op ${p.op}` };
    }
  }

  // ------------------------------------------------------------ site verification window

  openSite(id, parent) {
    // a visible window in the extensions' session: the user passes a bot challenge or logs in once, the cookies then go
    // with every request of the extension (same session, same User-Agent)
    const pkg = this.package(id);
    const url = pkg.manifest.metadata.source;
    if (!/^https?:\/\//i.test(url)) throw new Error('extension không có địa chỉ nguồn (source)');
    const win = new BrowserWindow({ width: 1100, height: 800, parent: parent || undefined, title: `Mở trang nguồn — ${pkg.manifest.metadata.name}`, autoHideMenuBar: true, webPreferences: { partition: PLUGIN_PARTITION, sandbox: true, contextIsolation: true, nodeIntegration: false } });
    win.webContents.setWindowOpenHandler(({ url: u }) => { if (/^https?:\/\//i.test(u)) win.loadURL(u).catch(() => {}); return { action: 'deny' }; });
    win.loadURL(url).catch(() => { /* offline / DNS error: the window shows Chromium's error page */ });
    return true;
  }

  shutdown() {
    // quitting: no new sandbox windows, queued scripts fail, every hidden window goes (window-all-closed can fire)
    this.closed = true;
    for (const job of this.queue.splice(0)) job.resolve({ ok: false, error: 'Lynxx is closing' });
    this.closeBrowsers(null);
    this.graphics?.close();
    for (const slot of [...this.pool]) { slot.dead = true; if (!slot.win.isDestroyed()) slot.win.destroy(); }
    this.pool = [];
  }

  setProxy(config) {
    return this.pluginSession ? this.pluginSession.setProxy(config) : Promise.resolve();
  }
}

module.exports = { PluginHost, PLUGIN_PARTITION, DEFAULT_REPOSITORIES, DEFAULT_EXTENSIONS };
