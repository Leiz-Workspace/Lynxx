# Hypnohub (hh) — extension specs

- Site: https://hypnohub.net/
- Engine: gelbooru_dapi (Gelbooru 0.2 DAPI (XML): index.php?page=dapi&s=post&q=index), confidence high
- robots.txt: found, crawl-delay 0.0 s
- Tag list: `hh_tags.json`, made by the extension's own update `tags.py:read_tags` (manifest `tagUpdate`, SDK 1.1): DAPI tag index, `index.php?page=dapi&s=tag&q=index&limit=1000&pid=N` until an empty page. Lynxx runs it only when the user asks (Tags A-Z, "Cập nhật"). The list in the zips is made with `lynxx-core tags --modules hh` (robots.txt applies). Not committed (`.gitignore` leaves out `extensions/*/*_tags.json`): the list stays next to `manifest.json` on the maintainer's machine and goes into the draft zip (`.extgen-drafts/`) and the catalog zip (`tools/build_catalog.py`: the list next to the manifest, or `--lists <dir>`)
- Generated: lynxx.extgen (template)

## Terms (read 2026-11-19)
- https://hypnohub.net/index.php?page=tos: the automation ban "does not apply to the API and its API content server(s)". Personal use only. No published rate limit, no key.
- API only: favorites / pools (html pages) are refused; 1 request per second.

## Evidence
- `https://hypnohub.net/index.php?page=dapi&s=post&q=index&limit=1` → 200 text/xml

## Test runs (2026-11-19)
- `record --query "femsub" --limit 3`: ok (count 195,716, 3 file links); `check`: ok (replay, same posts).
- A real download run with the core: the files downloaded (then deleted).

## Shipped tag list
- `python -m lynxx.tags_updater --modules hh --min-posts 2` (the extension's own `tags.py`, 1 request per second):
  38,335 tags, 69 requests, 68 s. DAPI tag index, 1000 tags per page, ended by an empty page; 66,332 rows read, 4 not valid tags skipped by the core. Compressed: 334 KB.

## Review checklist
- [x] robots.txt / terms of the site allow automated access to the paths used
- [x] no login, cookie or challenge bypass; only public data (or the user's own API key)
- [x] request interval is polite (>= 0.5 s for an API, more if the site asks)
- [x] file urls come from the site's own fields (no urls built for posts the site hides)
- [x] test run (`python -m lynxx.extgen check <folder>`) passes
