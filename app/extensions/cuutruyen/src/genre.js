// the site has no list of its tags: the most used ones (slug, name), most used first
var TAGS = [
  ["comedy", "comedy"],
  ["romance", "romance"],
  ["slice-of-life", "slice of life"],
  ["drama", "drama"],
  ["dang-tien-hanh", "đang tiến hành"],
  ["hai-huoc", "hài hước"],
  ["doi-thuong", "đời thường"],
  ["oneshot", "oneshot"],
  ["fantasy", "fantasy"],
  ["romcom", "romcom"],
  ["lang-man", "lãng mạn"],
  ["school-life", "school life"],
  ["hoc-duong", "học đường"],
  ["da-hoan-thanh", "đã hoàn thành"],
  ["action", "action"],
  ["hanh-dong", "hành động"],
  ["shounen", "shounen"],
  ["supernatural", "supernatural"],
  ["seinen", "seinen"],
  ["sieu-nhien", "siêu nhiên"],
  ["ecchi", "ecchi"],
  ["tam-ly", "tâm lý"],
  ["phieu-luu", "phiêu lưu"],
  ["co-mau", "có màu"],
  ["bao-luc", "bạo lực"],
  ["bi-an", "bí ẩn"],
  ["adventure", "adventure"],
  ["yuri", "yuri"],
  ["bi-kich", "bi kịch"],
  ["mystery", "mystery"],
];

function execute() {
  return Response.success(TAGS.map(function (t) { return { title: t[1], input: '/tags/' + t[0], script: 'tag.js' }; }));
}
