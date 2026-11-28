"""Markdown (.md, .markdown) -> :class:`Document`.

A self-contained renderer for the CommonMark subset books and notes use: ATX and
Setext headings, paragraphs, hard breaks, ``*``/``_`` emphasis and strong
emphasis (CommonMark delimiter rules), ``~~strike~~``, code spans, fenced and
indented code blocks, block quotes, nested ordered / bullet lists (tight and
loose), thematic breaks, inline links, reference links, images, autolinks,
backslash escapes, entities, simple GFM tables and raw HTML. Every text run is
escaped while rendering and the complete output goes through the shared
sanitizer, so raw HTML embedded in the Markdown is allowlisted like any page.

Chapters are cut at level-1 headings (level-2 when there is at most one level-1
heading). A YAML front matter block is stripped; its ``title`` / ``author`` /
``lang`` / ``description`` keys fill the metadata.
"""

from __future__ import annotations

import html
import re
import unicodedata
from pathlib import Path

from .base import DROP_WITH_CONTENT, Document, FormatError, decode_text, escape, read_limited, sanitize_fragment, strip_tags
from .htmldoc import LocalImageResolver, clean_xml_text, split_at_headings

MAX_NESTING = 24
"""Deeper block quotes / lists are rendered as plain paragraphs."""
MAX_DELIMITERS = 1000
"""Emphasis delimiters processed per paragraph; beyond that they stay literal."""
_RAW_WINDOW = 8192
"""Longest raw inline HTML tag / autolink looked for."""
MAX_OPEN_BRACKETS = 1000
"""Unclosed ``[`` / ``![`` tracked per paragraph; more are plain text."""

# ---------------------------------------------------------------- block patterns

_ATX = re.compile(r"(#{1,6})(?=[ \t]|$)(.*)$")
_ATX_CLOSE = re.compile(r"(?:^|[ \t]+)#+[ \t]*$")
_SETEXT = re.compile(r"(?:=+|-+)[ \t]*$")
_HR = re.compile(r"(?:(?:\*[ \t]*){3,}|(?:-[ \t]*){3,}|(?:_[ \t]*){3,})$")
_FENCE = re.compile(r"(`{3,}|~{3,})(.*)$")
_LIST = re.compile(r"(?:([-+*])|(\d{1,9})([.)]))(?=[ \t]|$)")
_TABLE_DELIM = re.compile(r"\|?[ \t]*:?-+:?[ \t]*(?:\|[ \t]*:?-+:?[ \t]*)*\|?[ \t]*$")
_HTML_BLOCK_RAW = re.compile(r"<(script|pre|style|textarea)(?:[\s>]|$)", re.IGNORECASE)
_HTML_BLOCK_TAG = re.compile(
    r"</?(?:address|article|aside|base|basefont|blockquote|body|caption|center|col|colgroup|dd|details|dialog|dir|"
    r"div|dl|dt|fieldset|figcaption|figure|footer|form|frame|frameset|h[1-6]|head|header|hr|html|iframe|legend|li|"
    r"link|main|menu|menuitem|nav|noframes|ol|optgroup|option|p|param|search|section|summary|table|tbody|td|tfoot|"
    r"th|thead|title|tr|track|ul)(?:[\s>]|/>|$)",
    re.IGNORECASE,
)
_OPEN_TAG = (r"<[A-Za-z][A-Za-z0-9-]*(?:\s+[A-Za-z_:][A-Za-z0-9_.:-]*"
             r"(?:\s*=\s*(?:[^\s\"'=<>`]+|'[^']*'|\"[^\"]*\"))?)*\s*/?>")
_CLOSE_TAG = r"</[A-Za-z][A-Za-z0-9-]*\s*>"
_HTML_BLOCK_ANY = re.compile(rf"(?:{_OPEN_TAG}|{_CLOSE_TAG})[ \t]*$")
_HTML_BLOCK_SPECIAL = (("<!--", "-->"), ("<?", "?>"), ("<![CDATA[", "]]>"), ("<!", ">"))
_REF_DEF = re.compile(
    r"\A[ ]{0,3}\[((?:[^\[\]\\]|\\.){1,999})\]:[ \t]*\n?[ \t]*(<[^<>\n]*>|\S+)"
    r"(?:(?:[ \t]+|[ \t]*\n[ \t]*)(\"(?:[^\"\\]|\\.)*\"|'(?:[^'\\]|\\.)*'|\((?:[^()\\]|\\.)*\)))?[ \t]*(?:\n|\Z)",
    re.DOTALL,
)

