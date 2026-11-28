load('common.js');

function execute(input, page) {
  var json = getJson(withPage(input, page));
  return listPage(json, json && json.data && json.data.mangas);
}
