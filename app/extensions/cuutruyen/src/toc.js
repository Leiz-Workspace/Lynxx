load('common.js');

function execute(url) {
  var id = mangaId(url);
  var json = id ? getJson('/mangas/' + id + '/chapters') : null;
  if (!json || !json.data) return Response.error('Không đọc được danh sách chương của ' + url);
  var chapters = json.data.slice().sort(function (a, b) { return a.order - b.order; });
  return Response.success(chapters.map(function (c) {
    return { name: 'Chương ' + c.number + (c.name ? ': ' + c.name : ''), url: BASE + '/mangas/' + id + '/chapters/' + c.id, host: BASE };
  }));
}
