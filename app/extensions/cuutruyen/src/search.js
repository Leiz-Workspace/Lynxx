load('common.js');

function execute(key, page) {
  var json = getJson(withPage('/mangas/search?q=' + encodeURIComponent(key), page));
  return listPage(json, json && json.data);
}
