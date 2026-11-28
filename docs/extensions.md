# Extension định dạng vBook trong Lynxx

Tài liệu tham chiếu cho người viết extension (tiếng Anh, giống reference của skill `new-site-extension`). Công cụ thử: `node app/plugin/cli.js <thư mục> check | detail | toc | chap | search | pack`.


Lynxx's runtime (`app/plugin/runtime.js`, `html.js`, `cryptojs.js`) follows the behaviour of vBook's Rhino + Jsoup
runtime. Anything not listed here is unsupported in Lynxx.

## Package

```
plugin.json      manifest (≤ 2 MB)
icon.png         optional (≤ 5 MB)
src/*.js         scripts, top level of src/ only
```

`plugin.json`:

```json
{
  "metadata": {
    "name": "Site Name", "author": "you", "version": 1,
    "source": "https://site.example",
    "regexp": "(https?://)?(www\\.)?site\\.example/truyen/[^/]+/?$",
    "description": "Đọc truyện trên Site Name", "locale": "vi_VN",
    "type": "comic", "nsfw": false
  },
  "script": { "home": "home.js", "genre": "genre.js", "detail": "detail.js", "search": "search.js",
              "page": "page.js", "toc": "toc.js", "chap": "chap.js" },
  "config": { "DOMAIN": { "title": "Domain", "default": "https://site.example", "mode": "input", "format": "text" } }
}
```

- `type`: `comic`, `novel` (`chinese_novel` = novel). Lynxx does not run `tts`, `translate`, `audio`, `video`, `ai`.
- `regexp` must match the **whole** book URL (tried with and without `http(s)://` and the trailing `/`).
- `version`: integer; bump it for every published change.
- `encrypt: true` packages cannot run in Lynxx (the vBook key derivation is private): never produce them.
- `config` keys are injected into every script as **string constants** (`DOMAIN`). The user edits them in Lynxx
  ("Cấu hình"). Never declare a variable with the same name; read it inside `try { if (DOMAIN) … } catch (e) {}`.

## Script contracts (all arguments are strings)

| Script | Signature | Returns |
|---|---|---|
| home | `execute()` | `[{title, input, script}]` tabs; each tab runs `script` as `execute(input, page)` |
| genre | `execute()` | same shape as home |
| gen.js (list) | `execute(input, page)` | `Response.success(items, next)` |
| search | `execute(query, page)` | `Response.success(items, next)` |
| detail | `execute(url)` | `{name, cover, author, description, detail, host, ongoing, genres, nsfw}` |
| page | `execute(url)` | array of absolute URLs of chapter-list pages (optional script) |
| toc | `execute(url)` | `[{name, url, host, lock, pay}]` oldest first |
| chap | `execute(url)` | novel: HTML string · comic: `[url]` or `[{link, fallback: [url], referrerpolicy}]` |

- Items: `{name, link, cover, description, host, tag}`; items without `name` are dropped; duplicates (same link) too.
- `page` is `""` on the first call, then the `next` you returned. `next = ""` ends paging. A page number, a full URL
  or an API cursor are all fine.
- `description` / `detail` may contain simple HTML (`<br>`, `<b>`): Lynxx flattens it to text.
- `genres` in detail: `[{title, input, script}]` (navigation entries).
- Novel `chap` may return a second value as the real chapter title: `Response.success(html, "Chương 5: …")`.
- `Response.error("message")` for expected failures; `return null` also counts as a failure.

## Globals

- `fetch(url, {method, headers, body, queries, timeout})` → response. Never throws: a network failure is
  `status 504`, `ok false`. `body` may be a string or a flat object (sent as a form). Cookies of the site go along
  automatically (Lynxx keeps one cookie jar for all extensions).
- response: `ok`, `status`, `url` (after redirects), `headers` (lower-case keys), `header(name)`, `text(charset?)`,
  `html(charset?)` (a Jsoup-like Document with `baseUri` = response URL), `json()`, `base64()`,
  `request.headers.cookie` (the cookie that was sent).
