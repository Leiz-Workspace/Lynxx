# Realbooru (rb) — extension specs

- Site: https://realbooru.com/
- Engine: custom (Gelbooru 0.2 software, HTML pages; the DAPI is offline), written by hand (`downloader.py: DownloaderRb`)
- robots.txt: found, `User-agent: *` / `Disallow: /public/remove.php` only, no Crawl-delay. A copy (2026-11-21, 42 bytes as served)
  is saved next to the manifest (`robots.txt`): the pool and favorites addresses are checked against it before any request
  (SDK 1.2 `require_robots_allowed`, RFC 9309)
- Search kinds (1.1.0, Lynxx 2.0.11+, SDK 1.2): `pool:<id>` and `favorited_by:<user id>`, listings of their own (see "Pools and
  favorites" below)
- Tag list: `rb_tags.json`, made by the extension's own update `tags.py:read_tags` (manifest `tagUpdate`, SDK 1.1, see "Tag list (tags.py)" below). Lynxx runs it only when the user asks (Tags A-Z, "Cập nhật"). Not committed (`.gitignore` leaves out `extensions/*/*_tags.json`): the list stays next to `manifest.json` on the maintainer's machine and goes into the draft zip (`.extgen-drafts/`) and the catalog zip (`tools/build_catalog.py`: the list next to the manifest, or `--lists <dir>`)
- Content: real people (photos / videos, cosplay, porn). Every post carries its tags (R9: the safety list applies)
- Generated: lynxx.extgen (`new --engine custom --no-tags`), downloader.py and tags.py written by the AI session, 2026-11-19

## Terms of service and the R3 override

- Terms URL: https://realbooru.com/tos.php (redirects to the forum thread https://realbooru.com/index.php?page=forum&s=view&id=6749#264530)
- Clause: "You will not use any automated process to retrieve or index any portion of the site or site contents, with the
  exception of public search engines." The same list also says the site is for personal use only.
- This is an R3 STOP by default. **The owner of the project explicitly overrode R3 for this site on 2026-11-19**: asked
  "bạn cho phép vượt R3 với site nào?", they chose "Realbooru". The override covers R3 only.
- Rules that still apply: robots.txt (R2), >= 1 request per second for HTML pages (R4), no cookies, no login, no API key
  (R1 / R12), no ad circumvention beyond not loading ads (the downloader never loads scripts / ads), no CAPTCHA or Cloudflare
  bypass: stop on a challenge (R5), only file links the site gives (R6), the safety filter (R9).

## Endpoints (evidence, 2026-11-19)

- DAPI `index.php?page=dapi&s=post&q=index&limit=1` → 200 text/xml `<response success="false" reason="Search error: API offline
  ... shut off indefinitely ...">`; the DAPI tag index (`&s=tag`) answers the same. Not used.
- Listing `index.php?page=post&s=list&tags=<tags>&pid=<offset>` → 200 text/html. 42 posts per page, `pid` is an offset
  (paginator links `pid=42, 84, ...`). Each `<div class="col thumb" id="s<ID>">` holds `<a href=".../index.php?page=post&s=view&id=<ID>">`
  and `<img src="https://realbooru.com/thumbnails/xx/yy/thumbnail_<md5>.jpg" title="tag one, tag two, ...">`: the full tag
  list, joined by ', ' with '_' shown as ' ' (a tag never holds a space, so the list is exact). Video posts have
  `style="border: 3px solid #0000ff;"` (all 'webm' tagged posts of the sample, plus one without the tag).
  The paginator's last link gives the last offset. No depth limit seen: `tags=all&pid=871038` (page 20740) is served.
  Page 0 of `tags=all` showed 40 thumbnails, not 42: some posts are hidden from the listing; they are never requested.
- Post page `index.php?page=post&s=view&id=<ID>` → 200 text/html: `<a href="https://realbooru.com//images/xx/yy/<hash>.<ext>">Original</a>`,
  `<img id="image" src=...>` (the shown image) or `<video><source src=....mp4><source src=....webm></video>`, "Posted at Nov, 19 2026"
  (day only), `<span id="psc<ID>">` score, `<input id="source" value=...>`, `<textarea id="tags">` (exact tag names), tag
  links with type classes (copyright, metadata, model, artist, character, tag-type-general).
- Tag list `index.php?page=tags&s=list&sort=asc&order_by=tag&pid=<offset>` → 200 text/html, 50 rows per page (a `limit`
  parameter is ignored), rows `<tr><td><count></td><td><span class="tag-type-<type>"><a ...><name></a></span></td><td><type> (edit)</td></tr>`.
  Last page offset 60150 (about 60,200 tags). Without `sort` / `order_by` the order is 'updated' (moves while reading).

## Downloader

- `DownloaderRb(Downloader)`, imports only from `lynxx.sdk` (+ bs4 / stdlib). Interval `_get_min_request_interval() = 1.0` s.
- Count: listing page 0 + the last page of the paginator (2 requests): `last_offset + posts on the last page`.
- Item infos come from the listing (id, tags, md5 from the thumbnail name, thumbnail as preview): no request per listed post;
  the tag / safety / gender filters run on them. Only posts that are really downloaded open their post page (1 request each,
  never with `-skip`), which also fills date, score, source, exact tags, extension and md5 (when the file name is an md5) for
  the info dumps.
- Capabilities: `SOURCES`, `PREVIEW`. Not declared: date filter (see below), comments, parent / child, video format choice.
  Favorites and pools are search kinds (below), not the core's favorites / pool pipeline: `syntax.fav` / `pool` stay empty.
- Media: images (jpg, png, gif...) and videos (webm originals; the player also lists an mp4, not used).

### R6: file links

- The file link is the post page's own "Original" link (or the first `<video><source>` when that link is missing); `-low_res`
  takes the page's shown image (`img#image`). Nothing is built from the md5 / thumbnail path: older posts have sha1 named
  originals (`/images/fd/c2/a8629c...27.jpg`) while their thumbnail is md5 named, so a built link would even be wrong.
- A post page without a file link (deleted, hidden) is skipped with a warning, never rebuilt. Posts hidden from the listing
  are never seen.

## Pools and favorites (search kinds, 1.1.0, 2026-11-21)

- Manifest `search` (Lynxx 2.0.11+): two listing kinds, `{"id": "pool", "token": "pool:", "value": "[0-9]+", "input": "id",
  "listing": true}` and the same for `favorites` with `favorited_by:`. The query stays one string: `pool:678 solo -webm`.
  As listings, the core moves every plain tag of the query to the local filters (required tags, a negative group), keeps the
  site's order (no id cursor, date limits refused: "'pool:678' lists posts in the site's order"), refuses an 'or' group, two
  listings, a negated one. The downloader refuses what the core leaves in the site query (metatags such as `rating:` / `sort:` /
  `id:`, positive wildcards): those pages take no search (`-463`, before any request). App: typed as `pool:<id>` and
  `favorited_by:<id>` (chips "Pool", "Yêu thích của người dùng"; 2.0.11 had them in its "Tìm theo" picker), input `id`.
- `pool:<id>` → `index.php?page=pool&s=show&id=<id>`: `<div id="pool-show"><h4>Pool: <name></h4>` then every post of the pool
  on that one page, in the pool's order (pool 678: 401 thumbnails, 399 posts, no paginator):
  `<span class="thumb" id="p<ID>"><a href="index.php?page=post&s=view&id=<ID>&amp;pool_id=678"><img class="preview"
  src="…/thumbnail_<md5>.jpg" title="<tags separated by spaces> rating:<r> score:<n> user:<uploader>">`. The trailing
  rating / score / uploader are left out of the tags; a post listed twice is kept once. Page size `POOL_PAGE_SIZE` 5000: one
  page for the core and for Preview (no next page).
- `favorited_by:<user id>` → `index.php?page=favorites&s=view&id=<user id>` (served to guests, no cookie set):
  `<span class="thumb"><a href="index.php?page=post&amp;s=view&amp;id=<ID>" id="p<ID>" …><img src="…/thumbnail_<md5>.jpg?<ID>"
  title="<tags separated by spaces>">`, a `posts[<ID>] = {'tags': …, 'rating': …, 'score': …, 'user': …}` script, then
  `<div id='paginator'>`. Pages are `pid` offsets: the paginator's smallest offset is the page size (50 until it is read, the
  Gelbooru 0.2 default), its largest the last page; count = last offset + posts on the last page. A paginator whose step is
  smaller than the posts on the first page (page numbers, not offsets) stops the run (`-465`) rather than read wrong pages.
