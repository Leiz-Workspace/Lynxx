// Screenshots of the UI for the README (docs/images/lynxx-*.png), dev only:
//   xvfb-run -a npx --prefix app electron app/test/readme-screenshots.js [--out=<folder>]
// The renderer runs with renderer/mock.js (invented books, covers drawn as SVG): the window's User-Agent does not name
// Electron, so app.js loads the mock bridge as in a plain browser. Nothing touches the network or a real profile.
'use strict';

const { app, BrowserWindow } = require('electron');
const fs = require('node:fs');
const os = require('node:os');
const path = require('node:path');

const arg = (k) => (process.argv.find((a) => a.startsWith(`--${k}=`)) || '').slice(k.length + 3);
const out = path.resolve(arg('out') || path.join(__dirname, '..', '..', 'docs', 'images'));
const index = path.join(__dirname, '..', 'renderer', 'index.html');
app.setPath('userData', fs.mkdtempSync(path.join(os.tmpdir(), 'lynxx-shots-')));
fs.mkdirSync(out, { recursive: true });

app.whenReady().then(async () => {
  const win = new BrowserWindow({ width: 1360, height: 860, useContentSize: true, show: true, backgroundColor: '#061015',
    webPreferences: { sandbox: true, contextIsolation: true } });
  win.webContents.setUserAgent('Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/142.0.0.0 Safari/537.36');
  await win.loadFile(index);
  const js = (code) => win.webContents.executeJavaScript(code, true);
  const wait = (ms) => new Promise((r) => setTimeout(r, ms));
  // the pointer starts where the X server puts it (xvfb-run: the middle of a 640x480 screen, over a card): it goes to an empty
  // corner of the sidebar first, so no card is shown hovered
  const park = () => win.webContents.sendInputEvent({ type: 'mouseMove', x: 20, y: 700 });
  const shot = async (name) => {
    park();
    await wait(500);
    fs.writeFileSync(path.join(out, name), (await win.webContents.capturePage()).toPNG());
    console.log('shot', name);
  };
  try {
    await wait(2500);
    await js("try { localStorage.clear(); } catch {} ; showPage('shelf')");
    await wait(800);
    await shot('lynxx-library.png');

    await js("showPage('sources')");
    await wait(2500);
    await shot('lynxx-sources.png');

    await js("showPage('shelf'); window.lynxxBooks.openCollection('book-1')");
    await wait(1200);
    await js("[...document.querySelectorAll('#modal button')].find((b) => b.textContent === 'Xuất…').click()");
    await wait(1200);
    await shot('lynxx-export.png');
    await js('closeModal()');

    await js("window.lynxx.books.download({ pluginId: 'comic-1', url: 'https://comic.example/b/1', scope: 'missing', label: 'Mèo Đen Ở Tiệm Sách' })");
    await wait(4800);
    await js("window.lynxx.books.download({ pluginId: 'novel-1', url: 'https://novel.example/b/2', scope: 'missing', label: 'Kiếm Đạo Độc Tôn' })");
    await wait(900);
    await js("window.lynxx.books.download({ pluginId: 'comic-1', url: 'https://comic.example/b/9', scope: 'all', label: 'Thợ Săn Bóng Đêm' })");
    await wait(1700);
    await js("showPage('downloads')");
    await shot('lynxx-downloads.png');
    await wait(4000);

    await js("window.lynxxBooks.openReader('book-2', 'nc1')");
    await wait(1800);
    await shot('lynxx-reader.png');
  } catch (err) {
    console.error(err);
    process.exitCode = 1;
  }
  app.quit();
});
