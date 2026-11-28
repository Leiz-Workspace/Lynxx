// node app/test/plugin-runtime.test.js — extension runtime compatibility (html parser, selectors, API, CryptoJS, service)
'use strict';

const assert = require('assert');
const crypto = require('crypto');
const H = require('../plugin/html.js');
const R = require('../plugin/runtime.js');
const { cryptoOp } = require('../plugin/host-crypto.js');
const { createService, htmlToText } = require('../plugin/service.js');
const { normalizeManifest, regexMatches, pluginId, capabilities } = require('../plugin/package.js');

let failures = 0;
function test(name, fn) {
  Promise.resolve().then(fn).then(() => console.log(`ok - ${name}`), (err) => { failures += 1; console.error(`not ok - ${name}\n${err.stack}`); });
}

const PAGES = {
  'https://site.test/book/1': `<html><head><title>Book</title></head><body>
    <h1 class="title">Truyện &amp; Thử</h1><div class="info"><p>Tác giả: <a>Ai Đó</a></p><p class="status">Đang ra</p></div>
    <div class="cover"><img data-src="//cdn.site.test/c.jpg"></div><div class="desc"><p>Dòng 1</p><p>Dòng <b>2</b></p></div>
    <ul class="list"><li><a href="/book/1/c2">Chương 2</a></li><li><a href="/book/1/c1">Chương 1</a></li><li><a href="/book/1/c1">dup</a></li></ul>
    <div class="pagination"><a href="?p=1">1</a><a class="current">2</a><a href="?p=3">3</a></div></body></html>`,
  'https://site.test/book/1/c1': '<div id="pages"><img src="/i/1.jpg"><img data-original="https://img.site.test/2.png" src="/x.gif"></div>',
  'https://site.test/novel/c1': '<div id="content">Xin chào<br>thế giới<script>alert(1)</script></div>',
};

function fakeHost(log = []) {
  return {
    requests: [],
    request(req) {
      this.requests.push(req);
      const url = req.url.split('?')[0];
      const page = PAGES[url];
      if (!page) return { ok: false, status: 404, url: req.url, headers: {}, body: '', requestHeaders: { cookie: 'a=1' } };
      return { ok: true, status: 200, url: req.url, headers: { 'content-type': 'text/html; charset=utf-8' }, body: Buffer.from(page).toString('base64'), requestHeaders: { cookie: 'a=1' } };
    },
    sleep() {}, log: (level, text) => log.push(`${level}:${text}`),
    storage: (() => { const m = new Map(); return (op, k, v) => { if (op === 'get') return m.get(k) ?? null; if (op === 'set') m.set(k, v); if (op === 'remove') m.delete(k); if (op === 'clear') m.clear(); return null; }; })(),
    cookie: () => '', browser: () => ({ id: 1, html: '', url: '' }), crypto: cryptoOp,
  };
}

const run = (files, entry, args, ctx = {}, host = fakeHost()) => R.runScript({ files, entry, args, host, ctx: { source: 'https://site.test', ...ctx } });

test('parser + jsoup selectors', () => {
  const doc = H.parse(PAGES['https://site.test/book/1'], 'https://site.test/book/1');
  assert.deepStrictEqual(H.select(doc, '.list li a').map((e) => H.textOf(e)), ['Chương 2', 'Chương 1', 'dup']);
  assert.strictEqual(H.select(doc, '.info p:contains(tác giả) a').length, 1);
  assert.strictEqual(H.textOf(H.select(doc, '.pagination a.current + a')[0]), '3');
  assert.strictEqual(H.select(doc, 'a[href~=c\\d$]').length, 3);
  assert.strictEqual(H.select(doc, '.list li:eq(1) a').length, 1);
  assert.strictEqual(H.select(doc, 'div:has(> img)').length, 1);
  assert.throws(() => H.select(doc, 'a:visible'), /unsupported/);
});

