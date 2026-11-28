// Lynxx plugin host: high-level operations over a script runner, with vBook's result conventions normalized
// (relative links joined with host/source, items without a name dropped, duplicates removed, HTML descriptions
// flattened, page.js + toc.js combined, comic image objects with fallback / referrerpolicy).
// runner(entry, args) -> Promise<{ok, data, next} | {ok: false, error}>; works in Node and Electron main.
'use strict';

const H = require('./html.js');

const MAX_ITEMS = 2000;
const MAX_CHAPTERS = 20000;
const MAX_TOC_PAGES = 500;
const MAX_IMAGES = 3000;

function absolute(url, base) {
  let s = String(url ?? '').trim();
  if (!s) return '';
  if (s.startsWith('//')) s = `https:${s}`;
  if (/^(data|blob|javascript|file|about):/i.test(s)) return '';
  try { return new URL(s, base || undefined).href; } catch { return /^https?:\/\//i.test(s) ? s : ''; }
}

function trimSlash(url) {
  const s = String(url ?? '');
  return /^https?:\/\//i.test(s) ? s.replace(/\/+$/, '') : s;
}

function htmlToText(html) {
  const s = String(html ?? '');
  if (!/[<&]/.test(s)) return s.trim();
  const doc = H.parse(s);
  const body = H.select(doc, 'body')[0];
  const out = [];
  const walk = (node) => {
    for (const c of node.children) {
      if (c.type === 'text') out.push(c.data.replace(/\s+/g, ' '));
      else if (c.type === 'element') {
        if (c.name === 'script' || c.name === 'style') continue;
        if (c.name === 'br') { out.push('\n'); continue; }
        const block = /^(p|div|li|h[1-6]|tr|section|article|blockquote|ul|ol|table|dd|dt)$/.test(c.name);
        if (block) out.push('\n');
        walk(c);
        if (block) out.push('\n');
      }
    }
  };
  walk(body);
  return out.join('').split('\n').map((l) => l.trim()).filter(Boolean).join('\n');
}

function splitFragmentOptions(url) {
  // vBook: "<url>#referrerpolicy=no-referrer&script=cover.js" — the fragment is never sent
  const s = String(url ?? '');
  const i = s.indexOf('#');
  if (i < 0) return { url: s, referrerPolicy: '', script: '' };
  const frag = new URLSearchParams(s.slice(i + 1));
  const referrerPolicy = frag.get('referrerpolicy') || '';
  const script = frag.get('script') || '';
  if (!referrerPolicy && !script) return { url: s, referrerPolicy: '', script: '' };
  return { url: s.slice(0, i), referrerPolicy, script };
}

function createService({ runner, manifest, capabilities }) {
  const source = manifest.metadata.source;
  const scriptFile = (key) => manifest.script[key];
  const need = (key) => {
    const file = scriptFile(key);
    if (!file) throw new Error(`extension không có script "${key}"`);
    return file;
  };
  const call = async (file, args) => {
    const r = await runner(file, args.map((a) => (a === undefined || a === null ? '' : String(a))));
    if (!r || !r.ok) {
      const err = new Error(r && r.error ? String(r.error) : 'extension lỗi');
      err.stack = r && r.stack ? r.stack : err.stack;
      throw err;
    }
    return r;
  };

  const tabs = (data) => (Array.isArray(data) ? data : []).slice(0, 500).filter((t) => t && typeof t === 'object' && t.title).map((t) => ({
    title: String(t.title).slice(0, 200), input: String(t.input ?? ''), script: String(t.script || ''), type: String(t.type || ''),
  }));

  function items(data, extraBase) {
    const seen = new Set();
    const out = [];
    for (const it of (Array.isArray(data) ? data : []).slice(0, MAX_ITEMS)) {
      if (!it || typeof it !== 'object') continue;
      const name = String(it.name ?? '').trim();
      if (!name) continue;
      const base = it.host || extraBase || source;
      const link = absolute(it.link, base);
      if (!link || seen.has(link)) continue;
      seen.add(link);
      const cover = splitFragmentOptions(it.cover);
      out.push({
        name: name.slice(0, 300), link, cover: absolute(cover.url, base), coverReferrerPolicy: String(it.referrerpolicy || cover.referrerPolicy || ''),
        description: htmlToText(it.description).slice(0, 500), tag: String(it.tag ?? '').slice(0, 60), host: String(it.host || ''),
      });
    }
    return out;
  }

  return {
    async home() { return capabilities.home ? tabs((await call(need('home'), [])).data) : []; },
    async genre() { return capabilities.genre ? tabs((await call(need('genre'), [])).data) : []; },
    async list(script, input, page) {
      if (!script) throw new Error('mục này không có script');
      const r = await call(script, [trimSlash(input), page || '']);
      return { items: items(r.data), next: r.next ? String(r.next) : '' };
    },
    async search(query, page) {
      const r = await call(need('search'), [query, page || '']);
      return { items: items(r.data), next: r.next ? String(r.next) : '' };
    },
    async detail(url) {
      const target = trimSlash(url);
      const r = await call(need('detail'), [target]);
      const d = r.data && typeof r.data === 'object' ? r.data : {};
      const base = d.host || source;
      const cover = splitFragmentOptions(d.cover);
      const genres = tabs(d.genres).map((g) => ({ title: g.title, input: g.input, script: g.script }));
      const tags = (Array.isArray(d.tags) ? d.tags : []).slice(0, 100).map((t) => (t && typeof t === 'object' ? String(t.title || t.name || '') : String(t ?? ''))).filter(Boolean);
      return {
        url: absolute(d.url, base) || target,
        name: String(d.name ?? '').trim().slice(0, 500),
        author: htmlToText(d.author).slice(0, 300),
        cover: absolute(cover.url, base),
        coverReferrerPolicy: String(d.referrerpolicy || cover.referrerPolicy || ''),
        description: htmlToText(d.description).slice(0, 20000),
        detail: htmlToText(d.detail).slice(0, 5000),
        host: String(d.host || ''),
        ongoing: d.ongoing === true || d.ongoing === 'true',
        nsfw: d.nsfw === true || manifest.metadata.nsfw,
        genres: genres.length ? genres.map((g) => g.title) : tags,
        language: String(d.locale || manifest.metadata.locale || ''),
        kind: capabilities.kind,
      };
    },
    async toc(url) {
      const target = trimSlash(url);
      let pages = [target];
      if (scriptFile('page') && capabilities.page) {
        const r = await call(scriptFile('page'), [target]);
        const list = (Array.isArray(r.data) ? r.data : []).map((p) => absolute(typeof p === 'string' ? p : p && (p.url || p.link), source)).filter(Boolean);
        if (list.length) pages = list.slice(0, MAX_TOC_PAGES);
      }
      const seen = new Set();
      const chapters = [];
      for (const page of pages) {
        const r = await call(need('toc'), [page]);
        for (const c of Array.isArray(r.data) ? r.data : []) {
          if (!c || typeof c !== 'object') continue;
          if (String(c.type || '') === 'section') continue;
          const link = absolute(c.url ?? c.link, c.host || source);
          if (!link || seen.has(link)) continue;
          seen.add(link);
          chapters.push({ name: String(c.name ?? '').trim().slice(0, 300) || `Chương ${chapters.length + 1}`, url: link, locked: c.lock === true || c.pay === true });
          if (chapters.length >= MAX_CHAPTERS) break;
        }
        if (chapters.length >= MAX_CHAPTERS) break;
      }
      return chapters;
    },
    async chap(url) {
      const target = trimSlash(url);
      const r = await call(need('chap'), [target]);
      if (capabilities.kind === 'comic') {
        const images = [];
        for (const img of (Array.isArray(r.data) ? r.data : []).slice(0, MAX_IMAGES)) {
          const raw = typeof img === 'string' ? img : img && typeof img === 'object' ? img.link : '';
          const opts = splitFragmentOptions(raw);
          const link = absolute(opts.url, source);
          if (!link) continue;
          const obj = typeof img === 'object' && img ? img : {};
          images.push({
            url: link,
            fallback: (Array.isArray(obj.fallback) ? obj.fallback : []).map((f) => absolute(f, source)).filter(Boolean).slice(0, 5),
            referrerPolicy: String(obj.referrerpolicy || opts.referrerPolicy || ''),
            script: String(obj.script || opts.script || ''),
          });
        }
        return { kind: 'comic', images };
      }
      return { kind: 'novel', html: typeof r.data === 'string' ? r.data : String(r.data ?? ''), title: r.next ? String(r.next) : '' };
    },
    async image(script, link) {
      // a per-image script returns the decoded image as bare base64 (vBook "image.js")
      const r = await runner(script, [String(link)]);
      if (r && typeof r.raw === 'string') return r.raw;
      if (!r || !r.ok || typeof r.data !== 'string') throw new Error(r && r.error ? r.error : 'image script failed');
      return r.data;
    },
  };
}

module.exports = { createService, absolute, htmlToText, splitFragmentOptions, trimSlash };
