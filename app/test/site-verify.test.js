// node app/test/site-verify.test.js
'use strict';
const assert = require('assert');
const {
  headerValues, isChallengeResponse, browserLikeUA, siteBaseHost, sameSite, cookieForHost, clearanceHosts, envProxyRules, ChallengeTracker,
  mergeHttpMaps, withSiteEntry,
} = require('../site-verify');

// User-Agent: Electron and the app's own tokens go, the Chrome version and everything else stay as they are
const electronUa = 'Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 (KHTML, like Gecko) lynxx-desktop/2.0.13 Chrome/142.0.7444.59 '
  + 'Electron/45.2.1 Safari/537.36';
const browserUa = 'Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/142.0.7444.59 Safari/537.36';
assert.strictEqual(browserLikeUA(electronUa, ['Lynxx', 'lynxx-desktop']), browserUa);
assert.strictEqual(browserLikeUA(electronUa.replace('lynxx-desktop', 'Lynxx'), ['Lynxx']), browserUa);
assert.strictEqual(browserLikeUA(browserUa, ['Lynxx']), browserUa); // already clean: unchanged (a persist session keeps it)
assert.strictEqual(browserLikeUA('Electron/45.2.1 Chrome/1.2', []), 'Chrome/1.2');
assert.ok(!/Electron\//.test(browserLikeUA(electronUa)));
assert.ok(/Chrome\/142\.0\.7444\.59/.test(browserLikeUA(electronUa, ['lynxx-desktop'])));
// a name that is a regex is taken literally
assert.strictEqual(browserLikeUA('A/1 x.y/2 xzy/3', ['x.y']), 'A/1 xzy/3');

// the core's challenge rule: 403 / 429 / 503 with cf-mitigated: challenge, header name and value in any letter case
assert.strictEqual(isChallengeResponse(403, { 'cf-mitigated': ['challenge'] }), true);
assert.strictEqual(isChallengeResponse(503, { 'CF-Mitigated': ['Challenge'] }), true);
assert.strictEqual(isChallengeResponse(429, { 'cf-mitigated': 'CHALLENGE' }), true);
assert.strictEqual(isChallengeResponse(200, { 'cf-mitigated': ['challenge'] }), false);
assert.strictEqual(isChallengeResponse(403, {}), false); // a plain 403 is the site's own refusal, not a challenge
assert.strictEqual(isChallengeResponse(403, { 'cf-mitigated': ['block'] }), false);
assert.strictEqual(isChallengeResponse(403, null), false);
assert.deepStrictEqual(headerValues({ 'Set-Cookie': ['a', 'b'], other: 'x' }, 'set-cookie'), ['a', 'b']);

// top-level navigation: https on the site's host or a subdomain (www. aside), same port, no credentials in the address
const site = 'https://www.example.com/';
assert.strictEqual(siteBaseHost(site), 'example.com');
assert.strictEqual(siteBaseHost('http://example.com/'), '');
assert.strictEqual(siteBaseHost('not a url'), '');
assert.ok(sameSite('https://example.com/posts?x=1', site));
assert.ok(sameSite('https://www.example.com/', site));
assert.ok(sameSite('https://img.example.com/a', site));
assert.ok(sameSite('https://EXAMPLE.com./', site));
assert.ok(!sameSite('http://example.com/', site)); // plain http
assert.ok(!sameSite('https://example.com.evil.net/', site));
assert.ok(!sameSite('https://evilexample.com/', site));
assert.ok(!sameSite('https://challenges.cloudflare.com/x', site)); // the challenge frame loads, but never as the top page
assert.ok(!sameSite('https://user:pw@example.com/', site));
assert.ok(!sameSite('https://example.com:8443/', site));
assert.ok(!sameSite('javascript:alert(1)', site));
assert.ok(!sameSite('file:///etc/passwd', site));
assert.ok(!sameSite('https://example.com/', 'http://example.com/')); // a module whose site is not https gets no window at all
assert.ok(!sameSite('', site));

// cookie domains: the site's host must be the domain or a subdomain of it
assert.ok(cookieForHost('.example.com', 'www.example.com'));
assert.ok(cookieForHost('example.com', 'example.com'));
assert.ok(!cookieForHost('.challenges.cloudflare.com', 'example.com'));
assert.ok(!cookieForHost('sub.example.com', 'example.com'));
assert.ok(!cookieForHost('', 'example.com'));
assert.ok(!cookieForHost('ample.com', 'example.com'));

// tracker: a cookie set while the challenge page is shown is not enough, the page loaded after it without a challenge is
{
  const t = new ChallengeTracker();
  assert.strictEqual(t.mainFrame(403, { 'cf-mitigated': ['challenge'] }), true);
  assert.strictEqual(t.passed, false);
  assert.strictEqual(t.notNeeded, false);
  t.cookieChanged({ value: 'v1', expirationDate: 1900000000.5 }, false);
  assert.strictEqual(t.passed, false);
  t.mainFrame(200, {});
  assert.strictEqual(t.passed, true);
  assert.deepStrictEqual(t.clearance, { value: 'v1', expires: 1900000000.5 });
  assert.strictEqual(t.notNeeded, false); // a challenge was seen
  // a cookie, then a challenge again (the cookie no longer works): not passed until a page loads without one
  t.mainFrame(503, { 'CF-MITIGATED': ['challenge'] });
  assert.strictEqual(t.passed, false);
  // overwrite: the old value is removed, the new one added; removing a value that is not the current one changes nothing
  t.cookieChanged({ value: 'v1' }, true);
  assert.strictEqual(t.clearance, null);
  t.cookieChanged({ value: 'v2' }, false);
  t.cookieChanged({ value: 'v1' }, true);
  assert.strictEqual(t.clearance.value, 'v2');
  assert.strictEqual(t.clearance.expires, 0); // a session cookie
  t.mainFrame(200, {});
  assert.strictEqual(t.passed, true);
  t.cookie(null); // the 1 s poll found none
  assert.strictEqual(t.passed, false);
}
{
  // a redirect is no page: an edge redirect before the challenge (apex -> www) with a cookie does not pass
  const t = new ChallengeTracker();
  t.cookie({ value: 'stale', expirationDate: 1 });
  assert.strictEqual(t.mainFrame(301, {}), false);
  assert.strictEqual(t.passed, false);
  assert.strictEqual(t.main, null);
  // the challenge, then the 302 that ends it (its Set-Cookie not seen yet): still the challenge
  t.mainFrame(403, { 'cf-mitigated': ['challenge'] });
  t.mainFrame(302, {});
  assert.strictEqual(t.passed, false);
  assert.strictEqual(t.main.challenge, true);
  t.mainFrame(200, {});
  assert.strictEqual(t.passed, true);
  // a 304 is the cached page; an error page is no page
  t.mainFrame(304, {});
  assert.strictEqual(t.passed, true);
  t.mainFrame(500, {});
  assert.strictEqual(t.passed, false);
  t.mainFrame(404, {});
  assert.strictEqual(t.passed, false);
}
{
  // no challenge, no cookie: nothing to verify; a site error is not "no challenge" (the window stays for the user)
  const t = new ChallengeTracker();
  assert.strictEqual(t.notNeeded, false); // nothing loaded yet
  t.mainFrame(301, {});
  assert.strictEqual(t.notNeeded, false); // a redirect alone: its page decides
  t.mainFrame(200, {});
  assert.strictEqual(t.notNeeded, true);
  assert.strictEqual(t.passed, false);
  t.mainFrame(500, {});
  assert.strictEqual(t.notNeeded, false);
  t.mainFrame(301, {});
  assert.strictEqual(t.notNeeded, false); // still the error page
  t.mainFrame(200, {});
  assert.strictEqual(t.notNeeded, true);
  t.cookie({ value: 'x' });
  assert.strictEqual(t.notNeeded, false);
  assert.strictEqual(t.passed, true);
  t.cookie({ value: '' }); // an empty value is no cookie
  assert.strictEqual(t.clearance, null);
}

// the stored cf_clearance a new check removes first: the site's host, its apex and its www. variant
assert.deepStrictEqual(clearanceHosts('https://www.example.com/'), ['www.example.com', 'example.com']);
assert.deepStrictEqual(clearanceHosts('https://Example.com.:8443/x'), ['example.com', 'www.example.com']);
assert.deepStrictEqual(clearanceHosts('https://203.0.113.5/'), ['203.0.113.5']);
assert.deepStrictEqual(clearanceHosts('http://example.com/'), []);
assert.deepStrictEqual(clearanceHosts('junk'), []);

// the core's environment proxy as Chromium proxy rules: none -> direct, credentials refused, nothing Chromium cannot use
assert.deepStrictEqual(envProxyRules(''), { rules: '' });
assert.deepStrictEqual(envProxyRules(null), { rules: '' });
assert.deepStrictEqual(envProxyRules('http://127.0.0.1:3128'), { rules: 'http://127.0.0.1:3128' });
assert.deepStrictEqual(envProxyRules(' http://proxy.example:8080/ '), { rules: 'http://proxy.example:8080' });
assert.deepStrictEqual(envProxyRules('proxy.example:8080'), { rules: 'http://proxy.example:8080' }); // requests reads it as http
assert.deepStrictEqual(envProxyRules('socks5h://127.0.0.1:1080'), { rules: 'socks5://127.0.0.1:1080' });
assert.deepStrictEqual(envProxyRules('socks4a://h:1080'), { rules: 'socks4://h:1080' });
assert.deepStrictEqual(envProxyRules('https://[::1]:8443'), { rules: 'https://[::1]:8443' });
assert.deepStrictEqual(envProxyRules('http://user:pw@proxy.example:8080'), { login: true });
assert.deepStrictEqual(envProxyRules('socks5://user@proxy.example:1080'), { login: true });
for (const bad of ['ftp://h:21', 'socks://h:1080', 'http://h:1/path', 'http://h:1/?x', 'http://', 'pac+http://h/x.pac']) {
  assert.deepStrictEqual(envProxyRules(bad), { invalid: true }, bad);
}

// merge: the module's other entries and the other modules stay, a name in another letter case is replaced
assert.deepStrictEqual(mergeHttpMaps({ 'user-agent': 'old', a: '1' }, { 'User-Agent': 'new' }), { a: '1', 'User-Agent': 'new' });
{
  const all = { zz: { other: '1', 'user-agent': 'old' }, yy: { 'User-Agent': 'keep' } };
  const out = withSiteEntry(all, 'zz', { 'User-Agent': 'new' });
  assert.deepStrictEqual(out, { zz: { other: '1', 'User-Agent': 'new' }, yy: { 'User-Agent': 'keep' } });
  assert.deepStrictEqual(all.zz, { other: '1', 'user-agent': 'old' }); // the input is not changed
  assert.deepStrictEqual(withSiteEntry({ zz: { shm_session: 's', CF_CLEARANCE: 'a' } }, 'zz', { cf_clearance: 'b' }),
    { zz: { shm_session: 's', cf_clearance: 'b' } });
  assert.deepStrictEqual(withSiteEntry(null, 'zz', { cf_clearance: 'b' }), { zz: { cf_clearance: 'b' } });
  assert.deepStrictEqual(withSiteEntry({ zz: 'junk' }, 'zz', { cf_clearance: 'b' }), { zz: { cf_clearance: 'b' } });
}
console.log('site-verify ok');