test('detail script: Http builder, empty first(), abs urls, Response.success', () => {
  const files = {
    'config.js': 'var BASE_URL = "https://site.test"; try { if (DOMAIN) BASE_URL = DOMAIN; } catch (e) {}',
    'detail.js': `load('config.js');
      function execute(url) {
        let doc = Http.get(url).html();
        let missing = doc.select(".nope").first();
        return Response.success({
          name: doc.select("h1.title").text(), author: doc.select(".info p:contains(Tác giả) a").first().text(),
          cover: doc.select(".cover img").attr("data-src"), description: doc.select(".desc").html(),
          ongoing: doc.select(".status").text().indexOf("Đang") >= 0, host: BASE_URL,
          extra: missing.text() + "|" + missing.attr("x") + "|" + String(doc.select(".nope")), abs: doc.select(".list a").first().attr("abs:href"),
        });
      }`,
  };
  const r = run(files, 'detail.js', ['https://site.test/book/1'], { config: { DOMAIN: 'https://site.test' } });
  assert.ok(r.ok, r.error);
  assert.strictEqual(r.data.name, 'Truyện & Thử');
  assert.strictEqual(r.data.author, 'Ai Đó');
  assert.strictEqual(r.data.extra, '||');
  assert.strictEqual(r.data.abs, 'https://site.test/book/1/c2');
  assert.strictEqual(r.data.host, 'https://site.test');
});

test('fetch never throws; 404 gives ok=false; POST params become a form body', () => {
  const host = fakeHost();
  const files = { 's.js': `function execute(k, page) {
      var r = fetch("https://site.test/missing");
      var p = Http.post("https://site.test/book/1").params({ q: k }).html();
      return Response.success([{ ok: r.ok, status: r.status, title: p.select("title").text(), cookie: r.request.headers.cookie }], page || "");
    }` };
  const r = run(files, 's.js', ['tìm', ''], {}, host);
  assert.ok(r.ok, r.error);
  assert.deepStrictEqual(r.data[0], { ok: false, status: 404, title: 'Book', cookie: 'a=1' });
  const post = host.requests.find((q) => q.method === 'POST');
  assert.strictEqual(post.body, 'q=t%C3%ACm');
  assert.strictEqual(r.next, '');
});

test('errors are reported, not thrown', () => {
  const r = run({ 'x.js': 'function execute() { return null.foo; }' }, 'x.js', []);
  assert.strictEqual(r.ok, false);
  assert.match(r.error, /null/);
  const n = run({ 'y.js': 'function execute() { return null; }' }, 'y.js', []);
  assert.strictEqual(n.ok, false);
  const e = run({ 'z.js': 'function execute() { return Response.error("Không tìm thấy"); }' }, 'z.js', []);
  assert.deepStrictEqual([e.ok, e.error], [false, 'Không tìm thấy']);
});

test('shadowing globals and config constants is legal', () => {
  const r = run({ 'a.js': 'let fetch = 1; let DOMAIN = "x"; function execute() { return Response.success(fetch + DOMAIN); }' }, 'a.js', [], { config: { DOMAIN: 'https://other.test' } });
  assert.deepStrictEqual([r.ok, r.data], [true, '1x']);
});

