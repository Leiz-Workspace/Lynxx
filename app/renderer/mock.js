// Browser-only mock of the preload bridge: lets the UI run in a plain browser (design work, screenshots, UI tests)
// with neutral placeholder thumbnails. app.js loads this file only when window.lynxx is missing and the page is not
// running inside Electron (there the preload bridge always exists), so it never ships behaviour into the real app.
(function () {
  if (window.lynxx || /\bElectron\//.test(navigator.userAgent)) return;
  const listeners = {};
  const emit = (ch, data) => (listeners[ch] || []).forEach((cb) => cb(data));
  const delay = (ms) => new Promise((r) => setTimeout(r, ms));
  // UI tests can slow down single calls to provoke out-of-order answers: window.__mockLatency = (method, params) => ms
  const latency = (method, p) => { try { return Number(window.__mockLatency?.(method, p)) || 0; } catch { return 0; } };
  const ICONS = [
    { id: 'full-mask-cyan', name: 'Full mask · Cyan' }, { id: 'exposed-jaw-cyan', name: 'Exposed jaw · Cyan' },
    { id: 'full-mask-monochrome-1024', name: 'Full mask · Mono' }, { id: 'exposed-jaw-monochrome-1024', name: 'Exposed jaw · Mono' },
  ];
  const mockTagRuns = new Map(); // module -> { count, message } of a running tag list update (tags.update below)
  const mockSchedule = { ok: true, kind: 'crontab', enabled: false, installed: false, frequency: 'weekly', time: '04:31',
    modules: [{ module: 'db', name: 'Danbooru' }, { module: 'fb', name: 'Fakebooru' }], log: '~/.config/Lynxx/logs/tags-schedule.log', lastRun: 0 };
  const settings = {
    appearance: { theme: 'ember', icon: 'full-mask-cyan', background: { mode: 'icon', image: '', blur: 4, opacity: 0.9, dim: 0.18 } },
    search: { module: 'db', autocomplete: true, safePreview: false, history: [], saved: [], tagLimits: {}, filtersOpen: true },
    download: { path: '/home/user/Downloads/Lynxx', threads: 4, images: true, videos: true, videoFormat: '', lowRes: false, maxVideoHeight: 0, prefix: true,
      gender: { select: [], conf: 'medium', strict: false, op: 'keep', mode: 'tags', verifyMedia: false },
      safetyBlacklist: false,
      appendInfo: false, reverse: false, limit: 0, parchi: false, preserveDate: false, skipKnown: false, subfolderPerQuery: false, dumpTags: false,
      dumpSources: false, dumpComments: false, dumpHashes: false, infoMode: 'run', warnNonEmpty: false, maxConcurrentJobs: 1 },
    connection: { proxy: '', ignoreProxy: false, proxyNoDownload: false, timeout: 0, retries: 0, headers: {}, cookies: {},
      siteHeaders: {}, siteCookies: {}, keyRateLimits: {} },
    tags: { autoUpdate: false, autoUpdateDays: 7, schedule: { enabled: false, frequency: 'weekly' } },
    advanced: { pythonPath: '', verbose: false, catalogUrl: 'https://github.com/Leiz-Workspace/Lynxx/releases/download/catalog/index.json' },
    ai: { model: 'claude-opus-5-5', maxUsd: 2 },
    genderModel: { url: 'http://127.0.0.1:8765', enabled: false },
    library: { trashDays: 30 },
  };
  const secrets = {}; // abbr -> true (the real app never sends key values to the renderer either)
  let aiKey = false;
  const aiStatus = () => ({ set: aiKey, encryption: true });
  let gmToken = false;
  let gmLoaded = false;
  const secretsStatus = () => ({ encryption: true, modules: Object.fromEntries(Object.keys(secrets).map((k) => [k, { set: true, encrypted: true }])), ai: aiStatus(), genderModel: { set: gmToken, encrypted: gmToken, encryption: true } });
  // extension generator: a scripted session (template path) producing an in-memory draft
  const drafts = [];
  const gen = { running: false, session: '', events: [] };
  const genEmit = (ev) => { gen.events.push(ev); emit('gen:event', ev); };
  const genRun = async (opts) => {
    const host = String(opts.url).replace(/^https?:\/\//, '').split('/')[0] || 'example.com';
    const id = host.split('.').filter((x) => x !== 'www')[0].replace(/[^a-z0-9]/g, '') || 'site';
    const session = `20261117-1200${String(drafts.length).padStart(2, '0')}-abc${String(drafts.length).padStart(3, '0')}`;
    Object.assign(gen, { running: true, session, events: [] });
    const ai = opts.mode === 'ai';
    genEmit({ type: 'start', session, url: opts.url, mode: opts.mode, model: ai ? settings.ai.model : '' });
    const steps = [['survey_site', 'Đọc robots.txt và nhận diện engine của site', 'engine gelbooru_dapi (high), robots.txt: found'],
      ['create_draft', 'Tạo thư mục bản nháp', `${id}: downloader.py, manifest.json, specs.md`], ['static_check', 'Kiểm tra tĩnh downloader.py', 'đạt'],
      ['test_run', 'Chạy thử trong sandbox', 'đạt'], ['finish', 'Kết thúc phiên', 'session finished']];
    let usd = 0;
    for (const [tool, label, result] of steps) {
      await delay(500);
      if (!gen.running) break;
      if (ai) genEmit({ type: 'log', source: 'model', text: `Gọi ${tool}.` });
      genEmit({ type: 'phase', tool, label, state: 'running' });
      genEmit({ type: 'log', tool, text: `${label}: ${result}` });
      if (ai) { usd += 0.04; genEmit({ type: 'usage', usd, maxUsd: opts.maxUsd || settings.ai.maxUsd, turns: steps.indexOf(steps.find((x) => x[0] === tool)) + 1 }); }
    }
    const status = gen.running ? 'ready' : 'cancelled';
    const report = { session, url: opts.url, site: `https://${host}/`, mode: opts.mode, model: ai ? settings.ai.model : '', status,
      summary: status === 'ready' ? `Site chạy engine gelbooru_dapi: bản nháp dùng template có sẵn. Chạy thử với tag "scenery": 46 bài, lấy được 3 link tệp.` : 'Đã huỷ.',
      ruleIds: [], extId: id, abbr: id.slice(0, 2), name: id[0].toUpperCase() + id.slice(1), version: '1.0.0', engine: 'gelbooru_dapi',
      draftDir: `/mock/extgen/drafts/${session}/${id}`, files: ['downloader.py', 'manifest.json', 'specs.md', 'tests/expected.json', 'tests/fixtures/001.xml', 'tests/fixtures/index.json'],
      checks: [{ id: 'manifest', ok: true, severity: 'error', detail: `${id} 1.0.0` }, { id: 'count', ok: true, severity: 'error', detail: "46 post(s) for 'scenery'" },
        { id: 'download', ok: true, severity: 'error', detail: '3 / 3 file link(s)' }, { id: 'preview', ok: true, severity: 'warning', detail: 'preview urls ok' }],
      testRuns: 1, requests: 5, usage: { input: 42000, output: 3100, cacheRead: 30000, cacheWrite: 9000, requests: 5 }, usd, turns: 5,
      startedAt: new Date().toISOString(), finishedAt: new Date().toISOString() };
    drafts.unshift(report);
    genEmit({ type: 'done', session, status, summary: report.summary, ruleIds: [], draftDir: report.draftDir, extId: id, usd, report });
    gen.running = false;
    emit('gen:event', { type: 'exit', session });
  };
  const iconUrl = (id) => `../assets/icons/${id}.png`;
  const appearance = () => ({
    ...settings.appearance, iconUrl: iconUrl(settings.appearance.icon),
    backgroundUrl: settings.appearance.background.mode === 'image' && settings.appearance.background.image ? settings.appearance.background.image : iconUrl(settings.appearance.icon),
    icons: ICONS.map((i) => ({ ...i, url: iconUrl(i.id) })), themes: ['ember', 'graphite', 'paper', 'oled'], liveTaskbarIcon: false,
  });
  // a kind of manifest 'search' as ext.list gives it (lynxx/modspec.py SearchKind)
  const kind = (k) => ({ token: '', value: '', input: 'tag', tagType: '', listing: false, pageSize: 0, example: '', label: '', use: '', order: 'id', ...k });
  const modules = [
    { id: 'danbooru', name: 'Danbooru', abbr: 'db', version: '1.0.0', source: 'builtin', default: true, installed: true, media: ['image', 'video'], capabilities: ['gender_tags', 'parent_child', 'preview'],
      site: 'https://danbooru.donmai.us/', description: 'Danbooru JSON API. Anonymous search is limited to 2 tags: extra tags are checked locally.', auth: { apiKey: true, apiKeyFormat: 'API_KEY,LOGIN' },
      syntax: { maxTags: 2 }, search: { idSearch: true, metaNames: ['favgroup', 'ordfavgroup'], kinds: [
        kind({ id: 'uploader', token: 'user:', value: '[^\\s:]+', input: 'text' }), kind({ id: 'favorites', token: 'favorited_by:', value: '[^:]+?', input: 'text', use: 'fav' }),
        kind({ id: 'pool', token: 'pool:', value: '[^:]+?', input: 'text', use: 'pool', example: '12345' })] } },
    { id: 'rule34', name: 'Rule34', abbr: 'rx', version: '2.0.0', source: 'builtin', default: true, installed: true, media: ['image', 'video'], capabilities: ['api_key', 'api_key_required', 'gender_tags', 'parent_child', 'preview'],
      site: 'https://rule34.xxx/', description: 'Gelbooru-based DAPI (XML). API key + user id recommended.', auth: { apiKey: true, apiKeyFormat: 'KEY,USER_ID', rateLimit: { requests: 60, seconds: 60 } }, syntax: { maxTags: 0 },
      search: { idSearch: true, metaNames: [], kinds: [kind({ id: 'uploader', token: 'user:', value: '[^\\s:]+', input: 'text' }),
        kind({ id: 'favorites', token: 'favorited_by:', value: '\\d+?', input: 'id', use: 'fav', example: '12345' }),
        kind({ id: 'pool', token: 'pool:', value: '\\d+?', input: 'id', use: 'pool', example: '12345' })] } },
    { id: 'e621', name: 'e621', abbr: 'en', version: '1.0.0', source: 'catalog', default: false, installed: false, media: ['image', 'video'], site: 'https://e621.net/', description: 'e621 JSON API (2 requests/second).', auth: {}, syntax: { maxTags: 40 }, search: { idSearch: true, metaNames: [], kinds: [] } },
    // its own update reads only the tags with 100+ posts and the core merges them into its list (tagUpdate.merge, tags.sources merge)
    { id: 'paheal', name: 'Rule34 Paheal', abbr: 'rp', version: '1.0.3', source: 'user', default: false, installed: true, media: ['image', 'video'], site: 'https://rule34.paheal.net/',
      description: 'Shimmie danbooru-compatible API. Max 3 tags.', auth: {}, syntax: { maxTags: 3 }, tagList: 'rp_tags.json',
      tagUpdate: { entry: 'tags.py:read_tags', merge: { minPosts: 100 } }, minSdkVersion: '1.1' },
    { id: 'xbooru', name: 'Xbooru', abbr: 'xb', version: '1.0.0', source: 'catalog', default: false, installed: false, media: ['image', 'video'], site: 'https://xbooru.com/', description: 'Gelbooru-based DAPI (XML).', auth: {}, syntax: { maxTags: 0 } },
    // made by the extension generator (lynxx.extgen): its tag list comes from its engine (tags.sources)
    { id: 'fakebooru', name: 'Fakebooru', abbr: 'fb', version: '1.0.0', source: 'user', default: false, installed: true, media: ['image', 'video'], site: 'https://fakebooru.example/',
      description: 'Fakebooru: Gelbooru 0.2 DAPI (XML)', auth: {}, syntax: { maxTags: 0 }, tagList: 'fb_tags.json',
      generator: { tool: 'lynxx.extgen', engine: 'gelbooru_dapi', createdAt: '2026-11-18T08:00:00Z' } },
    // ships its tag list and its own update (manifest tagUpdate): run only from the Tags A-Z button (tags.sources onDemand)
    { id: 'tbib', name: 'TBIB', abbr: 'tb', version: '1.0.0', source: 'user', default: false, installed: true, media: ['image', 'video'], site: 'https://tbib.org/',
      description: 'TBIB: Gelbooru DAPI', auth: {}, syntax: { maxTags: 0 }, tagList: 'tb_tags.json', tagUpdate: { entry: 'tags.py:read_tags' }, minSdkVersion: '1.1' },
    // a site without a tag listing: its list is learned from the posts previewed and downloaded (tags.learn, learned: true)
    { id: 'htmlsite', name: 'Htmlsite', abbr: 'hs', version: '1.0.0', source: 'user', default: false, installed: true, media: ['image'], site: 'https://htmlsite.example/',
      description: 'Htmlsite: custom HTML', auth: {}, syntax: { maxTags: 0 }, generator: { tool: 'lynxx.aigen', engine: 'custom', createdAt: '2026-11-18T09:00:00Z' } },
    // ships its list but has no tag source (tags.status bundled): learned tags go on top of it
    // site listings as search kinds (manifest 'search', Lynxx 2.0.11): artist / category / keyword pages of the site, no id search.
    // Typed with their token ('artist:sample_artist') or added from Tags A-Z: the app has no kind picker
    // its videos come in several heights: "Chất lượng video" is shown (capability video_quality_choice, -max_vres); "Tải đã chọn"
    // reads the Preview's page again (no id search: pick mode)
    { id: 'videosite', name: 'Videosite', abbr: 'vb', version: '1.1.0', source: 'user', default: false, installed: true, media: ['image', 'video'], site: 'https://videosite.example/',
      capabilities: ['preview', 'video_quality_choice'],
      description: 'Videosite: custom HTML', auth: {}, syntax: { maxTags: 1 }, tagList: 'vb_tags.json', minSdkVersion: '1.2',
      search: { idSearch: false, metaNames: [], kinds: [
        kind({ id: 'artist', token: 'artist:', value: '[a-z0-9_-]+', tagType: 'artist', listing: true, pageSize: 24, example: 'sample_artist' }),
        kind({ id: 'category', token: 'category:', value: '[a-z0-9_-]+', tagType: 'copyright', listing: true, pageSize: 24 }),
        kind({ id: 'keyword', token: 'search:', value: '[^\\s/]+', input: 'text', listing: true, pageSize: 24, example: 'night_city' }),
        kind({ id: 'uploader', token: 'user:', value: '\\d+', input: 'id', listing: true, example: '294' })] } },
    // learn-only, nothing learned yet (no row in tags.status)
    { id: 'clipsite', name: 'Clipsite', abbr: 'cv', version: '1.0.0', source: 'user', default: false, installed: true, media: ['video'], site: 'https://clipsite.example/',
      description: 'Clipsite: custom HTML, robots.txt forbids its tag API', auth: {}, syntax: { maxTags: 0 } },
  ];
  const PAGE_MAX = { db: 200, rx: 1000 };
  const TAGS = [
    ['scenery', 412000, 'general'], ['sky', 380000, 'general'], ['cloud', 300000, 'general'], ['cat_ears', 143800, 'general'], ['cat', 98000, 'general'],
    ['castle', 22000, 'general'], ['cherry_blossoms', 61000, 'general'], ['city', 52000, 'general'], ['coffee', 9000, 'general'],
    ['smile', 2500000, 'general'], ['solo', 5200000, 'general'], ['sunset', 41000, 'general'], ['star_(sky)', 33000, 'general'],
    ['sun', 30000, 'general'], ['sunlight', 260000, 'general'], ['sunflower', 21000, 'general'], ['sundress', 18000, 'general'],
    ['original', 1600000, 'copyright'], ['touhou', 900000, 'copyright'], ['hatsune_miku', 120000, 'character'], ['highres', 8400000, 'metadata'],
    ['absurdres', 2100000, 'metadata'], ['blue_sky', 120000, 'general'], ['bird', 70000, 'general'], ['book', 150000, 'general'],
    ['building', 90000, 'general'], ['ocean', 110000, 'general'], ['outdoors', 1300000, 'general'], ['mountain', 42000, 'general'],
    ['big_hair', 30000, 'general'], ['big_hat', 9000, 'general'], ['abstract', 12000, 'general'], ['animal_ears', 900000, 'general'],
    // made-up artists, a character and a series: the typed rows the A-Z type chips filter, and what 'artist:sa' suggests on vb
    ['sample_artist', 4200, 'artist'], ['sakura_painter', 2600, 'artist'], ['sky_sketcher', 900, 'artist'], ['cloud_brush', 310, 'artist'],
    ['sample_heroine', 15000, 'character'], ['sample_saga', 52000, 'copyright'],
    // rows of a site listing (an extension's own tag job may list them with their token; an artist page gives no post count)
    ['category:city_nights', 812, 'copyright'], ['category:cloud_stories', 240, 'copyright'], ['artist:night_animator', 1, 'artist'],
  ];
  // the tag list in use per module, as tags.browse / tags.types read it: db, rx and vb have typed lists, fb's engine
  // gives general tags only, the lists of the others carry no type (a site that gives none, a learned list), cv has none yet
  const UNTYPED = TAGS.map(([tag, posts]) => [tag, posts, 'unknown']);
  const listRows = (module) => {
    if (['db', 'rx', 'vb'].includes(module)) return TAGS;
    if (module === 'cv') return [];
    if (module === 'fb') return TAGS.filter((t) => t[2] === 'general');
    return UNTYPED;
  };
  const ALIASES = { kitty_ears: 'cat_ears', clouds: 'cloud', sunset_sky: 'sunset', blue_skies: 'blue_sky', cloudy_sky: 'cloud' };
  const META = /^(?:id|score|rating|user|md5|width|height|parent|source|order|date|status|fav|pool|filetype|limit|age|ordfav)[:=<>]/;
  const tagRow = (name) => TAGS.find((t) => t[0] === name);
  const lev = (a, b) => {
    const d = Array.from({ length: a.length + 1 }, (_, i) => [i]);
    for (let j = 1; j <= b.length; j++) d[0][j] = j;
    for (let i = 1; i <= a.length; i++) {
      for (let j = 1; j <= b.length; j++) d[i][j] = Math.min(d[i - 1][j] + 1, d[i][j - 1] + 1, d[i - 1][j - 1] + (a[i - 1] === b[j - 1] ? 0 : 1));
    }
    return d[a.length][b.length];
  };
  const palette = ['#ff6a1a', '#6fa8ff', '#4fd18b', '#b892ff', '#ffb547', '#ff5468', '#46c3c9'];
  const thumb = (id) => {
    const c1 = palette[id % palette.length]; const c2 = palette[(id * 3 + 1) % palette.length];
    const svg = `<svg xmlns="http://www.w3.org/2000/svg" width="300" height="300"><defs><linearGradient id="g" x1="0" y1="0" x2="1" y2="1"><stop offset="0" stop-color="${c1}"/><stop offset="1" stop-color="${c2}"/></linearGradient></defs><rect width="300" height="300" fill="url(#g)"/><circle cx="${80 + (id % 140)}" cy="${90 + (id % 90)}" r="${30 + (id % 40)}" fill="rgba(255,255,255,.35)"/><text x="16" y="282" font-family="monospace" font-size="20" fill="rgba(0,0,0,.55)">#${id}</text></svg>`;
    return `data:image/svg+xml;utf8,${encodeURIComponent(svg)}`;
  };
  // OR groups become separate tasks (like the core does for sites without native OR support)
  const splitTasks = (tags) => {
    const parts = tags.split(' ').filter(Boolean);
    const gi = parts.findIndex((t) => /^\(.+~.+\)$/.test(t));
    if (gi < 0) return [tags];
    return parts[gi].slice(1, -1).split('~').map((alt) => [...parts.slice(0, gi), alt, ...parts.slice(gi + 1)].join(' '));
  };
  const jobs = new Map();
  const jobQueries = new Map(); // job id -> its query, for "run again" (a pick of the Preview's page included, like main)
  // download jobs: a fake progress run; a multi-site group starts its next site when the previous one ended (like main)
  const runJob = (job, onEnd) => {
    job.status = 'running';
    job.startedAt = Date.now();
    job.progress = { stateName: 'SEARCHING' };
    emit('jobs:update', { ...job });
    let n = 0;
    const t = setInterval(() => {
      if (job.status !== 'running') { clearInterval(t); onEnd?.(); return; }
      n += 4;
      const known = job.group?.index ? Math.min(30, n) : 0; // later sites skip files an earlier site already brought (md5)
      job.progress = n < 12 ? { stateName: 'SCANNING_PAGES1', total: 96 } : { stateName: 'DOWNLOADING', totalAll: 96, processed: Math.min(96, n), success: Math.min(96, n) - known, known, bytes: n * 1.7e6 };
      // -gender_verify_media (model on, 'Tag + model', box ticked): about 1 file in 8 is a video checked again, 1 in 32 moved aside
      if (job.verify && n >= 12) Object.assign(job.progress, { genderVerified: Math.floor(Math.min(96, n) / 8), genderMismatch: Math.floor(Math.min(96, n) / 32) });
      if (n >= 96) { job.status = 'done'; job.progress.stateName = 'DONE'; clearInterval(t); emit('jobs:update', { ...job }); onEnd?.(); return; }
      emit('jobs:update', { ...job });
    }, 300);
  };
  const newJob = (q, extra = {}) => {
    const id = `job-${Date.now()}-${jobs.size}`;
    const verify = settings.genderModel.enabled && q.gender?.mode === 'model' && q.gender?.verifyMedia === true && (q.gender.select || []).length > 0;
    const job = { id, module: q.module, tags: q.tags, label: q.label || '', dest: q.path, status: 'queued', createdAt: Date.now(), startedAt: 0, progress: {}, logTail: ['mock job started'], group: null, verify, ...extra };
    jobs.set(id, job);
    return job;
  };
  const startNextInGroup = (gid) => {
    const next = [...jobs.values()].filter((j) => j.group?.id === gid && j.status === 'queued').sort((a, b) => a.group.index - b.group.index)[0];
    if (next) runJob(next, () => startNextInGroup(gid));
  };
  // library import job: scan, then metadata requests; window.__mockImportNames = false: nothing unknown / missing
  const runImport = (job, opts) => {
    job.status = 'running';
    job.startedAt = Date.now();
    const names = window.__mockImportNames !== false;
    const unknownNames = names ? ['IMG_0001.jpg: not recognized', 'wallpaper.png: not recognized', '12345.jpg: no module prefix in the name (id 12345): choose the module of such names'] : [];
    const notFoundItems = names ? ['sub/db_4410.jpg (db id 4410)', 'rx_99.png (rx id 99)'] : [];
    let n = 0;
    const t = setInterval(() => {
      if (job.status !== 'running') { clearInterval(t); return; }
      n += 10;
      const base = { scanned: Math.min(130, n * 2), known: 12, unknown: unknownNames.length, module: n < 80 ? 'db' : 'rx', dryRun: opts.dryRun };
      if (n < 60 || opts.dryRun) job.progress = { ...base, stateName: 'IMPORT_SCAN', totalAll: 0 };
      else job.progress = { ...base, stateName: 'IMPORT_FETCH', totalAll: 115, processed: Math.min(115, n - 60), success: Math.max(0, Math.min(115, n - 60) - notFoundItems.length), notFound: n - 60 >= 115 ? notFoundItems.length : 0, failed: 0, requests: Math.ceil((n - 60) / 10) };
      if ((opts.dryRun && n >= 60) || n - 60 >= 115) {
        job.status = 'done';
        job.exitCode = 0;
        job.progress = { ...job.progress, stateName: 'DONE', scanned: 130, totalAll: 115, unknownNames, notFoundItems, failedItems: [], mismatchItems: [],
          modules: { db: { files: 80 }, rx: { files: 35 } }, ok: true, exitCode: 0 };
        clearInterval(t);
      }
      emit('jobs:update', { ...job });
    }, 250);
  };
  const clean = { running: false, stop: false };
  // files of the "Đã tải" page; trashedAt / origPath: in the trash (two of them already, one deleted 26 days ago)
  const libExts = ['jpg', 'png', 'mp4', 'webm', 'mp3', 'jpg', 'gif', 'png'];
  const libFiles = Array.from({ length: 25 }, (_, i) => {
    const name = `${i % 3 ? 'db' : 'rx'}_${9000 - i}.${libExts[i % 8]}`;
    const trashedAt = i >= 23 ? Date.now() / 1000 - (i === 23 ? 3600 : 86400 * 26) : null;
    return { module: i % 3 ? 'db' : 'rx', id: String(9000 - i), md5: `${i}`.padStart(32, 'a'), name, ext: libExts[i % 8],
      path: trashedAt ? `/home/user/.config/Lynxx/trash/${i + 1}_${name}` : `/home/user/Downloads/Lynxx/${name}`,
      size: 350000 + i * 91000, tags: 'scenery sky cloud original', score: 10 + i, rating: 'g', date: '2026-11-10',
      downloadedAt: Date.now() / 1000 - i * 3600, exists: i % 7 !== 6, trashedAt, origPath: trashedAt ? `/home/user/Downloads/Lynxx/${name}` : '' };
  });
  const purgeMock = (pick) => {
    const gone = libFiles.filter((f) => f.trashedAt && pick(f));
    gone.forEach((f) => libFiles.splice(libFiles.indexOf(f), 1));
    return { purged: gone.length, files: gone.filter((f) => f.exists).length, bytes: gone.reduce((n, f) => n + (f.exists ? f.size : 0), 0), failed: [], kept: 0 };
  };
  window.lynxx = {
    __mock: true,
    sidecarStatus: async () => ({ ok: true, info: { version: '2.0 r1 (mock)' } }),
    rpc: async (method, p) => {
      await delay(60 + latency(method, p));
      if (method === 'app.info') return { name: 'Lynxx', version: '2.0 r1', python: '3.13' };
      if (method === 'ext.list') return modules;
      if (method === 'ext.install') { const m = modules.find((x) => x.id === p.id); if (m) m.installed = true; return m; }
      if (method === 'ext.uninstall') { const m = modules.find((x) => x.id === p.id); if (m) m.installed = false; return { ok: true }; }
      if (method === 'ext.remoteCatalog') return [];
      if (method === 'tags.autocomplete') {
        // types: only tags of those types; token: rows named '<token><prefix>' first, else plain rows, 'insert' carries the token
        const neg = p.prefix.startsWith('-'); const token = p.token || '';
        let base = p.prefix.replace(/^-/, '').toLowerCase();
        if (token && base.startsWith(token)) base = base.slice(token.length);
        const match = (pre) => TAGS.filter((t) => t[0].startsWith(pre) && (!p.types?.length || p.types.includes(t[2])))
          .sort((a, b) => b[1] - a[1]).slice(0, p.limit || 12);
        const rows = (token && match(`${token}${base}`).length) ? match(`${token}${base}`) : match(base);
        return rows.map(([tag, posts, type]) => ({ tag, posts, type, insert: `${neg ? '-' : ''}${token && !tag.startsWith(token) ? token : ''}${tag}` }));
      }
      if (method === 'tags.types') {
        // every type of the module's list in use (listRows, the rows tags.browse filters), '<token><value>' rows included
        const types = {};
        for (const [, , t] of listRows(p.module)) types[t] = (types[t] || 0) + 1;
        return { module: p.module, hasList: listRows(p.module).length > 0, types };
      }
      if (method === 'files.identify') {
        const items = (p.names || []).map((raw) => {
          const name = String(raw).trim();
          const base = name.split(/[\\/]/).pop().toLowerCase().replace(/\.[a-z0-9]+$/, '');
          let m;
          if ((m = /^([a-z]{2})_(\d+)/.exec(base))) return { name, module: m[1], kind: 'id', value: m[2], note: '' };
          if ((m = /^(\d+)/.exec(base))) return { name, module: p.module, kind: 'id', value: m[1], note: '' };
          if ((m = /[0-9a-f]{32}/.exec(base))) return { name, module: p.module, kind: 'md5', value: m[0], note: '' };
          return { name, module: p.module, kind: '', value: '', note: 'not recognized' };
        });
        const by = {};
        items.filter((it) => it.kind).forEach((it) => { (by[it.module] ||= []).push(`${it.kind}:${it.value}`); });
        const queries = Object.entries(by).map(([module, terms]) => ({ module, tags: terms.length > 1 ? `(${terms.join('~')})` : terms[0], count: terms.length, dropped: 0,
          idSearch: modules.find((m) => m.abbr === module)?.search?.idSearch !== false }));
        return { items, queries, max: 500 };
      }
      if (method === 'tags.fuzzy') {
        const text = String(p.text || '').toLowerCase();
        const flat = text.replace(/_/g, '');
        return TAGS.filter((t) => !p.types?.length || p.types.includes(t[2])).map(([tag, posts, type]) => ({ tag, posts, type, distance: Math.min(lev(text, tag), tag.replace(/_/g, '') === flat ? 1 : 9) }))
          .filter((r) => r.distance <= 2).sort((a, b) => a.distance - b.distance || b.posts - a.posts).slice(0, p.limit || 8);
      }
      if (method === 'tags.resolve') {
        return { items: (p.tags || []).slice(0, 64).map((raw) => {
          const tag = String(raw).trim().toLowerCase();
          if (META.test(tag)) return { tag, canonical: tag, alias: false, posts: 0, type: 'meta', known: true };
          if (/[*?]/.test(tag)) {
            const re = new RegExp(`^${tag.replace(/[.+^${}()|[\]\\]/g, '\\$&').replace(/\*/g, '.*').replace(/\?/g, '.')}$`);
            return { tag, canonical: tag, alias: false, posts: 0, type: 'wildcard', known: TAGS.some((t) => re.test(t[0])) };
          }
          const canonical = ALIASES[tag] || tag;
          const row = tagRow(canonical);
          return { tag, canonical, alias: canonical !== tag, posts: row ? row[1] : 0, type: row ? row[2] : 'unknown', known: !!row };
        }), hasList: true };
      }
      if (method === 'tags.browse') {
        let rows = listRows(p.module).filter((t) => (p.prefix === '#' ? !/^[a-z]/.test(t[0]) : t[0].startsWith(p.prefix)) && t[1] >= (p.minPosts || 0)
          && (!p.types?.length || p.types.includes(t[2])));
        rows = p.sort === 'posts' ? rows.sort((a, b) => b[1] - a[1]) : rows.sort((a, b) => a[0].localeCompare(b[0]));
        return { total: rows.length, items: rows.slice(p.offset, p.offset + p.limit).map(([tag, posts, type]) => ({ tag, posts, type })) };
      }
      if (method === 'tags.validate') {
        // the listing rules of search kinds (lynxx/tags_parser.py): one listing, not negated, no OR group with it
        const listing = modules.find((m) => m.abbr === p.module)?.search?.kinds?.filter((k) => k.listing) || [];
        const words = p.tags.split(' ');
        const isListing = (w) => listing.some((k) => w.replace(/^%?-?/, '').startsWith(k.token));
        const found = words.filter(isListing);
        const inGroup = words.filter((w) => /^\(.+~.+\)$/.test(w)).flatMap((w) => w.slice(1, -1).split('~')).find(isListing);
        let error = '';
        if (inGroup) error = `${inGroup.replace(/^%?-?/, '').replace(/%$/, '')} is a site listing: 'or' groups cannot be used with it`;
        else if (found.some((w) => /^%?-/.test(w))) error = `'${found[0].replace(/^%?-/, '').split(':')[0]}:' selects a site listing: it cannot be negated`;
        else if (found.length > 1) error = `one site listing per search: ${found.join(' ')}`;
        else if (found.length && words.some((w) => /^\(.+~.+\)$/.test(w))) error = `${found[0]} is a site listing: 'or' groups cannot be used with it`;
        const valid = !error && !/[\s]{2,}|\(\)/.test(p.tags);
        return { valid, tags: words, error };
      }
      if (method === 'tags.sources') {
        return [
          { module: 'rx', name: 'Rule34', engine: 'gelbooru_dapi', site: 'https://api.rule34.xxx/', key: 'required', keyFormat: 'KEY,USER_ID', robots: false, curated: true, tailUpdates: true, description: 'DAPI tag index, pages by number (pid)' },
          { module: 'db', name: 'Danbooru', engine: 'danbooru_json', site: 'https://danbooru.donmai.us/', key: 'optional', keyFormat: 'API_KEY,LOGIN', robots: false, curated: true, description: 'tags.json, id cursor' },
          { module: 'fb', name: 'Fakebooru', engine: 'gelbooru_dapi', site: 'https://fakebooru.example/', key: 'optional', keyFormat: 'API_KEY,USER_ID', robots: true, curated: false, tailUpdates: false, description: 'DAPI tag index, pages by number (pid)' },
          { module: 'tb', name: 'TBIB', engine: 'extension_job', site: 'https://tbib.org/', key: 'none', keyFormat: '', robots: true, curated: false, tailUpdates: false, onDemand: true, merge: false, mergeMinPosts: 0, job: 'tags.py:read_tags', description: "the extension's own update (tags.py:read_tags), run only when asked" },
          { module: 'rp', name: 'Rule34 Paheal', engine: 'extension_job', site: 'https://rule34.paheal.net/', key: 'none', keyFormat: '', robots: true, curated: false, tailUpdates: false, onDemand: true, merge: true, mergeMinPosts: 100, job: 'tags.py:read_tags', description: "the extension's own update (tags.py:read_tags), run only when asked: the tags with 100+ posts, merged into the list in use" },
        ].map((s) => ({ ...s, running: mockTagRuns.has(s.module), count: mockTagRuns.get(s.module)?.count || 0, message: mockTagRuns.get(s.module)?.message || '' }));
      }
      if (method === 'tags.learn') {
        // vb: on top of a bundled list; rp: on top of the list of a merge source (no button calls it, the learning after a download does)
        const onTop = p.module === 'vb' || p.module === 'rp';
        return { module: p.module, learned: !onTop, posts: 132, tags: 845, newPosts: 12, written: true, mayLearn: true, sourceList: false, indexed: true,
          bundled: p.module === 'vb', added: p.module === 'vb' ? 37 : p.module === 'rp' ? 12 : 0 };
      }
      if (method === 'tags.status') return [{ module: 'hs', path: '~/.config/Lynxx/tags/hs_tags.json', size: 0.03e6, mtime: Date.now() / 1000 - 3600, learned: true }, { module: 'db', path: '2tags/db_tags.json', size: 21e6, mtime: Date.now() / 1000 }, { module: 'rx', path: '2tags/rx_tags.json', size: 24.6e6, mtime: Date.now() / 1000 - 86400 * 56 }, { module: 'tb', path: '~/.config/Lynxx/extensions/tbib/tb_tags.json', size: 3.2e6, mtime: Date.now() / 1000 - 86400 * 9, shipped: true, version: '1.0.0' }, { module: 'rp', path: '~/.config/Lynxx/extensions/paheal/rp_tags.json', size: 4.33e6, mtime: Date.now() / 1000 - 86400 * 2, learned: false, bundled: false, shipped: true, merged: false, version: '1.0.3', added: 12 }, { module: 'vb', path: '~/.config/Lynxx/extensions/videosite/vb_tags.json', size: 18.9e6, mtime: Date.now() / 1000 - 7200, learned: false, bundled: true, version: '1.0.2', added: 37 }];
      if (method === 'files.nonEmpty') return { exists: true, nonEmpty: false };
      if (method === 'index.stats') {
        const now = Date.now() / 1000;
        const trashed = libFiles.filter((x) => x.trashedAt);
        return { path: '~/.config/Lynxx/index/downloads.sqlite', exists: true, schema: 2, files: 1834, posts: 1801, withMd5: 1790, tags: 9412,
          bytes: 2.9e9, dbBytes: 3.1e6, first: now - 86400 * 40, last: now - 3600, recording: true,
          modules: [{ module: 'rx', files: 1210, bytes: 2.1e9, last: now - 3600 }, { module: 'db', files: 624, bytes: 0.8e9, last: now - 86400 * 3 }],
          trash: { files: trashed.length, bytes: trashed.reduce((n, x) => n + x.size, 0), oldest: trashed.length ? Math.min(...trashed.map((x) => x.trashedAt)) : null } };
      }
      if (method === 'index.search') {
        // a few fake files (the browser cannot read local files: thumbnails fall back to the placeholder); trash: the deleted ones
        const q = String(p.tags || '').trim();
        const hit = libFiles.filter((x) => Boolean(x.trashedAt) === (p.trash === true) && (!p.module || x.module === p.module)
          && (!q || q.split(/\s+/).every((t) => x.tags.split(' ').includes(t))));
        if (p.order === 'trashed') hit.sort((a, b) => b.trashedAt - a.trashedAt);
        const size = p.pageSize || 50;
        const page = p.page || 0;
        return { total: hit.length, page, pageSize: size, items: hit.slice(page * size, (page + 1) * size).map((x) => ({ ...x })), terms: [] };
      }
      throw new Error(`mock: ${method}`);
    },
    tags: {
      update: async (m, _opts) => {
        // like the core: running until done (tags.sources running / count), progress every ~2 s, -32004 while it runs
        if (mockTagRuns.has(m)) throw new Error(`tag list update for '${m}' is already running`);
        mockTagRuns.set(m, { count: 0, message: '' });
        [1, 2, 3].forEach((k) => setTimeout(() => {
          const run = { count: k * 1500, message: `[${m}] ${k * 1500} tags...` };
          mockTagRuns.set(m, run);
          emit('sidecar:notify', { method: 'tags.updateProgress', params: { job: `tags-${m}`, module: m, ...run } });
        }, k * 2000));
        setTimeout(() => {
          mockTagRuns.delete(m);
          emit('sidecar:notify', { method: 'tags.updateDone', params: { module: m, written: true, message: 'written 4500 tags (mock)' } });
        }, 8000);
        return { job: `tags-${m}` };
      },
      scheduleStatus: async () => ({ ...mockSchedule }),
      schedule: async (o) => { Object.assign(mockSchedule, { enabled: !!o.enabled, installed: !!o.enabled, frequency: o.frequency || 'weekly' }); return { ...mockSchedule }; },
    },
    search: {
      count: async (q) => { await delay(300 + latency('search.count', q)); return { count: 143815, requiredTags: [] }; },
      preview: async (q, page) => {
        await delay(400 + latency('search.preview', { ...q, page }));
        const max = PAGE_MAX[q.module] || 100;
        const pageSize = Math.max(1, Math.min(max, Number(q.pageSize) || Math.min(max, 200)));
        const tasks = splitTasks(q.tags);
        const terms = q.tags.split(' ').filter((t) => !t.startsWith('-') && !t.includes(':') && !t.startsWith('('));
        const required = q.module === 'db' && terms.length > 2 ? terms.slice(2) : [];
        const sel = (q.gender && q.gender.select) || [];
        const labels = ['only_female', 'only_male', 'both', 'none', 'unknown'];
        const confs = ['high', 'medium', 'low'];
        const genderStats = {};
        const pages = 3; // every query has three pages in the mock
        const count = page < pages - 1 ? pageSize : Math.ceil(pageSize / 3);
        const items = Array.from({ length: count }, (_, i) => {
          const id = 9000000 - page * pageSize * 7 - i * 7;
          const tags = ['scenery', 'sky', 'outdoors', ...terms.slice(0, 2), i % 3 ? 'cloud' : 'sunset'];
          if (i % 4 === 0) tags.push(...required);
          const excludedBy = required.length > 0 && i % 4 !== 0 ? ['required'] : [];
          let gender;
          if (sel.length) {
            const label = labels[i % 5];
            gender = { label, conf: label === 'unknown' ? null : confs[i % 3], flags: [], evidence: ['mock'], source: 'tags', effective: label };
            genderStats[label] = (genderStats[label] || 0) + 1;
            if (!sel.includes(label)) excludedBy.push('gender');
          }
          // a few posts without a preview address, or whose preview does not load (the card shows its placeholder)
          const preview = i % 11 === 3 ? '' : i % 17 === 5 ? 'data:image/png;base64,AAAA' : thumb(id);
          return { id: String(id), preview, sample: preview, isVideo: i % 9 === 4, mediaKind: i % 9 === 4 ? 'video' : i % 13 === 7 ? 'audio' : 'image', downloaded: i % 5 === 1 ? 'id' : undefined, ext: i % 9 === 4 ? 'mp4' : i % 13 === 7 ? 'mp3' : 'jpg', score: String((id % 97)),
            width: '1920', height: '1080', date: '16-11-2026', rating: 'g', tags, gender, excludedBy, excluded: excludedBy.length > 0 };
        });
        // vb lists no post tags (search.preview localFiltersUnchecked): the tags checked locally are not checked in Preview
        const unchecked = q.module === 'vb' && q.tags.split(' ').length > 1;
        return { page, pageSize, hasMore: page < pages - 1, items, tasks, requiredTags: required, site: 'https://example.invalid/',
          genderStats, blockedCount: sel.length ? 2 : 0, genderEnabled: sel.length > 0, localFiltersUnchecked: unchecked };
      },
      related: async (q) => {
        await delay(500 + latency('search.related', q));
        const inQuery = new Set(q.tags.replace(/[()~,]/g, ' ').split(' ').map((t) => t.replace(/^-/, '')).filter(Boolean));
        const sample = q.module === 'db' ? 200 : 100;
        const items = TAGS.filter(([tag]) => !inQuery.has(tag)).slice(0, 30)
          .map(([tag, posts, type], i) => ({ tag, posts, type, count: Math.max(1, Math.round(sample * (0.9 - i * 0.03))) }));
        return { items, sample };
      },
    },
    settings: {
      get: async () => ({ settings, appearance: appearance(), secrets: secretsStatus() }),
      set: async (patch) => {
        // like main's deepMerge: objects merge, arrays and scalars replace
        const merge = (a, b) => { for (const [k, v] of Object.entries(b)) { if (v && typeof v === 'object' && !Array.isArray(v)) { a[k] = a[k] || {}; merge(a[k], v); } else a[k] = v; } };
        const copy = JSON.parse(JSON.stringify(patch));
        const days = copy.library?.trashDays;
        if (days !== undefined && !(Number.isInteger(days) && days >= 0 && days <= 365)) throw new Error('Số ngày giữ thùng rác phải là số nguyên từ 0 đến 365');
        const maps = {}; // headers / cookies (shared and per website) are replaced as a whole, like main does
        for (const k of ['headers', 'cookies', 'siteHeaders', 'siteCookies']) if (copy.connection && k in copy.connection) { maps[k] = copy.connection[k] || {}; delete copy.connection[k]; }
        merge(settings, copy);
        Object.assign(settings.connection, maps);
        if (patch.appearance) emit('appearance:changed', appearance());
        return { settings: JSON.parse(JSON.stringify(settings)), appearance: appearance(), secrets: secretsStatus() };
      },
    },
    secrets: {
      // same contract as main: an empty value never deletes a stored key, clear() does
      set: async (module, value) => { if (String(value || '').trim()) secrets[module] = true; return secretsStatus(); },
      clear: async (module) => { delete secrets[module]; return secretsStatus(); },
    },
    jobs: {
      start: async (q) => {
        if (!q.allowDuplicate && [...jobs.values()].some((j) => j.status === 'running' && j.module === q.module && j.tags === q.tags)) {
          throw new Error('DUPLICATE: một job giống hệt đang chạy');
        }
        const job = newJob(q);
        jobQueries.set(job.id, { ...q, allowDuplicate: undefined });
        runJob(job);
        return { ...job };
      },
      startMulti: async (q, modules) => {
        const list = [...new Set([q.module, ...(modules || [])])];
        if (list.length < 2 || list.length > 8) throw new Error('cần 2–8 module khác nhau');
        if (q.pick) throw new Error('mock: picked posts are never sent to several sites'); // the renderer never does
        const group = { id: `group-${Date.now()}`, size: list.length, modules: list };
        const made = list.map((module, index) => newJob({ ...q, module }, { group: { ...group, index } }));
        made.forEach((j) => emit('jobs:update', { ...j }));
        runJob(made[0], () => startNextInGroup(group.id));
        return made.map((j) => ({ ...j }));
      },
      cancel: async (id) => {
        const j = jobs.get(id);
        if (j) {
          const wasQueued = j.status === 'queued';
          j.status = 'cancelled';
          emit('jobs:update', { ...j });
          if (wasQueued && j.group) setTimeout(() => { if (![...jobs.values()].some((x) => x.group?.id === j.group.id && x.status === 'running')) startNextInGroup(j.group.id); }, 0);
        }
        return true;
      },
      rerun: async (id, allowDuplicate) => {
        const j = jobs.get(id);
        if (!j) throw new Error('job không còn');
        if (j.kind === 'import') return window.lynxx.index.importLibrary({ folder: j.dest, module: j.module === '*' ? '' : j.module });
        return window.lynxx.jobs.start({ ...(jobQueries.get(id) || { module: j.module, tags: j.tags, path: j.dest }), allowDuplicate: allowDuplicate === true });
      },
      list: async () => [...jobs.values()],
      clear: async () => { for (const [k, j] of jobs) if (j.status !== 'running') jobs.delete(k); return [...jobs.values()]; },
    },
    dialogs: { folder: async () => '', image: async () => '', zip: async () => '', document: async () => '' },
    ext: {
      installZip: async () => { throw new Error('mock'); },
      installCatalog: async (id) => { const m = modules.find((x) => x.id === id); if (m) m.installed = true; return { ...m, restartRequired: false }; },
      installRemote: async () => { throw new Error('mock'); },
    },
    genderModel: {
      // window.__gmDown = true: the model server does not answer. window.__gmHw: 'no_gpu' (default), 'gpu_no_runtime',
      // 'vram_low', 'gpu', 'gpu_fallback' (a GPU runtime that falls back to the CPU once loaded), 'cpu_forced' (--device cpu)
      status: async () => {
        await delay(250);
        const url = settings.genderModel.url;
        const local = /^https?:\/\/(127\.|localhost|\[::1\])/.test(url);
        if (window.__gmDown) return { url, reachable: false, local, error: `model server ${url} is not reachable (ConnectionError)` };
        const kind = window.__gmHw || 'no_gpu';
        const rtx = { vendor: 'nvidia', name: 'NVIDIA GeForce RTX 4070 Ti SUPER', vramMiB: 16376, vramFreeMiB: kind === 'vram_low' ? 600 : 15249, shared: false };
        const igpu = { vendor: 'amd', name: 'AMD GPU 0x164e', vramMiB: 512, vramFreeMiB: 496, shared: false };
        const gpus = kind === 'no_gpu' ? [igpu] : [rtx, igpu];
        const cuda = ['gpu', 'vram_low', 'gpu_fallback', 'cpu_forced'].includes(kind);
        const hardware = { gpus, providers: cuda ? ['CUDAExecutionProvider', 'CPUExecutionProvider'] : ['CPUExecutionProvider'], runtime: '1.32.0', ramMiB: 30728, ramFreeMiB: 20938, cpus: 16, os: 'linux' };
        const device = gmLoaded ? (kind === 'gpu' ? 'cuda' : 'cpu') : '';
        let code = kind === 'gpu_fallback' ? (gmLoaded ? 'gpu_fallback' : 'gpu') : kind;
        if (device === 'cuda') code = 'gpu';
        const check = { level: code === 'gpu' ? 'ok' : 'warn', code, gpu: kind === 'no_gpu' ? null : rtx, runtime: cuda ? 'cuda' : '', device, vramMinMiB: 1536, ramFreeMiB: 20938 };
        return { url, reachable: true, local, error: '', check,
          model: { id: 'wd-vit-tagger-v3', license: 'Apache-2.0', revision: '7f6b584d', ms: 3, loaded: gmLoaded, device, idleUnload: 900, hardware } };
      },
      load: async () => { await delay(600); gmLoaded = true; return window.lynxx.genderModel.status(); },
      unload: async () => { gmLoaded = false; return window.lynxx.genderModel.status(); },
      setToken: async (v) => { if (String(v).length < 8) throw new Error('token: 8–256 ký tự ASCII, không có dấu cách'); gmToken = true; return { set: true, encrypted: true, encryption: true }; },
      clearToken: async () => { gmToken = false; return { set: false, encrypted: false, encryption: true }; },
    },
    saved: {
      setAuto: async (key, every, from) => {
        const e = settings.search.saved.find((x) => `${x.module}\n${x.tags}` === key);
        if (!e) throw new Error('không tìm thấy truy vấn đã lưu');
        if (!every) delete e.auto;
        else e.auto = { ...(e.auto || {}), every, maxId: from === 'now' ? 12565561 : 0, lastRun: from === 'now' ? Date.now() : 0, lastStatus: from === 'now' ? 'baseline' : '', lastCount: 0 };
        return { settings: JSON.parse(JSON.stringify(settings)), appearance: appearance(), secrets: secretsStatus() };
      },
    },
    library: {
      open: async () => '', reveal: async () => true,
      // trash / restore move the fake files; forget, purge and emptyTrash ask first (main: a native dialog, here window.confirm)
      trash: async (paths) => {
        await delay(80);
        const r = { trashed: [], failed: [], refused: [] };
        for (const p of paths) {
          const x = libFiles.find((f) => f.path === p && !f.trashedAt && f.exists);
          if (!x) { r.refused.push(p); continue; }
          const trashPath = `/home/user/.config/Lynxx/trash/${libFiles.indexOf(x) + 1}_${x.name}`;
          Object.assign(x, { origPath: x.path, path: trashPath, trashedAt: Date.now() / 1000 });
          r.trashed.push({ path: p, trashPath });
        }
        return r;
      },
      restore: async (paths) => {
        await delay(80);
        const r = { restored: [], failed: [], refused: [] };
        for (const p of paths) {
          const x = libFiles.find((f) => f.path === p && f.trashedAt);
          if (!x) { r.refused.push(p); continue; }
          Object.assign(x, { path: x.origPath, origPath: '', trashedAt: null });
          r.restored.push({ path: p, restoredPath: x.path });
        }
        return r;
      },
      forget: async (paths) => {
        if (!window.confirm(`(mock) Xoá ${paths.length} mục khỏi thư viện? Không thể hoàn tác.`)) throw new Error('cancelled');
        const gone = libFiles.filter((f) => paths.includes(f.path) && !f.exists);
        gone.forEach((f) => libFiles.splice(libFiles.indexOf(f), 1));
        return { removed: gone.length, refused: paths.filter((p) => !gone.some((f) => f.path === p)) };
      },
      purge: async (paths) => {
        if (!window.confirm(`(mock) Xoá vĩnh viễn ${paths.length} file? Không thể hoàn tác.`)) throw new Error('cancelled');
        return purgeMock((f) => paths.includes(f.path));
      },
      emptyTrash: async () => {
        const n = libFiles.filter((f) => f.trashedAt).length;
        if (!n) return { purged: 0, files: 0, bytes: 0, failed: [], kept: 0 };
        if (!window.confirm(`(mock) Dọn sạch thùng rác? ${n} file bị xoá vĩnh viễn.`)) throw new Error('cancelled');
        return purgeMock(() => true);
      },
    },
    index: {
      // clean: main confirms natively (here: window.confirm), then index.cleanProgress / index.cleanDone notifications
      clean: async (opts) => {
        if (clean.running) throw new Error('index clean is already running');
        const dryRun = opts?.dryRun === true;
        if (!dryRun && !window.confirm('(mock) Dọn index các lần tải? Chỉ xoá dòng trong index, không xoá file.')) throw new Error('cancelled');
        Object.assign(clean, { running: true, stop: false });
        const job = `index-clean-${Date.now()}`;
        const total = 1834;
        let checked = 0;
        const t = setInterval(() => {
          checked = Math.min(total, checked + 500);
          const missing = Math.round(checked * 0.02);
          const r = { job, total, checked, missing, missingDirs: Math.min(9, missing), removed: dryRun ? 0 : Math.max(0, missing - Math.min(9, missing)), dryRun, missingFolders: false, cancelled: clean.stop, folders: ['/media/user/Ổ ngoài/Lynxx'] };
          if (checked < total && !clean.stop) { emit('sidecar:notify', { method: 'index.cleanProgress', params: r }); return; }
          clearInterval(t);
          clean.running = false;
          emit('sidecar:notify', { method: 'index.cleanDone', params: { ...r, ok: true, message: '' } });
        }, 400);
        return { job, dryRun, missingFolders: false };
      },
      cleanCancel: async () => { clean.stop = true; return { cancelled: clean.running }; },
      pickImportFolder: async () => '/home/user/Pictures/booru',
      importLibrary: async (opts) => {
        if (opts.folder !== '/home/user/Pictures/booru') throw new Error('choose the folder with the folder dialog first');
        const job = newJob({ module: opts.module || '*', tags: opts.folder, path: opts.folder },
          { kind: 'import', label: opts.dryRun ? 'Kiểm tra thư mục (không gửi request)' : 'Nhập thư viện vào index' });
        runImport(job, opts);
        return { ...job };
      },
    },
    gen: {
      status: async () => ({ running: gen.running, session: gen.session, events: gen.events.slice(-300), key: aiStatus(), models: ['claude-opus-5-5', 'claude-sonnet-5-5'], defaults: { ...settings.ai } }),
      start: async (opts) => {
        if (gen.running) throw new Error('một phiên tạo extension đang chạy');
        if (opts.repair) { const d = drafts.find((x) => x.session === opts.repair); opts = { ...opts, url: d.url, mode: 'ai' }; }
        if (opts.mode === 'ai' && !aiKey) throw new Error('Chưa có khoá Claude API: thêm trong Cài đặt → AI tạo extension, hoặc dùng chế độ không AI.');
        genRun(opts);
        return { started: true };
      },
      cancel: async () => { const was = gen.running; gen.running = false; return was; },
      list: async () => drafts.map((d) => ({ ...d })),
      get: async (session) => ({ ...drafts.find((d) => d.session === session) }),
      open: async () => '',
      exportZip: async (session) => ({ path: `/home/user/Downloads/${drafts.find((d) => d.session === session)?.extId}-1.0.0.zip`, sha256: 'e3b0c44298fc1c149afbf4c8996fb92427ae41e4649b934ca495991b7852b855' }),
      install: async (session) => {
        if (!window.confirm('(mock) Cài extension vào Lynxx?')) throw new Error('cancelled');
        const d = drafts.find((x) => x.session === session);
        modules.push({ id: d.extId, name: d.name, abbr: d.abbr, version: '1.0.0', source: 'user', default: false, installed: true, media: ['image', 'video'], site: d.site, description: 'Generated draft', auth: {}, syntax: { maxTags: 0 },
          generator: { tool: 'lynxx.aigen', mode: d.mode, model: d.model, engine: d.engine, createdAt: d.finishedAt } });
        return { restartRequired: false };
      },
      remove: async (session) => { if (!window.confirm('(mock) Xoá bản nháp?')) return false; const i = drafts.findIndex((d) => d.session === session); if (i >= 0) drafts.splice(i, 1); return true; },
      setKey: async (value) => { if (String(value || '').trim()) aiKey = true; return aiStatus(); },
      clearKey: async () => { aiKey = false; return aiStatus(); },
    },
    site: {
      // "Xác minh trong Lynxx": no window here; db stands for a site that does not ask right now, the others pass after a moment
      // and get a fake cookie / User-Agent merged into their own entries, like main (the value never goes back to the page)
      verify: async (module) => {
        const m = modules.find((x) => x.abbr === module && x.installed);
        if (!/^[a-z0-9]{2,4}$/.test(String(module)) || !m) throw new Error(`module ${module} chưa được cài`);
        await delay(1500);
        if (module === 'db') return { ok: false, reason: 'not-needed', message: `${m.name}: site không yêu cầu xác minh lúc này` };
        const c = settings.connection;
        const join = (map, entry) => {
          const own = Object.fromEntries(Object.entries(map[module] || {}).filter(([k]) => !Object.keys(entry).some((n) => n.toLowerCase() === k.toLowerCase())));
          map[module] = { ...own, ...entry };
        };
        join(c.siteCookies, { cf_clearance: `mock-clearance-${Date.now()}` });
        join(c.siteHeaders, { 'User-Agent': navigator.userAgent });
        return { ok: true, expires: Math.floor(Date.now() / 1000) + 15 * 60 };
      },
    },
    appearance: { setBackgroundImage: async () => appearance() },
    app: {
      info: async () => ({ version: '2.0.14', electron: 'mock', platform: 'browser', userData: '~/.config/Lynxx', python: '.venv/bin/python', repo: '.', sidecar: { name: 'Lynxx', version: '2.0 r1', python: '3.13' } }),
      relaunch: async () => location.reload(), installLauncher: async () => ({ ok: false, message: 'mock' }), openPath: async () => '', openExternal: async () => false,
    },
    thumbUrl: (url) => url, // (url, referer, module): the mock loads the address itself
    mediaUrl: (url) => url,
    on: (ch, cb) => { (listeners[ch] = listeners[ch] || []).push(cb); return () => {}; },
    ...bookMock(),
  };
  setTimeout(() => emit('sidecar:status', { ok: true, info: { version: '2.0 r1 (mock)' } }), 300);

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
        open: async () => ({ ok: false, error: 'mock' }), import: async () => ({ ok: false, error: 'mock' }), epubPages: async () => [],
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
