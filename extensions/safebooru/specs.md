# Safebooru (sf) — extension specs

- Site: https://safebooru.org/
- Engine: gelbooru_dapi (Gelbooru 0.2 DAPI (XML): index.php?page=dapi&s=post&q=index), confidence high
- robots.txt: found, crawl-delay 0.0 s
- Tag list: `sf_tags.json`, made by the extension's own update `tags.py:read_tags` (manifest `tagUpdate`, SDK 1.1): DAPI tag index, `index.php?page=dapi&s=tag&q=index&limit=1000&pid=N` until an empty page. Lynxx runs it only when the user asks (Tags A-Z, "Cập nhật"). The list in the zips is made with `lynxx-core tags --modules sf` (robots.txt applies). Not committed (`.gitignore` leaves out `extensions/*/*_tags.json`): the list stays next to `manifest.json` on the maintainer's machine and goes into the draft zip (`.extgen-drafts/`) and the catalog zip (`tools/build_catalog.py`: the list next to the manifest, or `--lists <dir>`)
- Generated: lynxx.extgen (template)

## Terms (read 2026-11-19)
- https://safebooru.org/index.php?page=tos: automated access is forbidden "with the exception of public search engines or by using our API without hammering". API content: no ads, no paywalls.
- API only: favorites / pools (html pages) are refused; 1 request per second.

## Evidence
- `https://safebooru.org/index.php?page=dapi&s=post&q=index&limit=1` → 200 text/xml

## Test runs (2026-11-19)
- `record --query "1girl" --limit 3`: ok (count 3,751,607, 3 file links); `check`: ok (replay, same posts).
- A real download run with the core: the files downloaded (then deleted).

## Shipped tag list
- `python -m lynxx.tags_updater --modules sf --min-posts 2` (the extension's own `tags.py`, 1 request per second):
  629,396 tags, 1,002 requests, 1,275 s. DAPI tag index, 1000 tags per page: 999,113 rows (the last page, pid 999, holds 113; pid 1000 is empty), 86 not valid tags skipped by the core. The file is 24.8 MB (5.0 MB compressed, within the catalog limits of 10 MB packed / 50 MB unpacked).
- **Tag names with HTML codes are kept on purpose** (48 old tags such as `&gt;_&lt;`, `adam&#039;s_apple`, next to `>_<`,
  `adam's_apple`): the list gives them as the site does, and the site finds the same posts with either form (checked
  2026-11-19: `>_<` and `&gt;_&lt;` both 53,563 posts, `adam's_apple` and `adam&#039;s_apple` both 3,268). Do not unescape
  them in `tags.py`.

## Review checklist
- [x] robots.txt / terms of the site allow automated access to the paths used
- [x] no login, cookie or challenge bypass; only public data (or the user's own API key)
- [x] request interval is polite (>= 0.5 s for an API, more if the site asks)
- [x] file urls come from the site's own fields (no urls built for posts the site hides)
- [x] test run (`python -m lynxx.extgen check <folder>`) passes
