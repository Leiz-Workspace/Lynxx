# TBIB (tb) — extension specs

- Site: https://tbib.org/
- Engine: gelbooru_dapi (Gelbooru 0.2 DAPI (XML): index.php?page=dapi&s=post&q=index), confidence high
- robots.txt: found, crawl-delay 0.0 s
- Tag list: `tb_tags.json`, made by the extension's own update `tags.py:read_tags` (manifest `tagUpdate`, SDK 1.1): the 50,000 most used tags from the html tag list sorted by count (see "Tag list" below). Lynxx runs it only when the user asks (Tags A-Z, "Cập nhật"). The list in the zips is made with `python -m lynxx.tags_updater --modules tb` (robots.txt applies). Not committed (`.gitignore` leaves out `extensions/*/*_tags.json`): the list stays next to `manifest.json` on the maintainer's machine and goes into the draft zip (`.extgen-drafts/`) and the catalog zip (`tools/build_catalog.py`: the list next to the manifest, or `--lists <dir>`)
- Generated: lynxx.extgen (template)

## Terms (read 2026-11-19)
- https://tbib.org/tos.php: "You will not use any automated process to retrieve or index any portion of the site or site contents, with the exception of public search engines." No API exemption. Personal use only.
- R3: forbidden. **Owner override, 2026-11-19:** asked "bạn cho phép vượt R3 với site nào?", the owner chose TBIB (with Gelbooru and Realbooru). Downloads use the DAPI only (open to guests): favorites / pools (html pages) are refused, 1 request per second. The tag update reads one html page type, the tag list (below), because the DAPI tag index cannot give a usable list.

## 'or' groups (1.0.1, 2026-11-21)
- The site's cheat sheet: `~tag1 ~tag2` "Search for posts that have tag1 or tag2. (Currently does not work)". 1.0.0 sent an 'or'
  group as `( a ~ b )` (the `DownloaderGelbooru` default); 1.0.1 splits it into one search per alternative, merged by the core.
- Favorites / pools stay refused: html pages only, and the owner's override covers the DAPI only (`syntax.fav` / `syntax.pool`
  are `""`, so the parser refuses `favorited_by:` / `pool:` and the app never offers them).
- Works on Lynxx 2.0.7 and later (no new manifest key, SDK 1.1 names only).

## Evidence
- `https://tbib.org/index.php?page=dapi&s=post&q=index&limit=1` → 200 text/xml

## Test runs (2026-11-19)
- `record --query "1girl" --limit 3`: ok (count 8,173,669, 3 file links); `check`: ok (replay, same posts).
- A real download run with the core: the files downloaded (then deleted).

## Tag list
- robots.txt disallows `page=atom`, `remove.php`, multi-tag html searches (`page=post&s=list&tags=*+*`) and `page=history`:
  the tag list `index.php?page=tags&s=list` is allowed.
- Not the DAPI tag index: `limit=1000` answers 100 tags (8,259 bytes for both `limit=100` and `limit=1000`), in hash order
  (neither id nor name nor count), and the html tag list's last page is `pid=2331150`: about 2.33 million tags, over
  23,000 DAPI requests, far past the core's budget of 5,000 (a first run was stopped after 2.5 hours and 289,000 tags).
- `tags.py` reads `index.php?page=tags&s=list&sort=desc&order_by=index_count&pid=<offset>` (50 rows: count, `tag-type-<type>`,
  name), most used first, until a page below `job.min_posts`, an empty or repeated page, or `MAX_PAGES` 1,000: the 50,000
  most used tags. Counts seen on 2026-11-19: 329 posts at offset 50,000, 129 at 100,000, 35 at 250,000. Rarer tags are
  missing from the list (the core counts a missing tag as rare) and the app learns them from posts.
- Rows with the same count can move between pages while the list is read: a name is given once.
- Shipped list (2026-11-19, `python -m lynxx.tags_updater --modules tb --min-posts 2`): 49,798 tags (50,000 rows, 202 seen
  twice as equal counts moved between pages), down to 329 posts, 1,001 requests, 1,005 s; general 36,773, character 5,667,
  artist 4,766, copyright 2,487, metadata 105. 2.1 MB (463 KB compressed).

## Review checklist
- [x] robots.txt allows the paths used; the terms forbid automated access: R3 overridden by the owner (above)
- [x] no login, cookie or challenge bypass; only public data (or the user's own API key)
- [x] request interval is polite (>= 0.5 s for an API, more if the site asks)
- [x] file urls come from the site's own fields (no urls built for posts the site hides)
- [x] test run (`python -m lynxx.extgen check <folder>`) passes