- `Http.get(url)` / `Http.post(url)` builder: `.params({})` / `.queries({})`, `.headers({})`, `.body(str)`,
  `.timeout(ms)`, then `.html(charset?)`, `.string(charset?)`, `.json()`. `Http.post(url).params({...})` without a body
  posts the params as a form.
- `Html.parse(html)`, `Html.clean(html, ["p", "br", …])`.
- `Response.success(data, next?)`, `Response.error(msg)`.
- `load("config.js")`: text substitution of another file of `src/` (literal name only). `load("crypto.js")` always
  gives the bundled CryptoJS (MD5, SHA1/256/512, Hmac*, AES/DES/TripleDES with CBC/ECB/CTR/CFB/OFB, PBKDF2, enc.Hex /
  Base64 / Utf8 / Latin1).
- `sleep(ms)` (≤ 30 s), `console.log` / `Console.log` / `Log.log`, `UserAgent.chrome() / android() / ios() / system()`.
- `localStorage` / `cacheStorage` (`getItem`, `setItem`, `removeItem`, `clear`), `localCookie.getCookie()` /
  `setCookie("a=b")`, `localConfig.getItem(key, default)`.
- `Engine.newBrowser()` → `launch(url, ms)`, `html(ms)`, `callJs(js, ms)`, `waitUrl(regex | [regex], ms)`, `urls()`,
  `block([regex])`, `loadHtml(base, html)`, `close()`. App only (hidden Chromium window); the CLI cannot run it.
- Not available: `Graphics`, `Java.type`, `java.*`, `Packages`, `Script`, `WebSocket`, `localDatabase`, `localBook`.

## Jsoup-like elements

- Elements: `select`, `first`, `last`, `get(i)`, `size`, `length`, `isEmpty`, `forEach`, `map`, `text` (joined),
  `html`, `outerHtml`, `attr` (first element that has it), `eachAttr`, `eachText`, `remove`, `not`, `is`, `eq`,
  `parents`, `next`, `prev`. `String(emptyElements) === ""`.
- Element: `select`, `selectFirst`, `text` (normalized whitespace), `ownText`, `wholeText`, `html`, `outerHtml`,
  `attr(name)` (`""` when missing; `attr("abs:href")` resolves against the page URL), `absUrl`, `hasAttr`, `id`,
  `className`, `hasClass`, `tagName`, `data` (script contents), `parent`, `children`, `child(i)`,
  `nextElementSibling`, `previousElementSibling`, `siblingElements`, `remove`, `empty`, `append`, `before`, `after`.
- `first()` / `last()` / `get(i)` on an empty set return an **empty element** (its methods return `""`), never null.
- The parser builds `html/head/body` and implied `tbody` like Jsoup, so selectors copied from browser devtools work.
- Selectors: tag, `#id`, `.class`, `[a]`, `[a=v]`, `[a^=v]`, `[a$=v]`, `[a*=v]`, `[a~=regex]`, `[a!=v]`, combinators
  `␣ > + ~`, `:first-child`, `:last-child`, `:nth-child(an+b)`, `:nth-of-type`, `:not()`, `:has()`, `:contains()`,
  `:containsOwn()`, `:matches(regex)`, `:matchesOwn()`, `:containsData()`, `:eq(n)`, `:lt(n)`, `:gt(n)`, `:empty`,
  `:root`. Attribute values compare case-insensitively (Jsoup). Unknown pseudo selectors throw.

## Lynxx specifics

- Lynxx paces requests per host (≥ 250 ms apart) and honours `Retry-After` on 429 / 503 by itself; do not add long
  `sleep()` calls.
- Requests to localhost / private addresses are refused.
- Extensions run in a Chromium sandbox without Node; a run is stopped after 120 s.
- Comic pages are downloaded by Lynxx itself (Referer = chapter URL unless `referrerpolicy` says otherwise), checked to
  be images and stored in the library; `fallback` URLs are tried when the main link fails.
