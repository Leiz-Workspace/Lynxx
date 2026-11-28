// Electron smoke test of the extension host, sandbox bridge and export helpers (dev only, needs a display):
//   npx --prefix app electron app/test/electron-smoke.js [--offline]
// Installs a small vBook extension into a scratch profile, runs its scripts in the real sandbox (sync bridge: HTTP to
// example.com unless --offline, sleep, storage, cookies, crypto), then checks printToPDF, the WebP -> JPEG transcoder and
// the lynxx-lib:// protocol. Exit code 0 = everything passed.
'use strict';

const fs = require('node:fs');
const os = require('node:os');
const path = require('node:path');
const { app, protocol, net, BrowserWindow } = require('electron');

const scratch = fs.mkdtempSync(path.join(os.tmpdir(), 'lynxx-smoke-'));
app.setPath('userData', scratch);
protocol.registerSchemesAsPrivileged([{ scheme: 'lynxx-lib', privileges: { standard: true, secure: true, supportFetchAPI: true, stream: true } }]);
const offline = process.argv.includes('--offline');

const results = [];
const check = (name, ok, detail = '') => { results.push({ name, ok: Boolean(ok), detail }); console.log(`${ok ? 'ok' : 'not ok'} - ${name}${detail ? ` (${detail})` : ''}`); };

function writeExtension(dir) {
  fs.mkdirSync(path.join(dir, 'src'), { recursive: true });
  fs.writeFileSync(path.join(dir, 'plugin.json'), JSON.stringify({
    metadata: { name: 'Smoke', author: 'lynxx', version: 1, source: 'https://example.com', regexp: 'example\\.com/?', type: 'novel' },
    script: { detail: 'detail.js', toc: 'toc.js', chap: 'chap.js' },
    config: { DOMAIN: { title: 'Domain', default: 'https://example.com', mode: 'input' } },
  }));
  fs.writeFileSync(path.join(dir, 'src', 'config.js'), 'var BASE = "https://example.com"; try { if (DOMAIN) BASE = DOMAIN; } catch (e) {}');
  fs.writeFileSync(path.join(dir, 'src', 'detail.js'), `load('config.js');
function execute(url) {
  var r = fetch(url);
  var doc = r.ok ? r.html() : Html.parse('<title>offline</title><p>offline</p>');
  return Response.success({ name: doc.select('title').text(), description: doc.select('p').first().text(), host: BASE });
}`);
  fs.writeFileSync(path.join(dir, 'src', 'toc.js'), 'function execute(url) { return Response.success([{ name: "Chương 1", url: url + "/c1" }]); }');
  fs.writeFileSync(path.join(dir, 'src', 'chap.js'), `load('crypto.js');
function execute(url) {
  var t0 = Date.now(); sleep(120); var slept = Date.now() - t0;
  localStorage.setItem('k', 'v');
  var md5 = CryptoJS.MD5('abc').toString();
  var enc = CryptoJS.AES.encrypt('chào', 'pass').toString();
  var dec = CryptoJS.AES.decrypt(enc, 'pass').toString(CryptoJS.enc.Utf8);
  return Response.success('<p>' + [slept >= 100, localStorage.getItem('k'), md5, dec, typeof localCookie.getCookie()].join('|') + '</p>');
}`);
}