test('CryptoJS facade matches Node crypto (passphrase AES, keyed AES-CBC, hashes)', () => {
  // OpenSSL-style passphrase encryption (what CryptoJS.AES.encrypt(text, "pass") produces)
  const salt = Buffer.from('0102030405060708', 'hex');
  const pass = Buffer.from('bí mật');
  let block = Buffer.alloc(0); let derived = Buffer.alloc(0);
  while (derived.length < 48) { block = crypto.createHash('md5').update(Buffer.concat([block, pass, salt])).digest(); derived = Buffer.concat([derived, block]); }
  const c = crypto.createCipheriv('aes-256-cbc', derived.subarray(0, 32), derived.subarray(32, 48));
  const ct = Buffer.concat([Buffer.from('Salted__'), salt, c.update('Nội dung chương'), c.final()]).toString('base64');
  const key = crypto.randomBytes(16); const iv = crypto.randomBytes(16);
  const c2 = crypto.createCipheriv('aes-128-cbc', key, iv);
  const ct2 = Buffer.concat([c2.update('{"a":1}'), c2.final()]).toString('base64');
  const files = { 'c.js': `load('crypto.js');
    function execute(ct, ct2, key, iv) {
      var a = CryptoJS.AES.decrypt(ct, "bí mật").toString(CryptoJS.enc.Utf8);
      var b = CryptoJS.AES.decrypt(ct2, CryptoJS.enc.Hex.parse(key), { iv: CryptoJS.enc.Hex.parse(iv), mode: CryptoJS.mode.CBC, padding: CryptoJS.pad.Pkcs7 }).toString(CryptoJS.enc.Utf8);
      var round = CryptoJS.AES.decrypt(CryptoJS.AES.encrypt("vòng", "k").toString(), "k").toString(CryptoJS.enc.Utf8);
      return Response.success([a, b, round, CryptoJS.MD5("abc").toString(), CryptoJS.SHA256("abc").toString(CryptoJS.enc.Base64), CryptoJS.HmacSHA256("m", "k").toString()]);
    }` };
  const r = run(files, 'c.js', [ct, ct2, key.toString('hex'), iv.toString('hex')]);
  assert.ok(r.ok, r.error);
  assert.strictEqual(r.data[0], 'Nội dung chương');
  assert.strictEqual(r.data[1], '{"a":1}');
  assert.strictEqual(r.data[2], 'vòng');
  assert.strictEqual(r.data[3], crypto.createHash('md5').update('abc').digest('hex'));
  assert.strictEqual(r.data[4], crypto.createHash('sha256').update('abc').digest('base64'));
  assert.strictEqual(r.data[5], crypto.createHmac('sha256', 'k').update('m').digest('hex'));
});

// headers only: what Graphics.createImage reads (the pixels are drawn by the app, app/plugin/graphics.js)
const IMG = {
  png: (w, h) => { const b = Buffer.alloc(33); Buffer.from([0x89, 0x50, 0x4e, 0x47, 0x0d, 0x0a, 0x1a, 0x0a, 0, 0, 0, 13]).copy(b); b.write('IHDR', 12, 'latin1'); b.writeUInt32BE(w, 16); b.writeUInt32BE(h, 20); return b; },
  jpeg: (w, h) => Buffer.concat([Buffer.from([0xff, 0xd8, 0xff, 0xe0, 0x00, 0x10]), Buffer.from('JFIF\0\x01\x01\0\0\x01\0\x01\0\0', 'latin1'),
    Buffer.from([0xff, 0xc0, 0x00, 0x11, 0x08, h >> 8, h & 255, w >> 8, w & 255, 3, 1, 0x22, 0, 2, 0x11, 1, 3, 0x11, 1])]),
  webp: (w, h) => { const b = Buffer.alloc(30); b.write('RIFF', 0, 'latin1'); b.write('WEBPVP8X', 8, 'latin1'); b.writeUInt32LE(10, 16); b.writeUIntLE(w - 1, 24, 3); b.writeUIntLE(h - 1, 27, 3); return b; },
  gif: (w, h) => { const b = Buffer.alloc(13); b.write('GIF89a', 0, 'latin1'); b.writeUInt16LE(w, 6); b.writeUInt16LE(h, 8); return b; },
};

