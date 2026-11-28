// Lynxx extension dev tool helper: one HTTP request for cli.js (which needs it synchronously and runs this file with
// spawnSync). Reads {url, method, headers, body, bodyBase64, timeout, allowPrivate} as JSON on stdin, prints
// {ok, status, statusText, url, headers, body (base64), setCookie} as JSON on stdout. Redirects are followed by hand so
// every hop is checked against loopback / private addresses.
'use strict';

const dns = require('node:dns').promises;
const net = require('node:net');

function privateIp(ip) {
  if (net.isIPv4(ip)) {
    const [a, b] = ip.split('.').map(Number);
    return a === 0 || a === 10 || a === 127 || a >= 224 || (a === 169 && b === 254) || (a === 172 && b >= 16 && b <= 31) || (a === 192 && b === 168) || (a === 100 && b >= 64 && b <= 127);
  }
  const v = ip.toLowerCase();
  return v === '::' || v === '::1' || v.startsWith('fc') || v.startsWith('fd') || v.startsWith('fe8') || v.startsWith('fe9') || v.startsWith('fea') || v.startsWith('feb') || v.startsWith('::ffff:');
}

async function checkHost(url, allowPrivate) {
  const u = new URL(url);
  if (!/^https?:$/.test(u.protocol)) throw new Error('unsupported scheme');
  if (allowPrivate) return;
  const host = u.hostname.replace(/^\[|\]$/g, '');
  if (host === 'localhost' || host.endsWith('.localhost')) throw new Error('blocked host');
  const addrs = net.isIP(host) ? [{ address: host }] : await dns.lookup(host, { all: true });
  if (!addrs.length || addrs.some((a) => privateIp(a.address))) throw new Error('blocked host (private address)');
}

(async () => {
  const chunks = [];
  for await (const c of process.stdin) chunks.push(c);
  const req = JSON.parse(Buffer.concat(chunks).toString('utf8'));
  let url = String(req.url);
  let method = String(req.method || 'GET').toUpperCase();
  let body = req.bodyBase64 ? Buffer.from(req.bodyBase64, 'base64') : (req.body === null || req.body === undefined ? undefined : String(req.body));
  const setCookie = [];
  const signal = AbortSignal.timeout(Math.min(Number(req.timeout) || 60000, 300000));
  for (let hop = 0; hop < 10; hop++) {
    await checkHost(url, req.allowPrivate);
    const res = await fetch(url, { method, headers: req.headers || {}, body: method === 'GET' || method === 'HEAD' ? undefined : body, redirect: 'manual', signal });
    setCookie.push(...(res.headers.getSetCookie ? res.headers.getSetCookie() : []));
    if ([301, 302, 303, 307, 308].includes(res.status) && res.headers.get('location')) {
      url = new URL(res.headers.get('location'), url).href;
      if (res.status === 303 || ((res.status === 301 || res.status === 302) && method === 'POST')) { method = 'GET'; body = undefined; }
      continue;
    }
    const buf = Buffer.from(await res.arrayBuffer());
    const headers = {};
    res.headers.forEach((v, k) => { headers[k.toLowerCase()] = v; });
    process.stdout.write(JSON.stringify({ ok: res.ok, status: res.status, statusText: res.statusText, url, headers, body: buf.toString('base64'), setCookie }));
    return;
  }
  throw new Error('too many redirects');
})().catch((err) => { process.stderr.write(String(err && err.message ? err.message : err)); process.exit(1); });
