// Lynxx main process: IPC for extensions (vBook format), book downloads, the collection library, the reader and exports.
// Kept out of main.js; main passes its helpers in (handle = trusted-sender IPC, sidecar, dialogs, confirmations).
'use strict';

const fs = require('node:fs');
const fsp = fs.promises;
const os = require('node:os');
const path = require('node:path');
const crypto = require('node:crypto');
const { pathToFileURL } = require('node:url');
const { BrowserWindow, dialog, protocol, shell, app } = require('electron');
const { PluginHost } = require('./host.js');
const { BookJobs } = require('./downloader.js');

const LIB_MIME = {
  '.jpg': 'image/jpeg', '.jpeg': 'image/jpeg', '.png': 'image/png', '.gif': 'image/gif', '.webp': 'image/webp', '.avif': 'image/avif',
  '.bmp': 'image/bmp', '.svg': 'image/svg+xml',
};
const EXPORT_FORMATS = {
  epub2: { name: 'EPUB 2', ext: ['epub'] }, epub3: { name: 'EPUB 3', ext: ['epub'] }, kepub: { name: 'KEPUB (Kobo)', ext: ['epub'] },
  cbz: { name: 'CBZ', ext: ['cbz'] }, pdf: { name: 'PDF', ext: ['pdf'] },
};
const IMPORT_EXTENSIONS = ['epub', 'txt', 'mobi', 'prc', 'azw', 'azw3', 'fb2', 'fbz', 'umd', 'pdf', 'cbz', 'zip', 'html', 'htm', 'xhtml', 'md', 'markdown',
  'chm', 'doc', 'dot', 'wps', 'docx', 'docm', 'dotx', 'dotm', 'odt', 'ott', 'fodt', 'rtf', 'hwp', 'hwpx', 'hwpml'];

function libraryRootOf(userData) { return path.join(userData, 'library'); }

function registerLibProtocol(userData) {
  // lynxx-lib://lib/<path inside the managed library>: pages, covers and chapter images for the reader / library page
  const root = path.resolve(libraryRootOf(userData));
  protocol.handle('lynxx-lib', async (request) => {
    try {
      const u = new URL(request.url);
      if (u.host !== 'lib') return new Response('not found', { status: 404 });
      const rel = decodeURIComponent(u.pathname.replace(/^\/+/, ''));
      const file = path.resolve(root, rel);
      if (file !== root && !file.startsWith(root + path.sep)) return new Response('forbidden', { status: 403 });
      const real = await fsp.realpath(file);
      if (!real.startsWith(await fsp.realpath(root) + path.sep)) return new Response('forbidden', { status: 403 });
      const type = LIB_MIME[path.extname(real).toLowerCase()];
      if (!type) return new Response('forbidden', { status: 403 });
      const buf = await fsp.readFile(real);
      // re-downloads keep the file names (0001.jpg, cover.jpg): never serve a cached copy
      return new Response(buf, { headers: { 'content-type': type, 'cache-control': 'no-cache' } });
    } catch {
      return new Response('not found', { status: 404 });
    }
  });
}

