// Lynxx plugin runtime: the vBook extension API (fetch, Http, Html, Response, load, Console, sleep, localStorage,
// localCookie, cacheStorage, UserAgent, Engine.newBrowser) on top of a synchronous `host` object.
// The same file runs in the sandboxed plugin renderer (plain <script>, host = preload bridge over sendSync) and in Node
// (tests and the dev CLI). Extension code is plain sloppy-mode JavaScript written for Rhino: it calls fetch()
// synchronously, so every host call is synchronous.
(function (root, factory) {
  if (typeof module === 'object' && module.exports) module.exports = factory(require('./html.js'), require('./cryptojs.js'));
  else root.LynxxRuntime = factory(root.LynxxHtml, root.LynxxCryptoJS);
}(typeof self !== 'undefined' ? self : this, (H, C) => {
  'use strict';

  const MAX_LOAD_DEPTH = 16;
  const MAX_SLEEP_MS = 30000;
  const UA = {
    chrome: 'Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/142.0.0.0 Safari/537.36',
    android: 'Mozilla/5.0 (Linux; Android 14; Pixel 8) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/142.0.0.0 Mobile Safari/537.36',
    ios: 'Mozilla/5.0 (iPhone; CPU iPhone OS 18_1 like Mac OS X) AppleWebKit/605.1.15 (KHTML, like Gecko) Version/18.1 Mobile/15E148 Safari/604.1',
    firefox: 'Mozilla/5.0 (Windows NT 10.0; Win64; x64; rv:144.0) Gecko/20100101 Firefox/144.0',
    safari: 'Mozilla/5.0 (Macintosh; Intel Mac OS X 14_6) AppleWebKit/605.1.15 (KHTML, like Gecko) Version/18.1 Safari/605.1.15',
  };

  // ------------------------------------------------------------ bytes / text helpers

  function base64ToBytes(b64) {
    if (!b64) return new Uint8Array(0);
    if (typeof Buffer !== 'undefined') return new Uint8Array(Buffer.from(b64, 'base64'));
    const bin = atob(b64);
    const out = new Uint8Array(bin.length);
    for (let i = 0; i < bin.length; i++) out[i] = bin.charCodeAt(i);
    return out;
  }

  function decodeBytes(bytes, charset) {
    const label = String(charset || 'utf-8').trim().toLowerCase().replace(/^["']|["']$/g, '');
    try { return new TextDecoder(label).decode(bytes); } catch { return new TextDecoder('utf-8').decode(bytes); }
  }

  function sniffCharset(headers, bytes) {
    const ct = headers['content-type'] || '';
    const m = /charset\s*=\s*["']?([\w.:-]+)/i.exec(ct);
    if (m) return m[1];
    const head = new TextDecoder('latin1').decode(bytes.subarray(0, 4096));
    const meta = /<meta[^>]+charset\s*=\s*["']?([\w.:-]+)/i.exec(head);
    if (meta) return meta[1];
    if (bytes[0] === 0xff && bytes[1] === 0xfe) return 'utf-16le';
    if (bytes[0] === 0xfe && bytes[1] === 0xff) return 'utf-16be';
    return 'utf-8';
  }

  function toQuery(params) {
    if (!params) return '';
    if (typeof params === 'string') return params.replace(/^\?/, '');
    const parts = [];
    for (const [k, v] of Object.entries(params)) {
      if (v === undefined || v === null) continue;
      const values = Array.isArray(v) ? v : [v];
      for (const one of values) parts.push(`${encodeURIComponent(k)}=${encodeURIComponent(String(one))}`);
    }
    return parts.join('&');
  }

  function withQuery(url, params) {
    const q = toQuery(params);
    if (!q) return url;
    const hash = url.indexOf('#');
    const base = hash >= 0 ? url.slice(0, hash) : url;
    return `${base}${base.includes('?') ? (base.endsWith('?') || base.endsWith('&') ? '' : '&') : '?'}${q}${hash >= 0 ? url.slice(hash) : ''}`;
  }

  function lowerHeaders(obj) {
    const out = {};
    for (const [k, v] of Object.entries(obj || {})) if (v !== undefined && v !== null) out[String(k)] = String(v);
    return out;
  }

  // ------------------------------------------------------------ Jsoup-like wrappers

  function createDom(ctx) {
    const cache = new WeakMap();

    class JElements extends Array {
      size() { return this.length; }
      // vBook: get / first / last on a missing index give an empty element whose methods return "" (never null)
      get(i) { return this[i] ?? emptyElement(); }
      first() { return this.length ? this[0] : emptyElement(); }
      last() { return this.length ? this[this.length - 1] : emptyElement(); }
      isEmpty() { return this.length === 0; }
      text() { return this.map((e) => e.text()).filter((t) => t.length).join(' '); }
      eachText() { return Array.from(this, (e) => e.text()).filter((t) => t.length); }
      html() { return Array.from(this, (e) => e.html()).join('\n'); }
      outerHtml() { return Array.from(this, (e) => e.outerHtml()).join('\n'); }
      toString() { return this.outerHtml(); }
      attr(name, value) {
        if (value !== undefined) { this.forEach((e) => e.attr(name, value)); return this; }
        for (const e of this) if (e.hasAttr(name)) return e.attr(name);
        return '';
      }
      eachAttr(name) { return Array.from(this, (e) => (e.hasAttr(name) ? e.attr(name) : null)).filter((v) => v !== null); }
      hasAttr(name) { return this.some((e) => e.hasAttr(name)); }
      removeAttr(name) { this.forEach((e) => e.removeAttr(name)); return this; }
      absUrl(name) { for (const e of this) if (e.hasAttr(name)) return e.absUrl(name); return ''; }
      hasClass(c) { return this.some((e) => e.hasClass(c)); }
      addClass(c) { this.forEach((e) => e.addClass(c)); return this; }
      removeClass(c) { this.forEach((e) => e.removeClass(c)); return this; }
      val() { return this.length ? this[0].val() : ''; }
      select(css) {
        const seen = new Set();
        const out = new JElements();
        for (const e of this) for (const m of e.select(css)) if (!seen.has(m._n)) { seen.add(m._n); out.push(m); }
        return out;
      }
      selectFirst(css) { return this.select(css).first(); }
      not(css) { return JElements.from(Array.prototype.filter.call(this, (e) => !e.is(css))); }
      is(css) { return this.some((e) => e.is(css)); }
      eq(i) { const e = this[i < 0 ? this.length + i : i]; return e ? JElements.of(e) : new JElements(); }
      parents() { const out = new JElements(); const seen = new Set(); for (const e of this) for (const p of e.parents()) if (!seen.has(p._n)) { seen.add(p._n); out.push(p); } return out; }
      next() { return JElements.from(Array.from(this, (e) => e.nextElementSibling()).filter(Boolean)); }
      prev() { return JElements.from(Array.from(this, (e) => e.previousElementSibling()).filter(Boolean)); }
      remove() { this.forEach((e) => e.remove()); return this; }
      empty() { this.forEach((e) => e.empty()); return this; }
      append(html) { this.forEach((e) => e.append(html)); return this; }
      prepend(html) { this.forEach((e) => e.prepend(html)); return this; }
      before(html) { this.forEach((e) => e.before(html)); return this; }
      after(html) { this.forEach((e) => e.after(html)); return this; }
      unwrap() { this.forEach((e) => e.unwrap()); return this; }
      tagName(name) { if (name !== undefined) { this.forEach((e) => e.tagName(name)); return this; } return this.length ? this[0].tagName() : ''; }
      toArray() { return Array.from(this); }
      toJSON() { return Array.from(this, (e) => e.outerHtml()); }
    }

    const emptyElement = () => {
      const node = new H.ElementNode('');
      node.empty = true;
      return new JElement(node);
    };
    const wrap = (node) => {
      if (!node) return null;
      if (node.type === 'text') return new JTextNode(node);
      let w = cache.get(node);
      if (!w) { w = node.type === 'document' ? new JDocument(node) : new JElement(node); cache.set(node, w); }
      return w;
    };
    const wrapAll = (nodes) => JElements.from(nodes.map(wrap));
    const fragmentNodes = (html) => {
      const body = H.select(H.parseFragment(String(html ?? '')), 'body')[0];
      return [...body.children];
    };

    class JTextNode {
      constructor(n) { this._n = n; }
      text() { return H.textOf(this._n); }
      wholeText() { return this._n.data; }
      getWholeText() { return this._n.data; }
      isBlank() { return !this._n.data.trim(); }
      remove() { this._n.remove(); return this; }
      toString() { return this._n.data; }
      outerHtml() { return H.outerHtml(this._n); }
    }

    class JElement {
      constructor(n) { this._n = n; }
      select(css) { return this._n.empty ? new JElements() : wrapAll(H.select(this._n, String(css))); }
      selectFirst(css) { return this._n.empty ? null : wrap(H.select(this._n, String(css))[0]); }
      size() { return this._n.empty ? 0 : 1; }
      isEmpty() { return Boolean(this._n.empty); }
      first() { return this; }
      last() { return this; }
      get() { return this; }
      forEach(fn) { if (!this._n.empty) fn(this, 0); }
      is(css) { return H.matchesSelector(this._n, String(css)); }
      closest(css) { let n = this._n; while (n && n.type === 'element') { if (H.matchesSelector(n, String(css))) return wrap(n); n = n.parent; } return null; }
      text(value) {
        if (value !== undefined) { this._n.children.forEach((c) => { c.parent = null; }); this._n.children = []; this._n.append(new H.TextNode(String(value))); return this; }
        return H.textOf(this._n);
      }
      ownText() { return H.textOf(this._n, true); }
      wholeText() { return H.wholeText(this._n); }
      hasText() { return H.textOf(this._n).length > 0; }
      html(value) {
        if (value !== undefined) { this.empty(); for (const k of fragmentNodes(value)) this._n.append(k); return this; }
        return H.innerHtml(this._n);
      }
      outerHtml() { return this._n.empty ? '' : H.outerHtml(this._n); }
      toString() { return this.outerHtml(); }
      toJSON() { return this.outerHtml(); }
      data() { return H.dataOf(this._n); }
      attr(name, value) {
        const key = String(name);
        if (value !== undefined) { this._n.setAttr(key, value); return this; }
        if (key.toLowerCase().startsWith('abs:')) return this.absUrl(key.slice(4));
        const v = this._n.getAttr(key);
        return v === null ? '' : v;
      }
      hasAttr(name) {
        const key = String(name);
        if (key.toLowerCase().startsWith('abs:')) return this._n.getAttr(key.slice(4)) !== null && this.absUrl(key.slice(4)) !== '';
        return this._n.getAttr(key) !== null;
      }
      removeAttr(name) { this._n.removeAttr(name); return this; }
      attributes() { return this._n.attrs.map(([key, value]) => ({ key, value, getKey: () => key, getValue: () => value })); }
      dataset() { const out = {}; for (const [k, v] of this._n.attrs) if (k.startsWith('data-')) out[k.slice(5)] = v; return out; }
      absUrl(name) {
        const v = this._n.getAttr(name);
        if (v === null || !v.trim()) return '';
        return H.resolveUrl(this.baseUri(), v.trim());
      }
      baseUri() { return H.baseUriOf(this._n) || ctx.baseUri || ''; }
      id() { return this._n.getAttr('id') || ''; }
      className() { return this._n.getAttr('class') || ''; }
      classNames() { return (this._n.getAttr('class') || '').split(/\s+/).filter(Boolean); }
      hasClass(c) { const want = String(c).toLowerCase(); return this.classNames().some((x) => x.toLowerCase() === want); }
      addClass(c) { if (!this.hasClass(c)) this._n.setAttr('class', [...this.classNames(), c].join(' ')); return this; }
      removeClass(c) { const want = String(c).toLowerCase(); this._n.setAttr('class', this.classNames().filter((x) => x.toLowerCase() !== want).join(' ')); return this; }
      toggleClass(c) { return this.hasClass(c) ? this.removeClass(c) : this.addClass(c); }
      tagName(name) { if (name !== undefined) { this._n.name = String(name).toLowerCase(); return this; } return this._n.name; }
      normalName() { return this._n.name; }
      nodeName() { return this._n.name; }
      val(value) {
        if (value !== undefined) { if (this._n.name === 'textarea') this.text(value); else this._n.setAttr('value', value); return this; }
        return this._n.name === 'textarea' ? H.wholeText(this._n) : (this._n.getAttr('value') || '');
      }
      parent() { const p = this._n.parent; return p && p.type === 'element' ? wrap(p) : null; }
      parents() { const out = new JElements(); let p = this._n.parent; while (p && p.type === 'element') { out.push(wrap(p)); p = p.parent; } return out; }
      children() { return wrapAll(this._n.elementChildren()); }
      child(i) { return wrap(this._n.elementChildren()[i]) || null; }
      childrenSize() { return this._n.elementChildren().length; }
      childNodeSize() { return this._n.children.length; }
      childNodes() { return this._n.children.filter((c) => c.type !== 'comment').map(wrap); }
      textNodes() { return this._n.children.filter((c) => c.type === 'text').map(wrap); }
      firstElementChild() { return wrap(this._n.elementChildren()[0]); }
      lastElementChild() { const k = this._n.elementChildren(); return wrap(k[k.length - 1]); }
      nextElementSibling() { return wrap(H.nextElement(this._n)); }
      previousElementSibling() { return wrap(H.prevElement(this._n)); }
      nextElementSiblings() { const out = new JElements(); let n = H.nextElement(this._n); while (n) { out.push(wrap(n)); n = H.nextElement(n); } return out; }
      previousElementSiblings() { const out = new JElements(); let n = H.prevElement(this._n); while (n) { out.push(wrap(n)); n = H.prevElement(n); } return out; }
      siblingElements() { const p = this._n.parent; return p ? wrapAll(p.elementChildren().filter((c) => c !== this._n)) : new JElements(); }
      firstElementSibling() { const p = this._n.parent; return p ? wrap(p.elementChildren()[0]) : this; }
      lastElementSibling() { const p = this._n.parent; if (!p) return this; const k = p.elementChildren(); return wrap(k[k.length - 1]); }
      elementSiblingIndex() { const p = this._n.parent; return p ? p.elementChildren().indexOf(this._n) : 0; }
      getElementsByTag(tag) { return this.select(String(tag)); }
      getElementById(id) { return wrap(H.allElements(this._n, true).find((e) => e.getAttr('id') === String(id))); }
      getElementsByClass(c) { return wrapAll(H.allElements(this._n, true).filter((e) => wrap(e).hasClass(c))); }
      getElementsByAttribute(a) { return wrapAll(H.allElements(this._n, true).filter((e) => e.getAttr(a) !== null)); }
      getElementsContainingText(t) { return this.select(`:contains(${t})`); }
      getAllElements() { return wrapAll(H.allElements(this._n, true)); }
      remove() { this._n.remove(); return this; }
      empty() { for (const c of this._n.children) c.parent = null; this._n.children = []; return this; }
      append(html) { for (const k of fragmentNodes(html)) this._n.append(k); return this; }
      prepend(html) { fragmentNodes(html).reverse().forEach((k) => this._n.insertAt(0, k)); return this; }
      appendText(t) { this._n.append(new H.TextNode(String(t))); return this; }
      prependText(t) { this._n.insertAt(0, new H.TextNode(String(t))); return this; }
      appendElement(tag) { const el = new H.ElementNode(String(tag).toLowerCase()); this._n.append(el); return wrap(el); }
      before(html) { const p = this._n.parent; if (p) { const i = p.children.indexOf(this._n); fragmentNodes(html).reverse().forEach((k) => p.insertAt(i, k)); } return this; }
      after(html) { const p = this._n.parent; if (p) { const i = p.children.indexOf(this._n) + 1; fragmentNodes(html).reverse().forEach((k) => p.insertAt(i, k)); } return this; }
      unwrap() { const p = this._n.parent; if (!p) return null; const i = p.children.indexOf(this._n); const kids = [...this._n.children]; this._n.remove(); kids.reverse().forEach((k) => p.insertAt(i, k)); return kids.length ? wrap(kids[kids.length - 1]) : null; }
      replaceWith(html) { this.before(html); this.remove(); return this; }
      wrap(html) { const nodes = fragmentNodes(html); const outer = nodes.find((x) => x.type === 'element'); if (!outer || !this._n.parent) return this; const p = this._n.parent; const i = p.children.indexOf(this._n); let inner = outer; while (inner.elementChildren().length) inner = inner.elementChildren()[0]; p.insertAt(i, outer); inner.append(this._n); return this; }
      clone() { return wrap(H.parseFragment(this.outerHtml()).children[0].children[1].children[0]); }
      ownerDocument() { let n = this._n; while (n.parent) n = n.parent; return n.type === 'document' ? wrap(n) : null; }
      root() { let n = this._n; while (n.parent) n = n.parent; return wrap(n); }
    }

    class JDocument extends JElement {
      title() { const t = H.select(this._n, 'title')[0]; return t ? H.textOf(t) : ''; }
      body() { return wrap(H.select(this._n, 'body')[0]); }
      head() { return wrap(H.select(this._n, 'head')[0]); }
      location() { return this._n.baseUri || ''; }
      documentType() { return null; }
      html(value) { if (value !== undefined) return super.html(value); return H.innerHtml(this._n); }
      outerHtml() { return H.innerHtml(this._n); }
      createElement(tag) { return wrap(new H.ElementNode(String(tag).toLowerCase())); }
      charset() { return 'UTF-8'; }
    }

    return { JElements, JElement, JDocument, wrap, emptyElement, parse: (html, base) => wrap(H.parse(String(html ?? ''), base || ctx.baseUri || '')) };
  }

  // ------------------------------------------------------------ API

  function createApi(host, ctx) {
    const dom = createDom(ctx);
    const log = (level, args) => {
      const text = args.map((a) => (typeof a === 'string' ? a : (() => { try { return JSON.stringify(a); } catch { return String(a); } })())).join(' ');
      try { host.log(level, text.slice(0, 4000)); } catch { /* logging never breaks a script */ }
    };

    class FetchResponse {
      constructor(raw, request) {
        this._raw = raw;
        this.ok = Boolean(raw.ok);
        this.status = raw.status | 0;
        this.statusCode = this.status;
        this.url = raw.url || request.url;
        this.headers = raw.headers || {};
        this.request = { url: request.url, method: request.method, headers: request.headers };
        this._bytes = null;
      }
      bytes() { if (!this._bytes) this._bytes = base64ToBytes(this._raw.body || ''); return this._bytes; }
      text(charset) { return decodeBytes(this.bytes(), charset || sniffCharset(this.headers, this.bytes())); }
      string(charset) { return this.text(charset); }
      html(charset) { return dom.parse(this.text(charset), this.url); }
      json() { return JSON.parse(this.text('utf-8')); }
      base64() { return this._raw.body || ''; }
      blob() { return { base64: () => this._raw.body || '', type: this.headers['content-type'] || '', size: this.bytes().length }; }
      header(name) { return this.headers[String(name).toLowerCase()] || ''; }
      get statusText() { return this._raw.statusText || ''; }
      readLine() { if (this._lines === undefined) this._lines = this.text().split(/\r?\n/); return this._lines.length ? this._lines.shift() : null; }
    }

    const resolveUrl = (url) => {
      const s = String(url ?? '').trim();
      if (/^https?:\/\//i.test(s)) return s;
      if (s.startsWith('//')) return `https:${s}`;
      return H.resolveUrl(ctx.baseUri || ctx.source || '', s) || s;
    };

    function doFetch(url, options = {}) {
      const opts = options || {};
      const method = String(opts.method || 'GET').toUpperCase();
      const headers = lowerHeaders(opts.headers);
      let target = resolveUrl(url);
      if (opts.queries || opts.params) target = withQuery(target, opts.queries || opts.params);
      let body = opts.body;
      if (body !== undefined && body !== null && typeof body === 'object') {
        const ct = Object.entries(headers).find(([k]) => k.toLowerCase() === 'content-type');
        if (ct && /json/i.test(ct[1])) body = JSON.stringify(body);
        else { body = toQuery(body); if (!ct) headers['Content-Type'] = 'application/x-www-form-urlencoded; charset=UTF-8'; }
      }
      const request = { url: target, method, headers, body: body === undefined || body === null ? null : String(body), bodyBase64: opts.bodyBase64 || null, timeout: Number(opts.timeout) || 0 };
      // vBook: fetch never throws; a network failure is status 504 with an empty body
      let raw;
      try { raw = host.request(request); } catch (err) { raw = { ok: false, status: 504, error: String(err && err.message ? err.message : err) }; }
      if (!raw || (!raw.status && raw.error)) raw = { ok: false, status: 504, headers: {}, body: '', error: raw ? raw.error : 'no response' };
      if (raw.error) log('warn', [`fetch ${target}: ${raw.error}`]);
      if (raw.requestHeaders) request.headers = { ...request.headers, ...raw.requestHeaders };
      return new FetchResponse(raw, request);
    }

    class HttpRequest {
      constructor(method, url) { this._method = method; this._url = url; this._headers = {}; this._query = null; this._body = null; this._bin = null; this._timeout = 0; }
      params(p) { this._query = { ...(this._query || {}), ...(p || {}) }; return this; }
      queries(p) { return this.params(p); }
      headers(h) { Object.assign(this._headers, h || {}); return this; }
      header(k, v) { this._headers[k] = v; return this; }
      body(b) { this._body = b; return this; }
      binary(b64, type) { this._bin = String(b64 || ''); if (type) this._headers['Content-Type'] = type; return this; }
      timeout(ms) { this._timeout = ms; return this; }
      execute() {
        // Http.post(url).params({...}) without a body posts the parameters as a form (santruyen, tsumino...)
        const postForm = this._method !== 'GET' && this._body === null && this._bin === null && this._query;
        const r = doFetch(this._url, { method: this._method, headers: this._headers, queries: postForm ? null : this._query, body: postForm ? this._query : this._body, bodyBase64: this._bin, timeout: this._timeout });
        this._last = r;
        return r;
      }
      html(charset) { return this.execute().html(charset); }
      string(charset) { return this.execute().text(charset); }
      text(charset) { return this.execute().text(charset); }
      json() { return this.execute().json(); }
      blob() { return this.execute().blob(); }
      cookie() { const r = this._last || this.execute(); return (r.request.headers && (r.request.headers.cookie || r.request.headers.Cookie)) || ''; }
    }

    const Http = {
      get: (url) => new HttpRequest('GET', url),
      post: (url) => new HttpRequest('POST', url),
      put: (url) => new HttpRequest('PUT', url),
      delete: (url) => new HttpRequest('DELETE', url),
    };

    const Html = {
      parse: (html, baseUri) => dom.parse(html, baseUri),
      clean: (html, allowed) => {
        const keep = new Set((allowed || []).map((t) => String(t).toLowerCase()));
        const doc = H.parseFragment(String(html ?? ''));
        const body = H.select(doc, 'body')[0];
        const walk = (node) => {
          for (const child of [...node.children]) {
            if (child.type === 'comment') { child.remove(); continue; }
            if (child.type !== 'element') continue;
            walk(child);
            if (['script', 'style', 'iframe', 'object', 'embed'].includes(child.name)) { child.remove(); continue; }
            if (!keep.has(child.name)) {
              const p = child.parent; const i = p.children.indexOf(child); const kids = [...child.children]; child.remove(); kids.reverse().forEach((k) => p.insertAt(i, k));
            } else child.attrs = child.attrs.filter(([k]) => !k.startsWith('on') && k !== 'style');
          }
        };
        walk(body);
        return H.innerHtml(body);
      },
    };

    const Response = {
      success: (data, next) => ({ __vbook: 'response', ok: true, data: data === undefined ? null : data, next: next === undefined || next === null ? null : String(next) }),
      error: (message) => ({ __vbook: 'response', ok: false, error: String(message ?? 'error') }),
    };

    const Console = { log: (...a) => log('info', a), info: (...a) => log('info', a), warn: (...a) => log('warn', a), error: (...a) => log('error', a), debug: (...a) => log('debug', a) };
    const config = ctx.config || {};
    const localConfig = { getItem: (k, def) => (Object.prototype.hasOwnProperty.call(config, String(k)) ? config[String(k)] : (def === undefined ? null : def)) };
    let cryptoLib = null;
    const getCrypto = () => { if (!cryptoLib) cryptoLib = C.createCryptoJS(host); return cryptoLib; };

    const storage = (op, key, value) => host.storage(op, key === undefined ? null : String(key), value === undefined ? null : String(value));
    const localStorage = {
      getItem: (k) => storage('get', k) ?? null,
      setItem: (k, v) => { storage('set', k, v); },
      removeItem: (k) => { storage('remove', k); },
      clear: () => { storage('clear'); },
    };
    const cacheMap = ctx.cache || new Map();
    const cacheStorage = {
      getItem: (k) => (cacheMap.has(String(k)) ? cacheMap.get(String(k)) : null),
      setItem: (k, v) => { if (cacheMap.size > 5000) cacheMap.clear(); cacheMap.set(String(k), v === undefined || v === null ? null : String(v)); },
      removeItem: (k) => { cacheMap.delete(String(k)); },
      clear: () => { cacheMap.clear(); },
    };
    const localCookie = {
      getCookie: (url) => host.cookie('get', url ? String(url) : null) || '',
      setCookie: (value, url) => { host.cookie('set', url ? String(url) : null, String(value ?? '')); },
      clear: () => { host.cookie('clear', null); },
    };

    const browsers = [];
    const Engine = {
      newBrowser: () => {
        const id = host.browser('open', {}).id;
        const b = {
          launch: (url, timeoutMs) => dom.parse(host.browser('launch', { id, url: resolveUrl(url), timeout: Number(timeoutMs) || 15000 }).html, resolveUrl(url)),
          launchAsync: (url) => { host.browser('launch', { id, url: resolveUrl(url), timeout: 1, async: true }); },
          html: (timeoutMs) => { const r = host.browser('html', { id, timeout: Number(timeoutMs) || 0 }); return dom.parse(r.html, r.url); },
          callJs: (script, waitMs) => { const r = host.browser('callJs', { id, script: String(script ?? ''), wait: Number(waitMs) || 0 }); return dom.parse(r.html, r.url); },
          waitUrl: (pattern, timeoutMs) => jsonList(host.browser('waitUrl', { id, patterns: (Array.isArray(pattern) ? pattern : [pattern]).map((p) => String(p ?? '')), timeout: Number(timeoutMs) || 15000 }).urls),
          urls: () => jsonList(host.browser('urls', { id }).urls),
          block: (patterns) => { host.browser('block', { id, patterns: (Array.isArray(patterns) ? patterns : [patterns]).map((p) => String(p ?? '')) }); },
          loadHtml: (base, html) => { const r = host.browser('loadHtml', { id, base: resolveUrl(base), html: String(html ?? '') }); return dom.parse(r.html, r.url); },
          getUrl: () => host.browser('html', { id, timeout: 0 }).url,
          setUserAgent: (ua) => { host.browser('userAgent', { id, ua: String(ua ?? '') }); },
          close: () => { host.browser('close', { id }); },
        };
        browsers.push(b);
        return b;
      },
    };

    // browser.urls() is a JSON string in older vBook apps and an array in newer ones: an array that prints as JSON
    const jsonList = (urls) => { const arr = Array.isArray(urls) ? urls.slice() : []; arr.toString = () => JSON.stringify(Array.from(arr)); return arr; };

    const unsupported = (name) => new Proxy({}, { get: () => () => { throw new Error(`${name} is not supported by Lynxx`); } });

    return {
      globals: {
        fetch: doFetch, Http, Html, Response, Console, console: Console, Log: Console, Engine, localStorage, localCookie, cacheStorage, localConfig,
        get CryptoJS() { return getCrypto(); },
        sleep: (ms) => { const n = Math.max(0, Math.min(MAX_SLEEP_MS, Number(ms) || 0)); if (n) host.sleep(n); },
        UserAgent: { chrome: () => UA.chrome, android: () => UA.android, ios: () => UA.ios, firefox: () => UA.firefox, safari: () => UA.safari, system: () => UA.chrome },
        Graphics: unsupported('Graphics'), Java: unsupported('Java'), java: unsupported('java'), Packages: unsupported('Packages'),
        Script: unsupported('Script'), WebSocket: unsupported('WebSocket'), localDatabase: unsupported('localDatabase'), localBook: unsupported('localBook'),
        load: (name) => { throw new Error(`load('${name}'): file not found in the extension`); },
        atob: typeof atob === 'function' ? atob : (s) => Buffer.from(String(s), 'base64').toString('binary'),
        btoa: typeof btoa === 'function' ? btoa : (s) => Buffer.from(String(s), 'binary').toString('base64'),
      },
      closeBrowsers: () => { for (const b of browsers) { try { b.close(); } catch { /* already closed */ } } },
      dom,
    };
  }

  // ------------------------------------------------------------ script loading

  const LOAD_RE = /(^|[;{}\s])load\s*\(\s*(['"])([^'"\n]{1,200})\2\s*\)\s*;?/g;

  const BUILTIN_CRYPTO = new Set(['crypto.js', 'cryptojs.js', 'crypto-js.js']);

  function inlineLoads(files, name, depth = 0, seen = new Set([name])) {
    // vBook: load('x.js') is text substitution before the script runs; a missing file expands to nothing and
    // crypto.js always means the bundled CryptoJS (a global here)
    const source = files[name];
    if (typeof source !== 'string') throw new Error(`script not found in the extension: ${name}`);
    if (depth > MAX_LOAD_DEPTH) throw new Error('load() nesting is too deep');
    return source.replace(LOAD_RE, (match, lead, _q, target) => {
      const file = target.replace(/^\.\//, '').replace(/^src\//, '');
      if (BUILTIN_CRYPTO.has(file.toLowerCase())) return `${lead}/* load('${file}'): bundled CryptoJS */`;
      if (!(file in files)) return `${lead}/* load('${file}'): not in the extension */`;
      if (seen.has(file)) return `${lead}/* load('${file}') already included */`;
      seen.add(file);
      return `${lead}\n${inlineLoads(files, file, depth + 1, seen)}\n;`;
    });
  }

  function normalizeResult(value) {
    if (value && typeof value === 'object' && value.__vbook === 'response') {
      return value.ok ? { ok: true, data: toPlain(value.data), next: value.next } : { ok: false, error: value.error };
    }
    if (value === null || value === undefined) return { ok: false, error: 'extension returned no result' };
    return { ok: true, data: toPlain(value), next: null };
  }

  function toPlain(value, depth = 0) {
    if (depth > 32) return null;
    if (value === null || value === undefined) return null;
    if (typeof value === 'string' || typeof value === 'number' || typeof value === 'boolean') return value;
    if (typeof value.toJSON === 'function' && !Array.isArray(value)) return value.toJSON();
    if (Array.isArray(value)) return Array.from(value, (v) => toPlain(v, depth + 1));
    if (typeof value === 'object') {
      const out = {};
      for (const [k, v] of Object.entries(value)) if (typeof v !== 'function') out[k] = toPlain(v, depth + 1);
      return out;
    }
    return String(value);
  }

  /**
   * Run one extension script: `execute(...args)` of `entry`.
   * files: {"detail.js": source, ...} (names relative to the extension's src/ folder)
   * ctx: {source (extension homepage), baseUri, cache (Map shared by the extension's runs)}
   * -> {ok, data, next} | {ok: false, error}
   */
  const IDENT = /^[A-Za-z_$][A-Za-z0-9_$]*$/;
  const RESERVED = new Set(['break', 'case', 'catch', 'class', 'const', 'continue', 'debugger', 'default', 'delete', 'do', 'else', 'export', 'extends', 'finally', 'for', 'function', 'if', 'import', 'in', 'instanceof', 'let', 'new', 'return', 'super', 'switch', 'this', 'throw', 'try', 'typeof', 'var', 'void', 'while', 'with', 'yield', 'enum', 'await', 'null', 'true', 'false', 'arguments', 'eval']);

  function runScript({ files, entry, args = [], host, ctx = {} }) {
    const api = createApi(host, { ...ctx, baseUri: ctx.baseUri || ctx.source || '' });
    try {
      const source = inlineLoads(files, entry);
      const names = Object.keys(api.globals);
      // plugin.json "config" values are injected as string constants (vBook); as outer parameters, an extension's own
      // `let DOMAIN` in the inner scope stays legal
      const config = Object.entries(ctx.config || {}).filter(([k]) => IDENT.test(k) && !RESERVED.has(k) && !names.includes(k));
      const params = [...names, ...config.map(([k]) => k)];
      // the extension's own top-level declarations live in an inner function: `let fetch = ...` there is legal
      // eslint-disable-next-line no-new-func
      const factory = new Function(...params, `return (function () {\n${source}\n;return typeof execute === 'function' ? execute : null;\n})();`);
      const execute = factory(...names.map((n) => api.globals[n]), ...config.map(([, v]) => (v === null || v === undefined ? '' : String(v))));
      if (typeof execute !== 'function') return { ok: false, error: `${entry} has no execute() function` };
      return normalizeResult(execute(...args));
    } catch (err) {
      return { ok: false, error: String(err && err.message ? err.message : err), stack: err && err.stack ? String(err.stack).split('\n').slice(0, 6).join('\n') : '' };
    } finally {
      api.closeBrowsers();
    }
  }

  return { runScript, createApi, inlineLoads, normalizeResult, withQuery, UA };
}));
