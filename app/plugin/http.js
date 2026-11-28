// Lynxx plugin host (Electron main): HTTP for extensions and downloads.
// - every hop of a redirect chain is checked against private / loopback addresses (assertPublicHost)
// - cookies live in the extensions' own session partition: sent and stored by hand so `response.request.headers.cookie`
//   (read by many extensions) is the cookie that was really sent
// - per-host pacing and Retry-After (429 / 503) so a download never hammers a site
// - downloadToFile streams into "<dest>.part", resumes it with Range when the server allows, then renames
'use strict';

const fs = require('node:fs');
const fsp = fs.promises;
const { net } = require('electron');

const MAX_BODY = 32 * 1024 * 1024;
const MAX_REDIRECTS = 10;
const DEFAULT_TIMEOUT = 45000; // well below the 120 s a whole script run may take
const RETRY_AFTER_MAX_MS = 60000; // page downloads
const RETRY_AFTER_SCRIPT_MAX_MS = 20000; // requests made by a running script
// net.request fails the whole request on these (ERR_INVALID_ARGUMENT, checked with Electron 45; see main.js upstreamHeaders)
const FORBIDDEN_HEADERS = new Set(['host', 'content-length', 'connection', 'transfer-encoding', 'keep-alive', 'upgrade', 'te', 'trailer', 'expect',
  'cookie2', 'x-http-method', 'x-http-method-override', 'x-method-override']);
const forbiddenHeader = (name) => FORBIDDEN_HEADERS.has(name) || name.startsWith('proxy-');

const sleep = (ms, signal) => new Promise((resolve, reject) => {
  if (signal?.aborted) { reject(new Error('cancelled')); return; }
  const onAbort = () => { clearTimeout(t); reject(new Error('cancelled')); };
  const t = setTimeout(() => { signal?.removeEventListener('abort', onAbort); resolve(); }, ms);
  signal?.addEventListener('abort', onAbort, { once: true });
});

function parseRetryAfter(value) {
  if (!value) return 0;
  const n = Number(value);
  if (Number.isFinite(n)) return Math.max(0, n * 1000);
  const at = Date.parse(value);
  return Number.isFinite(at) ? Math.max(0, at - Date.now()) : 0;
}

function parseSetCookie(line, url) {
  const parts = String(line).split(';').map((p) => p.trim());
  const [pair, ...attrs] = parts;
  const eq = pair.indexOf('=');
  if (eq <= 0) return null;
  const cookie = { url, name: pair.slice(0, eq).trim(), value: pair.slice(eq + 1).trim() };
  for (const attr of attrs) {
    const i = attr.indexOf('=');
    const key = (i < 0 ? attr : attr.slice(0, i)).trim().toLowerCase();
    const val = i < 0 ? '' : attr.slice(i + 1).trim();
    if (key === 'domain' && val) cookie.domain = val;
    else if (key === 'path' && val) cookie.path = val;
    else if (key === 'secure') cookie.secure = true;
    else if (key === 'httponly') cookie.httpOnly = true;
    else if (key === 'max-age' && /^-?\d+$/.test(val)) cookie.expirationDate = Date.now() / 1000 + Number(val);
    else if (key === 'expires' && !cookie.expirationDate) { const t = Date.parse(val); if (Number.isFinite(t)) cookie.expirationDate = t / 1000; }
    else if (key === 'samesite') cookie.sameSite = { lax: 'lax', strict: 'strict', none: 'no_restriction' }[val.toLowerCase()] || 'unspecified';
  }
  return cookie;
}

class PluginHttp {
  constructor({ session, assertPublicHost, userAgent, minIntervalMs = 250 }) {
    this.session = session;
    this.assertPublicHost = assertPublicHost;
    this.userAgent = userAgent;
    this.minIntervalMs = minIntervalMs;
    this.nextSlot = new Map(); // host -> earliest time of the next request
  }

  async pace(host, signal) {
    const now = Date.now();
    const slot = Math.max(now, this.nextSlot.get(host) || 0);
    this.nextSlot.set(host, slot + this.minIntervalMs);
    if (this.nextSlot.size > 2000) this.nextSlot.clear();
    if (slot > now) await sleep(slot - now, signal);
  }

  async cookieHeader(url) {
    try {
      const list = await this.session.cookies.get({ url });
      return list.map((c) => `${c.name}=${c.value}`).join('; ');
    } catch { return ''; }
  }

  async storeCookies(url, setCookie) {
    const lines = Array.isArray(setCookie) ? setCookie : setCookie ? [setCookie] : [];
    for (const line of lines.slice(0, 50)) {
      const cookie = parseSetCookie(line, url);
      if (!cookie) continue;
      try {
        if (cookie.expirationDate !== undefined && cookie.expirationDate < Date.now() / 1000) await this.session.cookies.remove(url, cookie.name);
        else await this.session.cookies.set(cookie);
      } catch { /* a cookie the jar refuses (bad domain) is dropped */ }
    }
  }