# ---------------------------------------------------------------- inline patterns

_ESCAPABLE = frozenset("!\"#$%&'()*+,-./:;<=>?@[\\]^_`{|}~")
_SPECIAL = re.compile(r"[\\`*_~\[\]!<&\n]")
_ENTITY = re.compile(r"&(?:#[xX][0-9a-fA-F]{1,6}|#[0-9]{1,7}|[A-Za-z][A-Za-z0-9]{1,31});")
_AUTOLINK = re.compile(r"<([A-Za-z][A-Za-z0-9+.-]{1,31}:[^\s<>\x00-\x1f]*)>")
_EMAIL = re.compile(
    r"<([A-Za-z0-9.!#$%&'*+/=?^_`{|}~-]+@[A-Za-z0-9](?:[A-Za-z0-9-]{0,61}[A-Za-z0-9])?"
    r"(?:\.[A-Za-z0-9](?:[A-Za-z0-9-]{0,61}[A-Za-z0-9])?)*)>"
)
_RAW_INLINE = re.compile(
    rf"{_OPEN_TAG}|{_CLOSE_TAG}|<!--.*?-->|<\?.*?\?>|<!\[CDATA\[.*?\]\]>|<![A-Za-z][^>]*>",
    re.DOTALL,
)
_RAW_TAG_NAME = re.compile(r"</?([A-Za-z][A-Za-z0-9-]*)")
_LABEL = re.compile(r"\[((?:[^\[\]\\]|\\.){0,999})\]")
_BACKTICKS: dict[int, re.Pattern[str]] = {}


def _leading(line: str) -> int:
    return len(line) - len(line.lstrip(" "))


def _expand_leading_tabs(line: str) -> str:
    if "\t" not in line:
        return line
    stripped = line.lstrip(" \t")
    return line[: len(line) - len(stripped)].expandtabs(4) + stripped


def _normalize_label(label: str) -> str:
    return re.sub(r"\s+", " ", label.strip()).casefold()


def _unescape_text(text: str) -> str:
    """Backslash escapes and entities in link destinations / titles / code info strings."""
    text = re.sub(r"\\([!\"#$%&'()*+,\-./:;<=>?@\[\\\]^_`{|}~])", r"\1", text)
    return _ENTITY.sub(lambda m: html.unescape(m.group(0)), text)


def _is_punct(char: str) -> bool:
    if not char:
        return False
    if char in _ESCAPABLE:
        return True
    return unicodedata.category(char)[0] in "PS"


def _slug(text: str) -> str:
    slug = re.sub(r"[^\w\- ]", "", text.lower(), flags=re.UNICODE).strip().replace(" ", "-")
    return slug[:80] or "section"


# ---------------------------------------------------------------- inline rendering

class _Delim:
    __slots__ = ("char", "count", "orig", "can_open", "can_close")

    def __init__(self, char: str, count: int, can_open: bool, can_close: bool) -> None:
        self.char, self.count, self.orig = char, count, count
        self.can_open, self.can_close = can_open, can_close

    def literal(self) -> str:
        return self.char * self.count


def _join(nodes: list) -> str:
    return "".join(node if isinstance(node, str) else node.literal() for node in nodes)


def _process_emphasis(nodes: list) -> None:
    """CommonMark "process emphasis" over ``nodes`` (strings and :class:`_Delim`), in place."""
    if sum(1 for node in nodes if isinstance(node, _Delim)) > MAX_DELIMITERS:
        return
    floors: dict[tuple[str, bool, int], int] = {}
    closer = 0
    while True:
        while closer < len(nodes) and not (isinstance(nodes[closer], _Delim) and nodes[closer].can_close):
            closer += 1
        if closer >= len(nodes):
            return
        close = nodes[closer]
        key = (close.char, close.can_open, close.orig % 3 if close.char != "~" else 0)
        floor = floors.get(key, -1)
        opener_at = -1
        index = closer - 1
        while index > floor:
            node = nodes[index]
            if isinstance(node, _Delim) and node.char == close.char and node.can_open:
                if close.char == "~":
                    if node.count == close.count:
                        opener_at = index
                        break
                elif not ((node.can_close or close.can_open) and (node.orig + close.orig) % 3 == 0
                          and not (node.orig % 3 == 0 and close.orig % 3 == 0)):
                    opener_at = index
                    break
            index -= 1
        if opener_at < 0:
            floors[key] = closer - 1
            if not close.can_open:
                nodes[closer] = close.literal()
            closer += 1
            continue
        opener = nodes[opener_at]
        if close.char == "~":
            used, tag = close.count, "del"
        else:
            used = 2 if opener.count >= 2 and close.count >= 2 else 1
            tag = "strong" if used == 2 else "em"
        opener.count -= used
        close.count -= used
        nodes[opener_at + 1:closer] = [f"<{tag}>{_join(nodes[opener_at + 1:closer])}</{tag}>"]
        for name, value in list(floors.items()):
            if value > opener_at:
                floors[name] = opener_at
        closer = opener_at + 2
        if opener.count == 0:
            del nodes[opener_at]
            closer -= 1
        if close.count == 0:
            del nodes[closer]


