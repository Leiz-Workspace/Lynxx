// Electron end-to-end test of the book pipeline (dev only, needs a display and a Python with requirements.txt):
//   LYNXX_PYTHON=.venv/bin/python npx --prefix app electron app/test/electron-e2e.js
// A local HTTP server plays a comic site and a novel site; the real sidecar (python -m lynxx.server) runs on a scratch
// data folder. Steps: install two vBook extensions -> download jobs through the sandbox -> library rows and files ->
// reader data -> exports (EPUB 2/3, KEPUB, CBZ, image PDF with WebP pages = Chromium transcode, text PDF = printToPDF)
// -> "update" run finds the chapter the site published since and downloads only that one.
// The extensions' session normally refuses private addresses; this test lifts that filter for 127.0.0.1 only.
'use strict';

const fs = require('node:fs');
const os = require('node:os');
const path = require('node:path');
const http = require('node:http');
const zlib = require('node:zlib');
const readline = require('node:readline');
const { spawn } = require('node:child_process');
const { app, protocol, net, BrowserWindow } = require('electron');

const REPO = path.resolve(__dirname, '..', '..');
const scratch = fs.mkdtempSync(path.join(os.tmpdir(), 'lynxx-e2e-'));
app.setPath('userData', scratch);
protocol.registerSchemesAsPrivileged([{ scheme: 'lynxx-lib', privileges: { standard: true, secure: true, supportFetchAPI: true, stream: true } }]);

const results = [];
const check = (name, ok, detail = '') => { results.push(Boolean(ok)); console.log(`${ok ? 'ok' : 'not ok'} - ${name}${detail ? ` (${detail})` : ''}`); };

// ---------------------------------------------------------------- fake sites
function png(w, h, rgb) {
  const raw = Buffer.alloc((w * 3 + 1) * h);
  for (let y = 0; y < h; y++) for (let x = 0; x < w; x++) rgb.forEach((v, i) => { raw[y * (w * 3 + 1) + 1 + x * 3 + i] = v; });
  const chunk = (type, data) => { const len = Buffer.alloc(4); len.writeUInt32BE(data.length); const body = Buffer.concat([Buffer.from(type), data]); const crc = Buffer.alloc(4); crc.writeUInt32BE(zlib.crc32(body) >>> 0); return Buffer.concat([len, body, crc]); };
  const ihdr = Buffer.alloc(13); ihdr.writeUInt32BE(w, 0); ihdr.writeUInt32BE(h, 4); ihdr[8] = 8; ihdr[9] = 2;
  return Buffer.concat([Buffer.from([0x89, 0x50, 0x4e, 0x47, 0x0d, 0x0a, 0x1a, 0x0a]), chunk('IHDR', ihdr), chunk('IDAT', zlib.deflateSync(raw)), chunk('IEND', Buffer.alloc(0))]);
}
let WEBP = null;
let comicChapters = 2;
const requests = [];
function site(req, res) {
  const url = new URL(req.url, 'http://127.0.0.1');
  requests.push(url.pathname);
  const send = (type, body, status = 200) => { res.writeHead(status, { 'content-type': type, 'set-cookie': 'sid=abc; Path=/' }); res.end(body); };
  if (url.pathname === '/comic/book') {
    const list = Array.from({ length: comicChapters }, (_, i) => `<li><a href="/comic/book/c${comicChapters - i}">Chương ${comicChapters - i}</a></li>`).join('');
    return send('text/html; charset=utf-8', `<h1>Truyện Tranh E2E</h1><div class="cover"><img src="/img/cover.png"></div><p class="author">Tác Giả</p><div class="desc"><p>Giới thiệu truyện.</p></div><ul class="chapters">${list}</ul>`);
  }
  let m = /^\/comic\/book\/c(\d+)$/.exec(url.pathname);
  if (m) return send('text/html', `<div class="pages"><img data-src="/img/${m[1]}-1.png"><img src="/img/${m[1]}-2.webp"><img src="/img/missing.png" data-cdn="/img/${m[1]}-3.png"></div>`);
  if (url.pathname === '/img/cover.png') return send('image/png', png(30, 40, [20, 120, 200]));
  if (url.pathname === '/img/missing.png') return send('text/plain', 'gone', 404);
  m = /^\/img\/(\d+)-(\d)\.(png|webp)$/.exec(url.pathname);
  if (m) {
    if (!req.headers.referer || !req.headers.referer.includes('/comic/book/c')) return send('text/plain', 'hotlink', 403);
    return m[3] === 'webp' ? send('image/webp', WEBP) : send('image/png', png(60, 90, [200, 40 * Number(m[1]), 40]));
  }
  if (url.pathname === '/novel/book') return send('text/html; charset=utf-8', '<h1>Truyện Chữ E2E</h1><div class="desc">Mô tả.</div><ul><li><a href="/novel/book/c1">Chương 1</a></li><li><a href="/novel/book/c2">Chương 2</a></li></ul>');
  m = /^\/novel\/book\/c(\d+)$/.exec(url.pathname);
  if (m) return send('text/html; charset=utf-8', `<div id="content">Đoạn một của chương ${m[1]}.<br>Đoạn hai <b>đậm</b>.<script>alert(1)</script><img src="/img/cover.png"></div>`);
  return send('text/plain', 'not found', 404);
}