  buildHeaders(url, given, cookie) {
    const headers = { 'User-Agent': this.userAgent, Accept: '*/*', 'Accept-Language': 'vi-VN,vi;q=0.9,en-US;q=0.8,en;q=0.7' };
    const lower = new Map(Object.keys(headers).map((k) => [k.toLowerCase(), k]));
    let ownCookie = '';
    for (const [k, v] of Object.entries(given || {})) {
      const key = String(k).trim();
      const lk = key.toLowerCase();
      if (!key || forbiddenHeader(lk) || v === undefined || v === null) continue;
      if (lk === 'cookie') { ownCookie = String(v); continue; }
      if (lower.has(lk)) delete headers[lower.get(lk)];
      headers[key] = String(v);
      lower.set(lk, key);
    }
    // the jar's cookies first, the extension's own Cookie header after (a name in both: the extension's value wins)
    const merged = new Map();
    for (const part of `${cookie}; ${ownCookie}`.split(';')) {
      const i = part.indexOf('=');
      if (i > 0) merged.set(part.slice(0, i).trim(), part.slice(i + 1).trim());
    }
    if (merged.size) headers.Cookie = [...merged].map(([k, v]) => `${k}=${v}`).join('; ');
    return headers;
  }

  requestOnce({ url, method, headers, body, signal, onResponse }) {
    return new Promise((resolve, reject) => {
      if (signal?.aborted) { reject(new Error('cancelled')); return; }
      const req = net.request({ url, method, session: this.session, useSessionCookies: false, redirect: 'manual' });
      for (const [k, v] of Object.entries(headers)) {
        try { req.setHeader(k, v); } catch { /* a value the network stack refuses (non Latin-1) is left out */ }
      }
      let settled = false;
      const fail = (e) => { if (!settled) { settled = true; reject(e); } };
      const onAbort = () => { try { req.abort(); } catch { /* ended */ } fail(new Error(signal?.reason?.message || 'cancelled')); };
      signal?.addEventListener('abort', onAbort, { once: true });
      req.on('redirect', (status, _method, location, responseHeaders) => {
        settled = true;
        try { req.abort(); } catch { /* ended */ }
        resolve({ redirect: true, status, location, headers: responseHeaders || {} });
      });
      req.on('response', (res) => {
        settled = true;
        resolve(onResponse(res, req));
      });
      req.on('error', fail);
      if (body && body.length) req.write(body);
      req.end();
    });
  }

  async collect(res, req, limit) {
    const chunks = [];
    let size = 0;
    for await (const chunk of res) {
      size += chunk.length;
      if (size > limit) { try { req.abort(); } catch { /* ended */ } throw new Error('response too large'); }
      chunks.push(chunk);
    }
    return Buffer.concat(chunks);
  }

