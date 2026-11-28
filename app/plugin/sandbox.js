// Lynxx extension sandbox page: runs one extension script per 'run' message with the extension runtime.
'use strict';

(() => {
  const bridge = window.lynxxSandbox;
  const caches = new Map(); // cacheStorage per extension, kept while this sandbox window lives
  const host = {
    request: (req) => bridge.call('request', req),
    sleep: (ms) => { bridge.call('sleep', ms); },
    log: (level, text) => bridge.log(level, text),
    storage: (op, key, value) => bridge.call('storage', { op, key, value }),
    cookie: (op, url, value) => bridge.call('cookie', { op, url, value }),
    browser: (op, payload) => bridge.call('browser', { op, ...(payload || {}) }),
    crypto: (op, args) => bridge.call('crypto', { op, args }),
    graphics: (payload) => bridge.call('graphics', payload),
  };
  bridge.onRun((job) => {
    let result;
    try {
      if (!caches.has(job.pluginId)) caches.set(job.pluginId, new Map());
      result = window.LynxxRuntime.runScript({ files: job.files, entry: job.entry, args: job.args || [], host, ctx: { ...(job.ctx || {}), cache: caches.get(job.pluginId) } });
      result = JSON.parse(JSON.stringify(result)); // plain data only crosses the bridge
    } catch (err) {
      result = { ok: false, error: String(err && err.message ? err.message : err) };
    }
    bridge.done(job.id, result);
  });
  bridge.ready();
})();
