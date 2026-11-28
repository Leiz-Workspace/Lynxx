// Lynxx desktop renderer (plain script, no bundler): the shell (navigation, appearance, settings, log). Library, sources,
// reader and download jobs are in renderer/books.js. Talks to main only through window.lynxx (preload bridge or mock.js).
let L = window.lynxx;
// every script of the page has run (renderer/books.js comes after this file): its boot hook is called only then
const pageScriptsLoaded = document.readyState === 'loading'
  ? new Promise((resolve) => document.addEventListener('DOMContentLoaded', resolve, { once: true })) : Promise.resolve();

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
const S = { settings: null, appearance: null, info: null, logLines: [] };

// ---------------- navigation ----------------
const PAGES = ['shelf', 'sources', 'reader', 'downloads', 'settings', 'log'];
function showPage(name) {
  if (!PAGES.includes(name)) return;
  $$('#nav button').forEach((b) => b.classList.toggle('active', b.dataset.page === name));
  $$('.page').forEach((p) => p.classList.toggle('active', p.id === `page-${name}`));
  if (name === 'log') showLogPage();
  try { window.onLynxxPage?.(name); } catch (err) { console.error(err); } // renderer/books.js
}
$$('#nav button').forEach((b) => b.addEventListener('click', () => showPage(b.dataset.page)));

// library, sources, reader and the download jobs: renderer/books.js

// ---------------- downloads badge (counts published by books.js on window.lynxxBookJobs) ----------------
function updateJobsBadge() {
  const books = window.lynxxBookJobs || { active: 0, total: 0 };
  const badge = $('#badge-jobs');
  setHidden(badge, books.active === 0);
  setText(badge, String(books.active));
  setHidden($('#jobs-empty'), books.total > 0);
}

// ---------------- appearance ----------------
function applyAppearance(a) {
  S.appearance = a;
  document.documentElement.dataset.theme = a.theme;
  $('#brand-icon').src = a.iconUrl;
  const bg = a.background || {};
  $('#bg').className = bg.mode === 'image' ? 'image' : bg.mode === 'none' ? 'none' : '';
  $('#bg-img').src = a.backgroundUrl;
  const root = document.documentElement.style;
  root.setProperty('--bg-blur', `${bg.mode === 'icon' ? (bg.blur ?? 4) : 0}px`);
  root.setProperty('--bg-opacity', String(bg.opacity ?? 0.9));
  root.setProperty('--bg-dim', String(bg.dim ?? 0.18));
  renderAppearanceSettings();
}

async function saveSettings(patch) {
  const r = await L.settings.set(patch);
  S.settings = r.settings;
  return r;
}

