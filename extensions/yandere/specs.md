# yande.re (yr) — extension specs

- Site: https://yande.re/
- Engine: moebooru_json (Moebooru JSON API: post.json), confidence high
- robots.txt: found, crawl-delay 0.0 s
- Tag list: `yr_tags.json`, made by the extension's own update `tags.py:read_tags` (manifest `tagUpdate`, SDK 1.1): `tag.json?limit=1000&page=N&order=name` until a short page. Lynxx runs it only when the user asks (Tags A-Z, "Cập nhật"). The list in the zips is made with `lynxx-core tags --modules yr` (robots.txt applies). Not committed (`.gitignore` leaves out `extensions/*/*_tags.json`): the list stays next to `manifest.json` on the maintainer's machine and goes into the draft zip (`.extgen-drafts/`) and the catalog zip (`tools/build_catalog.py`: the list next to the manifest, or `--lists <dir>`)
- Generated: lynxx.extgen (template)

## Terms (read 2026-11-19)
- https://yande.re/static/terms_of_service: nothing about automated access (uploads, content, copyright, privacy). https://yande.re/help/api: "mostly compatible with Danbooru API (version 1.13.0) to make scripting easy"; 100 posts per request at most, HTTP 421 "User Throttled". robots.txt: a sitemap only.
- 1 request per second.

## Evidence
- `https://yande.re/index.php?page=dapi&s=post&q=index&limit=1` → 404 text/html
- `https://yande.re/posts.json?limit=1` → 404 text/html
- `https://yande.re/post.json?limit=1` → 200 application/json

## Test runs (2026-11-19)
- `record --query "seifuku" --limit 3`: ok (count 158,688, 3 file links); `check`: ok (replay, same posts).
- A real download run with the core: the files downloaded (then deleted).

## Favorites (1.0.1, 2026-11-21)
- The site's favorites search is `vote:3:<user name>` (cheat sheet: "Search for posts favorited by the user Bob"). The core routes
  favorites only as `favorited_by:<name>` (manifest `syntax.fav`), so 1.0.0's `fav` regex `vote:3:[^:]+?` could never match and
  `favorited_by:` was refused. 1.0.1: `syntax.fav` is `favorited_by:[^:]+?`; the downloader's `_query_tags` sends it as
  `vote:3:<name>` (`-favorited_by:` as `-vote:3:`). `DownloaderJsonApi` keeps the tag in the query (no favorites conversion), and
  every address (search, count, id cursor, Preview) goes through `_query_tags`. `order:vote` (vote time) works only with it.
- User names: lowercase only (the core's metatag characters). Typing the site's own `vote:3:<name>` still works as before.
- Not tested live on yande.re (the survey's request budget was used up); the same engine answered guests on Sakugabooru
  (`extensions/sakugabooru/specs.md`). Works on Lynxx 2.0.7 and later (no new manifest key, SDK 1.1 names only).
- Pools: `pool:<id>` already works (results come in pool order, not id order: the date limits and the id cursor assume id
  order; a later version declares the pool kind with the site's order, Lynxx 2.0.11 manifest `search`).

## Shipped tag list
- `python -m lynxx.tags_updater --modules yr --min-posts 2` (the extension's own `tags.py`, 1 request per second):
  90,001 tags, 138 requests, 144 s. `tag.json` by name, 1000 per page, ended by a short page; 136,753 rows read, 28 not valid tags skipped by the core. yande.re keeps few general tags (465 with 2 posts or more): most of the list is artists and characters. Compressed: 723 KB.

## Review checklist
- [x] robots.txt / terms of the site allow automated access to the paths used
- [x] no login, cookie or challenge bypass; only public data (or the user's own API key)
- [x] request interval is polite (>= 0.5 s for an API, more if the site asks)
- [x] file urls come from the site's own fields (no urls built for posts the site hides)
- [x] test run (`python -m lynxx.extgen check <folder>`) passes