function writeExtension(dir, name, type, base) {
  fs.mkdirSync(path.join(dir, 'src'), { recursive: true });
  fs.writeFileSync(path.join(dir, 'plugin.json'), JSON.stringify({ metadata: { name, author: 'e2e', version: 1, source: base, regexp: `${base.replace(/[.:/]/g, (c) => `\\${c}`)}/${type}/book/?`, type }, script: { detail: 'detail.js', toc: 'toc.js', chap: 'chap.js' } }));
  fs.writeFileSync(path.join(dir, 'src', 'detail.js'), `function execute(url) { var d = fetch(url).html(); return Response.success({ name: d.select("h1").text(), cover: d.select(".cover img").attr("src"), author: d.select(".author").text(), description: d.select(".desc").html(), host: "${base}" }); }`);
  fs.writeFileSync(path.join(dir, 'src', 'toc.js'), `function execute(url) { var d = fetch(url).html(); var el = d.select("li a"); var out = []; for (var i = el.size() - 1; i >= 0; i--) out.push({ name: el.get(i).text(), url: el.get(i).attr("href"), host: "${base}" }); if (out.length && out[0].name > out[out.length - 1].name) out.reverse(); return Response.success(out); }`);
  fs.writeFileSync(path.join(dir, 'src', 'chap.js'), type === 'comic'
    ? 'function execute(url) { var d = fetch(url).html(); var out = []; d.select(".pages img").forEach(function (e) { var cdn = e.attr("data-cdn"); out.push({ link: e.attr("data-src") || e.attr("src"), fallback: cdn ? [cdn] : [] }); }); return Response.success(out); }'
    : 'function execute(url) { return Response.success(fetch(url).html().select("#content").html()); }');
}

// ---------------------------------------------------------------- sidecar client
function startSidecar() {
  const python = process.env.LYNXX_PYTHON || 'python3';
  const proc = spawn(python, ['-m', 'lynxx.server'], { cwd: REPO, env: { ...process.env, PYTHONPATH: REPO, LYNXX_DATA_DIR: scratch, PYTHONUNBUFFERED: '1', LYNXX_CATALOG_AUTOLOAD: '0' }, stdio: ['pipe', 'pipe', 'pipe'] });
  const pending = new Map();
  let id = 0;
  let readyResolve;
  const ready = new Promise((ok) => { readyResolve = ok; });
  readline.createInterface({ input: proc.stdout }).on('line', (line) => {
    let msg; try { msg = JSON.parse(line); } catch { return; }
    if (msg.method === 'ready') readyResolve();
    if (msg.id && pending.has(msg.id)) {
      const { resolve, reject } = pending.get(msg.id);
      pending.delete(msg.id);
      if (msg.error) reject(Object.assign(new Error(msg.error.message), { code: msg.error.code, data: msg.error.data })); else resolve(msg.result);
    }
  });
  proc.stderr.on('data', (d) => { if (process.env.E2E_VERBOSE) process.stderr.write(d); });
  return {
    ready, proc,
    call: async (method, params = {}) => { await ready; return new Promise((resolve, reject) => { const n = ++id; pending.set(n, { resolve, reject }); proc.stdin.write(`${JSON.stringify({ jsonrpc: '2.0', id: n, method, params })}\n`); }); },
    stop: () => { try { proc.stdin.write('{"jsonrpc":"2.0","method":"app.shutdown"}\n'); } catch { /* gone */ } setTimeout(() => proc.kill(), 1500); },
  };
}

