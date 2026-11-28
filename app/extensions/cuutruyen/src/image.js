load('common.js');

// puts a page back together from its shuffled strips (see chap.js)
function execute(link) {
  var cut = link.indexOf(' ');
  var url = cut < 0 ? link : link.slice(0, cut);
  var r = fetch(url, { headers: { Referer: BASE + '/' } });
  if (!r.ok) return Response.error('Không tải được ảnh (HTTP ' + r.status + ')');
  if (cut < 0) return r.base64();
  var img = Graphics.createImage(r.base64());
  var canvas = Graphics.createCanvas(img.width, img.height);
  JSON.parse(link.slice(cut + 1)).forEach(function (s) {
    canvas.drawImage(img, 0, s[0], img.width, s[2], 0, s[1], img.width, s[2]);
  });
  return canvas.capture();
}
