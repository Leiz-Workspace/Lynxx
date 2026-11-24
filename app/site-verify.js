// Lynxx desktop: pure helpers of "Xác minh trong Lynxx" (main.js verifySite). No Electron here, so
// app/test/site-verify.test.js runs them with plain node.
// The user passes a website's bot challenge (Cloudflare) themself in an app window; the app never solves, clicks or scripts it.
// These decide where that window may go, which User-Agent it shows, when the check counts as passed and how the result joins
// the user's per-site headers / cookies (settings.connection.siteHeaders / siteCookies)
'use strict';

const CHALLENGE_STATUS = new Set([403, 429, 503]);
const CLEARANCE_COOKIE = 'cf_clearance';

const isPlainObject = (v) => Boolean(v) && typeof v === 'object' && !Array.isArray(v);
const escapeRe = (s) => String(s).replace(/[.*+?^${}()|[\]\\]/g, '\\$&');

function headerValues(headers, name) {
  // Electron gives {Name: [values]} in the server's letter case; a plain {name: value} works too
  const want = String(name).toLowerCase();
  const out = [];
  for (const [k, v] of Object.entries(isPlainObject(headers) ? headers : {})) {
    if (k.toLowerCase() === want) for (const x of Array.isArray(v) ? v : [v]) out.push(String(x));
  }
  return out;
}

function isChallengeResponse(status, headers) {
  // the core's rule (lynxx/network.py): 403 / 429 / 503 with 'cf-mitigated: challenge', any letter case
  return CHALLENGE_STATUS.has(Number(status)) && headerValues(headers, 'cf-mitigated').some((v) => v.trim().toLowerCase() === 'challenge');
}

function browserLikeUA(ua, appNames = []) {
  // Electron's own User-Agent names Electron and the app ('… lynxx-desktop/2.0.13 Chrome/142.0.0.0 Electron/45.2.1 Safari/537.36'):
  // both go (WAF rules aimed at Electron, the app advertised to every site). The Chrome version stays: the page's scripts run on
  // that engine, and a User-Agent naming another one can fail the check again and again
  const names = ['Electron', ...appNames].filter((n) => typeof n === 'string' && n.trim()).map((n) => escapeRe(n.trim()));
  const re = new RegExp(`(^|\\s)(?:${names.join('|')})/\\S*`, 'gi');
  return String(ua || '').replace(re, '$1').replace(/\s{2,}/g, ' ').trim();
}

function siteBaseHost(site) {
  // host of a module's https website without a leading 'www.' ('' for anything else)
  try {
    const u = new URL(String(site || ''));
    return u.protocol === 'https:' ? u.hostname.toLowerCase().replace(/\.$/, '').replace(/^www\./, '') : '';
  } catch {
    return '';
  }
}

function sameSite(url, site) {
  // top-level navigation of the verification window: https on the site's host or one of its subdomains, same port. The
  // challenge's own frame (another host) is not a top-level navigation and is not checked here
  const base = siteBaseHost(site);
  let u;
  let s;
  try {
    u = new URL(String(url || ''));
    s = new URL(String(site || ''));
  } catch {
    return false;
  }
  if (!base || u.protocol !== 'https:' || u.username || u.password || u.port !== s.port) return false;
  const h = u.hostname.toLowerCase().replace(/\.$/, '');
  return h === base || h.endsWith(`.${base}`);
}

function cookieForHost(domain, host) {
  // a cookie of 'example.com' / '.example.com' reaches example.com and its subdomains: the site's host must be one of them
  const d = String(domain || '').toLowerCase().replace(/^\./, '').replace(/\.$/, '');
  const h = String(host || '').toLowerCase().replace(/\.$/, '');
  return Boolean(d) && (h === d || h.endsWith(`.${d}`));
}

function clearanceHosts(site) {
  // the hosts whose stored cf_clearance a new check removes first: the site's own, its apex and its www. variant (an address
  // has no variant)
  let host = '';
  try { host = new URL(String(site || '')).hostname.toLowerCase().replace(/\.$/, ''); } catch { return []; }
  const base = siteBaseHost(site);
  if (!base) return [];
  const isAddress = /^[\d.]+$/.test(base) || base.startsWith('[');
  return [...new Set([host, base, ...(isAddress ? [] : [`www.${base}`])])];
}

