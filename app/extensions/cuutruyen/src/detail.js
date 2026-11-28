load('common.js');

function execute(url) {
  var id = mangaId(url);
  var json = id ? getJson('/mangas/' + id) : null;
  if (!json || !json.data) return Response.error('Không đọc được truyện ' + url);
  var d = json.data;
  var tags = d.tags || [];
  var info = [];
  if (d.author && d.author.name) info.push('Tác giả: ' + d.author.name);
  if (d.team && d.team.name) info.push('Nhóm dịch: ' + d.team.name);
  info.push((d.chapters_count || 0) + ' chương');
  if (d.official_url) info.push('Bản chính thức: ' + d.official_url);
  return Response.success({
    url: BASE + '/mangas/' + d.id,
    name: d.name,
    cover: fixUrl(d.cover_url || d.cover_mobile_url),
    author: d.author ? d.author.name : '',
    description: d.full_description || d.description || '',
    detail: info.join('<br>'),
    ongoing: tags.some(function (t) { return t.slug === 'dang-tien-hanh'; }),
    nsfw: d.is_nsfw === true,
    genres: tags.map(function (t) { return { title: t.name, input: '/tags/' + t.slug, script: 'tag.js' }; }),
    host: BASE,
  });
}
