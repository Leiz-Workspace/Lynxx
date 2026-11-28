// Lynxx book downloads (Electron main): extension detail + toc -> library collection; every chapter that is not in
// the library yet -> chap script -> pages downloaded into staging ("<n>.part", resumed with Range, Retry-After honoured,
// bytes checked to be an image) -> the sidecar moves them into the managed library and records Page rows with sha256.
// Chapters already downloaded are never fetched again; an "update" run adds the chapters the site published since.
'use strict';

const fs = require('node:fs');
const fsp = fs.promises;
const path = require('node:path');
const crypto = require('node:crypto');
const { htmlToText } = require('./service.js');
const H = require('./html.js');

const IMAGE_CONCURRENCY = 3;
const PAGE_RETRIES = 3;
const CHAPTER_DELAY_MS = 400;
const JOBS_KEEP = 200;

function sniffImage(buf) {
  if (buf.length >= 3 && buf[0] === 0xff && buf[1] === 0xd8 && buf[2] === 0xff) return 'jpg';
  if (buf.length >= 8 && buf.subarray(0, 8).equals(Buffer.from([0x89, 0x50, 0x4e, 0x47, 0x0d, 0x0a, 0x1a, 0x0a]))) return 'png';
  if (buf.length >= 6 && /^GIF8[79]a$/.test(buf.subarray(0, 6).toString('latin1'))) return 'gif';
  if (buf.length >= 12 && buf.subarray(0, 4).toString('latin1') === 'RIFF' && buf.subarray(8, 12).toString('latin1') === 'WEBP') return 'webp';
  if (buf.length >= 12 && /^ftypavi[fs]$/.test(buf.subarray(4, 12).toString('latin1'))) return 'avif';
  if (buf.length >= 2 && buf[0] === 0x42 && buf[1] === 0x4d) return 'bmp';
  return '';
}

async function readHead(file, n = 16) {
  const fh = await fsp.open(file, 'r');
  try { const buf = Buffer.alloc(n); const { bytesRead } = await fh.read(buf, 0, n, 0); return buf.subarray(0, bytesRead); } finally { await fh.close(); }
}

function referer(policy, pageUrl) {
  if (policy === 'no-referrer') return {};
  if (policy === 'origin') { try { return { Referer: `${new URL(pageUrl).origin}/` }; } catch { return {}; } }
  return pageUrl ? { Referer: pageUrl } : {};
}

class BookJobs {
  /**
   * opts: {host (PluginHost), sidecar, libraryRoot, emit(job), log(text), notify(job)}
   */
  constructor(opts) {
    this.opts = opts;
    this.jobs = new Map();
    this.seq = 0;
    this.running = 0;
    this.maxRunning = 1;
  }

  public(job) {
    // a snapshot: listeners keep what they got even while the job moves on
    const { ctl, chapterUrls, ...rest } = job;
    return { ...rest, progress: { ...job.progress }, log: [...job.log] };
  }

  emit(job) { this.opts.emit(this.public(job)); }

  list() { return [...this.jobs.values()].map((j) => this.public(j)); }