app.on('window-all-closed', () => { /* helper windows come and go: the test decides when to exit */ });
app.whenReady().then(async () => {
  let code = 1;
  const server = http.createServer(site);
  await new Promise((ok) => server.listen(0, '127.0.0.1', ok));
  const base = `http://127.0.0.1:${server.address().port}`;
  const sidecar = startSidecar();
  try {
    const win = new BrowserWindow({ show: false, webPreferences: { offscreen: true } });
    await win.loadURL('about:blank');
    WEBP = Buffer.from((await win.webContents.executeJavaScript(`(() => { const c = document.createElement('canvas'); c.width = 60; c.height = 90; const g = c.getContext('2d'); g.fillStyle = '#3a6'; g.fillRect(0, 0, 60, 90); return c.toDataURL('image/webp'); })()`)).split(',')[1], 'base64');
    win.destroy();
    await Promise.race([sidecar.ready, new Promise((_, ko) => setTimeout(() => ko(new Error('sidecar did not start')), 60000))]);
    check('sidecar ready', true);

    const handlers = new Map();
    const events = [];
    const { setupReader, registerLibProtocol } = require('../plugin/ipc.js');
    registerLibProtocol(scratch);
    const reader = setupReader({
      handle: (ch, fn) => handlers.set(ch, fn), sidecar, send: (ch, data) => events.push([ch, data]), approvedPaths: new Set(), userData: scratch,
      assertPublicHost: async (u) => { if (!u.startsWith(base) && !/^https?:\/\//.test(u)) throw new Error('blocked'); },
      isDev: true, confirm: async () => true, getWindow: () => null, browserUA: (ua) => ua, log: () => {}, notifyBook: () => {},
    });
    reader.host.pluginSession.webRequest.onBeforeRequest(null); // test only: the fake sites live on 127.0.0.1
    const call = (ch, ...args) => handlers.get(ch)({}, ...args);

    const comicExt = await reader.host.installFromDir((() => { const d = path.join(scratch, 'src-comic'); writeExtension(d, 'Comic E2E', 'comic', base); return d; })());
    const novelExt = await reader.host.installFromDir((() => { const d = path.join(scratch, 'src-novel'); writeExtension(d, 'Novel E2E', 'novel', base); return d; })());
    const matched = await call('plugins:match', `${base}/comic/book`);
    check('plugins:match routes the book URL', matched[0]?.id === comicExt.id);

    const waitJob = async (id) => {
      for (let i = 0; i < 600; i++) {
        const j = (await call('book:jobs')).find((x) => x.id === id);
        if (['done', 'failed', 'cancelled'].includes(j.status)) return j;
        await new Promise((ok) => setTimeout(ok, 100));
      }
      throw new Error('job timeout');
    };
    const job = await waitJob((await call('book:download', { pluginId: comicExt.id, url: `${base}/comic/book`, scope: 'all' })).id);
    check('comic job done', job.status === 'done' && job.progress.chaptersDone === 2, `${job.status} ${JSON.stringify(job.progress)} ${job.error} ${job.log.join(' | ')}`);
    const shelf = await call('library:collections');
    const comic = shelf.find((c) => c.pluginId === comicExt.id);
    check('library lists the comic with its cover', comic && comic.ready === 2 && comic.coverUrl.startsWith('lynxx-lib://'));
    const detail = await call('library:collection', comic.id);
    check('chapters in reading order', detail.chapters.map((c) => c.title).join(',') === 'Chương 1,Chương 2' && detail.chapters.every((c) => c.pages === 3));
    const read = await call('reader:chapter', detail.chapters[0].id);
    const page = await net.fetch(read.pages[1].url);
    check('reader pages served by lynxx-lib (webp kept as downloaded)', read.kind === 'images' && page.status === 200 && page.headers.get('content-type') === 'image/webp');
    check('referer sent / fallback used for a missing page', requests.includes('/img/1-3.png') && requests.includes('/img/missing.png'));

    const out = path.join(scratch, 'out');
    const ex = async (format, options = {}) => reader.exportCollection({ collectionId: comic.id, format, options, output: path.join(out, `comic-${format}.${format === 'kepub' ? 'kepub.epub' : format === 'cbz' ? 'cbz' : format === 'pdf' ? 'pdf' : 'epub'}`) });
    const e2 = await ex('epub2');
    check('export EPUB 2', e2.chapters === 2 && fs.readFileSync(e2.path).subarray(30, 38).toString() === 'mimetype');
    check('export EPUB 3', (await ex('epub3')).chapters === 2);
    check('export KEPUB', (await ex('kepub')).chapters === 2);
    check('export CBZ', fs.statSync((await ex('cbz')).path).size > 0);
    const pdf = await ex('pdf');
    const pdfBytes = fs.readFileSync(pdf.path);
    check('export image PDF (WebP pages transcoded)', pdfBytes.subarray(0, 5).toString() === '%PDF-' && (pdfBytes.toString('latin1').match(/\/Type \/Page /g) || []).length === 7, `${pdfBytes.length} bytes`);

    // the site publishes chapter 3: an update downloads only it
    comicChapters = 3;
    const before = requests.filter((r) => /^\/comic\/book\/c\d$/.test(r)).length;
    const upd = await waitJob((await call('library:update', comic.id)).id);
    const chapterPages = requests.filter((r) => /^\/comic\/book\/c\d$/.test(r)).slice(before);
    check('update downloads only the new chapter', upd.status === 'done' && upd.progress.chaptersDone === 1 && chapterPages.join() === '/comic/book/c3', chapterPages.join());
    const refreshed = await call('library:exportRefresh', comic.id, (await call('library:collection', comic.id)).exports.find((x) => x.format === 'epub2').id);
    check('refresh an export with the new chapter', refreshed.chapters === 3);

    const njob = await waitJob((await call('book:download', { pluginId: novelExt.id, url: `${base}/novel/book`, scope: 'all' })).id);
    check('novel job done', njob.status === 'done' && njob.progress.chaptersDone === 2, njob.error);
    const novel = (await call('library:collections')).find((c) => c.pluginId === novelExt.id);
    const nd = await call('library:collection', novel.id);
    const text = await call('reader:chapter', nd.chapters[0].id);
    check('novel chapter sanitized, image local', text.kind === 'text' && !/script/.test(text.html) && /lynxx-lib:\/\//.test(text.html) && /Đoạn hai/.test(text.html));
    const npdf = await reader.exportCollection({ collectionId: novel.id, format: 'pdf', options: {}, output: path.join(out, 'novel.pdf') });
    check('text PDF via printToPDF, recorded as an export', fs.readFileSync(npdf.path).subarray(0, 5).toString() === '%PDF-' && npdf.chapters === 2);
    await call('reader:progressSet', { chapterId: nd.chapters[1].id, page: 0, scrollOffset: 0.4, mode: 'vertical', direction: 'ltr' });
    check('reading progress saved', (await call('library:collections')).find((c) => c.id === novel.id).lastChapterId === nd.chapters[1].id);
    check('jobs emitted progress events', events.some(([ch, d]) => ch === 'book:update' && d.progress.phase === 'chapters'));
    reader.shutdown();
    code = results.every(Boolean) ? 0 : 1;
  } catch (err) {
    console.error(err.stack || err);
    code = 1;
  } finally {
    console.log(`${results.filter(Boolean).length}/${results.length} passed`);
    sidecar.stop();
    server.close();
    setTimeout(() => { try { fs.rmSync(scratch, { recursive: true, force: true }); } catch { /* ignore */ } app.exit(code); }, 1800);
  }
});