test('Graphics: an extension that redraws images is downloadable', () => {
  const manifest = normalizeManifest({ metadata: { name: 'X', source: 'https://x.test', type: 'comic' }, script: { detail: 'detail.js', toc: 'toc.js', chap: 'chap.js' } });
  const files = { 'detail.js': 'function execute(u) {}', 'toc.js': 'function execute(u) {}', 'chap.js': 'function execute(u) {}' };
  const caps = capabilities(manifest, { ...files, 'image.js': 'var c = Graphics.createCanvas(w, h);' });
  assert.strictEqual(caps.graphics, true);
  assert.strictEqual(caps.downloadable, true);
});

test('Graphics: image sizes from PNG / JPEG / WebP / GIF headers', () => {
  for (const [type, make] of Object.entries(IMG)) assert.deepStrictEqual(R.imageSize(new Uint8Array(make(721, 1043))), { width: 721, height: 1043, type });
  assert.strictEqual(R.imageSize(new Uint8Array(Buffer.from('<html>'))), null);
});

test('Graphics: an image script puts scrambled strips back (canvas recorded, drawn by the host)', () => {
  // the shape of a comic site's image script: fetch the scrambled page, move horizontal strips, return capture()
  const image = 'function execute(url) { var parts = url.split(" "); var r = fetch(parts[0]); if (!r.ok) return null;'
    + ' var img = Graphics.createImage(r.base64()); var c = Graphics.createCanvas(img.width, img.height);'
    + ' JSON.parse(parts[1]).forEach(function (p) { c.drawImage(img, p[0], p[1], img.width, p[3], p[4], p[5], img.width, p[7]); });'
    + ' c.drawImage(img, 0, 0); c.drawImage(img, 1, 2, 3, 4); return c.capture(); }';
  const jpeg = IMG.jpeg(800, 1200).toString('base64');
  const host = fakeHost();
  const calls = [];
  host.request = (req) => ({ ok: true, status: 200, url: req.url, headers: { 'content-type': 'image/jpeg' }, body: jpeg });
  host.graphics = (p) => { calls.push(p); return { data: 'RESULT', type: 'image/jpeg' }; };
  const strips = [[0, 600, 800, 600, 0, 0, 800, 600], [0, 0, 800, 600, 0, 600, 800, 600]];
  const r = run({ 'image.js': image }, 'image.js', [`https://img.test/p1.jpg ${JSON.stringify(strips)}`], {}, host);
  assert.ok(r.ok, r.error);
  assert.strictEqual(r.data, 'RESULT');
  assert.strictEqual(calls.length, 1); // the size came from the header: one host call, the capture
  assert.deepStrictEqual({ ...calls[0], images: calls[0].images.length }, {
    op: 'compose', width: 800, height: 1200, images: 1, type: '', quality: 0,
    draws: [[0, 0, 600, 800, 600, 0, 0, 800, 600], [0, 0, 0, 800, 600, 0, 600, 800, 600], [0, 0, 0, 800, 1200, 0, 0, 800, 1200], [0, 0, 0, 800, 1200, 1, 2, 3, 4]],
  });
  assert.strictEqual(calls[0].images[0], jpeg);
});

test('Graphics: wrong input is an error of the script, not a crash', () => {
  const host = fakeHost();
  host.graphics = (p) => (p.op === 'size' ? { error: 'not decodable' } : { error: 'boom' });
  const bad = (src) => run({ 'a.js': `function execute() { ${src} }` }, 'a.js', [], {}, host);
  assert.match(bad('Graphics.createImage("PGh0bWw+"); return 1;').error, /not an image \(not decodable\)/);
  assert.match(bad('Graphics.createCanvas(0, 10); return 1;').error, /invalid size/);
  assert.match(bad('Graphics.createCanvas(10, 10).drawImage({}, 0, 0); return 1;').error, /needs an image/);
  assert.match(bad(`var i = Graphics.createImage("${IMG.png(4, 4).toString('base64')}"); var c = Graphics.createCanvas(4, 4); c.drawImage(i, "a", 0); return c.capture();`).error, /takes numbers/);
  assert.match(bad(`var i = Graphics.createImage("${IMG.png(4, 4).toString('base64')}"); var c = Graphics.createCanvas(4, 4); c.drawImage(i, 0, 0); return c.capture();`).error, /Graphics capture: boom/);
});