- Where users find a user id: the address of their own "Favorites" link on the site when logged in
  (`index.php?page=favorites&s=view&id=<id>`), or a post page's "Posted … by" link (`index.php?page=account&s=profile&id=<id>`).
  Profile pages by name redirect guests to `index.php?page=account&s=home` (302): names cannot be turned into ids.
- Video flag: these pages have no video border; the tags tell (`webm`, `mp4`, `video`, `sound`). The post page decides when a
  post is downloaded: `-skip_vid` / `-skip_img` are checked again there (a video tagged only `animated` is still skipped).
- A page without `pool-show` (no such pool, or a private pool: what the site shows a guest for a private pool was not seen)
  stops the run with "the site shows no pool <id>: there is no pool with this id, or it is private" (`-464`); a pool block
  without thumbnails, or a favorites page without them, gives 0 posts with a warning.
- Evidence, 2026-11-21 (robots.txt from the same day's survey, then 3 requests, at least 28 s apart, `Lynxx/2.0-r11`, Referer
  the site, no cookies): pool 678 (public, 401 thumbnails: 200, 365,996 bytes), the profile of a pool creator by name (302 to
  `page=account&s=home`), favorites of user 1 (200, one favorite, paginator `<b>1</b>` only). The pages are in
  `lynxx/tests/fixtures/realbooru/` with their tags and user names replaced by placeholders (`SOURCES.json`); a favorites page
  with more than one page was not seen: its paginator's links (href or onclick, both read) are the Gelbooru 0.2 ones.

