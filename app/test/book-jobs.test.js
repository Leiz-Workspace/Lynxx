// node app/test/book-jobs.test.js — book download jobs with a fake extension host and a fake sidecar (no Electron)
'use strict';

const assert = require('assert');
const fs = require('fs');
const os = require('os');
const path = require('path');
const { BookJobs } = require('../plugin/downloader.js');

const JPEG = Buffer.from([0xff, 0xd8, 0xff, 0xe0, 0, 16, 74, 70, 73, 70, 0]);
let failures = 0;
const tests = [];
const test = (name, fn) => tests.push([name, fn]);

function setup({ delayMs = 0, failPage = '' } = {}) {
  const root = fs.mkdtempSync(path.join(os.tmpdir(), 'lynxx-jobs-'));
  const calls = [];
  const downloads = [];
  const chapters = [
    { id: 'c1', url: 'https://s.test/b/1/c1', title: 'Chương 1', state: 'ready', locked: false },
    { id: 'c2', url: 'https://s.test/b/1/c2', title: 'Chương 2', state: 'pending', locked: false },
    { id: 'c3', url: 'https://s.test/b/1/c3', title: 'Chương 3', state: 'failed', locked: false },
    { id: 'c4', url: 'https://s.test/b/1/c4', title: 'Chương 4 (VIP)', state: 'pending', locked: true },
  ];
  const svc = {
    detail: async (url) => ({ url, name: 'Truyện', cover: 'https://s.test/cover.jpg', coverReferrerPolicy: '' }),
    toc: async () => chapters.map((c) => ({ name: c.title, url: c.url, locked: c.locked })),
    chap: async (url) => ({ kind: 'comic', images: [1, 2, 3].map((n) => ({ url: `${url}/p${n}.jpg`, fallback: n === 2 ? [`${url}/alt${n}.jpg`] : [], referrerPolicy: '', script: '' })) }),
    image: async () => JPEG.toString('base64'),
  };
  const host = {
    service: () => ({ pkg: { id: 'site-1', manifest: { metadata: { name: 'Site', source: 'https://s.test' } } }, caps: { downloadable: true, kind: 'comic' }, svc }),
    http: {
      downloadToFile: async (url, dest, { signal }) => {
        downloads.push({ url, referer: undefined });
        if (delayMs) await new Promise((ok, ko) => { const t = setTimeout(ok, delayMs); signal?.addEventListener('abort', () => { clearTimeout(t); ko(new Error('cancelled')); }); });
        if (failPage && url.endsWith(failPage)) throw Object.assign(new Error('HTTP 404'), { status: 404 });
        fs.mkdirSync(path.dirname(dest), { recursive: true });
        fs.writeFileSync(dest, JPEG);
        return { status: 200, bytes: JPEG.length };
      },
    },
  };
  const sidecar = {
    call: async (method, params) => {
      calls.push([method, params]);
      if (method === 'library.book.upsert') return { collectionId: 'col1', created: false, hasCover: false, chapters };
      if (method === 'library.chapter.commitImages') {
        for (const p of params.pages) assert.ok(fs.existsSync(p.path), `staged page exists: ${p.path}`);
        return { pageCount: params.pages.length };
      }
      return { ok: true };
    },
  };
  const events = [];
  const jobs = new BookJobs({ host, sidecar, libraryRoot: root, emit: (j) => events.push(j), log: () => {}, notify: () => {} });
  const wait = (id) => new Promise((resolve) => {
    const check = () => { const j = jobs.list().find((x) => x.id === id); if (['done', 'failed', 'cancelled'].includes(j.status)) resolve(j); else setTimeout(check, 10); };
    check();
  });
  return { root, calls, downloads, jobs, wait, events };
}

test('downloads only chapters that are not ready and not locked; pages go through staging', async () => {
  const t = setup();
  const job = t.jobs.start({ pluginId: 'site-1', url: 'https://s.test/b/1', scope: 'missing' });
  const done = await t.wait(job.id);
  assert.strictEqual(done.status, 'done');
  assert.strictEqual(done.progress.chaptersTotal, 2); // c2 + c3 (failed before): c1 ready, c4 locked
  assert.strictEqual(done.progress.chaptersDone, 2);
  const commits = t.calls.filter(([m]) => m === 'library.chapter.commitImages');
  assert.deepStrictEqual(commits.map(([, p]) => p.chapterId), ['c2', 'c3']);
  assert.deepStrictEqual(commits[0][1].pages.map((p) => path.basename(p.path)), ['0001.jpg', '0002.jpg', '0003.jpg']);
  assert.ok(t.calls.some(([m]) => m === 'library.book.cover'));
  assert.ok(!t.downloads.some((d) => d.url.includes('/c1/')));
});

test('selected scope downloads only the chosen chapters', async () => {
  const t = setup();
  const job = t.jobs.start({ pluginId: 'site-1', url: 'https://s.test/b/1', scope: 'selected', chapterUrls: ['https://s.test/b/1/c3'] });
  const done = await t.wait(job.id);
  assert.deepStrictEqual(t.calls.filter(([m]) => m === 'library.chapter.commitImages').map(([, p]) => p.chapterId), ['c3']);
  assert.strictEqual(done.progress.chaptersDone, 1);
});

test('a page that fails on every link marks the chapter failed and the job goes on', async () => {
  const t = setup({ failPage: 'p3.jpg' });
  const job = t.jobs.start({ pluginId: 'site-1', url: 'https://s.test/b/1' });
  const done = await t.wait(job.id);
  assert.strictEqual(done.status, 'done');
  assert.strictEqual(done.progress.chaptersFailed, 2);
  assert.ok(t.calls.some(([m, p]) => m === 'library.chapter.state' && p.state === 'failed'));
});

test('cancel stops a running job; pages already finished are reused by the next run', async () => {
  const t = setup({ delayMs: 40 });
  const job = t.jobs.start({ pluginId: 'site-1', url: 'https://s.test/b/1' });
  // cover (40 ms) + the first chapter's 3 pages in parallel (40 ms): cancel while the job waits between chapters
  await new Promise((ok) => setTimeout(ok, 150));
  assert.ok(t.jobs.cancel(job.id));
  const stopped = await t.wait(job.id);
  assert.strictEqual(stopped.status, 'cancelled');
  const before = t.downloads.length;
  assert.ok(before > 0);
  const again = t.jobs.start({ pluginId: 'site-1', url: 'https://s.test/b/1' });
  await t.wait(again.id);
  const pagesFetched = t.downloads.length - before;
  assert.ok(pagesFetched <= 4, `pages finished by the cancelled run are not fetched again (${pagesFetched})`); // cover + chapter 3
});

test('the same book cannot be queued twice', () => {
  const t = setup({ delayMs: 30 });
  t.jobs.start({ pluginId: 'site-1', url: 'https://s.test/b/1' });
  assert.throws(() => t.jobs.start({ pluginId: 'site-1', url: 'https://s.test/b/1' }), /đang được tải/);
});

(async () => {
  for (const [name, fn] of tests) {
    try { await fn(); console.log(`ok - ${name}`); } catch (err) { failures += 1; console.error(`not ok - ${name}\n${err.stack}`); }
  }
  if (failures) { console.error(`${failures} test(s) failed`); process.exit(1); }
  process.exit(0);
})();
