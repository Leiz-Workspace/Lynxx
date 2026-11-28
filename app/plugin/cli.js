#!/usr/bin/env node
// Lynxx extension dev tool: run an extension folder outside the app, with the same runtime and result
// normalization as Lynxx (app/plugin/runtime.js + service.js). For extension authors and the new-site-extension skill.
//
//   node app/plugin/cli.js <extension-dir> check
//   node app/plugin/cli.js <extension-dir> home | genre
//   node app/plugin/cli.js <extension-dir> search <query> [page]
//   node app/plugin/cli.js <extension-dir> list <script.js> <input> [page]
//   node app/plugin/cli.js <extension-dir> detail <book-url>
//   node app/plugin/cli.js <extension-dir> toc <book-url>
//   node app/plugin/cli.js <extension-dir> chap <chapter-url>
//   node app/plugin/cli.js <extension-dir> run <file.js> [args...]        (raw execute(...) result, no normalization)
//   node app/plugin/cli.js <extension-dir> match <url>                    (does metadata.regexp route this URL?)
//   node app/plugin/cli.js <extension-dir> pack [out.zip]                 (plugin.json + icon.png + src/*.js)
// Options: --config KEY=VALUE (repeatable), --offline <fixtures.json> ({url: html} map, no network), --verbose.
//
// Not a security boundary: it runs extension code in this Node process (node:vm is not a sandbox). Use it for
// extensions you are writing or reviewing; the app itself runs extensions in a Chromium sandbox.
'use strict';

const fs = require('node:fs');
const path = require('node:path');
const zlib = require('node:zlib');
const { spawnSync } = require('node:child_process');
const R = require('./runtime.js');
const { cryptoOp } = require('./host-crypto.js');
const { createService } = require('./service.js');
const { readPackageDir, capabilities, regexMatches, RE_SCRIPT_FILE } = require('./package.js');

function usage(msg) {
  if (msg) console.error(`error: ${msg}\n`);
  console.error(fs.readFileSync(__filename, 'utf8').split('\n').slice(3, 18).map((l) => l.replace(/^\/\/ ?/, '')).join('\n'));
  process.exit(2);
}

const argv = process.argv.slice(2);
const opts = { config: {}, offline: null, verbose: false, allowPrivate: false };
const rest = [];
for (let i = 0; i < argv.length; i++) {
  const a = argv[i];
  if (a === '--config') { const [k, ...v] = String(argv[++i] || '').split('='); opts.config[k] = v.join('='); } else if (a === '--offline') opts.offline = argv[++i];
  else if (a === '--verbose') opts.verbose = true;
  else if (a === '--allow-private') opts.allowPrivate = true;
  else rest.push(a);
}
const [dir, command, ...args] = rest;
if (!dir || !command) usage();

let pkg;
try { pkg = readPackageDir(dir); } catch (err) { usage(err.message); }
const config = { ...Object.fromEntries(Object.entries(pkg.manifest.config).map(([k, c]) => [k, c.default])), ...opts.config };
const fixtures = opts.offline ? JSON.parse(fs.readFileSync(opts.offline, 'utf8')) : null;
const jar = new Map(); // host -> Map(name -> value)
const storage = new Map();
const cache = new Map();
const sleeper = new Int32Array(new SharedArrayBuffer(4));
let requests = 0;

function cookieFor(url) {
  const host = new URL(url).hostname;
  const out = [];
  for (const [h, m] of jar) if (host === h || host.endsWith(`.${h}`)) for (const [k, v] of m) out.push(`${k}=${v}`);
  return out.join('; ');
}

function storeCookies(url, lines) {
  const host = new URL(url).hostname;
  for (const line of lines || []) {
    const [pair, ...attrs] = String(line).split(';');
    const eq = pair.indexOf('=');
    if (eq <= 0) continue;
    const domainAttr = attrs.map((a) => a.trim()).find((a) => /^domain=/i.test(a));
    const domain = domainAttr ? domainAttr.slice(7).replace(/^\./, '').toLowerCase() : host;
    if (!jar.has(domain)) jar.set(domain, new Map());
    jar.get(domain).set(pair.slice(0, eq).trim(), pair.slice(eq + 1).trim());
  }
}