## Tag list (tags.py)

- `read_tags(job)`: tries the DAPI tag index (1 request); while it is off, reads the HTML tag list sorted by name
  (`sort=asc&order_by=tag`, unique key, stable offsets), until a page has no 'next' link (or no rows, or repeats the previous
  page). Types: general / artist / copyright / character / metadata as given, `model` (the person shown) → `character`,
  anything else → general. Names with spaces / control characters (broken rows such as `\t1girl`, `\r\nbreasts`, the empty
  name) are skipped; HTML entities are decoded once; repeated names are given once.
- Harness (`--budget 10`): 380 tags, 0 bad, 380 unique, 10 requests, 10.4 s, ended by the budget (as expected).
- Full list: about 1,204 pages + the DAPI probe (+ robots.txt) ≈ 1,206 requests, about 21 minutes at 1 request/s, within the
  core's 5,000 request budget. Shipped list (2026-11-19, `python -m lynxx.tags_updater --modules rb --min-posts 2`):
  36,617 tags, 1,206 requests, 1,207 s. Manifest `tagUpdate` `tags.py:read_tags`, `minSdkVersion` 1.1.
- **Tag names with HTML codes are kept on purpose** (16 old tags such as `april_o&#039;neil`, `baldur&#039;s_gate_3`, next to
  `baldur's_gate_3`): the list gives them as the site does, and the site finds the same posts with either form (checked
  2026-11-19: `baldur&#039;s_gate_3` / `baldur's_gate_3` the same 15 posts, `covering_another&#039;s_breasts` /
  `covering_another's_breasts` the same post). Do not unescape them in `tags.py`.

## Test runs

- `record --query "latina" --limit 3` (try 1): every check passed. 7 requests (count: page 0 + last page `pid=13440`;
  run: the same 2 + 3 post pages); 13,452 posts; links `https://realbooru.com//images/...` (jpg, gif, gif).
- `check`: ok, replay of 7 requests, same posts.
- 1.1.0 (pools, favorites): `python -m unittest lynxx.tests.test_search_kinds.RealbooruRouteTests` (offline, the recorded
  pages): manifest / SDK 1.2 / robots.txt / catalog gate, the query rules, other site tags and robots.txt refused before any
  request, a pool (Preview, count, a `-dmode skip` run: one request, the pool's order, local filters), a pool the site does
  not show, favorites (recorded page; pages of 50 by href links and of 40 by onclick links written in its markup, the site's
  order, each page once; a paginator in page numbers refused), the post page deciding the media type.

## Open points

- File downloads: the site has hotlink protection (a file URL without a Referer redirects to `hotlink.php`, 22 bytes of
  text). The core sends `Referer: https://realbooru.com/` on every request. Real run 2026-11-19 (`lynxx -module rb -dlimit 2
  -- latina 'id:>1016700'`): 2 / 2 JPEG files downloaded. The oldest posts may have lost their files on the site
  (post 91: HTTP 404), which the core reports as failed items.
- Date filter: the date is only on the post page; supporting `-mindate / -maxdate` would need one request for each listed post
  (42 per page) before filtering, and the generator's test budget (25 requests) could not pass. Left out.
- The listing may hide posts (40 of 42 on one page): counts can be a little higher than what is downloaded.
- Pools / favorites (1.1.0): a private pool and a favorites page with several pages were not seen live (above). The listing's
  'model' type stays `character` in the list (a core 'model' type is the owner's decision). Lynxx 2.0.7 to 2.0.10 refuse this
  manifest ("unknown field(s): search") and keep 1.0.0: publish 1.1.0 in the official catalog only after 2.0.11 is released.

## Versions

- 1.0.0 (2026-11-19): listing pages + post pages, tag list update (`tags.py`).
- 1.1.0 (2026-11-21): pools and favorites as search kinds (manifest `search`, `minSdkVersion` 1.2, saved `robots.txt`); the search
  address takes no page size (Preview uses the module's 42); `-skip_vid` / `-skip_img` checked again on the post page.

## Review checklist
- [x] robots.txt allows the paths used; the terms forbid automated access: R3 overridden by the owner (above)
- [x] no login, cookie or challenge bypass; only public data
- [x] request interval is polite (1.0 s for HTML pages)
- [x] file urls come from the site's own fields (no urls built for posts the site hides)
- [x] test run (`python -m lynxx.extgen check <folder>`) passes