test('Graphics: the host checks what it is asked to draw', () => {
  const { composeJob } = require('../plugin/graphics.js');
  const png = IMG.png(2, 2).toString('base64');
  const job = composeJob({ width: 10, height: 20, images: [png], draws: [[0, 0, 0, 2, 2, 0, 0, 10, 20]] });
  assert.strictEqual(job.type, 'image/png'); // a PNG source stays PNG (transparency)
  assert.strictEqual(composeJob({ width: 1, height: 1, images: [IMG.jpeg(1, 1).toString('base64')], draws: [] }).type, 'image/jpeg');
  assert.strictEqual(composeJob({ width: 1, height: 1, images: [png], draws: [], type: 'webp', quality: 0.5 }).type, 'image/webp');
  assert.throws(() => composeJob({ width: 20000, height: 1, images: [], draws: [] }), /kích thước/);
  assert.throws(() => composeJob({ width: 1, height: 1, images: ['not base64!'], draws: [] }), /base64/);
  assert.throws(() => composeJob({ width: 1, height: 1, images: [png], draws: [[1, 0, 0, 1, 1, 0, 0, 1, 1]] }), /lệnh vẽ/);
  assert.throws(() => composeJob({ width: 1, height: 1, images: [png], draws: [[0, 0, 0, 1, 1, 0, 0, 1]] }), /lệnh vẽ/);
});

test('service: the link of a page drawn by an image script is passed to the script as it is', async () => {
  const manifest = normalizeManifest({ metadata: { name: 'S', source: 'https://s.test', type: 'comic' }, script: { detail: 'detail.js', toc: 'toc.js', chap: 'chap.js' } });
  const files = {
    'detail.js': 'function execute(u) {}', 'toc.js': 'function execute(u) {}',
    'chap.js': 'function execute(u) { return Response.success([{ link: "https://img.s.test/1.jpg [[0,1,2,3]]", script: "image.js" }, "/p/2.jpg", { link: "/p/3.jpg#script=img.js" }]); }',
    'image.js': 'function execute(u) { return "B64:" + u; }',
  };
  const svc = createService({ manifest, capabilities: capabilities(manifest, files), runner: async (entry, args) => run(files, entry, args) });
  const chap = await svc.chap('https://s.test/c/1');
  assert.deepStrictEqual(chap.images.map((i) => [i.url, i.script]), [['https://img.s.test/1.jpg [[0,1,2,3]]', 'image.js'], ['https://s.test/p/2.jpg', ''], ['/p/3.jpg', 'img.js']]);
  assert.strictEqual(await svc.image('image.js', chap.images[0].url), 'B64:https://img.s.test/1.jpg [[0,1,2,3]]');
});