const host = {
  request(req) {
    requests += 1;
    if (opts.verbose) console.error(`> ${req.method} ${req.url}`);
    if (fixtures) {
      const page = fixtures[req.url] ?? fixtures[req.url.replace(/\/+$/, '')];
      if (page === undefined) return { ok: false, status: 404, url: req.url, headers: {}, body: '' };
      return { ok: true, status: 200, url: req.url, headers: { 'content-type': 'text/html; charset=utf-8' }, body: Buffer.from(String(page)).toString('base64') };
    }
    const headers = { 'User-Agent': R.UA.chrome, 'Accept-Language': 'vi-VN,vi;q=0.9,en;q=0.8', ...(req.headers || {}) };
    const jarCookie = cookieFor(req.url);
    if (jarCookie) headers.Cookie = [jarCookie, headers.Cookie || headers.cookie].filter(Boolean).join('; ');
    delete headers.cookie;
    const r = spawnSync(process.execPath, [path.join(__dirname, 'cli-fetch.js')], {
      input: JSON.stringify({ ...req, headers, allowPrivate: opts.allowPrivate }), maxBuffer: 96 * 1024 * 1024, encoding: 'utf8', timeout: (Number(req.timeout) || 60000) + 5000,
    });
    if (r.error || r.status !== 0) return { ok: false, status: 504, url: req.url, headers: {}, body: '', error: String(r.error?.message || r.stderr || 'fetch failed').slice(0, 300) };
    const res = JSON.parse(r.stdout);
    storeCookies(res.url || req.url, res.setCookie);
    if (opts.verbose) console.error(`< ${res.status} ${res.url} (${Math.round((res.body || '').length * 0.75)} bytes)`);
    return { ...res, requestHeaders: { cookie: headers.Cookie || '' } };
  },
  sleep(ms) { Atomics.wait(sleeper, 0, 0, Math.min(Number(ms) || 0, 30000)); },
  log(level, text) { console.error(`[${level}] ${text}`); },
  storage(op, key, value) {
    if (op === 'get') return storage.has(key) ? storage.get(key) : null;
    if (op === 'set') storage.set(key, value);
    if (op === 'remove') storage.delete(key);
    if (op === 'clear') storage.clear();
    return null;
  },
  cookie(op, url, value) {
    const target = url || pkg.manifest.metadata.source;
    if (op === 'get') return cookieFor(target);
    if (op === 'set') storeCookies(target, String(value || '').split(';').map((p) => p.trim()).filter(Boolean));
    return null;
  },
  browser() { return { error: 'Engine.newBrowser() cần app Lynxx (cửa sổ Chromium ẩn); CLI không có trình duyệt' }; },
  crypto: (op, a) => cryptoOp(op, a),
};

const ctx = { source: pkg.manifest.metadata.source, config, cache };
const runner = async (entry, a) => R.runScript({ files: pkg.files, entry, args: a, host, ctx });
const caps = capabilities(pkg.manifest, pkg.files);
const svc = createService({ manifest: pkg.manifest, capabilities: caps, runner });
const print = (v) => console.log(JSON.stringify(v, null, 2));

