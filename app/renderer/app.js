// Lynxx desktop renderer (plain script, no bundler). Talks to main only through window.lynxx (preload bridge or mock.js).
// In Electron the preload bridge exists before this script runs; in a plain browser mock.js is loaded first (see start()).
let L = window.lynxx;

// ---------------- helpers ----------------
const $ = (sel, root = document) => root.querySelector(sel);
const $$ = (sel, root = document) => [...root.querySelectorAll(sel)];
function h(tag, attrs = {}, ...kids) {
  const el = document.createElement(tag);
  for (const [k, v] of Object.entries(attrs || {})) {
    if (v === undefined || v === null || v === false) continue;
    if (k === 'class') el.className = v;
    else if (k === 'text') el.textContent = v;
    else if (k.startsWith('on') && typeof v === 'function') el.addEventListener(k.slice(2), v);
    else if (k === 'dataset') Object.assign(el.dataset, v);
    else el.setAttribute(k, v === true ? '' : v);
  }
  for (const kid of kids.flat()) if (kid !== null && kid !== undefined && kid !== false) el.append(kid.nodeType ? kid : String(kid));
  return el;
}
const fmt = (n) => {
  n = Number(n) || 0;
  if (n >= 1e6) return `${(n / 1e6).toFixed(n >= 1e7 ? 0 : 1)}M`;
  if (n >= 1e3) return `${(n / 1e3).toFixed(n >= 1e4 ? 0 : 1)}K`;
  return String(n);
};
const nf = new Intl.NumberFormat('vi-VN');
const debounce = (fn, ms) => { let t; return (...a) => { clearTimeout(t); t = setTimeout(() => fn(...a), ms); }; };
const escapeRe = (s) => s.replace(/[.*+?^${}()|[\]\\]/g, '\\$&');
// write only on change: no style/layout invalidation for values that did not move
const setText = (el, t) => { if (el.textContent !== t) el.textContent = t; };
const setHidden = (el, hide) => { if (el.hidden !== hide) el.hidden = hide; };
const slot = (el, text) => { setHidden(el, text == null); if (text != null) setText(el, text); };

// Deferred DOM work (job progress, log lines) runs at most once per animation frame, however many events arrived.
const frameTasks = new Set();
let frameReq = 0;
function runFrame() {
  frameReq = 0;
  const tasks = [...frameTasks];
  frameTasks.clear();
  for (const fn of tasks) {
    try { fn(); } catch (err) { console.error(err); }
  }
}
function onFrame(fn) {
  frameTasks.add(fn);
  if (!frameReq) frameReq = requestAnimationFrame(runFrame);
}

// Append-only <pre> log. New lines are buffered and appended once per frame as a block-level chunk (so layout only
// touches the new block, not the whole text); old lines are dropped chunk by chunk only after `slack` extra lines
// piled up. Follows new output only while the user is at the bottom.
const LOG_CHUNK = 250;
function createLogView(pre, max, slack = Math.ceil(max / 4)) {
  const chunks = []; // [{ node, n }] in document order
  let count = 0;
  let pending = [];
  let stick = true;
  const atBottom = () => pre.scrollTop + pre.clientHeight >= pre.scrollHeight - 30;
  pre.addEventListener('scroll', () => { stick = atBottom(); }, { passive: true });
  const chunked = (lines) => {
    const frag = document.createDocumentFragment();
    for (let i = 0; i < lines.length; i += LOG_CHUNK) {
      const part = lines.slice(i, i + LOG_CHUNK);
      const node = h('span', { class: 'log-chunk' }, part.join('\n'));
      chunks.push({ node, n: part.length });
      count += part.length;
      frag.append(node);
    }
    return frag;
  };
  const reveal = () => { if (stick && pre.isConnected) pre.scrollTop = pre.scrollHeight; };
  function flush() {
    if (!pending.length) return;
    const add = pending;
    pending = [];
    if (!pre.isConnected) return;
    if (count + add.length > max + slack) {
      const h0 = stick ? 0 : pre.scrollHeight;
      const t0 = stick ? 0 : pre.scrollTop;
      while (chunks.length && count + add.length - chunks[0].n >= max) {
        const c = chunks.shift();
        c.node.remove();
        count -= c.n;
      }
      // keep the lines the user is reading in place while the top of the log is trimmed
      if (!stick) pre.scrollTop = Math.max(0, t0 - (h0 - pre.scrollHeight));
    }
    pre.append(chunked(add));
    reveal();
  }
  return {
    push(lines) {
      pending.push(...lines);
      if (pending.length > max) pending.splice(0, pending.length - max);
      onFrame(flush);
    },
    reset(lines = []) {
      pending = [];
      chunks.length = 0;
      count = 0;
      pre.replaceChildren(chunked(lines.slice(-max)));
      reveal();
    },
    clear() { stick = true; this.reset([]); },
    reveal,
  };
}

const ago = (ts) => {
  const s = (Date.now() - (Number(ts) || 0)) / 1000;
  if (s < 60) return 'vừa xong';
  if (s < 3600) return `${Math.floor(s / 60)} phút trước`;
  if (s < 86400) return `${Math.floor(s / 3600)} giờ trước`;
  if (s < 86400 * 30) return `${Math.floor(s / 86400)} ngày trước`;
  return new Date(ts).toLocaleDateString('vi-VN');
};

function toast(msg, kind = '', ms = 0) {
  const el = h('div', { class: `toast ${kind}` }, msg);
  $('#toasts').append(el);
  setTimeout(() => el.remove(), ms || (kind === 'err' ? 7000 : 3500));
}
function toastAction(msg, label, fn, ms = 10000, kind = 'ok') {
  // a toast with one button (ex. "Hoàn tác" after a delete): the button runs once, then the toast goes away
  const el = h('div', { class: `toast ${kind} has-action` }, h('span', {}, msg),
    h('button', { class: 'btn sm', onclick: () => { el.remove(); fn(); } }, label));
  $('#toasts').append(el);
  setTimeout(() => el.remove(), ms);
  return el;
}
function errMsg(err) {
  const m = String(err?.message || err || 'lỗi');
  return m.replace(/^Error invoking remote method '[^']+': (Error: )?/, '');
}
// Optional sidecar methods (newer than main / the core): the first "not allowed / not found" answer turns the feature off
// for the session instead of retrying on every keystroke.
const unsupported = new Set();
const isUnsupported = (err) => /not allowed|method not found|unknown method|no handler|-32601/i.test(errMsg(err));
async function rpcOptional(method, params) {
  if (unsupported.has(method)) return null;
  try {
    return await L.rpc(method, params);
  } catch (err) {
    if (isUnsupported(err)) { unsupported.add(method); return null; }
    throw err;
  }
}
function openModal(content) {
  const card = $('#modal-card');
  card.replaceChildren(content);
  $('#modal').hidden = false;
}
function closeModal() { $('#modal').hidden = true; $('#modal-card').replaceChildren(); }
$('#modal').addEventListener('click', (e) => { if (e.target.id === 'modal') closeModal(); });
document.addEventListener('keydown', (e) => { if (e.key === 'Escape' && !$('#modal').hidden) closeModal(); });

function prompt2(title, fields) {
  return new Promise((resolve) => {
    const inputs = fields.map((f) => h('input', { type: f.type || 'text', placeholder: f.placeholder || '', value: f.value || '' }));
    const form = h('div', {},
      h('h2', { text: title }),
      h('div', { class: 'form-grid' }, fields.map((f, i) => h('label', { class: 'field' }, h('span', { text: f.label }), inputs[i]))),
      h('div', { class: 'modal-actions' },
        h('button', { class: 'btn ghost', onclick: () => { closeModal(); resolve(null); } }, 'Huỷ'),
        h('button', { class: 'btn primary', onclick: () => { const v = inputs.map((x) => x.value.trim()); closeModal(); resolve(v); } }, 'OK')));
    openModal(form);
    inputs[0]?.focus();
  });
}

// ---------------- state ----------------
const S = {
  settings: null, appearance: null, secrets: null, info: null,
  modules: [], module: 'rx',
  chips: [], chipSeq: 0, selected: new Set(),
  // moved: the user picked a suggestion with the arrow keys (only then Enter takes a suggestion instead of the typed text)
  ac: { items: [], index: -1, seq: 0, moved: false, fuzzy: false },
  // key: previewKey() of the query the posts on screen answer; failed: the last Preview request failed (the grid is an older
  // answer, maybe of this same query: a page turn that did not load), see previewStale. query: the request of the posts on
  // screen as sent (with pageSize): "Tải đã chọn" on a site without id search reads that page again (pick mode, downloadSelected)
  preview: { page: 0, items: [], selected: new Set(), site: '', module: '', tags: '', key: '', failed: false, showExcluded: false, seq: 0, hasMore: true, kind: 'all', dateFilter: '',
    query: null, pageSize: 0 },
  countSeq: 0,
  tagLists: null, // modules that have a local tag list (tags.status), null until known
  jobs: new Map(),
  logLines: [],
};
const META_PREFIXES = ['id', 'score', 'rating', 'user', 'md5', 'width', 'height', 'parent', 'source', 'updated', 'sort', 'order', 'date',
  'status', 'fav', 'favorited_by', 'favoritedby', 'pool', 'set', 'poster', 'filetype', 'limit', 'age', 'mpixels', 'ratio', 'favcount', 'tagcount'];
// metatags of a module: the names above, the tokens of its search kinds and the metatag names of its manifest ('search')
const metaReCache = new Map();
function metaRe(abbr = S.module) {
  const s = moduleInfo(abbr)?.search || {};
  const names = [...new Set([...META_PREFIXES, ...declaredKinds(abbr).filter((k) => k.token).map((k) => k.token.slice(0, -1)),
    ...(Array.isArray(s.metaNames) ? s.metaNames.map(String) : [])])];
  const key = names.join('|');
  let re = metaReCache.get(key);
  if (!re) {
    re = new RegExp(`^(?:${names.map(escapeRe).join('|')})(?:[:=]|[<>]=?)`);
    if (metaReCache.size > 32) metaReCache.clear();
    metaReCache.set(key, re);
  }
  return re;
}
const POST_URL = {
  rx: (id) => `https://rule34.xxx/index.php?page=post&s=view&id=${id}`,
  db: (id) => `https://danbooru.donmai.us/posts/${id}`,
  en: (id) => `https://e621.net/posts/${id}`,
};
const STATE_VI = {
  IDLE: 'Chờ', LAUNCHING: 'Khởi động', SEARCHING: 'Đang tìm', SCANNING_PAGES1: 'Quét trang (1/2)', SCANNING_PAGES2: 'Quét trang (2/2)',
  FILTERING_ITEMS1: 'Lọc (1/4)', FILTERING_ITEMS2: 'Lọc (2/4)', FILTERING_ITEMS3: 'Lọc (3/4)', FILTERING_ITEMS4: 'Lọc (4/4)',
  DOWNLOADING: 'Đang tải', DONE: 'Hoàn tất', IMPORT_SCAN: 'Quét thư mục', IMPORT_FETCH: 'Lấy metadata từ website',
};
const GENDER_VI = { only_female: 'Nữ', only_male: 'Nam', both: 'Nam+Nữ', none: 'Không có', unknown: '?' };
const CONF_VI = { high: 'cao', medium: 'vừa', low: 'thấp' };
// why a preview post is excluded (search.preview excludedBy); other reasons (neg, required) read 'lọc cục bộ'
const REASON_VI = { gender: 'lọc giới tính', date: 'lọc ngày' };
const JOB_VI = { queued: 'Chờ', running: 'Đang chạy', cancelling: 'Đang huỷ', cancelled: 'Đã huỷ', done: 'Xong', failed: 'Lỗi' };

const moduleInfo = (abbr) => S.modules.find((m) => m.abbr === abbr);
const hasCap = (abbr, cap) => {
  const caps = moduleInfo(abbr)?.capabilities;
  const name = { videoFormat: 'video_format_choice', vres: 'video_quality_choice', parchi: 'parent_child', gender: 'gender_tags' }[cap] || cap;
  if (Array.isArray(caps)) return caps.includes(name);
  if (cap === 'videoFormat') return abbr === 'rs';
  if (cap === 'vres') return false; // a choice of video height: only once the module says it has one
  if (cap === 'parchi') return ['rx', 'db', 'en', 'xb', 'bb'].includes(abbr);
  if (cap === 'gender') return ['rx', 'db', 'en', 'bb'].includes(abbr);
  return true;
};

// modules whose API needs the user's own key (Lynxx ships no shared key): nothing runs without one
const needsKey = (abbr) => {
  const caps = moduleInfo(abbr)?.capabilities; // unknown until the module list is loaded: never claimed then
  return Array.isArray(caps) && caps.includes('api_key_required') && !S.secrets?.modules?.[abbr];
};
function renderKeyNeeded() {
  const m = moduleInfo(S.module);
  const show = !!S.settings && needsKey(S.module);
  $('#key-needed').hidden = !show;
  if (show) {
    $('#key-needed-text').textContent = `${m?.name || S.module} cần khoá API riêng của bạn (Lynxx không dùng khoá chung). `
      + 'Lấy khoá trên site (tài khoản → Options → API Access Credentials), rồi nhập dạng KEY,USER_ID.';
  }
}
function requireKey(abbr) {
  if (!needsKey(abbr)) return true;
  toast(`${moduleInfo(abbr)?.name || abbr} cần khoá API riêng: Cài đặt → API key theo module`, 'err');
  return false;
}
$('#key-needed-btn').addEventListener('click', () => {
  showPage('settings');
  const row = $('#api-keys');
  row.scrollIntoView({ block: 'center' });
  [...row.querySelectorAll('.key-row')].find((r) => r.querySelector('.mod-abbr')?.textContent === S.module)?.querySelector('input')?.focus();
});

// ---------------- navigation ----------------
function showPage(name) {
  $$('#nav button').forEach((b) => b.classList.toggle('active', b.dataset.page === name));
  $$('.page').forEach((p) => p.classList.toggle('active', p.id === `page-${name}`));
  if (name === 'tags') { refreshTagStatus(); renderTagSchedule(); }
  if (name === 'settings') refreshIndexStats();
  if (name === 'library') openLibrary();
  if (name === 'extensions') renderExtensions();
  if (name === 'log') showLogPage();
  if (name === 'downloads') jobViews.forEach((v) => v.logView?.reveal());
}
$$('#nav button').forEach((b) => b.addEventListener('click', () => showPage(b.dataset.page)));

// ---------------- local PDF/EPUB reader ----------------
let readerUrl = '';
$('#reader-open')?.addEventListener('click', async () => {
  const file = await L.dialogs.document();
  if (!file) return;
  const imported = await L.reader.import(file, file.split(/[\\/]/).pop().replace(/\.[^.]+$/, ''));
  if (!imported?.ok) { toast(imported?.error || 'Không nhập được tài liệu', 'err'); return; }
  const result = await L.reader.open(imported.path);
  if (!result?.ok) { toast(result?.error || 'Không mở được tài liệu', 'err'); return; }
  readerUrl = result.url;
  const frame = $('#reader-frame');
  const book = $('#reader-book');
  const pages = $('#reader-pages');
  pages.replaceChildren();
  frame.hidden = result.type !== 'pdf';
  book.hidden = result.type !== 'epub';
  if (result.type === 'epub') {
    try {
      const items = await L.reader.epubPages(imported.path);
      items.forEach((item) => {
        const image = document.createElement('img');
        image.src = item.src;
        image.alt = `Trang ${item.number}`;
        image.loading = 'lazy';
        pages.append(image);
      });
    } catch (err) {
      toast(`Không đọc được EPUB: ${errMsg(err)}`, 'err');
      return;
    }
  } else {
    frame.src = readerUrl;
  }
  $('#reader-empty').hidden = true;
  pages.classList.toggle('reader-horizontal', $('#reader-mode').value === 'horizontal');
  frame.classList.toggle('reader-horizontal', $('#reader-mode').value === 'horizontal');
});
$('#reader-mode')?.addEventListener('change', (event) => {
  $('#reader-frame')?.classList.toggle('reader-horizontal', event.target.value === 'horizontal');
  $('#reader-pages')?.classList.toggle('reader-horizontal', event.target.value === 'horizontal');
});

// ---------------- appearance ----------------
function applyAppearance(a) {
  S.appearance = a;
  document.documentElement.dataset.theme = a.theme;
  $('#brand-icon').src = a.iconUrl;
  const bg = a.background || {};
  const bgEl = $('#bg');
  bgEl.className = bg.mode === 'image' ? 'image' : bg.mode === 'none' ? 'none' : '';
  $('#bg-img').src = a.backgroundUrl;
  const root = document.documentElement.style;
  root.setProperty('--bg-blur', `${bg.mode === 'icon' ? (bg.blur ?? 4) : 0}px`);
  root.setProperty('--bg-opacity', String(bg.opacity ?? 0.9));
  root.setProperty('--bg-dim', String(bg.dim ?? 0.18));
  renderAppearanceSettings();
}

// ---------------- modules ----------------
async function loadModules() {
  try {
    S.modules = await L.rpc('ext.list');
  } catch (err) {
    toast(`Không đọc được danh sách extension: ${errMsg(err)}`, 'err');
    return;
  }
  const installed = S.modules.filter((m) => m.installed);
  const was = S.module;
  if (!installed.some((m) => m.abbr === S.module)) S.module = installed[0]?.abbr || 'rx';
  noteModuleChanges(S.modules);
  // the module in use was removed: Tags A-Z follows the one that takes its place (its rows, its type filters)
  if (S.module !== was) azInstances.forEach((az) => az.reset());
  const updates = S.modules.filter((m) => m.updateAvailable);
  setHidden($('#badge-ext'), !updates.length);
  setText($('#badge-ext'), String(updates.length));
  const key = updates.map((m) => `${m.id}@${m.catalogVersion}`).join(',');
  if (key && key !== S.updatesNotified) { // once per set of versions, not on every reload of the list
    S.updatesNotified = key;
    toast(`Có bản mới cho ${updates.map((m) => `${m.name} (v${m.catalogVersion})`).join(', ')}: xem trang Extensions`, 'ok');
  }
  renderModuleButton();
  rekindChips(); // the module's kind tokens / metatag names are known now
  if (S.chips.length) renderChips(); // kind chips read "Nghệ sĩ: x" once the kinds are known
  if (S.settings) { renderApiKeys(); renderTagLimits(); renderVerifyModules(); }
}
async function loadTagLists() {
  try {
    const rows = await L.rpc('tags.status');
    S.tagLists = new Set(rows.filter((r) => r.size > 0).map((r) => r.module));
    S.chips.forEach(decorateChip);
    scheduleResolve();
  } catch { /* sidecar not ready yet: retried when it reports ready */ }
}
function renderModuleButton() {
  const m = moduleInfo(S.module);
  $('#module-abbr').textContent = S.module;
  $('#module-name').textContent = m?.name || S.module;
  $('#f-vformat-wrap').hidden = !hasCap(S.module, 'videoFormat');
  $('#f-vres-wrap').hidden = !hasCap(S.module, 'vres');
  $('#f-audio-wrap').hidden = !(moduleInfo(S.module)?.media || []).includes('audio');
  $('#f-parchi-wrap').hidden = !hasCap(S.module, 'parchi');
  renderKeyNeeded();
  renderGenderFilter();
  renderMultiSites();
  renderIdSearch();
  renderDateFields();
  filtersSummary(); // the summary names only the choices this module shows (audio, video quality)
}

// ---------------- gender filter (tier 0: tags) ----------------
function genderState() {
  const g = S.settings?.download?.gender || {};
  return { select: Array.isArray(g.select) ? g.select : [], conf: g.conf || 'medium', strict: !!g.strict, op: g.op === 'exclude' ? 'exclude' : 'keep',
    mode: g.mode === 'model' ? 'model' : 'tags', verifyMedia: g.verifyMedia === true };
}
function renderGenderFilter() {
  if (!S.settings) return;
  const g = genderState();
  const ex = g.op === 'exclude';
  $$('#f-gender .pill').forEach((b) => { b.classList.toggle('on', g.select.includes(b.dataset.g)); b.classList.toggle('ex', ex); });
  $$('#f-gender-op button').forEach((b) => b.classList.toggle('on', b.dataset.op === g.op));
  $('#f-gender-conf').value = g.conf;
  $('#f-gender-mode').value = g.mode;
  $('#f-gender-strict').checked = g.strict;
  // -gender_verify_media: shown while 'Tag + model' can be picked, usable with it picked
  const verify = $('#f-gender-verify');
  $('#f-gender-verify-wrap').hidden = !gmOn();
  verify.checked = g.verifyMedia;
  verify.disabled = g.mode !== 'model';
  $('#f-gender-verify-wrap').title = g.mode === 'model'
    ? 'Sau khi tải, video và ảnh động mà tag chưa đủ kết luận được model xem lại trên tối đa 8 khung hình. File không khớp bộ lọc được chuyển vào thư mục con _gender_mismatch của thư mục lưu (không xoá). Model server cần ffmpeg để đọc video.'
    : 'Chỉ dùng được với cách xác định "Tag + model (ảnh)"';
  const hint = [];
  if (g.select.length) hint.push(`${ex ? 'loại' : 'giữ'}: ${genderPicked(g)}`);
  if (g.select.length && !hasCap(S.module, 'gender')) hint.push(`${S.module.toUpperCase()} ít tag giới tính, phần lớn post sẽ là "Không rõ"`);
  $('#f-gender-hint').textContent = hint.join(' · ');
}
// pills as words: 'Không rõ / không có' stands for both 'none' and 'unknown'
const GENDER_ALL = ['only_female', 'only_male', 'both', 'none', 'unknown'];
function genderPicked(g) {
  const names = { only_female: 'chỉ nữ', only_male: 'chỉ nam', both: 'cả hai', unknown: 'không rõ / không có' };
  return [...new Set(g.select.map((x) => (x === 'none' ? 'unknown' : x)))].map((x) => names[x] || x).join(', ');
}
function saveGender(patch) {
  const g = { ...genderState(), ...patch };
  S.settings.download.gender = g;
  saveSettings({ download: { gender: g } });
  renderGenderFilter();
  markPreviewStale(); // the posts on screen were filtered with the previous choice
}
$('#f-gender').addEventListener('click', (e) => {
  const b = e.target.closest('.pill');
  if (!b) return;
  const labels = b.dataset.g === 'unknown' ? ['none', 'unknown'] : [b.dataset.g];
  const g = genderState();
  const sel = new Set(g.select);
  const on = labels.every((l) => sel.has(l));
  labels.forEach((l) => (on ? sel.delete(l) : sel.add(l)));
  if (g.op === 'exclude' && sel.size >= GENDER_ALL.length) { toast('Loại bỏ mọi nhóm thì không còn post nào', 'err'); return; }
  saveGender({ select: [...sel] });
});
$('#f-gender-op').addEventListener('click', (e) => {
  const op = e.target.closest('button')?.dataset.op;
  const g = genderState();
  if (!op || op === g.op) return;
  // same filter, other wording: the picked groups become the other groups (none + unknown stay together)
  const picked = new Set(g.select);
  const select = g.select.length ? GENDER_ALL.filter((l) => !picked.has(l)) : [];
  saveGender({ op, select: select.length >= GENDER_ALL.length ? [] : select });
});
$('#f-gender-conf').addEventListener('change', (e) => saveGender({ conf: e.target.value }));
$('#f-gender-mode').addEventListener('change', (e) => {
  if (e.target.value === 'model' && !gmOn()) {
    e.target.value = 'tags';
    toast('Model nhận diện đang tắt: bật ở Cài đặt → Model nhận diện giới tính', 'err');
    return;
  }
  if (e.target.value === 'model' && S.genderModel && !S.genderModel.reachable) {
    toast(`Model server chưa phản hồi (${S.genderModel.error || S.settings.genderModel.url}): preview và job sẽ báo lỗi cho tới khi server chạy`, 'err');
  }
  saveGender({ mode: e.target.value });
});
$('#f-gender-strict').addEventListener('change', (e) => saveGender({ strict: e.target.checked }));
$('#f-gender-verify').addEventListener('change', (e) => saveGender({ verifyMedia: e.target.checked }));
function renderModuleMenu() {
  const menu = $('#module-menu');
  const installed = S.modules.filter((m) => m.installed);
  menu.replaceChildren(
    ...installed.map((m) => h('button', { class: m.abbr === S.module ? 'on' : '', onclick: () => setModule(m.abbr) },
      h('span', { class: 'mod-abbr' }, m.abbr), h('span', {}, m.name), m.default ? h('span', { class: 'tagpill acc', style: 'margin-left:auto' }, 'mặc định') : null)),
    h('div', { class: 'menu-foot' }, `${S.modules.length - installed.length} extension khác có thể cài trong mục Extensions`),
  );
}
function warmupTags(abbr) {
  // load the module tag list in the sidecar ahead of the first keystroke (~1 s for large lists)
  L.rpc('tags.autocomplete', { module: abbr, prefix: 'zz', limit: 1 }).catch(() => {});
}
function setModule(abbr) {
  S.module = abbr;
  warmupTags(abbr);
  rekindChips(); // what is a metatag depends on the module (its kind tokens, its metatag names)
  $('#module-menu').hidden = true;
  renderModuleButton();
  L.settings.set({ search: { module: abbr } });
  azInstances.forEach((az) => az.reset());
  renderChips(); // chip hints (post counts, aliases) belong to the module's tag list
  onQueryChanged();
}
$('#module-btn').addEventListener('click', (e) => {
  e.stopPropagation();
  const menu = $('#module-menu');
  if (menu.hidden) renderModuleMenu();
  menu.hidden = !menu.hidden;
});
document.addEventListener('click', (e) => { if (!e.target.closest('#module-picker')) $('#module-menu').hidden = true; });

// ---------------- search kinds ----------------
// The query stays one string. A kind is declared by the module's manifest ('search', ext.list rows) with a token: one tag
// '<token><value>' of the query ('user:Some.Name', 'artist:some_artist'), typed by hand or added from Tags A-Z ('artist:x' rows of
// a list). There is no picker: the "Tìm theo" select of 2.0.11 is gone, and with it the kinds by tag type (plain tags, as ever)
const SEARCH_KIND_VI = { artist: 'Nghệ sĩ', character: 'Nhân vật', copyright: 'Series', model: 'Người mẫu', category: 'Chuyên mục',
  uploader: 'Người đăng', favorites: 'Yêu thích của người dùng', pool: 'Pool', collection: 'Bộ sưu tập', keyword: 'Từ khoá' };
const kindLabel = (k) => String(k.label || SEARCH_KIND_VI[k.id] || k.id);
// kinds the module's manifest declares (ids this app labels: a newer core may know more)
function declaredKinds(abbr = S.module) {
  const kinds = moduleInfo(abbr)?.search?.kinds;
  return Array.isArray(kinds) ? kinds.filter((k) => k && SEARCH_KIND_VI[k.id]) : [];
}
// the kind whose token starts a text (a tag without its '-', or what is being typed), as the core reads it: case matters
const tokenKind = (text, abbr = S.module) => declaredKinds(abbr).find((k) => k.token && text.startsWith(k.token)) || null;
// the kind of a query tag on a module: its token starts the tag (a leading '-' and the %…% escape set aside)
function tagKind(tag, abbr = S.module) {
  let t = String(tag || '');
  if (t.length > 2 && t.startsWith('%') && t.endsWith('%')) t = t.slice(1, -1);
  const negated = t.startsWith('-');
  t = t.replace(/^-/, '');
  const k = tokenKind(t, abbr);
  return k ? { kind: k, value: t.slice(k.token.length), negated } : null;
}
// every tag of a query, group members included ('-' kept on excluded ones)
function queryTags(q) {
  return splitQuery(q).tokens.flatMap((tok) => {
    const c = parseToken(tok);
    if (!c) return [];
    if (c.kind === 'or') return c.items;
    if (c.kind === 'neg') return c.items.map((t) => `-${t.replace(/^-/, '')}`);
    return [`${c.exclude ? '-' : ''}${c.text}`];
  });
}
const queryKinds = (q = queryString(), abbr = S.module) => queryTags(q).map((t) => tagKind(t, abbr)).filter(Boolean);
// the kind that makes the results come in the site's order (a listing, or order 'site'): no date limits, no id cursor
function siteOrderKind(q = queryString(), abbr = S.module) {
  return queryKinds(q, abbr).find((m) => !m.negated && (m.kind.listing || m.kind.order === 'site'))?.kind || null;
}
// tags.types of a module's list in use, for the A-Z type filters: module -> promise of { hasList, types: { general: n, ... } },
// or of null when the core is older (no tags.types) or the sidecar did not answer (then asked again next time)
const listTypes = new Map();
function loadListTypes(abbr) {
  if (!L) return Promise.resolve(null);
  if (!listTypes.has(abbr)) {
    const ask = rpcOptional('tags.types', { module: abbr })
      .then((r) => (r?.types && typeof r.types === 'object' ? { hasList: Boolean(r.hasList), types: r.types } : null))
      .catch(() => {
        if (listTypes.get(abbr) === ask) listTypes.delete(abbr);
        return null;
      });
    listTypes.set(abbr, ask);
  }
  return listTypes.get(abbr);
}
// the module's list changed (an update, tags learned, its extension): its types are asked again by the A-Z browsers
function forgetListTypes(abbr) {
  listTypes.delete(abbr);
  azInstances.forEach((az) => az.typesChanged(abbr));
}
// an extension installed, updated or removed may bring or take a tag list: what ext.list says per abbreviation, the last time
const moduleStamps = new Map();
function noteModuleChanges(modules) {
  const now = new Map();
  for (const m of modules) now.set(m.abbr, `${now.get(m.abbr) || ''}${m.id}@${m.version}:${m.source}:${m.installed ? 1 : 0};`);
  const first = !moduleStamps.size;
  for (const abbr of new Set([...moduleStamps.keys(), ...now.keys()])) {
    if (!first && moduleStamps.get(abbr) !== now.get(abbr)) forgetListTypes(abbr);
  }
  moduleStamps.clear();
  now.forEach((v, k) => moduleStamps.set(k, v));
}
// what a kind is, for the title of its chip
function kindTitle(k) {
  const parts = [`${kindLabel(k)} (${k.token}<giá trị>)`];
  if (k.listing) parts.push('danh sách riêng của site: tag khác trong truy vấn được lọc cục bộ, theo thứ tự của site, không lọc theo ngày, không dùng nhóm OR');
  else if (k.order === 'site') parts.push('kết quả theo thứ tự của site, không lọc theo ngày');
  if (k.example) parts.push(`ví dụ: ${k.token}${k.example}`);
  return parts.join(' · ');
}
// the value of a kind chip -> its query tag, included or excluded (the ± of the chip). Names of tags in lower case. A value with
// characters the core's tag check refuses goes in the %…% escape ('-' inside), unless the kind's value regex allows it. Favorites /
// pool kinds ('use') go through the core's tag check whatever their regex: lower case (the sites' user and pool names are), and
// the escape for anything else than [a-z0-9_.-]
function kindValueAllowed(k, v) {
  if (!k.value) return false;
  try { return new RegExp(`^(?:${k.value})$`).test(v); } catch { return false; } // a Python only construct: escaped
}
const listingNotExcluded = (k) => `${kindLabel(k)} chọn một danh sách riêng của site: không loại trừ được`;
// why a tag the app is about to add as an exclusion (a suggestion, a row of Tags A-Z) is not added: a listing's. '' otherwise
function excludeRefused(tag) {
  const k = tagKind(tag)?.kind;
  return k?.listing ? listingNotExcluded(k) : '';
}
function kindTag(k, value, exclude = false) {
  let v = String(value || '');
  if (k.input === 'tag' || k.use) v = v.toLowerCase();
  if (!v) return { error: `${kindLabel(k)}: chưa có giá trị` };
  if (k.input === 'id' && !/^\d+$/.test(v)) return { error: `${kindLabel(k)}: giá trị phải là số (id)` };
  if (exclude && k.listing) return { error: listingNotExcluded(k) };
  const plain = k.use ? /^[a-z0-9_.-]+$/.test(v) && (!k.value || kindValueAllowed(k, v)) : /^[a-z0-9_-]+$/.test(v) || kindValueAllowed(k, v);
  return { tag: plain ? `${exclude ? '-' : ''}${k.token}${v}` : `%${exclude ? '-' : ''}${k.token}${v}%` };
}
// ± of a chip: a tag of a kind is built again from its value (an escaped one keeps its '-' inside the %…%), a listing is never
// excluded; other tags flip their '-'. '' when done, else why not
function toggleChipExclude(c) {
  if (c.kind !== 'tag' && c.kind !== 'meta') return '';
  const km = tagKind(c.text);
  if (!km) { c.exclude = !c.exclude; return ''; }
  const r = kindTag(km.kind, km.value, !(c.exclude || km.negated));
  if (r.error) return r.error;
  const p = parseToken(r.tag);
  if (p) Object.assign(c, { kind: p.kind, text: p.text, exclude: p.exclude });
  return '';
}
// chips of tags / metatags typed before the module list (or another module) was known: meta or not per the module in use
function rekindChips() {
  let changed = false;
  for (const c of S.chips) {
    if (c.kind !== 'tag' && c.kind !== 'meta') continue;
    const kind = metaRe().test(c.text) ? 'meta' : 'tag';
    if (kind !== c.kind) { c.kind = kind; changed = true; }
  }
  return changed;
}

