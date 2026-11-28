<p align="center">
  <img src="app/assets/icons/full-mask-cyan.png" alt="Lynxx" width="128" height="128" />
</p>

# Lynxx

Lynxx is a desktop app to **download, read and export comics, novels and books**. Sites are added with extensions in
the **vBook format** (the same `plugin.json` + `src/*.js` packages the vBook app uses), and local files of about thirty
formats open in the same library and reader.

- **Library ("Thư viện")**: every book is a collection of chapters with cover, author, description and reading
  progress. Downloaded chapters are never fetched again; "Cập nhật chương mới" reads the site's chapter list again and
  downloads only what is new.
- **Sources ("Nguồn truyện")**: install vBook-format extensions from a folder, a `plugin.zip`, a link or a repository
  index (`…/plugin.json`), browse their home tabs and genres, search, open a book by pasting its link, pick chapters and
  download. Extension scripts run in a Chromium sandbox; their only way out is Lynxx's HTTP bridge (cookie jar per app,
  private addresses refused, per-host pacing, `Retry-After`).
- **Downloads ("Tải xuống")**: one job per book: chapter list, then every missing chapter; pages go to a staging folder
  (`.part` files resumed with `Range`, checked to be images, cancellable) before the library records them with their
  SHA-256.
- **Reader ("Đọc truyện")**: comics in vertical scroll, horizontal slide or single page, left-to-right or
  right-to-left (manga); novels as reflowable text with font size; PDF with Chromium's viewer. Table of contents,
  previous / next chapter, ←/→ keys, `[` `]` for chapters, F for full screen; the position is saved per chapter.
- **Export**: EPUB 2, EPUB 3, KEPUB (Kobo), CBZ (with `ComicInfo.xml` bookmarks) and PDF (comics: one page per image
  with chapter bookmarks; novels: printed by Chromium with a linked table of contents). Options: scope (all, downloaded,
  a range), introduction page (cover, author, genres, description), table of contents with links to every chapter,
  chapter titles, what to do with chapters not downloaded yet (download first, skip, keep the image links as vBook
  does), and keeping an exported file up to date when new chapters arrive.
- **Import from device**: epub, txt, mobi, prc, azw, azw3, fb2, fbz, umd, pdf, cbz, zip, html, htm, xhtml, md, markdown,
  chm, doc, dot, wps, docx, docm, dotx, dotm, odt, ott, fodt, rtf, hwp, hwpx, hwpml. Files are converted without any
  network access (pure-Python readers, sanitized XHTML); DRM-protected books are refused. The original file is kept.

Lynxx started as a fork of Lustra, a booru downloader; since 2.1.0 that part is gone and Lynxx is about comics, novels and
books only.

## Download

Installers for Linux (AppImage, deb), Windows (setup exe) and macOS (dmg, zip) are on the
[releases page](https://github.com/Leiz-Workspace/Lynxx/releases). The Windows and macOS builds are not signed: on macOS
open the app the first time with right click → Open.

## Run from source

```bash
npm --prefix app install
npm --prefix app start
```

The Electron app starts the Python sidecar (`python3 -m lynxx.server`, standard library only, Python 3.11 or newer) itself. Data lives in `~/.config/Lynxx`
(`library/` for books, `index/library.sqlite`, `plugins/` for extensions).

## Extensions

A vBook extension is a folder:

```
plugin.json      metadata (name, source, regexp, type comic | novel, …) and the script map
icon.png
src/home.js genre.js gen.js search.js detail.js page.js toc.js chap.js config.js …
```

Lynxx implements the vBook runtime (synchronous `fetch`, `Http`, `Html.parse` with Jsoup selectors, `Response`,
`load()`, CryptoJS, `localStorage`, `Engine.newBrowser()`); encrypted packages (`metadata.encrypt`) and the `tts`,
`translate`, `audio`, `video` types are not supported. Authors can run an extension outside the app:

```bash
node app/plugin/cli.js path/to/extension check
node app/plugin/cli.js path/to/extension detail "https://site.example/book"
node app/plugin/cli.js path/to/extension pack
```

Details: [docs/extensions.md](docs/extensions.md). With Claude Code, the `new-site-extension` skill writes and tests a
new extension from a site link.

## Tests

```bash
python3 -m unittest discover -s lynxx/tests -t .
npm --prefix app run check
# in Electron (needs a display, or xvfb-run -a):
npx --prefix app electron app/test/electron-smoke.js
npx --prefix app electron app/test/electron-e2e.js
```

Building the installers, the packaged self-test and the release flow (staging → release candidate, production → release):
[docs/operations.md](docs/operations.md).

## License

MIT. Lynxx is based on the original work of trickerer01 (see [THIRD_PARTY_NOTICES.md](THIRD_PARTY_NOTICES.md)).
