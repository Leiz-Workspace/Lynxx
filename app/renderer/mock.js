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
      if (method === 'app.info') return { name: 'Lynxx', version: '1.0 r0', python: '3.13' };
      if (method === 'formats.supported') return { extensions: ['epub', 'pdf', 'txt', 'mobi', 'azw3', 'fb2', 'cbz', 'docx', 'odt', 'rtf', 'html', 'md'] };
      throw new Error(`method not allowed: ${method}`);
    },
    sidecarStatus: async () => ({ ok: true, info: { name: 'Lynxx', version: '1.0 r0', python: '3.13' } }),
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
      info: async () => ({ version: '1.0.0', electron: 'mock', platform: 'browser', userData: '~/.config/Lynxx', python: '.venv/bin/python', core: 'python', repo: '.', sidecar: { name: 'Lynxx', version: '1.0 r0', python: '3.13' } }),
      relaunch: async () => location.reload(), installLauncher: async () => ({ ok: false, message: 'mock' }), openPath: async () => '', openExternal: async () => false,
    },
    thumbUrl: (url) => url, // the mock loads the address itself
    on: (ch, cb) => { (listeners[ch] = listeners[ch] || []).push(cb); return () => {}; },
    ...bookMock(),
  };
  setTimeout(() => emit('sidecar:status', { ok: true, info: { version: '1.0 r0' } }), 300);

  // ---------------------------------------------------------------- library / extensions / reader (renderer/books.js)
  function bookMock() {
    // book covers for screenshots: a gradient, a few shapes, the title in two or three lines (invented titles only)
    const esc = (t) => String(t).replace(/[&<>']/g, (c) => ({ '&': '&amp;', '<': '&lt;', '>': '&gt;', "'": '&apos;' })[c]);
    const lines = (title, max = 11) => title.split(' ').reduce((acc, w) => {
      const last = acc[acc.length - 1];
      if (last && (last + ' ' + w).length <= max) acc[acc.length - 1] = `${last} ${w}`; else acc.push(w);
      return acc;
    }, []);
    const coverArt = (title, author, hue, motif = 0) => {
      const ls = lines(title).slice(0, 4);
      const shapes = [
        `<circle cx='230' cy='120' r='70' fill='hsla(${hue + 40},90%,75%,.35)'/><path d='M0 330 L90 250 L160 300 L230 220 L300 290 L300 400 L0 400Z' fill='hsla(${hue},60%,8%,.55)'/>`,
        `<circle cx='70' cy='90' r='110' fill='hsla(${hue + 20},80%,70%,.18)'/><circle cx='250' cy='300' r='140' fill='hsla(${hue - 30},80%,60%,.18)'/>`,
        `<path d='M150 60 L190 160 L150 140 L110 160Z' fill='hsla(${hue + 50},90%,80%,.45)'/><rect x='0' y='300' width='300' height='100' fill='hsla(${hue},50%,6%,.5)'/>`,
        `<g stroke='hsla(${hue + 30},90%,80%,.35)' stroke-width='2' fill='none'><circle cx='150' cy='150' r='60'/><circle cx='150' cy='150' r='90'/><circle cx='150' cy='150' r='120'/></g>`,
      ][motif % 4];
      const text = ls.map((l, i) => `<text x='24' y='${236 + i * 30 - (ls.length - 1) * 15}' font-family='Noto Serif, Georgia, serif' font-weight='700' font-size='25' fill='#fff'>${esc(l)}</text>`).join('');
      return `data:image/svg+xml;utf8,${encodeURIComponent(`<svg xmlns='http://www.w3.org/2000/svg' width='300' height='400' viewBox='0 0 300 400'><defs><linearGradient id='g' x1='0' y1='0' x2='.4' y2='1'><stop offset='0' stop-color='hsl(${hue},55%,42%)'/><stop offset='1' stop-color='hsl(${hue + 35},60%,12%)'/></linearGradient></defs><rect width='300' height='400' fill='url(#g)'/>${shapes}${text}<text x='24' y='372' font-family='Noto Sans, sans-serif' font-size='14' fill='hsla(0,0%,100%,.75)'>${esc(author)}</text></svg>`)}`;
    };
    // a comic page: panels, figures and speech bubbles in grey tones
    const comicPage = (chapter, n) => {
      const h = 1150;
      const panels = n % 2 ? [[30, 30, 740, 420], [30, 470, 355, 650], [415, 470, 355, 650]] : [[30, 30, 355, 520], [415, 30, 355, 520], [30, 580, 740, 540]];
      const body = panels.map(([x, y, w, ph], i) => {
        const tone = 225 - ((n * 3 + i * 7) % 5) * 18;
        const cx = x + w * (0.35 + (i % 2) * 0.3);
        return `<rect x='${x}' y='${y}' width='${w}' height='${ph}' fill='rgb(${tone},${tone},${tone + 4})' stroke='#111' stroke-width='5'/>`
          + `<circle cx='${cx}' cy='${y + ph * 0.42}' r='${Math.min(w, ph) * 0.13}' fill='rgb(${tone - 70},${tone - 70},${tone - 60})'/>`
          + `<path d='M${cx - Math.min(w, ph) * 0.22} ${y + ph} Q${cx} ${y + ph * 0.5} ${cx + Math.min(w, ph) * 0.22} ${y + ph}Z' fill='rgb(${tone - 90},${tone - 90},${tone - 80})'/>`
          + `<ellipse cx='${x + w * 0.72}' cy='${y + 70}' rx='${Math.min(110, w * 0.25)}' ry='46' fill='#fff' stroke='#111' stroke-width='3'/>`
          + `<path d='M${x + w * 0.62} ${y + 108} l-22 34 l40 -26Z' fill='#fff' stroke='#111' stroke-width='3'/>`;
      }).join('');
      return `data:image/svg+xml;utf8,${encodeURIComponent(`<svg xmlns='http://www.w3.org/2000/svg' width='800' height='${h}' viewBox='0 0 800 ${h}'><rect width='800' height='${h}' fill='#fbfbf8'/>${body}<text x='770' y='${h - 8}' text-anchor='end' font-family='sans-serif' font-size='18' fill='#666'>${esc(chapter)} · ${n}</text></svg>`)}`;
    };
    const TITLES = [
      ['Học Viện Ma Pháp Kỳ Lạ', 'Tác giả A', 205], ['Thợ Săn Bóng Đêm', 'Hạ Vũ', 265], ['Mèo Đen Ở Tiệm Sách', 'Lam An', 28],
      ['Cơn Gió Phương Bắc', 'Bắc Phong', 180], ['Bầu Trời Thứ Bảy', 'Mộc Lan', 330], ['Kiếm Sĩ Tập Sự', 'Trần Minh', 12],
      ['Hành Tinh Băng', 'K. Lâm', 195], ['Quán Trà Cuối Phố', 'Tô Hà', 95], ['Người Giữ Ngọn Hải Đăng', 'Biển Xanh', 220],
      ['Rồng Giấy', 'Phong Linh', 0], ['Mùa Hè Ở Làng Chài', 'Thu Thảo', 45], ['Thành Phố Không Ngủ', 'Nam Sơn', 285],
    ];
    const NOVEL_TEXT = [
      'Gió đầu đông lùa qua dãy hành lang đá, mang theo mùi tuyết từ những đỉnh núi phía bắc. Lâm Phong dừng lại trước cánh cổng gỗ cũ, ngón tay vô thức siết chặt chuôi kiếm đã sứt mẻ.',
      'Ba năm trước, cũng ở chính nơi này, sư phụ đã trao cho cậu thanh kiếm ấy cùng một câu nói mà đến giờ cậu vẫn chưa hiểu hết: “Kiếm không nằm ở lưỡi, mà nằm ở người cầm nó.”',
      'Trong sân, mấy đệ tử mới đang tập những thế kiếm cơ bản. Tiếng hô đều đặn vang lên giữa không gian tĩnh lặng, xen lẫn tiếng lá khô xào xạc dưới chân.',
      '— Sư huynh về rồi! — một giọng nói non nớt reo lên. Tiểu Đào chạy vội ra, hai má đỏ ửng vì lạnh, trên tay vẫn còn cầm nửa chiếc bánh bao nóng hổi.',
      'Lâm Phong bật cười, xoa đầu cô bé. Nụ cười ấy khiến khuôn mặt vốn lạnh lùng của cậu bỗng trở nên ấm áp lạ thường, như ánh nắng hiếm hoi giữa mùa đông.',
      'Nhưng nụ cười chỉ kéo dài trong chốc lát. Phía xa, trên đỉnh tháp canh, ngọn lửa hiệu màu xanh vừa được thắp lên — dấu hiệu mà cả môn phái đã không thấy suốt hai mươi năm qua.',
      'Cậu ngẩng đầu nhìn lên. Những bông tuyết đầu tiên bắt đầu rơi, lặng lẽ phủ trắng mái ngói rêu phong. Một cảm giác bất an mơ hồ dâng lên trong lòng.',
      '“Thứ đến rồi cũng sẽ đến,” Lâm Phong thầm nghĩ, rồi bước nhanh về phía đại điện, nơi các trưởng lão hẳn đang chờ.',
    ];
    const chaptersOf = (prefix, n, ready, kind) => Array.from({ length: n }, (_, i) => ({ id: `${prefix}${i + 1}`, title: kind === 'text' ? `Chương ${i + 1}: ${['Tuyết đầu mùa', 'Ngọn lửa xanh', 'Lời hẹn cũ', 'Đường lên núi', 'Kẻ lạ mặt'][i % 5]}` : `Chương ${i + 1}`, state: i < ready ? 'ready' : 'pending', kind, pages: kind === 'image' && i < ready ? 8 : 0, url: '', locked: false, error: '', position: i }));
    const comicChapters = Array.from({ length: 12 }, (_, i) => ({ id: `cc${i + 1}`, title: `Chương ${i + 1}`, state: i < 8 ? 'ready' : (i === 8 ? 'failed' : 'pending'), kind: 'image', pages: i < 8 ? 6 : 0, url: `https://comic.example/b/1/c${i + 1}`, locked: i === 11, error: i === 8 ? 'trang 3: HTTP 403' : '', position: i }));
    const novelChapters = chaptersOf('nc', 30, 30, 'text');
    const collections = [
      { id: 'book-1', title: 'Học Viện Ma Pháp Kỳ Lạ', kind: 'comic', author: 'Tác Giả A', source: 'ComicSite', pluginId: 'comic-1', bookUrl: 'https://comic.example/b/1', coverUrl: coverArt('Học Viện Ma Pháp Kỳ Lạ', 'Tác giả A', 205, 0), chapters: comicChapters, lastChapterId: 'cc3', description: 'Một cậu bé bình thường được nhận vào học viện ma pháp.\nNhững ngày tháng kỳ lạ bắt đầu.', genres: ['Hài hước', 'Học đường', 'Phiêu lưu'], ongoing: true, exports: [] },
      { id: 'book-2', title: 'Kiếm Đạo Độc Tôn', kind: 'novel', author: 'Tác Giả B', source: 'NovelSite', pluginId: 'novel-1', bookUrl: 'https://novel.example/b/2', coverUrl: coverArt('Kiếm Đạo Độc Tôn', 'Tác giả B', 150, 2), chapters: novelChapters, lastChapterId: '', description: 'Truyện tiên hiệp.', genres: ['Tiên hiệp'], ongoing: false, exports: [{ id: 'e1', format: 'epub2', path: '/home/user/Documents/Kiếm Đạo Độc Tôn_NovelSite.epub', chapters: 30, size: 1200000, updatedAt: '', autoUpdate: true, options: {}, exists: true }] },
      { id: 'local-2', title: 'Truyện nhập từ EPUB vBook', kind: 'comic', author: '', source: 'EPUB', pluginId: '', bookUrl: '', coverUrl: coverArt('Truyện nhập từ EPUB vBook', 'EPUB', 300, 3), remoteChapters: 2, chapters: [
        { id: 'rc1', title: 'Chương 1', state: 'ready', kind: 'image', pages: 6, url: '', locked: false, error: '', position: 0 },
        { id: 'rc2', title: 'Chương 2', state: 'pending', kind: 'image', pages: 0, url: '', locked: false, error: '', position: 1 },
        { id: 'rc3', title: 'Chương 3', state: 'pending', kind: 'image', pages: 0, url: '', locked: false, error: '', position: 2 }], lastChapterId: '', description: 'Nhập từ file vBook có chương chỉ chứa link ảnh.', genres: [], ongoing: false, exports: [] },
      { id: 'local-1', title: 'Sổ tay ghi chú', kind: 'document', author: '', source: 'DOCX', pluginId: '', bookUrl: '', coverUrl: '', chapters: [{ id: 'dc1', title: 'Phần 1', state: 'ready', kind: 'text', pages: 0, url: '', locked: false, error: '', position: 0 }], lastChapterId: '', description: '', genres: [], ongoing: false, exports: [] },
    ];
    TITLES.slice(1, 9).forEach(([title, author, hue], i) => {
      const comic = i % 3 !== 2;
      const n = 20 + ((i * 13) % 60);
      const ready = i === 4 ? n - 3 : n;
      collections.splice(2 + (i % 2), 0, { id: `more-${i}`, title, kind: comic ? 'comic' : 'novel', author, source: comic ? 'ComicSite' : 'NovelSite', pluginId: comic ? 'comic-1' : 'novel-1', bookUrl: `https://comic.example/b/m${i}`, coverUrl: coverArt(title, author, hue, i + 1), chapters: chaptersOf(`m${i}c`, n, ready, comic ? 'image' : 'text'), lastChapterId: i % 2 ? `m${i}c${3 + i}` : '', description: 'Truyện mẫu cho ảnh chụp giao diện.', genres: ['Phiêu lưu'], ongoing: i % 2 === 0, exports: [] });
    });
    const summary = (c) => ({ id: c.id, title: c.title, kind: c.kind, author: c.author, source: c.source, pluginId: c.pluginId, bookUrl: c.bookUrl, coverUrl: c.coverUrl, chapters: c.chapters.length, ready: c.chapters.filter((x) => x.state === 'ready').length, failed: c.chapters.filter((x) => x.state === 'failed').length, lastChapterId: c.lastChapterId, lastChapterTitle: c.chapters.find((x) => x.id === c.lastChapterId)?.title || '' });
    const progress = {};
    const plugins = [
      { id: 'comic-1', name: 'ComicSite', author: 'mock', version: 3, source: 'https://comic.example', type: 'comic', nsfw: false, enabled: true, supported: true, description: 'Đọc truyện tranh trên ComicSite', capabilities: { content: true, home: true, genre: true, search: true, detail: true, toc: true, chap: true, page: false, downloadable: true, kind: 'comic' }, config: [{ key: 'DOMAIN', title: 'Domain', mode: 'input', value: 'https://comic.example', default: 'https://comic.example' }], missing: [], icon: '' },
      { id: 'novel-1', name: 'NovelSite', author: 'mock', version: 1, source: 'https://novel.example', type: 'novel', nsfw: false, enabled: true, supported: true, description: 'Truyện chữ', capabilities: { content: true, home: true, genre: false, search: true, detail: true, toc: true, chap: true, page: false, downloadable: true, kind: 'novel' }, config: [], missing: [], icon: '' },
      { id: 'tts-1', name: 'Some TTS', author: 'mock', version: 1, source: 'https://tts.example', type: 'tts', nsfw: false, enabled: true, supported: false, unsupportedReason: 'Loại "tts" chưa được hỗ trợ (chỉ novel / comic)', capabilities: { content: false, kind: 'novel' }, config: [], missing: [], icon: '' },
    ];
    const items = (_prefix, page) => TITLES.map(([title, author, hue], i) => ({ name: title, link: `https://comic.example/b/${(Number(page) || 1) * 100 + i}`, cover: coverArt(title, author, hue, i), description: `Chương ${20 + ((i * 17) % 90)}`, tag: i % 5 === 0 ? 'Hot' : '', coverReferrerPolicy: '' }));
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
        detail: async (_id, url) => { await delay(350); return { url, name: 'Thợ Săn Bóng Đêm', author: 'Hạ Vũ', cover: coverArt('Thợ Săn Bóng Đêm', 'Hạ Vũ', 265, 1), coverReferrerPolicy: '', description: 'Giới thiệu truyện mẫu.\nDòng thứ hai.', detail: 'Tình trạng: Đang ra\nLượt xem: 1.2M', host: '', ongoing: true, nsfw: false, genres: ['Hành động', 'Fantasy'], language: 'vi_VN', kind: 'comic' }; },
        toc: async () => { await delay(300); return Array.from({ length: 25 }, (_, i) => ({ name: `Chương ${i + 1}`, url: `https://comic.example/b/9/c${i + 1}`, locked: i === 24 })); },
      },
      books: {
        download: async (params) => {
          const job = { id: `book-${++seq}`, kind: 'book', pluginId: params.pluginId, url: params.url, label: params.label || '', title: params.label || 'Thợ Săn Bóng Đêm', status: 'queued', scope: params.scope, createdAt: Date.now(), progress: { phase: 'queued', chaptersTotal: 0, chaptersDone: 0, chaptersFailed: 0, chaptersSkipped: 2, pagesDone: 0, pagesTotal: 0 }, collectionId: 'book-1', error: '', log: [] };
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
          if (ch.kind === 'image') return { ...base, kind: 'images', pages: Array.from({ length: 6 }, (_, k) => ({ number: k + 1, url: comicPage(ch.title, k + 1) })) };
          return { ...base, kind: 'text', html: Array.from({ length: 24 }, (_, k) => `<p>${NOVEL_TEXT[k % NOVEL_TEXT.length]}</p>`).join('') };
        },
        progressGet: async (id) => progress[id] || null,
        progressSet: async (p) => { progress[p.chapterId] = p; const c = collections.find((x) => x.chapters.some((ch) => ch.id === p.chapterId)); if (c) c.lastChapterId = p.chapterId; return { ok: true }; },
      },
    };
  }
}());
