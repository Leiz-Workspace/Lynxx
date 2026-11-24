# Gelbooru (gb) — extension specs

- Site: https://gelbooru.com/
- Engine: gelbooru_dapi (Gelbooru 0.2 DAPI (XML): index.php?page=dapi&s=post&q=index), confidence low
- robots.txt: found, crawl-delay 0.0 s
- Tag list: `gb_tags.json`, made by the extension's own update `tags.py:read_tags` (manifest `tagUpdate`, SDK 1.1): DAPI tag index as json sorted by post count, `index.php?page=dapi&s=tag&q=index&json=1&orderby=count&order=desc&limit=100&pid=N`, down to 20 posts (1.0.2, below), with the user's key. Lynxx runs it only when the user asks (Tags A-Z, "Cập nhật"). The list in the zips is made with `lynxx-core tags --modules gb` (robots.txt applies). Not committed (`.gitignore` leaves out `extensions/*/*_tags.json`): the list stays next to `manifest.json` on the maintainer's machine and goes into the draft zip (`.extgen-drafts/`) and the catalog zip (`tools/build_catalog.py`: the list next to the manifest, or `--lists <dir>`)
- Generated: lynxx.extgen (template)

## Terms (read 2026-11-19)
- https://gelbooru.com/tos.php: "You will not use any automated process to retrieve or index any portion of the site or site contents, with the exception of public search engines." Personal use only.
- API docs https://gelbooru.com/index.php?page=wiki&s=view&id=18780: keys (`&api_key=...&user_id=...`, account options) and throttling "to mitigate abusive behavior"; the API terms speak to "all bots, apps and websites" (no ads, no paywalls on API content). Anonymous DAPI requests answer HTTP 401: the user's own key is required (manifest `auth.apiKey`, capability API_KEY_REQUIRED).
- R3: the terms forbid automation and the API docs address bots and apps, so it is unclear. **Owner override, 2026-11-19:** asked "bạn cho phép vượt R3 với site nào?", the owner chose Gelbooru (with TBIB and Realbooru). Built API only: favorites / pools (html pages) are refused, 1 request per second.
- Live-tested on 2026-11-21 with the owner's key (1.0.2, below); `extgen record` was not run.

## Favorites, pools, 'or' groups (1.0.1, 2026-11-21)
- gelbooru.com's cheat sheet has `fav:<user id>` ("posts favorited by the user with the id of 1", roughly 25k posts searchable)
  and `pool:<id>` (roughly 50k), and the API docs say the DAPI `tags` take "all the meta-tags". Both stay inside the DAPI search,
  so they fit the owner's API-only override: no html page is read.
- 1.0.1: `syntax.fav` `favorited_by:\d+?` (the numeric user id, as on the site), sent as `fav:<id>` by
  `_consume_custom_module_tags`; `syntax.pool` `pool:\d+?`, sent as typed. The downloader keeps no favorites user / pool id, so
  `DownloaderGelbooru` never builds its html favorites / pool addresses; 1.0.0's refusals (-460 / -461) are gone.
- 'or' groups: the site documents `tag1 {tag2 ~ tag3}`, which "does not work with meta tags", not the `( a ~ b )` that
  `DownloaderGelbooru` sends: 1.0.1 splits them into one search per alternative (as rule34.xyz / Realbooru do).
- **Not tested live** in 1.0.1 (no key). 1.0.2: `pool:1` answered a valid empty list (below); `favorited_by:<id>` and an 'or'
  group are still untested.
- Works on Lynxx 2.0.7 and later (no new manifest key, SDK 1.1 names only).

## 1.0.2 (2026-11-21): gelbooru.com answers child elements
The owner's report: "Tag gb: failed: tags.py:read_tags gave no tag (expected (name, posts, type) for each tag of the site)", with
a key entered (KEY,USER_ID).

