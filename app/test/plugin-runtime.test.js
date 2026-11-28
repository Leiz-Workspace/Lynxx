// node app/test/plugin-runtime.test.js — vBook runtime compatibility (html parser, selectors, API, CryptoJS, service)
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

test('htmlToText keeps paragraphs', () => {
  assert.strictEqual(htmlToText('<p>a</p><p>b<br>c</p>'), 'a\nb\nc');
});

process.on('beforeExit', () => { if (failures) { console.error(`${failures} test(s) failed`); process.exitCode = 1; } });
