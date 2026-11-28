// Lynxx renderer: collection library ("Thư viện"), vBook-format extensions ("Nguồn truyện"), book download jobs, the
// export dialog and the reader. Loaded after app.js and uses its helpers ($, h, toast, errMsg, openModal, showPage…).
// Every call goes through window.lynxx (preload bridge or mock.js); extension code never runs in this page.
'use strict';

(() => {
  const api = () => window.lynxx;
  const esc = (s) => String(s ?? '');
  const KIND_LABEL = { comic: 'Truyện tranh', novel: 'Truyện chữ', document: 'Tài liệu' };
  const STATE_ICON = { ready: '✓', pending: '·', failed: '✗', partial: '◐' };
  const store = {
    get: (k, d) => { try { const v = localStorage.getItem(`lynxx.${k}`); return v === null ? d : JSON.parse(v); } catch { return d; } },
    set: (k, v) => { try { localStorage.setItem(`lynxx.${k}`, JSON.stringify(v)); } catch { /* no storage */ } },
  };
  const thumb = (url, referer) => (url ? api().thumbUrl(url, referer || '') : '');
  const cover = (src, title, cls = 'cover') => (src
    ? h('img', { class: cls, src, alt: '', loading: 'lazy', onerror: (e) => { e.target.replaceWith(placeholder(title, cls)); } })
    : placeholder(title, cls));
  function placeholder(title, cls) {
    const t = String(title || '?').trim();
    return h('div', { class: `${cls} ph` }, h('span', {}, t.slice(0, 1).toUpperCase()));
  }
  const busy = async (btn, fn) => {
    const old = btn?.textContent;
    if (btn) { btn.disabled = true; btn.dataset.busy = '1'; }
    try { return await fn(); } finally { if (btn) { btn.disabled = false; delete btn.dataset.busy; if (old !== undefined && btn.textContent !== old && btn.dataset.keep !== '1') btn.textContent = old; } }
  };
  const toggle = (checked, onchange) => {
    const input = h('input', { type: 'checkbox', class: 'switch' });
    input.checked = Boolean(checked);
    if (onchange) input.addEventListener('change', () => onchange(input.checked));
    return input;
  };

  // ================================================================ library (Thư viện)
  const shelf = { items: [], kind: '', q: '', loaded: false };

  async function loadShelf() {
    try { shelf.items = await api().shelf.collections(); shelf.loaded = true; } catch (err) { toast(`Không đọc được thư viện: ${errMsg(err)}`, 'err'); shelf.items = []; }
    renderShelf();
  }

  function renderShelf() {
    const grid = $('#shelf-grid');
    const q = shelf.q.trim().toLowerCase();
    const list = shelf.items.filter((c) => (!shelf.kind || c.kind === shelf.kind)
      && (!q || `${c.title} ${c.author} ${c.source}`.toLowerCase().includes(q)));
    grid.replaceChildren(...list.map((c) => {
      const progress = c.lastChapterTitle ? `Đang đọc: ${c.lastChapterTitle}` : (c.kind === 'document' ? (c.source || 'Tài liệu') : '');
      const counts = c.chapters > 1 || c.pluginId ? `${nf.format(c.ready)}/${nf.format(c.chapters)} chương` : '';
      return h('div', { class: 'book-card glass', tabindex: '0', title: c.title, onclick: () => openCollection(c.id), onkeydown: (e) => { if (e.key === 'Enter') openCollection(c.id); } },
        h('div', { class: 'book-cover' }, cover(c.coverUrl, c.title), c.failed ? h('span', { class: 'book-flag danger', title: 'Có chương tải lỗi' }, `${c.failed} lỗi`) : null),
        h('div', { class: 'book-meta' },
          h('div', { class: 'book-title' }, c.title),
          h('div', { class: 'book-sub muted' }, [c.source, KIND_LABEL[c.kind]].filter(Boolean).join(' · ')),
          counts ? h('div', { class: 'book-sub muted' }, counts) : null,
          progress ? h('div', { class: 'book-sub accent' }, progress) : null));
    }));
    $('#shelf-empty').hidden = !shelf.loaded || shelf.items.length > 0;
  }

  $('#shelf-q')?.addEventListener('input', debounce((e) => { shelf.q = e.target.value; renderShelf(); }, 120));
  $$('#shelf-kind button').forEach((b) => b.addEventListener('click', () => {
    $$('#shelf-kind button').forEach((x) => x.classList.toggle('on', x === b));
    shelf.kind = b.dataset.kind;
    renderShelf();
  }));
  $('#shelf-import')?.addEventListener('click', (e) => importFiles(e.currentTarget));
  $('#shelf-link')?.addEventListener('click', async () => {
    const r = await prompt2('Mở truyện từ link', [{ label: 'Link trang truyện', placeholder: 'https://…' }]);
    if (r && r[0]) openByUrl(r[0]);
  });

  async function importFiles(btn) {
    const results = await busy(btn, () => api().shelf.importFile()).catch((err) => { toast(errMsg(err), 'err'); return []; });
    if (!results || !results.length) return null;
    const ok = results.filter((r) => r.ok);
    const bad = results.filter((r) => !r.ok);
    if (ok.length) toast(`Đã nhập ${ok.length} file vào thư viện`, 'ok');
    bad.forEach((r) => toast(`${r.file}: ${r.error}`, 'err', 9000));
    await loadShelf();
    return ok;
  }

  // ---------------------------------------------------------------- collection detail
  async function openCollection(id) {
    let c;
    try { c = await api().shelf.collection(id); } catch (err) { toast(errMsg(err), 'err'); return; }
    const selected = new Set();
    const fromPlugin = Boolean(c.pluginId);
    const missing = c.chapters.filter((x) => x.state !== 'ready' && !x.locked).length;
    const readBtn = h('button', { class: 'btn primary', onclick: () => { closeModal(); openReader(c.id, c.lastChapterId || c.chapters.find((x) => x.state === 'ready')?.id); } },
      c.lastChapterId ? 'Đọc tiếp' : 'Đọc');
    readBtn.disabled = !c.chapters.some((x) => x.state === 'ready');
    const dlSel = h('button', { class: 'btn sm', disabled: true, onclick: async () => {
      try { await api().books.download({ pluginId: c.pluginId, url: c.bookUrl, scope: 'selected', chapterUrls: c.chapters.filter((x) => selected.has(x.id)).map((x) => x.url) }); toast('Đã thêm vào hàng tải', 'ok'); closeModal(); } catch (err) { toast(errMsg(err), 'err'); }
    } }, 'Tải chương đã chọn');
    const list = h('div', { class: 'chapter-list' }, c.chapters.map((ch) => {
      const box = h('input', { type: 'checkbox', disabled: ch.state === 'ready' || ch.locked || !fromPlugin });
      box.addEventListener('change', () => { if (box.checked) selected.add(ch.id); else selected.delete(ch.id); dlSel.disabled = !selected.size; dlSel.textContent = selected.size ? `Tải ${selected.size} chương đã chọn` : 'Tải chương đã chọn'; });
      const row = h('div', { class: `chapter-row st-${ch.state}${ch.id === c.lastChapterId ? ' current' : ''}`, title: ch.error || '' },
        fromPlugin ? box : null,
        h('span', { class: 'st' }, ch.locked ? '🔒' : (STATE_ICON[ch.state] || '·')),
        h('span', { class: 'ct' }, ch.title),
        ch.kind === 'image' && ch.pages ? h('span', { class: 'muted cp' }, `${ch.pages} trang`) : null);
      if (ch.state === 'ready') row.addEventListener('click', (e) => { if (e.target !== box) { closeModal(); openReader(c.id, ch.id); } });
      return row;
    }));
    const exports = c.exports.length ? h('div', { class: 'export-list' }, h('h3', {}, 'File đã xuất'), c.exports.map((x) => h('div', { class: 'export-row' },
      h('span', { class: 'fmt' }, ({ epub2: 'EPUB 2', epub3: 'EPUB 3', kepub: 'KEPUB', cbz: 'CBZ', pdf: 'PDF' })[x.format] || x.format),
      h('span', { class: 'mono ep', title: x.path }, x.path.split(/[\\/]/).pop()),
      h('span', { class: 'muted' }, `${x.chapters} chương${x.autoUpdate ? ' · tự cập nhật' : ''}${x.exists ? '' : ' · file không còn'}`),
      h('button', { class: 'btn sm ghost', title: 'Tạo lại file với các chương mới nhất (giữ tuỳ chọn cũ)', onclick: async (e) => {
        await busy(e.currentTarget, async () => { try { const r = await api().shelf.exportRefresh(c.id, x.id); toast(`Đã cập nhật ${r.path.split(/[\\/]/).pop()} (${r.chapters} chương)`, 'ok'); } catch (err) { toast(errMsg(err), 'err'); } });
      } }, 'Cập nhật'),
      h('button', { class: 'btn sm ghost', onclick: () => api().shelf.revealExport(c.id, x.id) }, 'Mở thư mục'),
      h('button', { class: 'btn sm ghost', title: 'Bỏ khỏi danh sách (không xoá file)', onclick: async (e) => { await api().shelf.exportForget(x.id); e.currentTarget.closest('.export-row').remove(); } }, '×')))) : null;
    const body = h('div', { class: 'collection-view' },
      h('div', { class: 'cv-head' },
        cover(c.coverUrl, c.title, 'cv-cover'),
        h('div', { class: 'cv-info' },
          h('h2', {}, c.title),
          c.author ? h('div', { class: 'muted' }, c.author) : null,
          h('div', { class: 'muted' }, [c.source, KIND_LABEL[c.kind], `${c.chapters.filter((x) => x.state === 'ready').length}/${c.chapters.length} chương`, c.ongoing ? 'Đang ra' : ''].filter(Boolean).join(' · ')),
          c.genres?.length ? h('div', { class: 'cv-genres' }, c.genres.slice(0, 20).map((g) => h('span', { class: 'tagpill' }, g))) : null,
          c.description ? h('p', { class: 'cv-desc' }, c.description) : null,
          h('div', { class: 'row' }, readBtn,
            fromPlugin ? h('button', { class: 'btn', title: 'Đọc lại mục lục trên nguồn và tải các chương chưa có', onclick: async (e) => {
              await busy(e.currentTarget, async () => { try { await api().shelf.update(c.id); toast('Đang cập nhật chương mới — xem trang Tải xuống', 'ok'); closeModal(); } catch (err) { toast(errMsg(err), 'err'); } });
            } }, missing ? `Cập nhật / tải ${missing} chương còn thiếu` : 'Cập nhật chương mới') : null,
            c.remoteChapters ? h('button', { class: 'btn', title: 'Các chương này chỉ có link ảnh gốc trong file đã nhập: tải ảnh về để đọc offline và xuất lại đầy đủ', onclick: async (e) => {
              await busy(e.currentTarget, async () => { try { if (await api().shelf.fillRemote(c.id)) { toast('Đang tải ảnh — xem trang Tải xuống', 'ok'); closeModal(); } } catch (err) { toast(errMsg(err), 'err'); } });
            } }, `Tải ảnh ${c.remoteChapters} chương còn thiếu`) : null,
            h('button', { class: 'btn', onclick: () => openExportDialog(c) }, 'Xuất…'),
            h('button', { class: 'btn ghost danger', onclick: async () => { if (await api().shelf.remove(c.id)) { closeModal(); toast('Đã xoá khỏi thư viện', 'ok'); loadShelf(); } } }, 'Xoá')))),
      exports,
      h('div', { class: 'cv-chapters-head' }, h('h3', {}, `Chương (${c.chapters.length})`), fromPlugin ? dlSel : null),
      list,
      h('div', { class: 'modal-actions' }, h('button', { class: 'btn ghost', onclick: closeModal }, 'Đóng')));
    openModal(body);
    $('#modal-card').classList.add('wide');
    list.querySelector('.current')?.scrollIntoView({ block: 'center' });
  }

  // ---------------------------------------------------------------- export dialog (format + options)
  function openExportDialog(c) {
    const isComic = c.kind === 'comic' || (c.chapters.length && c.chapters.every((x) => x.kind === 'image' || x.state !== 'ready'));
    const saved = store.get('exportOptions', {});
    const st = {
      format: saved.format && (isComic || saved.format !== 'cbz') ? saved.format : 'epub2',
      scope: 'all', from: 1, to: c.chapters.length, includeIntro: saved.includeIntro ?? true, includeToc: saved.includeToc ?? true,
      showTitles: saved.showTitles ?? true, missing: c.pluginId ? (saved.missing || 'download') : 'skip', autoUpdate: Boolean(saved.autoUpdate),
    };
    const formats = [['epub2', 'EPUB 2'], ['epub3', 'EPUB 3'], ['kepub', 'KEPUB (Kobo)'], ['cbz', 'CBZ'], ['pdf', 'PDF']].filter(([f]) => f !== 'cbz' || isComic);
    const fmtValue = h('span', { class: 'opt-sub' });
    const fmtMenu = h('div', { class: 'opt-menu glass', hidden: true }, formats.map(([f, label]) => h('button', { class: 'opt-item', dataset: { f }, onclick: (e) => {
      e.stopPropagation(); st.format = f; fmtMenu.hidden = true; sync();
    } }, h('span', {}, label), h('span', { class: 'ck' }, '✓'))));
    const scopeSel = h('select', {},
      h('option', { value: 'all' }, 'Tất cả chương'), h('option', { value: 'downloaded' }, 'Chỉ chương đã tải'), h('option', { value: 'range' }, 'Khoảng chương…'));
    const from = h('input', { type: 'number', min: '1', max: String(c.chapters.length), value: '1', class: 'num' });
    const to = h('input', { type: 'number', min: '1', max: String(c.chapters.length), value: String(c.chapters.length), class: 'num' });
    const range = h('span', { class: 'row', hidden: true }, 'từ', from, 'đến', to);
    const missingSel = h('select', {},
      c.pluginId ? h('option', { value: 'download' }, 'Tải trước khi xuất') : null, h('option', { value: 'skip' }, 'Bỏ qua'),
      h('option', { value: 'link' }, c.remoteChapters ? 'Giữ link ảnh gốc (như vBook)' : 'Ghi liên kết tới chương gốc'));
    const sw = (key) => toggle(st[key], (v) => { st[key] = v; });
    const missingCount = c.chapters.filter((x) => x.state !== 'ready').length;
    const optRow = (icon, label, control, sub) => h('div', { class: 'opt-row' }, h('span', { class: 'opt-ic' }, icon), h('div', { class: 'opt-label' }, h('span', {}, label), sub || null), control);
    const go = h('button', { class: 'btn primary' }, 'Xuất');
    function sync() {
      fmtValue.textContent = formats.find(([f]) => f === st.format)?.[1] || st.format;
      fmtMenu.querySelectorAll('.opt-item').forEach((b) => b.classList.toggle('on', b.dataset.f === st.format));
      range.hidden = scopeSel.value !== 'range';
      st.scope = scopeSel.value;
      missingRow.hidden = st.scope === 'downloaded' || !missingCount;
    }
    scopeSel.addEventListener('change', sync);
    missingSel.value = st.missing;
    missingSel.addEventListener('change', () => { st.missing = missingSel.value; });
    const missingRow = optRow('⧗', `Chương chưa tải (${missingCount})`, missingSel);
    const fmtRow = h('div', { class: 'opt-row clickable', onclick: () => { fmtMenu.hidden = !fmtMenu.hidden; } },
      h('span', { class: 'opt-ic' }, '⎘'), h('div', { class: 'opt-label' }, h('span', {}, 'Format'), fmtValue), h('span', { class: 'chev' }, '›'), fmtMenu);
    go.addEventListener('click', async () => {
      const options = {
        scope: st.scope === 'range' ? 'range' : st.scope, from: Number(from.value) || 1, to: Number(to.value) || c.chapters.length,
        includeIntro: st.includeIntro, includeToc: st.includeToc, showTitles: st.showTitles, autoUpdate: st.autoUpdate,
        missing: st.missing === 'link' ? 'link' : 'skip',
      };
      store.set('exportOptions', { format: st.format, includeIntro: st.includeIntro, includeToc: st.includeToc, showTitles: st.showTitles, missing: st.missing, autoUpdate: st.autoUpdate });
      await busy(go, async () => {
        go.textContent = 'Đang xuất…';
        try {
          const r = await api().shelf.export({ collectionId: c.id, format: st.format, options, downloadMissing: st.missing === 'download' && st.scope !== 'downloaded' });
          if (!r) return;
          closeModal();
          if (r.queued) toast('Đang tải chương còn thiếu; file sẽ được xuất khi tải xong (xem trang Tải xuống)', 'ok', 6000);
          else toastAction(`Đã xuất ${r.path.split(/[\\/]/).pop()} · ${r.chapters} chương`, 'Mở thư mục', () => api().shelf.revealExport(c.id, r.id), 9000);
        } catch (err) { toast(`Xuất lỗi: ${errMsg(err)}`, 'err', 9000); }
      });
    });
    const head = h('div', { class: 'exp-head glass' }, cover(c.coverUrl, c.title, 'exp-cover'),
      h('div', {}, h('div', { class: 'exp-title' }, c.title), h('div', { class: 'accent' }, c.source || KIND_LABEL[c.kind] || ''),
        h('div', { class: 'muted small' }, `(${c.chapters.length} chương)`), h('div', { class: 'muted small' }, `Đã tải: ${c.chapters.filter((x) => x.state === 'ready').length}`)));
    const dialog = h('div', { class: 'export-dialog' }, head,
      h('h3', { class: 'opt-h' }, 'Format'), h('div', { class: 'opt-group glass top' }, fmtRow),
      h('h3', { class: 'opt-h' }, 'Options'),
      h('div', { class: 'opt-group glass' },
        optRow('≡', 'Phạm vi', h('span', { class: 'row' }, scopeSel, range)),
        missingRow,
        optRow('ⓘ', 'Kèm phần giới thiệu', sw('includeIntro'), h('span', { class: 'opt-sub' }, 'Bìa, tác giả, thể loại, mô tả')),
        optRow('☰', 'Kèm mục lục', sw('includeToc'), h('span', { class: 'opt-sub' }, 'Trang mục lục có link tới từng chương')),
        optRow('Tт', 'Hiện tên chương', sw('showTitles')),
        optRow('↻', 'Tự cập nhật file khi tải chương mới', sw('autoUpdate'))),
      h('div', { class: 'modal-actions' }, h('button', { class: 'btn ghost', onclick: closeModal }, 'Huỷ'), go));
    sync();
    openModal(dialog);
    $('#modal-card').classList.add('wide');
    document.addEventListener('click', function off(e) { if (!fmtRow.contains(e.target)) { fmtMenu.hidden = true; } if (!document.body.contains(dialog)) document.removeEventListener('click', off); });
  }

  // ================================================================ sources (vBook extensions)
  const src = { plugins: [], current: '', results: [], next: '', more: null, tab: 'browse' };

  async function loadPlugins() {
    try { src.plugins = await api().plugins.list(); } catch (err) { toast(errMsg(err), 'err'); src.plugins = []; }
    const usable = src.plugins.filter((p) => !p.broken && p.enabled && p.supported);
    const sel = $('#src-plugin');
    const keep = src.current || store.get('sourcePlugin', '');
    sel.replaceChildren(...usable.map((p) => h('option', { value: p.id }, `${p.name}${p.nsfw ? ' (18+)' : ''} · ${KIND_LABEL[p.capabilities.kind] || ''}`)));
    src.current = usable.some((p) => p.id === keep) ? keep : (usable[0]?.id || '');
    sel.value = src.current;
    $('#src-empty').hidden = usable.length > 0;
    renderInstalled();
    if (src.tab === 'browse') loadHome();
  }

  const plugin = (id = src.current) => src.plugins.find((p) => p.id === id);

  async function loadHome() {
    const row = $('#src-home');
    row.replaceChildren();
    $('#src-results').replaceChildren();
    $('#src-more').hidden = true;
    const p = plugin();
    if (!p) return;
    $('#src-search').disabled = !p.capabilities.search;
    $('#src-q').disabled = !p.capabilities.search;
    $('#src-genres').hidden = !p.capabilities.genre;
    if (!p.capabilities.home) { if (p.capabilities.search) $('#src-q').focus(); return; }
    let tabs = [];
    try { tabs = await api().plugins.home(p.id); } catch (err) { row.append(h('span', { class: 'danger' }, `home.js: ${errMsg(err)}`)); return; }
    row.replaceChildren(...tabs.map((t, i) => h('button', { class: `chip-btn${i === 0 ? ' on' : ''}`, onclick: (e) => {
      row.querySelectorAll('.chip-btn').forEach((b) => b.classList.toggle('on', b === e.currentTarget));
      runList(() => api().plugins.listItems(p.id, t.script, t.input, ''), (next) => api().plugins.listItems(p.id, t.script, t.input, next));
    } }, t.title)));
    if (tabs[0]) runList(() => api().plugins.listItems(p.id, tabs[0].script, tabs[0].input, ''), (next) => api().plugins.listItems(p.id, tabs[0].script, tabs[0].input, next));
  }

  let listSeq = 0;
  async function runList(first, more) {
    const seq = ++listSeq;
    const grid = $('#src-results');
    grid.classList.add('stale');
    $('#src-more').hidden = true;
    try {
      const r = await first();
      if (seq !== listSeq) return;
      src.results = r.items;
      src.next = r.next;
      src.more = more;
      grid.replaceChildren(...r.items.map(resultCard));
      if (!r.items.length) grid.append(h('div', { class: 'empty' }, 'Không có kết quả.'));
    } catch (err) {
      if (seq !== listSeq) return;
      grid.replaceChildren(h('div', { class: 'empty danger' }, errMsg(err)));
    } finally {
      if (seq === listSeq) { grid.classList.remove('stale'); $('#src-more').hidden = !src.next; }
    }
  }

  function resultCard(item) {
    return h('div', { class: 'book-card glass', tabindex: '0', title: item.name, onclick: () => openDetail(src.current, item.link) },
      h('div', { class: 'book-cover' }, cover(item.coverReferrerPolicy === 'no-referrer' ? thumb(item.cover, '') : thumb(item.cover, item.link), item.name), item.tag ? h('span', { class: 'book-flag' }, item.tag) : null),
      h('div', { class: 'book-meta' }, h('div', { class: 'book-title' }, item.name), item.description ? h('div', { class: 'book-sub muted' }, item.description) : null));
  }

  $('#src-plugin')?.addEventListener('change', (e) => { src.current = e.target.value; store.set('sourcePlugin', src.current); loadHome(); });
  const doSearch = () => {
    const q = $('#src-q').value.trim();
    const p = plugin();
    if (!q || !p) return;
    $('#src-home').querySelectorAll('.chip-btn').forEach((b) => b.classList.remove('on'));
    runList(() => api().plugins.search(p.id, q, ''), (next) => api().plugins.search(p.id, q, next));
  };
  $('#src-search')?.addEventListener('click', doSearch);
  $('#src-q')?.addEventListener('keydown', (e) => { if (e.key === 'Enter') doSearch(); });
  $('#src-more')?.addEventListener('click', async (e) => {
    if (!src.next || !src.more) return;
    await busy(e.currentTarget, async () => {
      try {
        const r = await src.more(src.next);
        const seen = new Set(src.results.map((x) => x.link));
        const fresh = r.items.filter((x) => !seen.has(x.link));
        src.results.push(...fresh);
        $('#src-results').append(...fresh.map(resultCard));
        src.next = fresh.length && r.next !== src.next ? r.next : '';
      } catch (err) { toast(errMsg(err), 'err'); }
    });
    $('#src-more').hidden = !src.next;
  });
  $('#src-genres')?.addEventListener('click', async () => {
    const p = plugin();
    if (!p) return;
    let list = [];
    try { list = await api().plugins.genre(p.id); } catch (err) { toast(errMsg(err), 'err'); return; }
    openModal(h('div', {}, h('h2', {}, `Thể loại · ${p.name}`), h('div', { class: 'genre-grid' }, list.map((g) => h('button', { class: 'chip-btn', onclick: () => {
      closeModal();
      $('#src-home').querySelectorAll('.chip-btn').forEach((b) => b.classList.remove('on'));
      runList(() => api().plugins.listItems(p.id, g.script, g.input, ''), (next) => api().plugins.listItems(p.id, g.script, g.input, next));
    } }, g.title))), h('div', { class: 'modal-actions' }, h('button', { class: 'btn ghost', onclick: closeModal }, 'Đóng'))));
  });
  const openUrlBox = () => { const u = $('#src-url').value.trim(); if (u) openByUrl(u); };
  $('#src-open')?.addEventListener('click', openUrlBox);
  $('#src-url')?.addEventListener('keydown', (e) => { if (e.key === 'Enter') openUrlBox(); });

  async function openByUrl(url) {
    if (!/^https?:\/\//i.test(url)) { toast('Link phải bắt đầu bằng http(s)://', 'err'); return; }
    let hits = [];
    try { hits = await api().plugins.match(url); } catch (err) { toast(errMsg(err), 'err'); return; }
    if (!hits.length) { toast('Không có extension nào đã cài khớp với link này. Cài extension của trang đó ở trang Nguồn truyện.', 'err', 8000); return; }
    openDetail(hits[0].id, url);
  }

  // ---------------------------------------------------------------- book detail (from an extension)
  async function openDetail(pluginId, url) {
    const p = plugin(pluginId) || { id: pluginId, name: pluginId };
    const box = h('div', { class: 'collection-view' }, h('div', { class: 'empty' }, `Đang đọc thông tin truyện từ ${p.name}…`));
    openModal(box);
    $('#modal-card').classList.add('wide');
    let detail;
    try { detail = await api().plugins.detail(pluginId, url); } catch (err) { box.replaceChildren(h('div', { class: 'empty danger' }, `detail.js: ${errMsg(err)}`), h('div', { class: 'modal-actions' }, h('button', { class: 'btn ghost', onclick: closeModal }, 'Đóng'))); return; }
    const selected = new Set();
    const chapterBox = h('div', { class: 'chapter-list' }, h('div', { class: 'empty' }, 'Đang đọc mục lục…'));
    const dlAll = h('button', { class: 'btn primary', onclick: () => download('all') }, 'Tải tất cả');
    const dlSel = h('button', { class: 'btn', disabled: true, onclick: () => download('selected') }, 'Tải chương đã chọn');
    const addOnly = h('button', { class: 'btn ghost', title: 'Chỉ thêm vào thư viện (mục lục), chưa tải chương nào', onclick: () => download('none') }, 'Thêm vào thư viện');
    let toc = [];
    async function download(scope) {
      try {
        await api().books.download({ pluginId, url, scope, chapterUrls: scope === 'selected' ? toc.filter((c) => selected.has(c.url)).map((c) => c.url) : [], label: detail.name });
        closeModal();
        toastAction(scope === 'none' ? 'Đang thêm vào thư viện' : `Đang tải ${detail.name}`, 'Xem', () => showPage('downloads'));
      } catch (err) { toast(errMsg(err), 'err'); }
    }
    box.replaceChildren(
      h('div', { class: 'cv-head' },
        cover(detail.cover ? thumb(detail.cover, detail.coverReferrerPolicy === 'no-referrer' ? '' : url) : '', detail.name, 'cv-cover'),
        h('div', { class: 'cv-info' },
          h('h2', {}, detail.name || url),
          detail.author ? h('div', { class: 'muted' }, detail.author) : null,
          h('div', { class: 'muted' }, [p.name, KIND_LABEL[detail.kind], detail.ongoing ? 'Đang ra' : 'Hoàn thành'].filter(Boolean).join(' · ')),
          detail.genres?.length ? h('div', { class: 'cv-genres' }, detail.genres.slice(0, 20).map((g) => h('span', { class: 'tagpill' }, g))) : null,
          detail.detail ? h('p', { class: 'cv-detail muted' }, detail.detail) : null,
          detail.description ? h('p', { class: 'cv-desc' }, detail.description) : null,
          h('div', { class: 'row' }, dlAll, dlSel, addOnly, h('button', { class: 'btn ghost', onclick: () => api().app.openExternal(url) }, 'Mở trang web')))),
      h('div', { class: 'cv-chapters-head' }, h('h3', { id: 'detail-count' }, 'Chương'),
        h('span', { class: 'row' }, h('button', { class: 'btn sm ghost', onclick: () => { chapterBox.querySelectorAll('input').forEach((i) => { i.checked = true; i.dispatchEvent(new Event('change')); }); } }, 'Chọn hết'),
          h('button', { class: 'btn sm ghost', onclick: () => { chapterBox.querySelectorAll('input').forEach((i) => { i.checked = false; i.dispatchEvent(new Event('change')); }); } }, 'Bỏ chọn'))),
      chapterBox,
      h('div', { class: 'modal-actions' }, h('button', { class: 'btn ghost', onclick: closeModal }, 'Đóng')));
    try {
      toc = await api().plugins.toc(pluginId, detail.url || url);
      $('#detail-count').textContent = `Chương (${toc.length})`;
      chapterBox.replaceChildren(...toc.map((ch) => {
        const cb = h('input', { type: 'checkbox', disabled: ch.locked });
        cb.addEventListener('change', () => { if (cb.checked) selected.add(ch.url); else selected.delete(ch.url); dlSel.disabled = !selected.size; dlSel.textContent = selected.size ? `Tải ${selected.size} chương` : 'Tải chương đã chọn'; });
        return h('label', { class: 'chapter-row' }, cb, h('span', { class: 'st' }, ch.locked ? '🔒' : ''), h('span', { class: 'ct' }, ch.name));
      }));
      if (!toc.length) chapterBox.replaceChildren(h('div', { class: 'empty' }, 'Extension không trả về chương nào.'));
    } catch (err) {
      chapterBox.replaceChildren(h('div', { class: 'empty danger' }, `toc.js: ${errMsg(err)}`));
      dlAll.disabled = true;
    }
  }

  // ---------------------------------------------------------------- installed extensions
  function renderInstalled() {
    const box = $('#src-plugins');
    if (!src.plugins.length) { box.replaceChildren(h('div', { class: 'empty glass' }, 'Chưa cài extension nào.')); return; }
    box.replaceChildren(...src.plugins.map((p) => {
      if (p.broken) return h('div', { class: 'plugin-row glass' }, h('div', { class: 'pl-ic ph' }, '!'), h('div', { class: 'pl-main' }, h('b', {}, p.id), h('div', { class: 'danger' }, p.broken)),
        h('button', { class: 'btn sm ghost danger', onclick: async () => { if (await api().plugins.uninstall(p.id).catch((e) => toast(errMsg(e), 'err'))) loadPlugins(); } }, 'Gỡ'));
      const caps = p.capabilities || {};
      return h('div', { class: `plugin-row glass${p.enabled ? '' : ' off'}` },
        p.icon ? h('img', { class: 'pl-ic', src: p.icon, alt: '' }) : h('div', { class: 'pl-ic ph' }, p.name.slice(0, 1)),
        h('div', { class: 'pl-main' },
          h('div', {}, h('b', {}, p.name), h('span', { class: 'muted' }, ` v${p.version} · ${p.author || '?'} · ${KIND_LABEL[caps.kind] || p.type}`), p.nsfw ? h('span', { class: 'tagpill danger' }, '18+') : null),
          h('div', { class: 'muted small mono' }, p.source),
          p.description ? h('div', { class: 'small' }, p.description) : null,
          p.unsupportedReason ? h('div', { class: 'warn small' }, p.unsupportedReason) : null,
          p.missing?.length ? h('div', { class: 'warn small' }, `Thiếu file: ${p.missing.join(', ')}`) : null,
          h('div', { class: 'muted small' }, ['home', 'genre', 'search', 'detail', 'page', 'toc', 'chap'].filter((k) => caps[k]).join(' · '))),
        h('div', { class: 'pl-actions' },
          h('label', { class: 'check', title: 'Bật / tắt extension' }, toggle(p.enabled, async (v) => { await api().plugins.setEnabled(p.id, v); loadPlugins(); }), 'Bật'),
          p.config?.length ? h('button', { class: 'btn sm ghost', onclick: () => configDialog(p) }, 'Cấu hình') : null,
          h('button', { class: 'btn sm ghost', title: 'Mở trang nguồn trong cửa sổ của Lynxx (vượt kiểm tra chống bot, đăng nhập); cookie dùng chung với extension', onclick: () => api().plugins.openSite(p.id).catch((e) => toast(errMsg(e), 'err')) }, 'Mở trang nguồn'),
          h('button', { class: 'btn sm ghost danger', onclick: async () => { try { if (await api().plugins.uninstall(p.id)) { toast(`Đã gỡ ${p.name}`, 'ok'); loadPlugins(); } } catch (e) { toast(errMsg(e), 'err'); } } }, 'Gỡ')));
    }));
  }

  function configDialog(p) {
    const inputs = p.config.map((c) => {
      let input;
      if (c.mode === 'select' && c.values?.length) input = h('select', {}, c.values.map((v) => h('option', { value: v.value }, v.title || v.value)));
      else if (c.mode === 'toggle') { input = h('input', { type: 'checkbox' }); input.checked = String(c.value) === 'true'; }
      else input = h('input', { type: 'text', value: c.value ?? '' });
      if (input.tagName === 'SELECT') input.value = c.value;
      return { c, input };
    });
    openModal(h('div', {}, h('h2', {}, `Cấu hình · ${p.name}`),
      h('div', { class: 'form-grid' }, inputs.map(({ c, input }) => h('label', { class: 'field' }, h('span', {}, c.title || c.key), input, c.subtitle ? h('small', { class: 'muted' }, c.subtitle) : null))),
      h('div', { class: 'modal-actions' }, h('button', { class: 'btn ghost', onclick: closeModal }, 'Huỷ'),
        h('button', { class: 'btn primary', onclick: async () => {
          const values = Object.fromEntries(inputs.map(({ c, input }) => [c.key, input.type === 'checkbox' ? String(input.checked) : input.value]));
          try { await api().plugins.setConfig(p.id, values); closeModal(); toast('Đã lưu cấu hình', 'ok'); loadPlugins(); } catch (e) { toast(errMsg(e), 'err'); }
        } }, 'Lưu'))));
  }

  const installed = (p) => { if (p) { toast(`Đã cài ${p.name} v${p.version}`, 'ok'); loadPlugins(); } };
  $('#src-install-folder')?.addEventListener('click', () => api().plugins.installFolder().then(installed, (e) => toast(errMsg(e), 'err')));
  $('#src-install-zip')?.addEventListener('click', () => api().plugins.installZip().then(installed, (e) => toast(errMsg(e), 'err')));
  $('#src-install-url')?.addEventListener('click', async () => {
    const r = await prompt2('Cài extension từ link', [{ label: 'Link plugin.zip', placeholder: 'https://…/plugin.zip' }]);
    if (r && r[0]) api().plugins.installUrl(r[0]).then(installed, (e) => toast(errMsg(e), 'err'));
  });

  // ---------------------------------------------------------------- repositories
  const repo = { data: null };
  async function renderRepoSaved() {
    let list = [];
    try { list = await api().plugins.repositories(); } catch { list = []; }
    $('#repo-saved').replaceChildren(...list.map((u) => h('span', { class: 'chip-btn', title: u },
      h('span', { onclick: () => { $('#repo-url').value = u; loadRepo(); } }, u.replace(/^https?:\/\//, '').slice(0, 60)),
      h('b', { class: 'x', title: 'Bỏ kho này', onclick: async (e) => { e.stopPropagation(); await api().plugins.repoRemove(u); renderRepoSaved(); } }, '×'))));
  }
  async function loadRepo() {
    const url = $('#repo-url').value.trim();
    if (!url) return;
    await busy($('#repo-load'), async () => {
      try { repo.data = await api().plugins.repoFetch(url); renderRepo(); } catch (err) { toast(errMsg(err), 'err'); }
    });
  }
  function renderRepo() {
    const box = $('#repo-items');
    if (!repo.data) { box.replaceChildren(); return; }
    const q = $('#repo-filter').value.trim().toLowerCase();
    const type = $('#repo-type').value;
    const items = repo.data.items.filter((it) => (!q || `${it.name} ${it.author} ${it.source} ${it.description}`.toLowerCase().includes(q))
      && (!type || (type === 'novel' ? ['novel', 'chinese_novel'].includes(it.type) : it.type === type)));
    box.replaceChildren(h('div', { class: 'muted small' }, `${items.length}/${repo.data.items.length} extension${repo.data.author ? ` · ${repo.data.author}` : ''}`), ...items.slice(0, 500).map((it) => {
      const update = it.installedVersion !== null && it.version > it.installedVersion;
      const btn = h('button', { class: `btn sm${update ? ' primary' : ''}`, disabled: !it.supportedType || (it.installedVersion !== null && !update), onclick: async (e) => {
        await busy(e.currentTarget, async () => {
          try { const p = await api().plugins.installUrl(it.path, repo.data.url); if (p) { installed(p); it.installedVersion = p.version; renderRepo(); } } catch (err) { toast(errMsg(err), 'err'); }
        });
      } }, !it.supportedType ? 'Chưa hỗ trợ' : update ? `Cập nhật v${it.version}` : it.installedVersion !== null ? 'Đã cài' : 'Cài');
      return h('div', { class: 'plugin-row glass' },
        it.icon ? h('img', { class: 'pl-ic', src: thumb(it.icon, ''), alt: '', loading: 'lazy', onerror: (e) => { e.target.replaceWith(h('div', { class: 'pl-ic ph' }, it.name.slice(0, 1))); } }) : h('div', { class: 'pl-ic ph' }, it.name.slice(0, 1)),
        h('div', { class: 'pl-main' },
          h('div', {}, h('b', {}, it.name), h('span', { class: 'muted' }, ` v${it.version} · ${it.author || '?'} · ${it.type}${it.locale ? ` · ${it.locale}` : ''}`), it.nsfw ? h('span', { class: 'tagpill danger' }, '18+') : null),
          h('div', { class: 'muted small mono' }, it.source), it.description ? h('div', { class: 'small' }, it.description) : null),
        h('div', { class: 'pl-actions' }, btn));
    }));
  }
  $('#repo-load')?.addEventListener('click', loadRepo);
  $('#repo-url')?.addEventListener('keydown', (e) => { if (e.key === 'Enter') loadRepo(); });
  $('#repo-save')?.addEventListener('click', async () => { const u = $('#repo-url').value.trim(); if (!u) return; try { await api().plugins.repoAdd(u); renderRepoSaved(); toast('Đã lưu kho', 'ok'); } catch (e) { toast(errMsg(e), 'err'); } });
  $('#repo-filter')?.addEventListener('input', debounce(renderRepo, 120));
  $('#repo-type')?.addEventListener('change', renderRepo);

  $$('#src-tabs button').forEach((b) => b.addEventListener('click', () => {
    src.tab = b.dataset.tab;
    $$('#src-tabs button').forEach((x) => x.classList.toggle('on', x === b));
    for (const t of ['browse', 'installed', 'repo']) $(`#src-${t}`).hidden = t !== src.tab;
    if (src.tab === 'repo') renderRepoSaved();
    if (src.tab === 'browse' && !$('#src-results').children.length) loadHome();
  }));

  // ================================================================ book download jobs (Tải xuống)
  const bookJobs = new Map();
  const PHASE = { queued: 'Đang chờ', detail: 'Đọc thông tin truyện', toc: 'Đọc mục lục', chapters: 'Đang tải chương', export: 'Đang xuất file', done: 'Xong', failed: 'Lỗi', cancelled: 'Đã huỷ', cancelling: 'Đang huỷ' };
  function upsertBookJob(job) {
    bookJobs.set(job.id, job);
    onFrame(renderBookJobs);
    if (['done', 'failed', 'cancelled'].includes(job.status) && shelf.loaded) loadShelf();
  }
  function renderBookJobs() {
    const box = $('#book-jobs');
    if (!box) return;
    const list = [...bookJobs.values()].sort((a, b) => b.createdAt - a.createdAt);
    box.replaceChildren(...list.map((j) => {
      const p = j.progress || {};
      const active = ['queued', 'running', 'cancelling'].includes(j.status);
      const pct = j.status === 'done' ? 100 : p.chaptersTotal ? Math.min(100, 100 * ((p.chaptersDone + p.chaptersFailed) + (p.pagesTotal ? p.pagesDone / p.pagesTotal : 0)) / p.chaptersTotal) : 0;
      const plug = plugin(j.pluginId);
      return h('div', { class: `job glass book-job st-${j.status}` },
        h('div', { class: 'job-head' },
          h('span', { class: `status ${j.status}` }, PHASE[j.status === 'running' ? p.phase : j.status] || j.status),
          h('span', { class: 'job-q' }, j.title || j.label || j.url),
          h('span', { class: 'muted' }, plug ? plug.name : ''),
          h('span', { class: 'grow' }),
          active ? h('button', { class: 'btn sm danger', onclick: () => api().books.cancel(j.id) }, 'Huỷ') : null,
          !active && j.collectionId ? h('button', { class: 'btn sm ghost', onclick: () => { showPage('shelf'); openCollection(j.collectionId); } }, 'Mở trong thư viện') : null),
        h('div', { class: `bar${j.status === 'running' && !p.chaptersTotal ? ' indet' : ''}` }, h('i', { style: `width:${pct.toFixed(1)}%` })),
        h('div', { class: 'job-meta muted' },
          p.chaptersTotal ? `${p.chaptersDone}/${p.chaptersTotal} chương` : (j.status === 'done' && !p.chaptersTotal ? 'Không có chương mới' : ''),
          p.chaptersFailed ? h('span', { class: 'danger' }, ` · ${p.chaptersFailed} chương lỗi`) : null,
          p.chaptersSkipped ? ` · ${p.chaptersSkipped} chương đã có / bị khoá` : '',
          p.current && active ? ` · ${p.current}${p.pagesTotal ? ` (${p.pagesDone}/${p.pagesTotal} trang)` : ''}` : '',
          j.error ? h('span', { class: 'danger' }, ` · ${j.error}`) : null),
        j.log?.length && !active ? h('details', { class: 'job-log' }, h('summary', {}, `Ghi chú (${j.log.length})`), h('pre', { class: 'mono' }, j.log.join('\n'))) : null);
    }));
    window.lynxxBookJobs = { active: list.filter((j) => ['queued', 'running', 'cancelling'].includes(j.status)).length, total: list.length };
    if (typeof updateJobsBadge === 'function') updateJobsBadge();
  }
  $('#jobs-clear')?.addEventListener('click', async () => { try { (await api().books.clear()); [...bookJobs.keys()].forEach((k) => { if (!['queued', 'running', 'cancelling'].includes(bookJobs.get(k).status)) bookJobs.delete(k); }); renderBookJobs(); } catch { /* older main */ } });

  // ================================================================ reader
  const rd = {
    collection: null, chapter: null, mode: store.get('readerMode', 'vertical'), dir: store.get('readerDir', 'ltr'), font: store.get('readerFont', 18),
    page: 0, saveTimer: 0, observer: null,
  };
  const shellEl = () => $('#reader-shell');
  $('#rd-mode').value = rd.mode;
  $('#rd-dir').value = rd.dir;

  async function openReader(collectionId, chapterId) {
    if (!chapterId) { toast('Chưa có chương nào đã tải để đọc', 'err'); return; }
    showPage('reader');
    try {
      rd.collection = await api().shelf.collection(collectionId); // fresh: chapters may have been downloaded since
    } catch (err) { toast(errMsg(err), 'err'); return; }
    await loadChapter(chapterId);
  }

  function renderToc() {
    const sel = $('#rd-toc');
    const c = rd.collection;
    if (!c) { sel.replaceChildren(); sel.hidden = true; return; }
    sel.hidden = c.chapters.length < 2;
    sel.replaceChildren(...c.chapters.map((ch) => h('option', { value: ch.id, disabled: ch.state !== 'ready' }, `${ch.title}${ch.state !== 'ready' ? ' (chưa tải)' : ''}`)));
    if (rd.chapter) sel.value = rd.chapter.chapterId;
    $('#rd-book').textContent = c.title;
  }

  async function loadChapter(chapterId) {
    saveProgress(true);
    let data;
    try { data = await api().reader.chapter(chapterId); } catch (err) { toast(`Không mở được chương: ${errMsg(err)}`, 'err'); return; }
    if (data.upgradedFrom) { rd.collection = await api().shelf.collection(data.collectionId); }
    rd.chapter = data;
    renderToc();
    $('#rd-prev').disabled = !data.prevId;
    $('#rd-next').disabled = !data.nextId;
    $('#rd-end').hidden = true;
    $('#reader-empty').hidden = true;
    const pages = $('#reader-pages');
    const text = $('#reader-text');
    const frame = $('#reader-frame');
    pages.hidden = data.kind !== 'images';
    text.hidden = data.kind !== 'text';
    frame.hidden = data.kind !== 'pdf';
    $('#rd-font').hidden = data.kind !== 'text';
    $('#rd-mode').disabled = data.kind !== 'images';
    $('#rd-dir').disabled = data.kind !== 'images' || rd.mode === 'vertical';
    const progress = data.progress || null;
    if (data.kind === 'images') renderImages(data, progress);
    else if (data.kind === 'text') renderText(data, progress);
    else if (data.kind === 'pdf') { frame.src = data.url || ''; $('#rd-ind').textContent = ''; }
    else {
      $('#reader-empty').hidden = false;
      $('#reader-empty').textContent = data.kind === 'pending' ? 'Chương này chưa được tải. Mở truyện trong Thư viện để tải.' : 'Không đọc được loại nội dung này.';
    }
    if (rd.collection) {
      const ch = rd.collection.chapters.find((x) => x.id === chapterId);
      if (ch) rd.collection.lastChapterId = chapterId;
    }
    // remember the chapter even before the first scroll
    saveProgressSoon();
  }

  function renderImages(data, progress) {
    const box = $('#reader-pages');
    box.className = `reader-pages mode-${rd.mode} dir-${rd.dir}`;
    rd.page = Math.min(progress?.page || 0, Math.max(0, data.pages.length - 1));
    box.replaceChildren(...data.pages.map((p, i) => h('div', { class: 'rd-page', dataset: { i: String(i) } },
      h('img', { src: p.url, alt: `Trang ${p.number}`, loading: i < 4 || Math.abs(i - rd.page) < 3 ? 'eager' : 'lazy', draggable: 'false' }))));
    if (rd.mode === 'vertical') box.append(h('div', { class: 'rd-tail' }, data.nextId ? h('button', { class: 'btn primary', onclick: () => loadChapter(data.nextId) }, 'Chương sau ›') : h('span', { class: 'muted' }, 'Hết chương')));
    rd.observer?.disconnect();
    rd.observer = new IntersectionObserver((entries) => {
      for (const e of entries) if (e.isIntersecting && e.intersectionRatio >= 0.5) { rd.page = Number(e.target.dataset.i); updateIndicator(); saveProgressSoon(); }
    }, { root: box, threshold: [0.5] });
    box.querySelectorAll('.rd-page').forEach((el) => rd.observer.observe(el));
    requestAnimationFrame(() => showImagePage(rd.page, false));
    updateIndicator();
    box.focus({ preventScroll: true });
  }

  function showImagePage(i, smooth = true) {
    const box = $('#reader-pages');
    const pages = box.querySelectorAll('.rd-page');
    if (!pages.length) return;
    rd.page = Math.max(0, Math.min(pages.length - 1, i));
    if (rd.mode === 'paged') pages.forEach((p, k) => { p.classList.toggle('shown', k === rd.page); });
    else pages[rd.page].scrollIntoView({ behavior: smooth ? 'smooth' : 'auto', block: 'start', inline: 'start' });
    updateIndicator();
    saveProgressSoon();
  }

  function updateIndicator() {
    const d = rd.chapter;
    if (!d) return;
    if (d.kind === 'images') $('#rd-ind').textContent = `${rd.page + 1}/${d.pages.length}`;
  }

  function readerCss() {
    const cs = getComputedStyle(document.documentElement);
    const v = (n, d) => (cs.getPropertyValue(n).trim() || d);
    return `html{background:${v('--panel-solid', '#0d1b21')};color-scheme:${document.documentElement.dataset.theme === 'paper' ? 'light' : 'dark'};}
      html,body{margin:0;color:${v('--text', '#eee')};}
      body{font-family:"Noto Serif","Source Serif 4",Georgia,serif;font-size:${rd.font}px;line-height:1.7;padding:24px max(24px,calc((100vw - 760px)/2)) 80px;}
      h1,h2,h3{line-height:1.3} h1.ch{font-size:1.5em;text-align:center;margin:.2em 0 1.2em}
      p{margin:0 0 .8em;text-align:justify} img{max-width:100%;height:auto;display:block;margin:1em auto}
      a{color:${v('--accent', '#f80')}} table{border-collapse:collapse} td,th{border:1px solid ${v('--border-strong', '#555')};padding:4px 8px}
      .page img{margin:0 auto}`;
  }

  function renderText(data, progress) {
    const frame = $('#reader-text');
    const html = `<!doctype html><html><head><meta charset="utf-8"><style>${readerCss()}</style></head><body><h1 class="ch">${escapeHtml(data.title)}</h1>${data.html}</body></html>`;
    frame.onload = () => {
      const doc = frame.contentDocument;
      if (!doc) return;
      // links never navigate the reader: in-book anchors scroll, web links open in the browser
      doc.addEventListener('click', (e) => {
        const a = e.target.closest?.('a[href]');
        if (!a) return;
        e.preventDefault();
        const href = a.getAttribute('href') || '';
        if (href.startsWith('#')) doc.getElementById(href.slice(1))?.scrollIntoView();
        else if (/^https?:\/\//i.test(href)) api().app.openExternal(href);
      });
      doc.addEventListener('keydown', onReaderKey);
      const win = frame.contentWindow;
      const scroller = doc.scrollingElement || doc.documentElement;
      if (progress && progress.scrollOffset > 0) scroller.scrollTop = progress.scrollOffset * Math.max(1, scroller.scrollHeight - win.innerHeight);
      win.addEventListener('scroll', () => {
        const max = Math.max(1, scroller.scrollHeight - win.innerHeight);
        const frac = scroller.scrollTop / max;
        $('#rd-ind').textContent = `${Math.round(frac * 100)}%`;
        $('#rd-end').hidden = !(frac > 0.995 && rd.chapter?.nextId);
        saveProgressSoon();
      }, { passive: true });
      $('#rd-ind').textContent = '';
      frame.focus();
    };
    frame.srcdoc = html;
  }

  function escapeHtml(s) { return String(s ?? '').replace(/&/g, '&amp;').replace(/</g, '&lt;').replace(/>/g, '&gt;').replace(/"/g, '&quot;'); }

  function currentOffset() {
    const d = rd.chapter;
    if (!d) return 0;
    if (d.kind === 'text') {
      const doc = $('#reader-text').contentDocument;
      const win = $('#reader-text').contentWindow;
      const scroller = doc?.scrollingElement;
      if (!scroller || !win) return 0;
      return Math.min(1, scroller.scrollTop / Math.max(1, scroller.scrollHeight - win.innerHeight));
    }
    return 0;
  }

  function saveProgressSoon() {
    clearTimeout(rd.saveTimer);
    rd.saveTimer = setTimeout(() => saveProgress(false), 700);
  }

  function saveProgress(now) {
    const d = rd.chapter;
    if (!d || !['images', 'text', 'pdf'].includes(d.kind)) return;
    if (now) clearTimeout(rd.saveTimer);
    api().reader.progressSet({ chapterId: d.chapterId, page: d.kind === 'images' ? rd.page : 0, scrollOffset: currentOffset(), mode: d.kind === 'images' ? rd.mode : 'vertical', zoom: 1, direction: rd.dir }).catch(() => {});
  }

  function step(delta) {
    const d = rd.chapter;
    if (!d) return;
    if (d.kind !== 'images' || rd.mode === 'vertical') return;
    const forward = rd.dir === 'rtl' ? -delta : delta;
    const next = rd.page + forward;
    if (next >= d.pages.length) { if (d.nextId) loadChapter(d.nextId); return; }
    if (next < 0) { if (d.prevId) loadChapter(d.prevId); return; }
    showImagePage(next);
  }

  function onReaderKey(e) {
    if (!$('#page-reader').classList.contains('active')) return;
    if (e.target && ['INPUT', 'SELECT', 'TEXTAREA'].includes(e.target.tagName)) return;
    const d = rd.chapter;
    if (e.key === 'f' || e.key === 'F' || e.key === 'F11') { e.preventDefault(); toggleFullscreen(); return; }
    if (e.key === '[') { if (d?.prevId) loadChapter(d.prevId); return; }
    if (e.key === ']') { if (d?.nextId) loadChapter(d.nextId); return; }
    if (!d) return;
    const paging = d.kind === 'images' && rd.mode !== 'vertical';
    if (e.key === 'ArrowLeft') { e.preventDefault(); if (paging) step(-1); else if (d.prevId) loadChapter(d.prevId); }
    else if (e.key === 'ArrowRight') { e.preventDefault(); if (paging) step(1); else if (d.nextId) loadChapter(d.nextId); }
    else if (paging && (e.key === 'PageDown' || e.key === ' ')) { e.preventDefault(); step(rd.dir === 'rtl' ? -1 : 1); }
    else if (paging && e.key === 'PageUp') { e.preventDefault(); step(rd.dir === 'rtl' ? 1 : -1); }
    else if (paging && e.key === 'Home') showImagePage(0);
    else if (paging && e.key === 'End') showImagePage(d.pages.length - 1);
  }
  document.addEventListener('keydown', onReaderKey);

  function toggleFullscreen() {
    if (document.fullscreenElement) document.exitFullscreen().catch(() => {});
    else $('#page-reader').requestFullscreen().catch((err) => toast(errMsg(err), 'err'));
  }

  $('#reader-pages')?.addEventListener('click', (e) => {
    if (rd.mode !== 'paged' || !rd.chapter || rd.chapter.kind !== 'images') return;
    const box = e.currentTarget.getBoundingClientRect();
    const left = e.clientX < box.left + box.width / 2;
    step(left ? -1 : 1);
  });
  $('#rd-mode')?.addEventListener('change', (e) => {
    rd.mode = e.target.value;
    store.set('readerMode', rd.mode);
    $('#rd-dir').disabled = rd.mode === 'vertical';
    if (rd.chapter?.kind === 'images') renderImages(rd.chapter, { page: rd.page });
  });
  $('#rd-dir')?.addEventListener('change', (e) => {
    rd.dir = e.target.value;
    store.set('readerDir', rd.dir);
    if (rd.chapter?.kind === 'images') renderImages(rd.chapter, { page: rd.page });
  });
  $('#rd-font')?.addEventListener('click', (e) => {
    const b = e.target.closest('button');
    if (!b) return;
    rd.font = Math.max(12, Math.min(32, rd.font + Number(b.dataset.step) * 2));
    store.set('readerFont', rd.font);
    const doc = $('#reader-text').contentDocument;
    if (doc?.body) doc.body.style.fontSize = `${rd.font}px`;
  });
  $('#rd-toc')?.addEventListener('change', (e) => loadChapter(e.target.value));
  $('#rd-prev')?.addEventListener('click', () => { if (rd.chapter?.prevId) loadChapter(rd.chapter.prevId); });
  $('#rd-next')?.addEventListener('click', () => { if (rd.chapter?.nextId) loadChapter(rd.chapter.nextId); });
  $('#rd-end-next')?.addEventListener('click', () => { if (rd.chapter?.nextId) loadChapter(rd.chapter.nextId); });
  $('#rd-full')?.addEventListener('click', toggleFullscreen);
  $('#rd-back')?.addEventListener('click', () => { saveProgress(true); showPage('shelf'); });
  $('#reader-open')?.addEventListener('click', async (e) => {
    const ok = await importFiles(e.currentTarget);
    if (ok && ok[0]) { rd.collection = null; openReader(ok[0].collectionId, ok[0].chapterId); }
  });
  window.addEventListener('beforeunload', () => saveProgress(true));

  // ================================================================ wiring
  window.onLynxxPage = (name) => {
    if (name === 'shelf') loadShelf();
    if (name === 'sources') { if (!src.plugins.length) loadPlugins(); else renderInstalled(); }
    if (name !== 'reader') saveProgress(true);
  };
  window.onLynxxBoot = async () => {
    loadShelf();
    loadPlugins();
    try { (await api().books.jobs()).forEach(upsertBookJob); } catch { /* older main */ }
    try { api().on('book:update', upsertBookJob); } catch { /* older preload */ }
  };
  window.lynxxBooks = { openCollection, openReader, loadShelf, loadPlugins };
})();