class _InlineRenderer:
    def __init__(self, refs: dict[str, tuple[str, str]]) -> None:
        self.refs = refs

    def render(self, text: str) -> str:
        nodes: list = []
        brackets: list[list] = []  # [node index, is image, active, text position after "["]
        self.inactive_below = 0  # link openers below this stack index are inactive (no links in links)
        buf: list[str] = []
        no_closer: set[int] = set()
        size = len(text)
        pos = 0

        def flush() -> None:
            if buf:
                nodes.append(escape("".join(buf)))
                buf.clear()

        while pos < size:
            found = _SPECIAL.search(text, pos)
            if found is None:
                buf.append(text[pos:])
                break
            start = found.start()
            if start > pos:
                buf.append(text[pos:start])
            char = text[start]
            pos = start + 1
            if char == "\\":
                following = text[pos] if pos < size else ""
                if following == "\n":
                    flush()
                    nodes.append("<br/>")
                    pos += 1
                    while pos < size and text[pos] == " ":
                        pos += 1
                elif following and following in _ESCAPABLE:
                    buf.append(following)
                    pos += 1
                else:
                    buf.append("\\")
            elif char == "`":
                run_end = pos
                while run_end < size and text[run_end] == "`":
                    run_end += 1
                length = run_end - start
                closing = None
                if length not in no_closer:
                    pattern = _BACKTICKS.get(length)
                    if pattern is None:
                        pattern = _BACKTICKS.setdefault(length, re.compile(r"(?<!`)`{%d}(?!`)" % length))
                    closing = pattern.search(text, run_end)
                    if closing is None:
                        no_closer.add(length)
                if closing is None:
                    buf.append("`" * length)
                    pos = run_end
                else:
                    code = text[run_end:closing.start()].replace("\n", " ")
                    if code.startswith(" ") and code.endswith(" ") and code.strip(" "):
                        code = code[1:-1]
                    flush()
                    nodes.append(f"<code>{escape(code)}</code>")
                    pos = closing.end()
            elif char in "*_~":
                run_end = pos
                while run_end < size and text[run_end] == char:
                    run_end += 1
                count = run_end - start
                pos = run_end
                if char == "~" and count > 2:
                    buf.append(char * count)
                    continue
                before = text[start - 1] if start > 0 else "\n"
                after = text[run_end] if run_end < size else "\n"
                left = not after.isspace() and (not _is_punct(after) or before.isspace() or _is_punct(before))
                right = not before.isspace() and (not _is_punct(before) or after.isspace() or _is_punct(after))
                if char == "_":
                    can_open = left and (not right or _is_punct(before))
                    can_close = right and (not left or _is_punct(after))
                else:
                    can_open, can_close = left, right
                if not can_open and not can_close:
                    buf.append(char * count)
                    continue
                flush()
                nodes.append(_Delim(char, count, can_open, can_close))
            elif char == "!":
                if pos < size and text[pos] == "[" and len(brackets) < MAX_OPEN_BRACKETS:
                    flush()
                    brackets.append([len(nodes), True, True, pos + 1])
                    nodes.append("![")
                    pos += 1
                else:
                    buf.append("!")
            elif char == "[" and len(brackets) < MAX_OPEN_BRACKETS:
                flush()
                brackets.append([len(nodes), False, True, pos])
                nodes.append("[")
            elif char == "]":
                pos = self._close_bracket(text, pos, nodes, brackets, buf, flush)
                self.inactive_below = min(self.inactive_below, len(brackets))
            elif char == "[":
                buf.append("[")
            elif char == "<":
                window = min(size, start + _RAW_WINDOW)
                link = _AUTOLINK.match(text, start, window)
                if link:
                    url = link.group(1)
                    flush()
                    nodes.append(f'<a href="{escape(url)}">{escape(url)}</a>')
                    pos = link.end()
                    continue
                mail = _EMAIL.match(text, start, window)
                if mail:
                    address = mail.group(1)
                    flush()
                    nodes.append(f'<a href="mailto:{escape(address)}">{escape(address)}</a>')
                    pos = mail.end()
                    continue
                raw = _RAW_INLINE.match(text, start, window)
                if raw:
                    tag = _RAW_TAG_NAME.match(raw.group(0))
                    if tag and tag.group(1).lower() in DROP_WITH_CONTENT:
                        buf.append(raw.group(0))  # shown as text: an unclosed <script> must not swallow the book
                    else:
                        flush()
                        nodes.append(raw.group(0))
                    pos = raw.end()
                    continue
                buf.append("<")
            elif char == "&":
                entity = _ENTITY.match(text, start)
                if entity:
                    buf.append(html.unescape(entity.group(0)))
                    pos = entity.end()
                else:
                    buf.append("&")
            else:  # newline: hard break after two spaces, else soft break
                pending = "".join(buf)
                buf.clear()
                stripped = pending.rstrip(" ")
                buf.append(stripped)
                flush()
                nodes.append("<br/>\n" if len(pending) - len(stripped) >= 2 else "\n")
                while pos < size and text[pos] == " ":
                    pos += 1
        flush()
        _process_emphasis(nodes)
        return _join(nodes)

    def _close_bracket(self, text: str, pos: int, nodes: list, brackets: list[list], buf: list[str], flush) -> int:
        if not brackets:
            buf.append("]")
            return pos
        opener = brackets[-1]
        if not opener[1] and len(brackets) - 1 < self.inactive_below:
            opener[2] = False
        if not opener[2]:
            brackets.pop()
            buf.append("]")
            return pos
        label_raw = text[opener[3]:pos - 1] if pos - 1 - opener[3] <= 999 else ""
        dest: str | None = None
        title = ""
        end = pos
        parsed = self._inline_link(text, pos) if pos < len(text) and text[pos] == "(" else None
        if parsed:
            dest, title, end = parsed
        else:
            label = _LABEL.match(text, pos, min(len(text), pos + 1100))
            key, ref_end = label_raw, pos
            if label:
                key = label.group(1) if label.group(1).strip() else label_raw
                ref_end = label.end()
            ref = self.refs.get(_normalize_label(key)) if key.strip() and len(key) <= 999 else None
            if ref is not None:
                dest, title = ref
                end = ref_end
        if dest is None:
            brackets.pop()
            buf.append("]")
            return pos
        flush()
        start = opener[0]
        inner = nodes[start + 1:]
        _process_emphasis(inner)
        inner_html = _join(inner)
        title_attr = f' title="{escape(title)}"' if title else ""
        if opener[1]:
            node = f'<img src="{escape(dest)}" alt="{escape(strip_tags(inner_html))}"{title_attr}/>' if dest else ""
        else:
            node = f'<a href="{escape(dest)}"{title_attr}>{inner_html}</a>'
        del nodes[start:]
        nodes.append(node)
        brackets.pop()
        if not opener[1]:
            self.inactive_below = len(brackets)
        return end

    @staticmethod
    def _inline_link(text: str, pos: int) -> tuple[str, str, int] | None:
        size = len(text)
        index = pos + 1
        while index < size and text[index] in " \t\n":
            index += 1
        out: list[str] = []
        if index < size and text[index] == "<":
            index += 1
            while index < size and text[index] not in "<>\n":
                if text[index] == "\\" and index + 1 < size and text[index + 1] in _ESCAPABLE:
                    out.append(text[index + 1])
                    index += 2
                    continue
                out.append(text[index])
                index += 1
            if index >= size or text[index] != ">":
                return None
            index += 1
        else:
            depth = 0
            while index < size:
                char = text[index]
                if char == "\\" and index + 1 < size and text[index + 1] in _ESCAPABLE:
                    out.append(text[index + 1])
                    index += 2
                    continue
                if char.isspace() or ord(char) < 0x20:
                    break
                if char == "(":
                    depth += 1
                    if depth > 32:
                        return None
                elif char == ")":
                    if depth == 0:
                        break
                    depth -= 1
                out.append(char)
                index += 1
            if depth:
                return None
        dest = "".join(out)
        after_dest = index
        while index < size and text[index] in " \t\n":
            index += 1
        title = ""
        if index > after_dest and index < size and text[index] in "\"'(":
            closer = ")" if text[index] == "(" else text[index]
            index += 1
            chars: list[str] = []
            while index < size and text[index] != closer:
                if text[index] == "\\" and index + 1 < size and text[index + 1] in _ESCAPABLE:
                    chars.append(text[index + 1])
                    index += 2
                    continue
                if closer == ")" and text[index] == "(":
                    return None
                chars.append(text[index])
                index += 1
            if index >= size:
                return None
            title = "".join(chars)
            index += 1
            while index < size and text[index] in " \t\n":
                index += 1
        if index < size and text[index] == ")":
            return _ENTITY.sub(lambda m: html.unescape(m.group(0)), dest), html.unescape(title), index + 1
        return None


