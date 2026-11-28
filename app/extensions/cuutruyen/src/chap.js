load('common.js');

// The site serves each page cut into horizontal strips in shuffled order. drm_data of a page is the base64 of
// "#v4|<dy>-<height>|..." XORed with the key of the site's reader: the k-th strip of the served image (from the top,
// heights in order) belongs at row dy of the page; image.js draws them back in place.
var DRM_KEY = '3141592653589793';

function strips(drm) {
  var bin = atob(String(drm).replace(/\s+/g, ''));
  var text = '';
  for (var i = 0; i < bin.length; i++) text += String.fromCharCode(bin.charCodeAt(i) ^ DRM_KEY.charCodeAt(i % DRM_KEY.length));
  if (text.indexOf('#v4|') !== 0) throw new Error('drm_data của trang có định dạng lạ');
  var out = [];
  var sy = 0;
  text.slice(4).split('|').forEach(function (part) {
    var p = part.split('-');
    var dy = parseInt(p[0], 10);
    var h = parseInt(p[1], 10);
    if (isNaN(dy) || isNaN(h) || h <= 0) throw new Error('drm_data của trang có định dạng lạ');
    out.push([sy, dy, h]);
    sy += h;
  });
  return out;
}

function execute(url) {
  var m = /chapters\/(\d+)/.exec(String(url));
  var json = m ? getJson('/chapters/' + m[1]) : null;
  if (!json || !json.data) return Response.error('Không đọc được chương ' + url);
  var pages = (json.data.pages || []).slice().sort(function (a, b) { return a.order - b.order; });
  return Response.success(pages.map(function (p) {
    var link = fixUrl(p.image_url);
    // the input of image.js: "<image url> <strips as JSON>"; a page without drm_data is a plain image
    return p.drm_data ? { link: link + ' ' + JSON.stringify(strips(p.drm_data)), script: 'image.js' } : link;
  }));
}