function check() {
  const problems = [];
  const warnings = [];
  const m = pkg.manifest.metadata;
  if (!m.source || !/^https?:\/\//.test(m.source)) problems.push('metadata.source phải là URL http(s) của trang');
  if (!m.version) warnings.push('metadata.version nên là số nguyên > 0 (dùng để báo có bản cập nhật)');
  if (m.encrypt) problems.push('metadata.encrypt = true: Lynxx không chạy extension mã hoá');
  if (!caps.content) problems.push(`metadata.type "${m.type}": Lynxx chỉ tải novel / comic / chinese_novel`);
  for (const key of ['detail', 'toc', 'chap']) if (!caps[key]) problems.push(`thiếu script "${key}" (bắt buộc để tải)`);
  if (!caps.search) warnings.push('không có script "search": người dùng chỉ mở được truyện bằng link hoặc home/genre');
  if (m.regexp) { try { new RegExp(m.regexp); } catch (err) { problems.push(`metadata.regexp không hợp lệ: ${err.message}`); } } else warnings.push('không có metadata.regexp: link dán vào Lynxx chỉ khớp theo host');
  for (const file of pkg.missing) problems.push(`plugin.json trỏ tới src/${file} nhưng file không có`);
  for (const [name, source] of Object.entries(pkg.files)) {
    try {
      // eslint-disable-next-line no-new-func
      new Function(R.inlineLoads(pkg.files, name));
    } catch (err) { problems.push(`src/${name}: lỗi cú pháp: ${err.message}`); }
    if (/\bJava\.type\b|\bjava\.|\bPackages\./.test(source)) problems.push(`src/${name}: dùng Java interop (không có trong Lynxx)`);
    if (/\bGraphics\./.test(source)) warnings.push(`src/${name}: dùng Graphics (Lynxx không hỗ trợ)`);
    if (/\bEngine\.newBrowser\b/.test(source)) warnings.push(`src/${name}: dùng Engine.newBrowser (chỉ chạy trong app, CLI không thử được)`);
    if (/\bfunction\s+execute\s*\(/.test(source) === false && Object.values(pkg.manifest.script).includes(name)) problems.push(`src/${name}: không có function execute(...)`);
  }
  const ok = !problems.length;
  print({ ok, id: pkg.id, name: m.name, version: m.version, type: m.type, capabilities: caps, problems, warnings });
  process.exitCode = ok ? 0 : 1;
}

function crc32(buf) {
  if (typeof zlib.crc32 === 'function') return zlib.crc32(buf) >>> 0;
  let c = ~0;
  for (let i = 0; i < buf.length; i++) { c ^= buf[i]; for (let k = 0; k < 8; k++) c = (c >>> 1) ^ (0xedb88320 & -(c & 1)); }
  return ~c >>> 0;
}

function pack(out) {
  const files = [['plugin.json', fs.readFileSync(path.join(pkg.root, 'plugin.json'))]];
  if (pkg.icon) files.push(['icon.png', fs.readFileSync(pkg.icon)]);
  for (const name of Object.keys(pkg.files).sort()) if (RE_SCRIPT_FILE.test(name)) files.push([`src/${name}`, fs.readFileSync(path.join(pkg.root, 'src', name))]);
  const local = [];
  const central = [];
  let offset = 0;
  for (const [name, data] of files) {
    const deflated = zlib.deflateRawSync(data, { level: 9 });
    const nameBuf = Buffer.from(name, 'utf8');
    const crc = crc32(data);
    const header = Buffer.alloc(30);
    header.writeUInt32LE(0x04034b50, 0); header.writeUInt16LE(20, 4); header.writeUInt16LE(0x0800, 6); header.writeUInt16LE(8, 8);
    header.writeUInt32LE(0, 10); header.writeUInt32LE(crc, 14); header.writeUInt32LE(deflated.length, 18); header.writeUInt32LE(data.length, 22);
    header.writeUInt16LE(nameBuf.length, 26); header.writeUInt16LE(0, 28);
    local.push(header, nameBuf, deflated);
    const c = Buffer.alloc(46);
    c.writeUInt32LE(0x02014b50, 0); c.writeUInt16LE(20, 4); c.writeUInt16LE(20, 6); c.writeUInt16LE(0x0800, 8); c.writeUInt16LE(8, 10);
    c.writeUInt32LE(0, 12); c.writeUInt32LE(crc, 16); c.writeUInt32LE(deflated.length, 20); c.writeUInt32LE(data.length, 24);
    c.writeUInt16LE(nameBuf.length, 28); c.writeUInt32LE(0, 30); c.writeUInt32LE(0, 34); c.writeUInt32LE(0, 38); c.writeUInt32LE(offset, 42);
    central.push(c, nameBuf);
    offset += header.length + nameBuf.length + deflated.length;
  }
  const centralBuf = Buffer.concat(central);
  const end = Buffer.alloc(22);
  end.writeUInt32LE(0x06054b50, 0); end.writeUInt16LE(files.length, 8); end.writeUInt16LE(files.length, 10);
  end.writeUInt32LE(centralBuf.length, 12); end.writeUInt32LE(offset, 16);
  const target = path.resolve(out || path.join(pkg.root, 'plugin.zip'));
  fs.writeFileSync(target, Buffer.concat([...local, centralBuf, end]));
  print({ ok: true, path: target, files: files.map(([n]) => n) });
}

(async () => {
  try {
    switch (command) {
      case 'check': check(); break;
      case 'home': print(await svc.home()); break;
      case 'genre': print(await svc.genre()); break;
      case 'search': print(await svc.search(args[0] || '', args[1] || '')); break;
      case 'list': print(await svc.list(args[0], args[1] || '', args[2] || '')); break;
      case 'detail': print(await svc.detail(args[0])); break;
      case 'toc': { const toc = await svc.toc(args[0]); print({ count: toc.length, first: toc.slice(0, 5), last: toc.slice(-3) }); break; }
      case 'chap': { const c = await svc.chap(args[0]); print(c.kind === 'comic' ? { kind: c.kind, count: c.images.length, images: c.images.slice(0, 10) } : { kind: c.kind, title: c.title, length: c.html.length, preview: c.html.slice(0, 600) }); break; }
      case 'run': print(await runner(args[0], args.slice(1))); break;
      case 'match': print({ url: args[0], matches: regexMatches(pkg.manifest, args[0] || '') }); break;
      case 'pack': pack(args[0]); break;
      default: usage(`unknown command ${command}`);
    }
    if (opts.verbose) console.error(`(${requests} request)`);
  } catch (err) {
    console.error(`error: ${err.message}`);
    if (opts.verbose && err.stack) console.error(err.stack);
    process.exitCode = 1;
  }
})();