# ---------------------------------------------------------------- block parsing

class _Markdown:
    def __init__(self) -> None:
        self.refs: dict[str, tuple[str, str]] = {}
        self.slugs: set[str] = set()
        self.first_heading = ""

    # -- helpers
    @staticmethod
    def _html_block_kind(stripped: str) -> str:
        if _HTML_BLOCK_RAW.match(stripped):
            return "raw"
        for opener, _closer in _HTML_BLOCK_SPECIAL:
            if stripped.startswith(opener):
                if opener == "<!" and not re.match(r"<![A-Za-z]", stripped):
                    continue
                return opener
        if _HTML_BLOCK_TAG.match(stripped):
            return "tag"
        if _HTML_BLOCK_ANY.match(stripped):
            return "any"
        return ""

    def _starts_block(self, line: str) -> bool:
        """Would ``line`` interrupt a paragraph (so it is not a lazy continuation line)?"""
        if _leading(line) >= 4:
            return False
        stripped = line.lstrip(" ")
        if not stripped:
            return True
        if stripped[0] in "#" and _ATX.match(stripped):
            return True
        if stripped[0] == ">" or _FENCE.match(stripped) or _HR.match(stripped):
            return True
        marker = _LIST.match(stripped)
        if marker and stripped[marker.end():].strip() and (marker.group(1) or marker.group(2) == "1"):
            return True
        kind = self._html_block_kind(stripped) if stripped[0] == "<" else ""
        return bool(kind) and kind != "any"

    def _extract_refs(self, text: str) -> str:
        while text.lstrip(" ").startswith("["):
            match = _REF_DEF.match(text)
            if not match or not match.group(1).strip():
                break
            label = _normalize_label(match.group(1))
            dest = match.group(2)
            if dest.startswith("<") and dest.endswith(">"):
                dest = dest[1:-1]
            title = match.group(3) or ""
            if title:
                title = title[1:-1]
            if label and label not in self.refs:
                self.refs[label] = (_unescape_text(dest), _unescape_text(title))
            text = text[match.end():]
        return text

    # -- main block parser
    def parse(self, lines: list[str], depth: int = 0) -> list[tuple]:
        blocks: list[tuple] = []
        para: list[str] = []

        def flush_para() -> None:
            if para:
                text = self._extract_refs("\n".join(para)).strip()
                if text:
                    blocks.append(("para", text))
                para.clear()

        index, count = 0, len(lines)
        while index < count:
            line = _expand_leading_tabs(lines[index])
            if not line.strip():
                flush_para()
                index += 1
                continue
            indent = _leading(line)
            if indent >= 4:
                if para:  # an indented line cannot interrupt a paragraph
                    para.append(line.lstrip())
                    index += 1
                    continue
                code: list[str] = []
                while index < count:
                    current = _expand_leading_tabs(lines[index])
                    if current.strip() and _leading(current) < 4:
                        break
                    code.append(current[4:] if current.strip() else "")
                    index += 1
                while code and not code[-1].strip():
                    code.pop()
                blocks.append(("code", "", "\n".join(code)))
                continue
            stripped = line[indent:]
            first = stripped[0]
            if first in "`~":
                fence = _FENCE.match(stripped)
                if fence and not (fence.group(1)[0] == "`" and "`" in fence.group(2)):
                    flush_para()
                    marker = fence.group(1)
                    info = _unescape_text(fence.group(2).strip())
                    closing = re.compile(r"[ ]{0,3}%s{%d,}[ \t]*$" % (re.escape(marker[0]), len(marker)))
                    index += 1
                    code = []
                    while index < count:
                        current = lines[index]
                        if closing.match(current):
                            index += 1
                            break
                        cut = min(indent, _leading(current))
                        code.append(current[cut:])
                        index += 1
                    blocks.append(("code", info.split()[0] if info else "", "\n".join(code)))
                    continue
            if first == "#":
                atx = _ATX.match(stripped)
                if atx:
                    flush_para()
                    content = _ATX_CLOSE.sub("", atx.group(2).strip())
                    blocks.append(("heading", len(atx.group(1)), content.strip()))
                    index += 1
                    continue
            if para and first in "=-" and _SETEXT.match(stripped):
                text = self._extract_refs("\n".join(para)).strip()
                para.clear()
                if text:
                    blocks.append(("heading", 1 if first == "=" else 2, text))
                    index += 1
                    continue
                para.append(stripped)
                index += 1
                continue
            if first in "*-_" and _HR.match(stripped):
                flush_para()
                blocks.append(("hr",))
                index += 1
                continue
            if first == ">":
                if depth >= MAX_NESTING:
                    para.append(stripped)
                    index += 1
                    continue
                flush_para()
                quoted: list[str] = []
                while index < count:
                    current = _expand_leading_tabs(lines[index])
                    lead = _leading(current)
                    rest = current[lead:]
                    if lead < 4 and rest.startswith(">"):
                        rest = rest[1:]
                        if rest[:1] in (" ", "\t"):
                            rest = rest[1:]
                        quoted.append(rest)
                        index += 1
                    elif current.strip() and quoted and quoted[-1].strip() and not self._starts_block(current) \
                            and not _FENCE.match(quoted[-1].lstrip(" ")):
                        quoted.append(current)
                        index += 1
                    else:
                        break
                blocks.append(("quote", self.parse(quoted, depth + 1)))
                continue
            marker = _LIST.match(stripped)
            if marker and not (para and not (stripped[marker.end():].strip() and (marker.group(1) or marker.group(2) == "1"))):
                if depth >= MAX_NESTING:
                    para.append(stripped)
                    index += 1
                    continue
                flush_para()
                index = self._parse_list(lines, index, depth, blocks)
                continue
            if "|" in stripped and index + 1 < count:
                table = self._try_table(lines, index)
                if table is not None:
                    flush_para()
                    block, index = table
                    blocks.append(block)
                    continue
            if first == "<":
                kind = self._html_block_kind(stripped)
                if kind and not (para and kind == "any"):
                    flush_para()
                    index = self._parse_html_block(lines, index, kind, blocks)
                    continue
            para.append(stripped)
            index += 1
        flush_para()
        return blocks

    def _parse_html_block(self, lines: list[str], index: int, kind: str, blocks: list[tuple]) -> int:
        count = len(lines)
        collected: list[str] = []
        if kind in ("tag", "any"):
            while index < count and lines[index].strip():
                collected.append(lines[index])
                index += 1
        else:
            if kind == "raw":
                tag = _HTML_BLOCK_RAW.match(lines[index].lstrip(" ")).group(1).lower()
                end_marker = f"</{tag}>"
            else:
                end_marker = dict(_HTML_BLOCK_SPECIAL)[kind]
            start = index
            found = False
            while index < count:
                collected.append(lines[index])
                index += 1
                if end_marker in collected[-1].lower():
                    found = True
                    break
            if not found:  # unterminated: stop at the first blank line instead of eating the rest of the file
                index = start
                collected = []
                while index < count and lines[index].strip():
                    collected.append(lines[index])
                    index += 1
        blocks.append(("html", "\n".join(collected)))
        return index

    def _try_table(self, lines: list[str], index: int) -> tuple[tuple, int] | None:
        header_line = lines[index].strip()
        delim_line = lines[index + 1].strip()
        if "|" not in delim_line or not _TABLE_DELIM.match(delim_line) or "-" not in delim_line:
            return None
        if _leading(lines[index + 1]) >= 4:
            return None
        header = _split_row(header_line)
        delims = _split_row(delim_line)
        if len(header) != len(delims) or not header:
            return None
        aligns = []
        for cell in delims:
            cell = cell.strip()
            if cell.startswith(":") and cell.endswith(":"):
                aligns.append("center")
            elif cell.endswith(":"):
                aligns.append("right")
            elif cell.startswith(":"):
                aligns.append("left")
            else:
                aligns.append("")
        rows: list[list[str]] = []
        index += 2
        while index < len(lines):
            line = lines[index]
            if not line.strip() or self._starts_block(line):
                break
            cells = _split_row(line.strip())
            cells = (cells + [""] * len(header))[: len(header)]
            rows.append(cells)
            index += 1
        return ("table", aligns, header, rows), index

    def _parse_list(self, lines: list[str], index: int, depth: int, blocks: list[tuple]) -> int:
        count = len(lines)
        first_line = _expand_leading_tabs(lines[index])
        first = _LIST.match(first_line[_leading(first_line):])
        ordered = first.group(2) is not None
        marker_char = first.group(3) if ordered else first.group(1)
        start = int(first.group(2)) if ordered else 1
        items: list[list[tuple]] = []
        loose = False
        while index < count:
            line = _expand_leading_tabs(lines[index])
            lead = _leading(line)
            if lead >= 4:
                break
            stripped = line[lead:]
            marker = _LIST.match(stripped)
            if not marker or (marker.group(3) if marker.group(2) is not None else marker.group(1)) != marker_char:
                break
            if (marker.group(2) is not None) != ordered or _HR.match(stripped):
                break
            marker_end = lead + marker.end()
            rest = line[marker_end:]
            rest_lead = len(rest) - len(rest.lstrip(" \t"))
            spaces = (" " * marker_end + rest[:rest_lead]).expandtabs(4)[marker_end:]
            rest = spaces + rest[rest_lead:]
            if not rest.strip():
                content_indent = marker_end + 1
                first_content = ""
            else:
                gap = len(rest) - len(rest.lstrip(" "))
                if gap > 4:
                    content_indent = marker_end + 1
                    first_content = rest[1:]
                else:
                    content_indent = marker_end + gap
                    first_content = rest[gap:]
            item_lines = [first_content]
            index += 1
            in_fence = bool(_FENCE.match(first_content.lstrip(" ")))
            while index < count:
                current = _expand_leading_tabs(lines[index])
                if not current.strip():
                    if not first_content.strip() and len(item_lines) == 1:
                        break  # an item can begin with at most one blank line
                    item_lines.append("")
                    index += 1
                    continue
                if _leading(current) >= content_indent:
                    item_lines.append(current[content_indent:])
                    if _FENCE.match(item_lines[-1].lstrip(" ")):
                        in_fence = not in_fence
                    index += 1
                    continue
                if item_lines[-1].strip() and not in_fence and not self._starts_block(current) \
                        and not _LIST.match(current.lstrip(" ")):
                    item_lines.append(current.lstrip(" "))
                    index += 1
                    continue
                break
            trailing = 0
            while item_lines and not item_lines[-1].strip():
                item_lines.pop()
                trailing += 1
            if trailing and index < count:
                following = _expand_leading_tabs(lines[index])
                next_marker = _LIST.match(following[_leading(following):]) if _leading(following) < 4 else None
                if next_marker and (next_marker.group(2) is not None) == ordered \
                        and (next_marker.group(3) if ordered else next_marker.group(1)) == marker_char:
                    loose = True
            if not loose:
                fence_open = False
                for item_line in item_lines[1:]:
                    if _FENCE.match(item_line.lstrip(" ")):
                        fence_open = not fence_open
                    elif not item_line.strip() and not fence_open:
                        loose = True
                        break
            items.append(self.parse(item_lines, depth + 1))
        blocks.append(("list", ordered, start, not loose, items))
        return index

    # -- rendering
    def render(self, blocks: list[tuple], tight: bool = False) -> str:
        inline = _InlineRenderer(self.refs)
        out: list[str] = []
        for block in blocks:
            kind = block[0]
            if kind == "para":
                content = inline.render(block[1])
                out.append(content if tight else f"<p>{content}</p>")
            elif kind == "heading":
                content = inline.render(block[2])
                plain = strip_tags(content)
                if not self.first_heading and plain:
                    self.first_heading = plain[:300]
                slug = base = _slug(plain)
                suffix = 1
                while slug in self.slugs:
                    slug = f"{base}-{suffix}"
                    suffix += 1
                self.slugs.add(slug)
                out.append(f'<h{block[1]} id="{escape(slug)}">{content}</h{block[1]}>')
            elif kind == "code":
                language = re.sub(r"[^A-Za-z0-9_+-]", "", block[1])[:40]
                cls = f' class="language-{language}"' if language else ""
                body = escape(block[2])
                out.append(f"<pre><code{cls}>{body}\n</code></pre>" if body else f"<pre><code{cls}></code></pre>")
            elif kind == "hr":
                out.append("<hr/>")
            elif kind == "quote":
                out.append(f"<blockquote>{self.render(block[1])}</blockquote>")
            elif kind == "list":
                _, ordered, start, is_tight, items = block
                if ordered:
                    open_tag = f'<ol start="{start}">' if start != 1 else "<ol>"
                else:
                    open_tag = "<ul>"
                rendered = "".join(f"<li>{self.render(item, is_tight)}</li>" for item in items)
                out.append(f"{open_tag}{rendered}{'</ol>' if ordered else '</ul>'}")
            elif kind == "table":
                _, aligns, header, rows = block

                def cell(tag: str, text: str, align: str) -> str:
                    style = f' style="text-align: {align}"' if align else ""
                    return f"<{tag}{style}>{inline.render(text)}</{tag}>"

                head = "".join(cell("th", text, align) for text, align in zip(header, aligns))
                body = "".join("<tr>" + "".join(cell("td", text, align) for text, align in zip(row, aligns)) + "</tr>"
                               for row in rows)
                out.append(f"<table><thead><tr>{head}</tr></thead>{f'<tbody>{body}</tbody>' if body else ''}</table>")
            elif kind == "html":
                # each raw block is made self-contained here; images / links are resolved by the final pass
                out.append(sanitize_fragment(block[1], resolve_src=lambda src: src, resolve_link=lambda href: href))
        return "\n".join(out) if not tight else "".join(out)


