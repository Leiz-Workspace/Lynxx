// Browser-only mock of the preload bridge: lets the UI run in a plain browser (design work, screenshots, UI tests)
// with neutral placeholder covers. app.js loads this file only when window.lynxx is missing and the page is not
// running inside Electron (there the preload bridge always exists), so it never ships behaviour into the real app.
(function () {
  if (window.lynxx || /\bElectron\//.test(navigator.userAgent)) return;
  const listeners = {};
  const emit = (ch, data) => (listeners[ch] || []).forEach((cb) => cb(data));
  const delay = (ms) => new Promise((r) => setTimeout(r, ms));
  const ICONS = [
    { id: 'full-mask-cyan', name: 'Full mask · Cyan' }, { id: 'exposed-jaw-cyan', name: 'Exposed jaw · Cyan' },
    { id: 'full-mask-monochrome-1024', name: 'Full mask · Mono' }, { id: 'exposed-jaw-monochrome-1024', name: 'Exposed jaw · Mono' },
  ];
  const settings = {
    version: 4,
    appearance: { theme: 'abyss', icon: 'full-mask-cyan', background: { mode: 'icon', image: '', blur: 4, opacity: 0.9, dim: 0.18 } },
    download: { notify: true }, connection: { proxy: '', ignoreProxy: false }, advanced: { pythonPath: '' }, window: { width: 1360, height: 860 },
  };
  const iconUrl = (id) => `../assets/icons/${id}.png`;
  const appearance = () => ({
    ...settings.appearance, iconUrl: iconUrl(settings.appearance.icon), backgroundUrl: iconUrl(settings.appearance.icon),
    icons: ICONS.map((i) => ({ ...i, url: iconUrl(i.id) })), themes: ['abyss', 'graphite', 'paper', 'oled'], liveTaskbarIcon: true, platform: 'browser',
  });
  const merge = (a, b) => { for (const [k, v] of Object.entries(b)) a[k] = v && typeof v === 'object' && !Array.isArray(v) ? merge(a[k] || {}, v) : v; return a; };

  window.lynxx = {
    rpc: async (method) => {
      if (method === 'app.info') return { name: 'Lynxx', version: '2.1 r0 (mock)', python: '3.13' };
      if (method === 'formats.supported') return { extensions: ['epub', 'pdf', 'txt', 'mobi', 'azw3', 'fb2', 'cbz', 'docx', 'odt', 'rtf', 'html', 'md'] };
      throw new Error(`method not allowed: ${method}`);
    },
    sidecarStatus: async () => ({ ok: true, info: { name: 'Lynxx', version: '2.1 r0 (mock)', python: '3.13' } }),
    settings: {
      get: async () => ({ settings: structuredClone(settings), appearance: appearance() }),
      set: async (patch) => {
        merge(settings, patch);
        if (patch.appearance) emit('appearance:changed', appearance());
        return { settings: structuredClone(settings), appearance: appearance(), iconSync: { launcher: 'unsupported', shortcuts: 0 } };
      },
    },
    dialogs: { folder: async () => '', image: async () => '', document: async () => '' },
    appearance: { setBackgroundImage: async () => appearance() },
    app: {
      info: async () => ({ version: '2.1.0', electron: 'mock', platform: 'browser', userData: '~/.config/Lynxx', python: '.venv/bin/python', core: 'python', repo: '.', sidecar: { name: 'Lynxx', version: '2.1 r0', python: '3.13' } }),
      relaunch: async () => location.reload(), installLauncher: async () => ({ ok: false, message: 'mock' }), openPath: async () => '', openExternal: async () => false,
    },
    thumbUrl: (url) => url, // the mock loads the address itself
    on: (ch, cb) => { (listeners[ch] = listeners[ch] || []).push(cb); return () => {}; },
    ...bookMock(),
  };
  setTimeout(() => emit('sidecar:status', { ok: true, info: { version: '2.1 r0 (mock)' } }), 300);

  // ---------------------------------------------------------------- library / extensions / reader (renderer/books.js)
  function bookMock() {
    const svg = (w, h, label, hue) => `data:image/svg+xml;utf8,${encodeURIComponent(`<svg xmlns='http://www.w3.org/2000/svg' width='${w}' height='${h}'><rect width='100%' height='100%' fill='hsl(${hue},28%,22%)'/><text x='50%' y='50%' fill='hsl(${hue},40%,75%)' font-family='sans-serif' font-size='${Math.round(w / 9)}' text-anchor='middle'>${label}</text></svg>`)}`;
    const comicChapters = Array.from({ length: 12 }, (_, i) => ({ id: `cc${i + 1}`, title: `Chương ${i + 1}`, state: i < 8 ? 'ready' : (i === 8 ? 'failed' : 'pending'), kind: 'image', pages: i < 8 ? 6 : 0, url: `https://comic.example/b/1/c${i + 1}`, locked: i === 11, error: i === 8 ? 'trang 3: HTTP 403' : '', position: i }));
    const novelChapters = Array.from({ length: 30 }, (_, i) => ({ id: `nc${i + 1}`, title: `Chương ${i + 1}: Tiêu đề chương`, state: 'ready', kind: 'text', pages: 0, url: `https://novel.example/b/2/c${i + 1}`, locked: false, error: '', position: i }));
    const collections = [
      { id: 'book-1', title: 'Học Viện Ma Pháp Kỳ Lạ', kind: 'comic', author: 'Tác Giả A', source: 'ComicSite', pluginId: 'comic-1', bookUrl: 'https://comic.example/b/1', coverUrl: svg(300, 400, 'Bìa', 200), chapters: comicChapters, lastChapterId: 'cc3', description: 'Một cậu bé bình thường được nhận vào học viện ma pháp.\nNhững ngày tháng kỳ lạ bắt đầu.', genres: ['Hài hước', 'Học đường', 'Phiêu lưu'], ongoing: true, exports: [] },
      { id: 'book-2', title: 'Kiếm Đạo Độc Tôn', kind: 'novel', author: 'Tác Giả B', source: 'NovelSite', pluginId: 'novel-1', bookUrl: 'https://novel.example/b/2', coverUrl: '', chapters: novelChapters, lastChapterId: '', description: 'Truyện tiên hiệp.', genres: ['Tiên hiệp'], ongoing: false, exports: [{ id: 'e1', format: 'epub2', path: '/home/user/Documents/Kiếm Đạo Độc Tôn_NovelSite.epub', chapters: 30, size: 1200000, updatedAt: '', autoUpdate: true, options: {}, exists: true }] },
      { id: 'local-2', title: 'Truyện nhập từ EPUB vBook', kind: 'comic', author: '', source: 'EPUB', pluginId: '', bookUrl: '', coverUrl: svg(300, 400, 'EPUB', 120), remoteChapters: 2, chapters: [
        { id: 'rc1', title: 'Chương 1', state: 'ready', kind: 'image', pages: 6, url: '', locked: false, error: '', position: 0 },
        { id: 'rc2', title: 'Chương 2', state: 'pending', kind: 'image', pages: 0, url: '', locked: false, error: '', position: 1 },
        { id: 'rc3', title: 'Chương 3', state: 'pending', kind: 'image', pages: 0, url: '', locked: false, error: '', position: 2 }], lastChapterId: '', description: 'Nhập từ file vBook có chương chỉ chứa link ảnh.', genres: [], ongoing: false, exports: [] },
      { id: 'local-1', title: 'Sổ tay ghi chú', kind: 'document', author: '', source: 'DOCX', pluginId: '', bookUrl: '', coverUrl: '', chapters: [{ id: 'dc1', title: 'Phần 1', state: 'ready', kind: 'text', pages: 0, url: '', locked: false, error: '', position: 0 }], lastChapterId: '', description: '', genres: [], ongoing: false, exports: [] },
    ];
    const summary = (c) => ({ id: c.id, title: c.title, kind: c.kind, author: c.author, source: c.source, pluginId: c.pluginId, bookUrl: c.bookUrl, coverUrl: c.coverUrl, chapters: c.chapters.length, ready: c.chapters.filter((x) => x.state === 'ready').length, failed: c.chapters.filter((x) => x.state === 'failed').length, lastChapterId: c.lastChapterId, lastChapterTitle: c.chapters.find((x) => x.id === c.lastChapterId)?.title || '' });
    const progress = {};
    const plugins = [
      { id: 'comic-1', name: 'ComicSite', author: 'mock', version: 3, source: 'https://comic.example', type: 'comic', nsfw: false, enabled: true, supported: true, description: 'Đọc truyện tranh trên ComicSite', capabilities: { content: true, home: true, genre: true, search: true, detail: true, toc: true, chap: true, page: false, downloadable: true, kind: 'comic' }, config: [{ key: 'DOMAIN', title: 'Domain', mode: 'input', value: 'https://comic.example', default: 'https://comic.example' }], missing: [], icon: '' },
      { id: 'novel-1', name: 'NovelSite', author: 'mock', version: 1, source: 'https://novel.example', type: 'novel', nsfw: false, enabled: true, supported: true, description: 'Truyện chữ', capabilities: { content: true, home: true, genre: false, search: true, detail: true, toc: true, chap: true, page: false, downloadable: true, kind: 'novel' }, config: [], missing: [], icon: '' },
      { id: 'tts-1', name: 'Some TTS', author: 'mock', version: 1, source: 'https://tts.example', type: 'tts', nsfw: false, enabled: true, supported: false, unsupportedReason: 'Loại "tts" chưa được hỗ trợ (chỉ novel / comic)', capabilities: { content: false, kind: 'novel' }, config: [], missing: [], icon: '' },
    ];
    const items = (prefix, page) => Array.from({ length: 12 }, (_, i) => ({ name: `${prefix} ${(Number(page) || 1) * 100 + i}`, link: `https://comic.example/b/${i + 1}`, cover: svg(300, 400, String(i + 1), (i * 37) % 360), description: `Chương ${20 + i}`, tag: i % 5 === 0 ? 'Hot' : '', coverReferrerPolicy: '' }));
    const bookJobs = new Map();
    let seq = 0;
    const runJob = async (job) => {
      const steps = ['detail', 'toc'];
      job.status = 'running';
      for (const phase of steps) { job.progress.phase = phase; emit('book:update', { ...job }); await delay(400); }
      job.progress.phase = 'chapters';
      job.progress.chaptersTotal = job.scope === 'none' ? 0 : 4;
      for (let c = 0; c < job.progress.chaptersTotal && job.status === 'running'; c++) {
        job.progress.current = `Chương ${c + 1}`;
        job.progress.pagesTotal = 6;
        for (let p = 0; p < 6 && job.status === 'running'; p++) { job.progress.pagesDone = p + 1; emit('book:update', { ...job }); await delay(120); }
        job.progress.chaptersDone += 1;
      }
      if (job.status === 'running') job.status = 'done';
      job.endedAt = Date.now();
      job.progress.phase = job.status;
      emit('book:update', { ...job });
    };
    return {
      plugins: {
        list: async () => plugins.map((p) => ({ ...p })),
        repositories: async () => ['https://example.org/vbook/plugin.json'],
        installFolder: async () => null, installZip: async () => null,
        installUrl: async (url) => { await delay(300); const p = { ...plugins[1], id: `x-${Date.now()}`, name: url.split('/').slice(-2, -1)[0] || 'Mới' }; plugins.push(p); return p; },
        repoFetch: async (url) => { await delay(300); return { url, author: 'Mock repo', description: '', items: [
          { name: 'ComicSite', author: 'mock', version: 4, type: 'comic', locale: 'vi_VN', source: 'https://comic.example', description: 'Truyện tranh', path: 'https://example.org/comic/plugin.zip', icon: '', nsfw: false, installedVersion: 3, installedId: 'comic-1', supportedType: true },
          { name: 'Another Novel', author: 'mock', version: 2, type: 'novel', locale: 'vi_VN', source: 'https://another.example', description: 'Truyện chữ', path: 'https://example.org/another/plugin.zip', icon: '', nsfw: false, installedVersion: null, installedId: '', supportedType: true },
          { name: 'Google TTS', author: 'mock', version: 1, type: 'tts', locale: '', source: '', description: 'Đọc truyện', path: 'https://example.org/tts/plugin.zip', icon: '', nsfw: false, installedVersion: null, installedId: '', supportedType: false },
        ] }; },
        repoAdd: async (u) => [u], repoRemove: async () => [],
        uninstall: async (id) => { const i = plugins.findIndex((p) => p.id === id); if (i >= 0) plugins.splice(i, 1); return true; },
        setEnabled: async (id, v) => { const p = plugins.find((x) => x.id === id); p.enabled = v; return { ...p }; },
        setConfig: async (id) => ({ ...plugins.find((x) => x.id === id) }),
        openSite: async () => true,
        match: async (url) => plugins.filter((p) => p.supported && url.includes(new URL(p.source).hostname)),
        home: async () => [{ title: 'Mới cập nhật', input: '/new', script: 'gen.js' }, { title: 'Hot', input: '/hot', script: 'gen.js' }, { title: 'Hoàn thành', input: '/full', script: 'gen.js' }],
        genre: async () => ['Hành động', 'Hài hước', 'Học đường', 'Phiêu lưu', 'Tình cảm'].map((t) => ({ title: t, input: `/g/${t}`, script: 'gen.js' })),
        listItems: async (_id, _s, input, page) => { await delay(250); return { items: items(input.replace('/', '') || 'Truyện', page), next: (Number(page) || 1) < 3 ? String((Number(page) || 1) + 1) : '' }; },
        search: async (_id, q, page) => { await delay(250); return { items: items(q, page), next: '' }; },
        detail: async (_id, url) => { await delay(350); return { url, name: 'Truyện Mẫu Từ Nguồn', author: 'Tác Giả C', cover: svg(300, 400, 'Detail', 30), coverReferrerPolicy: '', description: 'Giới thiệu truyện mẫu.\nDòng thứ hai.', detail: 'Tình trạng: Đang ra\nLượt xem: 1.2M', host: '', ongoing: true, nsfw: false, genres: ['Hành động', 'Fantasy'], language: 'vi_VN', kind: 'comic' }; },
        toc: async () => { await delay(300); return Array.from({ length: 25 }, (_, i) => ({ name: `Chương ${i + 1}`, url: `https://comic.example/b/9/c${i + 1}`, locked: i === 24 })); },
      },
      books: {
        download: async (params) => {
          const job = { id: `book-${++seq}`, kind: 'book', pluginId: params.pluginId, url: params.url, label: params.label || '', title: params.label || 'Truyện Mẫu Từ Nguồn', status: 'queued', scope: params.scope, createdAt: Date.now(), progress: { phase: 'queued', chaptersTotal: 0, chaptersDone: 0, chaptersFailed: 0, chaptersSkipped: 2, pagesDone: 0, pagesTotal: 0 }, collectionId: 'book-1', error: '', log: [] };
          bookJobs.set(job.id, job);
          emit('book:update', { ...job });
          runJob(job);
          return { ...job };
        },
        jobs: async () => [...bookJobs.values()].map((j) => ({ ...j })),
        cancel: async (id) => { const j = bookJobs.get(id); if (j) { j.status = 'cancelled'; emit('book:update', { ...j }); } return true; },
        clear: async () => { for (const [k, j] of bookJobs) if (j.status !== 'running') bookJobs.delete(k); return []; },
      },
      shelf: {
        collections: async () => collections.map(summary),
        collection: async (id) => { const c = collections.find((x) => x.id === id); if (!c) throw new Error('collection not found'); return JSON.parse(JSON.stringify(c)); },
        update: async (id) => window.lynxx.books.download({ pluginId: collections.find((x) => x.id === id).pluginId, url: 'https://comic.example/b/1', scope: 'missing' }),
        fillRemote: async (id) => window.lynxx.books.download({ pluginId: '', url: 'https://remote.example/', scope: 'missing', label: collections.find((x) => x.id === id).title }),
        remove: async (id) => { if (!window.confirm('(mock) Xoá khỏi thư viện?')) return false; const i = collections.findIndex((x) => x.id === id); collections.splice(i, 1); return true; },
        importFile: async () => { await delay(400); collections.push({ ...collections[2], id: `local-${Date.now()}`, title: 'Tài liệu mới nhập' }); return [{ ok: true, file: 'tai-lieu.docx', collectionId: collections[collections.length - 1].id, chapterId: 'dc1' }, { ok: false, file: 'loi.mobi', error: 'Không đọc được file: mobi: DRM-protected book' }]; },
        export: async (p) => { await delay(600); const c = collections.find((x) => x.id === p.collectionId); const ext = p.format === 'kepub' ? 'kepub.epub' : p.format.replace(/\d/, ''); const path = `/home/user/Documents/${c.title}_${c.source}.${ext}`; c.exports.unshift({ id: `e${Date.now()}`, format: p.format, path, chapters: c.chapters.filter((x) => x.state === 'ready').length, size: 1, updatedAt: '', autoUpdate: Boolean(p.options.autoUpdate), options: p.options, exists: true }); return { id: c.exports[0].id, path, chapters: c.exports[0].chapters, queued: false }; },
        exportRefresh: async (cid, eid) => { await delay(300); const e = collections.find((x) => x.id === cid).exports.find((x) => x.id === eid); return { path: e.path, chapters: e.chapters }; },
        exportForget: async () => ({ ok: true }), revealExport: async () => true,
      },
      reader: {
        import: async () => ({ ok: false, error: 'mock' }),
        chapter: async (chapterId) => {
          await delay(150);
          const c = collections.find((x) => x.chapters.some((ch) => ch.id === chapterId));
          const ready = c.chapters.filter((x) => x.state === 'ready');
          const i = ready.findIndex((x) => x.id === chapterId);
          const ch = ready[i];
          const base = { chapterId, collectionId: c.id, title: ch.title, prevId: ready[i - 1]?.id || '', nextId: ready[i + 1]?.id || '', progress: progress[chapterId] || null, state: 'ready', upgradedFrom: '' };
          if (ch.kind === 'image') return { ...base, kind: 'images', pages: Array.from({ length: 6 }, (_, k) => ({ number: k + 1, url: svg(800, 1150, `${ch.title} · ${k + 1}`, (k * 50 + i * 20) % 360) })) };
          return { ...base, kind: 'text', html: Array.from({ length: 40 }, (_, k) => `<p>Đoạn ${k + 1} của ${ch.title}. Nội dung truyện chữ mẫu để kiểm tra trình đọc: cỡ chữ, cuộn và lưu vị trí đọc.</p>`).join('') };
        },
        progressGet: async (id) => progress[id] || null,
        progressSet: async (p) => { progress[p.chapterId] = p; const c = collections.find((x) => x.chapters.some((ch) => ch.id === p.chapterId)); if (c) c.lastChapterId = p.chapterId; return { ok: true }; },
      },
    };
  }
}());