  /**
   * start({pluginId, url, scope: 'all' | 'missing' | 'selected', chapterUrls?: [], label?, then?: {export: {...}}})
   */
  start(params) {
    const pluginId = String(params.pluginId || '');
    const url = String(params.url || '');
    if (!pluginId || !/^https?:\/\//i.test(url)) throw new Error('cần extension và link truyện');
    const dup = [...this.jobs.values()].find((j) => j.pluginId === pluginId && j.url === url && ['queued', 'running', 'cancelling'].includes(j.status));
    if (dup) throw new Error('truyện này đang được tải');
    const id = `book-${Date.now()}-${++this.seq}`;
    const job = {
      id, kind: 'book', pluginId, url, label: String(params.label || ''), status: 'queued', createdAt: Date.now(), startedAt: 0, endedAt: 0,
      scope: ['all', 'missing', 'selected', 'none'].includes(params.scope) ? params.scope : 'missing',
      chapterUrls: Array.isArray(params.chapterUrls) ? params.chapterUrls.map(String).slice(0, 20000) : [],
      progress: { phase: 'queued', chaptersTotal: 0, chaptersDone: 0, chaptersFailed: 0, chaptersSkipped: 0, pagesDone: 0, pagesTotal: 0, bytes: 0, current: '' },
      collectionId: params.collectionId || '', title: '', error: '', log: [], then: params.then || null,
    };
    this.jobs.set(id, job);
    this.trim();
    this.emit(job);
    this.pump();
    return this.public(job);
  }

  trim() {
    if (this.jobs.size <= JOBS_KEEP) return;
    for (const [id, j] of this.jobs) {
      if (this.jobs.size <= JOBS_KEEP) break;
      if (['done', 'failed', 'cancelled'].includes(j.status)) this.jobs.delete(id);
    }
  }

  pump() {
    for (const job of this.jobs.values()) {
      if (this.running >= this.maxRunning) return;
      if (job.status !== 'queued') continue;
      this.running += 1;
      job.status = 'running';
      job.startedAt = Date.now();
      job.ctl = new AbortController();
      this.emit(job);
      this.run(job).then(() => {
        job.status = job.ctl.signal.aborted ? 'cancelled' : (job.progress.chaptersFailed ? 'done' : 'done');
      }, (err) => {
        job.status = job.ctl.signal.aborted ? 'cancelled' : 'failed';
        job.error = String(err && err.message ? err.message : err).slice(0, 500);
      }).finally(() => {
        job.endedAt = Date.now();
        job.progress.phase = job.status;
        this.running -= 1;
        this.emit(job);
        try { this.opts.notify?.(this.public(job)); } catch { /* optional */ }
        this.pump();
      });
    }
  }

  cancel(id) {
    const job = this.jobs.get(id);
    if (!job) return false;
    if (job.status === 'queued') { job.status = 'cancelled'; job.endedAt = Date.now(); this.emit(job); return true; }
    if (job.status === 'running') { job.status = 'cancelling'; job.ctl.abort(new Error('cancelled')); this.emit(job); return true; }
    return false;
  }

  clearFinished() {
    for (const [id, j] of this.jobs) if (['done', 'failed', 'cancelled'].includes(j.status)) this.jobs.delete(id);
    return this.list();
  }

  active() { return [...this.jobs.values()].filter((j) => ['queued', 'running', 'cancelling'].includes(j.status)); }

  shutdown() { for (const j of this.jobs.values()) if (j.status === 'running' || j.status === 'queued') this.cancel(j.id); }

  note(job, text) {
    job.log.push(text);
    if (job.log.length > 60) job.log.splice(0, job.log.length - 60);
    this.opts.log(`[${job.id}] ${text}`);
  }

  check(job) { if (job.ctl.signal.aborted) throw new Error('cancelled'); }

  /**
   * Pages known only by address (an imported export that links its undownloaded chapters): download them into the
   * library, chapter by chapter, like extension downloads.
   */
  startRemote({ collectionId, label }) {
    const id = `book-${Date.now()}-${++this.seq}`;
    const dup = [...this.jobs.values()].find((j) => j.collectionId === collectionId && j.remote && ['queued', 'running', 'cancelling'].includes(j.status));
    if (dup) throw new Error('đang tải ảnh cho truyện này');
    const job = {
      id, kind: 'book', remote: true, pluginId: '', url: '', label: String(label || ''), status: 'queued', createdAt: Date.now(), startedAt: 0, endedAt: 0, scope: 'missing', chapterUrls: [],
      progress: { phase: 'queued', chaptersTotal: 0, chaptersDone: 0, chaptersFailed: 0, chaptersSkipped: 0, pagesDone: 0, pagesTotal: 0, bytes: 0, current: '' },
      collectionId, title: String(label || ''), error: '', log: [], then: null,
    };
    this.jobs.set(id, job);
    this.trim();
    this.emit(job);
    this.pump();
    return this.public(job);
  }

  async runRemote(job) {
    const { sidecar } = this.opts;
    const chapters = await sidecar.call('library.collection.remote', { collectionId: job.collectionId }, 60000);
    job.progress.chaptersTotal = chapters.length;
    job.progress.phase = 'chapters';
    this.emit(job);
    const svc = { image: async () => { throw new Error('no image script'); } };
    for (const chapter of chapters) {
      this.check(job);
      job.progress.current = chapter.title;
      const dir = path.join(this.opts.libraryRoot, 'staging', job.collectionId, chapter.id);
      try {
        await fsp.mkdir(dir, { recursive: true });
        job.progress.pagesTotal = chapter.pages.length;
        job.progress.pagesDone = 0;
        this.emit(job);
        const files = [];
        for (const [i, url] of chapter.pages.entries()) {
          // no chapter page is known for these links: no Referer (the way an EPUB reader would load them)
          files.push(await this.page(job, svc, { url, fallback: [], referrerPolicy: 'no-referrer', script: '' }, '', dir, i + 1));
          job.progress.pagesDone += 1;
          this.emit(job);
        }
        await sidecar.call('library.chapter.commitImages', { chapterId: chapter.id, pages: files.map((f, i) => ({ path: f.path, sourceUrl: chapter.pages[i] })) }, 300000);
        job.progress.chaptersDone += 1;
      } catch (err) {
        if (job.ctl.signal.aborted) throw err;
        job.progress.chaptersFailed += 1;
        this.note(job, `${chapter.title}: ${err.message}`);
      }
      this.emit(job);
    }
    job.progress.current = '';
  }

  async run(job) {
    if (job.remote) return this.runRemote(job);
    const { host, sidecar } = this.opts;
    const { pkg, caps, svc } = host.service(job.pluginId);
    if (!caps.downloadable) throw new Error('extension này không tải được (thiếu detail / toc / chap)');
    job.progress.phase = 'detail';
    this.emit(job);
    const detail = await svc.detail(job.url);
    this.check(job);
    job.title = detail.name || job.url;
    job.progress.phase = 'toc';
    this.emit(job);
    const toc = await svc.toc(detail.url || job.url);
    this.check(job);
    if (!toc.length) throw new Error('extension không trả về chương nào');
    const book = await sidecar.call('library.book.upsert', {
      pluginId: pkg.id, pluginName: pkg.manifest.metadata.name, source: pkg.manifest.metadata.source, url: job.url, kind: caps.kind,
      detail, chapters: toc,
    }, 60000);
    job.collectionId = book.collectionId;
    // cover: once, or again when it failed before
    if (detail.cover && !book.hasCover) {
      try {
        const dir = path.join(this.opts.libraryRoot, 'staging', book.collectionId);
        await fsp.mkdir(dir, { recursive: true });
        const dest = path.join(dir, 'cover.img');
        await host.http.downloadToFile(detail.cover, dest, { headers: referer(detail.coverReferrerPolicy, job.url), signal: job.ctl.signal, maxBytes: 20 * 1024 * 1024 });
        if (sniffImage(await readHead(dest))) await sidecar.call('library.book.cover', { collectionId: book.collectionId, path: dest, sourceUrl: detail.cover }, 60000);
        else await fsp.rm(dest, { force: true });
      } catch (err) { if (job.ctl.signal.aborted) throw err; this.note(job, `ảnh bìa: ${err.message}`); }
    }
    const wanted = new Set(job.chapterUrls);
    const todo = book.chapters.filter((c) => {
      if (c.state === 'ready') return false;
      if (job.scope === 'none') return false;
      if (job.scope === 'selected') return wanted.has(c.url);
      return true;
    });
    const locked = todo.filter((c) => c.locked);
    job.progress.chaptersSkipped = book.chapters.length - todo.length + locked.length;
    const queue = todo.filter((c) => !c.locked);
    job.progress.chaptersTotal = queue.length;
    job.progress.phase = 'chapters';
    this.emit(job);
    for (const chapter of queue) {
      this.check(job);
      job.progress.current = chapter.title;
      this.emit(job);
      try {
        if (caps.kind === 'comic') await this.comicChapter(job, svc, book, chapter);
        else await this.novelChapter(job, svc, book, chapter);
        job.progress.chaptersDone += 1;
      } catch (err) {
        if (job.ctl.signal.aborted) throw err;
        job.progress.chaptersFailed += 1;
        this.note(job, `${chapter.title}: ${err.message}`);
        await sidecar.call('library.chapter.state', { chapterId: chapter.id, state: 'failed', error: String(err.message).slice(0, 300) }, 30000).catch(() => {});
      }
      this.emit(job);
      await new Promise((ok) => setTimeout(ok, CHAPTER_DELAY_MS));
    }
    job.progress.current = '';
    if (job.then?.export && !job.ctl.signal.aborted) {
      job.progress.phase = 'export';
      this.emit(job);
      await this.opts.exportAfter?.(job.collectionId, job.then.export);
    }
  }

  async comicChapter(job, svc, book, chapter) {
    const { host, sidecar } = this.opts;
    const result = await svc.chap(chapter.url);
    this.check(job);
    const images = result.images || [];
    if (!images.length) throw new Error('chương không có ảnh');
    const dir = path.join(this.opts.libraryRoot, 'staging', book.collectionId, chapter.id);
    await fsp.mkdir(dir, { recursive: true });
    job.progress.pagesTotal = images.length;
    job.progress.pagesDone = 0;
    this.emit(job);
    const files = new Array(images.length);
    let next = 0;
    let failed = '';
    const worker = async () => {
      while (next < images.length && !failed) {
        const index = next++;
        try {
          files[index] = await this.page(job, svc, images[index], chapter.url, dir, index + 1);
          job.progress.pagesDone += 1;
          this.emit(job);
        } catch (err) {
          if (job.ctl.signal.aborted) throw err;
          failed = `trang ${index + 1}: ${err.message}`;
        }
      }
    };
    await Promise.all(Array.from({ length: Math.min(IMAGE_CONCURRENCY, images.length) }, worker));
    this.check(job);
    if (failed) throw new Error(failed);
    const commit = await sidecar.call('library.chapter.commitImages', {
      // a page drawn by an image script has no link of its own (its input is the script's, ex. a scrambled image + strips)
      chapterId: chapter.id, pages: files.map((f, i) => ({ path: f.path, sourceUrl: images[i].script ? '' : images[i].url })),
    }, 300000);
    job.progress.bytes += files.reduce((n, f) => n + f.bytes, 0);
    return commit;
  }

  async page(job, svc, image, chapterUrl, dir, number) {
    const { host } = this.opts;
    const stem = path.join(dir, String(number).padStart(4, '0'));
    // a page finished by an earlier (cancelled / failed) run is reused as it is
    for (const ext of ['jpg', 'png', 'webp', 'gif', 'avif', 'bmp']) {
      const done = `${stem}.${ext}`;
      try { const st = await fsp.stat(done); if (st.size > 0) return { path: done, bytes: st.size }; } catch { /* not yet */ }
    }
    if (image.script) {
      const b64 = await svc.image(image.script, image.url);
      const buf = Buffer.from(String(b64).replace(/^data:[^,]*,/, ''), 'base64');
      const ext = sniffImage(buf);
      if (!ext) throw new Error('script ảnh không trả về ảnh hợp lệ');
      const dest = `${stem}.${ext}`;
      await fsp.writeFile(dest, buf);
      return { path: dest, bytes: buf.length };
    }
    const urls = [image.url, ...(image.fallback || [])];
    let lastError = null;
    for (const url of urls) {
      // the partial file is named after its URL: a .part of a fallback link is never resumed against another link
      const tmp = `${stem}.${crypto.createHash('sha1').update(url).digest('hex').slice(0, 12)}.download`;
      for (let attempt = 0; attempt < PAGE_RETRIES; attempt++) {
        this.check(job);
        try {
          const r = await host.http.downloadToFile(url, tmp, { headers: { ...referer(image.referrerPolicy, chapterUrl) }, signal: job.ctl.signal, maxBytes: 60 * 1024 * 1024 });
          const ext = sniffImage(await readHead(tmp));
          if (!ext) { await fsp.rm(tmp, { force: true }); throw new Error('nội dung tải về không phải ảnh'); }
          const dest = `${stem}.${ext}`;
          await fsp.rename(tmp, dest);
          return { path: dest, bytes: r.bytes };
        } catch (err) {
          if (job.ctl.signal.aborted) throw err;
          lastError = err;
          if (err.status === 404 || err.status === 403 || err.status === 410) break; // next fallback URL
          await new Promise((ok) => setTimeout(ok, 1000 * (attempt + 1)));
        }
      }
    }
    throw lastError || new Error('không tải được ảnh');
  }

  async novelChapter(job, svc, book, chapter) {
    const { host, sidecar } = this.opts;
    const result = await svc.chap(chapter.url);
    this.check(job);
    const html = String(result.html || '');
    if (!htmlToText(html).trim() && !/<img/i.test(html)) throw new Error('chương trống');
    // images inside a novel chapter are downloaded too (best effort); the sidecar rewrites their src
    const dir = path.join(this.opts.libraryRoot, 'staging', book.collectionId, chapter.id);
    const images = [];
    const doc = H.parse(html, chapter.url);
    const srcs = [...new Set(H.select(doc, 'img').map((img) => img.getAttr('src') || img.getAttr('data-src') || '').filter(Boolean))].slice(0, 200);
    if (srcs.length) await fsp.mkdir(dir, { recursive: true });
    let n = 0;
    for (const src of srcs) {
      this.check(job);
      let abs = '';
      try { abs = new URL(src, chapter.url).href; } catch { continue; }
      if (!/^https?:/i.test(abs)) continue;
      const dest = path.join(dir, `img-${String(++n).padStart(3, '0')}`);
      try {
        await host.http.downloadToFile(abs, dest, { headers: referer('', chapter.url), signal: job.ctl.signal, maxBytes: 30 * 1024 * 1024 });
        if (sniffImage(await readHead(dest))) images.push({ src, path: dest });
        else await fsp.rm(dest, { force: true });
      } catch (err) { if (job.ctl.signal.aborted) throw err; this.note(job, `${chapter.title}: ảnh ${n}: ${err.message}`); }
    }
    await sidecar.call('library.chapter.commitText', { chapterId: chapter.id, html, title: result.title || '', images }, 120000);
  }
}

module.exports = { BookJobs, sniffImage };