def _split_row(line: str) -> list[str]:
    text = line.strip()
    if text.startswith("|"):
        text = text[1:]
    if text.endswith("|") and not text.endswith("\\|"):
        text = text[:-1]
    return [cell.strip() for cell in re.split(r"(?<!\\)\|", text)]


# ---------------------------------------------------------------- front matter

_FRONT_MATTER = re.compile(r"\A---[ \t]*\n(.*?)\n(?:---|\.\.\.)[ \t]*(?:\n|\Z)", re.DOTALL)


def _yaml_scalar(value: str) -> str:
    value = value.strip()
    if len(value) >= 2 and value[0] == value[-1] and value[0] in "\"'":
        value = value[1:-1]
    return value.strip()


def _front_matter(text: str) -> tuple[dict[str, list[str]], str]:
    match = _FRONT_MATTER.match(text[:65536])
    if not match:
        return {}, text
    meta: dict[str, list[str]] = {}
    key = ""
    for raw in match.group(1).split("\n"):
        if not raw.strip() or raw.lstrip().startswith("#"):
            continue
        item = re.match(r"^\s+-\s+(.*)$", raw) or re.match(r"^-\s+(.*)$", raw)
        if item and key:
            meta.setdefault(key, []).append(_yaml_scalar(item.group(1)))
            continue
        pair = re.match(r"^([A-Za-z_][\w-]*)\s*:\s*(.*)$", raw)
        if not pair:
            continue
        key = pair.group(1).lower()
        value = pair.group(2).strip()
        if value.startswith("[") and value.endswith("]"):
            meta[key] = [_yaml_scalar(part) for part in value[1:-1].split(",") if part.strip()]
        elif value and value not in ("|", ">", "|-", ">-"):
            meta[key] = [_yaml_scalar(value)]
    return meta, text[match.end():]