test('service: detail/toc/chap normalization (comic)', async () => {
  const manifest = normalizeManifest({ metadata: { name: 'Site', source: 'https://site.test', type: 'comic', regexp: 'site\\.test/book/\\d+' }, script: { detail: 'detail.js', toc: 'toc.js', chap: 'chap.js' } });
  const files = {
    'detail.js': 'function execute(url) { var d = fetch(url).html(); return Response.success({ name: d.select("h1").text(), cover: d.select(".cover img").attr("data-src"), description: d.select(".desc").html(), host: "https://site.test" }); }',
    'toc.js': 'function execute(url) { var d = fetch(url).html(); var out = []; var el = d.select(".list a"); for (var i = el.size() - 1; i >= 0; i--) out.push({ name: el.get(i).text(), url: el.get(i).attr("href"), host: "https://site.test" }); return Response.success(out); }',
    'chap.js': 'function execute(url) { var d = fetch(url).html(); var imgs = []; d.select("#pages img").forEach(function (e) { imgs.push({ link: e.attr("data-original") || e.attr("src"), fallback: [e.attr("src")] }); }); return Response.success(imgs); }',
  };
  const caps = capabilities(manifest, files);
  assert.ok(caps.downloadable);
  const svc = createService({ manifest, capabilities: caps, runner: async (entry, args) => run(files, entry, args) });
  const detail = await svc.detail('https://site.test/book/1/');
  assert.strictEqual(detail.cover, 'https://cdn.site.test/c.jpg');
  assert.strictEqual(detail.description, 'Dòng 1\nDòng 2');
  const toc = await svc.toc('https://site.test/book/1');
  assert.deepStrictEqual(toc.map((c) => c.url), ['https://site.test/book/1/c1', 'https://site.test/book/1/c2']);
  const chap = await svc.chap(toc[0].url);
  assert.deepStrictEqual(chap.images.map((i) => i.url), ['https://site.test/i/1.jpg', 'https://img.site.test/2.png']);
  assert.ok(regexMatches(manifest, 'https://site.test/book/12/'));
  assert.ok(!regexMatches(manifest, 'https://site.test/book/12/c1'));
  assert.match(pluginId(manifest.metadata), /^site-[0-9a-f]{8}$/);
});