**What was wrong** (1.0.0 and 1.0.1 were made without a key, from rule34.xxx's answers):
- Tags: `s=tag&q=index` answers `<tags type="array" limit="100" offset="0" count="1487082"><tag><id>827834</id><name>..</name>
  <count>8</count><type>4</type><ambiguous>0</ambiguous></tag>..`: child elements. `tags.py` read rule34.xxx's attributes
  (`<tag id=".." name=".." .../>`), found none, and the core stopped with the error above. `limit=1000` answers 100 tags.
- Posts: `s=post&q=index` answers `<posts limit="2" offset="0" count="14630217"><post><id>15305114</id><created_at>Sat Nov 21
  00:31:00 -0600 2026</created_at>..<tags>..</tags><file_url>https://img4.gelbooru.com/images/..</file_url>..</post>`: child
  elements again, while `DownloaderGelbooru` reads attributes (`' id="'`, `file_url="`, `created_at="`...). Search, Preview and
  downloads were broken too (ids and file urls not found); the count (`<posts count=..>`) was right.
- Search depth: `limit=1&pid=20001` answers the text "Too deep! Pull it back some. Holy fuck." (no posts), and so does
  `tags=rating:general&limit=100&pid=201` (offset 20,100; one more request after the review): the site limits the offset, pages
  of 100 end at `pid=199` (offset 19,900), 20,000 posts. 1.0.1 had rule34.xxx's 200,099: a search of 20,001 to 200,099 posts read by page numbers would have
  asked for pages the site answers "Too deep!".

**1.0.2**
- `downloader.py`: search pages reach `_get_all_post_tags` as bytes (`_parses_raw_pages`); each `<post>` with child elements
  becomes the attribute item string the core reads (`_post_item_string`: id first, then every field in the site's order, entities
  decoded once, `"` as `'`, line breaks as spaces, tags on single spaces, `<parent_id>0</parent_id>` left empty so
  `-include_parchi` never looks for a post 0). Not read with the core's html parser: `<source>` and `<image>` are void elements
  there and lose their text. Posts with attributes and empty pages go to `DownloaderGelbooru`'s own reading, unchanged.
- Search depth: `MAX_SEARCH_DEPTH` 20,000 + 1, because the core refuses page numbers for a task of `MAX_SEARCH_DEPTH` posts or
  more (`_resolve_task_pages`). A task of up to 20,000 posts is read by page numbers (`pid` 0 to 199), with any search.
  - More than 20,000 posts: only a single task in the default order without date limits goes on, by id cursor (`id:<N`,
    `id:>N sort:id:asc`; the core's streaming pipeline). The extension's `_prefer_id_cursor` takes the cursor from
    `MAX_SEARCH_DEPTH` posts on (`DownloaderGelbooru` waits for one more post, which would leave that task to the refusal).
  - The others use page numbers only, and a task of more than 20,000 posts is refused: nothing is downloaded, and the log says
    "too many results, can only fetch html for 201 pages! Try adding an ID filter." (the core counts one page too many). These
    are 'or' groups (one task per alternative here, never streamed), `sort:` tags, `-mindate` / `-maxdate` (the date is not
    part of the site query, so the count is the whole search) and `-include_parchi`. Popular tags are far past 20,000 posts
    (each of the 100 most used has 2,741,737 or more): narrow such a search with `id:<N` / `id:>N` or another tag.
- `tags.py`: `index.php?page=dapi&s=tag&q=index&json=1&orderby=count&order=desc&limit=100&pid=N` with the user's key, most used
  first, until a page whose last tag has fewer than the floor, an empty or repeated page. Floor = max(job.min_posts, 20).
  `tag` may be one object; names are html unescaped once, lowercased, spaces as `_`; the nameless rank 0 (14,115,642 posts) is
  skipped; type 6 (deprecated) is left out (82 of the 100 most used tags are deprecated misspellings such as `1firl`, 8,540,770
  posts, and `1_girl`, which would come before the real tags in autocompletion; 4 of 100 at rank 100,000, none at 300,000); they
  still count for the floor, the log line says how many were left out. No key: stops before any request; HTTP 401 / 403, "Missing authentication" or a json
  error: "the site needs your own API key (KEY,USER_ID)"; a first page that is not a json tag list: the error quotes its first
  200 characters (a later one stops the read, keeping the tags read, with that quote in the log); a page not sorted by count: an
  error (the floor would stop at a random tag).
- Floor 20 and its cost: rank 100,000 had 94 posts, rank 300,000 15: about 250,000 tags, 2,500 pages, inside the core's budget of
  5,000 requests a list. The app sets no time limit on an update; at the core's pace for extension sites (one request a second
  at least, answers of ~7.5 KB) about 45 minutes. Without an order the index is not stable across pages (a tag on page 0 came
  again on page 1), so the old page by page read could not be kept.
- Same manifest keys, `minSdkVersion` 1.1: runs on Lynxx 2.0.7 and later (`TagJob.get_text` / `log` / `min_posts`,
  `_parses_raw_pages` are in 2.0.7).

**Recorded** (lynxx/tests/fixtures/gelbooru/, SOURCES.json; addresses with `&api_key=<KEY>&user_id=<UID>`, the key in no file):
18 requests in all on 2026-11-21, one at a time, 1 to 1.5 s apart (the last one: the depth check above, not recorded).
- Survey, 9 requests (lynxx.extgen SafeFetcher, robots.txt included): `s=tag&q=index&limit=1000&pid=0` (xml),
  `s=tag&q=index&limit=3&pid=0|1&json=1`, `s=post&q=index&limit=2` (xml), `s=tag&q=index&json=1&limit=100&orderby=count&order=desc&pid=0|1000|3000`.
- Check of 1.0.2 installed from the repository into a temporary data dir, 8 requests through the core (its tag update and
  the sidecar's Preview / count): robots.txt (found; allows the DAPI addresses) and `pid=0` of the tag update, run as
  `tags.update` does with the floor raised to 3,000,000 for that run only: 91 tags written to `gb_tags.json`, with types
  (general, metadata), by the build before type 6 was left out; 1.0.2 as shipped writes 13 tags from that page (78 deprecated
  left out, the recorded page in the tests). Preview `brother_and_sister rating:general` (`limit=10&pid=0`): 10 posts with ids, dates, ratings,
  sizes, md5, tags and thumbnail / sample urls; its count (`limit=100&pid=0`): 17,530. `pool:1`: a valid empty list (count 0).
  `rating:general` `limit=1&pid=20001`: "Too deep!". `rating:general id:<15305114` (`limit=3`): 15305113, 15305112,
  15305111; `rating:general id:>15305000 sort:id:asc`: 15305003, 15305006, 15305007. No media file was downloaded.

**Not tested**: a full tag update (about 2,500 requests); a download (media files); favorites (`favorited_by:<id>`, not run:
the only known ids are users' own); a pool with posts; an 'or' group; video posts (no video came in the answers);
`-include_parchi` with a real parent; the answer to a wrong key; date filters (`-mindate` / `-maxdate`) and `sort:` searches;
a search of more than 20,000 posts through the core (the id cursor was only checked by hand, 3 posts a request).

**Known limits** (offline tests, `lynxx/tests/test_gelbooru_ext.py`): a task of more than 20,000 posts is refused unless it
is a single task in the default order without date limits. 'or' groups, `sort:` (even with `-dlimit`), `-mindate` / `-maxdate`
and `-include_parchi` are refused above 20,000 posts a task.

## Evidence
- `https://gelbooru.com/index.php?page=dapi&s=post&q=index&limit=1` → 401 text/html
- `https://gelbooru.com/posts.json?limit=1` → 404 text/html
- `https://gelbooru.com/post.json?limit=1` → 404 text/html
- `https://gelbooru.com/api/v1/json/search/images?q=*&per_page=1` → 404 text/html
- `https://gelbooru.com/api/posts/?limit=1` → 404 text/html
- `https://gelbooru.com/api/danbooru/find_posts?limit=1` → 404 text/html
- `https://gelbooru.com/` → 200 text/html

## Notes
- The DAPI answers 401 without a key (the survey above, made without one): the downloader uses the DAPI with the user's own
  key (`&api_key=..&user_id=..`), the same as rule34.xxx.
- No shipped list: there is no `gb_tags.json`, in the draft zip nor next to the manifest (the core skips a missing list). The
  user builds it in the app with their own key (Tags A-Z, "Cập nhật"), or the maintainer makes it with
  `LYNXX_API_KEY_GB=KEY,USER_ID python -m lynxx.tags_updater --modules gb --out <dir>` and builds the catalog with
  `tools/build_catalog.py --lists <dir>`.
- `extgen check` still reports "no recorded test run" (`record` was not run); the answers recorded by hand are the fixtures above.

## Review checklist
- [x] robots.txt allows the paths used; the terms forbid automated access: R3 overridden by the owner (above)
- [x] no login, cookie or challenge bypass; only public data (or the user's own API key)
- [x] request interval is polite (>= 0.5 s for an API, more if the site asks)
- [x] file urls come from the site's own fields (no urls built for posts the site hides)
- [ ] test run (`python -m lynxx.extgen check <folder>`) passes: not run (1.0.2 was checked live by hand with the owner's key, above)
