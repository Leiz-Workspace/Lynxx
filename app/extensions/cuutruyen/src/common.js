// Cứu Truyện for Lynxx: the JSON API of the site (/api/v2). Shared by every script (load('common.js')).
var BASE = 'https://cuutruyen.net';
try { if (DOMAIN && /^https?:\/\/[^/]+/.test(DOMAIN)) BASE = String(DOMAIN).replace(/\/+$/, ''); } catch (e) { /* no config */ }
var API = BASE + '/api/v2';
var PER_PAGE = 24;

// the API still names the old storage hosts; the site's own reader swaps them for these before loading an image
function fixUrl(url) {
  return String(url || '')
    .replace(/^(?:https?:)?\/\/storage-ct\.lrclib\.net(?=\/|$)/, 'https://storage-bravo.cuutruyen.net')
    .replace(/^(?:https?:)?\/\/storage-ct-riften\.site(?=\/|$)/, 'https://storage-charlie.cuutruyen.net');
}

function getJson(path) {
  var r = fetch(API + path, { headers: { Accept: 'application/json', Referer: BASE + '/' } });
  return r.ok ? r.json() : null;
}

function withPage(path, page) {
  return path + (path.indexOf('?') < 0 ? '?' : '&') + 'page=' + (parseInt(page, 10) || 1) + '&per_page=' + PER_PAGE;
}

function mangaId(url) {
  var m = /mangas\/(\d+)/.exec(String(url));
  return m ? m[1] : '';
}

function toItem(m) {
  return {
    name: m.name,
    link: BASE + '/mangas/' + m.id,
    cover: fixUrl(m.cover_mobile_url || m.cover_url),
    description: m.newest_chapter_number ? 'Chương ' + m.newest_chapter_number : (m.author_name || ''),
    host: BASE,
  };
}

// one page of a paged list: {data | data.mangas, _metadata: {current_page, total_pages}}
function listPage(json, list) {
  if (!json) return Response.error('Cứu Truyện không trả lời (trang đang lỗi hoặc bị nhà mạng chặn)');
  var meta = json._metadata || {};
  var next = meta.current_page && meta.total_pages && meta.current_page < meta.total_pages ? String(meta.current_page + 1) : null;
  return Response.success((list || []).map(toItem), next);
}