test('bundled Cứu Truyện: lists, detail, toc, pages put back from drm_data (offline)', async () => {
  const path = require('path');
  const { readPackageDir } = require('../plugin/package.js');
  const pkg = readPackageDir(path.join(__dirname, '..', 'extensions', 'cuutruyen'));
  const caps = capabilities(pkg.manifest, pkg.files);
  assert.ok(caps.downloadable && caps.graphics && caps.genre && caps.search, JSON.stringify(caps));
  const old = 'https://storage-ct.lrclib.net/file/cuutruyen/uploads';
  // drm_data of a real page (1024x1457): "#v4|750-150|1200-150|150-150|600-150|450-150|300-150|900-150|1350-107|1050-150|0-150"
  const drm = 'EEcATQIMAhsEBgVECAUJAx4AAQFJCAcGGAIACEUBCQMeAAEBSQ0HBhgCAAhF\nBAkDHgABAUkAAgYYAgAIRQYKBgMcBQECRQMGAAMYCQwHRQMeAAEB\n';
  const API = {
    '/mangas/recently_updated?page=2&per_page=24': { data: [{ id: 7, name: 'Truyện A', cover_url: `${old}/manga/7/cover/a.jpg`, newest_chapter_number: '12' }], _metadata: { current_page: 2, total_pages: 3 } },
    '/tags/romcom?page=1&per_page=24': { data: { mangas: [{ id: 8, name: 'Truyện B', cover_url: `${old}/manga/8/b.jpg` }], tag: { name: 'romcom' } }, _metadata: { current_page: 1, total_pages: 1 } },
    '/mangas/search?q=th%E1%BB%AD&page=1&per_page=24': { data: [{ id: 9, name: 'Thử', cover_url: 'https://storage-ct-riften.site/c.jpg', author_name: 'X' }], _metadata: { current_page: 1, total_pages: 1 } },
    '/mangas/7': { data: { id: 7, name: 'Truyện A', cover_url: `${old}/manga/7/cover/a.jpg`, author: { name: 'Tác giả' }, team: { name: 'Nhóm' }, chapters_count: 2, full_description: '<p>Mô tả</p>', tags: [{ name: 'hài hước', slug: 'hai-huoc' }, { name: 'đang tiến hành', slug: 'dang-tien-hanh' }] } },
    '/mangas/7/chapters': { data: [{ id: 102, order: 1, number: '2', name: 'Kết' }, { id: 101, order: 0, number: '1', name: null }] },
    '/chapters/101': { data: { pages: [{ order: 1, image_url: `${old}/page/2/plain.jpg`, drm_data: '' }, { order: 0, image_url: `${old}/page/1/scrambled.jpg`, drm_data: drm }] } },
  };
  const header = IMG.jpeg(1024, 1457).toString('base64');
  const draws = [];
  const host = fakeHost();
  host.request = (req) => {
    if (/^https:\/\/storage-bravo\.cuutruyen\.net\//.test(req.url)) return { ok: true, status: 200, url: req.url, headers: { 'content-type': 'image/jpeg' }, body: header };
    const json = API[req.url.replace('https://cuutruyen.net/api/v2', '')];
    return json ? { ok: true, status: 200, url: req.url, headers: { 'content-type': 'application/json' }, body: Buffer.from(JSON.stringify(json)).toString('base64') } : { ok: false, status: 404, url: req.url, headers: {}, body: '' };
  };
  host.graphics = (p) => { draws.push(p); return { data: 'PAGE', type: 'image/jpeg' }; };
  const svc = createService({ manifest: pkg.manifest, capabilities: caps, runner: async (entry, args) => run(pkg.files, entry, args, { source: pkg.manifest.metadata.source }, host) });
  assert.strictEqual((await svc.home()).length, 4);
  const genres = await svc.genre();
  assert.ok(genres.length >= 20 && genres.every((g) => g.script === 'tag.js' && /^\/tags\/[a-z-]+$/.test(g.input)));
  const list = await svc.list('list.js', '/mangas/recently_updated', '2');
  assert.deepStrictEqual(list, { next: '3', items: [{ name: 'Truyện A', link: 'https://cuutruyen.net/mangas/7', cover: 'https://storage-bravo.cuutruyen.net/file/cuutruyen/uploads/manga/7/cover/a.jpg', coverReferrerPolicy: '', description: 'Chương 12', tag: '', host: 'https://cuutruyen.net' }] });
  assert.strictEqual((await svc.list('tag.js', '/tags/romcom', '')).items[0].name, 'Truyện B');
  const found = await svc.search('thử', '');
  assert.strictEqual(found.items[0].cover, 'https://storage-charlie.cuutruyen.net/c.jpg');
  const detail = await svc.detail('https://cuutruyen.net/mangas/7');
  assert.deepStrictEqual([detail.name, detail.author, detail.description, detail.ongoing, detail.genres], ['Truyện A', 'Tác giả', 'Mô tả', true, ['hài hước', 'đang tiến hành']]);
  const toc = await svc.toc('https://cuutruyen.net/mangas/7');
  assert.deepStrictEqual(toc.map((c) => [c.name, c.url]), [['Chương 1', 'https://cuutruyen.net/mangas/7/chapters/101'], ['Chương 2: Kết', 'https://cuutruyen.net/mangas/7/chapters/102']]);
  const chap = await svc.chap(toc[0].url);
  assert.deepStrictEqual(chap.images.map((i) => i.script), ['image.js', '']);
  assert.strictEqual(chap.images[1].url, 'https://storage-bravo.cuutruyen.net/file/cuutruyen/uploads/page/2/plain.jpg');
  assert.strictEqual(await svc.image('image.js', chap.images[0].url), 'PAGE');
  const rows = [[0, 750, 150], [150, 1200, 150], [300, 150, 150], [450, 600, 150], [600, 450, 150], [750, 300, 150], [900, 900, 150], [1050, 1350, 107], [1157, 1050, 150], [1307, 0, 150]];
  assert.deepStrictEqual(draws[0].draws, rows.map(([sy, dy, h]) => [0, 0, sy, 1024, h, 0, dy, 1024, h]));
  assert.deepStrictEqual([draws[0].width, draws[0].height], [1024, 1457]);
});

test('htmlToText keeps paragraphs', () => {
  assert.strictEqual(htmlToText('<p>a</p><p>b<br>c</p>'), 'a\nb\nc');
});

process.on('beforeExit', () => { if (failures) { console.error(`${failures} test(s) failed`); process.exitCode = 1; } });
