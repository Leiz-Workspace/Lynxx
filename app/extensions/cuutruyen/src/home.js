load('common.js');

function execute() {
  return Response.success([
    { title: 'Mới cập nhật', input: '/mangas/recently_updated', script: 'list.js' },
    { title: 'Nổi bật', input: '/mangas/top?duration=all', script: 'list.js' },
    { title: 'Nổi bật tuần', input: '/mangas/top?duration=week', script: 'list.js' },
    { title: 'Nổi bật tháng', input: '/mangas/top?duration=month', script: 'list.js' },
  ]);
}