// ---------------- id searches (manifest search.idSearch: false when 'id:' searches do not work on the module) ----------------
const FILES_TITLE = $('#btn-files').title;
const idSearchOk = (abbr) => moduleInfo(abbr)?.search?.idSearch !== false;
function renderIdSearch() {
  const ok = idSearchOk(S.module);
  const b = $('#btn-files');
  b.disabled = !ok;
  b.title = ok ? FILES_TITLE : 'Site này không tìm được theo id post hay md5 nên không tìm theo tên file được. Dán link post của site khác vẫn mở hộp tìm.';
  updateSelButton();
}

// ---------------- date limits: none for a kind in the site's order (a listing, a pool in pool order...) ----------------
// on the module or on a site picked in "Tải thêm từ site khác" (every site gets the same dates; one in the site's order would
// refuse them): { kind, abbr } or null
function dateBlockingKind(q = queryString()) {
  for (const abbr of [S.module, ...multi]) {
    const kind = siteOrderKind(q, abbr);
    if (kind) return { kind, abbr };
  }
  return null;
}
function renderDateFields() {
  const b = dateBlockingKind();
  const k = b?.kind;
  const where = b && b.abbr !== S.module ? ` trên ${moduleInfo(b.abbr)?.name || b.abbr} (Tải thêm từ site khác)` : '';
  for (const id of ['#f-datemin', '#f-datemax']) {
    const el = $(id);
    el.disabled = !!k;
    el.closest('.field').title = k ? `${kindLabel(k)} (${k.token})${where} lấy post theo thứ tự của site: không lọc theo ngày được. `
      + 'Khoảng ngày đã nhập được giữ lại và dùng lại khi bỏ kiểu tìm này khỏi truy vấn.' : '';
  }
}
const dateValue = (id) => ($(id).disabled ? '' : $(id).value);