const PROXY_SCHEMES = { 'http:': 'http', 'https:': 'https', 'socks5:': 'socks5', 'socks5h:': 'socks5', 'socks4:': 'socks4', 'socks4a:': 'socks4' };

function envProxyRules(proxy) {
  // the proxy the core takes from its environment for the site ('scheme://host:port' from the sidecar's net.proxyFor, '' for
  // none) as Chromium proxy rules: {rules: ''} = none (direct), {login: true} one with a user name / password (Chromium takes no
  // credentials there), {invalid: true} one the window cannot use. The value itself is never part of an error
  let p = typeof proxy === 'string' ? proxy.trim() : '';
  if (!p) return { rules: '' };
  if (!/^[a-z][a-z0-9+.-]*:\/\//i.test(p)) p = `http://${p}`; // 'host:port' is an http proxy to requests too
  let u;
  try { u = new URL(p); } catch { return { invalid: true }; }
  if (u.username || u.password) return { login: true };
  const scheme = PROXY_SCHEMES[u.protocol.toLowerCase()];
  if (!scheme || !u.hostname || /[\s;,]/.test(u.host) || (u.pathname && u.pathname !== '/') || u.search || u.hash) return { invalid: true };
  return { rules: `${scheme}://${u.host}` };
}

// What the window has seen: the latest page of the site and its cf_clearance cookie. Passed = a cookie and a latest page that
// loaded without a challenge (a cookie set while the challenge page is still shown waits for the page it reloads to). Only a
// 2xx document (or a 304, the cached one) is a page: a redirect is not (an edge redirect before the challenge, the 302 that
// ends one), and an error page is no page. The window removes the cookie of an earlier check before it loads (main.js
// runVerifyWindow), so a cookie seen here was set during this check
class ChallengeTracker {
  constructor() {
    this.main = null; // {status, challenge, page} of the latest main-frame response that is not a redirect
    this.challenged = false; // a challenge was seen since the window opened
    this.clearance = null; // {value, expires (seconds since 1970, 0 = a session cookie)}
  }

  mainFrame(status, headers) {
    const code = Number(status) || 0;
    const challenge = isChallengeResponse(code, headers);
    if (challenge) this.challenged = true;
    // a redirect leaves the latest page as it was: the page it leads to decides
    if (!challenge && code >= 300 && code < 400 && code !== 304) return challenge;
    this.main = { status: code, challenge, page: !challenge && ((code >= 200 && code < 300) || code === 304) };
    return challenge;
  }

  // the cookie as the window's storage has it now (null: none)
  cookie(c) {
    this.clearance = c && typeof c.value === 'string' && c.value ? { value: c.value, expires: Number(c.expirationDate) || 0 } : null;
  }

  // a cookies 'changed' event: an overwrite removes the old value before it adds the new one
  cookieChanged(c, removed) {
    if (!removed) this.cookie(c);
    else if (this.clearance && c && c.value === this.clearance.value) this.clearance = null;
  }

  get passed() {
    return Boolean(this.clearance && this.main?.page);
  }

  // a page loaded without any challenge and there is no cookie: nothing to verify right now
  get notNeeded() {
    return Boolean(this.main?.page && !this.challenged && !this.clearance);
  }
}

function mergeHttpMaps(base, own) {
  // own (a website's entries) replaces base entries of the same name, letter case aside: the core's maps ignore it
  const names = new Set(Object.keys(own).map((k) => k.toLowerCase()));
  return { ...Object.fromEntries(Object.entries(base).filter(([k]) => !names.has(k.toLowerCase()))), ...own };
}

function withSiteEntry(all, abbr, entry) {
  // the per-site map {abbr: {name: value}} with entry joined to abbr's own entries (same name in any letter case replaced);
  // abbr's other entries and the other modules are kept
  const maps = isPlainObject(all) ? all : {};
  return { ...maps, [abbr]: mergeHttpMaps(isPlainObject(maps[abbr]) ? maps[abbr] : {}, entry) };
}

module.exports = {
  CLEARANCE_COOKIE, headerValues, isChallengeResponse, browserLikeUA, siteBaseHost, sameSite, cookieForHost, clearanceHosts, envProxyRules,
  ChallengeTracker, mergeHttpMaps, withSiteEntry,
};