function setupReader(ctx) {
  const { handle, sidecar, send, confirm, getWindow, approvedPaths, userData, assertPublicHost, browserUA, log, isDev } = ctx;
  const libraryRoot = libraryRootOf(userData);
  const host = new PluginHost({
    dataDir: userData, assertPublicHost, isDev,
    userAgent: (ses) => browserUA(ses.getUserAgent()),
    unpackZip: (zip, dest) => sidecar.call('plugins.unpack', { archive: zip, dest }, 60000),
    log: (text) => log(text),
  });
  host.init();

  const exportCollection = async (params) => {
    const { collectionId, format, options, output } = params;
    try {
      return await sidecar.call('export.build', { collectionId, format, options, output }, 1800000);
    } catch (err) {
      if (err.code === -32011 && err.data?.transcode?.length) {
        // the sidecar lists at most 5000 files per answer: convert, ask again, until it has everything
        const map = {};
        const cleanups = [];
        try {
          let pending = err.data.transcode;
          for (let round = 0; round < 20; round++) {
            const t = await transcodeImages(pending.filter((p) => !(p in map)));
            cleanups.push(t.cleanup);
            Object.assign(map, t.map);
            try { return await sidecar.call('export.build', { collectionId, format, options, output, replacements: map }, 1800000); } catch (again) {
              if (again.code !== -32011 || !again.data?.transcode?.some((p) => !(p in map))) throw again;
              pending = again.data.transcode;
            }
          }
          throw new Error('quá nhiều ảnh cần chuyển định dạng');
        } finally { for (const c of cleanups) await c(); }
      }
      if (err.code === -32010) {
        const tmp = await fsp.mkdtemp(path.join(os.tmpdir(), 'lynxx-print-'));
        try {
          const bundle = await sidecar.call('export.bundle', { collectionId, options, output: path.join(tmp, 'book.html') }, 600000);
          await printToPdf(bundle.path, output, bundle.title);
          return await sidecar.call('export.record', { collectionId, format: 'pdf', output, options: bundle.options, chapterIds: bundle.chapters }, 60000);
        } finally { await fsp.rm(tmp, { recursive: true, force: true }); }
      }
      throw err;
    }
  };

  const refreshAutoExports = async (collectionId) => {
    let targets = [];
    try { targets = await sidecar.call('export.autoTargets', { collectionId }, 30000); } catch { return; }
    for (const t of targets) {
      try { await exportCollection({ collectionId, format: t.format, options: t.options, output: t.path }); } catch (err) { log(`auto export ${t.path}: ${err.message}`); }
    }
  };

  const jobs = new BookJobs({
    host, sidecar, libraryRoot, log,
    emit: (job) => send('book:update', job),
    notify: (job) => { if (job.status === 'done' && job.collectionId && job.progress.chaptersDone) refreshAutoExports(job.collectionId); ctx.notifyBook?.(job); },
    exportAfter: (collectionId, spec) => exportCollection({ collectionId, ...spec }),
  });

  const pluginCall = async (id, fn) => {
    const { svc } = host.service(String(id || ''));
    return fn(svc);
  };
  const str = (v, max = 4000) => String(v ?? '').slice(0, max);

  // ------------------------------------------------------------ extensions
  handle('plugins:list', () => host.list());
  handle('plugins:repositories', () => host.state.repositories || []);
  handle('plugins:installFolder', async () => {
    const r = await dialog.showOpenDialog(getWindow(), { title: 'Chọn thư mục extension (có plugin.json)', properties: ['openDirectory'] });
    if (r.canceled || !r.filePaths[0]) return null;
    if (!fs.existsSync(path.join(r.filePaths[0], 'plugin.json'))) throw new Error('thư mục không có plugin.json');
    const ok = await confirm(`Cài extension từ thư mục ${path.basename(r.filePaths[0])}?`,
      'Extension vBook là JavaScript chạy trong sandbox của Lynxx và gửi request tới website của nó thay bạn. Chỉ cài từ nguồn bạn tin cậy; bạn chịu trách nhiệm về quyền tải nội dung từ website đó.');
    if (!ok) return null;
    return host.installFromDir(r.filePaths[0], { origin: 'folder' });
  });
  handle('plugins:installZip', async () => {
    const r = await dialog.showOpenDialog(getWindow(), { title: 'Chọn plugin.zip', properties: ['openFile'], filters: [{ name: 'Extension vBook', extensions: ['zip'] }] });
    if (r.canceled || !r.filePaths[0]) return null;
    const ok = await confirm(`Cài extension từ ${path.basename(r.filePaths[0])}?`,
      'Extension vBook là JavaScript chạy trong sandbox của Lynxx và gửi request tới website của nó thay bạn. Chỉ cài từ nguồn bạn tin cậy; bạn chịu trách nhiệm về quyền tải nội dung từ website đó.');
    if (!ok) return null;
    return host.installFromZip(r.filePaths[0], { origin: 'zip' });
  });
  handle('plugins:installUrl', async (_e, url, repository) => {
    const target = str(url);
    if (!/^https?:\/\//i.test(target)) throw new Error('link phải bắt đầu bằng http(s)://');
    const ok = await confirm('Cài extension từ link này?', `${target}\n\nExtension vBook là JavaScript chạy trong sandbox của Lynxx và gửi request tới website của nó thay bạn. Chỉ cài từ nguồn bạn tin cậy.`);
    if (!ok) return null;
    return host.installFromUrl(target, { repository: str(repository) });
  });
  handle('plugins:repoFetch', (_e, url) => {
    const target = str(url);
    if (!/^https?:\/\//i.test(target)) throw new Error('link kho phải bắt đầu bằng http(s)://');
    return host.fetchRepository(target);
  });
  handle('plugins:repoAdd', (_e, url) => {
    const target = str(url);
    if (!/^https?:\/\//i.test(target)) throw new Error('link kho phải bắt đầu bằng http(s)://');
    return host.addRepository(target);
  });
  handle('plugins:repoRemove', (_e, url) => host.removeRepository(str(url)));
  handle('plugins:uninstall', async (_e, id) => {
    const info = host.info(host.package(str(id, 60)));
    const ok = await confirm(`Gỡ extension ${info.name}?`, 'Truyện đã tải vẫn còn trong thư viện; chỉ không cập nhật được chương mới cho tới khi cài lại.');
    if (!ok) return false;
    return host.uninstall(info.id);
  });
  handle('plugins:setEnabled', (_e, id, enabled) => host.setEnabled(str(id, 60), enabled === true));
  handle('plugins:setConfig', (_e, id, values) => host.setConfig(str(id, 60), values && typeof values === 'object' ? values : {}));
  handle('plugins:openSite', (_e, id) => host.openSite(str(id, 60), getWindow()));
  handle('plugins:match', (_e, url) => host.matchUrl(str(url)));
  handle('plugins:home', (_e, id) => pluginCall(id, (s) => s.home()));
  handle('plugins:genre', (_e, id) => pluginCall(id, (s) => s.genre()));
  handle('plugins:listItems', (_e, id, script, input, page) => pluginCall(id, (s) => s.list(str(script, 100), str(input), str(page, 2000))));
  handle('plugins:search', (_e, id, query, page) => pluginCall(id, (s) => s.search(str(query, 500), str(page, 2000))));
  handle('plugins:detail', (_e, id, url) => pluginCall(id, (s) => s.detail(str(url))));
  handle('plugins:toc', (_e, id, url) => pluginCall(id, (s) => s.toc(str(url))));

  // ------------------------------------------------------------ downloads
  // `then` (export after download) is main's own: an export path never comes from the renderer
  handle('book:download', (_e, params) => jobs.start(params && typeof params === 'object' ? { ...params, then: null, collectionId: '' } : {}));
  handle('book:jobs', () => jobs.list());
  handle('book:cancel', (_e, id) => jobs.cancel(str(id, 100)));
  handle('book:clear', () => jobs.clearFinished());

  // ------------------------------------------------------------ library
  handle('library:collections', () => sidecar.call('library.collections', {}, 60000));
  handle('library:collection', (_e, id) => sidecar.call('library.collection', { collectionId: str(id, 100) }, 60000));
  handle('library:update', async (_e, id) => {
    const detail = await sidecar.call('library.collection', { collectionId: str(id, 100) }, 60000);
    if (!detail.pluginId || !detail.bookUrl) throw new Error('truyện này không đến từ extension: không có chương mới để cập nhật');
    return jobs.start({ pluginId: detail.pluginId, url: detail.bookUrl, scope: 'missing', collectionId: detail.id, label: `Cập nhật: ${detail.title}` });
  });
  handle('library:fillRemote', async (_e, id) => {
    const detail = await sidecar.call('library.collection', { collectionId: str(id, 100) }, 60000);
    if (!detail.remoteChapters) throw new Error('không có chương nào còn link ảnh gốc để tải');
    const ok = await confirm(`Tải ảnh cho ${detail.remoteChapters} chương của "${detail.title}"?`,
      'Các chương này chỉ có link ảnh (file gốc chưa nhúng ảnh). Lynxx tải ảnh từ các link đó vào thư viện; sau đó có thể xuất lại EPUB đầy đủ ảnh.');
    if (!ok) return null;
    return jobs.startRemote({ collectionId: detail.id, label: `Tải ảnh còn thiếu: ${detail.title}` });
  });
  handle('library:delete', async (_e, id) => {
    const detail = await sidecar.call('library.collection', { collectionId: str(id, 100) }, 60000);
    const ok = await confirm(`Xoá "${detail.title}" khỏi thư viện?`, 'Mọi chương đã tải và file gốc trong thư viện Lynxx bị xoá khỏi ổ đĩa. File đã xuất (EPUB/PDF…) ở nơi bạn lưu không bị xoá. Không thể hoàn tác.');
    if (!ok) return false;
    return (await sidecar.call('library.collection.delete', { collectionId: detail.id }, 120000)).ok;
  });
  handle('library:importFile', async () => {
    const r = await dialog.showOpenDialog(getWindow(), {
      title: 'Nhập sách / truyện từ máy', properties: ['openFile', 'multiSelections'],
      filters: [{ name: 'Sách, truyện, tài liệu', extensions: IMPORT_EXTENSIONS }, { name: 'Tất cả', extensions: ['*'] }],
    });
    if (r.canceled) return [];
    const out = [];
    for (const file of r.filePaths.slice(0, 50)) {
      try {
        const imported = await sidecar.call('library.import', { source: file, title: '' }, 1800000);
        out.push({ ok: true, file: path.basename(file), ...imported });
      } catch (err) { out.push({ ok: false, file: path.basename(file), error: err.message }); }
    }
    return out;
  });

  // ------------------------------------------------------------ exports
  handle('library:export', async (_e, params) => {
    const p = params && typeof params === 'object' ? params : {};
    const collectionId = str(p.collectionId, 100);
    const format = str(p.format, 10);
    if (!EXPORT_FORMATS[format]) throw new Error('định dạng xuất không hợp lệ');
    const { name } = await sidecar.call('export.suggest', { collectionId, format }, 30000);
    const r = await dialog.showSaveDialog(getWindow(), {
      title: `Xuất ${EXPORT_FORMATS[format].name}`, defaultPath: path.join(app.getPath('documents'), name),
      filters: [{ name: EXPORT_FORMATS[format].name, extensions: EXPORT_FORMATS[format].ext }],
    });
    if (r.canceled || !r.filePath) return null;
    let output = r.filePath;
    const want = format === 'kepub' ? '.kepub.epub' : `.${EXPORT_FORMATS[format].ext[0]}`;
    if (!output.toLowerCase().endsWith(want)) output = output.replace(/\.(kepub\.epub|epub|cbz|pdf)$/i, '') + want;
    const options = p.options && typeof p.options === 'object' ? p.options : {};
    if (p.downloadMissing) {
      const detail = await sidecar.call('library.collection', { collectionId }, 60000);
      if (detail.pluginId && detail.bookUrl && detail.chapters.some((c) => c.state !== 'ready' && !c.locked)) {
        const job = jobs.start({ pluginId: detail.pluginId, url: detail.bookUrl, scope: 'missing', collectionId, label: `Tải chương còn thiếu rồi xuất: ${detail.title}`, then: { export: { format, options, output } } });
        return { queued: true, job };
      }
    }
    const result = await exportCollection({ collectionId, format, options, output });
    return { ...result, queued: false };
  });
  handle('library:exportRefresh', async (_e, collectionId, exportId) => {
    const detail = await sidecar.call('library.collection', { collectionId: str(collectionId, 100) }, 60000);
    const exp = detail.exports.find((x) => x.id === str(exportId, 100));
    if (!exp) throw new Error('không tìm thấy file đã xuất');
    return exportCollection({ collectionId: detail.id, format: exp.format, options: exp.options, output: exp.path });
  });
  handle('library:exportForget', (_e, exportId) => sidecar.call('export.forget', { exportId: str(exportId, 100) }, 30000));
  handle('library:revealExport', async (_e, collectionId, exportId) => {
    const detail = await sidecar.call('library.collection', { collectionId: str(collectionId, 100) }, 60000);
    const exp = detail.exports.find((x) => x.id === str(exportId, 100));
    if (exp && fs.existsSync(exp.path)) shell.showItemInFolder(exp.path);
    return Boolean(exp);
  });

  // ------------------------------------------------------------ reader
  handle('reader:chapter', async (_e, chapterId) => {
    const data = await sidecar.call('reader.chapter', { chapterId: str(chapterId, 100) }, 120000);
    if (data.kind === 'pdf' && data.path) {
      approvedPaths.add(data.path);
      data.url = pathToFileURL(data.path).href;
    }
    return data;
  });
  handle('reader:progressGet', (_e, chapterId) => sidecar.call('reader.progress.get', { chapterId: str(chapterId, 100) }, 30000));
  handle('reader:progressSet', (_e, p) => {
    const q = p && typeof p === 'object' ? p : {};
    return sidecar.call('reader.progress.set', {
      chapterId: str(q.chapterId, 100), page: Math.max(0, Math.floor(Number(q.page) || 0)), scrollOffset: Math.max(0, Number(q.scrollOffset) || 0),
      mode: ['vertical', 'horizontal', 'paged'].includes(q.mode) ? q.mode : 'vertical', zoom: Math.min(5, Math.max(0.2, Number(q.zoom) || 1)),
      direction: q.direction === 'rtl' ? 'rtl' : 'ltr',
    }, 30000);
  });

  return { host, jobs, exportCollection, shutdown: () => { jobs.shutdown(); host.shutdown(); } };
}

// ------------------------------------------------------------ Chromium helpers

async function transcodeImages(paths) {
  // WebP / GIF / AVIF / BMP pages -> JPEG (white background) with Chromium's decoders, for the PDF writer
  const dir = await fsp.mkdtemp(path.join(os.tmpdir(), 'lynxx-transcode-'));
  const win = new BrowserWindow({ show: false, webPreferences: { sandbox: true, contextIsolation: true, nodeIntegration: false, offscreen: true } });
  const map = {};
  try {
    await win.loadURL('about:blank');
    let crashed = null;
    win.webContents.on('render-process-gone', (_e, d) => { crashed = new Error(`image converter stopped (${d.reason})`); });
    const withTimeout = (p, ms) => Promise.race([p, new Promise((_, ko) => setTimeout(() => ko(crashed || new Error('image conversion timed out')), ms))]);
    for (const file of paths.slice(0, 20000)) {
      if (crashed) throw crashed;
      // the bytes go in as a data: URL (a file:// image would taint the canvas)
      const bytes = await fsp.readFile(file);
      const head = bytes.subarray(0, 12).toString('latin1');
      const mime = head.startsWith('RIFF') && head.slice(8) === 'WEBP' ? 'image/webp' : head.startsWith('GIF8') ? 'image/gif'
        : head.slice(4, 11) === 'ftypavi' ? 'image/avif' : head.startsWith('BM') ? 'image/bmp' : 'image/png';
      const source = `data:${mime};base64,${bytes.toString('base64')}`;
      // canvases stop at 32767 px a side (long webtoon strips): such images are scaled down to fit
      const dataUrl = await withTimeout(win.webContents.executeJavaScript(`(async () => {
        const img = new Image(); img.src = ${JSON.stringify(source)}; await img.decode();
        const scale = Math.min(1, 32000 / Math.max(img.naturalWidth, img.naturalHeight), Math.sqrt(250e6 / (img.naturalWidth * img.naturalHeight)));
        const c = document.createElement('canvas'); c.width = Math.max(1, Math.floor(img.naturalWidth * scale)); c.height = Math.max(1, Math.floor(img.naturalHeight * scale));
        const g = c.getContext('2d'); g.fillStyle = '#fff'; g.fillRect(0, 0, c.width, c.height); g.drawImage(img, 0, 0, c.width, c.height);
        return c.toDataURL('image/jpeg', 0.92);
      })()`, true), 120000);
      if (!/^data:image\/jpeg;base64,./.test(String(dataUrl))) throw new Error(`không chuyển được ảnh ${path.basename(file)}`);
      const out = path.join(dir, `${crypto.createHash('sha1').update(file).digest('hex')}.jpg`);
      await fsp.writeFile(out, Buffer.from(String(dataUrl).split(',')[1] || '', 'base64'));
      map[file] = out;
    }
  } finally {
    if (!win.isDestroyed()) win.destroy();
  }
  return { map, cleanup: () => fsp.rm(dir, { recursive: true, force: true }) };
}

async function printToPdf(htmlPath, output, title) {
  const win = new BrowserWindow({ show: false, width: 900, height: 1200, webPreferences: { sandbox: true, contextIsolation: true, nodeIntegration: false, javascript: false, offscreen: true } });
  try {
    win.webContents.setWindowOpenHandler(() => ({ action: 'deny' }));
    win.webContents.on('will-navigate', (e) => e.preventDefault());
    let crashed = null;
    win.webContents.on('render-process-gone', (_e, d) => { crashed = new Error(`PDF renderer stopped (${d.reason})`); });
    await win.loadFile(htmlPath);
    const pdf = await Promise.race([
      win.webContents.printToPDF({ printBackground: true, preferCSSPageSize: true, generateDocumentOutline: true, generateTaggedPDF: true, displayHeaderFooter: false }),
      new Promise((_, ko) => setTimeout(() => ko(crashed || new Error('PDF printing timed out')), 20 * 60 * 1000)),
    ]);
    const tmp = `${output}.${process.pid}.tmp`;
    await fsp.writeFile(tmp, pdf);
    await fsp.rename(tmp, output);
    return { path: output, size: pdf.length, title };
  } finally {
    if (!win.isDestroyed()) win.destroy();
  }
}

module.exports = { registerLibProtocol, setupReader, IMPORT_EXTENSIONS, EXPORT_FORMATS, _transcodeImages: transcodeImages, _printToPdf: printToPdf };
