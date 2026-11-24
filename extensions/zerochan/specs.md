# Zerochan (zc) — extension specs

- Site: https://www.zerochan.net/
- Engine: custom (Zerochan's own read-only API), written by hand from the 'custom' scaffold
- robots.txt: found. `Crawl-delay: 10` is its first line, before any `User-agent` group (see "robots.txt" below)
- Interval: 10 s (`_get_min_request_interval`), owner's decision of 2026-11-19
- Tag list: `zc_tags.json`, made by the extension's own update `tags.py:read_tags` (public tag directory `/tags?p=N`). Not committed (`.gitignore` leaves out `extensions/*/*_tags.json`): the list stays next to `manifest.json` on the maintainer's machine and goes into the draft zip (`.extgen-drafts/`) and the catalog zip (`tools/build_catalog.py`: the list next to the manifest, or `--lists <dir>`)
- Generated: lynxx.extgen `new --engine custom --no-tags`, then downloader.py / tags.py written for this site (2026-11-19)

## API terms (https://www.zerochan.net/api, read 2026-11-19)
- "a simple read-only API to programmatically request data": automated access is allowed (no R3 stop).
- GET only; most endpoints answer when `json` or `xml` is appended to the normal url.
  Documented: `/?p=1&l=..&s=id|fav&t=..&d=..&c=..&json`, `/{Tag}?json` (+ `&strict`), `/{Tag1},{Tag2}?json`, `/{id}?json`; `l` 1–250.
- Rate limit: 60 requests per minute ("chronically exceeding" may lead to a ban).
- The User-Agent must hold the project name and the client's Zerochan username (example "My AI learning app - MyUsername");
  without it requests may still work but the project "may be banned for being anonymous".

## Owner's decision (2026-11-19)
- Build it with a 10 s interval (robots.txt Crawl-delay, stricter than the API's 60/min).
- The user puts their own username in the User-Agent through the app's per-site headers:
  Settings → Kết nối → "Headers theo site (JSON)": `{"zc": {"User-Agent": "Lynxx - <username>"}}`.
  The module default (`LYNXX_USER_AGENT`) never overrides a header the user set (lynxx/network.py). The extension never asks
  for, nor stores, a username or a key (R12). The manifest description says this.

## robots.txt
```
Crawl-delay: 10            <- first line, outside any group
User-agent: *              Disallow: /forum /*?ref=* /notifications /comments /usertags /*?justified /*?screen /*?m=
User-agent: Googlebot ...  (search engines) same + /similar, /*xml*, /*json*, page+offset mixes, /*?referer=, /*?post=
```
- The paths used (`/<tag>?p=..&l=100&s=id&xml`, `/<id>?json`, `/tags?p=N`) are allowed for `*` (the `/*xml*` / `/*json*`
  rules belong to the search engine group only).
- RFC 9309 has no group for a line before the first `User-agent`: Lynxx's parser (lynxx/robots.py) read crawl-delay 0, so
  the generator (survey, record of 2026-11-19) paced this site at 1 s. Fixed in PR #30 (2026-11-19): a Crawl-delay before
  any group applies to every agent, so the generator and the tag updater (this extension's `tags.py`) now wait 10 s.
  The downloader's own 10 s applies to download runs in the app.

## Endpoints used (evidence, 2026-11-19, generator User-Agent)
| Purpose | Url | Answer |
|---|---|---|
| listing (search) | `/Hatsune+Miku?p=1&l=3&s=id&xml` | 200 text/xml (RSS), 100 items (ignores `l`), ids newest first, `media:keywords` = all tags, `media:content` = 600 px sample + original width/height, `media:thumbnail` = 240 px avif; `<description>Zerochan has 84,165 …` |
| deep listing page | `/Hatsune+Miku?p=100&l=100&s=id&xml` | 200, 100 items, next `p=101` |
| too deep | `/Hatsune+Miku?p=600&l=100&s=id&xml` | 403 "Page number too high" (guest page limit, somewhere in 101..600) |
| past the guest posts | `/hatsune+miku+(default+outfit)?p=30&l=100&s=id&xml` | 200, empty channel, title "page 30 of 30" while the feed says 2,917 posts: the site's count includes posts guests cannot see |
| lower case tag | `/hatsune+miku+(default+outfit)?p=1&…&xml` | 200 directly (no redirect): tag urls are case-insensitive |
| one post | `/3248583?json` | 200 application/json: `id, small, medium, large, full, width, height, size, hash, primary, tags` (no date, no source) |
| json listing (not used) | `/Hatsune+Miku?json`, `/Hatsune+Miku?p=1&l=3&s=id&json` | **500** application/json, body `{\r\n{"items": […]}` followed by the html page: unreadable by the core (5xx is retried then dropped) |
| html listing (not used) | `/Hatsune+Miku?s=id` | 200, 48 posts, a direct `static.zerochan.net/….full.<id>.<ext>` link per post, only the primary tag, "59,952 images" |
| tag directory | `/tags`, `/tags?p=100` | 200 html, 299 tags per page, most used first, `<li class="<type>">…<a href="/Tag">Tag</a> 3,004,036</li>`, `?p=N+1` Next link; page 100 ends at 85 posts |

The guest RSS listing and the guest html listing hold the same posts (48 / 48 ids of the html page found in the RSS page):
members only posts are left out of both.

## How the downloader works
- Search url: Lynxx tags joined with `+`, `_` → space, urlencoded like the site (`quote_plus`, `/` → `%2F`, `(` → `%28`),
  joined with `,`: `/<tag>[,<tag>]?p=N&l=100&s=id&xml`. One tag per website query (`maxTags: 1`,
  `localPositiveOverflow: true`): Zerochan cannot exclude tags, so every `-tag` (and every extra positive tag) is checked
  locally on the tags of the listing (they include the parent tags, ex. `vocaloid` on Hatsune Miku posts).
  A `-tag` that still reaches the url stops the run with a clear message (never silently ignored).
- Every post carries all its tags from the listing (lower case, spaces as `_`), so the tag / safety filters (R9) run before
  any per-post request.
- Count: the feed's count is an upper bound (members only posts included). The exact guest count comes from the last page that
  holds posts: 1 request for searches up to 100 posts, 2 when the last page is where the count says, else a binary search
  (2,917 → 6–7 requests). Never deeper than 100 pages: a bigger search is capped at its 10,000 newest posts with a warning
  (`MAX_PAGES = 0` for the core, the cap is in the count).
- File url: `/<id>?json` → `full`, asked only for posts that are downloaded (after the filters): one request per downloaded
  post, 10 s apart (~6 posts per minute). `-lowres` uses the listing's 600 px sample instead (no extra request).
  The json body is parsed strictly, with a fallback on the `"full"` field for stray characters around it.
- Dates: the API has none (listing nor post): no `DATE_FILTER` capability, posts are undated. No `SOURCES` either.
- Capabilities: `PREVIEW` (240 px avif thumbnail of the listing).

## R6 reasoning
- Only urls the API returns are downloaded: `full` of `/<id>?json` (or the listing's own sample url with `-lowres`).
  Nothing is built from the id, the md5 or the thumbnail path (gallery-dl style `static.zerochan.net/.full.<id>.jpg` guessing
  is not done).
- Members only posts are not in the guest listing, so they are never requested. A post whose `/<id>?json` has no `full`
  (or an unknown file type) is skipped with a warning.
- No login, no cookies, no key: public data only (R1, R12).

## Tag list decision (tags.py)
- The API has no tag listing, but the public directory `/tags?p=N` (allowed by robots.txt) lists 299 tags per page with their
  post counts, most used first, and the tag type as the row class.
- `read_tags` reads it until a page without a Next link, a page whose last count is below `job.min_posts`, or 100 pages:
  the ~30,000 most used tags (≥ ~85 posts on 2026-11-19), 100 requests (~17 min: the core's tag updater honors the 10 s
  Crawl-delay since PR #30). The whole directory (every tag down to 1 post) would need well over a thousand pages,
  so rarer tags are left to the app, which learns them from posts.
- Types: mangaka / studio → artist; character → character; series / game / movie / ova / artbook / visual novel / vtuber →
  copyright; source / meta / medium → metadata; theme, hair, eyes, outfit, group, ecchi… → general. Names lower case.
- Harness run (2026-11-19, budget 4, 10 s): 897 tags from 3 pages (4 requests incl. robots.txt, 30.8 s), 0 bad, all unique
  (general 735, copyright 68, metadata 48, character 38, artist 8); stopped by the test budget as expected.
- Shipped list (2026-11-19, `python -m lynxx.tags_updater --modules zc --min-posts 2`): 29,899 tags, 101 requests, 1,001 s
  (10 s apart); the core skipped 1 row that was not a valid tag. Manifest `tagUpdate` `tags.py:read_tags`, `minSdkVersion` 1.1.
- **Tag names with HTML codes are kept on purpose** (`licht&lt;リヒト&gt;`, `シンフォ\\└(&#039;ω&#039;)」//`): they are
  Zerochan's own tag names (checked 2026-11-19: `/Licht%26lt%3Bリヒト%26gt%3B?…&xml` lists its 131 posts, `/Licht%3Cリヒト%3E?…&xml`
  answers 404). The downloader encodes them as `%26lt%3B`; the core refuses the unescaped form `licht<リヒト>`. Do not unescape
  them in `tags.py`.

## Test runs
- `record --query "pict_pear" --limit 3` (2026-11-19): every check passed on the first run: count 86, 3 file links
  (`static.zerochan.net/….full.<id>.jpg|png`), tags 86 / 86, previews. 6 live requests (robots.txt, the listing twice — count and
  download runs — and 3 × `/<id>?json`).
- `check`: ok, replay 5 requests, same posts as the recorded run.

## No id searches (1.1.0, 2026-11-21)
- Zerochan has no id (or date) filter in its API (only `p` and `l`), so an `id:` query is searched as a tag. Lynxx appends
  `id:>N` to a scheduled re-run of a saved query ("Chỉ post mới từ bây giờ"), and "Tải đã chọn" / "Theo tên file" search `id:` too: on this
  module they gave wrong results. 1.1.0 declares `"search": {"idSearch": false}` (Lynxx 2.0.11 manifest key): the app then
  never sends an `id:` query here (no "only newer posts" schedule; "Tải đã chọn" and "Theo tên file" are disabled).
- No `metaNames`: the API takes tag names only (its "meta tags" such as Fanart are tag pages, typed `metadata` in the list; the
  listing filters `d=`, `c=`, `strict` are not used). Artist / character / series are plain tags of
  the tag list's types (mangaka / studio as artist; the type chips of Tags A-Z), nothing to declare.
- Lynxx 2.0.7 to 2.0.10 refuse this manifest ("unknown field(s): search") and keep 1.0.0: publish 1.1.0 in the official catalog
  only after 2.0.11 is released (operations.md, the catalog gate for `search`).

## Limits / open points
- 10,000 newest posts per search at most (guest page limit); no date filter; one tag per website query.
- Tags whose real name holds `_` (ex. `tcp_ppp`) are searched as `tcp ppp`; tag alias redirects were not tested.
- The JSON listing answers HTTP 500 (2026-11-19); if Zerochan fixes it, it would allow `l` up to 250 per page.
- The html listing has a direct full size link per post: reading it next to the RSS page would save the per-post request,
  at the cost of a second listing format (48 posts per page) to keep in step. Not done in this draft.

## Review checklist
- [x] robots.txt allows the paths used; the 10 s Crawl-delay is the module interval (and the core's since PR #30, see above)
- [x] API terms allow automated read-only access; the User-Agent rule is in the manifest description
- [x] no login, cookie or challenge bypass; only public data
- [x] request interval: 10 s constant
- [x] file urls come from the API's own fields (`full`, sample); nothing built for hidden posts
- [x] `record` and `check` pass
