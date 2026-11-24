# Sakugabooru (sk) — extension specs

- Site: https://www.sakugabooru.com/
- Engine: moebooru_json (Moebooru JSON API: post.json), confidence high
- robots.txt: found, crawl-delay 0.0 s
- Tag list: `sk_tags.json`, made by the extension's own update `tags.py:read_tags` (manifest `tagUpdate`, SDK 1.1): `tag.json?limit=1000&page=N&order=name` until a short page. Lynxx runs it only when the user asks (Tags A-Z, "Cập nhật"). The list in the zips is made with `lynxx-core tags --modules sk` (robots.txt applies). Not committed (`.gitignore` leaves out `extensions/*/*_tags.json`): the list stays next to `manifest.json` on the maintainer's machine and goes into the draft zip (`.extgen-drafts/`) and the catalog zip (`tools/build_catalog.py`: the list next to the manifest, or `--lists <dir>`)
- Generated: lynxx.extgen (template)

## Terms (read 2026-11-19)
- https://www.sakugabooru.com/static/terms_of_service: nothing about automated access (same text as yande.re); /help/api: Moebooru API, 100 posts per request at most, 421 when throttled. robots.txt (Cloudflare managed): `Allow: /` for every agent, AI training crawlers disallowed by name, `Content-Signal: ai-train=no`.
- 1 request per second. Fixtures: recorded with `western`, because the posts of most tags link their YouTube source and the domain denylist scan (R7) reads the fixtures too.

## Evidence
- `https://www.sakugabooru.com/index.php?page=dapi&s=post&q=index&limit=1` → 404 text/html
- `https://www.sakugabooru.com/posts.json?limit=1` → 404 application/json
- `https://www.sakugabooru.com/post.json?limit=1` → 200 application/json

## Test runs (2026-11-19)
- `record --query "western" --limit 3`: ok (count 17,313, 3 file links); `check`: ok (replay, same posts).
- A real download run with the core: the files downloaded (then deleted).

## Favorites and tag types (1.0.1, 2026-11-21)
- Favorites: as yande.re (same engine): `syntax.fav` is `favorited_by:[^:]+?` and `_query_tags` sends it as the site's
  `vote:3:<user name>` (1.0.0's `vote:3:` regex could never match what the core routes as favorites). Lowercase names.
- Checked live as a guest (1 request, 2026-11-21, no cookies; robots.txt of the same day, `User-agent: *` / `Allow: /`):
  `post.json?tags=vote:3:<user>&limit=1`, with the top uploader of the posts of pool 249 (survey data) as `<user>`, answered 200
  with 1 post from 2026-11-17, not the site's newest post (ids grow by about 90 a day): guests get favorites results.
- Tag types (`tags.py` TYPES): Sakugabooru's own types are General 0, Artist 1, Copyright 3, Terminology 4, Meta 5 (the `/tag`
  page's type menu; `presumed`, 37,867 posts, is `meta`). 1.0.0 used yande.re's mapping (5 = circle -> artist, 4 = character),
  so meta tags such as `presumed` and `remake` were listed as artists. 1.0.1: 4 -> general, 5 -> metadata. The list shipped
  with 1.0.0 keeps the old types until it is made again (`python -m lynxx.tags_updater --modules sk --min-posts 2`) or the user
  updates it (Tags A-Z, "Cập nhật").
- Works on Lynxx 2.0.7 and later (no new manifest key, SDK 1.1 names only).

## Shipped tag list
- `python -m lynxx.tags_updater --modules sk --min-posts 2` (the extension's own `tags.py`, 1 request per second):
  15,511 tags, 24 requests, 24 s. `tag.json` by name, 1000 per page, ended by a short page; 22,573 rows read, 19 not valid tags skipped by the core. Compressed: 152 KB.

## Review checklist
- [x] robots.txt / terms of the site allow automated access to the paths used
- [x] no login, cookie or challenge bypass; only public data (or the user's own API key)
- [x] request interval is polite (>= 0.5 s for an API, more if the site asks)
- [x] file urls come from the site's own fields (no urls built for posts the site hides)
- [x] test run (`python -m lynxx.extgen check <folder>`) passes