app.on('window-all-closed', () => { /* helper windows come and go: the test decides when to exit */ });
app.whenReady().then(async () => {
  let code = 1;
  try {
    const { PluginHost } = require('../plugin/host.js');
    const ipc = require('../plugin/ipc.js');
    const host = new PluginHost({
      dataDir: scratch, isDev: true, assertPublicHost: async (u) => { if (!/^https?:\/\//.test(u)) throw new Error('scheme'); },
      userAgent: (ses) => ses.getUserAgent().replace(/\s(Electron|lynxx-desktop)\/\S+/g, ''), unpackZip: async () => { throw new Error('no zip in smoke test'); },
      log: (t) => console.log(`  log: ${t}`),
    });
    host.init();
    const extDir = path.join(scratch, 'ext-src');
    writeExtension(extDir);
    const info = await host.installFromDir(extDir, { origin: 'folder' });
    check('install from folder', info.id && info.capabilities.downloadable, info.id);
    const { svc } = host.service(info.id);
    const detail = await svc.detail(offline ? 'https://example.invalid' : 'https://example.com');
    check('sandbox detail + sync fetch', offline ? detail.name === 'offline' : /Example Domain/.test(detail.name), detail.name);
    const toc = await svc.toc('https://example.com');
    check('toc', toc.length === 1 && toc[0].url === 'https://example.com/c1');
    const chap = await svc.chap('https://example.com/c1');
    const [slept, stored, md5, dec, cookieType] = chap.html.replace(/<\/?p>/g, '').split('|');
    check('sleep bridge', slept === 'true');
    check('storage bridge', stored === 'v');
    check('crypto bridge', md5 === '900150983cd24fb0d6963f7d28e17f72' && dec === 'chào', `${md5} ${dec}`);
    check('cookie bridge', cookieType === 'string');
    const loop = await host.run(info.id, 'toc.js', ['x'], { timeoutMs: 3000 });
    check('run returns', loop.ok);
    fs.writeFileSync(path.join(host.installedDir, info.id, 'src', 'spin.js'), 'function execute() { for (;;) {} }');
    host.cache.delete(info.id);
    const spins = await Promise.all([1, 2, 3, 4].map(() => host.run(info.id, 'spin.js', [], { timeoutMs: 1500 })));
    const after = await host.run(info.id, 'toc.js', ['y'], { timeoutMs: 5000 });
    check('endless scripts time out, the next run still works', spins.every((r) => !r.ok && /giây/.test(r.error)) && after.ok, spins.map((r) => r.error).join(' / '));

    // export helpers
    const html = path.join(scratch, 'book.html');
    fs.writeFileSync(html, '<!doctype html><meta charset="utf-8"><h1 id="a">Chương 1</h1><p><a href="#b">tới chương 2</a></p><h1 id="b" style="break-before:page">Chương 2</h1><p>Tiếng Việt có dấu</p>');
    const pdf = path.join(scratch, 'book.pdf');
    await ipc._printToPdf(html, pdf, 'Smoke');
    const head = fs.readFileSync(pdf).subarray(0, 5).toString('latin1');
    check('printToPDF', head === '%PDF-', `${fs.statSync(pdf).size} bytes`);
    const win = new BrowserWindow({ show: false, webPreferences: { offscreen: true } });
    await win.loadURL('about:blank');
    const webp = await win.webContents.executeJavaScript(`(() => { const c = document.createElement('canvas'); c.width = 40; c.height = 30; const g = c.getContext('2d'); g.fillStyle = '#c33'; g.fillRect(0, 0, 40, 30); return c.toDataURL('image/webp'); })()`);
    win.destroy();
    const webpFile = path.join(scratch, 'p.webp');
    fs.writeFileSync(webpFile, Buffer.from(webp.split(',')[1], 'base64'));
    const t = await ipc._transcodeImages([webpFile]);
    const jpg = fs.readFileSync(t.map[webpFile]);
    check('WebP -> JPEG transcode', jpg[0] === 0xff && jpg[1] === 0xd8, `${jpg.length} bytes`);
    await t.cleanup();
    // lynxx-lib:// serves images inside the library only
    ipc.registerLibProtocol(scratch);
    const libFile = path.join(scratch, 'library', 'collections', 'c1', 'cover.jpg');
    fs.mkdirSync(path.dirname(libFile), { recursive: true });
    fs.writeFileSync(libFile, jpg);
    const okRes = await net.fetch('lynxx-lib://lib/collections/c1/cover.jpg');
    check('lynxx-lib serves a library image', okRes.status === 200 && (await okRes.arrayBuffer()).byteLength === jpg.length);
    const bad = await net.fetch('lynxx-lib://lib/../plugins/state.json');
    check('lynxx-lib refuses paths outside the library', bad.status !== 200, String(bad.status));
    host.shutdown();
    code = results.every((r) => r.ok) ? 0 : 1;
  } catch (err) {
    console.error(err.stack || err);
    code = 1;
  } finally {
    console.log(`${results.filter((r) => r.ok).length}/${results.length} passed`);
    try { fs.rmSync(scratch, { recursive: true, force: true }); } catch { /* ignore */ }
    app.exit(code);
  }
});