// ---------------- tag chips ----------------
// Query text -> tokens. Whitespace splits tokens, except inside an OR group "( a ~ b )" or an exclusion group "-( a , b )".
// Tags may contain parentheses themselves (star_(sky), :( ), so only a word starting with "(" or "-(" opens a group, and it
// only takes in the next word across a group boundary ("(", "~", "," or ")"). open: the text ends inside an unclosed group.
const parenDepth = (s) => { let d = 0; for (const ch of s) { if (ch === '(') d++; else if (ch === ')') d--; } return d; };
function splitQuery(text) {
  const words = String(text || '').trim().split(/\s+/).filter(Boolean);
  const tokens = [];
  let open = false;
  for (let i = 0; i < words.length; i++) {
    let tok = words[i];
    if (/^-?\(/.test(tok)) {
      while (parenDepth(tok) > 0 && i + 1 < words.length && (/[(~,]$/.test(tok) || /^[)~,]/.test(words[i + 1]))) tok += words[++i];
      if (parenDepth(tok) > 0 && i === words.length - 1) open = true;
    }
    tokens.push(tok);
  }
  return { tokens, open };
}
function parseToken(tok) {
  tok = tok.trim();
  if (!tok) return null;
  // groups mirror the core parser (tags_parser.py): parts may contain parentheses, "(star_(sky)~cloud)", "-(star_(sky),cloud)"
  let m = /^-\((.+)\)$/.exec(tok);
  if (m && m[1].includes(',')) {
    const items = m[1].split(',').map((s) => s.trim()).filter(Boolean);
    if (items.length > 1) return { kind: 'neg', items };
  }
  m = /^\((.+)\)$/.exec(tok);
  if (m && m[1].includes('~')) {
    const items = m[1].split('~').map((s) => s.trim()).filter(Boolean);
    if (items.length > 1) return { kind: 'or', items };
  }
  let exclude = false;
  let text = tok;
  if (text.length > 1 && text.startsWith('-')) { exclude = true; text = text.slice(1); }
  return { kind: metaRe().test(text) ? 'meta' : 'tag', text, exclude };
}
function chipToString(c) {
  if (c.kind === 'or') return `(${c.items.join('~')})`;
  if (c.kind === 'neg') return `-(${c.items.join(',')})`;
  return `${c.exclude ? '-' : ''}${c.text}`;
}
const chipsToString = (chips) => chips.map(chipToString).join(' ');
const queryString = () => chipsToString(S.chips);

function addChip(token) {
  const c = parseToken(token);
  if (!c) return;
  const s = chipToString(c);
  if (S.chips.some((x) => chipToString(x) === s)) return;
  S.chips.push({ ...c, id: ++S.chipSeq });
  renderChips();
  onQueryChanged();
}
// a typed favorites / pool tag ('favorited_by:Some_Name', 'pool:My_Pool') is written the way the core's tag check takes it, as
// the 2.0.11 picker did: lower case, the %…% escape for anything else than [a-z0-9_.-] (kindTag). Other typed tags stay as typed
function typedKindTag(token, abbr = S.module) {
  const km = /^[-%]?[a-z]/.test(token) ? tagKind(token, abbr) : null;
  if (!km?.kind.use || !km.value) return token;
  const r = kindTag(km.kind, km.value, km.negated);
  return r.tag || token;
}
function addTokens(text) {
  splitQuery(text).tokens.forEach((tok) => addChip(typedKindTag(tok)));
}
function removeChip(id) {
  S.chips = S.chips.filter((c) => c.id !== id);
  S.selected.delete(id);
  renderChips();
  onQueryChanged();
}
function setChips(chips) {
  S.chips = chips;
  S.selected.clear();
  renderChips();
  onQueryChanged();
}

const CHIP_TITLE = { tag: 'Tag', meta: 'Meta tag', or: 'Nhóm OR: post có ít nhất một tag', neg: 'Nhóm loại trừ: bỏ post có đủ tất cả tag' };
const chipViews = new Map(); // chip id -> { root, info, title }: tag hints are patched into a chip without rebuilding the row
function renderChips() {
  chipViews.clear();
  $('#chips').replaceChildren(...S.chips.map((c) => {
    // a tag of one of the module's search kinds ('artist:some_artist') reads "Nghệ sĩ: some_artist"
    const km = c.kind === 'tag' || c.kind === 'meta' ? tagKind(c.text) : null;
    const label = c.kind === 'or' ? c.items.join(' ∨ ') : c.kind === 'neg' ? `−(${c.items.join(' ∧ ')})` : km ? km.value : c.text;
    const cls = ['chip', c.kind === 'meta' || km ? 'meta' : '', km ? 'kind' : '', c.kind === 'or' ? 'or' : '', c.kind === 'neg' ? 'neg' : '',
      c.exclude || km?.negated ? 'exclude' : '', S.selected.has(c.id) ? 'sel' : ''].join(' ');
    const title = km ? `${kindTitle(km.kind)}\n${chipToString(c)} — bấm để chọn, bấm đúp để sửa`
      : `${CHIP_TITLE[c.kind]} — bấm để chọn, bấm đúp để sửa`;
    const info = h('span', { class: 'ci' });
    const root = h('span', {
      class: cls, title,
      onclick: (e) => { if (e.target.closest('button')) return; toggleChipSel(c.id); },
      ondblclick: (e) => { if (!e.target.closest('button')) editChip(c); },
    },
    km ? h('span', { class: 'k' }, `${kindLabel(km.kind)}:`) : null,
    h('span', { class: 't' }, label), info,
    (c.kind === 'tag' || c.kind === 'meta') && !(km?.kind.listing && !km.negated && !c.exclude) ? h('button', { class: 'pm', title: 'Đảo bao gồm/loại trừ', onclick: () => {
      const why = toggleChipExclude(c);
      if (why) { toast(why, 'err'); return; }
      renderChips(); onQueryChanged();
    } }, '±') : null,
    h('button', { class: 'x', title: 'Xoá', onclick: () => removeChip(c.id) }, '×'));
    chipViews.set(c.id, { root, info, title });
    decorateChip(c);
    return root;
  }));
  const n = S.selected.size;
  $('#chip-actions').hidden = n === 0;
  $('#chip-sel-count').textContent = `${n} tag đã chọn`;
  scheduleResolve();
}
function toggleChipSel(id) {
  if (S.selected.has(id)) S.selected.delete(id); else S.selected.add(id);
  renderChips();
}
function editChip(c) {
  removeChip(c.id);
  const input = $('#tag-text');
  input.value = chipToString(c); // a tag of a search kind too: token and escape as typed (Enter gives the same tag back)
  input.focus();
}

// ----- chip hints from the module tag list (tags.resolve): post count, alias of a renamed tag, unknown tag -----
const resolveCache = new Map(); // `${module}\n${tag}` -> row | null (request pending) | false (no answer for this tag)
const resolveQueue = new Map(); // module -> Set(tag)
const resolveHasList = new Map(); // module -> the sidecar has a tag list for it (tags.resolve hasList)
const RESOLVE_BATCH = 64;
const rkey = (module, tag) => `${module}\n${tag}`;
function chipTags(c) {
  // tag names and wildcard patterns; metatags and %escaped% tags (no validation on purpose) have nothing to look up
  const raw = c.kind === 'tag' ? [c.text] : (c.kind === 'or' || c.kind === 'neg') ? c.items : [];
  const re = metaRe();
  return raw.map((t) => t.replace(/^-/, '').trim().toLowerCase()).filter((t) => t && !re.test(t) && !/^%.*%$/.test(t));
}
function scheduleResolve() {
  if (unsupported.has('tags.resolve') || !L) return;
  const module = S.module;
  if (resolveHasList.get(module) === false || (S.tagLists && !S.tagLists.has(module))) return; // no tag list: nothing to look up
  let queued = false;
  for (const c of S.chips) {
    for (const t of chipTags(c)) {
      const k = rkey(module, t);
      if (resolveCache.has(k)) continue;
      resolveCache.set(k, null);
      if (!resolveQueue.has(module)) resolveQueue.set(module, new Set());
      resolveQueue.get(module).add(t);
      queued = true;
    }
  }
  if (queued) flushResolve();
}
const flushResolve = debounce(async () => {
  const batches = [...resolveQueue];
  resolveQueue.clear();
  for (const [module, set] of batches) {
    const tags = [...set];
    for (let i = 0; i < tags.length; i += RESOLVE_BATCH) {
      const part = tags.slice(i, i + RESOLVE_BATCH);
      let r = null;
      try {
        r = await rpcOptional('tags.resolve', { module, tags: part });
      } catch { /* transient: forget the pending marks so the next render asks again */ }
      if (!r) { part.forEach((t) => resolveCache.delete(rkey(module, t))); continue; }
      if (typeof r.hasList === 'boolean') resolveHasList.set(module, r.hasList);
      // one item per input tag, same order (the sidecar normalizes the names, so match by position)
      const items = Array.isArray(r.items) ? r.items : [];
      const byName = new Map(items.map((it) => [String(it.tag).toLowerCase(), it]));
      part.forEach((t, k) => resolveCache.set(rkey(module, t), (items.length === part.length ? items[k] : byName.get(t)) || false));
    }
    if (module === S.module) S.chips.forEach(decorateChip);
  }
  if (resolveCache.size > 5000) for (const [k, v] of resolveCache) if (v !== null) resolveCache.delete(k);
}, 180);
function reresolveTags(module) {
  // the module's list changed (tags learned, an update, the "Học từ file đã tải" button): the tags of the chips shown are asked
  // again and keep their hints until the answers arrive (no blink); its other cached answers are dropped
  resolveHasList.delete(module);
  const shown = new Set(module === S.module ? S.chips.flatMap(chipTags) : []);
  let queued = false;
  for (const k of [...resolveCache.keys()]) {
    if (!k.startsWith(`${module}\n`)) continue;
    const t = k.slice(module.length + 1);
    if (!shown.has(t) || unsupported.has('tags.resolve')) { resolveCache.delete(k); continue; }
    if (!resolveQueue.has(module)) resolveQueue.set(module, new Set());
    resolveQueue.get(module).add(t);
    queued = true;
  }
  if (queued) flushResolve();
}
function chipHints(c) {
  const module = S.module;
  // "unknown tag" only means something when the module has a tag list
  const listKnown = resolveHasList.has(module) ? resolveHasList.get(module) : !!S.tagLists?.has(module);
  const rows = chipTags(c).map((t) => [t, resolveCache.get(rkey(module, t))]).filter(([, r]) => r);
  return {
    rows,
    aliases: rows.filter(([t, r]) => r.alias && r.canonical && r.canonical !== t),
    unknown: listKnown ? rows.filter(([, r]) => !r.known && r.type !== 'meta') : [],
  };
}
function decorateChip(c) {
  const v = chipViews.get(c.id);
  if (!v) return;
  const { rows, aliases, unknown } = chipHints(c);
  const kids = [];
  const lines = [];
  for (const [t, r] of rows) {
    if (r.alias && r.canonical && r.canonical !== t) lines.push(`${t}: alias của ${r.canonical}${r.posts ? ` (${nf.format(r.posts)} post)` : ''}`);
    else if (!r.known && r.type === 'wildcard') lines.push(`${t}: mẫu wildcard không khớp tag nào trong danh sách tag của ${S.module.toUpperCase()}`);
    else if (!r.known && r.type !== 'meta') lines.push(`${t}: không có trong danh sách tag của ${S.module.toUpperCase()} (gõ sai hoặc danh sách cũ?)`);
    else if (r.posts) lines.push(`${t}: ${nf.format(r.posts)} post`);
  }
  if (c.kind === 'tag' && rows.length === 1 && rows[0][1].posts > 0 && !unknown.length) kids.push(h('span', { class: 'cnt' }, fmt(rows[0][1].posts)));
  if (aliases.length) {
    const label = c.kind === 'tag' ? `→ ${aliases[0][1].canonical}` : `→ ${aliases.length} alias`;
    kids.push(h('button', { class: 'alias', title: `Thay bằng tag chuẩn: ${aliases.map(([t, r]) => `${t} → ${r.canonical}`).join(', ')}`,
      onclick: () => replaceAliases(c) }, label));
  }
  if (unknown.length) kids.push(h('span', { class: 'warn', title: lines.filter((l) => /không (có|khớp)/.test(l)).join('\n') }, '?'));
  v.info.replaceChildren(...kids);
  v.root.classList.toggle('unknown', unknown.length > 0);
  v.root.classList.toggle('aliased', aliases.length > 0);
  v.root.title = lines.length ? `${v.title}\n${lines.join('\n')}` : v.title;
}
function replaceAliases(c) {
  const map = new Map(chipHints(c).aliases.map(([t, r]) => [t, r.canonical]));
  if (!map.size) return;
  const swap = (t) => { const neg = t.startsWith('-'); const base = neg ? t.slice(1) : t; const to = map.get(base.toLowerCase()); return to ? `${neg ? '-' : ''}${to}` : t; };
  const next = { ...c };
  if (c.kind === 'tag') next.text = swap(c.text);
  else next.items = [...new Set(c.items.map(swap))];
  const s = chipToString(next);
  // the canonical tag may already be in the query: then the alias chip just goes away
  const chips = S.chips.some((x) => x.id !== c.id && chipToString(x) === s) ? S.chips.filter((x) => x.id !== c.id) : S.chips.map((x) => (x.id === c.id ? next : x));
  S.chips = chips;
  S.selected.delete(c.id);
  renderChips();
  onQueryChanged();
  toast(`Đã thay alias: ${[...map].map(([a, b]) => `${a} → ${b}`).join(', ')}`, 'ok');
}

// ----- grouping: merging chips into an OR / exclusion group can change what the query means -----
function planGroup(act) {
  const sel = S.chips.filter((c) => S.selected.has(c.id));
  const items = [];
  const warn = [];
  if (act === 'or') {
    for (const c of sel) {
      if (c.kind === 'or') items.push(...c.items);
      else if (c.kind === 'neg') {
        items.push(...c.items);
        warn.push(`Nhóm loại trừ ${chipToString(c)} trở thành các tag bao gồm: post chỉ cần có một trong ${c.items.join(', ')}.`);
      } else if (c.exclude) {
        items.push(`-${c.text}`);
        warn.push(c.kind === 'meta' ? `Meta tag phủ định -${c.text} không dùng được trong nhóm OR (truy vấn sẽ không hợp lệ).`
          : `-${c.text} trong nhóm OR nghĩa là post không có ${c.text} cũng khớp: kết quả rộng hơn rất nhiều.`);
      } else items.push(c.text);
    }
  } else {
    let excluded = 0;
    for (const c of sel) {
      if (c.kind === 'neg') { items.push(...c.items); excluded++; } else if (c.kind === 'or') {
        items.push(...c.items);
        warn.push(`Nhóm OR ${chipToString(c)} trong nhóm loại trừ thành điều kiện “có đủ tất cả”, không còn là “một trong”.`);
      } else {
        items.push(c.text);
        if (c.exclude) excluded++;
        else warn.push(`${c.text} đang là tag bắt buộc, sẽ đổi thành tag bị loại trừ (trong nhóm).`);
        if (c.kind === 'meta') warn.push(`Meta tag ${c.text} trong nhóm loại trừ chỉ được so với tên tag của post nên gần như không bao giờ khớp.`);
      }
    }
    if (excluded) warn.push('Trước: mỗi tag/nhóm bị loại trừ bỏ mọi post khớp với nó. Sau: chỉ bỏ post có đủ tất cả tag trong nhóm mới, nên kết quả rộng hơn.');
  }
  const uniq = [...new Set(items)];
  if (uniq.length < 2) return { error: 'Chọn ít nhất 2 tag để gộp nhóm' };
  const idx = S.chips.findIndex((c) => S.selected.has(c.id));
  const chips = S.chips.filter((c) => !S.selected.has(c.id));
  chips.splice(Math.max(0, idx), 0, { kind: act, items: uniq, id: ++S.chipSeq });
  return { chips, warn };
}
function confirmQueryChange(title, chips, warn) {
  return new Promise((resolve) => {
    const done = (ok) => { closeModal(); resolve(ok); };
    openModal(h('div', { class: 'qchange' },
      h('h2', { text: title }),
      h('div', { class: 'qc-row' }, h('span', { class: 'muted' }, 'Trước'), h('code', {}, queryString())),
      h('div', { class: 'qc-row' }, h('span', { class: 'muted' }, 'Sau'), h('code', {}, chipsToString(chips))),
      h('ul', { class: 'qc-warn' }, warn.map((w) => h('li', {}, w))),
      h('div', { class: 'modal-actions' },
        h('button', { class: 'btn ghost', onclick: () => done(false) }, 'Huỷ'),
        h('button', { class: 'btn primary', onclick: () => done(true) }, 'Vẫn gộp'))));
    $('#modal-card .btn.primary')?.focus();
  });
}

$('#chip-actions').addEventListener('click', async (e) => {
  const act = e.target.closest('button')?.dataset.act;
  if (!act) return;
  const sel = S.chips.filter((c) => S.selected.has(c.id));
  if (act === 'clear') { S.selected.clear(); renderChips(); return; }
  if (act === 'remove') { setChips(S.chips.filter((c) => !S.selected.has(c.id))); return; }
  if (act === 'toggle') {
    const why = sel.map(toggleChipExclude).filter(Boolean);
    if (why.length) toast([...new Set(why)].join('\n'), 'err');
    renderChips();
    onQueryChanged();
    return;
  }
  if (act === 'or' || act === 'neg') {
    const plan = planGroup(act);
    if (plan.error) { toast(plan.error, 'err'); return; }
    if (plan.warn.length && !(await confirmQueryChange('Gộp nhóm sẽ đổi nghĩa truy vấn', plan.chips, plan.warn))) return;
    setChips(plan.chips);
    return;
  }
  if (act === 'split') {
    const out = [];
    for (const c of S.chips) {
      if (S.selected.has(c.id) && (c.kind === 'or' || c.kind === 'neg')) {
        c.items.forEach((it) => { const p = parseToken(c.kind === 'neg' ? `-${it}` : it); if (p) out.push({ ...p, id: ++S.chipSeq }); });
      } else out.push(c);
    }
    setChips(out);
  }
});

// ---------------- autocomplete ----------------
const tagInput = $('#tag-text');
$('#tag-input').addEventListener('click', () => tagInput.focus());
tagInput.addEventListener('focus', () => $('#tag-input').classList.add('focus'));
tagInput.addEventListener('blur', () => { $('#tag-input').classList.remove('focus'); setTimeout(hideAc, 150); });

// Text starting with the token of one of the module's search kinds ('artist:ses', '-artist:ses') asks for that kind's values:
// suggestions of the kind's tag type, shown without the token and inserted with it. Any other text suggests tags of every type
const acKind = (raw) => tokenKind(raw.replace(/^-/, ''));
// what is looked up: the typed text without its '-' and without the kind's token
function acBase(raw) {
  const b = raw.replace(/^-/, '');
  const k = tokenKind(b);
  return k ? b.slice(k.token.length) : b;
}
// a suggestion's name as shown and compared with the typed text: without the kind's token ('category:x' rows of a list)
const acName = (it, k) => (k && it.tag.startsWith(k.token) ? it.tag.slice(k.token.length) : it.tag);
const acInsert = (tag, neg, k) => `${neg ? '-' : ''}${k && !tag.startsWith(k.token) ? k.token : ''}${tag}`;
function acEligible(raw) {
  if (!S.settings?.search?.autocomplete) return false;
  const k = acKind(raw);
  const base = acBase(raw);
  // ids and free words have nothing to suggest
  if (k) return k.input === 'tag' && base.length >= 2 && !/^\(|[~,]/.test(base);
  // a group being typed ("(a~b", "-(a,b") is not a single tag; tags with parentheses ("star_(s") are
  return base.length >= 2 && !metaRe().test(base) && !/^\(|[~,]/.test(base);
}
const requestAc = debounce(async () => {
  const raw = tagInput.value.trim();
  if (!acEligible(raw)) { hideAc(); return; }
  const base = acBase(raw);
  const neg = raw.startsWith('-');
  const seq = ++S.ac.seq;
  const module = S.module;
  const k = acKind(raw);
  const types = k?.tagType ? [k.tagType] : undefined;
  try {
    let items = await L.rpc('tags.autocomplete', { module, prefix: `${neg ? '-' : ''}${base}`, limit: 12, types, token: k?.token });
    if (seq !== S.ac.seq) return; // a newer keystroke (or a pick / hide) happened meanwhile
    let fuzzy = false;
    if (!items.length && base.length >= 3) {
      const near = await rpcOptional('tags.fuzzy', { module, text: base.toLowerCase(), limit: 8, types }).catch(() => null);
      if (seq !== S.ac.seq) return;
      items = near || [];
      fuzzy = items.length > 0;
    }
    if (tagInput.value.trim() !== raw || S.module !== module) return; // typed on, or another module picked (its kinds differ)
    items = items.map((it) => ({ ...it, insert: acInsert(it.tag, neg, k) }));
    Object.assign(S.ac, { items, fuzzy, index: items.length ? 0 : -1, moved: false });
    renderAc(base);
  } catch {
    if (seq === S.ac.seq) hideAc();
  }
}, 110);
// Called on every keystroke before the debounced request: answers still in flight are now stale (seq), and suggestions
// that no longer match the typed prefix disappear at once instead of lingering until the next answer.
function pruneAc() {
  S.ac.seq++;
  const raw = tagInput.value.trim();
  if ($('#ac').hidden || S.ac.fuzzy || !acEligible(raw)) { hideAc(); return; }
  const base = acBase(raw).toLowerCase();
  const neg = raw.startsWith('-');
  const k = acKind(raw);
  const keep = S.ac.items.filter((it) => acName(it, k).toLowerCase().startsWith(base)).map((it) => ({ ...it, insert: acInsert(it.tag, neg, k) }));
  if (!keep.length) { hideAc(); return; }
  Object.assign(S.ac, { items: keep, index: 0, moved: false });
  renderAc(acBase(raw));
}

function renderAc(base) {
  const box = $('#ac');
  const k = acKind(tagInput.value.trim());
  if (!S.ac.items.length) {
    const what = k ? `${kindLabel(k).toLowerCase()} nào bắt đầu bằng “${base}”` : `gợi ý cho “${base}”`;
    box.replaceChildren(h('div', { class: 'ac-hint' }, `Không có ${what} trong danh sách tag của ${S.module.toUpperCase()}. `
      + `Danh sách có thể cũ: cập nhật trong mục Tags A–Z.${k ? ' Enter vẫn thêm đúng chữ đã gõ.' : ''}`));
    box.hidden = false;
    return;
  }
  const lo = base.toLowerCase();
  const { moved, index, fuzzy } = S.ac;
  box.replaceChildren(
    ...(fuzzy ? [h('div', { class: 'ac-head' }, `Không có ${k ? kindLabel(k).toLowerCase() : 'tag'} bắt đầu bằng “${base}”. Có phải bạn muốn: …`)] : []),
    ...S.ac.items.map((it, i) => {
      const name = acName(it, k);
      const nm = !fuzzy && name.toLowerCase().startsWith(lo)
        ? h('span', { class: 'nm' }, h('b', {}, name.slice(0, lo.length)), name.slice(lo.length))
        : h('span', { class: 'nm' }, name);
      const state = i === index ? (moved ? 'on' : 'cand') : '';
      return h('div', { class: `ac-item ${fuzzy ? 'fuzzy' : ''} ${state}`, onmousedown: (e) => { e.preventDefault(); pickAc(i, e.shiftKey); } },
        h('span', { class: `type-dot t-${it.type}`, title: it.type }), nm,
        state === 'cand' ? h('kbd', { title: 'Tab để nhận gợi ý này' }, 'Tab') : null,
        h('span', { class: 'cnt' }, fmt(it.posts)));
    }),
    h('div', { class: 'ac-hint' }, moved
      ? '↑↓ chọn · Enter/Tab thêm gợi ý đang chọn · Shift+Enter thêm dạng loại trừ'
      : 'Enter thêm đúng chữ đã gõ · Tab nhận gợi ý đầu · ↑↓ chọn gợi ý khác · Shift+Enter loại trừ'));
  box.hidden = false;
}
function hideAc() {
  S.ac.seq++; // whatever is still in flight must not reopen the list
  const box = $('#ac');
  box.hidden = true;
  box.replaceChildren(); // no stale suggestion nodes left behind for the next query
  Object.assign(S.ac, { items: [], index: -1, moved: false, fuzzy: false });
}
function pickAc(i, exclude = false) {
  const it = S.ac.items[i];
  if (!it) return;
  const ins = it.insert || it.tag;
  // a kind's value, or a list row that carries a token ('category:x'): a listing is not excluded
  const why = exclude || ins.startsWith('-') ? excludeRefused(ins) : '';
  if (why) { toast(why, 'err'); return; }
  addChip(exclude && !ins.startsWith('-') ? `-${ins}` : ins);
  tagInput.value = '';
  hideAc();
}
function commitTyped(exclude = false) {
  const typed = tagInput.value.trim();
  if (!typed) return false;
  // Shift+Enter on a single typed tag adds it as an exclusion, like it does for a suggestion; a listing ('artist:x' of a site's own
  // artist list) is not excluded. A tag of a search kind is typed with its token ('user:Some.Name') and added as typed
  const single = !/\s/.test(typed) && !typed.startsWith('-') && !typed.startsWith('(');
  const why = exclude && single ? excludeRefused(typed) : '';
  if (why) { toast(why, 'err'); return true; }
  addTokens(exclude && single ? `-${typed}` : typed);
  tagInput.value = '';
  hideAc();
  return true;
}
tagInput.addEventListener('input', () => {
  const v = tagInput.value;
  if (/\s$/.test(v) && !splitQuery(v).open) { addTokens(v); tagInput.value = ''; hideAc(); return; }
  pruneAc();
  requestAc();
});
tagInput.addEventListener('keydown', (e) => {
  if (e.isComposing || e.keyCode === 229) return; // IME (Vietnamese input methods): Enter confirms the composition
  const n = S.ac.items.length;
  const acOpen = !$('#ac').hidden && n > 0;
  const base = () => acBase(tagInput.value.trim());
  if (e.key === 'ArrowDown' && acOpen) {
    e.preventDefault();
    S.ac.index = S.ac.moved ? (S.ac.index + 1) % n : 0;
    S.ac.moved = true;
    renderAc(base());
    return;
  }
  if (e.key === 'ArrowUp' && acOpen) {
    e.preventDefault();
    S.ac.index = S.ac.moved ? (S.ac.index - 1 + n) % n : n - 1;
    S.ac.moved = true;
    renderAc(base());
    return;
  }
  if (e.key === 'Tab' && acOpen && S.ac.index >= 0) { e.preventDefault(); pickAc(S.ac.index, e.shiftKey); return; }
  if (e.key === 'Enter' && acOpen) {
    // Enter adds exactly what was typed ("cat" stays "cat", never "cat_ears"), unless a suggestion was picked with the
    // arrow keys or the typed text is itself one of the suggestions
    const k = acKind(tagInput.value.trim());
    const exact = S.ac.items.findIndex((it) => acName(it, k).toLowerCase() === base().toLowerCase());
    const i = S.ac.moved && S.ac.index >= 0 ? S.ac.index : exact;
    if (i >= 0) { e.preventDefault(); pickAc(i, e.shiftKey); return; }
  }
  if (e.key === 'Enter' || (e.key === 'Tab' && tagInput.value.trim())) {
    e.preventDefault();
    if (commitTyped(e.key === 'Enter' && e.shiftKey)) return;
    if (e.key === 'Enter' && e.ctrlKey) downloadClicked();
    else if (e.key === 'Enter') runPreview(0);
    return;
  }
  if (e.key === 'Escape') { hideAc(); return; }
  if (e.key === 'Backspace' && !tagInput.value && S.chips.length) { editChip(S.chips[S.chips.length - 1]); e.preventDefault(); }
});
tagInput.addEventListener('paste', (e) => {
  const text = e.clipboardData.getData('text');
  if (looksLikeFiles(text)) { // file names / paths / post links: search by file instead of adding them as tags
    e.preventDefault();
    openFilesSearch(text.trim());
    return;
  }
  if (!/\s/.test(text.trim())) return;
  e.preventDefault();
  const v = tagInput.value;
  addTokens(v.slice(0, tagInput.selectionStart ?? v.length) + text + v.slice(tagInput.selectionEnd ?? v.length));
  tagInput.value = '';
  hideAc();
});

// ---------------- query validation / count ----------------
const validate = debounce(async () => {
  const q = queryString();
  $('#query-text').textContent = q ? `${S.module}: ${q}` : '';
  const st = $('#query-status');
  st.className = ''; st.textContent = '';
  $('#tag-input').classList.remove('invalid');
  if (!q) return;
  try {
    const r = await L.rpc('tags.validate', { module: S.module, tags: q });
    if (!r.valid) {
      $('#tag-input').classList.add('invalid');
      st.className = 'err';
      // the core says why when it knows (search kinds: a listing negated, two listings, an OR group with a listing...)
      st.textContent = r.error ? `Truy vấn không hợp lệ: ${parseErrorVi(r.error)}`
        : 'Cú pháp tag không hợp lệ (bọc tag trong %…% để bỏ qua kiểm tra)';
    }
  } catch { /* sidecar not ready */ }
}, 250);
// tags.validate 'error' (core, English) -> Vietnamese; anything else is shown as the core wrote it
const PARSE_ERRORS_VI = [
  [/^'(.+)' selects a site listing: it cannot be negated$/, (m) => `${m[1]} chọn một danh sách riêng của site, không loại trừ được`],
  [/^one site listing per search: (.+)$/, (m) => `mỗi lần tìm chỉ dùng được một danh sách riêng của site (${m[1]})`],
  [/^(.+) is a site listing: 'or' groups cannot be used with it$/, (m) => `${m[1]} là danh sách riêng của site, không dùng nhóm OR cùng nó được`],
  [/^(.+) is a site listing: favorites \/ pools cannot be used with it$/, (m) => `${m[1]} là danh sách riêng của site, không dùng chung với yêu thích / pool được`],
  [/^'(.+)': not a valid value for '(.+)'$/, (m) => `“${m[1]}” không phải giá trị hợp lệ cho ${m[2]} (bọc trong %…% để bỏ qua kiểm tra)`],
  [/^(.+) lists posts in the site's order: 'or' groups cannot be used with it$/, (m) => `${m[1]} lấy post theo thứ tự của site, không dùng nhóm OR cùng nó được`],
  [/^'(.+)': write the '-' inside the escape: '(.+)'$/, (m) => `“${m[1]}”: dấu - phải nằm trong %…% (${m[2]})`],
];
function parseErrorVi(text) {
  for (const [re, fn] of PARSE_ERRORS_VI) { const m = re.exec(String(text)); if (m) return fn(m); }
  return String(text);
}
function onQueryChanged() {
  S.countSeq++; // a count still running belongs to the previous query
  validate();
  renderDateFields(); // a kind in the site's order turns the date limits off
  if (multi.size) renderMultiSites(); // a kind another picked site does not have
  markRelatedStale();
  if (!queryString()) clearPreview(); // no tag left: the posts of the previous query must not stay on screen
  else markPreviewStale();
}

// ----- preview of another query: emptied when the query is, marked stale when it changed (until the next Preview) -----
const RESULTS_HINT = $('#results-info').innerHTML;
// everything a preview answer depends on: posts fetched with other tags, site, dates or content filters are not this query's
function previewKey(q = currentQuery()) {
  const g = q.gender || {};
  const gender = g.select?.length ? [g.op, [...g.select].sort(), g.conf, !!g.strict, g.mode] : []; // no group picked: filter off
  return JSON.stringify([q.module, q.tags, q.dateMin || '', q.dateMax || '', gender, !!q.safetyBlacklist]);
}
// the posts on screen answer another query or filters, or the last Preview failed (the grid is an older answer)
function previewStale() {
  const P = S.preview;
  return P.items.length > 0 && (P.failed || P.key !== previewKey());
}
// the posts on screen were fetched for the query and filters now in the form (a later request may have failed)
const previewOfThisQuery = () => S.preview.items.length > 0 && S.preview.key === previewKey();
function markPreviewStale() {
  if (!S.preview.items.length) return;
  $('#grid').classList.toggle('stale', previewStale());
  if (previewStale() && !$('#results-info').dataset.stale) {
    $('#results-info').dataset.stale = '1';
    $('#results-info').append(h('span', { class: 'stale-note' }, S.preview.failed ? ' · các post bên dưới là kết quả cũ'
      : ' · kết quả của truy vấn hoặc bộ lọc trước: bấm Preview để xem lại'));
  } else if (!previewStale() && $('#results-info').dataset.stale) renderGrid();
}
// date fields and the safety check box change what a preview shows (the gender filter: saveGender)
['#f-datemin', '#f-datemax'].forEach((id) => ['input', 'change'].forEach((ev) => $(id).addEventListener(ev, markPreviewStale)));
$('#f-safety').addEventListener('change', markPreviewStale);
function clearPreview() {
  const P = S.preview;
  P.seq++; // an answer still on its way belongs to the cleared query
  Object.assign(P, { page: 0, items: [], tags: '', site: '', module: '', key: '', failed: false, required: [], tasks: [], hasMore: true,
    localUnchecked: false, query: null, pageSize: 0 });
  P.selected.clear();
  gridCards.clear();
  $('#grid').replaceChildren();
  $('#grid').classList.remove('stale');
  $('#grid-empty').hidden = true;
  $('#kind-filter').hidden = true;
  $('#results-tools').hidden = true;
  delete $('#results-info').dataset.stale;
  $('#results-info').innerHTML = RESULTS_HINT;
  const btn = $('#btn-preview');
  btn.disabled = false;
  btn.textContent = 'Preview';
  updateSelButton();
}

function currentQuery() {
  return {
    module: S.module, tags: queryString(),
    dateMin: dateValue('#f-datemin'), dateMax: dateValue('#f-datemax'), // none for a kind in the site's order
    gender: genderState(),
    safetyBlacklist: $('#f-safety').checked, // -safety_blacklist: its own filter, never implied by the gender filter
  };
}
// closed filters panel: one line summary of what differs from "everything" (media, dates, limit, gender...)
function filtersSummary() {
  const o = downloadOptions();
  const q = { dateMin: dateValue('#f-datemin'), dateMax: dateValue('#f-datemax') };
  const media = [o.images && 'ảnh', o.videos && 'video', !$('#f-audio-wrap').hidden && o.audio && 'âm thanh'].filter(Boolean);
  const g = genderState();
  const fmt = (d) => d.split('-').reverse().join('/');
  const parts = [
    media.length ? media.join(' + ') : 'không tải loại nào',
    q.dateMin || q.dateMax ? `${q.dateMin ? fmt(q.dateMin) : '…'} → ${q.dateMax ? fmt(q.dateMax) : '…'}` : '',
    o.limit ? `${o.limit} post ${o.reverse ? 'mới' : 'cũ'} nhất` : '', // the limit keeps the first posts in the chosen order
    `${o.threads} luồng`,
    g.select.length ? `giới tính ${g.op === 'exclude' ? 'bỏ' : 'giữ'}: ${genderPicked(g)}` : '',
    g.select.length && g.mode === 'model' && g.verifyMedia && gmOn() ? 'xem lại video sau khi tải' : '',
    o.skipKnown ? 'bỏ file đã tải' : '',
    $('#f-safety').checked ? 'chặn nội dung gợi ý trẻ vị thành niên' : '',
    o.maxVideoHeight && !$('#f-vres-wrap').hidden ? `video ≤ ${o.maxVideoHeight}p` : '',
    o.lowRes ? 'ảnh nhẹ' : '',
    multi.size ? `+${multi.size} site` : '',
  ].filter(Boolean);
  $('#filters-sum').textContent = `· ${parts.join(' · ')}`;
}
$('#filters').addEventListener('toggle', () => {
  filtersSummary();
  if (S.settings && S.settings.search.filtersOpen !== $('#filters').open) saveSettings({ search: { filtersOpen: $('#filters').open } });
});
$('#filters').addEventListener('change', filtersSummary);
// the safety blacklist check box is remembered (download.safetyBlacklist) and sent with each query
$('#f-safety').addEventListener('change', (e) => saveSettings({ download: { safetyBlacklist: e.target.checked } }));
$('#filters').addEventListener('click', () => requestAnimationFrame(filtersSummary)); // gender pills are buttons
// the quality choices are remembered (settings.download) and shown again at the next start
$('#f-vformat').addEventListener('change', (e) => saveSettings({ download: { videoFormat: e.target.value } }));
$('#f-vres').addEventListener('change', (e) => saveSettings({ download: { maxVideoHeight: Number(e.target.value) || 0 } }));
$('#f-lowres').addEventListener('change', (e) => saveSettings({ download: { lowRes: e.target.value === '1' } }));

function downloadOptions() {
  return {
    images: $('#f-images').checked, videos: $('#f-videos').checked, audio: $('#f-audio').checked,
    // a remembered format is not sent to a module without the choice (the core would warn and wait on every job)
    videoFormat: $('#f-vformat-wrap').hidden ? '' : $('#f-vformat').value,
    // images only ("Bản nhẹ"); the video file is picked by height (maxVideoHeight, -max_vres)
    // the remembered height only while its field is shown: a hidden one would still cap the extra sites of a multi-site group
    lowRes: $('#f-lowres').value === '1', maxVideoHeight: $('#f-vres-wrap').hidden ? 0 : Number($('#f-vres').value) || 0,
    threads: Number($('#f-threads').value) || 4, limit: Number($('#f-limit').value) || 0, reverse: $('#f-order').value === 'desc',
    parchi: $('#f-parchi').checked, skipKnown: $('#f-skip-known').checked,
  };
}

$('#btn-count').addEventListener('click', async () => {
  const q = currentQuery();
  if (!q.tags) { toast('Chưa có tag nào', 'err'); return; }
  if (!requireKey(q.module)) return;
  recordHistory(q);
  const seq = ++S.countSeq;
  const btn = $('#btn-count');
  btn.disabled = true; btn.textContent = 'Đang đếm…';
  try {
    const r = await L.search.count(q);
    if (seq !== S.countSeq) return; // the query changed (or a newer count started) meanwhile
    const st = $('#query-status');
    st.className = r.count > 0 ? 'ok' : 'err';
    const parts = r.taskCounts?.length > 1 ? ` (tổng ${r.taskCounts.length} truy vấn con, có thể trùng)` : '';
    // dateFilter 'local': the site cannot search by date, its count includes every date (the download filters them)
    const dates = { local: ' (chưa trừ post ngoài khoảng ngày: site không đếm theo ngày)', unsupported: ` (${q.module} không lọc được theo ngày)` };
    st.textContent = `${r.approximate ? '≤ ' : ''}${nf.format(r.count)} kết quả${parts}`
      + `${r.requiredTags?.length ? ` (trước khi lọc cục bộ: ${r.requiredTags.join(', ')})` : ''}${dates[r.dateFilter] || ''}`;
    if (r.cfChallenge) {
      if (L.site?.verify) {
        toastAction('Website đang chặn bằng bot challenge (Cloudflare): số đếm có thể sai.', 'Xác minh trong Lynxx',
          () => verifySite(q.module, ['Đếm lại', () => $('#btn-count').click()]), 15000, 'err');
      } else toast('Website đang chặn bằng bot challenge. Xem mục Cài đặt → Kết nối (cookies theo site).', 'err');
    }
  } catch (err) {
    if (seq === S.countSeq) toast(`Đếm thất bại: ${errMsg(err)}`, 'err');
  } finally {
    btn.disabled = false; btn.textContent = 'Đếm';
  }
});

// ---------------- preview ----------------
// "max" asks for the module's own page size (the sidecar caps pageSize to it, e.g. rx 1000, db 200)
const PAGE_SIZE_MAX = 100000;
function previewPageSize() {
  const v = $('#pv-size').value;
  return v === 'max' ? PAGE_SIZE_MAX : Number(v) || 200;
}
async function runPreview(page = 0) {
  const q = { ...currentQuery(), pageSize: previewPageSize() };
  if (!q.tags) { toast('Chưa có tag nào', 'err'); return; }
  if (!requireKey(q.module)) return;
  recordHistory(q);
  // every request gets a sequence number: an older answer arriving late never replaces a newer one
  const seq = ++S.preview.seq;
  const btn = $('#btn-preview');
  btn.disabled = true; btn.textContent = 'Đang tải…';
  $('#results-info').textContent = 'Đang lấy kết quả…';
  try {
    const r = await L.search.preview(q, page);
    if (seq !== S.preview.seq) return;
    Object.assign(S.preview, { page, items: r.items, site: r.site, module: q.module, tags: q.tags, query: q, key: previewKey(q), failed: false, required: r.requiredTags || [], tasks: r.tasks || [],
      genderStats: r.genderStats || {}, blockedCount: r.blockedCount || 0, hiddenNoTags: r.hiddenNoTags || 0, genderEnabled: !!r.genderEnabled, genderModel: r.genderModel || {},
      pageSize: r.pageSize || 0, hasMore: r.hasMore !== false, merged: typeof r.hasMore === 'boolean', dateFilter: r.dateFilter || '',
      localUnchecked: r.localFiltersUnchecked === true });
    S.preview.selected.clear();
    renderGrid();
  } catch (err) {
    if (seq !== S.preview.seq) return;
    previewFailed(err, q.module, page);
  } finally {
    if (seq === S.preview.seq) { btn.disabled = false; btn.textContent = 'Preview'; }
  }
}
// a site behind a bot challenge (Cloudflare): Lynxx never passes it itself. The user does, in a window of the app ("Xác minh trong
// Lynxx", verifySite) or in their own browser (cookie and User-Agent copied to Settings → Kết nối)
const CHALLENGE_HELP = 'Preview thất bại: site đang chặn bằng bot challenge (Cloudflare), Lynxx không tự vượt qua. Bấm “Xác minh trong '
  + 'Lynxx” để tự qua bước kiểm tra trong một cửa sổ của app (cookie và User-Agent được lưu cho riêng site này), hoặc qua bước kiểm tra '
  + 'trong trình duyệt rồi chép cookie cf_clearance và User-Agent của trình duyệt đó vào Cài đặt → Kết nối, mục theo site (mã site như rn).';
function previewFailed(err, module, page = 0) {
  // the grid still on screen is not the answer to this request: dimmed until a Preview works. Its picked posts stay picked (still
  // posts of this query when only a page turn failed: "Tải xuống" asks about them, see downloadClicked)
  S.preview.failed = true;
  $('#results-info').textContent = `Lỗi: ${errMsg(err)}`;
  delete $('#results-info').dataset.stale;
  markPreviewStale();
  if (!/bot challenge/i.test(errMsg(err))) toast(`Preview thất bại: ${errMsg(err)}`, 'err');
  else if (L.site?.verify && module) {
    toastAction(CHALLENGE_HELP, 'Xác minh trong Lynxx', () => verifySite(module, ['Preview lại', () => runPreview(page)]), 20000, 'err');
  } else toast(CHALLENGE_HELP, 'err', 15000);
}
$('#btn-preview').addEventListener('click', () => runPreview(0));
$('#pg-prev').addEventListener('click', () => { if (S.preview.page > 0) runPreview(S.preview.page - 1); });
$('#pg-next').addEventListener('click', () => { if (S.preview.hasMore) runPreview(S.preview.page + 1); });
$('#pv-size').addEventListener('change', () => { if (S.preview.tags && S.preview.tags === queryString()) runPreview(0); });
$('#show-excluded').addEventListener('change', (e) => { S.preview.showExcluded = e.target.checked; renderGrid(); });

// posts of the page shown in the grid: local filters (unless shown) and the kind chip (image / video / audio)
function visibleItems() {
  const P = S.preview;
  return P.items.filter((it) => (P.showExcluded || !it.excluded) && (P.kind === 'all' || (it.mediaKind || (it.isVideo ? 'video' : 'image')) === P.kind));
}
const KIND_VI = { all: 'Tất cả', image: 'Ảnh', video: 'Video', audio: 'Âm thanh' };
function renderKindFilter() {
  const P = S.preview;
  const counts = { image: 0, video: 0, audio: 0 };
  for (const it of P.items) if (P.showExcluded || !it.excluded) counts[it.mediaKind || (it.isVideo ? 'video' : 'image')] += 1;
  const kinds = Object.keys(counts).filter((k) => counts[k] > 0);
  if (P.kind !== 'all' && !counts[P.kind]) P.kind = 'all';
  const box = $('#kind-filter');
  box.hidden = kinds.length < 2; // one kind only: nothing to choose
  box.replaceChildren(...['all', ...kinds].map((k) => h('button', {
    class: P.kind === k ? 'on' : '', title: `Chỉ hiện ${KIND_VI[k].toLowerCase()} trên trang này`,
    onclick: () => { P.kind = k; renderGrid(); },
  }, k === 'all' ? KIND_VI.all : `${KIND_VI[k]} ${counts[k]}`)));
}
function renderGrid() {
  const P = S.preview;
  renderKindFilter();
  const items = visibleItems();
  const hidden = P.items.length - items.length;
  // merged multi-task pages hold up to pageSize posts of every sub-query
  const size = !P.pageSize ? '' : P.merged && P.tasks?.length > 1 ? ` (${P.pageSize}/trang mỗi truy vấn con)` : ` (tối đa ${P.pageSize}/trang)`;
  const parts = [`Trang ${P.page + 1}: ${P.items.length} post${size}`];
  const by = (reason) => P.items.filter((it) => (it.excludedBy || []).includes(reason)).length;
  const byGender = by('gender');
  const byDate = by('date');
  const byOther = P.items.filter((it) => it.excluded && !(it.excludedBy || []).some((x) => REASON_VI[x])).length;
  if (byOther) parts.push(`${byOther} bị lọc cục bộ`);
  // dates the site cannot search by (rule34 and most others; Danbooru can): each page hides its posts outside them, the
  // download finds the pages by date
  if (byDate) parts.push(`${byDate} post ngoài khoảng ngày bị ${P.showExcluded ? 'làm mờ' : 'ẩn'}`);
  if (P.dateFilter === 'unchecked') parts.push('site không trả ngày đăng trong danh sách: preview chưa lọc theo ngày (tải xuống vẫn lọc)');
  if (P.dateFilter === 'unsupported') parts.push(`${P.module} không lọc được theo ngày: khoảng ngày bị bỏ qua`);
  if (P.genderEnabled) {
    parts.push(`${byGender} loại theo giới tính`);
    const unknown = P.genderStats?.unknown || 0;
    if (unknown) parts.push(`${unknown} không rõ`);
    if (P.blockedCount) parts.push(`${P.blockedCount} bị chặn (an toàn)`);
    if (P.hiddenNoTags) parts.push(`${P.hiddenNoTags} ẩn vì site không trả tag để kiểm tra`);
    const gm = P.genderModel || {};
    if (gm.error) parts.push(`model: ${gm.error}`);
    else if (gm.checked) parts.push(`model xem ${gm.checked} post chưa rõ, gán nhãn ${gm.labeled}${gm.skipped ? `, ${gm.skipped} post còn lại chưa xem` : ''}`);
  }
  if (hidden) parts.push('post bị lọc đang ẩn');
  if (P.required?.length) parts.push(`tag kiểm tra cục bộ: ${P.required.join(', ')}`);
  // the site's listing gives no post tags (the module reads them from each post's page while downloading)
  if (P.localUnchecked) parts.push('chưa lọc cục bộ trong Preview: danh sách của site không có tag của post, Tải xuống vẫn lọc');
  if (P.tasks?.length > 1) {
    parts.push(P.merged ? `gộp ${P.tasks.length} truy vấn con (đã khử trùng, id giảm dần)` : `truy vấn tách thành ${P.tasks.length} task, preview hiển thị task đầu`);
  }
  if (!P.hasMore) parts.push('trang cuối');
  $('#results-info').textContent = parts.join(' · ');
  delete $('#results-info').dataset.stale;
  $('#grid').classList.remove('stale');
  markPreviewStale(); // the query may have changed since these posts were fetched
  $('#results-tools').hidden = false;
  $('#pg-num').textContent = String(P.page + 1);
  $('#pg-prev').disabled = P.page === 0;
  $('#pg-next').disabled = !P.hasMore;
  $('#pg-next').title = P.hasMore ? 'Trang sau' : 'Không còn trang sau';
  $('#grid-empty').hidden = items.length > 0;
  $('#grid-empty').textContent = byDate && byDate === P.items.length
    ? 'Mọi post trên trang này nằm ngoài khoảng ngày. Site này không tìm theo ngày nên Preview chỉ lọc từng trang; Tải xuống tự tìm đúng các trang.'
    : 'Không có post nào trên trang này.';
  gridCards.clear();
  $('#grid').replaceChildren(...items.map((it) => {
    const audio = it.mediaKind === 'audio';
    const kind = audio ? 'audio' : it.isVideo ? 'video' : 'image';
    const img = it.preview
      ? h('img', { loading: 'lazy', alt: '', src: L.thumbUrl(it.preview, P.site, P.module), onerror: (e) => e.target.replaceWith(thumbPlaceholder(kind, true)) })
      : audio ? h('div', { class: 'audio-ph', 'aria-hidden': 'true' }, '♪') : thumbPlaceholder(kind, false);
    const g = it.gender;
    const why = [...new Set((it.excludedBy || []).map((x) => REASON_VI[x] || 'lọc cục bộ'))].join(', ');
    const gTitle = g ? `Giới tính: ${GENDER_VI[g.label] || g.label}${g.conf ? ` · tin cậy ${CONF_VI[g.conf]}` : ''}`
      + `${g.evidence?.length ? ` (${g.evidence.join(', ')})` : ''}${g.flags?.length ? ` · cờ: ${g.flags.join(', ')}` : ''}`
      + `${g.source === 'model' ? ' · từ ảnh (model)' : g.source === 'tags+model' ? ' · tag + ảnh (model)' : ''}${g.modelSkipped ? ' · model chưa xem' : ''}\n` : '';
    const card = h('div', {
      class: `card ${P.selected.has(it.id) ? 'sel' : ''} ${it.excluded ? 'excluded' : ''}`, 'data-why': why,
      title: gTitle + (it.tags || []).slice(0, 40).join(' '),
      onclick: () => toggleSel(it.id),
      ondblclick: () => openLightbox(it),
    }, img, h('span', { class: 'tick' }, '✓'),
    h('div', { class: 'badges' },
      it.isVideo ? h('span', { class: 'b vid' }, '▶ video') : null,
      audio ? h('span', { class: 'b vid' }, '♪ âm thanh') : null,
      it.downloaded ? h('span', { class: 'b done', title: it.downloaded === 'md5' ? 'Đã tải (cùng file, có thể từ site khác)' : 'Đã tải' }, '✓ đã tải') : null,
      it.ext ? h('span', { class: 'b' }, it.ext) : null,
      it.score !== undefined && it.score !== '' ? h('span', { class: 'b' }, `★ ${it.score}`) : null,
      it.rating ? h('span', { class: 'b' }, it.rating) : null,
      g ? h('span', { class: `b g ${g.conf || 'low'}` }, GENDER_VI[g.label] || '?') : null));
    const same = gridCards.get(it.id);
    if (same) same.push(card); else gridCards.set(it.id, [card]);
    return card;
  }));
  updateSelButton();
}
// post id -> its card(s) in the current grid: selection changes touch only those nodes, never rebuild the grid
const gridCards = new Map();
// a post without a preview address, or whose preview did not load (the site or its CDN refused it): a neutral tile in the
// theme's colors instead of the browser's broken image icon. The card stays clickable and selectable
const THUMB_ICONS = {
  image: '<svg viewBox="0 0 24 24"><rect x="3" y="4" width="18" height="16" rx="2"/><path d="m3 16 5-5 4 4 3-3 6 6"/><circle cx="15.5" cy="8.5" r="1.5"/></svg>',
  video: '<svg viewBox="0 0 24 24"><rect x="3" y="4" width="18" height="16" rx="2"/><path d="M7 4v16M17 4v16M3 9h4m10 0h4M3 15h4m10 0h4"/></svg>',
  audio: '<svg viewBox="0 0 24 24"><path d="M9 18V5l11-2v13"/><circle cx="6" cy="18" r="3"/><circle cx="17" cy="16" r="3"/></svg>',
};
function thumbPlaceholder(kind, failed) {
  const icon = h('span', { class: 'thumb-ph-icon', 'aria-hidden': 'true' });
  icon.innerHTML = THUMB_ICONS[kind] || THUMB_ICONS.image; // static markup above, nothing from the site
  return h('div', { class: 'thumb-ph', dataset: { kind } }, icon,
    h('span', {}, failed ? 'không tải được ảnh xem trước' : 'không có ảnh xem trước'));
}
function toggleSel(id) {
  const s = S.preview.selected;
  if (s.has(id)) s.delete(id); else s.add(id);
  gridCards.get(id)?.forEach((card) => card.classList.toggle('sel', s.has(id)));
  updateSelButton();
}
function syncSelection() {
  // bulk change: without .no-anim every card would start its own border-color transition (~3x the cost)
  const grid = $('#grid');
  grid.classList.add('no-anim');
  const s = S.preview.selected;
  for (const [id, cards] of gridCards) for (const card of cards) card.classList.toggle('sel', s.has(id));
  updateSelButton();
  onFrame(() => onFrame(() => grid.classList.remove('no-anim')));
}
// "Tải đã chọn": an 'id:' query where the site searches by id, else the job reads the Preview's page again and keeps the picked
// posts (pick mode: needs the request of the page on screen and the page size the Preview reported)
const PICK_IDS_MAX = 1000;
const PICK_OFF = 'Site này không tìm được theo id post và Preview không báo cỡ trang: không tải riêng các post đã chọn được. Dùng Tải xuống để tải cả truy vấn.';
const pickOk = () => S.preview.pageSize > 0 && Boolean(S.preview.query);
const selectedOk = (abbr) => idSearchOk(abbr) || pickOk();
function updateSelButton() {
  const n = S.preview.selected.size;
  const b = $('#dl-selected');
  const ok = selectedOk(S.preview.module || S.module);
  b.disabled = n === 0 || !ok;
  b.title = n > 0 && !ok ? PICK_OFF : ''; // nothing picked yet: nothing to explain
  b.textContent = `Tải đã chọn (${n})`;
}
$('#sel-all').addEventListener('click', () => { visibleItems().filter((i) => !i.excluded).forEach((i) => S.preview.selected.add(i.id)); syncSelection(); });
$('#sel-none').addEventListener('click', () => { S.preview.selected.clear(); syncSelection(); });
$('#sel-invert').addEventListener('click', () => {
  S.preview.items.filter((i) => !i.excluded).forEach((i) => (S.preview.selected.has(i.id) ? S.preview.selected.delete(i.id) : S.preview.selected.add(i.id)));
  syncSelection();
});
// "Tải đã chọn" downloads exactly the picked posts: the post limit, dates and gender filter of the filters panel would drop some
// of them (limit 1 with two picked posts downloaded one), so they are left out. Media types and file options still apply
function downloadSelected() {
  const ids = [...S.preview.selected];
  if (!ids.length) return;
  const module = S.preview.module;
  const label = `${ids.length} post đã chọn từ: ${S.preview.tags}`;
  if (!idSearchOk(module)) {
    // no id search: the Preview's own request (its tags, dates, safety filter; not the form, which may have changed since) and
    // the page on screen; the core reads that page again and keeps these posts, its date / gender / limit filters are not applied
    if (!pickOk()) { toast(PICK_OFF, 'err'); return; }
    // a query of several sub-queries (an OR group the site cannot search) merges page N of each one: up to pageSize posts each
    const max = Math.min(PICK_IDS_MAX, S.preview.pageSize * Math.max(1, (S.preview.tasks || []).length));
    if (ids.length > max) { toast(`Tối đa ${max} post mỗi lần với truy vấn này`, 'err'); return; }
    const { pageSize, ...q } = S.preview.query;
    startDownload({ ...q, label, gender: { ...(q.gender || genderState()), select: [] }, options: { limit: 0 },
      pick: { page: S.preview.page, size: S.preview.pageSize, ids } });
    return;
  }
  // danbooru understands id:1,2,3 natively (free metatag); other modules use an OR group of ids (one request per id)
  let tags;
  if (module === 'db') tags = `id:${ids.join(',')}`;
  else if (ids.length === 1) tags = `id:${ids[0]}`;
  else {
    if (ids.length > 200) { toast('Tối đa 200 post mỗi lần với module này', 'err'); return; }
    tags = `(${ids.map((i) => `id:${i}`).join('~')})`;
  }
  startDownload({ module, tags, label, dateMin: '', dateMax: '', gender: { ...genderState(), select: [] }, options: { limit: 0 } });
}
$('#dl-selected').addEventListener('click', downloadSelected);
// "Tải xuống" (and Ctrl+Enter) with posts picked in the preview on screen: the user may mean those posts, not the whole query
// (whose first post, in ascending order, is its oldest one): ask. Posts of another query or filters are not asked about: the query
// is what changed. A failed page turn still asks: the grid on screen is a page of this query
function downloadClicked() {
  const n = S.preview.selected.size;
  // picked posts that cannot be downloaded on their own (no id search, no page size from the Preview): only the whole query
  if (!n || !previewOfThisQuery() || !selectedOk(S.preview.module)) { startDownload(); return; }
  const done = (pick) => { closeModal(); if (pick === 'selected') downloadSelected(); else if (pick === 'query') startDownload(); };
  openModal(h('div', { class: 'qchange dl-ask' },
    h('h2', { text: 'Tải những gì?' }),
    h('p', { class: 'muted' }, `Bạn đang chọn ${n} post trong Preview. Tải cả truy vấn sẽ tải mọi post khớp với tag và bộ lọc.`),
    h('div', { class: 'modal-actions' },
      h('button', { class: 'btn ghost', onclick: () => done(null) }, 'Huỷ'),
      h('button', { class: 'btn', id: 'dl-ask-query', onclick: () => done('query') }, 'Tải cả truy vấn'),
      h('button', { class: 'btn primary', id: 'dl-ask-selected', onclick: () => done('selected') }, `Tải ${n} post đã chọn`))));
  $('#dl-ask-selected')?.focus();
}

function lightboxMedia(it) {
  // video / audio play in the app (streamed through main, see streamMedia); images show the sample
  const kind = it.mediaKind || (it.isVideo ? 'video' : 'image');
  const site = S.preview.site;
  const abbr = S.preview.module; // its per-site headers / cookies reach its own website (Settings → Kết nối)
  const media = it.file || it.sample;
  if (kind === 'video' && media && L.mediaUrl) {
    return h('video', { controls: true, autoplay: true, preload: 'metadata', poster: it.preview ? L.thumbUrl(it.preview, site, abbr) : null,
      src: L.mediaUrl(media, site, abbr) });
  }
  if (kind === 'audio' && media && L.mediaUrl) {
    return h('div', { class: 'cover' },
      it.preview ? h('img', { src: L.thumbUrl(it.preview, site, abbr), alt: '' }) : h('div', { class: 'audio-ph' }, '♪'),
      h('audio', { controls: true, autoplay: true, preload: 'metadata', src: L.mediaUrl(media, site, abbr) }));
  }
  const src = it.sample && !it.isVideo ? it.sample : it.preview;
  return src ? h('img', { src: L.thumbUrl(src, site, abbr), alt: '', onerror: (e) => e.target.replaceWith(thumbPlaceholder(kind, true)) })
    : thumbPlaceholder(kind, false);
}
function openLightbox(it) {
  const addTag = (t, ex) => { addChip(ex ? `-${t}` : t); toast(`Đã thêm ${ex ? '−' : ''}${t}`); };
  const link = POST_URL[S.preview.module]?.(it.id);
  openModal(h('div', { class: 'lightbox' },
    h('div', {}, lightboxMedia(it)),
    h('div', {},
      h('h2', {}, `#${it.id} `, h('span', { class: 'muted' }, [it.ext, it.width && `${it.width}×${it.height}`, it.date].filter(Boolean).join(' · '))),
      link ? h('p', {}, h('a', { href: '#', onclick: (e) => { e.preventDefault(); L.app.openExternal(link); } }, 'Mở post trên website ↗')) : null,
      it.gender ? h('p', {}, h('b', {}, `Giới tính: ${GENDER_VI[it.gender.label] || it.gender.label}`),
        h('span', { class: 'muted' }, ` · tin cậy ${CONF_VI[it.gender.conf] || '—'} · từ tag`
          + `${it.gender.evidence?.length ? ` (${it.gender.evidence.join(', ')})` : ''}${it.gender.flags?.length ? ` · cờ: ${it.gender.flags.join(', ')}` : ''}`)) : null,
      h('p', { class: 'muted' }, 'Bấm + để thêm tag vào truy vấn, − để loại trừ.'),
      h('div', { class: 'lb-tags' }, (it.tags || []).map((t) => h('span', { class: 'lb-tag' }, t,
        h('button', { title: 'Thêm', onclick: () => addTag(t, false) }, '+'), h('button', { title: 'Loại trừ', onclick: () => addTag(t, true) }, '−')))),
      h('div', { class: 'modal-actions' },
        h('button', { class: 'btn', onclick: () => { toggleSel(it.id); closeModal(); } }, S.preview.selected.has(it.id) ? 'Bỏ chọn' : 'Chọn'),
        h('button', { class: 'btn ghost', onclick: closeModal }, 'Đóng')))));
}

// ---------------- library: offline search of the download index (index.search, no website request) ----------------
// view 'trash': the files deleted from the library (moved to the trash by the core, restorable until purged).
// selected: paths of the cards picked on this page (click, shift+click for a range); anchor: index of the last plain click
const LIB = { page: 0, pageSize: 60, total: 0, items: [], seq: 0, ready: false, view: 'library', selected: new Set(), anchor: -1,
  cards: new Map(), trash: null };
const LIB_KIND = (ext) => (['mp4', 'webm'].includes(ext) ? 'video' : ['mp3', 'ogg', 'oga', 'opus', 'flac', 'm4a', 'wav'].includes(ext) ? 'audio' : 'image');
function fileUrl(p) {
  // local file for <img> / <video> (the renderer is a file:// page, CSP allows file:)
  const norm = String(p).replace(/\\/g, '/');
  return `file://${norm.startsWith('/') ? '' : '/'}${norm.split('/').map((seg) => (/^[A-Za-z]:$/.test(seg) ? seg : encodeURIComponent(seg))).join('/')}`;
}
async function openLibrary() {
  if (!LIB.ready) {
    LIB.ready = true;
    const mods = S.modules.filter((m) => m.installed);
    $('#lib-module').replaceChildren(h('option', { value: '' }, 'Mọi module'), ...mods.map((m) => h('option', { value: m.abbr }, `${m.abbr} · ${m.name}`)));
    runLibrary(0);
  }
  refreshLibraryStats();
}
async function refreshLibraryStats() {
  try {
    const st = await L.rpc('index.stats');
    LIB.trash = st.trash || { files: 0, bytes: 0, oldest: null };
    const t = LIB.trash;
    setText($('#lib-stats'), !st.exists ? 'chưa có file nào trong index'
      : LIB.view === 'trash' ? `${nf.format(t.files)} file · ${fmtBytes(t.bytes)} trong thùng rác` : `${nf.format(st.files || 0)} file · ${fmtBytes(st.bytes || 0)}`);
    setText($('#lib-trash-count'), t.files ? `(${nf.format(t.files)})` : '');
    if (LIB.view === 'trash' && !LIB.total && !LIB.items.length) renderLibrary(); // "trống" or "không khớp bộ lọc" needs the count
    updateLibTools();
  } catch { /* old core: no index */ }
}
async function runLibrary(page) {
  const seq = ++LIB.seq;
  const params = { tags: $('#lib-q').value.trim(), module: $('#lib-module').value, dateFrom: $('#lib-from').value, dateTo: $('#lib-to').value,
    order: $('#lib-order').value, page, pageSize: LIB.pageSize, trash: LIB.view === 'trash' };
  try {
    const r = await L.rpc('index.search', params);
    if (seq !== LIB.seq) return;
    const last = Math.max(0, Math.ceil(r.total / LIB.pageSize) - 1);
    if (!r.items.length && r.page > last) { runLibrary(last); return; } // the last page was emptied (files deleted)
    Object.assign(LIB, { page: r.page, total: r.total, items: r.items, anchor: -1 });
    LIB.selected.clear();
    renderLibrary();
  } catch (err) {
    if (seq === LIB.seq) setText($('#lib-info'), `Không tìm được: ${errMsg(err)}`);
  }
}
const trashDays = () => S.settings?.library?.trashDays ?? 30;
function trashLeft(it) {
  // days before the automatic purge (settings.library.trashDays, main.js purgeOldTrash); '' when the trash is never emptied
  const days = trashDays();
  if (!days || !it.trashedAt) return '';
  const left = Math.ceil(days - (Date.now() / 1000 - it.trashedAt) / 86400);
  return left > 0 ? `còn ${left} ngày` : 'sắp xoá';
}
function renderLibrary() {
  const trash = LIB.view === 'trash';
  const pages = Math.max(1, Math.ceil(LIB.total / LIB.pageSize));
  const missing = LIB.items.filter((it) => !it.exists).length;
  const days = trashDays();
  const inTrash = LIB.trash?.files || 0; // the whole trash (index.stats), not only what the search bar keeps
  setText($('#lib-info'), trash
    ? (LIB.total ? `${nf.format(LIB.total)} file trong thùng rác · ${days ? `tự xoá vĩnh viễn sau ${days} ngày` : 'không tự xoá'}`
      : inTrash ? `Không có file nào trong thùng rác khớp bộ lọc (thùng rác có ${nf.format(inTrash)} file).` : 'Thùng rác trống.')
    : LIB.total ? `${nf.format(LIB.total)} file${missing ? ` · ${missing} file trên trang này không còn trên đĩa` : ''}`
      : 'Không có file nào khớp. Index chỉ có những file tải từ khi có tính năng này (và khi đang bật ghi index).');
  setText($('#lib-page'), `${LIB.page + 1} / ${pages}`);
  $('#lib-prev').disabled = LIB.page <= 0;
  $('#lib-next').disabled = LIB.page + 1 >= pages;
  LIB.cards.clear();
  $('#lib-grid').replaceChildren(...LIB.items.map((it, i) => {
    const kind = LIB_KIND(String(it.ext || '').toLowerCase());
    const thumb = it.exists && kind === 'image'
      ? h('img', { loading: 'lazy', decoding: 'async', alt: '', src: fileUrl(it.path), onerror: (e) => e.target.replaceWith(h('div', { class: 'ph' }, '🖼')) })
      : h('div', { class: 'ph' }, it.exists ? (kind === 'video' ? '▶' : '♪') : '✕');
    const left = trash ? trashLeft(it) : '';
    const where = trash ? `\nVị trí cũ: ${it.origPath}` : '';
    const card = h('div', {
      class: `card ${it.exists ? '' : 'missing'}`, dataset: { path: it.path },
      title: `${it.name}${where}\n${(it.tags || '').slice(0, 400)}\n(bấm để chọn, bấm đúp để xem)`,
      onclick: (e) => libCardClick(e, i), ondblclick: () => openLibraryItem(it),
    }, thumb, h('span', { class: 'tick' }, '✓'), h('div', { class: 'badges' },
      h('span', { class: 'b' }, it.module), it.ext ? h('span', { class: 'b' }, it.ext) : null,
      it.size ? h('span', { class: 'b' }, fmtBytes(it.size)) : null, it.exists ? null : h('span', { class: 'b' }, 'đã mất'),
      left ? h('span', { class: 'b left' }, left) : null));
    LIB.cards.set(it.path, card);
    return card;
  }));
  updateLibTools();
}
function updateLibTools() {
  const trash = LIB.view === 'trash';
  const n = LIB.selected.size;
  for (const [p, card] of LIB.cards) card.classList.toggle('sel', LIB.selected.has(p));
  for (const [id, label, show] of [['#lib-delete', 'Xoá', !trash], ['#lib-restore', 'Khôi phục', trash], ['#lib-purge', 'Xoá vĩnh viễn', trash]]) {
    setHidden($(id), !show);
    setText($(id), `${label} (${n})`);
    $(id).disabled = !n;
  }
  setHidden($('#lib-empty'), !trash);
  $('#lib-empty').disabled = !LIB.trash?.files;
  $('#lib-sel-none').disabled = !n;
  $('#lib-sel-page').disabled = !LIB.items.length;
}
function libCardClick(e, i) {
  const it = LIB.items[i];
  if (!it) return;
  if (e.shiftKey && LIB.anchor >= 0) { // a range from the last clicked card
    const [a, b] = LIB.anchor < i ? [LIB.anchor, i] : [i, LIB.anchor];
    LIB.items.slice(a, b + 1).forEach((x) => LIB.selected.add(x.path));
  } else {
    if (LIB.selected.has(it.path)) LIB.selected.delete(it.path); else LIB.selected.add(it.path);
    LIB.anchor = i;
  }
  updateLibTools();
}
const libSelectedItems = () => LIB.items.filter((it) => LIB.selected.has(it.path));
function setLibraryView(view) {
  if (LIB.view === view) return;
  LIB.view = view;
  $$('#lib-view button').forEach((b) => b.classList.toggle('on', b.dataset.view === view));
  // newest deletion first in the trash, newest download first in the library (unless another order was picked)
  const order = $('#lib-order');
  order.querySelector('option[value="trashed"]').hidden = view !== 'trash';
  if (view === 'trash' && order.value === 'downloaded') order.value = 'trashed';
  if (view !== 'trash' && order.value === 'trashed') order.value = 'downloaded';
  LIB.selected.clear();
  LIB.items = [];
  renderLibrary();
  runLibrary(0);
  refreshLibraryStats();
}
function afterLibraryChange() {
  runLibrary(LIB.page);
  refreshLibraryStats();
  if ($('#page-settings').classList.contains('active')) refreshIndexStats();
}
// errors of the core (lynxx/dlindex.py) the user can act on
const LIB_ERRORS = [[/^the trash folder is not reachable/, 'không truy cập được thư mục thùng rác (ổ đã rút?)']];
const libError = (e) => LIB_ERRORS.find(([re]) => re.test(String(e || '')))?.[1] || e;
function reportLibFailed(failed, what) {
  if (!failed?.length) return;
  toast(`${what} ${failed.length} file: ${libError(failed[0].error)}${failed.length > 1 ? '…' : ''}`, 'err');
}
async function releaseMedia() {
  // a playing video keeps its file open (Windows refuses to move or delete it): stop the lightbox media before the file is touched
  const media = $$('#modal-card video, #modal-card audio');
  for (const m of media) { m.pause(); m.removeAttribute('src'); m.load(); }
  closeModal();
  if (media.length) await new Promise((r) => setTimeout(r, 200));
}
async function libDelete(items) {
  // library: files on disk go to the trash (undo from the toast); rows whose file is already gone are dropped (main asks first)
  const present = items.filter((it) => it.exists).map((it) => it.path);
  const gone = items.filter((it) => !it.exists).map((it) => it.path);
  if (present.length) {
    try {
      const r = await L.library.trash(present);
      reportLibFailed(r.failed, 'Không chuyển được vào thùng rác');
      const moved = r.trashed.map((x) => x.trashPath);
      // the toast sits above the lightbox: a lightbox opened meanwhile on one of these files (Thùng rác) is closed and its media
      // stopped first, as its own buttons do (an open video keeps its file: Windows refuses to move it)
      const undo = async () => {
        if (moved.includes($('#modal-card .lightbox')?.dataset.path)) await releaseMedia();
        libRestore(moved, true);
      };
      if (moved.length) toastAction(`Đã chuyển ${moved.length} file vào thùng rác`, 'Hoàn tác', undo);
    } catch (err) { toast(`Không xoá được: ${errMsg(err)}`, 'err'); }
  }
  if (gone.length) {
    try {
      const r = await L.library.forget(gone);
      if (r.removed) toast(`Đã bỏ ${r.removed} mục không còn file khỏi thư viện`, 'ok');
    } catch (err) { if (!/cancelled/.test(errMsg(err))) toast(`Không xoá được: ${errMsg(err)}`, 'err'); }
  }
  afterLibraryChange();
}
async function libRestore(paths, undo = false) {
  try {
    const r = await L.library.restore(paths);
    reportLibFailed(r.failed, 'Không khôi phục được');
    if (r.restored.length) toast(undo ? `Đã hoàn tác: ${r.restored.length} file về chỗ cũ` : `Đã khôi phục ${r.restored.length} file`, 'ok');
  } catch (err) { toast(`Không khôi phục được: ${errMsg(err)}`, 'err'); }
  afterLibraryChange();
}
async function libPurge(paths) {
  try {
    const r = await (paths ? L.library.purge(paths) : L.library.emptyTrash());
    reportLibFailed(r.failed, 'Không xoá được');
    // purged counts rows: files deleted, rows whose file was already gone, rows of files outside a trash folder (kept on disk)
    const gone = Math.max(0, (r.purged || 0) - (r.files || 0) - (r.kept || 0));
    const parts = [r.files ? `Đã xoá vĩnh viễn ${r.files} file (${fmtBytes(r.bytes)})` : '', gone ? `bỏ ${gone} mục không còn file` : '',
      r.kept ? `giữ ${r.kept} file nằm ngoài thùng rác` : ''].filter(Boolean);
    if (parts.length) toast(parts.join(', ').replace(/^./, (c) => c.toUpperCase()), 'ok');
  } catch (err) { if (!/cancelled/.test(errMsg(err))) toast(`Không xoá được: ${errMsg(err)}`, 'err'); }
  afterLibraryChange();
}
function openLibraryItem(it) {
  const kind = LIB_KIND(String(it.ext || '').toLowerCase());
  const trash = Boolean(it.trashedAt);
  const media = !it.exists ? h('div', { class: 'audio-ph' }, 'File không còn trên đĩa')
    : kind === 'video' ? h('video', { controls: true, autoplay: true, src: fileUrl(it.path) })
      : kind === 'audio' ? h('div', { class: 'cover' }, h('div', { class: 'audio-ph' }, '♪'), h('audio', { controls: true, autoplay: true, src: fileUrl(it.path) }))
        : h('img', { src: fileUrl(it.path), alt: '' });
  const tags = String(it.tags || '').split(' ').filter(Boolean);
  const searchTag = (t) => { closeModal(); $('#lib-q').value = t; runLibrary(0); };
  // the media is stopped and the lightbox closed before the file is moved or deleted
  const act = (fn) => async () => { await releaseMedia(); fn(); };
  const left = trash ? trashLeft(it) : '';
  const actions = trash ? [
    it.exists ? h('button', { class: 'btn primary', onclick: act(() => libRestore([it.path])) }, 'Khôi phục') : null,
    h('button', { class: 'btn danger', onclick: act(() => libPurge([it.path])) }, 'Xoá vĩnh viễn'),
  ] : [
    it.exists && L.library ? h('button', { class: 'btn', onclick: () => L.library.open(it.path).then((r) => r && toast(r, 'err')) }, 'Mở file') : null,
    L.library ? h('button', { class: 'btn', onclick: () => L.library.reveal(it.path) }, 'Mở thư mục') : null,
    L.library?.trash ? h('button', { class: 'btn danger', title: it.exists ? 'Chuyển vào thùng rác' : 'Bỏ khỏi thư viện (file đã mất)', onclick: act(() => libDelete([it])) },
      it.exists ? 'Xoá' : 'Xoá khỏi thư viện') : null,
  ];
  openModal(h('div', { class: 'lightbox', dataset: { path: it.path } },
    h('div', {}, media),
    h('div', {},
      h('h2', {}, `${it.module} #${it.id} `, h('span', { class: 'muted' }, [it.ext, it.size && fmtBytes(it.size), it.date, it.rating].filter(Boolean).join(' · '))),
      trash ? h('p', { class: 'muted' }, `Trong thùng rác · đã xoá ${ago(it.trashedAt * 1000)}${left ? ` · ${left}` : ''}`) : null,
      h('p', { class: 'muted lib-path' }, trash ? `Vị trí cũ: ${it.origPath}` : it.path),
      it.md5 ? h('p', { class: 'muted lib-path' }, `md5 ${it.md5}`) : null,
      h('p', { class: 'muted' }, 'Bấm một tag để tìm các file khác có tag đó.'),
      h('div', { class: 'lb-tags' }, tags.slice(0, 300).map((t) => h('span', { class: 'lb-tag', onclick: () => searchTag(t), style: 'cursor:pointer' }, t))),
      h('div', { class: 'modal-actions' }, ...actions, h('button', { class: 'btn ghost', onclick: closeModal }, 'Đóng')))));
}
$('#lib-search').addEventListener('click', () => runLibrary(0));
$('#lib-q').addEventListener('keydown', (e) => { if (e.key === 'Enter') runLibrary(0); });
['#lib-module', '#lib-order', '#lib-from', '#lib-to'].forEach((id) => $(id).addEventListener('change', () => runLibrary(0)));
$('#lib-prev').addEventListener('click', () => { if (LIB.page > 0) runLibrary(LIB.page - 1); });
$('#lib-next').addEventListener('click', () => runLibrary(LIB.page + 1));
$$('#lib-view button').forEach((b) => b.addEventListener('click', () => setLibraryView(b.dataset.view)));
$('#lib-sel-page').addEventListener('click', () => { LIB.items.forEach((it) => LIB.selected.add(it.path)); updateLibTools(); });
$('#lib-sel-none').addEventListener('click', () => { LIB.selected.clear(); LIB.anchor = -1; updateLibTools(); });
$('#lib-delete').addEventListener('click', () => { if (LIB.selected.size) libDelete(libSelectedItems()); });
$('#lib-restore').addEventListener('click', () => {
  const paths = libSelectedItems().filter((it) => it.exists).map((it) => it.path);
  if (paths.length) libRestore(paths); else toast('File đã chọn không còn trong thùng rác', 'err');
});
$('#lib-purge').addEventListener('click', () => { if (LIB.selected.size) libPurge(libSelectedItems().map((it) => it.path)); });
$('#lib-empty').addEventListener('click', () => libPurge(null));
// the key labelled "delete" on a Mac keyboard sends Backspace (fn + delete sends Delete)
const IS_MAC = /Mac/i.test(navigator.platform || navigator.userAgent || '');
document.addEventListener('keydown', (e) => {
  // library page: Delete moves the selected cards to the trash (reversible), Escape clears the selection. Capture phase: runs
  // before the listener that closes the lightbox on Escape, so closing it keeps the selection
  if (!$('#page-library').classList.contains('active') || !$('#modal').hidden) return;
  if (['INPUT', 'TEXTAREA', 'SELECT'].includes(document.activeElement?.tagName)) return;
  const del = e.key === 'Delete' || (IS_MAC && e.key === 'Backspace');
  if (del && LIB.view === 'library' && LIB.selected.size) { e.preventDefault(); libDelete(libSelectedItems()); }
  else if (e.key === 'Escape' && LIB.selected.size) { LIB.selected.clear(); updateLibTools(); }
}, true);

// ---------------- downloads ----------------
async function startDownload(override = null) {
  const q = override ? { ...currentQuery(), ...override } : currentQuery();
  if (!q.tags) { toast('Chưa có tag nào', 'err'); return; }
  if (!requireKey(q.module)) return;
  // "download selected" builds an id: query of this site: never sent to the other sites
  const sites = !override && multi.size && multiAvailable() ? [...multi] : null;
  const conflict = sites ? multiKindConflict(q.tags, q.module, sites) : '';
  if (conflict) { toast(conflict, 'err'); return; }
  if (!override?.tags) recordHistory(q); // "download selected" builds an id: query, not something the user typed
  const path = $('#f-path').value.trim() || S.settings.download.path;
  try {
    if (S.settings.download.warnNonEmpty) {
      const r = await L.rpc('files.nonEmpty', { path });
      if (r.nonEmpty && !S.settings.download.subfolderPerQuery
        && !window.confirm(`Thư mục “${path}” không trống, kết quả có thể lẫn với lượt tải khác. Vẫn tiếp tục?`)) return;
    }
    const options = { ...downloadOptions(), ...(override?.options || {}) };
    const job = await addJob((allowDuplicate) => (sites
      ? L.jobs.startMulti({ ...q, path, options, allowDuplicate }, sites)
      : L.jobs.start({ ...q, path, options, allowDuplicate })));
    if (Array.isArray(job)) toast(`Đã thêm ${job.length} job, chạy lần lượt: ${job.map((j) => j.module).join(' → ')}`, 'ok');
    else if (job) toast(`Đã thêm job: ${q.label || q.tags}`, 'ok');
  } catch (err) {
    toast(`Không tạo được job: ${errMsg(err)}`, 'err');
  }
}
// main refuses an identical active job ('DUPLICATE: …'): ask, then retry on purpose
async function addJob(call) {
  const add = (r) => { (Array.isArray(r) ? r : [r]).forEach(upsertJob); return r; }; // a multi-site download returns its jobs
  try {
    return add(await call(false));
  } catch (err) {
    const m = /DUPLICATE: (.*)$/.exec(errMsg(err));
    if (!m) throw err;
    if (!window.confirm(`${m[1][0].toUpperCase()}${m[1].slice(1)}. Vẫn thêm job này?`)) return null;
    return add(await call(true));
  }
}
async function rerunJob(id) {
  try {
    const j = await addJob((allowDuplicate) => L.jobs.rerun(id, allowDuplicate));
    if (j) toast(`Đã chạy lại: ${j.label || j.tags}`, 'ok');
  } catch (err) { toast(`Không chạy lại được: ${errMsg(err)}`, 'err'); }
}
$('#btn-download').addEventListener('click', downloadClicked);

// Each job owns one DOM subtree that is created once and then patched in place (at most once per frame), so buttons
// never get swapped out from under the cursor and an open log keeps its scroll position.
const JOB_LOG_MAX = 200;
const JOB_ACTIVE = new Set(['queued', 'running', 'cancelling']);
const jobViews = new Map(); // job id -> view
const dirtyJobs = new Set();
let jobsResort = false;

function upsertJob(job) {
  // logTail only seeds a job we have not seen yet; afterwards every line arrives once through 'jobs:log'
  const { logTail, ...rest } = job;
  const j = S.jobs.get(job.id);
  if (j) Object.assign(j, rest);
  else {
    S.jobs.set(job.id, { ...rest, showLog: false, log: (logTail || []).slice(-JOB_LOG_MAX) });
    jobsResort = true;
  }
  if (job.kind === 'import') onImportJob(job);
  dirtyJobs.add(job.id);
  onFrame(flushJobs);
}
function renderJobs() {
  S.jobs.forEach((_, id) => dirtyJobs.add(id));
  jobsResort = true;
  onFrame(flushJobs);
}
function jobProgress(j) {
  const p = j.progress || {};
  // streaming: files download while search pages are still being scanned, so totalAll can keep growing
  const downloading = (p.stateName === 'DOWNLOADING' || p.stateName === 'IMPORT_FETCH' || !!p.streaming) && p.totalAll > 0;
  const pct = j.status === 'done' ? 100 : downloading ? Math.min(100, (100 * (p.processed || 0)) / p.totalAll) : 0;
  return { p, pct, indet: j.status === 'running' && !downloading };
}
function createJobView(id) {
  const job = () => S.jobs.get(id);
  // "Bị bot challenge chặn": the user passes the check in a window of the app; once saved, the same button runs the job again
  const cfBtn = h('button', { class: 'btn sm', style: 'margin-left:8px', hidden: true, onclick: () => jobChallengeClicked(id) }, 'Xác minh trong Lynxx');
  const m = {
    module: h('span'), state: h('span'), task: h('span'), files: h('span'),
    scan: h('span', { class: 'hint', title: 'Vẫn đang quét thêm trang kết quả, tổng số file có thể còn tăng' }, 'đang quét thêm…'),
    counts: h('span'), cf: h('span', { class: 'danger' }, 'Bị bot challenge chặn', cfBtn), error: h('span', { class: 'danger' }),
    partial: h('span', { class: 'warn' }, 'Xong, một số file lỗi'), gender: h('span'), blocked: h('span'), known: h('span'), imp: h('span'),
    mismatch: h('span', { class: 'warn', title: 'Video / ảnh động được model xem lại sau khi tải mà không khớp bộ lọc giới tính: đã chuyển vào thư mục con _gender_mismatch, không xoá' }),
    speed: h('span', { class: 'mono' }), eta: h('span'),
    dest: h('span', { class: 'mono' }),
  };
  Object.values(m).forEach((el) => { el.hidden = true; });
  const v = {
    m, cfBtn, verified: false, width: null, pre: null, logView: null,
    status: h('span', { class: 'status' }),
    q: h('span', { class: 'job-q' }),
    cancel: h('button', { class: 'btn sm danger', onclick: () => L.jobs.cancel(id) }, 'Huỷ'),
    rerun: h('button', { class: 'btn sm ghost', title: 'Chạy lại đúng truy vấn và tuỳ chọn của job này', onclick: () => rerunJob(id) }, 'Chạy lại'),
    samples: [], // [time, bytes] of the last seconds: download speed
    logBtn: h('button', { class: 'btn sm ghost', onclick: () => toggleJobLog(id) }, 'Log'),
    lists: h('button', { class: 'btn sm ghost', hidden: true, title: 'Tên không nhận ra, post không có trên site, lỗi', onclick: () => { const j = job(); if (j) openImportLists(j); } }, 'Danh sách'),
    fill: h('i'),
  };
  v.bar = h('div', { class: 'bar' }, v.fill);
  v.root = h('div', { class: 'job glass', dataset: { id } },
    h('div', { class: 'job-head' }, v.status, v.q,
      h('div', { class: 'job-actions' }, v.cancel, v.rerun, v.lists,
        h('button', { class: 'btn sm ghost', onclick: () => { const j = job(); if (j) L.app.openPath(j.dest); } }, 'Mở thư mục'),
        v.logBtn)),
    v.bar, h('div', { class: 'job-meta' }, Object.values(m)));
  return v;
}
function updateJobView(v, j) {
  const { p, pct, indet } = jobProgress(j);
  const cls = `status ${j.status}`;
  if (v.status.className !== cls) v.status.className = cls;
  setText(v.status, JOB_VI[j.status] || j.status);
  setText(v.q, j.label || j.tags);
  if (v.q.title !== j.tags) v.q.title = j.tags;
  setHidden(v.cancel, !['queued', 'running'].includes(j.status));
  setHidden(v.rerun, !['done', 'failed', 'cancelled'].includes(j.status) || !L.jobs.rerun);
  setText(v.logBtn, j.showLog ? 'Ẩn log' : 'Log');
  v.bar.classList.toggle('indet', indet);
  const width = indet ? '' : `${pct}%`; // inline width would override the indeterminate animation's width
  if (v.width !== width) { v.width = width; v.fill.style.width = width; }
  const { m } = v;
  slot(m.module, jobModuleText(j));
  slot(m.state, p.stateName && j.status !== 'failed' ? `Trạng thái: ${STATE_VI[p.stateName] || p.stateName}` : jobWaitText(j));
  slot(m.task, p.tasks > 1 ? `Task ${p.task || 0}/${p.tasks}` : null);
  const files = jobFilesText(j, p);
  slot(m.files, files);
  setHidden(m.scan, !(files && p.scanning && j.status === 'running'));
  slot(m.counts, (p.success || p.failed) ? `✓ ${p.success || 0} · ✗ ${p.failed || 0}` : null);
  setHidden(m.cf, !p.cfChallenge);
  if (p.cfChallenge) {
    // after a verification the button runs the job again, once the job has ended (a running one keeps its old cookie)
    setHidden(v.cfBtn, !L.site?.verify || !/^[a-z0-9]{2,4}$/.test(j.module || '') || (v.verified && JOB_ACTIVE.has(j.status)));
    setText(v.cfBtn, v.verified ? 'Chạy lại' : 'Xác minh trong Lynxx');
  }
  slot(m.error, j.status === 'failed' && j.error ? `Lỗi: ${j.error}` : null);
  setHidden(m.partial, !(j.status === 'done' && j.exitCode === 3));
  slot(m.gender, p.genderFiltered ? `Lọc giới tính: ${nf.format(p.genderFiltered)}` : null);
  slot(m.blocked, p.blocked ? `Chặn (an toàn): ${nf.format(p.blocked)}` : null);
  slot(m.known, p.known ? `${j.kind === 'import' ? 'Đã có trong index' : 'Bỏ qua (đã tải)'}: ${nf.format(p.known)}` : null);
  slot(m.mismatch, p.genderMismatch ? `Không khớp, đã chuyển: ${nf.format(p.genderMismatch)} / ${nf.format(p.genderVerified || 0)} file đã xem lại` : null);
  slot(m.imp, j.kind === 'import' ? importCountsText(p) : null);
  setHidden(v.lists, !(j.kind === 'import' && !JOB_ACTIVE.has(j.status) && (p.unknown || p.notFound || p.failed || p.mismatch)));
  const transfer = jobTransfer(v, j, p);
  slot(m.speed, transfer.speed);
  slot(m.eta, transfer.eta);
  slot(m.dest, j.dest ? String(j.dest) : null);
}
async function jobChallengeClicked(id) {
  const j = S.jobs.get(id);
  const v = jobViews.get(id);
  if (!j || !v) return;
  if (v.verified && !JOB_ACTIVE.has(j.status)) { rerunJob(id); return; }
  const again = () => { const x = S.jobs.get(id); if (x && !JOB_ACTIVE.has(x.status)) rerunJob(id); else toast('Job vẫn đang chạy: chạy lại khi nó xong', 'err'); };
  if (!(await verifySite(j.module, JOB_ACTIVE.has(j.status) ? null : ['Chạy lại', again]))) return;
  v.verified = true;
  dirtyJobs.add(id);
  onFrame(flushJobs);
}
// speed over the last ~6 s of progress samples, size of what this run received, rough time left from the pace per file
function jobTransfer(v, j, p) {
  const bytes = Number(p.bytes) || 0;
  if (j.status !== 'running') {
    v.samples.length = 0;
    return { speed: bytes ? `Đã nhận ${fmtBytes(bytes)}` : null, eta: null };
  }
  const now = Date.now();
  const last = v.samples[v.samples.length - 1];
  if (!last || last[1] !== bytes || now - last[0] > 1000) v.samples.push([now, bytes]);
  while (v.samples.length > 2 && now - v.samples[0][0] > 6000) v.samples.shift();
  const [t0, b0] = v.samples[0];
  const rate = now - t0 > 900 ? ((bytes - b0) * 1000) / (now - t0) : 0;
  const speed = bytes ? `${fmtBytes(bytes)}${rate > 0 ? ` · ${fmtBytes(rate)}/s` : ''}` : null;
  let eta = null;
  const done = p.processed || 0;
  if (p.stateName === 'DOWNLOADING' && !p.scanning && done > 0 && p.totalAll > done && j.startedAt) {
    const secs = ((now - j.startedAt) / done) * (p.totalAll - done) / 1000;
    eta = secs < 60 ? 'còn < 1 phút' : secs < 3600 ? `còn ~${Math.round(secs / 60)} phút` : `còn ~${(secs / 3600).toFixed(1)} giờ`;
  }
  return { speed, eta };
}
function flushJobs() {
  const host = $('#jobs');
  for (const [id, v] of jobViews) if (!S.jobs.has(id)) { v.root.remove(); jobViews.delete(id); }
  for (const id of dirtyJobs) {
    const j = S.jobs.get(id);
    if (!j) continue;
    let v = jobViews.get(id);
    if (!v) { v = createJobView(id); jobViews.set(id, v); jobsResort = true; }
    updateJobView(v, j);
  }
  const groups = new Set([...dirtyJobs].map((id) => S.jobs.get(id)?.group?.id).filter(Boolean));
  dirtyJobs.clear();
  if (jobsResort) {
    jobsResort = false;
    // newest first; only nodes that are out of place are moved (a new job is inserted, existing ones stay put).
    // A multi-site group is one box (jobSlot) placed where its newest job goes
    let i = 0;
    const placed = new Set();
    for (const j of [...S.jobs.values()].sort((a, b) => b.createdAt - a.createdAt)) {
      const el = jobSlot(j);
      if (!el || placed.has(el)) continue;
      placed.add(el);
      if (host.children[i] !== el) host.insertBefore(el, host.children[i] || null);
      i++;
    }
    pruneJobGroups();
  }
  groups.forEach(updateGroupView);
  let active = 0;
  for (const j of S.jobs.values()) if (JOB_ACTIVE.has(j.status)) active++;
  const badge = $('#badge-jobs');
  setHidden(badge, active === 0);
  setText(badge, String(active));
  setHidden($('#jobs-empty'), S.jobs.size > 0);
}
function toggleJobLog(id) {
  const j = S.jobs.get(id);
  const v = jobViews.get(id);
  if (!j || !v) return;
  j.showLog = !j.showLog;
  if (j.showLog) {
    v.pre = h('pre', { class: 'job-log' });
    v.root.append(v.pre);
    v.logView = createLogView(v.pre, JOB_LOG_MAX, 50);
    v.logView.reset(j.log);
  } else {
    v.pre?.remove();
    v.pre = null;
    v.logView = null;
  }
  setText(v.logBtn, j.showLog ? 'Ẩn log' : 'Log');
}
function appendJobLog(id, text) {
  const j = S.jobs.get(id);
  if (!j) return;
  j.log.push(text);
  if (j.log.length > JOB_LOG_MAX + 50) j.log.splice(0, j.log.length - JOB_LOG_MAX);
  if (j.showLog) jobViews.get(id)?.logView?.push([text]);
}
$('#jobs-clear').addEventListener('click', async () => {
  const list = await L.jobs.clear();
  const keep = new Set(list.map((j) => j.id));
  for (const id of [...S.jobs.keys()]) if (!keep.has(id)) S.jobs.delete(id);
  list.forEach(upsertJob);
  renderJobs();
});

// ---------------- A-Z tag browser (drawer + page) ----------------
// The type filters follow the module's list in use (tags.types): the usual five are always there, one the list has no tag of is
// off; the other types the list has (species, lore on e621) get a filter of their own, and its rows without a type (lists of sites
// that give none) the 'chưa rõ loại' one. An older core (no tags.types): the five filters, all on
const AZ_TYPES = ['general', 'artist', 'character', 'copyright', 'metadata'];
const AZ_EXTRA_ORDER = ['species', 'lore', 'invalid1'];
const AZ_TYPE_LABEL = { invalid1: 'invalid', unknown: 'chưa rõ loại' };
const azTypeLabel = (t) => AZ_TYPE_LABEL[t] || t;
// 'loại artist, character', 'chưa rõ loại', 'loại artist hoặc chưa rõ loại' (the filters add up: a tag of any of them)
function azTypesText(types) {
  const named = types.filter((t) => t !== 'unknown').map(azTypeLabel);
  return [named.length ? `loại ${named.join(', ')}` : '', types.includes('unknown') ? 'chưa rõ loại' : ''].filter(Boolean).join(' hoặc ');
}
const azInstances = [];
function createAZ(host) {
  // info: tags.types of the module shown ({ hasList, types }), null while not known or with an older core
  const st = { letter: 'a', prefix: '', types: new Set(), minPosts: 0, sort: 'posts', offset: 0, total: 0, loading: false, seq: 0,
    info: null, infoModule: '', reloads: 0 };
  const letters = h('div', { class: 'az-letters' }, [...'abcdefghijklmnopqrstuvwxyz', '#'].map((ch) =>
    h('button', { class: ch === st.letter ? 'on' : '', onclick: () => { st.letter = ch; st.prefix = ''; prefixInput.value = ''; reload(); } }, ch)));
  const prefixInput = h('input', { type: 'text', placeholder: 'Lọc theo tiền tố (vd: big_)…', oninput: debounce(() => { st.prefix = prefixInput.value.trim().toLowerCase(); reload(); }, 200) });
  const minSel = h('select', { onchange: () => { st.minPosts = Number(minSel.value); reload(); } },
    [0, 10, 100, 1000, 10000].map((v) => h('option', { value: v }, v ? `≥ ${fmt(v)} post` : 'Mọi số post')));
  const sortSeg = h('div', { class: 'seg' },
    h('button', { class: 'on', dataset: { s: 'posts' } }, 'Phổ biến'), h('button', { dataset: { s: 'az' } }, 'A→Z'));
  sortSeg.addEventListener('click', (e) => {
    const s = e.target.dataset.s;
    if (!s) return;
    st.sort = s;
    $$('button', sortSeg).forEach((b) => b.classList.toggle('on', b.dataset.s === s));
    reload();
  });
  const types = h('div', { class: 'az-types' });
  const typeCount = (t) => Number(st.info?.types?.[t]) || 0;
  const typeOff = (t) => Boolean(st.info) && typeCount(t) <= 0;
  const typeBtns = new Map(); // type -> its filter button, kept across renders (focus stays on the one clicked)
  function typeButton(t) {
    let b = typeBtns.get(t);
    if (!b) {
      b = h('button', { type: 'button', class: 'pill', dataset: { type: t }, onclick: () => {
        if (typeOff(t)) return;
        if (st.types.has(t)) st.types.delete(t); else st.types.add(t);
        renderTypes();
        reload();
      } }, h('span', { class: `type-dot t-${t}` }), azTypeLabel(t));
      typeBtns.set(t, b);
    }
    return b;
  }
  function renderTypes() {
    const mod = (st.infoModule || S.module).toUpperCase();
    const info = st.info;
    const rank = (t) => (t === 'unknown' ? 1000 : (AZ_EXTRA_ORDER.indexOf(t) + 1 || 100));
    const extra = info ? Object.keys(info.types).filter((t) => !AZ_TYPES.includes(t) && typeCount(t) > 0)
      .sort((a, b) => rank(a) - rank(b) || a.localeCompare(b)) : [];
    const btns = [...AZ_TYPES, ...extra].map(typeButton);
    if (btns.length !== types.children.length || btns.some((b, i) => types.children[i] !== b)) types.replaceChildren(...btns);
    for (const b of btns) {
      const t = b.dataset.type;
      const n = typeCount(t);
      const off = typeOff(t);
      let title = '';
      if (off) title = info.hasList ? `Danh sách tag đang dùng của ${mod} không có tag loại ${azTypeLabel(t)}` : `Chưa có danh sách tag cho ${mod}`;
      else if (info) title = t === 'unknown' ? `${nf.format(n)} tag (danh sách không ghi loại của chúng)` : `${nf.format(n)} tag`;
      b.classList.toggle('on', st.types.has(t));
      b.classList.toggle('off', off);
      b.setAttribute('aria-pressed', String(st.types.has(t)));
      if (off) b.setAttribute('aria-disabled', 'true'); else b.removeAttribute('aria-disabled');
      if (title) b.title = title; else b.removeAttribute('title');
    }
  }
  // the type filters of the module shown (tags.types, asked once per module and list); a selected type its list has no tag of is
  // dropped, and so is an extra one when the types are not known (its filter is not shown). Returns whether the selection changed
  async function syncTypes() {
    const module = S.module;
    const info = await loadListTypes(module);
    if (module !== S.module) return false;
    st.info = info;
    st.infoModule = module;
    let dropped = false;
    for (const t of [...st.types]) {
      if (info ? typeCount(t) <= 0 : !AZ_TYPES.includes(t)) { st.types.delete(t); dropped = true; }
    }
    renderTypes();
    return dropped;
  }
  renderTypes();
  const list = h('div', { class: 'az-list' });
  const foot = h('div', { class: 'az-foot' }, h('span', {}, ''), h('span', { class: 'muted' }, ''));
  host.replaceChildren(letters, h('div', { class: 'az-tools' }, prefixInput, minSel, sortSeg), types, list, foot);
  // why nothing is listed: no list, or nothing of the selected types / prefix / post count in it
  function emptyText(prefix) {
    const info = st.infoModule === S.module ? st.info : null;
    if (!info) return st.prefix ? 'Không có tag nào với tiền tố này' : 'Chưa có danh sách tag cho module này';
    if (!info.hasList) return 'Chưa có danh sách tag cho module này';
    if (!st.types.size && st.prefix && !st.minPosts) return 'Không có tag nào với tiền tố này';
    const what = st.types.size ? `tag ${azTypesText([...st.types])}` : 'tag nào';
    const start = prefix === '#' ? 'không bắt đầu bằng chữ cái a–z' : `bắt đầu bằng “${prefix}”`;
    return `Không có ${what} ${start}${st.minPosts ? ` với ≥ ${fmt(st.minPosts)} post` : ''}`;
  }

  async function load(append) {
    // paging waits for the running load; a reload (letter, prefix, filter or module change) always starts and makes
    // whatever is still in flight stale through the sequence number
    if (append && st.loading) return;
    const seq = ++st.seq;
    st.loading = true;
    const module = S.module;
    // a typed prefix is used as is ("big_" -> big_*, never letter + prefix); otherwise the selected letter
    const prefix = st.prefix || st.letter;
    try {
      const r = await L.rpc('tags.browse', { module, prefix, offset: append ? st.offset : 0, limit: 150, minPosts: st.minPosts,
        types: [...st.types], sort: st.sort });
      if (seq !== st.seq || module !== S.module) return;
      if (!append) { list.replaceChildren(); list.scrollTop = 0; st.offset = 0; }
      st.total = r.total;
      st.offset += r.items.length;
      list.append(...r.items.map((it) => h('div', { class: 'az-row' },
        h('span', { class: `type-dot t-${it.type}`, title: it.type }), h('span', { class: 'nm', title: it.tag }, it.tag),
        h('span', { class: 'cnt' }, fmt(it.posts)),
        h('button', { class: 'btn sm', title: 'Thêm vào truy vấn', onclick: () => { addChip(it.tag); toast(`Đã thêm ${it.tag}`); } }, '+'),
        h('button', { class: 'btn sm', title: 'Loại trừ', onclick: () => {
          const why = excludeRefused(it.tag); // an 'artist:x' row of a site listing
          if (why) { toast(why, 'err'); return; }
          addChip(`-${it.tag}`); toast(`Đã loại trừ ${it.tag}`);
        } }, '−'))));
      foot.firstChild.textContent = `${nf.format(st.total)} tag · ${module.toUpperCase()}${st.prefix ? ` · tiền tố “${st.prefix}”` : ''}`;
      foot.lastChild.textContent = st.total === 0 ? emptyText(prefix) : st.offset < st.total ? 'Cuộn để tải thêm' : '';
    } catch (err) {
      if (seq === st.seq) foot.firstChild.textContent = `Lỗi: ${errMsg(err)}`;
    } finally {
      if (seq === st.seq) st.loading = false;
    }
  }
  async function reload() {
    // the highlighted letter follows a typed prefix
    const on = st.prefix ? (/^[a-z]/.test(st.prefix) ? st.prefix[0] : '') : st.letter;
    $$('button', letters).forEach((b) => b.classList.toggle('on', b.textContent === on));
    // the type filters first (known at once after the first time): a filter the new module's list cannot match is not sent
    const n = ++st.reloads;
    // while the filters are asked: rows of the previous letter / module still in flight are stale, and no paging (load(true))
    st.seq++;
    st.loading = true;
    await syncTypes();
    if (n === st.reloads) load(false);
  }
  list.addEventListener('scroll', () => {
    if (list.scrollTop + list.clientHeight > list.scrollHeight - 200 && st.offset < st.total) load(true);
  });
  // module change: only an instance that was opened before reloads now, the others load when first shown. The module's list
  // changed (forgetListTypes): its filters are asked again, the rows shown stay unless a selected filter went away
  const api = { reload, reset: () => { if (api.loaded) reload(); }, loaded: false,
    typesChanged: (abbr) => {
      if (!api.loaded || abbr !== S.module) return;
      syncTypes().then((dropped) => { if (dropped || st.total === 0) reload(); }); // nothing shown yet (no list before): no scroll lost
    } };
  azInstances.push(api);
  return api;
}
const azDrawer = createAZ($('#az-drawer-host'));
const azPage = createAZ($('#az-page-host'));
$('#btn-az').addEventListener('click', () => {
  const d = $('#az-drawer');
  d.hidden = !d.hidden;
  if (!d.hidden) $('#hist-drawer').hidden = true;
  if (!d.hidden && !azDrawer.loaded) { azDrawer.loaded = true; azDrawer.reload(); }
});
$('#az-close').addEventListener('click', () => { $('#az-drawer').hidden = true; });

// ---------------- search history / saved queries (settings.search.history, settings.search.saved) ----------------
// Both lists are written whole (main's deep merge replaces arrays); main validates and caps them as well.
const HISTORY_MAX = 50;
const SAVED_MAX = 200;
const QUERY_MAX_LEN = 2000;
const NAME_MAX_LEN = 80;
const searchList = (key) => (Array.isArray(S.settings?.search?.[key]) ? S.settings.search[key] : []);
async function saveSearchList(key, list) {
  S.settings.search[key] = list; // shown at once; the saved copy (validated by main) replaces it when it comes back
  try { await saveSettings({ search: { [key]: list } }); } catch (err) { toast(`Không lưu được: ${errMsg(err)}`, 'err'); }
  if (!$('#hist-drawer').hidden) renderHistory();
}
function recordHistory(q) {
  const tags = String(q.tags || '').trim().slice(0, QUERY_MAX_LEN);
  if (!tags || !S.settings) return;
  const list = searchList('history').filter((e) => !(e.module === q.module && e.tags === tags));
  list.unshift({ module: q.module, tags, at: Date.now() });
  saveSearchList('history', list.slice(0, HISTORY_MAX));
}
function loadQuery(e) {
  if (e.module !== S.module) {
    if (!moduleInfo(e.module)?.installed) { toast(`Module ${e.module} chưa được cài`, 'err'); return; }
    setModule(e.module);
  }
  S.chips = [];
  S.selected.clear();
  splitQuery(e.tags).tokens.forEach((t) => { const c = parseToken(t); if (c) S.chips.push({ ...c, id: ++S.chipSeq }); });
  renderChips();
  onQueryChanged();
  $('#hist-drawer').hidden = true;
  tagInput.focus();
}
async function saveQuery(src = null) {
  const q = src || currentQuery();
  const tags = String(q.tags || '').trim();
  if (!tags) { toast('Chưa có tag nào', 'err'); return; }
  const v = await prompt2('Lưu truy vấn', [{ label: `Tên (${q.module}: ${tags.length > 60 ? `${tags.slice(0, 60)}…` : tags})`, value: tags.slice(0, NAME_MAX_LEN) }]);
  const name = v?.[0]?.slice(0, NAME_MAX_LEN);
  if (!name) return;
  const list = searchList('saved').filter((e) => e.name !== name);
  if (list.length >= SAVED_MAX) { toast(`Tối đa ${SAVED_MAX} truy vấn đã lưu`, 'err'); return; }
  list.unshift({ name, module: q.module, tags: tags.slice(0, QUERY_MAX_LEN), at: Date.now() });
  await saveSearchList('saved', list);
  toast(`Đã lưu truy vấn “${name}”`, 'ok');
}
async function renameSaved(e) {
  const v = await prompt2('Đổi tên truy vấn', [{ label: 'Tên mới', value: e.name }]);
  const name = v?.[0]?.slice(0, NAME_MAX_LEN);
  if (!name || name === e.name) return;
  if (searchList('saved').some((x) => x.name === name) && !window.confirm(`Đã có truy vấn tên “${name}”. Ghi đè?`)) return;
  saveSearchList('saved', searchList('saved').filter((x) => x.name !== name).map((x) => (x.name === e.name ? { ...x, name } : x)));
}
function deleteSaved(e) {
  if (!window.confirm(`Xoá truy vấn đã lưu “${e.name}”?`)) return;
  saveSearchList('saved', searchList('saved').filter((x) => x.name !== e.name));
}
// saved query downloaded again on a schedule by main (only newer posts, files already downloaded are skipped)
const AUTO_VI = { 6: '6 giờ', 12: '12 giờ', 24: 'mỗi ngày', 72: '3 ngày', 168: 'mỗi tuần' };
// main adds 'id:>N' to a scheduled run only where the module searches by id and the query is not a site listing (main.js autoIdFilter)
const autoIdFilterOk = (e) => idSearchOk(e.module) && !queryKinds(e.tags, e.module).some((m) => m.kind.listing && !m.negated);
const AUTO_NO_ID = 'Truy vấn này không lọc được theo id post (site không tìm theo id, hoặc là danh sách riêng của site): '
  + 'mỗi lần chạy xét lại cả kết quả, file đã tải được bỏ qua.';
function autoTitle(e) {
  const a = e.auto;
  if (!a?.every) return 'Tự tải lại định kỳ: tắt';
  const last = a.lastRun ? `lần cuối ${ago(a.lastRun)}` : 'chưa chạy';
  const st = { done: `: ${a.lastCount} file mới`, failed: `: lỗi ${a.lastError || ''}`, cancelled: ': đã huỷ', running: ': đang chạy', baseline: ' (mốc bắt đầu)' }[a.lastStatus] || '';
  const ids = !autoIdFilterOk(e) ? ' · không lọc theo id: bỏ qua file đã tải' : a.maxId ? ` · chỉ lấy post có id > ${a.maxId}` : '';
  return `Tự tải lại ${AUTO_VI[a.every]} · ${last}${st}${ids}`;
}
async function editSavedAuto(e) {
  if (!L.saved) return;
  const sel = h('select', {}, h('option', { value: '0' }, 'Tắt'), ...Object.entries(AUTO_VI).map(([v, t]) => h('option', { value: v }, t)));
  sel.value = String(e.auto?.every || 24);
  const byId = autoIdFilterOk(e); // "only newer posts" needs 'id:>N'
  const fromNow = h('input', { type: 'radio', name: 'auto-from', checked: byId, disabled: !byId });
  const fromAll = h('input', { type: 'radio', name: 'auto-from', checked: !byId });
  const save = async () => {
    const every = Number(sel.value);
    closeModal();
    try {
      const r = await L.saved.setAuto(`${e.module}\n${e.tags}`, every, fromNow.checked ? 'now' : 'all');
      S.settings = r.settings;
      renderHistory();
      toast(every ? `“${e.name}” sẽ tự tải lại ${AUTO_VI[every]}` : `Đã tắt tự tải lại “${e.name}”`, 'ok');
    } catch (err) { toast(`Không đặt được lịch: ${errMsg(err)}`, 'err'); }
  };
  openModal(h('div', {},
    h('h2', {}, `Tự tải lại “${e.name}”`),
    h('p', { class: 'muted' }, `${e.module}: ${e.tags}`),
    h('label', { class: 'field' }, h('span', {}, 'Chu kỳ'), sel),
    e.auto?.every ? h('p', { class: 'muted' }, autoTitle(e)) : h('div', { class: 'radio-list' }, h('span', { class: 'muted' }, 'Lần đầu'),
      h('label', { title: byId ? '' : AUTO_NO_ID }, fromNow, 'Chỉ post mới từ bây giờ'),
      h('label', {}, fromAll, 'Tải cả kết quả hiện có (bỏ qua file đã tải)')),
    h('p', { class: 'muted' }, byId ? 'Lynxx chạy truy vấn khi đang mở, theo thư mục và tuỳ chọn tải mặc định trong Cài đặt; mỗi lần chỉ lấy post mới hơn lần trước.'
      : `Lynxx chạy truy vấn khi đang mở, theo thư mục và tuỳ chọn tải mặc định trong Cài đặt. ${AUTO_NO_ID}`),
    h('div', { class: 'modal-actions' }, h('button', { class: 'btn ghost', onclick: closeModal }, 'Huỷ'), h('button', { class: 'btn primary', onclick: save }, 'Lưu'))));
}
let histTab = 'history';
function renderHistory() {
  $$('#hist-tabs button').forEach((b) => b.classList.toggle('on', b.dataset.tab === histTab));
  $('#hist-clear').hidden = histTab !== 'history' || !searchList('history').length;
  const host = $('#hist-list');
  const stop = (fn) => (ev) => { ev.stopPropagation(); fn(); };
  if (histTab === 'history') {
    const list = searchList('history');
    host.replaceChildren(...(list.length ? list.map((e) => h('div', { class: 'hist-row', title: `${e.module}: ${e.tags}`, onclick: () => loadQuery(e) },
      h('span', { class: 'mod-abbr' }, e.module),
      h('div', { class: 'hist-main' }, h('code', {}, e.tags), h('span', { class: 'muted' }, ago(e.at))),
      h('button', { class: 'btn sm ghost', title: 'Lưu truy vấn này', onclick: stop(() => saveQuery(e)) }, '☆'),
      h('button', { class: 'btn sm ghost', title: 'Xoá khỏi lịch sử', onclick: stop(() => saveSearchList('history', searchList('history').filter((x) => !(x.module === e.module && x.tags === e.tags)))) }, '×')))
      : [h('div', { class: 'empty' }, 'Chưa có lịch sử. Mỗi lần Đếm, Preview hoặc Tải xuống, truy vấn được ghi lại ở đây.')]));
  } else {
    const list = searchList('saved');
    host.replaceChildren(...(list.length ? list.map((e) => h('div', { class: 'hist-row', title: `${e.module}: ${e.tags}`, onclick: () => loadQuery(e) },
      h('span', { class: 'mod-abbr' }, e.module),
      h('div', { class: 'hist-main' }, h('b', {}, e.name), h('code', {}, e.tags)),
      h('button', { class: `btn sm ${e.auto?.every ? '' : 'ghost'}`, title: autoTitle(e), onclick: stop(() => editSavedAuto(e)) },
        e.auto?.every ? `⟳ ${AUTO_VI[e.auto.every]}` : '⟳'),
      h('button', { class: 'btn sm ghost', title: 'Đổi tên', onclick: stop(() => renameSaved(e)) }, 'Đổi tên'),
      h('button', { class: 'btn sm ghost danger', title: 'Xoá', onclick: stop(() => deleteSaved(e)) }, 'Xoá')))
      : [h('div', { class: 'empty' }, 'Chưa có truy vấn nào được lưu. Bấm “☆ Lưu truy vấn” để lưu truy vấn hiện tại.')]));
  }
}
$('#btn-history').addEventListener('click', () => {
  const d = $('#hist-drawer');
  d.hidden = !d.hidden;
  if (!d.hidden) {
    $('#az-drawer').hidden = true;
    renderHistory();
    // schedules run in main: refresh their last run / status
    L.settings.get().then((r) => { S.settings = r.settings; if (!d.hidden) renderHistory(); }).catch(() => {});
  }
});
$('#hist-close').addEventListener('click', () => { $('#hist-drawer').hidden = true; });
$('#hist-tabs').addEventListener('click', (e) => { const t = e.target.closest('button')?.dataset.tab; if (t) { histTab = t; renderHistory(); } });
$('#hist-save').addEventListener('click', () => saveQuery());
$('#btn-save-query').addEventListener('click', () => saveQuery());

// ---------------- search by file name / post link ----------------
// Pasted, dropped or picked file names, paths and post / file links -> 'id:' / 'md5:' query (sidecar files.identify).
// Only names are used: dropped and picked files are never opened or read.
const reFileish = new RegExp([
  '\\.(?:jpe?g|jfif|png|gif|webp|avif|bmp|webm|mp4|mkv|mov|m4v|swf)(?=\\s|$|[?#])', // a media file name
  '^\\s*https?://\\S+', // a link
  '(?:^|[^0-9a-f])[0-9a-f]{32}(?:[^0-9a-f]|$)', // an md5
].join('|'), 'im');
const looksLikeFiles = (text) => reFileish.test(String(text || ''));
const FILE_NOTE_VI = {
  'not recognized': 'Không nhận ra',
  'info list file, not a post': 'File danh sách info, không phải post',
  'no post id or md5 in the link': 'Link không có id post hoặc md5',
  'unknown site, current module assumed': 'site lạ, dùng module đang chọn',
};
function namesFromTransfer(dt) {
  const names = [...(dt?.files || [])].map((f) => f.name);
  if (!names.length) {
    const uris = (dt?.getData('text/uri-list') || dt?.getData('text/plain') || '').split(/\r?\n/).filter((s) => s && !s.startsWith('#'));
    names.push(...uris);
  }
  return names;
}
function openFilesSearch(initial = '') {
  const area = h('textarea', {
    class: 'files-input', rows: '6', spellcheck: 'false',
    placeholder: 'Mỗi dòng một mục: tên file (db_12345.jpg, 12345.jpg, <md5>.png), đường dẫn, link post hoặc link ảnh',
  });
  area.value = initial;
  const picker = h('input', { type: 'file', multiple: true, hidden: true });
  const list = h('div', { class: 'files-list' });
  const actions = h('div', { class: 'files-queries' });
  let seq = 0;
  const run = async () => {
    const my = ++seq;
    const lines = area.value.split(/\r?\n/).map((s) => s.trim()).filter(Boolean);
    if (!lines.length) { list.replaceChildren(); actions.replaceChildren(); return; }
    let r;
    try {
      r = await rpcOptional('files.identify', { names: lines, module: S.module });
    } catch (err) {
      if (my === seq) list.replaceChildren(h('div', { class: 'err' }, errMsg(err)));
      return;
    }
    if (my !== seq) return; // a newer edit is being identified
    if (!r) { list.replaceChildren(h('div', { class: 'err' }, 'Lõi Python chưa hỗ trợ tìm theo tên file (cần cập nhật).')); return; }
    const ok = r.items.filter((it) => it.kind).length;
    list.replaceChildren(
      h('div', { class: 'muted files-sum' }, `Nhận ra ${ok}/${r.items.length} mục${lines.length > r.max ? ` (chỉ xét ${r.max} mục đầu)` : ''}`),
      ...r.items.map((it) => h('div', { class: `files-item${it.kind ? '' : ' bad'}` },
        h('span', { class: 'files-name mono', title: it.name }, it.name),
        h('span', { class: 'files-res' }, it.kind
          ? `${it.module} · ${it.kind} ${it.kind === 'md5' ? `${it.value.slice(0, 12)}…` : it.value}${it.note ? ` (${FILE_NOTE_VI[it.note] || it.note})` : ''}`
          : (FILE_NOTE_VI[it.note] || it.note || 'Không nhận ra')))),
    );
    // the current module first, then by number of items
    const qs = [...r.queries].sort((a, b) => (b.module === S.module) - (a.module === S.module) || b.count - a.count);
    actions.replaceChildren(...qs.map((q, i) => {
      const mi = moduleInfo(q.module);
      const name = mi?.name || q.module.toUpperCase();
      // idSearch false (manifest 'search'): 'id:' / 'md5:' searches do not work on that site
      const ok = q.idSearch !== false && idSearchOk(q.module);
      return h('button', {
        class: `btn ${i === 0 && ok ? 'primary' : ''}`,
        title: ok ? q.tags : `${name} không tìm được theo id post hay md5: không tìm các mục này trên site đó được`,
        disabled: !ok,
        onclick: () => {
          if (!mi?.installed) { toast(`Module ${name} chưa được cài (Extensions)`, 'err'); return; }
          closeModal();
          loadQuery({ module: q.module, tags: q.tags });
          runPreview(0);
          if (q.dropped) toast(`Chỉ tìm ${q.count} mục đầu trên ${name}: mỗi mục là một lượt tìm riêng, tối đa 64`, 'err');
        },
      }, `Tìm ${q.count} mục trên ${name}`);
    }));
  };
  const runSoon = debounce(run, 250);
  area.addEventListener('input', runSoon);
  const addNames = (names) => {
    if (!names.length) return;
    area.value = [area.value.trim(), ...names].filter(Boolean).join('\n');
    run();
  };
  picker.addEventListener('change', () => { addNames([...picker.files].map((f) => f.name)); picker.value = ''; });
  area.addEventListener('drop', (e) => { e.preventDefault(); e.stopPropagation(); addNames(namesFromTransfer(e.dataTransfer)); });
  openModal(h('div', { class: 'files-modal' },
    h('h3', {}, 'Tìm theo tên file / link'),
    h('p', { class: 'muted' }, 'Dán hoặc kéo-thả file đã tải, đường dẫn, link post hay link ảnh. Lynxx chỉ đọc tên file, không đọc nội dung. '
      + 'Nhận ra: tên Lynxx đặt (db_12345…, 12345…), tên gốc md5, tên tải từ Danbooru (__…__md5), link post của các site đã biết.'),
    area,
    h('div', { class: 'files-tools' },
      h('button', { class: 'btn sm', onclick: () => picker.click() }, 'Chọn file…'),
      h('button', { class: 'btn sm ghost', onclick: () => { area.value = ''; run(); area.focus(); } }, 'Xoá hết'),
      picker),
    list,
    h('div', { class: 'modal-actions' }, actions, h('button', { class: 'btn ghost', onclick: closeModal }, 'Đóng'))));
  area.focus();
  if (initial) run();
}
$('#btn-files').addEventListener('click', () => openFilesSearch());
// files / links dropped on the search bar open the same dialog (the window itself never navigates, see below)
$('.searchbar').addEventListener('drop', (e) => {
  const names = namesFromTransfer(e.dataTransfer);
  if (!names.length) return;
  e.preventDefault();
  e.stopPropagation();
  openFilesSearch(names.join('\n'));
});
$('#hist-clear').addEventListener('click', () => { if (window.confirm('Xoá toàn bộ lịch sử tìm kiếm?')) saveSearchList('history', []); });

// ---------------- related tags (search.related: tags that co-occur in the first results page) ----------------
const related = { seq: 0, tags: '', module: '', items: [], sample: 0 };
function relatedSupported() { return typeof L?.search?.related === 'function'; }
async function loadRelated() {
  const q = currentQuery();
  if (!q.tags) { toast('Chưa có tag nào', 'err'); return; }
  const seq = ++related.seq;
  $('#related').hidden = false;
  $('#related-info').textContent = 'Đang lấy tag liên quan…';
  $('#related-refresh').disabled = true;
  try {
    const r = await L.search.related(q);
    if (seq !== related.seq) return;
    Object.assign(related, { tags: q.tags, module: q.module, items: r.items || [], sample: r.sample || 0 });
    renderRelated();
  } catch (err) {
    if (seq === related.seq) $('#related-info').textContent = `Lỗi: ${errMsg(err)}`;
  } finally {
    if (seq === related.seq) $('#related-refresh').disabled = false;
  }
}
function renderRelated() {
  const inQuery = new Set(S.chips.flatMap((c) => (c.kind === 'or' || c.kind === 'neg' ? c.items : [c.text]))
    .flatMap((t) => [t, tagKind(t)?.value || '']).map((t) => t.replace(/^-/, '').toLowerCase()).filter(Boolean)); // 'artist:x' has x
  const { sample } = related;
  const showMeta = $('#related-meta').checked;
  const items = showMeta ? related.items : related.items.filter((it) => it.type !== 'metadata');
  const hiddenMeta = related.items.length - items.length;
  $('#related-info').textContent = related.items.length
    ? `hay đi kèm trong ${sample} post đầu của “${related.tags}”${hiddenMeta ? ` · ẩn ${hiddenMeta} tag metadata` : ''}`
    : sample ? 'Không có tag nào đi kèm' : 'Website này không trả tag trong kết quả tìm kiếm';
  $('#related-list').replaceChildren(...items.map((it) => {
    const has = inQuery.has(it.tag.toLowerCase());
    return h('span', { class: `rel-item ${has ? 'in' : ''}`, title: `${it.tag}: ${it.count}/${sample} post đầu · ${nf.format(it.posts || 0)} post trên site` },
      h('span', { class: `type-dot t-${it.type}` }), h('span', { class: 'nm' }, it.tag), h('span', { class: 'cnt' }, `${it.count}`),
      h('button', { title: 'Thêm vào truy vấn', disabled: has, onclick: () => { addChip(it.tag); renderRelated(); } }, '+'),
      h('button', { title: 'Loại trừ', disabled: has, onclick: () => { addChip(`-${it.tag}`); renderRelated(); } }, '−'));
  }));
  markRelatedStale();
}
function markRelatedStale() {
  if ($('#related').hidden || !related.tags) return;
  const stale = related.tags !== queryString() || related.module !== S.module;
  $('#related').classList.toggle('stale', stale);
  $('#related-refresh').textContent = stale ? 'Làm mới theo truy vấn mới' : 'Làm mới';
}
$('#btn-related').addEventListener('click', () => loadRelated());
$('#related-refresh').addEventListener('click', () => loadRelated());
$('#related-meta').addEventListener('change', () => { if (related.tags) renderRelated(); });
$('#related-close').addEventListener('click', () => { related.seq++; $('#related').hidden = true; });

// ---------------- download index (settings page) ----------------
// index.stats reads <data dir>/index/downloads.sqlite (lynxx/dlindex.py); the full offline search page comes later
function fmtBytes(n) {
  const v = Number(n) || 0;
  if (v >= 1073741824) return `${(v / 1073741824).toFixed(2)} GB`;
  if (v >= 1048576) return `${(v / 1048576).toFixed(1)} MB`;
  return `${nf.format(Math.round(v / 1024))} KB`;
}
let indexStatsSeq = 0;
async function refreshIndexStats() {
  const seq = ++indexStatsSeq;
  const table = $('#index-stats');
  let r;
  try {
    r = await L.rpc('index.stats');
  } catch (err) {
    if (seq === indexStatsSeq) table.replaceChildren(h('tr', {}, h('td', { class: 'muted' }, `Không đọc được index: ${errMsg(err)}`)));
    return;
  }
  if (seq !== indexStatsSeq) return;
  const when = (t) => (t ? new Date(t * 1000).toLocaleString('vi-VN') : '—');
  $('#index-path').textContent = r.path || '';
  const trash = r.trash || { files: 0, bytes: 0 };
  setText($('#index-trash-info'), trash.files ? `(${nf.format(trash.files)} file · ${fmtBytes(trash.bytes)})` : '(trống)');
  $('#index-empty-trash').disabled = !trash.files;
  if (!r.exists) {
    table.replaceChildren(h('tr', {}, h('td', { class: 'muted' }, r.recording === false
      ? 'Chưa có index; đang tắt ghi (LYNXX_DLINDEX=0).' : 'Chưa có index: file đầu tiên tải xong sẽ được ghi vào.')));
    return;
  }
  const row = (k, v) => h('tr', {}, h('th', {}, k), h('td', { colspan: 3 }, v));
  table.replaceChildren(
    row('File', nf.format(r.files)),
    row('Post', nf.format(r.posts)),
    row('Có md5', nf.format(r.withMd5)),
    row('Tag khác nhau', nf.format(r.tags)),
    row('Dung lượng file', fmtBytes(r.bytes)),
    row('Dung lượng index', fmtBytes(r.dbBytes)),
    row('Thùng rác', trash.files ? `${nf.format(trash.files)} file · ${fmtBytes(trash.bytes)}` : 'trống'),
    row('Lần tải gần nhất', when(r.last)),
    ...(r.recording === false ? [row('Ghi index', 'đang tắt (LYNXX_DLINDEX=0)')] : []),
    ...(r.modules?.length ? [h('tr', {}, h('th', {}, 'Module'), h('th', {}, 'File'), h('th', {}, 'Dung lượng'), h('th', {}, 'Gần nhất')),
      ...r.modules.map((m) => h('tr', {}, h('td', {}, h('span', { class: 'mod-abbr' }, m.module)), h('td', {}, nf.format(m.files)),
        h('td', {}, fmtBytes(m.bytes)), h('td', {}, when(m.last))))] : []));
}
$('#index-refresh').addEventListener('click', () => refreshIndexStats());
$('#s-trash-days').addEventListener('change', async (e) => {
  // a shorter time empties what is older at once (main); the "còn X ngày" badges follow
  try { await saveSettings({ library: { trashDays: Number(e.target.value) } }); } catch (err) { toast(`Không lưu được: ${errMsg(err)}`, 'err'); }
  if (LIB.ready && LIB.view === 'trash') renderLibrary();
});
$('#index-empty-trash').addEventListener('click', async () => {
  await libPurge(null); // main asks first (native dialog)
  refreshIndexStats();
});
$('#s-skip-known').addEventListener('change', (e) => { $('#f-skip-known').checked = e.target.checked; });

// ---------------- download index tools: clean (index.clean in the sidecar, via main) ----------------
// finished: ids of cleans whose index.cleanDone arrived (a small index is done before main's reply reaches the renderer)
const IDX = { cleaning: '', finished: new Set() };
function renderCleanState(text) {
  slot($('#index-clean-status'), text);
  setHidden($('#index-clean-stop'), !IDX.cleaning);
  $('#index-clean').disabled = Boolean(IDX.cleaning);
  $('#index-clean-check').disabled = Boolean(IDX.cleaning);
}
async function startIndexClean(dryRun) {
  if (!L.index) { toast('Bản app này chưa có công cụ dọn index', 'err'); return; }
  try {
    // main asks for confirmation (native dialog, with the "missing folders" choice); a dry run only counts
    const r = await L.index.clean(dryRun ? { dryRun: true } : undefined);
    if (IDX.finished.has(r.job)) return; // the result is already shown
    IDX.cleaning = r.job;
    renderCleanState(dryRun ? 'Đang kiểm tra…' : 'Đang dọn…');
  } catch (err) {
    if (!/cancelled/.test(errMsg(err))) toast(`Không dọn được index: ${errMsg(err)}`, 'err');
  }
}
function onIndexCleanNotify(method, p) {
  if (method === 'index.cleanProgress') {
    if (IDX.finished.has(p.job)) return;
    IDX.cleaning = p.job;
    renderCleanState(`${p.dryRun ? 'Đang kiểm tra' : 'Đang dọn'}: ${nf.format(p.checked)} / ${nf.format(p.total)} dòng · `
      + `${nf.format(p.missing)} file không còn${p.dryRun ? '' : ` · đã bỏ ${nf.format(p.removed)} dòng`}`);
    return;
  }
  IDX.cleaning = '';
  if (p.job) IDX.finished.add(p.job);
  if (!p.ok) {
    renderCleanState(`Lỗi: ${p.message || '?'}`);
    toast(`Dọn index lỗi: ${p.message || '?'}`, 'err');
    return;
  }
  const keptDirs = p.missingDirs && !p.missingFolders;
  const kept = keptDirs ? ` ${nf.format(p.missingDirs)} dòng có cả thư mục không còn được giữ lại (ổ cứng ngoài / thư mục mạng chưa kết nối?).` : '';
  const folders = p.folders?.length ? ` Thư mục không còn: ${p.folders.slice(0, 5).join(', ')}${p.folders.length > 5 ? '…' : ''}` : '';
  const text = p.dryRun
    ? `Đã kiểm ${nf.format(p.checked)} dòng: ${nf.format(p.missing)} file không còn trên đĩa, dọn sẽ bỏ ${nf.format(p.missing - (keptDirs ? p.missingDirs : 0))} dòng.${kept}`
    : `${p.cancelled ? 'Đã dừng' : 'Đã dọn'}: kiểm ${nf.format(p.checked)} / ${nf.format(p.total)} dòng, bỏ ${nf.format(p.removed)} dòng có file không còn.${kept} Không file nào bị xoá.`;
  renderCleanState(text + folders);
  if (!p.dryRun) {
    toast(text, 'ok');
    refreshIndexStats();
    if (LIB.ready) runLibrary(LIB.page);
  }
}
$('#index-clean-check').addEventListener('click', () => startIndexClean(true));
$('#index-clean').addEventListener('click', () => startIndexClean(false));
$('#index-clean-stop').addEventListener('click', () => L.index?.cleanCancel().catch(() => {}));

// ---------------- download index tools: library import (a job: lynxx/dlindex_tools.py import) ----------------
function openImportDialog() {
  if (!L.index) { toast('Bản app này chưa có công cụ nhập thư viện', 'err'); return; }
  let folder = '';
  const pathEl = h('code', { class: 'import-path', title: '' }, 'chưa chọn');
  const modSel = h('select', {}, h('option', { value: '' }, 'Không chọn (liệt kê, không tra)'),
    ...S.modules.filter((m) => m.installed).map((m) => h('option', { value: m.abbr }, `${m.abbr} · ${m.name}`)));
  const recursive = h('input', { type: 'checkbox', checked: true });
  const verify = h('input', { type: 'checkbox' });
  const dry = h('input', { type: 'checkbox' });
  const startBtn = h('button', { class: 'btn primary', disabled: true }, 'Bắt đầu nhập');
  const pick = async () => {
    const p = await L.index.pickImportFolder(); // native dialog in main: the only way to give the import a folder
    if (!p) return;
    folder = p;
    setText(pathEl, p);
    pathEl.title = p;
    startBtn.disabled = false;
  };
  startBtn.addEventListener('click', async () => {
    const opts = { folder, module: modSel.value, recursive: recursive.checked, verifyMd5: verify.checked, dryRun: dry.checked };
    try {
      const job = await addJob((allowDuplicate) => L.index.importLibrary({ ...opts, allowDuplicate }));
      if (!job) return;
      closeModal();
      toast(opts.dryRun ? 'Đã thêm job kiểm tra thư mục' : 'Đã thêm job nhập thư viện: xem trang Tải xuống', 'ok');
      showPage('downloads');
    } catch (err) {
      toast(`Không bắt đầu được: ${errMsg(err)}`, 'err');
    }
  });
  const row = (label, hint, ctl) => h('label', { class: 'setting' }, h('span', {}, label, hint ? h('span', { class: 'muted' }, ` ${hint}`) : null), ctl);
  openModal(h('div', { class: 'import-modal' },
    h('h2', {}, 'Nhập thư viện có sẵn vào index'),
    h('p', { class: 'muted' }, 'Lynxx quét thư mục, nhận module và id của post từ tên file (db_123.jpg, rx_456_…png, __tag__<md5>.jpg của '
      + 'Danbooru), rồi lấy tag, điểm, rating, ngày post và md5 từ website theo lô, đúng giới hạn tốc độ của từng site (Danbooru 100 post '
      + 'mỗi request, site khác 1 post mỗi request, tối đa 2 request mỗi giây). Không đọc nội dung file (trừ khi bật so md5), không đổi '
      + 'tên hay di chuyển file. File đã có trong index được bỏ qua; tên không nhận ra được liệt kê, không đoán.'),
    h('div', { class: 'setting' }, h('span', {}, 'Thư mục'), h('span', { class: 'row' }, pathEl, h('button', { class: 'btn sm', onclick: pick }, 'Chọn…'))),
    row('Module của tên không có tiền tố', '(12345.jpg, <md5>.jpg): chỉ chọn khi cả thư mục là file của một site', modSel),
    row('Quét cả thư mục con', '', recursive),
    row('So md5 với file gốc trên site', '(đọc từng file, chậm hơn; bỏ file đã sửa / ảnh nhẹ)', verify),
    row('Chỉ kiểm tra tên file', '(không gửi request, không ghi index)', dry),
    h('div', { class: 'modal-actions' }, h('button', { class: 'btn ghost', onclick: closeModal }, 'Huỷ'), startBtn)));
}
function openImportLists(j) {
  const p = j.progress || {};
  const block = (title, items, total) => (!total ? null : h('div', { class: 'import-list' },
    h('h3', {}, `${title} (${nf.format(total)})`),
    h('div', { class: 'files-list' }, (items || []).map((t) => h('div', { class: 'files-item' }, h('span', { class: 'files-name', title: t }, t)))),
    total > (items || []).length ? h('p', { class: 'muted' }, `… và ${nf.format(total - (items || []).length)} mục khác: xem Log của job`) : null));
  openModal(h('div', { class: 'files-modal' },
    h('h2', {}, p.dryRun ? 'Kiểm tra thư mục' : 'Kết quả nhập thư viện'), h('p', { class: 'muted lib-path' }, j.dest),
    block('Không nhận ra tên', p.unknownNames, p.unknown),
    block('Không có trên website (đã xoá, bị ẩn hoặc sai module)', p.notFoundItems, p.notFound),
    block('Lỗi khi lấy metadata (chạy lại job để thử tiếp)', p.failedItems, p.failed),
    block('md5 khác file gốc (không nhập)', p.mismatchItems, p.mismatch),
    h('div', { class: 'modal-actions' }, h('button', { class: 'btn ghost', onclick: closeModal }, 'Đóng'))));
}
const importSeen = new Map(); // import job id -> last status: the index views are refreshed once, when the job ends
function onImportJob(job) {
  const prev = importSeen.get(job.id);
  importSeen.set(job.id, job.status);
  if (prev === undefined || prev === job.status || JOB_ACTIVE.has(job.status) || job.progress?.dryRun) return;
  refreshIndexStats();
  if (LIB.ready) runLibrary(LIB.page);
}
$('#index-import').addEventListener('click', () => openImportDialog());
$('#lib-import').addEventListener('click', () => openImportDialog());

// ---------------- job card texts (download, multi-site, import) ----------------
function jobModuleText(j) {
  if (j.kind === 'import') return `Nhập thư viện${j.module && j.module !== '*' ? ` · tên không tiền tố: ${j.module}` : ''}`;
  return `Module: ${j.module}${j.group ? ` · site ${j.group.index + 1}/${j.group.size}` : ''}`;
}
function jobWaitText(j) {
  return j.status === 'queued' && j.group?.index > 0 ? 'Chờ site trước tải xong' : null;
}
function jobFilesText(j, p) {
  if (j.kind !== 'import') return p.totalAll ? `${nf.format(p.processed || 0)} / ${nf.format(p.totalAll)} file` : p.total ? `Tìm thấy ${nf.format(p.total)}` : null;
  if (p.dryRun && p.stateName === 'DONE') {
    const per = Object.entries(p.modules || {}).map(([m, st]) => `${m}: ${nf.format(st.files)}`).join(', ');
    return `${nf.format(p.scanned || 0)} file media, ${nf.format(p.totalAll || 0)} file sẽ được tra${per ? ` (${per})` : ''}`;
  }
  if (p.stateName === 'IMPORT_SCAN' || (!p.totalAll && p.scanned)) return `Đã quét ${nf.format(p.scanned || 0)} file media`;
  return p.totalAll ? `${nf.format(p.processed || 0)} / ${nf.format(p.totalAll)} file · ${nf.format(p.requests || 0)} request` : null;
}
function importCountsText(p) {
  const parts = [p.unknown && `không nhận ra: ${nf.format(p.unknown)}`, p.notFound && `không có trên site: ${nf.format(p.notFound)}`,
    p.mismatch && `md5 khác: ${nf.format(p.mismatch)}`].filter(Boolean);
  return parts.length ? parts.join(' · ') : null;
}

// ---------------- multi-site download: one query, one job per site (jobs:startMulti), md5 dedupe by the index ----------------
const multi = new Set(); // extra modules picked for the current query (not saved)
const multiAvailable = () => typeof L?.jobs?.startMulti === 'function';
function renderMultiSites() {
  const field = $('#f-multi');
  const others = S.modules.filter((m) => m.installed && m.abbr !== S.module);
  for (const abbr of [...multi]) if (!others.some((m) => m.abbr === abbr)) multi.delete(abbr); // also drops the current module
  setHidden(field, !multiAvailable() || !others.length);
  $('#f-multi-pills').replaceChildren(...others.map((m) => h('button', {
    type: 'button', class: `pill${multi.has(m.abbr) ? ' on' : ''}`, title: m.site || m.name,
    onclick: () => { if (!multi.delete(m.abbr)) multi.add(m.abbr); renderMultiSites(); filtersSummary(); },
  }, `${m.abbr} · ${m.name}`)));
  const warn = $('#f-multi-warn');
  if (multi.size) {
    warn.replaceChildren(h('b', {}, `Tải từ ${multi.size + 1} site: ${[S.module, ...multi].join(' → ')}. `),
      'Cùng chuỗi tag được gửi nguyên văn tới từng site, nhưng tên tag, metatag và alias khác nhau giữa các site (rating, tên nhân vật, '
      + 'tag đã đổi tên…): thử truy vấn trên từng site bằng Preview trước. Các site chạy lần lượt; luôn bỏ qua file đã tải và đặt tiền '
      + 'tố module cho tên file, nên post trùng md5 với file site trước đã tải sẽ không tải lại (chỉ với site có báo md5).');
  }
  const conflict = multi.size ? multiKindConflict() : '';
  if (conflict) warn.append(h('b', { class: 'kind-conflict' }, ` ${conflict}`));
  setHidden(warn, !multi.size);
  renderDateFields(); // a picked site may take this query's kind in its own order: no dates then
  setText($('#btn-download'), multi.size ? `Tải xuống (${multi.size + 1} site)` : 'Tải xuống');
}
// A tag of a search kind ('artist:x', 'user:Some.Name') means something only on the sites that declare the same kind (same id,
// same token): any other picked site would search it as a plain tag
function multiKindConflict(q = queryString(), abbr = S.module, others = [...multi]) {
  for (const m of queryKinds(q, abbr)) {
    const missing = others.filter((o) => o !== abbr && !declaredKinds(o).some((k) => k.id === m.kind.id && k.token === m.kind.token));
    if (missing.length) {
      return `Kiểu tìm “${kindLabel(m.kind)}” (${m.kind.token}) không có ở ${missing.map((o) => moduleInfo(o)?.name || o).join(', ')}: `
        + 'bỏ site đó khỏi “Tải thêm từ site khác”, hoặc bỏ tag này khỏi truy vấn.';
    }
  }
  return '';
}
const jobGroups = new Map(); // group id -> view: one box per multi-site group, its jobs inside in site order
const groupJobs = (id) => [...S.jobs.values()].filter((j) => j.group?.id === id).sort((a, b) => a.group.index - b.group.index);
function groupView(id) {
  let v = jobGroups.get(id);
  if (!v) {
    v = { title: h('span', { class: 'job-q' }), info: h('span', { class: 'muted' }), body: h('div', { class: 'job-group-body' }),
      cancel: h('button', { class: 'btn sm danger', onclick: () => cancelGroup(id) }, 'Huỷ cả nhóm') };
    v.root = h('div', { class: 'job-group', dataset: { group: id } },
      h('div', { class: 'job-group-head' }, h('span', { class: 'status' }, 'Nhiều site'), v.title, v.info, h('div', { class: 'job-actions' }, v.cancel)),
      v.body);
    jobGroups.set(id, v);
  }
  return v;
}
function jobSlot(j) {
  // the element that takes the job's place in the list: its own card, or the box of its multi-site group
  const el = jobViews.get(j.id)?.root;
  if (!el || !j.group?.id) return el;
  const g = groupView(j.group.id);
  groupJobs(j.group.id).forEach((x, k) => {
    const xe = jobViews.get(x.id)?.root;
    if (xe && g.body.children[k] !== xe) g.body.insertBefore(xe, g.body.children[k] || null);
  });
  updateGroupView(j.group.id);
  return g.root;
}
function updateGroupView(id) {
  const v = jobGroups.get(id);
  const list = groupJobs(id);
  if (!v || !list.length) return;
  setText(v.title, list[0].tags);
  const ended = list.filter((j) => !JOB_ACTIVE.has(j.status)).length;
  const sum = (k) => list.reduce((n, j) => n + (Number(j.progress?.[k]) || 0), 0);
  setText(v.info, `${list.map((j) => j.module).join(' → ')} · xong ${ended}/${list.length} site · ${nf.format(sum('success'))} file mới`
    + `${sum('known') ? ` · bỏ qua ${nf.format(sum('known'))} file đã có` : ''}`);
  setHidden(v.cancel, !list.some((j) => ['queued', 'running'].includes(j.status)));
}
function cancelGroup(id) {
  // later sites first: a queued site never starts while an earlier one is being cancelled
  groupJobs(id).reverse().filter((j) => ['queued', 'running'].includes(j.status)).forEach((j) => L.jobs.cancel(j.id));
}
function pruneJobGroups() {
  for (const [id, v] of jobGroups) {
    if (!groupJobs(id).length) { v.root.remove(); jobGroups.delete(id); }
  }
}

// ---------------- tag lists status / update ----------------
// sources come from the core (tags.sources, lynxx/tags_updater.py): the reviewed modules (rx, db, xb, bb, en), every installed
// extension with its own update (manifest 'tagUpdate', onDemand: run only from the button here) and every installed extension made
// from an engine with a tag listing (lynxx.extgen writes '$generator.engine' and 'tagList' into its manifest)
const TAG_KEY_NOTE = { required: 'Cần API key của bạn', optional: 'Dùng API key của bạn nếu có' };
const LEARNED_TITLE = 'Site không có danh sách tag dùng được (không có API, hoặc robots.txt cấm): Lynxx học dần từ tag của các post '
  + 'đã xem trong Preview và đã tải, không gửi thêm request. Số post là số post đã thấy ở đây, không phải số trên site.';
const ON_DEMAND_NOTE = 'Job cập nhật của chính extension: chỉ chạy khi bạn bấm Cập nhật, không chạy theo lịch, khi mở app hay khi cài.';
// tags.sources merge: the extension's own job reads only the tags with mergeMinPosts+ posts and the core merges them into the list in
// use (Paheal from 1.0.3, manifest tagUpdate.merge)
const mergeNote = (n) => `Job chỉ đọc các tag có từ ${nf.format(n)} post trở lên rồi gộp vào danh sách đang dùng: các tag đó lấy số post mới, `
  + 'tag mới được thêm, mọi tag khác giữ nguyên.';
// a list shipped inside an extension that has no tag source (tags.status bundled: Rule34Hentai, Rule34.us; Paheal until 1.0.2): the
// core adds the tags learned from previewed / downloaded posts that it lacks (lynxx/tags_learned.py)
const bundledTitle = (v) => `Site không có nguồn danh sách tag mà Lynxx đọc được. Danh sách này đi kèm extension${v ? ` v${v}` : ''} `
  + 'và không tự cập nhật. Tag mới được học thêm từ các post bạn xem trong Preview hoặc tải về (không gửi thêm request).';
const NOT_LEARNED_TITLE = 'Lynxx chưa thấy post nào của site này. Xem Preview hoặc tải vài post của site để Lynxx học tag (không gửi thêm request).';
const fmtListSize = (n) => (n >= 1048576 ? `${(n / 1048576).toFixed(1)} MB` : `${Math.max(1, Math.round(n / 1024))} KB`);
function tagSourceTitle(src) {
  const rule = `${src.onDemand ? `${ON_DEMAND_NOTE} ` : ''}${src.merge ? `${mergeNote(src.mergeMinPosts)} ` : ''}`
    + `${src.curated ? 'Module đã duyệt: API chính thức của site.' : 'Tuân thủ robots.txt của site.'}`;
  const key = TAG_KEY_NOTE[src.key] ? ` ${TAG_KEY_NOTE[src.key]} (${src.keyFormat}).` : '';
  return `${src.site} — ${src.description}. ${rule}${key}`;
}
// updates running in the core, module -> { count, message }: from tags.sources (running, also jobs started by main and after a reload
// of the window) and tags.updateProgress; gone at tags.updateDone. A running row keeps its button off until the job ends
const tagUpdates = new Map();
const tagRunningText = (count) => (count ? `đang cập nhật… ${nf.format(count)} tag` : 'đang cập nhật…');
const tagRunningButton = (message) => h('button', { class: 'btn sm', disabled: true, title: message || '', dataset: { running: '1' } }, 'Đang cập nhật…');
function showTagRunning(m, count = tagUpdates.get(m)?.count, message = tagUpdates.get(m)?.message) {
  tagUpdates.set(m, { count: Number(count) || 0, message: message || '' });
  const cell = $(`#tagupd-${m}`);
  if (cell) { cell.textContent = tagRunningText(count); cell.title = message || ''; }
  const act = $(`#tagact-${m}`);
  const btn = act && $('[data-running]', act);
  if (btn) btn.title = message || '';
  else if (act) act.replaceChildren(tagRunningButton(message));
}
let tagStatusGen = 0;
async function refreshTagStatus() {
  if (!azPage.loaded) { azPage.loaded = true; azPage.reload(); }
  const gen = ++tagStatusGen;
  let rows = [];
  let sources = [];
  try {
    rows = await L.rpc('tags.status');
    S.tagLists = new Set(rows.filter((r) => r.size > 0).map((r) => r.module));
  } catch { /* ignore */ }
  try { sources = await L.rpc('tags.sources'); } catch { /* core without tags.sources: no update buttons */ }
  if (gen !== tagStatusGen) return; // a later call asked the core after this one: its answer is the newer one
  const byMod = Object.fromEntries(rows.map((r) => [r.module, r]));
  const srcByMod = Object.fromEntries((Array.isArray(sources) ? sources : []).map((src) => [src.module, src]));
  for (const src of Object.values(srcByMod)) {
    if (src.running) tagUpdates.set(src.module, { count: Number(src.count) || 0, message: src.message || '' });
    else if (src.running === false) tagUpdates.delete(src.module);
  }
  const installed = S.modules.filter((m) => m.installed);
  const mods = [...installed.filter((m) => srcByMod[m.abbr]), ...installed.filter((m) => !srcByMod[m.abbr])]; // updatable first
  const table = $('#tag-status');
  table.replaceChildren(
    h('tr', {}, h('th', {}, 'Module'), h('th', {}, 'Nguồn'), h('th', {}, 'File'), h('th', {}, 'Dung lượng'), h('th', {}, 'Cập nhật'),
      h('th', {}, '')),
    ...mods.map(({ abbr: m, name, version }) => {
      const r = byMod[m];
      const src = srcByMod[m];
      const keyMissing = src?.key === 'required' && !S.secrets?.modules?.[m];
      const run = src ? tagUpdates.get(m) : undefined;
      const bundled = !src && r?.bundled;
      // shipped: the list in use is still the one inside an extension that has a source (its own job, an engine of the generator),
      // not updated here yet: the 'Cập nhật' button stays, the column gives the extension version like a bundled list
      const shipped = bundled || (!!src && !!r?.shipped);
      const ver = shipped ? r.version || version || '' : '';
      let action;
      if (run) action = tagRunningButton(run.message);
      else if (!src && r && !r.learned && !bundled) action = h('span', { class: 'muted', title: 'Extension này không có nguồn cập nhật: giữ danh sách đi kèm' }, 'không có nguồn');
      else if (!src) {
        action = h('button', { class: 'btn sm', title: 'Đọc tag của các file đã tải (index), không gửi request tới site', onclick: async (e) => {
          e.currentTarget.disabled = true;
          try {
            const res = await L.rpc('tags.learn', { module: m });
            // mayLearn false: the module has a list from a source (or learning is off); indexed false: nothing downloaded yet;
            // bundled: the learned tags go on top of the extension's list (added: those it lacks)
            const onTop = res.bundled ? ` (${nf.format(res.added || 0)} tag chưa có trong danh sách đi kèm)` : '';
            if (res.mayLearn === false) toast(res.sourceList ? `Tag ${m}: đã có danh sách tag từ nguồn, không cần học` : `Tag ${m}: chức năng học tag đang tắt`);
            else if (res.newPosts) toast(`Tag ${m}: học thêm từ ${nf.format(res.newPosts)} post đã tải${onTop}`);
            else if (res.indexed === false) toast(`Chưa có post nào của ${name || m} trong index: tải vài post (hoặc xem Preview) để Lynxx học tag`);
            else toast(`Tag ${m}: không có post đã tải mới`);
            if (res.written) { reresolveTags(m); forgetListTypes(m); } // a chip shown as unknown may be known now
            refreshTagStatus();
            loadTagLists();
          } catch (err) { toast(errMsg(err), 'err'); }
        } }, 'Học từ file đã tải');
      } else if (keyMissing) action = h('span', { class: 'muted', title: `Nhập key ở Cài đặt → API key (${src.keyFormat})` }, 'cần API key');
      else {
        const update = (full) => async (e) => {
          e.currentTarget.disabled = true;
          try {
            await L.tags.update(m, full ? { full: true } : undefined);
            toast(`Đang ${full ? 'đọc lại toàn bộ' : 'cập nhật'} danh sách tag ${m}…`);
          } catch (err) {
            const running = /already running/i.test(errMsg(err));
            toast(running ? `Danh sách tag ${m} đang được cập nhật` : errMsg(err), running ? undefined : 'err');
          }
          refreshTagStatus(); // the core says whether it runs (a job that failed at once has already ended)
        };
        // tail sources (Rule34): 'Cập nhật' reads the last pages only (a full read every 30 days on its own)
        action = src.tailUpdates
          ? h('span', { class: 'row' },
            h('button', { class: 'btn sm', title: 'Chỉ đọc các trang cuối, nơi có tag mới và tag vừa đổi số post (vài phút). Cứ 30 ngày tự đọc lại toàn bộ một lần', onclick: update(false) }, 'Cập nhật'),
            h('button', { class: 'btn sm ghost', title: 'Đọc lại cả danh sách (Rule34: khoảng 1,5–2 giờ; đóng app giữa chừng thì lần sau đọc tiếp)', onclick: update(true) }, 'Đọc lại toàn bộ'))
          : h('button', { class: 'btn sm', title: [src.onDemand && ON_DEMAND_NOTE, src.merge && mergeNote(src.mergeMinPosts)].filter(Boolean).join(' '),
            onclick: update(false) }, 'Cập nhật');
      }
      const written = r?.mtime ? new Date(r.mtime * 1000).toLocaleString('vi-VN') : '';
      // bundled / shipped: the file's time is the build's (installed from the catalog bundled in the app, copied with its time) or
      // the install's (a zip, the remote catalog): neither says how old the data is, so the extension version only. After the
      // first 'Cập nhật' the list in use is the one written here, and its time is the update's (a merge: of the tags it read only)
      const shippedTitle = src?.merge
        ? `Danh sách đi kèm extension, chưa cập nhật ở máy này. Không rõ ngày của dữ liệu.${keyMissing ? '' : ` Bấm "Cập nhật" để lấy số post mới của các tag có từ ${nf.format(src.mergeMinPosts)} post và thêm tag mới; các tag khác giữ nguyên.`}`
        : `Danh sách đi kèm extension, chưa cập nhật ở máy này. Không rõ ngày của dữ liệu.${keyMissing ? '' : ' Bấm "Cập nhật" để đọc danh sách mới từ site.'}`;
      // merged: the core says an update that merges wrote this list (a list of 2tags/, when the app runs from the source, is not one)
      const writtenTitle = src?.merge && r?.merged
        ? `Giờ của lần cập nhật gần nhất. Tag site liệt kê ở mức từ ${nf.format(src.mergeMinPosts)} post: số post của site lúc đó. Tag khác: số post của danh sách trước đó.`
        : '';
      const [updText, updTitle] = run ? [tagRunningText(run.count), run.message || '']
        : shipped ? [`theo extension${ver ? ` v${ver}` : ''}`, bundled
          ? 'Danh sách đổi khi extension có phiên bản mới (trang Extensions). Không rõ ngày của dữ liệu.'
          : shippedTitle]
          : written ? [written, writtenTitle]
            : !src && !r ? ['chưa học tag nào', NOT_LEARNED_TITLE] : ['chưa có', ''];
      // added: learned tags served on top of the list (the core sets it only for a list the learning adds to: a bundled list, or
      // the list of a merge source, shipped or merged)
      const addedTitle = bundled ? 'Tag học từ post đã xem / đã tải mà danh sách đi kèm chưa có'
        : 'Tag học từ post đã xem / đã tải mà danh sách chưa có (thường là tag ít post). Lần "Cập nhật" sau có tag nào thì tag đó lấy số post của site.';
      const added = r?.added ? h('span', { class: 'muted tag-added', title: addedTitle }, ` +${nf.format(r.added)} tag học thêm`) : null;
      return h('tr', {},
        h('td', { title: name || m }, h('span', { class: 'mod-abbr' }, m)),
        h('td', { title: src ? tagSourceTitle(src) : bundled ? bundledTitle(ver) : (r && !r.learned ? '' : LEARNED_TITLE) },
          src ? (src.name || name || m) : bundled ? 'đi kèm extension' : (r && !r.learned ? '—' : 'học từ post đã xem / đã tải')),
        h('td', { title: r?.path || '' }, r ? h('span', { class: 'mono' }, r.path.split(/[\\/]/).slice(-2).join('/')) : '—', added),
        h('td', { title: r?.learned ? 'Danh sách học từ post đã xem / đã tải' : '' }, r ? `${fmtListSize(r.size)}${r.learned ? ' (đã học)' : ''}` : '—'),
        h('td', { id: `tagupd-${m}`, title: updTitle }, updText),
        h('td', { id: `tagact-${m}` }, action));
    }));
  $('#tags-autoupdate').checked = !!S.settings.tags.autoUpdate;
  $('#tags-autoupdate-days').value = S.settings.tags.autoUpdateDays;
}
$('#tags-autoupdate').addEventListener('change', (e) => saveSettings({ tags: { autoUpdate: e.target.checked } }));
$('#tags-autoupdate-days').addEventListener('change', (e) => saveSettings({ tags: { autoUpdateDays: Math.max(1, Number(e.target.value) || 7) } }));

// ---------------- scheduled tag update (system scheduler, main.js tagScheduleStatus) ----------------
function showTagSchedule(st) {
  const on = $('#tags-schedule-on');
  on.checked = !!(st.enabled && st.installed);
  on.disabled = !st.ok;
  $('#tags-schedule-freq').value = st.frequency || 'weekly';
  $('#tags-schedule-freq').disabled = !st.ok;
  // 'auto' (main.js TAG_SCHEDULE_MODULES): the core picks the installed modules with a tag source that need no key when it runs
  const names = (st.modules || []).map((m) => m.name || m.module).filter(Boolean);
  const mods = names.length ? `các module không cần khoá: ${names.join(', ')}` : 'các module đã cài có nguồn tag và không cần khoá';
  const where = st.kind === 'schtasks' ? 'Task Scheduler' : 'crontab của bạn';
  const last = st.lastRun ? new Date(st.lastRun).toLocaleString('vi-VN') : 'chưa chạy lần nào';
  const text = !st.ok ? st.reason
    : st.enabled && st.installed ? `Đã đặt lịch trong ${where}: ${st.frequency === 'daily' ? 'mỗi ngày' : 'thứ Hai hằng tuần'} lúc ${st.time}, cho ${mods}. `
      + `Lần chạy gần nhất: ${last}. Log: ${st.log}.`
      : st.enabled ? `Lịch đã bật nhưng không còn trong ${where} (bị xoá ngoài app?): tắt rồi bật lại để đặt lại.`
        : `Chưa đặt lịch. Khi bật, app thêm một mục vào ${where} cho ${mods}.`;
  $('#tags-schedule-status').textContent = `${text} Extension cài sau cũng được cập nhật theo lịch, trừ extension có job cập nhật riêng: `
    + 'job đó chỉ chạy khi bạn bấm Cập nhật. Module cần khoá (Rule34) không chạy theo lịch: được cập nhật khi mở app (ô ở trên) hoặc bằng nút Cập nhật.';
}
async function renderTagSchedule() {
  if (typeof L.tags?.scheduleStatus !== 'function') { $('#tags-schedule').hidden = true; return; }
  try { showTagSchedule(await L.tags.scheduleStatus()); } catch (err) { $('#tags-schedule-status').textContent = `Không đọc được lịch: ${errMsg(err)}`; }
}
async function saveTagSchedule() {
  try {
    const st = await L.tags.schedule({ enabled: $('#tags-schedule-on').checked, frequency: $('#tags-schedule-freq').value });
    showTagSchedule(st);
    toast(st.enabled ? 'Đã đặt lịch cập nhật danh sách tag' : 'Đã bỏ lịch cập nhật danh sách tag', 'ok');
  } catch (err) {
    toast(`Không đặt được lịch: ${errMsg(err)}`, 'err');
    renderTagSchedule();
  }
}
$('#tags-schedule-on').addEventListener('change', saveTagSchedule);
$('#tags-schedule-freq').addEventListener('change', () => { if ($('#tags-schedule-on').checked) saveTagSchedule(); });

// ---------------- extensions ----------------
// the official signed catalog, also the default of settings.advanced.catalogUrl (main.js): an empty field falls back to it
const OFFICIAL_CATALOG_URL = 'https://github.com/Leiz-Workspace/Lynxx/releases/download/catalog/index.json';
async function renderExtensions() {
  await loadModules();
  $('#ext-grid').replaceChildren(...S.modules.map((m) => extCard(m, false)));
  $('#catalog-url').value = S.settings.advanced.catalogUrl || OFFICIAL_CATALOG_URL;
  renderGen();
}
function extCard(m, remote) {
  const media = (m.media || []).map((x) => h('span', { class: 'tagpill' }, { image: 'ảnh', video: 'video', audio: 'âm thanh' }[x] || x));
  const badges = [
    m.source === 'builtin' ? h('span', { class: 'tagpill acc' }, 'built-in') : null,
    m.default ? h('span', { class: 'tagpill acc' }, 'mặc định') : null,
    m.installed ? h('span', { class: 'tagpill ok' }, 'đã cài') : null,
    m.updateAvailable ? h('span', { class: 'tagpill warn', title: `Catalog có v${m.catalogVersion}` }, `có bản v${m.catalogVersion}`) : null,
    originBadge(m.generator),
    h('span', { class: 'tagpill' }, `v${m.version}`),
    ...trustBadges(m, remote),
  ];
  let action;
  if (m.denied) {
    // domain denylist (rules R6 / R7 / R8): shown with the reason, never installable
    action = h('span', { class: 'muted', title: m.denied.message || '' }, `Không cài được (${m.denied.rule || 'bị chặn'})`);
  } else if (remote) {
    // remote catalog entry: compared with the installed copy of the same extension. Main takes the archive url and sha256 from
    // the catalog the core verified, not from this card
    const local = S.modules.find((x) => x.id === m.id && x.installed);
    const install = () => installExt({ remoteId: m.id }, m.name);
    if (local && local.source !== 'builtin' && versionNewer(m.version, local.version)) {
      action = h('button', { class: 'btn sm primary', onclick: install }, `Cập nhật v${local.version} → v${m.version}`);
    } else if (local) action = h('span', { class: 'muted' }, local.version === m.version ? 'Đã cài bản này' : `Đã cài v${local.version}`);
    else action = h('button', { class: 'btn sm', onclick: install }, 'Cài');
  } else if (m.updateAvailable) {
    action = [h('button', { class: 'btn sm primary', onclick: () => installExt({ id: m.id }, m.name) }, `Cập nhật lên v${m.catalogVersion}`),
      h('button', { class: 'btn sm danger', onclick: () => uninstallExt(m) }, 'Gỡ')];
  } else if (m.source === 'builtin') action = h('span', { class: 'muted' }, 'Luôn bật');
  else if (m.installed) action = h('button', { class: 'btn sm danger', onclick: () => uninstallExt(m) }, 'Gỡ');
  else action = h('button', { class: 'btn sm primary', onclick: () => installExt({ id: m.id }, m.name) }, 'Cài đặt');
  return h('div', { class: 'ext glass' },
    h('div', { class: 'ext-head' }, h('span', { class: 'mod-abbr' }, m.abbr || '?'), h('b', {}, m.name)),
    h('div', { class: 'tags' }, badges, media),
    h('p', {}, m.description || ''),
    m.site ? h('a', { href: '#', class: 'muted', onclick: (e) => { e.preventDefault(); L.app.openExternal(m.site); } }, m.site) : null,
    h('div', { class: 'row' }, action));
}
// signed remote catalog (lynxx/trust.py) and domain denylist badges of an extension card
function trustBadges(m, remote) {
  const out = [];
  if (m.denied) out.push(h('span', { class: 'tagpill err', title: m.denied.message || '' }, 'bị chặn'));
  if (remote && m.signed) out.push(h('span', { class: 'tagpill ok', title: `Catalog có chữ ký hợp lệ, khoá ${m.keyId || '?'}` }, 'đã ký'));
  else if (remote) out.push(h('span', { class: 'tagpill warn', title: 'Catalog chưa ký: chỉ nhận ở chế độ nhà phát triển (LYNXX_CATALOG_ALLOW_UNSIGNED)' }, 'chưa ký'));
  return out;
}
// '1.10.0' > '1.9.2' (numeric parts; same rule as lynxx.extensions.version_newer)
function versionNewer(a, b) {
  const key = (v) => String(v || '').split('-')[0].split('.').map((p) => (/^\d+$/.test(p) ? Number(p) : p));
  const x = key(a);
  const y = key(b);
  for (let i = 0; i < Math.max(x.length, y.length); i++) {
    const p = x[i] ?? 0;
    const q = y[i] ?? 0;
    if (p === q) continue;
    if (typeof p === 'number' && typeof q === 'number') return p > q;
    return String(p) > String(q);
  }
  return false;
}
// generated extensions ('$generator' of the manifest, rule R13): AI or template, with the details in the tooltip
function originBadge(g) {
  if (!g || !g.tool) return null;
  const ai = g.mode === 'ai' || Boolean(g.model);
  const template = !ai && g.mode === 'template';
  const when = g.createdAt ? new Date(g.createdAt).toLocaleDateString('vi-VN') : '';
  const how = ai ? `Tạo bằng AI (${g.model || 'Claude'})` : template ? 'Tạo từ template có sẵn của Lynxx'
    : 'Tạo bằng công cụ extgen (skill / lập trình viên)';
  const title = [how, g.engine && `engine ${g.engine}`, when && `ngày ${when}`, g.tool].filter(Boolean).join(' · ');
  return h('span', { class: `tagpill ${ai ? 'warn' : ''}`, title }, ai ? 'AI' : template ? 'template' : 'extgen');
}
async function installExt(params, name) {
  try {
    const r = params.id ? await L.ext.installCatalog(params.id) : await L.ext.installRemote({ id: params.remoteId, name });
    toast(r?.restartRequired ? `Đã cài ${name}. Khởi động lại app để dùng phiên bản mới.` : `Đã cài ${name}`, 'ok');
    renderExtensions();
  } catch (err) { if (!/cancelled/.test(errMsg(err))) toast(`Cài thất bại: ${errMsg(err)}`, 'err'); }
}
async function uninstallExt(m) {
  if (!window.confirm(`Gỡ extension ${m.name}?`)) return;
  try {
    await L.rpc('ext.uninstall', { id: m.id });
    toast(`Đã gỡ ${m.name}. Khởi động lại để giải phóng hoàn toàn.`, 'ok');
    renderExtensions();
  } catch (err) { toast(errMsg(err), 'err'); }
}
$('#ext-refresh').addEventListener('click', renderExtensions);
$('#ext-zip').addEventListener('click', async () => {
  const file = await L.dialogs.zip();
  if (!file) return;
  const v = await prompt2('Cài extension từ file .zip', [{ label: 'SHA-256 của file (bắt buộc, lấy từ nơi phát hành)', placeholder: 'e3b0c442…' }]);
  if (!v) return;
  try {
    // an update of an extension this app run already loaded takes effect after a restart (the catalog installs say so too)
    const r = await L.ext.installZip(file, v[0]);
    const name = r?.name ? `${r.name} v${r.version}` : 'extension';
    toast(r?.restartRequired ? `Đã cài ${name}. Khởi động lại app để dùng phiên bản mới.` : `Đã cài ${name}`, 'ok');
    renderExtensions();
  } catch (err) { if (!/cancelled/.test(errMsg(err))) toast(errMsg(err), 'err'); }
});
$('#catalog-load').addEventListener('click', async () => {
  const url = $('#catalog-url').value.trim();
  if (!url) return;
  await saveSettings({ advanced: { catalogUrl: url } });
  try {
    const items = await L.rpc('ext.remoteCatalog', { url });
    $('#remote-grid').replaceChildren(...items.map((m) => extCard({ ...m, media: m.media || [] }, true)));
    if (!items.length) toast('Catalog trống');
  } catch (err) { toast(`Không tải được catalog: ${errMsg(err)}`, 'err'); }
});

// ---------------- extension generator ----------------
// A session turns a site link into a draft (manifest.json, downloader.py, test fixtures, report): it never installs.
// "Cài vào app…" asks main, which re-checks the draft offline, packs it and confirms with a native dialog.
const GEN_RULES = {
  R1: 'chỉ truy cập công khai hoặc API chính thức bằng khoá của bạn',
  R2: 'robots.txt của site không cho phép',
  R3: 'điều khoản của site cấm tải tự động',
  R4: 'giới hạn tốc độ / số yêu cầu tới site',
  R5: 'site có chống bot (Cloudflare…): Lynxx không vượt qua',
  R6: 'không tạo link cho bài bị ẩn / giới hạn',
  R7: 'không hỗ trợ DRM / luồng mã hoá',
  R8: 'nội dung bất hợp pháp',
  R9: 'bộ lọc an toàn của Lynxx phải giữ nguyên',
  R10: 'chỉ https, địa chỉ công khai, đúng site đó; code chỉ dùng thư viện cho phép',
  'model-refusal': 'model từ chối yêu cầu',
  budget: 'hết ngân sách chi phí / lượt gọi model',
};
const GEN_STATUS = {
  ready: ['sẵn sàng cài', 'ok'], failed: ['chưa đạt', 'err'], refused: ['bị từ chối', 'warn'], cancelled: ['đã huỷ', ''],
  error: ['lỗi', 'err'], unknown: ['không rõ', ''],
};
const G = { mode: 'ai', running: false, key: null, session: '', restored: false };

function genStatusPill(status) {
  const [label, kind] = GEN_STATUS[status] || [status, ''];
  return h('span', { class: `tagpill ${kind}` }, label);
}
function setGenMode(mode) {
  G.mode = mode === 'template' ? 'template' : 'ai';
  $$('#gen-mode button').forEach((b) => b.classList.toggle('on', b.dataset.mode === G.mode));
  $('#gen-budget-wrap').hidden = G.mode !== 'ai';
  $('#gen-hint').textContent = G.mode === 'ai'
    ? 'Claude khảo sát site, viết / sửa downloader.py khi site không có template sẵn và chạy thử tới khi đạt. Tính phí Claude API theo lượng dùng, dừng khi chạm mức tối đa.'
    : 'Miễn phí, không cần khoá: cho site chạy Gelbooru 0.2 (DAPI), Danbooru, e621, Moebooru, Philomena, Szurubooru hoặc Shimmie (có API danbooru), dùng template có sẵn của Lynxx.';
  renderGenNote();
}
function renderGenNote() {
  const note = $('#gen-note');
  if (G.mode === 'ai' && G.key && !G.key.set) {
    note.replaceChildren('Chưa có khoá Claude API. ',
      h('a', { href: '#', onclick: (e) => { e.preventDefault(); showPage('settings'); $('#ai-key').focus(); } }, 'Thêm trong Cài đặt → AI tạo extension'),
      ', hoặc chọn "Không dùng AI" cho site Gelbooru, Danbooru, e621, Moebooru, Philomena, Szurubooru, Shimmie.');
    note.hidden = false;
  } else note.hidden = true;
}
function genSetRunning(running) {
  G.running = running;
  $('#gen-start').disabled = running;
  $('#gen-cancel').hidden = !running;
  if (running) $('#gen-dot').className = 'dot busy';
}
function genAppend(text, cls = '') {
  const log = $('#gen-log');
  const atEnd = log.scrollTop + log.clientHeight >= log.scrollHeight - 8;
  log.append(h('span', { class: cls }, `${text}\n`));
  while (log.childNodes.length > 400) log.firstChild.remove();
  if (atEnd) log.scrollTop = log.scrollHeight;
}
function onGenEvent(ev, replay = false) {
  if (!ev || typeof ev.type !== 'string') return;
  if (ev.type === 'start') {
    G.session = ev.session || '';
    $('#gen-run').hidden = false;
    if (!replay) $('#gen-log').replaceChildren();
    $('#gen-phase').textContent = 'Đang bắt đầu…';
    $('#gen-usage').textContent = '';
    genAppend(`Phiên ${ev.session} · ${ev.url} · ${ev.mode === 'ai' ? `AI (${ev.model})` : 'không dùng AI'}`);
  } else if (ev.type === 'phase') {
    $('#gen-phase').textContent = ev.state === 'writing' ? `Claude đang soạn lệnh: ${ev.label}…` : `${ev.label}…`;
  } else if (ev.type === 'log') {
    const model = ev.source === 'model';
    genAppend(model ? `Claude: ${ev.text}` : `• ${ev.text}`, model ? 'model' : ev.error ? 'err' : '');
  } else if (ev.type === 'usage') {
    $('#gen-usage').textContent = `$${Number(ev.usd || 0).toFixed(2)} / $${Number(ev.maxUsd || 0).toFixed(2)} · ${ev.turns} lượt`;
  } else if (ev.type === 'done') {
    const [label, kind] = GEN_STATUS[ev.status] || [ev.status, ''];
    $('#gen-run').hidden = false;
    $('#gen-phase').textContent = `Kết thúc: ${label}`;
    $('#gen-dot').className = `dot ${kind === 'ok' ? 'ok' : kind === 'err' ? 'err' : ''}`;
    if (ev.summary) genAppend(ev.summary, kind === 'err' ? 'err' : '');
    if (!replay) {
      toast(ev.status === 'ready' ? 'Đã tạo bản nháp extension: xem lại rồi bấm "Cài vào app…"' : `Tạo extension: ${label}`, ev.status === 'ready' ? 'ok' : 'err');
      renderGenDrafts();
    }
  } else if (ev.type === 'exit') {
    genSetRunning(false);
    renderGenDrafts();
  }
}
async function renderGen() {
  if (!L.gen) { $('#gen-panel').hidden = true; return; }
  try {
    const st = await L.gen.status();
    G.key = st.key;
    if (!$('#gen-budget').value) $('#gen-budget').value = st.defaults?.maxUsd ?? 2;
    setGenMode(G.mode);
    genSetRunning(st.running);
    if (!G.restored && st.events?.length) { // renderer reloaded: rebuild the log of the last / running session
      $('#gen-log').replaceChildren();
      st.events.forEach((e) => onGenEvent(e, true));
      if (st.running) genSetRunning(true);
    }
    G.restored = true;
  } catch (err) { toast(`Trình tạo extension: ${errMsg(err)}`, 'err'); }
  renderGenDrafts();
}
async function renderGenDrafts() {
  if (!L.gen) return;
  let items = [];
  try { items = await L.gen.list(); } catch (err) { toast(`Không đọc được bản nháp: ${errMsg(err)}`, 'err'); }
  $('#gen-drafts').replaceChildren(...(items.length ? items.map(genDraftRow) : [h('p', { class: 'muted' }, 'Chưa có bản nháp nào.')]));
}
function genDraftRow(d) {
  const at = d.finishedAt || d.startedAt;
  const when = at ? new Date(at).toLocaleString('vi-VN') : d.session;
  const running = G.running && G.session === d.session;
  const act = (label, fn, cls = 'btn sm ghost', title = null) => h('button', { class: cls, onclick: fn, title }, label);
  const meta = [d.site || d.url, when, d.mode === 'ai' ? `$${Number(d.usd || 0).toFixed(2)}` : d.mode === 'template' ? 'không AI' : ''];
  return h('div', { class: 'gen-draft' },
    h('div', { class: 'gen-draft-head' },
      d.abbr ? h('span', { class: 'mod-abbr' }, d.abbr) : null,
      h('b', {}, d.name || d.url || d.session),
      running ? h('span', { class: 'tagpill acc' }, 'đang chạy') : genStatusPill(d.status),
      d.engine ? h('span', { class: 'tagpill' }, d.engine) : null,
      h('span', { class: 'muted' }, meta.filter(Boolean).join(' · '))),
    d.summary ? h('p', {}, d.summary.length > 400 ? `${d.summary.slice(0, 400)}…` : d.summary) : null,
    h('div', { class: 'row' },
      act('Chi tiết', () => showGenDetail(d.session)),
      d.draftDir ? act('Mở thư mục', () => L.gen.open(d.session)) : null,
      d.draftDir && !running ? act('Xuất .zip…', () => exportGenDraft(d)) : null,
      d.status === 'ready' && d.draftDir && !running ? act('Cài vào app…', () => installGenDraft(d), 'btn sm primary') : null,
      d.status !== 'ready' && d.status !== 'refused' && d.draftDir && !running && !G.running
        ? act('Sửa bằng AI', () => repairGenDraft(d), 'btn sm', 'Claude đọc lỗi lần trước và sửa tiếp bản nháp (bản cũ giữ nguyên)') : null,
      running ? null : act('Xoá', () => removeGenDraft(d), 'btn sm ghost danger')));
}
async function exportGenDraft(d) {
  try {
    const r = await L.gen.exportZip(d.session);
    if (!r) return;
    const copy = () => navigator.clipboard?.writeText(r.sha256).then(() => toast('Đã chép SHA-256', 'ok'), () => {});
    openModal(h('div', { class: 'gen-detail' },
      h('h2', { text: 'Đã xuất extension' }),
      h('p', {}, r.path),
      h('p', { class: 'muted' }, 'SHA-256 (cần khi cài bằng "Cài từ file .zip…" hoặc chia sẻ cho người khác):'),
      h('pre', {}, r.sha256),
      h('div', { class: 'modal-actions' }, h('button', { class: 'btn ghost', onclick: copy }, 'Chép SHA-256'),
        h('button', { class: 'btn primary', onclick: closeModal }, 'Đóng'))));
  } catch (err) { toast(`Không xuất được: ${errMsg(err)}`, 'err'); }
}
async function repairGenDraft(d) {
  if (G.key && !G.key.set) { toast('Cần khoá Claude API: thêm trong Cài đặt → AI tạo extension', 'err'); return; }
  genSetRunning(true);
  $('#gen-run').hidden = false;
  $('#gen-log').replaceChildren();
  $('#gen-usage').textContent = '';
  $('#gen-phase').textContent = `Đang khởi động sửa bản nháp ${d.name || d.extId}…`;
  $('#gen-panel').scrollIntoView({ behavior: 'smooth', block: 'start' });
  try {
    await L.gen.start({ repair: d.session, maxUsd: Number($('#gen-budget').value) || undefined });
  } catch (err) {
    genSetRunning(false);
    $('#gen-dot').className = 'dot err';
    $('#gen-phase').textContent = 'Không bắt đầu được';
    toast(errMsg(err), 'err');
  }
}
async function installGenDraft(d) {
  try {
    const r = await L.gen.install(d.session);
    toast(r?.restartRequired ? `Đã cài ${d.name}. Khởi động lại app để dùng phiên bản mới.` : `Đã cài ${d.name || d.extId}`, 'ok');
    renderExtensions();
  } catch (err) { if (!/cancelled/.test(errMsg(err))) toast(`Cài thất bại: ${errMsg(err)}`, 'err'); }
}
async function removeGenDraft(d) {
  try {
    if (await L.gen.remove(d.session)) { toast('Đã xoá bản nháp', 'ok'); renderGenDrafts(); }
  } catch (err) { toast(errMsg(err), 'err'); }
}
async function showGenDetail(session) {
  let d;
  try { d = await L.gen.get(session); } catch (err) { toast(errMsg(err), 'err'); return; }
  const mark = (c) => (c.ok ? '✓' : c.severity === 'warning' ? '!' : '✗');
  const checks = (d.checks || []).map((c) => h('tr', {}, h('td', {}, `${mark(c)} ${c.id}`), h('td', {}, String(c.detail || '').slice(0, 400))));
  const rules = (d.ruleIds || []).map((r) => `${r}: ${GEN_RULES[r] || ''}`);
  const u = d.usage || {};
  const info = [
    ['Site', d.site || d.url || '—'],
    ['Extension', d.extId ? `${d.name} (${d.extId} · ${d.abbr}) v${d.version}` : '—'],
    ['Engine', d.engine || '—'],
    ['Cách tạo', d.mode === 'ai' ? `AI · ${d.model}` : 'template có sẵn, không AI'],
    ['Chạy thử', `${d.testRuns || 0} lần · ${d.requests || 0} yêu cầu tới site`],
    d.mode === 'ai' ? ['Chi phí', `$${Number(d.usd || 0).toFixed(3)} · ${nf.format(u.input || 0)} token vào · ${nf.format(u.output || 0)} token ra · `
      + `${nf.format(u.cacheRead || 0)} token cache · ${d.turns || 0} lượt`] : null,
    ['Tệp', (d.files || []).join(', ') || '—'],
    d.error ? ['Lỗi', d.error] : null,
  ].filter(Boolean);
  openModal(h('div', { class: 'gen-detail' },
    h('h2', {}, `${d.name || d.url || session} `, genStatusPill(d.status)),
    d.summary ? h('pre', {}, d.summary) : null,
    rules.length ? h('div', { class: 'notice' }, `Quy tắc: ${rules.join('; ')}`) : null,
    h('table', { class: 'table' }, info.map(([k, v]) => h('tr', {}, h('th', {}, k), h('td', {}, v)))),
    checks.length ? h('h3', {}, 'Kiểm tra') : null,
    checks.length ? h('table', { class: 'table' }, checks) : null,
    h('div', { class: 'modal-actions' }, h('button', { class: 'btn primary', onclick: closeModal }, 'Đóng'))));
}
$('#gen-mode').addEventListener('click', (e) => { const b = e.target.closest('button'); if (b) setGenMode(b.dataset.mode); });
$('#gen-url').addEventListener('keydown', (e) => { if (e.key === 'Enter' && !G.running) $('#gen-start').click(); });
$('#gen-start').addEventListener('click', async () => {
  const url = $('#gen-url').value.trim();
  if (!url) { toast('Nhập link website', 'err'); $('#gen-url').focus(); return; }
  const media = ['image', 'video', 'audio'].filter((m) => $(`#gen-m-${m}`).checked);
  if (!media.length) { toast('Chọn ít nhất một loại media', 'err'); return; }
  genSetRunning(true);
  $('#gen-run').hidden = false;
  $('#gen-log').replaceChildren();
  $('#gen-usage').textContent = '';
  $('#gen-phase').textContent = 'Đang khởi động…';
  try {
    await L.gen.start({ url, mode: G.mode, media, query: $('#gen-query').value.trim(), maxUsd: Number($('#gen-budget').value) || undefined });
  } catch (err) {
    genSetRunning(false);
    $('#gen-dot').className = 'dot err';
    $('#gen-phase').textContent = 'Không bắt đầu được';
    toast(errMsg(err), 'err');
  }
});
$('#gen-cancel').addEventListener('click', async () => {
  try { if (await L.gen.cancel()) $('#gen-phase').textContent = 'Đang huỷ…'; } catch (err) { toast(errMsg(err), 'err'); }
});

// Settings → AI tạo extension: the key is write-only (main stores it encrypted, the renderer only learns whether one is set)
function renderAiSettings() {
  if (!L.gen) { $('#ai-key').closest('.panel').hidden = true; return; }
  const st = S.secrets?.ai || { set: false, encryption: false };
  $('#ai-key-note').textContent = st.encryption
    ? 'Khoá được mã hoá bằng keyring của hệ điều hành và không hiện lại trên giao diện; chỉ tiến trình tạo extension nhận được khoá.'
    : 'Không có keyring hệ điều hành: Lynxx không lưu khoá Claude API dạng chữ thường. Chế độ "Không dùng AI" vẫn dùng được.';
  $('#ai-key-state').replaceChildren(st.set ? h('span', { class: 'tagpill ok' }, 'đã lưu (mã hoá)') : '');
  $('#ai-key').placeholder = st.set ? 'Khoá mới (trống = giữ nguyên)' : 'sk-ant-…';
  $('#ai-key').disabled = !st.encryption;
  $('#ai-key-save').disabled = !st.encryption;
  $('#ai-key-clear').hidden = !st.set;
  $('#ai-model').value = S.settings.ai?.model || 'claude-opus-5-5';
  $('#ai-budget').value = S.settings.ai?.maxUsd ?? 2;
}
function onAiKeyStatus(st) {
  S.secrets = { ...S.secrets, ai: st };
  G.key = st;
  renderAiSettings();
  renderGenNote();
}
$('#ai-key-save').addEventListener('click', async () => {
  const v = $('#ai-key').value.trim();
  if (!v) { toast(S.secrets?.ai?.set ? 'Ô trống: khoá đã lưu được giữ nguyên.' : 'Nhập khoá trước khi lưu.', S.secrets?.ai?.set ? '' : 'err'); return; }
  try {
    onAiKeyStatus(await L.gen.setKey(v));
    $('#ai-key').value = '';
    toast('Đã lưu khoá Claude API (mã hoá)', 'ok');
  } catch (err) { toast(`Không lưu được khoá: ${errMsg(err)}`, 'err'); }
});
$('#ai-key').addEventListener('keydown', (e) => { if (e.key === 'Enter') $('#ai-key-save').click(); });
$('#ai-key-clear').addEventListener('click', async () => {
  if (!window.confirm('Xoá khoá Claude API khỏi máy? Khoá không khôi phục được.')) return;
  try { onAiKeyStatus(await L.gen.clearKey()); toast('Đã xoá khoá Claude API', 'ok'); } catch (err) { toast(errMsg(err), 'err'); }
});
$('#ai-model').addEventListener('change', (e) => saveSettings({ ai: { model: e.target.value } }));
$('#ai-budget').addEventListener('change', async (e) => {
  await saveSettings({ ai: { maxUsd: Number(e.target.value) || 2 } });
  $('#ai-budget').value = S.settings.ai.maxUsd;
  $('#gen-budget').value = S.settings.ai.maxUsd;
});

// ---------------- settings ----------------
async function saveSettings(patch) {
  const r = await L.settings.set(patch);
  S.settings = r.settings;
  S.secrets = r.secrets;
  return r;
}
function renderAppearanceSettings() {
  const a = S.appearance;
  if (!a) return;
  const names = { ember: 'Ember', graphite: 'Graphite', paper: 'Paper', oled: 'OLED' };
  const swatch = { ember: 'linear-gradient(135deg,#1a1512 55%,#ff6a1a 55%)', graphite: 'linear-gradient(135deg,#18181a 55%,#e8e8e8 55%)',
    paper: 'linear-gradient(135deg,#fffcf8 55%,#e2570b 55%)', oled: 'linear-gradient(135deg,#000 55%,#ff6a1a 55%)' };
  $('#theme-row').replaceChildren(...a.themes.map((t) => h('button', {
    class: `theme-sw ${t === a.theme ? 'on' : ''}`, style: `background:${swatch[t]}`,
    onclick: () => saveSettings({ appearance: { theme: t } }),
  }, h('span', { class: 'sw-label' }, names[t] || t))));
  $('#icon-row').replaceChildren(...a.icons.map((ic) => h('button', {
    class: `icon-sw ${ic.id === a.icon ? 'on' : ''}`, title: ic.name,
    onclick: async () => {
      const r = await saveSettings({ appearance: { icon: ic.id } });
      showIconNotice(r.iconSync, r.appearance);
    },
  }, h('img', { src: ic.url, alt: ic.name }))));
  const bg = a.background;
  $$('#bg-mode button').forEach((b) => b.classList.toggle('on', b.dataset.mode === bg.mode));
  $('#bg-image-row').hidden = bg.mode !== 'image';
  $('#bg-blur-row').hidden = bg.mode !== 'icon';
  $('#bg-image-name').textContent = bg.image ? bg.image.split(/[\\/]/).pop() : 'chưa chọn';
  $('#bg-blur').value = bg.blur;
  $('#bg-opacity').value = Math.round((bg.opacity ?? 0.9) * 100);
  $('#bg-dim-range').value = Math.round((bg.dim ?? 0.18) * 100);
}
// sync: what main updated outside the window (iconSync of settings:set): launcher 'updated' | 'unchanged' | 'missing' |
// 'foreign' | 'unsupported', shortcuts (Windows), restartForDock (Wayland reads the launcher when the app starts)
function showIconNotice(sync, a) {
  const n = $('#icon-notice');
  const done = [];
  if (sync?.launcher === 'updated') done.push('launcher');
  if (sync?.shortcuts) done.push(`${sync.shortcuts} shortcut`);
  if (sync?.error) toast(`Không cập nhật được icon ngoài app: ${sync.error}`, 'err');
  const relaunch = h('button', { class: 'btn sm primary', onclick: () => L.app.relaunch() }, 'Khởi động lại ngay');
  if (a?.liveTaskbarIcon !== false) {
    n.hidden = true;
    toast(`Đã đổi icon${done.length ? ` (cả ${done.join(', ')})` : ''}`, 'ok');
    return;
  }
  // Wayland: the dock / taskbar icon comes from the launcher (lynxx.desktop) only
  n.hidden = false;
  if (sync?.launcher === 'missing') {
    n.replaceChildren(
      h('div', {}, 'Icon trong app đã đổi. Trên Wayland, dock/taskbar lấy icon từ launcher (lynxx.desktop) nhưng máy chưa có launcher của Lynxx, '
        + 'nên dock đang hiện icon chung. Tạo launcher (Lynxx sẽ tự cập nhật nó mỗi khi bạn đổi icon) rồi mở lại app.'),
      h('div', { class: 'row' },
        h('button', {
          class: 'btn sm', onclick: async () => {
            const r = await L.app.installLauncher();
            toast(r.ok ? `Đã tạo launcher: ${r.path}` : r.message, r.ok ? 'ok' : 'err');
            if (r.ok) showIconNotice({ launcher: 'updated', restartForDock: true }, a);
          },
        }, 'Tạo launcher'), relaunch));
  } else if (sync?.launcher === 'foreign') {
    n.replaceChildren(
      h('div', {}, 'Icon trong app đã đổi. Launcher lynxx.desktop trên máy do công cụ khác tạo (ví dụ công cụ tích hợp AppImage) nên Lynxx không sửa nó; '
        + 'dock trên Wayland vẫn hiện icon của launcher đó.'));
  } else {
    n.replaceChildren(
      h('div', {}, 'Icon trong app và launcher đã đổi. Dock trên Wayland chỉ đọc launcher khi app mở: khởi động lại để dock hiện icon mới.'),
      h('div', { class: 'row' }, relaunch));
  }
}
$('#bg-mode').addEventListener('click', async (e) => {
  const mode = e.target.dataset.mode;
  if (!mode) return;
  if (mode === 'image' && !S.appearance.background.image) {
    const file = await L.dialogs.image();
    if (!file) return;
    await L.appearance.setBackgroundImage(file);
    return;
  }
  saveSettings({ appearance: { background: { mode } } });
});
$('#bg-pick').addEventListener('click', async () => {
  const file = await L.dialogs.image();
  if (file) await L.appearance.setBackgroundImage(file);
});
const saveBg = debounce((patch) => saveSettings({ appearance: { background: patch } }), 150);
$('#bg-blur').addEventListener('input', (e) => { document.documentElement.style.setProperty('--bg-blur', `${e.target.value}px`); saveBg({ blur: Number(e.target.value) }); });
$('#bg-opacity').addEventListener('input', (e) => { document.documentElement.style.setProperty('--bg-opacity', String(e.target.value / 100)); saveBg({ opacity: e.target.value / 100 }); });
$('#bg-dim-range').addEventListener('input', (e) => { document.documentElement.style.setProperty('--bg-dim', String(e.target.value / 100)); saveBg({ dim: e.target.value / 100 }); });
$('#safe-preview').addEventListener('change', (e) => { document.body.classList.toggle('safe', e.target.checked); saveSettings({ search: { safePreview: e.target.checked } }); });

function bindSetting(id, path, kind = 'checkbox') {
  const el = $(id);
  const keys = path.split('.');
  let v = S.settings;
  for (const k of keys) v = v?.[k];
  if (kind === 'checkbox') el.checked = !!v; else el.value = v ?? '';
  el.addEventListener('change', () => {
    let val = kind === 'checkbox' ? el.checked : kind === 'number' ? Number(el.value) || 0 : el.value;
    const patch = {};
    let cur = patch;
    keys.forEach((k, i) => { cur[k] = i === keys.length - 1 ? val : {}; cur = cur[k]; });
    saveSettings(patch);
    val = null;
  });
}
// gender image model: a model server (lynxx/model_server.py) the app calls; address in settings, token kept by main
function renderGenderModelToken() {
  const st = S.secrets?.genderModel || { set: false, encrypted: false, encryption: false };
  $('#gm-token-state').replaceChildren(st.set ? h('span', { class: 'tagpill ok' }, st.encrypted ? 'đã lưu (mã hoá)' : 'đã lưu') : '');
  $('#gm-token').placeholder = st.set ? 'Token mới (trống = giữ nguyên)' : 'không cần khi server chạy trên máy này';
  $('#gm-token-clear').hidden = !st.set;
}
// on: turned on in Settings (after the GPU / VRAM check) and an address set; off: the gender filter goes by tags
const gmOn = () => S.settings?.genderModel?.enabled === true && Boolean(S.settings?.genderModel?.url);
const GM_DEVICE = { cuda: 'GPU (CUDA)', rocm: 'GPU (ROCm)', migraphx: 'GPU (MIGraphX)', directml: 'GPU (DirectML)', cpu: 'CPU' };
const gmGb = (mib) => `${(Number(mib) / 1024).toLocaleString('vi-VN', { maximumFractionDigits: 1 })} GB`;
function gmHardwareText(st) {
  const hw = st?.model?.hardware;
  if (!hw) return st?.reachable ? 'server không báo phần cứng (bản cũ)' : '—';
  const gpus = (hw.gpus || []).map((g) => (Number.isInteger(g.vramMiB)
    ? `${g.name}: VRAM ${gmGb(g.vramMiB)}${Number.isInteger(g.vramFreeMiB) ? `, trống ${gmGb(g.vramFreeMiB)}` : ''}`
    : g.shared ? `${g.name} (dùng chung RAM)`
      : `${g.name} (không đọc được VRAM${g.vendor === 'nvidia' ? ': server không có nvidia-smi' : ''})`));
  const rt = st.check?.runtime;
  return [gpus.length ? gpus.join(' · ') : 'không có GPU',
    `onnxruntime ${hw.runtime || '?'} ${rt ? `có ${GM_DEVICE[rt] || rt}` : 'bản CPU'}`,
    Number.isInteger(hw.ramFreeMiB) ? `RAM trống ${gmGb(hw.ramFreeMiB)}` : ''].filter(Boolean).join(' · ');
}
// the hardware check of the server (lynxx/model_hw.py hardware_check) as words: null when the model can use a GPU
function gmWarning(check) {
  if (!check || check.level === 'ok') return null;
  const g = check.gpu;
  const free = g && Number.isInteger(g.vramFreeMiB) ? gmGb(g.vramFreeMiB) : '';
  const ram = Number.isInteger(check.ramFreeMiB) ? ` (RAM trống ${gmGb(check.ramFreeMiB)})` : '';
  const cpu = `model sẽ chạy bằng CPU: mỗi ảnh khoảng 0,3–1 giây và chiếm khoảng 550 MB RAM${ram} khi đang nạp.`;
  switch (check.code) {
    case 'no_gpu': return { title: 'Máy chạy model không có VRAM', guide: 'hardware',
      text: `Server không thấy GPU nào có VRAM riêng (từ ${gmGb(check.vramMinMiB || 1536)}), nên ${cpu}`,
      advice: 'Nên để tắt nếu máy yếu, ít RAM hoặc đang chạy pin. Lọc theo tag vẫn hoạt động bình thường.' };
    case 'gpu_no_runtime': return { title: 'Có GPU nhưng model chưa dùng được VRAM', guide: 'gpu',
      text: `Server có ${g?.name || 'GPU'}${free ? ` (${free} VRAM trống)` : ''} nhưng onnxruntime đang cài là bản CPU, nên ${cpu}`,
      advice: 'Nên để tắt cho tới khi cài onnxruntime-gpu (xem Hướng dẫn → Dùng GPU).' };
    case 'cpu_forced': return { title: 'Server được đặt chạy bằng CPU', guide: 'gpu',
      text: `Server có ${g?.name || 'GPU'}${free ? ` (${free} VRAM trống)` : ''} nhưng được chạy với --device cpu (hoặc LYNXX_GENDER_MODEL_DEVICE=cpu), nên ${cpu}`,
      advice: 'Bỏ tuỳ chọn này rồi khởi động lại server để dùng GPU, hoặc để tắt.' };
    case 'vram_low': return { title: 'GPU không đủ VRAM trống', guide: 'hardware',
      text: `${g?.name || 'GPU'} chỉ còn ${free} VRAM trống, model cần khoảng ${gmGb(check.vramMinMiB || 1536)}.`,
      advice: 'Đóng bớt ứng dụng đang dùng GPU (game, AI…) rồi bấm Kiểm tra lại, hoặc để tắt.' };
    case 'gpu_fallback': return { title: 'Model đang chạy bằng CPU dù server có GPU', guide: 'gpu',
      text: `onnxruntime có GPU nhưng không nạp được (thiếu thư viện CUDA / cuDNN hoặc driver), nên ${cpu}`,
      advice: 'Xem Hướng dẫn → Dùng GPU, hoặc tắt model.' };
    default: return { title: 'Không biết phần cứng của server', guide: 'server',
      text: 'Model server là bản cũ, không báo GPU / VRAM.', advice: 'Cập nhật model server rồi bấm Kiểm tra lại.' };
  }
}
function renderGenderModelStatus(st) {
  const m = st?.model || {};
  const loaded = m.loaded ? `model đang nạp trên ${GM_DEVICE[m.device] || m.device || '?'}` : 'model chưa nạp (nạp khi cần)';
  const text = !st ? 'tắt (chưa có địa chỉ)' : st.reachable
    ? [`kết nối được · ${m.id} (${m.license}) · ${m.ms} ms`, m.loaded === undefined ? '' : loaded,
      st.error ? `lỗi: ${st.error}` : '', st.local ? '' : 'ảnh của post được gửi tới máy chủ này'].filter(Boolean).join(' · ')
    : `không kết nối được: ${st.error}`;
  setText($('#gm-status'), text);
  $('#gm-status').classList.toggle('err', Boolean(st && (!st.reachable || st.error)));
  setText($('#gm-hw'), gmHardwareText(st));
  const warn = st?.reachable ? gmWarning(st.check) : null;
  const box = $('#gm-warn');
  box.hidden = !warn;
  if (warn) {
    box.replaceChildren(h('b', {}, warn.title), ' ', warn.text, ' ', gmOn() ? warn.advice : 'Bật model lúc này sẽ cần xác nhận.', ' ',
      h('button', { class: 'btn sm ghost', onclick: () => openGuide(warn.guide) }, 'Hướng dẫn'));
  }
}
async function refreshGenderModel() {
  if (!L.genderModel) { $('#gm-url').closest('.panel').hidden = true; return; }
  const url = S.settings.genderModel?.url || '';
  if (document.activeElement !== $('#gm-url')) $('#gm-url').value = url;
  $('#gm-enabled').checked = gmOn();
  renderGenderModelToken();
  const opt = $('#f-gender-mode option[value="model"]');
  opt.disabled = !gmOn();
  opt.textContent = gmOn() ? 'Tag + model (ảnh)' : 'Tag + model (bật model trong Cài đặt)';
  if (!gmOn() && genderState().mode === 'model') saveGender({ mode: 'tags' });
  else renderGenderFilter(); // the media check box follows the model switch
  if (!url) { S.genderModel = null; renderGenderModelStatus(null); return; }
  setText($('#gm-status'), 'đang kiểm tra…');
  try {
    S.genderModel = await L.genderModel.status();
  } catch (err) {
    S.genderModel = { reachable: false, error: errMsg(err) };
  }
  renderGenderModelStatus(S.genderModel);
}
// before the model is turned on: the GPU / VRAM of the server. Without one the user is told to keep it off (with the guide)
function gmConfirm(warn, reachable) {
  return new Promise((resolve) => {
    const done = (v) => { closeModal(); resolve(v); };
    openModal(h('div', { class: 'qchange gm-dialog' },
      h('h2', { text: warn.title }),
      h('p', {}, warn.text),
      h('p', { class: 'gm-advice' }, warn.advice),
      h('div', { class: 'modal-actions' },
        h('button', { class: 'btn ghost', onclick: () => done('guide') }, 'Hướng dẫn'),
        reachable ? h('button', { class: 'btn ghost danger', onclick: () => done('on') }, 'Vẫn bật (chạy bằng CPU)') : null,
        h('button', { class: 'btn primary', onclick: () => done('off') }, reachable ? 'Để tắt' : 'Đóng'))));
    $('#modal-card .btn.primary')?.focus();
  });
}
async function turnGenderModelOn() {
  if (!S.settings.genderModel?.url) { toast('Nhập địa chỉ model server trước', 'err'); return; }
  setText($('#gm-status'), 'đang kiểm tra GPU / VRAM của server…');
  let st;
  try { st = await L.genderModel.status(); } catch (err) { st = { reachable: false, error: errMsg(err) }; }
  S.genderModel = st;
  renderGenderModelStatus(st);
  if (!st.reachable) {
    const c = await gmConfirm({ title: 'Không kết nối được model server', text: st.error || '',
      advice: 'Chạy model server trước (Hướng dẫn → Chạy model server) rồi bật lại.', guide: 'server' }, false);
    if (c === 'guide') openGuide('server');
    return;
  }
  const warn = gmWarning(st.check);
  if (warn) {
    const c = await gmConfirm(warn, true);
    if (c === 'guide') { openGuide(warn.guide); return; }
    if (c !== 'on') { toast('Model vẫn tắt', ''); return; }
  }
  setText($('#gm-status'), 'đang nạp model trên server…');
  let r;
  try { r = await L.genderModel.load(); } catch (err) { r = { reachable: false, error: errMsg(err) }; }
  S.genderModel = r;
  if (!r.reachable || r.error) { renderGenderModelStatus(r); toast(`Không nạp được model: ${r.error}`, 'err'); return; }
  await saveSettings({ genderModel: { enabled: true } });
  const dev = GM_DEVICE[r.model?.device] || r.model?.device || '?';
  toast(`Đã bật model · chạy bằng ${dev}. Chọn "Tag + model" ở bộ lọc giới tính để dùng.`, r.check?.level === 'ok' ? 'ok' : '');
  if (r.check?.code === 'gpu_fallback') toast(gmWarning(r.check).title, 'err');
}
async function turnGenderModelOff() {
  await saveSettings({ genderModel: { enabled: false } });
  let freed = false;
  if (S.genderModel?.reachable && S.genderModel.local) { // a server on another computer may serve others: left as it is
    try { const r = await L.genderModel.unload(); freed = r.reachable && !r.error; } catch { /* the server may be gone already */ }
  }
  toast(freed ? 'Đã tắt model, server đã giải phóng bộ nhớ' : 'Đã tắt model: bộ lọc giới tính dùng tag', 'ok');
}
$('#gm-enabled').addEventListener('change', async (e) => {
  const on = e.target.checked;
  e.target.checked = !on; // the switch moves once the check is done
  e.target.disabled = true;
  try {
    await (on ? turnGenderModelOn() : turnGenderModelOff());
  } catch (err) {
    toast(errMsg(err), 'err');
  } finally {
    e.target.disabled = false;
    refreshGenderModel();
  }
});
$('#gm-guide').addEventListener('click', () => openGuide());
// general guide (guide.js): text spans `code` and **bold**, never HTML
function guideInline(text) {
  return String(text).split(/(`[^`]+`|\*\*[^*]+\*\*)/).filter(Boolean).map((part) => (part.startsWith('`')
    ? h('code', {}, part.slice(1, -1)) : part.startsWith('**') ? h('b', {}, part.slice(2, -2)) : part));
}
function guideBlock([kind, body]) {
  if (kind === 'ul') return h('ul', {}, body.map((li) => h('li', {}, guideInline(li))));
  if (kind === 'note') return h('p', { class: 'guide-note' }, guideInline(body));
  if (kind === 'code') {
    const copy = () => navigator.clipboard?.writeText(body).then(() => toast('Đã chép lệnh', 'ok'), () => {});
    return h('div', { class: 'guide-code' }, h('code', {}, body), h('button', { class: 'btn sm ghost', title: 'Chép lệnh', onclick: copy }, 'Chép'));
  }
  return h('p', {}, guideInline(body));
}
function openGuide(section) {
  const g = window.LYNXX_GUIDE;
  if (!g) { toast('Không tải được hướng dẫn', 'err'); return; }
  const go = (id, behavior = 'smooth') => { // below the sticky table of contents
    const card = $('#modal-card');
    const sec = $(`#guide-${id}`);
    if (!sec) return;
    const top = card.scrollTop + sec.getBoundingClientRect().top - card.getBoundingClientRect().top;
    card.scrollTo({ top: top - card.querySelector('.guide-toc').offsetHeight - 8, behavior });
  };
  openModal(h('div', { class: 'guide' },
    h('h2', { text: g.title }),
    h('nav', { class: 'guide-toc' }, g.sections.map((s) => h('button', { class: 'btn sm ghost', onclick: () => go(s.id) }, s.title))),
    g.sections.map((s) => h('section', { id: `guide-${s.id}` }, h('h3', { text: s.title }), s.blocks.map(guideBlock))),
    h('div', { class: 'modal-actions' }, h('button', { class: 'btn primary', onclick: closeModal }, 'Đóng'))));
  if (section) go(section, 'auto');
  else $('#modal-card .btn.primary')?.focus({ preventScroll: true });
}
$('#gm-url').addEventListener('change', async (e) => {
  try {
    await saveSettings({ genderModel: { url: e.target.value.trim() } });
  } catch (err) {
    toast(errMsg(err), 'err');
    e.target.value = S.settings.genderModel?.url || '';
    return;
  }
  refreshGenderModel();
});
$('#gm-check').addEventListener('click', () => refreshGenderModel());
$('#gm-token-save').addEventListener('click', async () => {
  const v = $('#gm-token').value.trim();
  if (!v) { toast(S.secrets?.genderModel?.set ? 'Ô trống: token đã lưu được giữ nguyên.' : 'Nhập token trước khi lưu.', S.secrets?.genderModel?.set ? '' : 'err'); return; }
  try {
    S.secrets = { ...S.secrets, genderModel: await L.genderModel.setToken(v) };
    $('#gm-token').value = '';
    toast('Đã lưu token model server', 'ok');
  } catch (err) { toast(`Không lưu được token: ${errMsg(err)}`, 'err'); }
  refreshGenderModel();
});
$('#gm-token').addEventListener('keydown', (e) => { if (e.key === 'Enter') $('#gm-token-save').click(); });
$('#gm-token-clear').addEventListener('click', async () => {
  try { S.secrets = { ...S.secrets, genderModel: await L.genderModel.clearToken() }; toast('Đã xoá token', 'ok'); } catch (err) { toast(errMsg(err), 'err'); }
  refreshGenderModel();
});

// table of contents of the settings page: one button per panel title
function renderSettingsNav() {
  const nav = $('#settings-nav');
  nav.replaceChildren(...$$('#page-settings .settings > .panel').filter((p) => !p.hidden).map((p) => {
    const title = p.querySelector('h2')?.textContent || '';
    return h('button', { onclick: () => p.scrollIntoView({ behavior: 'smooth', block: 'start' }) }, title);
  }));
}

function renderSettings() {
  const s = S.settings;
  bindSetting('#s-path', 'download.path', 'text');
  bindSetting('#s-subfolder', 'download.subfolderPerQuery');
  bindSetting('#s-prefix', 'download.prefix');
  bindSetting('#s-append', 'download.appendInfo');
  bindSetting('#s-preserve', 'download.preserveDate');
  bindSetting('#s-dump-tags', 'download.dumpTags');
  bindSetting('#s-dump-sources', 'download.dumpSources');
  bindSetting('#s-dump-comments', 'download.dumpComments');
  bindSetting('#s-dump-hashes', 'download.dumpHashes');
  bindSetting('#s-infomode', 'download.infoMode', 'text');
  bindSetting('#s-warn', 'download.warnNonEmpty');
  bindSetting('#s-notify', 'download.notify');
  bindSetting('#s-concurrent', 'download.maxConcurrentJobs', 'number');
  bindSetting('#s-skip-known', 'download.skipKnown');
  const days = s.library?.trashDays ?? 30;
  const daysSel = $('#s-trash-days');
  if (![...daysSel.options].some((o) => Number(o.value) === days)) daysSel.append(h('option', { value: String(days) }, `${days} ngày`));
  daysSel.value = String(days);
  $('#c-proxy').value = s.connection.proxy || '';
  $('#c-ignore').checked = !!s.connection.ignoreProxy;
  $('#c-nodown').checked = !!s.connection.proxyNoDownload;
  $('#c-timeout').value = s.connection.timeout || 0;
  $('#c-retries').value = s.connection.retries || 0;
  $('#c-headers').value = Object.keys(s.connection.headers || {}).length ? JSON.stringify(s.connection.headers, null, 1) : '';
  $('#c-cookies').value = Object.keys(s.connection.cookies || {}).length ? JSON.stringify(s.connection.cookies, null, 1) : '';
  $('#c-site-headers').value = httpMapText(s.connection.siteHeaders);
  $('#c-site-cookies').value = httpMapText(s.connection.siteCookies);
  $('#a-python').value = s.advanced.pythonPath || '';
  $('#a-verbose').checked = !!s.advanced.verbose;
  $('#a-catalog').value = s.advanced.catalogUrl || '';
  $('#safe-preview').checked = !!s.search.safePreview;
  document.body.classList.toggle('safe', !!s.search.safePreview);
  // search page defaults
  $('#f-path').value = s.download.path;
  $('#f-threads').value = s.download.threads;
  $('#f-images').checked = s.download.images;
  $('#f-videos').checked = s.download.videos;
  $('#f-audio').checked = s.download.audio !== false;
  $('#filters').open = s.search.filtersOpen !== false;
  $('#f-vformat').value = ['mp4', 'webm'].includes(s.download.videoFormat) ? s.download.videoFormat : '';
  const vres = Number.isInteger(s.download.maxVideoHeight) && s.download.maxVideoHeight > 0 ? s.download.maxVideoHeight : 0;
  const vresSel = $('#f-vres');
  if (![...vresSel.options].some((o) => Number(o.value) === vres)) vresSel.append(h('option', { value: String(vres) }, `${vres}p`));
  vresSel.value = String(vres);
  $('#f-lowres').value = s.download.lowRes ? '1' : '';
  $('#f-order').value = s.download.reverse ? 'desc' : 'asc';
  $('#f-limit').value = s.download.limit;
  $('#f-parchi').checked = s.download.parchi;
  $('#f-skip-known').checked = !!s.download.skipKnown;
  $('#f-safety').checked = s.download.safetyBlacklist === true;
  filtersSummary(); // once every field above holds its saved value
  renderApiKeys();
  renderTagLimits();
}
$('#s-path-pick').addEventListener('click', async () => {
  const p = await L.dialogs.folder($('#s-path').value);
  if (p) { $('#s-path').value = p; $('#f-path').value = p; saveSettings({ download: { path: p } }); }
});
$('#f-path-pick').addEventListener('click', async () => { const p = await L.dialogs.folder($('#f-path').value); if (p) $('#f-path').value = p; });
$('#f-path-open').addEventListener('click', () => L.app.openPath($('#f-path').value));
$('#c-save').addEventListener('click', async () => {
  const parse = (id) => {
    const t = $(id).value.trim();
    if (!t) return {};
    const v = JSON.parse(t);
    if (typeof v !== 'object' || Array.isArray(v)) throw new Error('phải là JSON object');
    return v;
  };
  try {
    const headers = parse('#c-headers');
    const cookies = parse('#c-cookies');
    const siteHeaders = parse('#c-site-headers');
    const siteCookies = parse('#c-site-cookies');
    const proxy = $('#c-proxy').value.trim();
    if (proxy && !/^(https?|socks5h?):\/\/([^@/]+@)?[^/:]+:\d+\/?$/i.test(proxy)) throw new Error('proxy dạng type://[user:pass@]host:port');
    // one save: main replaces the headers / cookies maps as a whole (removed entries go away) and rejects invalid
    // entries before writing anything
    await saveSettings({ connection: { proxy, ignoreProxy: $('#c-ignore').checked, proxyNoDownload: $('#c-nodown').checked,
      timeout: Number($('#c-timeout').value) || 0, retries: Number($('#c-retries').value) || 0, headers, cookies, siteHeaders, siteCookies } });
    toast('Đã lưu cài đặt kết nối', 'ok');
    renderVerifyStatus();
  } catch (err) { toast(`Không lưu được: ${errMsg(err)}`, 'err'); }
});

// ---------------- "Xác minh trong Lynxx": the user passes a site's bot challenge in a window of the app ----------------
// main opens the module's own website (never an address from here) in a sandboxed window with its own session, waits for the
// cf_clearance cookie and saves it with that window's User-Agent as the module's own cookie / header. The cookie value never
// comes back here: the two boxes of Cài đặt → Kết nối read it from the saved settings
const httpMapText = (m) => (m && Object.keys(m).length ? JSON.stringify(m, null, 1) : '');
const verifyRuns = new Map(); // abbr -> promise of the verification this window started (true: saved)
const verifyDone = new Map(); // abbr -> {at, expires} of this session's verifications (status line)
function mergeSiteHttpText(text, abbr, entry) {
  // the text of a per-site box with entry joined to abbr's entries (same name in any letter case replaced); everything else as
  // the user typed it. null: the text is not a JSON object (left alone)
  let all = {};
  if (String(text || '').trim()) {
    try { all = JSON.parse(text); } catch { return null; }
    if (!all || typeof all !== 'object' || Array.isArray(all)) return null;
  }
  const mine = all[abbr] && typeof all[abbr] === 'object' && !Array.isArray(all[abbr]) ? all[abbr] : {};
  const names = new Set(Object.keys(entry).map((k) => k.toLowerCase()));
  all[abbr] = { ...Object.fromEntries(Object.entries(mine).filter(([k]) => !names.has(k.toLowerCase()))), ...entry };
  return JSON.stringify(all, null, 1);
}
function refreshSiteHttpFields(abbr) {
  // main saved the new entries; the boxes may hold edits not saved yet ("Lưu kết nối" replaces the saved maps with the boxes):
  // the new entries are joined to the text instead of replacing it
  const c = S.settings.connection;
  const left = [];
  for (const [sel, entry, label] of [['#c-site-headers', { 'User-Agent': c.siteHeaders?.[abbr]?.['User-Agent'] }, 'Headers theo site'],
    ['#c-site-cookies', { cf_clearance: c.siteCookies?.[abbr]?.cf_clearance }, 'Cookies theo site']]) {
    if (Object.values(entry).some((x) => typeof x !== 'string')) continue;
    const text = mergeSiteHttpText($(sel).value, abbr, entry);
    if (text === null) left.push(label);
    else $(sel).value = text;
  }
  if (left.length) toast(`Ô ${left.join(', ')} đang sửa dở và không phải JSON hợp lệ: chưa thêm mục mới vào đó (cài đặt đã lưu có rồi)`, 'err', 10000);
}
function verifyExpiryText(expires) {
  if (!expires) return 'cookie không ghi hạn';
  const d = new Date(expires * 1000);
  const time = d.toLocaleTimeString('vi-VN', { hour: '2-digit', minute: '2-digit' });
  return `hết hạn lúc ${time}${d.toDateString() === new Date().toDateString() ? '' : ` ngày ${d.toLocaleDateString('vi-VN')}`}`;
}
// then: [label, fn] of the button the success toast offers (Preview / count / job again)
function verifySite(module, then = null) {
  if (!L.site?.verify) { toast('Bản app này chưa có “Xác minh trong Lynxx”', 'err'); return Promise.resolve(false); }
  const open = verifyRuns.get(module);
  if (open) { L.site.verify(module).catch(() => {}); return open; } // main brings its window to the front, the first call answers
  const run = runVerify(module, then).finally(() => { verifyRuns.delete(module); renderVerifyStatus(); });
  verifyRuns.set(module, run);
  renderVerifyStatus();
  return run;
}
async function runVerify(module, then) {
  const name = moduleInfo(module)?.name || module;
  toast(`Đang mở ${name} trong cửa sổ xác minh: tự qua bước kiểm tra của site ở đó, cửa sổ tự đóng khi xong (tối đa 5 phút)`, 'ok', 8000);
  try {
    const r = await L.site.verify(module);
    if (!r?.ok) {
      toast(r?.message || `Chưa xác minh được ${name}`, r?.reason === 'not-needed' ? 'ok' : 'err', 8000);
      return false;
    }
    const fresh = await L.settings.get();
    S.settings = fresh.settings;
    S.secrets = fresh.secrets;
    refreshSiteHttpFields(module);
    verifyDone.set(module, { at: Date.now(), expires: Number(r.expires) || 0 });
    const c = S.settings.connection;
    // the cookie is bound to the IP: files that leave without the proxy may be challenged again
    const ipNote = c.proxy && !c.ignoreProxy && c.proxyNoDownload ? ' File tải không qua proxy nên đi từ IP khác: phần tải file có thể vẫn bị chặn.' : '';
    const msg = `Đã xác minh ${name}: lưu cookie cf_clearance và User-Agent cho ${module} (${verifyExpiryText(r.expires)}).${ipNote}`;
    if (then) toastAction(msg, then[0], then[1], 20000);
    else toast(msg, 'ok', 10000);
    return true;
  } catch (err) {
    toast(`Không xác minh được ${name}: ${errMsg(err)}`, 'err', 10000);
    return false;
  }
}
function renderVerifyModules() {
  // Cài đặt → Kết nối: installed modules with an https website
  const sel = $('#c-verify-module');
  const was = sel.value || S.module;
  const mods = S.modules.filter((m) => m.installed && /^https:\/\//i.test(m.site || ''));
  sel.replaceChildren(...mods.map((m) => h('option', { value: m.abbr }, `${m.abbr} · ${m.name}`)));
  if (mods.some((m) => m.abbr === was)) sel.value = was;
  setHidden($('#c-verify-row'), !L.site?.verify || !mods.length);
  renderVerifyStatus();
}
function renderVerifyStatus() {
  const m = $('#c-verify-module').value;
  const done = verifyDone.get(m);
  const at = (ts) => new Date(ts).toLocaleTimeString('vi-VN', { hour: '2-digit', minute: '2-digit' });
  let text = '';
  if (!m || !L.site?.verify) text = '';
  else if (verifyRuns.has(m)) text = 'Đang chờ bạn qua bước kiểm tra trong cửa sổ xác minh…';
  else if (done) text = `Đã xác minh lúc ${at(done.at)}, ${verifyExpiryText(done.expires)}.`;
  else if (S.settings?.connection?.siteCookies?.[m]?.cf_clearance) text = 'Đã có cookie cf_clearance cho module này (lưu trước đây, không rõ hạn).';
  else text = 'Chưa có cookie cf_clearance cho module này.';
  setText($('#c-verify-status'), text);
}
$('#c-verify-module').addEventListener('change', renderVerifyStatus);
$('#c-verify').addEventListener('click', () => { const m = $('#c-verify-module').value; if (m) verifySite(m); });

$('#a-save').addEventListener('click', async () => {
  await saveSettings({ advanced: { pythonPath: $('#a-python').value.trim(), verbose: $('#a-verbose').checked, catalogUrl: $('#a-catalog').value.trim() } });
  toast('Đã lưu. Đổi Python cần khởi động lại app.', 'ok');
});
function renderApiKeys() {
  const sec = S.secrets || { modules: {}, encryption: false };
  $('#keys-note').textContent = sec.encryption
    ? 'Key được mã hoá bằng keyring của hệ điều hành (safeStorage) và không bao giờ gửi tới giao diện.'
    : 'Không có keyring hệ điều hành: key sẽ được lưu dạng văn bản thường trong settings.json.';
  const canClear = typeof L.secrets?.clear === 'function';
  const mods = S.modules.filter((m) => m.installed && m.auth?.apiKey);
  $('#api-keys').replaceChildren(...mods.flatMap((m) => {
    const st = sec.modules?.[m.abbr];
    const input = h('input', { type: 'password', placeholder: st ? 'Key mới (trống = giữ nguyên)' : (m.auth.apiKeyFormat || 'KEY,USER'),
      title: st ? 'Nhập key mới để thay key đã lưu. Để trống thì key đã lưu được giữ nguyên.' : '', autocomplete: 'off' });
    const save = async () => {
      // an empty field never deletes the stored key: that is what "Xoá key" is for
      if (!input.value.trim()) { toast(st ? 'Ô trống: key đã lưu được giữ nguyên.' : 'Nhập key trước khi lưu.', st ? '' : 'err'); return; }
      try {
        S.secrets = await L.secrets.set(m.abbr, input.value);
        input.value = '';
        renderApiKeys();
        renderKeyNeeded();
        toast(`Đã lưu API key cho ${m.name}`, 'ok');
      } catch (err) { toast(`Không lưu được key: ${errMsg(err)}`, 'err'); }
    };
    input.addEventListener('keydown', (e) => { if (e.key === 'Enter') save(); });
    const clear = async () => {
      if (!window.confirm(`Xoá API key của ${m.name}? Key sẽ bị xoá khỏi máy và không khôi phục được.`)) return;
      try {
        S.secrets = await L.secrets.clear(m.abbr);
        renderApiKeys();
        renderKeyNeeded();
        toast(`Đã xoá API key của ${m.name}`, 'ok');
      } catch (err) { toast(`Không xoá được key: ${errMsg(err)}`, 'err'); }
    };
    return [h('div', { class: 'setting key-row' }, h('span', {}, h('span', { class: 'mod-abbr' }, m.abbr), ` ${m.name} `, st ? h('span', { class: 'tagpill ok' }, st.encrypted ? 'đã lưu (mã hoá)' : 'đã lưu') : null),
      h('span', { class: 'row' }, input,
        h('button', { class: 'btn sm', onclick: save }, 'Lưu'),
        st && canClear ? h('button', { class: 'btn sm ghost danger', onclick: clear }, 'Xoá key') : null)),
    keyRateLimitRow(m)].filter(Boolean);
  }));
}
// Requests a minute the site allows the user's key (settings.connection.keyRateLimits, 0 = what the site publishes: manifest
// auth.rateLimit). Shown for modules that declare a limit per key (Rule34: 60 a minute). Main writes it where the core reads it:
// downloads, Preview and tag list updates of that key keep this pace together, and slow down on their own after a 429.
function keyRateLimitRow(m) {
  const rl = m.auth?.rateLimit;
  if (!rl?.requests || !rl?.seconds) return null;
  const published = Math.max(1, Math.round(rl.requests * 60 / rl.seconds));
  const cur = Number(S.settings?.connection?.keyRateLimits?.[m.abbr]) || 0;
  const input = h('input', { type: 'number', min: 0, max: 100000, step: 1, class: 'tiny', value: cur || '', placeholder: String(published),
    title: `Để trống hoặc 0 = giới hạn site công bố (${published} request mỗi phút)` });
  input.addEventListener('change', async () => {
    const n = Math.max(0, Math.min(100000, Math.floor(Number(input.value) || 0)));
    input.value = n || '';
    try {
      await saveSettings({ connection: { keyRateLimits: { [m.abbr]: n } } });
      toast(n ? `${m.name}: tối đa ${n} request mỗi phút cho khoá của bạn` : `${m.name}: dùng giới hạn site công bố (${published} request mỗi phút)`, 'ok');
    } catch (err) { toast(`Không lưu được: ${errMsg(err)}`, 'err'); }
  });
  return h('div', { class: 'setting' },
    h('span', { class: 'muted' }, `Giới hạn của khoá ${m.name} (site công bố ${published}; admin cấp thêm thì nhập số mới)`),
    h('span', { class: 'row' }, input, h('span', { class: 'muted' }, 'request/phút')));
}
// Account tag limit per module (settings.search.tagLimits, 0 = module default): main passes it to the core as -tags_limit.
// Shown for modules whose manifest declares a tag limit (syntax.maxTags), and always for Danbooru.
function renderTagLimits() {
  const limits = S.settings?.search?.tagLimits || {};
  const mods = S.modules.filter((m) => m.installed && (m.syntax?.maxTags > 0 || m.abbr === 'db'));
  $('#tag-limits').replaceChildren(...mods.map((m) => {
    const def = m.syntax?.maxTags || (m.abbr === 'db' ? 2 : 0);
    const input = h('input', { type: 'number', min: 0, max: 100, step: 1, class: 'tiny', value: Number(limits[m.abbr]) || 0, title: `0 = mặc định của module (${def})` });
    input.addEventListener('change', async () => {
      const n = Math.max(0, Math.min(100, Math.floor(Number(input.value) || 0)));
      input.value = n;
      try {
        await saveSettings({ search: { tagLimits: { [m.abbr]: n } } });
        toast(n ? `${m.name}: tối đa ${n} tag mỗi lần tìm` : `${m.name}: dùng giới hạn mặc định (${def} tag)`, 'ok');
      } catch (err) { toast(`Không lưu được: ${errMsg(err)}`, 'err'); }
    });
    return h('div', { class: 'setting' },
      h('span', {}, h('span', { class: 'mod-abbr' }, m.abbr), ` ${m.name} `, h('span', { class: 'muted' }, `(mặc định ${def})`)),
      h('span', { class: 'row' }, input, h('span', { class: 'muted' }, 'tag')));
  }));
}
async function renderAbout() {
  const i = await L.app.info();
  S.info = i;
  $('#brand-ver').textContent = `v${i.version}`;
  $('#about').textContent = [
    `Lynxx desktop ${i.version} · Electron ${i.electron} · ${i.platform}`,
    `Lõi Python: ${i.sidecar ? `${i.sidecar.name} ${i.sidecar.version} (Python ${i.sidecar.python})` : 'chưa sẵn sàng'}`,
    `Python: ${i.python}`, `Dữ liệu: ${i.userData}`, `Mã nguồn: ${i.repo}`,
    '', 'Lynxx dựa trên mã nguồn gốc của trickerer01 (github.com/trickerer01), giấy phép MIT.',
  ].join('\n');
}

// ---------------- log ----------------
// S.logLines is the model (trimmed in batches); the <pre> is only touched while the log page is visible, otherwise
// it is rebuilt once when the page is opened.
const LOG_MAX = 3000;
const LOG_SLACK = 500;
const logView = createLogView($('#log'), LOG_MAX, LOG_SLACK);
let logStale = false;
function appendLog(source, text) {
  const line = `[${source}] ${text}`;
  S.logLines.push(line);
  if (S.logLines.length > LOG_MAX + LOG_SLACK) S.logLines.splice(0, S.logLines.length - LOG_MAX);
  if ($('#page-log').classList.contains('active')) logView.push([line]);
  else logStale = true;
}
function showLogPage() {
  if (logStale) { logStale = false; logView.reset(S.logLines); } else logView.reveal();
}
$('#log-clear').addEventListener('click', () => { S.logLines = []; logStale = false; logView.clear(); });

// ---------------- events from main ----------------
let coreOk = false;
function onSidecarStatus(st) {
  if (!st) return;
  $('#core-dot').className = `dot ${st.ok ? 'ok' : st.error === 'starting' ? '' : 'err'}`;
  $('#core-status').textContent = st.ok ? `Lõi sẵn sàng · ${st.info?.version || ''}`
    : st.error === 'starting' ? 'Đang khởi động lõi…' : `Lõi lỗi: ${st.error}`;
  if (st.ok && !coreOk) {
    loadModules(); renderAbout(); warmupTags(S.module); loadTagLists();
    // a core that exited took its running tag updates with it (no tags.updateDone comes): the new one says what runs
    tagUpdates.clear();
    if (azPage.loaded) refreshTagStatus();
  }
  coreOk = !!st.ok;
}
function listen() {
  L.on('sidecar:status', onSidecarStatus);
  L.on('log', (m) => appendLog(m.source, m.text));
  L.on('jobs:update', (job) => upsertJob(job));
  L.on('jobs:log', (m) => appendJobLog(m.id, m.text));
  L.on('sidecar:notify', ({ method, params }) => {
    if (method === 'tags.updateProgress') {
      // whoever started it (this page, main's auto update or a new extension): the row shows it and its button stays off
      showTagRunning(params.module, params.count, params.message);
    } else if (method === 'index.cleanProgress' || method === 'index.cleanDone') {
      onIndexCleanNotify(method, params);
    } else if (method === 'library.trashPurged') {
      // main emptied the trash of files older than settings.library.trashDays
      if (LIB.ready) { refreshLibraryStats(); if (LIB.view === 'trash') runLibrary(LIB.page); }
      if ($('#page-settings').classList.contains('active')) refreshIndexStats();
    } else if (method === 'app.notice') {
      // main process notices (ex. settings.json was corrupt: backed up, API keys recovered)
      toast(`${params.title ? `${params.title}: ` : ''}${params.message || ''}`, params.level === 'info' ? 'ok' : 'err');
    } else if (method === 'tags.learned') {
      // a module without a tag list learned it from previewed / downloaded posts (core, lynxx/tags_learned.py), or learned tags
      // its bundled list lacks: a tag shown as unknown before may be known now. Sent only when the list changed
      reresolveTags(params.module);
      forgetListTypes(params.module);
      loadTagLists();
      if (azPage.loaded) refreshTagStatus();
    } else if (method === 'tags.updateDone') {
      tagUpdates.delete(params.module);
      toast(`Tag ${params.module}: ${params.message}`, params.written ? 'ok' : 'err');
      if (params.written) { reresolveTags(params.module); forgetListTypes(params.module); }
      refreshTagStatus();
      loadTagLists();
    }
  });
  L.on('appearance:changed', (a) => applyAppearance(a));
  try { L.on('nav:page', (page) => { if (['downloads', 'search', 'extensions'].includes(page)) showPage(page); }); } catch { /* older preload */ }
  if (L.gen) L.on('gen:event', (ev) => onGenEvent(ev));
}

// files or links dropped on the window must not navigate the app away
['dragover', 'drop'].forEach((ev) => document.addEventListener(ev, (e) => e.preventDefault()));

// ---------------- boot ----------------
async function boot() {
  const r = await L.settings.get();
  S.settings = r.settings;
  S.secrets = r.secrets;
  S.module = r.settings.search.module || 'rx';
  try { localStorage.removeItem('lynxx.searchKind'); } catch { /* no storage */ } // the choice of the "Tìm theo" picker (2.0.11 only)
  applyAppearance(r.appearance);
  renderSettings();
  renderAiSettings();
  renderSettingsNav();
  refreshGenderModel();
  renderModuleButton();
  $('#btn-related').hidden = !relatedSupported();
  await loadModules();
  renderAbout().catch(() => {});
  try { renderJobs(); (await L.jobs.list()).forEach(upsertJob); } catch { /* ignore */ }
  // the first status event may have been sent before this page loaded
  try { onSidecarStatus(await L.sidecarStatus()); } catch { /* ignore */ }
  tagInput.focus();
}
function start() {
  L = window.lynxx;
  listen();
  boot();
}
// The preload bridge is defined before any page script runs, so in Electron start() runs synchronously and mock.js is
// never fetched. In a plain browser (design work, UI tests) the mock bridge is loaded first.
if (window.lynxx) start();
else if (/\bElectron\//.test(navigator.userAgent)) {
  $('#core-dot').className = 'dot err';
  $('#core-status').textContent = 'Lỗi: không có cầu nối tới tiến trình chính (preload)';
} else {
  const mock = document.createElement('script');
  mock.src = 'mock.js';
  mock.addEventListener('load', start);
  document.head.append(mock);
}