const THEME_NAMES = { abyss: 'Abyss', graphite: 'Graphite', paper: 'Paper', oled: 'OLED' };
const THEME_SWATCH = {
  abyss: 'linear-gradient(135deg,#0d1b21 55%,#2fd4d8 55%)', graphite: 'linear-gradient(135deg,#18181a 55%,#e8e8e8 55%)',
  paper: 'linear-gradient(135deg,#fbfeff 55%,#087680 55%)', oled: 'linear-gradient(135deg,#000 55%,#2fd4d8 55%)',
};
function renderAppearanceSettings() {
  const a = S.appearance;
  if (!a) return;
  $('#theme-row').replaceChildren(...a.themes.map((t) => h('button', {
    class: `theme-sw ${t === a.theme ? 'on' : ''}`, style: `background:${THEME_SWATCH[t]}`,
    onclick: () => saveSettings({ appearance: { theme: t } }).catch((err) => toast(errMsg(err), 'err')),
  }, h('span', { class: 'sw-label' }, THEME_NAMES[t] || t))));
  $('#icon-row').replaceChildren(...a.icons.map((ic) => h('button', {
    class: `icon-sw ${ic.id === a.icon ? 'on' : ''}`, title: ic.name,
    onclick: async () => {
      try {
        const r = await saveSettings({ appearance: { icon: ic.id } });
        showIconNotice(r.iconSync, r.appearance);
      } catch (err) { toast(errMsg(err), 'err'); }
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
// 'foreign' | 'other' | 'skipped' | 'unsupported', shortcuts (Windows), restartForDock (Wayland reads the launcher when the app starts)
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
  } else if (sync?.launcher === 'other') {
    n.replaceChildren(
      h('div', {}, 'Icon trong app đã đổi. Launcher lynxx.desktop trên máy mở một bản Lynxx khác (ví dụ AppImage cạnh bản cài) nên bản này không sửa nó; '
        + 'dock trên Wayland vẫn hiện icon của launcher đó.'));
  } else if (sync?.launcher === 'skipped') {
    n.replaceChildren(h('div', {}, 'Icon trong app đã đổi. Bản chạy từ mã nguồn hoặc hồ sơ thử không sửa launcher của máy.'));
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
    if (file) await L.appearance.setBackgroundImage(file);
    return;
  }
  saveSettings({ appearance: { background: { mode } } }).catch((err) => toast(errMsg(err), 'err'));
});
$('#bg-pick').addEventListener('click', async () => {
  const file = await L.dialogs.image();
  if (file) await L.appearance.setBackgroundImage(file);
});
const saveBg = debounce((patch) => saveSettings({ appearance: { background: patch } }).catch(() => {}), 150);
$('#bg-blur').addEventListener('input', (e) => { document.documentElement.style.setProperty('--bg-blur', `${e.target.value}px`); saveBg({ blur: Number(e.target.value) }); });
$('#bg-opacity').addEventListener('input', (e) => { document.documentElement.style.setProperty('--bg-opacity', String(e.target.value / 100)); saveBg({ opacity: e.target.value / 100 }); });
$('#bg-dim-range').addEventListener('input', (e) => { document.documentElement.style.setProperty('--bg-dim', String(e.target.value / 100)); saveBg({ dim: e.target.value / 100 }); });

// ---------------- settings ----------------
// table of contents of the settings page: one button per panel title
function renderSettingsNav() {
  $('#settings-nav').replaceChildren(...$$('#page-settings .settings > .panel').filter((p) => !p.hidden).map((p) => {
    const title = p.querySelector('h2')?.textContent || '';
    return h('button', { onclick: () => p.scrollIntoView({ behavior: 'smooth', block: 'start' }) }, title);
  }));
}

function renderSettings() {
  const s = S.settings;
  $('#s-notify').checked = s.download.notify !== false;
  $('#c-proxy').value = s.connection.proxy || '';
  $('#c-ignore').checked = Boolean(s.connection.ignoreProxy);
  $('#c-dns').value = s.connection.dns || 'system';
  $('#c-doh').value = s.connection.dohUrl || '';
  $('#c-doh-row').hidden = $('#c-dns').value !== 'custom';
  $('#a-python').value = s.advanced.pythonPath || '';
}
$('#s-notify').addEventListener('change', (e) => saveSettings({ download: { notify: e.target.checked } }).catch((err) => toast(errMsg(err), 'err')));
$('#c-save').addEventListener('click', async () => {
  try {
    await saveSettings({ connection: { proxy: $('#c-proxy').value.trim(), ignoreProxy: $('#c-ignore').checked, dns: $('#c-dns').value, dohUrl: $('#c-doh').value.trim() } });
    toast('Đã lưu kết nối', 'ok');
  } catch (err) { toast(`Không lưu được: ${errMsg(err)}`, 'err'); }
});
$('#c-dns').addEventListener('change', () => { $('#c-doh-row').hidden = $('#c-dns').value !== 'custom'; });
$('#a-save').addEventListener('click', async () => {
  try {
    const before = S.settings.advanced.pythonPath;
    const wanted = $('#a-python').value.trim();
    await saveSettings({ advanced: { pythonPath: wanted } });
    if (S.settings.advanced.pythonPath !== wanted) {
      toast('Không đổi Python: đường dẫn không hợp lệ hoặc bạn đã huỷ', 'err');
      $('#a-python').value = S.settings.advanced.pythonPath || '';
    } else if (wanted !== before) {
      toastAction('Đã lưu. Python mới được dùng khi Lynxx khởi động lại', 'Khởi động lại', () => L.app.relaunch());
    } else toast('Đã lưu', 'ok');
  } catch (err) { toast(`Không lưu được: ${errMsg(err)}`, 'err'); }
});

async function renderAbout() {
  const i = await L.app.info();
  S.info = i;
  $('#brand-ver').textContent = `v${i.version}`;
  $('#about').textContent = [
    `Lynxx ${i.version} · Electron ${i.electron} · ${i.platform}`,
    `Lõi: ${i.sidecar ? `${i.sidecar.name} ${i.sidecar.version} (Python ${i.sidecar.python})` : 'chưa sẵn sàng'}${i.core === 'bundled' ? ' · bản đóng gói' : ''}`,
    `Chạy lõi bằng: ${i.python}`, `Dữ liệu: ${i.userData}`,
    '', 'Lynxx dựa trên mã nguồn gốc của trickerer01 (github.com/trickerer01), giấy phép MIT.',
  ].join('\n');
  $('#a-python-row').hidden = i.core === 'bundled';
  $('#a-launcher-row').hidden = i.platform !== 'linux';
  renderSettingsNav();
}
$('#a-launcher').addEventListener('click', async () => {
  try {
    const r = await L.app.installLauncher();
    toast(r.ok ? `Đã tạo launcher: ${r.path}` : r.message, r.ok ? 'ok' : 'err');
  } catch (err) { toast(errMsg(err), 'err'); }
});
$('#a-data').addEventListener('click', () => { if (S.info?.userData) L.app.openPath(S.info.userData); });

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
  if (st.ok && !coreOk) renderAbout().catch(() => {});
  coreOk = Boolean(st.ok);
}
function listen() {
  L.on('sidecar:status', onSidecarStatus);
  L.on('log', (m) => appendLog(m.source, m.text));
  L.on('appearance:changed', (a) => applyAppearance(a));
  L.on('nav:page', (page) => showPage(page));
}

// files or links dropped on the window must not navigate the app away
['dragover', 'drop'].forEach((ev) => document.addEventListener(ev, (e) => e.preventDefault()));

// ---------------- boot ----------------
async function boot() {
  const r = await L.settings.get();
  S.settings = r.settings;
  applyAppearance(r.appearance);
  renderSettings();
  renderSettingsNav();
  renderAbout().catch(() => {});
  // the first status event may have been sent before this page loaded
  try { onSidecarStatus(await L.sidecarStatus()); } catch { /* ignore */ }
  await pageScriptsLoaded;
  try { window.onLynxxBoot?.(); } catch (err) { console.error(err); } // renderer/books.js
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