# ---------------------------------------------------------------- converter

def render_markdown(text: str) -> tuple[str, str]:
    """Render Markdown source to (unsanitized XHTML, first heading text)."""
    parser = _Markdown()
    blocks = parser.parse(text.split("\n"))
    return parser.render(blocks), parser.first_heading


def convert(path: Path) -> Document:
    path = Path(path)
    data = read_limited(path)
    text = clean_xml_text(decode_text(data)).replace("\r\n", "\n").replace("\r", "\n")
    meta, text = _front_matter(text)
    document = Document(source_format=(path.suffix.lstrip(".").lower() or "md"))
    markup, first_heading = render_markdown(text)
    fragment = sanitize_fragment(markup, resolve_src=LocalImageResolver(document, path.parent))
    title = (meta.get("title") or [""])[0] or first_heading
    document.title = title[:300]
    authors = meta.get("author") or meta.get("authors") or []
    document.authors = [author[:200] for author in authors if author][:20]
    language = (meta.get("lang") or meta.get("language") or [""])[0]
    if re.fullmatch(r"[A-Za-z]{1,8}(?:[-_][A-Za-z0-9]{1,8})*", language):
        document.language = language
    document.description = ((meta.get("description") or meta.get("summary") or [""])[0])[:5000]
    chapters = split_at_headings(fragment, document.title or path.stem)
    if not chapters:
        raise FormatError("Markdown document has no readable content")
    document.chapters = chapters
    return document