  /**
   * One extension request: {url, method, headers, body (string) | bodyBase64, timeout}.
   * -> {ok, status, statusText, url, headers, body (base64), requestHeaders} | {ok: false, status: 504, error}
   */
  async request(req, { signal: outer } = {}) {
    const timeout = Math.min(Math.max(Number(req.timeout) || DEFAULT_TIMEOUT, 1000), 300000);
    const ctl = new AbortController();
    const timer = setTimeout(() => ctl.abort(new Error('timeout')), timeout);
    const onOuter = () => ctl.abort(new Error('cancelled'));
    outer?.addEventListener('abort', onOuter, { once: true });
    try {
      let url = String(req.url || '');
      let method = String(req.method || 'GET').toUpperCase();
      if (!/^(GET|POST|PUT|DELETE|PATCH|HEAD|OPTIONS)$/.test(method)) throw new Error(`unsupported method ${method}`);
      let body = req.bodyBase64 ? Buffer.from(String(req.bodyBase64), 'base64') : (req.body === null || req.body === undefined ? null : Buffer.from(String(req.body), 'utf8'));
      let attempts = 0;
      for (let hop = 0; ; hop++) {
        if (!/^https?:\/\//i.test(url)) throw new Error('unsupported scheme');
        await this.assertPublicHost(url);
        const host = new URL(url).host;
        await this.pace(host, ctl.signal);
        const headers = this.buildHeaders(url, req.headers, await this.cookieHeader(url));
        const r = await this.requestOnce({
          url, method, headers, body, signal: ctl.signal,
          onResponse: async (res, raw) => ({ res, bytes: method === 'HEAD' ? Buffer.alloc(0) : await this.collect(res, raw, MAX_BODY) }),
        });
        if (r.redirect) {
          await this.storeCookies(url, r.headers['set-cookie']);
          if (hop >= MAX_REDIRECTS) throw new Error('too many redirects');
          url = new URL(r.location, url).href;
          if (r.status === 303 || ((r.status === 301 || r.status === 302) && method === 'POST')) { method = 'GET'; body = null; }
          continue;
        }
        const { res, bytes } = r;
        await this.storeCookies(url, res.headers['set-cookie']);
        const status = res.statusCode;
        if ((status === 429 || status === 503) && attempts < 2) {
          const wait = parseRetryAfter(res.headers['retry-after']);
          if (wait > 0 && wait <= RETRY_AFTER_SCRIPT_MAX_MS) {
            attempts += 1;
            this.nextSlot.set(host, Date.now() + wait);
            await sleep(wait, ctl.signal);
            hop -= 1;
            continue;
          }
        }
        const outHeaders = {};
        for (const [k, v] of Object.entries(res.headers)) outHeaders[k.toLowerCase()] = Array.isArray(v) ? v.join(', ') : String(v);
        return {
          ok: status >= 200 && status < 300, status, statusText: res.statusMessage || '', url, headers: outHeaders, body: bytes.toString('base64'),
          requestHeaders: { cookie: headers.Cookie || '', 'user-agent': headers['User-Agent'] || '' },
        };
      }
    } catch (err) {
      return { ok: false, status: 504, headers: {}, body: '', url: String(req.url || ''), error: String(err && err.message ? err.message : err) };
    } finally {
      clearTimeout(timer);
      outer?.removeEventListener('abort', onOuter);
    }
  }

  /**
   * Download url into dest (via dest + '.part'): resumes an existing .part with Range, honours Retry-After, verifies
   * nothing itself (the caller checks the bytes). -> {status, bytes, contentType}
   */
  async downloadToFile(url, dest, { headers = {}, signal, maxBytes = 100 * 1024 * 1024, timeout = 120000 } = {}) {
    const part = `${dest}.part`;
    let attempts = 0;
    for (;;) {
      let have = 0;
      try { have = (await fsp.stat(part)).size; } catch { have = 0; }
      const ctl = new AbortController();
      const timer = setTimeout(() => ctl.abort(new Error('timeout')), timeout);
      const onOuter = () => ctl.abort(new Error('cancelled'));
      signal?.addEventListener('abort', onOuter, { once: true });
      try {
        let target = url;
        let result = null;
        for (let hop = 0; hop <= MAX_REDIRECTS; hop++) {
          if (!/^https?:\/\//i.test(target)) throw new Error('unsupported scheme');
          await this.assertPublicHost(target);
          const host = new URL(target).host;
          await this.pace(host, ctl.signal);
          const h = this.buildHeaders(target, headers, await this.cookieHeader(target));
          h.Accept = headers.Accept || 'image/avif,image/webp,image/apng,image/*,*/*;q=0.8';
          if (have > 0) h.Range = `bytes=${have}-`;
          const r = await this.requestOnce({
            url: target, method: 'GET', headers: h, body: null, signal: ctl.signal,
            onResponse: async (res, raw) => {
              const status = res.statusCode;
              if (status === 429 || status === 503) {
                for await (const _ of res) { /* drain */ } // eslint-disable-line no-unused-vars
                return { retryAfter: parseRetryAfter(res.headers['retry-after']), status };
              }
              if (status === 416 && have > 0) { for await (const _ of res) { /* drain */ } return { complete: true, status }; } // eslint-disable-line no-unused-vars
              if (status !== 200 && status !== 206) { for await (const _ of res) { /* drain */ } return { failed: true, status }; } // eslint-disable-line no-unused-vars
              const append = status === 206 && have > 0;
              const out = fs.createWriteStream(part, { flags: append ? 'a' : 'w' });
              let size = append ? have : 0;
              try {
                for await (const chunk of res) {
                  size += chunk.length;
                  if (size > maxBytes) { try { raw.abort(); } catch { /* ended */ } throw new Error('file too large'); }
                  if (!out.write(chunk)) await new Promise((ok) => out.once('drain', ok));
                }
              } finally {
                await new Promise((ok) => out.end(ok));
              }
              const ct = Array.isArray(res.headers['content-type']) ? res.headers['content-type'][0] : res.headers['content-type'];
              return { done: true, status, bytes: size, contentType: String(ct || '') };
            },
          });
          if (r.redirect) {
            await this.storeCookies(target, r.headers['set-cookie']);
            target = new URL(r.location, target).href;
            continue;
          }
          result = r;
          break;
        }
        if (!result) throw new Error('too many redirects');
        if (result.retryAfter !== undefined) {
          if (attempts < 3) {
            attempts += 1;
            await sleep(Math.min(Math.max(result.retryAfter, 2000 * attempts), RETRY_AFTER_MAX_MS), signal);
            continue;
          }
          throw new Error(`HTTP ${result.status}`);
        }
        if (result.failed) throw Object.assign(new Error(`HTTP ${result.status}`), { status: result.status });
        await fsp.rename(part, dest);
        const bytes = (await fsp.stat(dest)).size;
        return { status: result.status, bytes, contentType: result.contentType || '' };
      } finally {
        clearTimeout(timer);
        signal?.removeEventListener('abort', onOuter);
      }
    }
  }
}

module.exports = { PluginHttp, parseSetCookie, parseRetryAfter, sleep };
