"""Rich Text Format (.rtf) -> :class:`Document`.

A real RTF reader rather than a regex stripper:

- tokenizer for groups, control words with numeric parameters, control symbols,
  ``\\'hh`` escapes and ``\\binN`` raw data;
- ``\\'hh`` bytes and raw 8-bit text decoded with the code page of the current
  font (``\\fcharset`` / ``\\cpg`` in the font table) or the document
  (``\\ansicpg``, ``\\mac``, ``\\pc``; default cp1252); multi-byte code pages
  (Shift-JIS, GBK, Big5, UHC) are decoded across consecutive escapes;
- ``\\uN`` with ``\\ucN`` fallback skipping (surrogate pairs joined);
- bold / italic / underline / strike / superscript / subscript scoped by groups,
  hidden text dropped;
- ``\\par \\line \\tab \\page``, simple tables (``\\cell \\row``), HYPERLINK fields
  (instruction parsed, result kept), footnotes collected into a final "Ghi chú"
  chapter, PNG / JPEG pictures (hex or ``\\bin``) embedded as resources;
- skipped destinations: font / color / style tables, info, headers, footers,
  field instructions, list tables, shapes' fallback pictures and every unknown
  ``{\\* ...}`` destination;
- headings from ``\\outlinelevelN`` or "heading N" styles of the style sheet, used
  to cut chapters (otherwise "Chapter N"-like short paragraphs, otherwise one
  chapter).
"""

from __future__ import annotations

import binascii
import codecs
import re
from pathlib import Path

from .base import Chapter, Document, FormatError, escape, read_limited, sanitize_fragment, sniff_image_type, strip_tags
from .htmldoc import clean_xml_text, heading_split_level, split_at_headings

MAX_GROUP_DEPTH = 1000
MAX_PICT_BYTES = 50 * 1024 * 1024
MAX_PICT_TOTAL = 512 * 1024 * 1024
MAX_UC = 16

_TOKEN = re.compile(
    rb"\\([a-zA-Z]{1,32})(-?\d{1,10})? ?"  # 1, 2: control word and parameter
    rb"|\\'([0-9a-fA-F]{2})"  # 3: hex escape
    rb"|\\([^a-zA-Z])"  # 4: control symbol
    rb"|([{}])"  # 5: group
    rb"|[\r\n]+"  # ignored line breaks
    rb"|([^\\{}\r\n]+)"  # 6: text
    rb"|(\\)",  # 7: dangling backslash
    re.DOTALL,
)
_HEX_ONLY = re.compile(rb"[^0-9A-Fa-f]+")

# \fcharset -> code page
_CHARSET_CODEPAGES = {
    0: 1252, 2: "symbol", 77: 10000, 78: 10001, 79: 10003, 80: 10008, 81: 10002, 84: 10005, 85: 10004,
    128: 932, 129: 949, 130: 1361, 134: 936, 136: 950, 161: 1253, 162: 1254, 163: 1258, 177: 1255, 178: 1256,
    186: 1257, 204: 1251, 222: 874, 238: 1250, 254: 437, 255: 850,
}
_CODEPAGE_CODECS = {10000: "mac_roman", 10001: "cp932", 10002: "cp950", 10003: "cp949", 10004: "mac_arabic",
                    10005: "hebrew", 10006: "mac_greek", 10007: "mac_cyrillic", 10008: "gb2312", 10029: "mac_latin2",
                    10079: "mac_iceland", 10081: "mac_turkish", 65001: "utf-8", 20127: "ascii", 28591: "latin-1",
                    936: "gbk", 1361: "johab"}
# Symbol-font bytes (also reached as U+F0xx from \uN) for the glyphs that matter in books.
_SYMBOL_GLYPHS = {0xB7: "\u2022", 0xA7: "\u25aa", 0xD8: "\u27a2", 0xFC: "\u2713", 0x76: "\u2756", 0x6E: "\u25a0",
                  0x71: "\u2751", 0x6F: "\u25a1", 0xE0: "\u2192", 0xAE: "\u2192", 0xDE: "\u21d2", 0x2D: "\u2212"}
_GREEK = dict(zip(b"abgdezhqiklmnxoprstufcywjvJVABGDEZHQIKLMNXOPRSTUFCYW",
                  "αβγδεζηθικλμνξοπρστυφχψωϕϖϑςΑΒΓΔΕΖΗΘΙΚΛΜΝΞΟΠΡΣΤΥΦΧΨΩ"))
_LANGUAGES = {
    1033: "en-US", 2057: "en-GB", 1066: "vi", 2052: "zh-CN", 1028: "zh-TW", 3076: "zh-HK", 1041: "ja", 1042: "ko",
    1049: "ru", 1058: "uk", 1031: "de", 1036: "fr", 3082: "es", 1034: "es", 1040: "it", 1046: "pt-BR", 2070: "pt-PT",
    1045: "pl", 1029: "cs", 1038: "hu", 1043: "nl", 1053: "sv", 1044: "nb", 1030: "da", 1035: "fi", 1032: "el",
    1055: "tr", 1037: "he", 1025: "ar", 1054: "th", 1057: "id", 1086: "ms",
}
_CHAR_WORDS = {
    b"emdash": "\u2014", b"endash": "\u2013", b"lquote": "\u2018", b"rquote": "\u2019", b"ldblquote": "\u201c",
    b"rdblquote": "\u201d", b"bullet": "\u2022", b"emspace": "\u2003", b"enspace": "\u2002", b"qmspace": "\u2005",
    b"zwj": "\u200d", b"zwnj": "\u200c", b"ltrmark": "\u200e", b"rtlmark": "\u200f", b"zwbo": "\u200b",
    b"tab": "\u2003",
}
_CHAR_SYMBOLS = {b"\\": "\\", b"{": "{", b"}": "}", b"~": "\u00a0", b"-": "\u00ad", b"_": "\u2011"}
_SKIP_DESTINATIONS = frozenset(
    w.encode() for w in (
        "colortbl header headerl headerr headerf footer footerl footerr footerf xe tc tcn txe rxe ftnsep ftnsepc "
        "ftncn aftnsep aftnsepc aftncn private pntxta pntxtb pn revtbl listtable listoverridetable rsidtbl filetbl "
        "template buptim creatim printim revtim operator comment company category manager keywords hlinkbase "
        "docvar userprops nonshppict shprslt objdata objclass objname datafield generator themedata "
        "colorschememapping datastore latentstyles xmlnstbl mmathPr pgdsctbl listpicture picprop blipuid bkmkstart "
        "bkmkend atnid atnauthor annotation atrfstart atrfend atnref atndate atntime atnicn atnparent fchars lchars "
        "pnseclvl do background wgrffmtfilter passwordhash protusertbl oldcprops oldpprops oldsprops oldtprops "
        "nesttableprops falt panose fname gridtbl mhtmltag htmltag factoidname formfield ffdeftext ffformat "
        "ffhelptext ffstattext ffentrymcr ffexitmcr ffname ffl pgdsc"
    ).split()
)
_PICT_FORMATS = {b"pngblip": "png", b"jpegblip": "jpeg", b"emfblip": "skip", b"wmetafile": "skip",
                 b"macpict": "skip", b"pmmetafile": "skip", b"dibitmap": "skip", b"wbitmap": "skip"}
_HEADING_STYLE = re.compile(
    r"^(?:heading|überschrift|titre|titolo|título|titulo|encabezado|заголовок|kop|nagłówek|rubrik|otsikko|"
    r"overskrift|nadpis|címsor|başlık|tiêu đề|標題|标题|見出し|제목)\s*([1-9])$",
    re.IGNORECASE,
)
_NUMBER = (r"(?:\d{1,4}|[ivxlcdm]{1,8}|[零一二三四五六七八九十百千]{1,6}|one|two|three|four|five|six|seven|eight|"
           r"nine|ten|eleven|twelve|thirteen|fourteen|fifteen|sixteen|seventeen|eighteen|nineteen|twenty|first|"
           r"second|third|một|hai|ba|bốn|năm|sáu|bảy|tám|chín|mười)")
_CHAPTER_LINE = re.compile(
    rf"^(?:(?:chương|chuong|chapter|chap\.|hồi|quyển|quyen|tập|phần|volume|vol\.|part|book|episode|глава|часть)"
    rf"\s*{_NUMBER}(?![^\W\d_]).*"
    r"|(?:prologue|epilogue|lời mở đầu|lời nói đầu|ngoại truyện|vĩ thanh|phần kết|пролог|эпилог)(?:[\s.:：\-–—].*)?"
    r"|第\s*[\d零一二三四五六七八九十百千万〇两]+\s*[章节回卷集部篇话].*"
    r"|제\s*\d+\s*[장화권].*)$",
    re.IGNORECASE,
)


def _codec_for(codepage: int | str) -> str:
    if codepage == "symbol":
        return "symbol"
    if isinstance(codepage, int):
        name = _CODEPAGE_CODECS.get(codepage, f"cp{codepage}")
    else:
        name = str(codepage)
    try:
        return codecs.lookup(name).name
    except LookupError:
        return "cp1252"


class _Story:
    """A flow of blocks: the main text or one footnote."""

    __slots__ = ("blocks", "runs", "rows", "row", "cell", "number")

    def __init__(self, number: int = 0) -> None:
        self.blocks: list[tuple] = []
        self.runs: list[tuple] = []
        self.rows: list[list[list[tuple]]] = []
        self.row: list[list[tuple]] = []
        self.cell: list[tuple] = []
        self.number = number


class _Dest:
    __slots__ = ("kind", "buf", "data", "raw", "blip", "num", "outline", "para", "story", "field", "parent", "sn",
                 "toolarge", "footnote")

    def __init__(self, kind: str) -> None:
        self.kind = kind
        self.buf: list[str] = []
        self.data = bytearray()
        self.raw = bytearray()
        self.blip = ""
        self.num = 0
        self.outline: int | None = None
        self.para = True
        self.story: _Story | None = None
        self.field: list[str] | None = None
        self.parent: _Dest | None = None
        self.sn = ""
        self.toolarge = False
        self.footnote = False


class _State:
    __slots__ = ("dest", "owner", "star", "uc", "bold", "italic", "underline", "strike", "sup", "sub", "hidden",
                 "font", "cp", "intbl", "style", "outline", "align", "story", "href", "field")

    def copy(self) -> "_State":
        new = _State.__new__(_State)
        new.dest, new.owner, new.star, new.uc = self.dest, False, False, self.uc
        new.bold, new.italic, new.underline, new.strike = self.bold, self.italic, self.underline, self.strike
        new.sup, new.sub, new.hidden, new.font, new.cp = self.sup, self.sub, self.hidden, self.font, self.cp
        new.intbl, new.style, new.outline, new.align = self.intbl, self.style, self.outline, self.align
        new.story, new.href, new.field = self.story, self.href, self.field
        return new


_BODY = "body"
_COLLECT = frozenset({"fonttbl", "style", "title", "author", "subject", "doccomm", "fldinst", "sn"})


class _RtfParser:
    def __init__(self, document: Document) -> None:
        self.document = document
        self.doc_cp = "cp1252"
        self.deff = 0
        self.lang = 0
        self.font_charset: dict[int, int] = {}
        self.font_cpg: dict[int, int] = {}
        self.font_names: dict[int, str] = {}
        self.font_codec: dict[int, str] = {}
        self.style_levels: dict[int, int] = {}
        self.info: dict[str, str] = {}
        self.main = _Story()
        self.footnotes: list[_Story] = []
        self.pict_total = 0
        root = _State()
        root.dest = _Dest(_BODY)
        root.owner = True
        root.star = False
        root.uc = 1
        root.bold = root.italic = root.underline = root.strike = root.sup = root.sub = root.hidden = False
        root.font = None
        root.cp = self.doc_cp
        root.intbl = False
        root.style = 0
        root.outline = None
        root.align = ""
        root.story = self.main
        root.href = ""
        root.field = None
        self.state = root
        self.stack: list[_State] = []
        self.pending = bytearray()
        self.pending_cp = ""
        self.pending_font: int | None = None
        self.skip = 0
        self.high_surrogate = 0
        self.listtext_depth = 0

    # ------------------------------------------------------------ fonts / text

    def _font_codec(self, number: int | None) -> str:
        if number is None:
            return self.doc_cp
        codec = self.font_codec.get(number)
        if codec is None:
            if number in self.font_cpg:
                codec = _codec_for(self.font_cpg[number])
            elif number in self.font_charset and self.font_charset[number] in _CHARSET_CODEPAGES:
                codec = _codec_for(_CHARSET_CODEPAGES[self.font_charset[number]])
            else:
                codec = self.doc_cp
            self.font_codec[number] = codec
        return codec

    def _symbol_text(self, data: bytes, font: int | None) -> str:
        greek = "symbol" in self.font_names.get(font if font is not None else -1, "symbol").lower()
        out = []
        for byte in data:
            if byte in _SYMBOL_GLYPHS:
                out.append(_SYMBOL_GLYPHS[byte])
            elif greek and byte in _GREEK:
                out.append(_GREEK[byte])
            else:
                out.append(bytes([byte]).decode("cp1252", "replace"))
        return "".join(out)

    def add_bytes(self, data: bytes) -> None:
        state = self.state
        if self.pending and (state.cp != self.pending_cp or state.font != self.pending_font):
            self.flush()
        self.pending_cp = state.cp
        self.pending_font = state.font
        self.pending += data

    def flush(self) -> None:
        if not self.pending:
            return
        data = bytes(self.pending)
        self.pending.clear()
        if self.pending_cp == "symbol":
            text = self._symbol_text(data, self.pending_font)
        else:
            text = data.decode(self.pending_cp, errors="replace")
        self.emit(text)

    def emit(self, text: str) -> None:
        state = self.state
        kind = state.dest.kind
        if kind == _BODY:
            if not state.hidden and text:
                fmt = (state.bold, state.italic, state.underline, state.strike, state.sup, state.sub, state.href)
                state.story.runs.append((text, fmt, ""))
        elif kind in _COLLECT:
            dest = state.dest
            if kind == "fonttbl":
                while ";" in text:
                    head, _, text = text.partition(";")
                    name = ("".join(dest.buf) + head).strip()
                    dest.buf.clear()
                    if name:
                        self.font_names[dest.num] = name
                dest.buf.append(text)
            elif kind == "fldinst":
                if dest.field is not None and sum(len(part) for part in dest.field) < 4096:
                    dest.field.append(text)
            elif sum(len(part) for part in dest.buf) < 65536:
                dest.buf.append(text)

    def emit_unicode(self, code: int) -> None:
        if code < 0:
            code += 65536
        if 0xD800 <= code <= 0xDBFF:
            self.high_surrogate = code
            return
        if 0xDC00 <= code <= 0xDFFF:
            if self.high_surrogate:
                code = 0x10000 + ((self.high_surrogate - 0xD800) << 10) + (code - 0xDC00)
                self.high_surrogate = 0
            else:
                code = 0xFFFD
        elif self.high_surrogate:
            self.high_surrogate = 0
            self.emit("\ufffd")
        if self.listtext_depth and 0xE000 <= code <= 0xF8FF:  # list bullet drawn with a symbol font
            self.emit("\u2022")
            return
        if 0xF020 <= code <= 0xF0FF:  # Symbol / Wingdings glyph mapped to the private use area
            byte = code - 0xF000
            self.emit(self._symbol_text(bytes([byte]), self.state.font) if byte in _SYMBOL_GLYPHS
                      or self._font_codec(self.state.font) == "symbol" else "")
            return
        self.emit(chr(code))

    # ------------------------------------------------------------ paragraphs / tables

    def _level(self, state: _State) -> int:
        if state.outline is not None:
            return state.outline + 1
        return self.style_levels.get(state.style, 0)

    def end_paragraph(self, in_cell: bool = False) -> None:
        state = self.state
        story = state.story
        runs, story.runs = story.runs, []
        block = ("p", self._level(state), state.align, runs)
        if state.intbl or in_cell:
            story.cell.append(block)
        else:
            self.flush_table(story)
            story.blocks.append(block)

    @staticmethod
    def flush_table(story: _Story) -> None:
        if story.cell:
            story.row.append(story.cell)
            story.cell = []
        if story.row:
            story.rows.append(story.row)
            story.row = []
        if story.rows:
            story.blocks.append(("table", story.rows))
            story.rows = []

    def finish_story(self, story: _Story) -> None:
        if story.runs:
            self.end_paragraph(in_cell=bool(story.cell or story.row))
        self.flush_table(story)

    # ------------------------------------------------------------ groups

    def open_group(self) -> None:
        self.flush()
        self.skip = 0
        if len(self.stack) >= MAX_GROUP_DEPTH:
            raise FormatError("RTF nesting is too deep")
        state = self.state
        new = state.copy()
        if state.dest.kind == "stylesheet":
            new.dest = _Dest("style")
            new.owner = True
        self.stack.append(state)
        self.state = new

    def close_group(self) -> None:
        self.flush()
        self.skip = 0
        if not self.stack:
            return
        ended = self.state
        self.state = self.stack.pop()
        if self.listtext_depth and len(self.stack) < self.listtext_depth:
            self.listtext_depth = 0
        if ended.owner and ended.dest is not self.state.dest:
            self.finish_destination(ended.dest, ended)

    def set_dest(self, kind: str) -> _Dest:
        dest = _Dest(kind)
        self.state.dest = dest
        self.state.owner = True
        return dest

    def finish_destination(self, dest: _Dest, ended: _State) -> None:
        kind = dest.kind
        if kind == "style":
            name = "".join(dest.buf).split(";", 1)[0].strip()
            if dest.para:
                level = dest.outline + 1 if dest.outline is not None and dest.outline < 9 else 0
                match = _HEADING_STYLE.match(name)
                if match:
                    level = int(match.group(1))
                if level:
                    self.style_levels[dest.num] = level
        elif kind == "fonttbl":
            if self.state.font is None:
                self.state.cp = self._font_codec(self.deff)
        elif kind in ("title", "author", "subject", "doccomm"):
            value = re.sub(r"\s+", " ", "".join(dest.buf)).strip()
            if value:
                self.info[kind] = value
        elif kind == "sn":
            if dest.parent is not None:
                dest.parent.sn = "".join(dest.buf).strip()
        elif kind == "pict":
            self.finish_pict(dest)
        elif dest.footnote and dest.story is not None:
            saved = self.state
            self.state = ended
            self.finish_story(dest.story)
            self.state = saved
            self.footnotes.append(dest.story)

    def finish_pict(self, dest: _Dest) -> None:
        if dest.toolarge or dest.blip in ("", "skip"):
            return
        state = self.state
        parent = state.dest
        if parent.kind != _BODY and not (parent.kind == "pictonly" and parent.sn in ("pib", "")):
            return
        if dest.raw:
            data = bytes(dest.raw)
        else:
            hexdata = bytes(dest.data)
            if len(hexdata) % 2:
                hexdata = hexdata[:-1]
            try:
                data = binascii.unhexlify(hexdata)
            except (binascii.Error, ValueError):
                return
        media_type = sniff_image_type(data)
        if media_type not in ("image/png", "image/jpeg", "image/gif", "image/webp"):
            return
        if len(data) > MAX_PICT_BYTES or self.pict_total + len(data) > MAX_PICT_TOTAL:
            return
        self.pict_total += len(data)
        name = self.document.add_resource(data, media_type, "image")
        fmt = (False, False, False, False, False, False, "")
        state.story.runs.append(("", fmt, name))

    # ------------------------------------------------------------ control words

    def control_word(self, word: bytes, param: int | None) -> None:
        state = self.state
        dest = state.dest
        kind = dest.kind
        star = state.star
        state.star = False
        if kind == "skip":
            return
        on = param is None or param != 0
        # -- destinations
        if word in _SKIP_DESTINATIONS:
            self.set_dest("skip")
            return
        if word == b"fonttbl":
            self.set_dest("fonttbl")
        elif word == b"stylesheet":
            self.set_dest("stylesheet")
        elif word == b"info":
            self.set_dest("info")
        elif word in (b"title", b"author", b"subject", b"doccomm"):
            if kind == "info" or kind == "upr":
                self.set_dest(word.decode())
            else:
                self.set_dest("skip")
        elif word == b"pict":
            if kind == _BODY or kind == "pictonly":
                self.set_dest("pict")
            else:
                self.set_dest("skip")
        elif word == b"field":
            state.field = []
        elif word == b"fldinst":
            new = self.set_dest("fldinst")
            new.field = state.field if state.field is not None else []
        elif word == b"fldrslt":
            if kind != _BODY:
                self.set_dest("skip")
                return
            self.set_dest(_BODY).story = state.story
            inst = "".join(state.field or [])
            link = re.search(r'HYPERLINK\s+"([^"]{1,2000})"', inst) or re.search(r"HYPERLINK\s+(\S{1,2000})", inst)
            if link and not re.search(r"HYPERLINK\s+\\l", inst):
                url = link.group(1).strip()
                state.href = url if re.match(r"^https?://", url, re.IGNORECASE) else ""
        elif word == b"footnote":
            if kind != _BODY or state.story is not self.main:
                self.set_dest("skip")
                return
            story = _Story(len(self.footnotes) + 1)
            new = self.set_dest(_BODY)
            new.story = story
            new.footnote = True
            state.story = story  # a footnote is its own flow of paragraphs
            state.intbl = False
        elif word == b"chftn":
            if kind == _BODY:
                if state.story is self.main:
                    number = len(self.footnotes) + 1
                    fmt = (False, False, False, False, True, False, f"#rtf-fn-{number}")
                    self.flush()
                    state.story.runs.append((str(number), fmt, ""))
                else:
                    self.emit(str(state.story.number))
        elif word in (b"shpinst", b"object"):
            if kind == _BODY:
                self.set_dest("pictonly")
            else:
                self.set_dest("skip")
        elif word in (b"shptxt", b"result"):
            if kind in ("pictonly", _BODY):
                self.set_dest(_BODY).story = state.story
            else:
                self.set_dest("skip")
        elif word == b"sn":
            if kind == "pictonly":
                new = self.set_dest("sn")
                new.parent = dest
            else:
                self.set_dest("skip")
        elif word == b"upr":
            new = self.set_dest("upr")
            new.parent = dest
        elif word == b"ud":
            if kind == "upr" and dest.parent is not None:
                state.dest = dest.parent
                state.owner = False
            else:
                self.set_dest("skip")
        elif word in (b"listtext", b"pntext"):
            self.listtext_depth = len(self.stack)
        elif star and word not in (b"shppict", b"shp", b"sp", b"sv"):
            self.set_dest("skip")
        # -- inside destinations with their own vocabulary (other words such as u, uc, f act as usual)
        elif kind == "fonttbl" and word in (b"f", b"fcharset", b"cpg"):
            if param is not None:
                if word == b"f":
                    dest.num = param
                elif word == b"fcharset":
                    self.font_charset[dest.num] = param
                else:
                    self.font_cpg[dest.num] = param
                self.font_codec.pop(dest.num, None)
        elif kind == "style" and word in (b"s", b"cs", b"ds", b"ts", b"tsrowd", b"outlinelevel"):
            if word == b"s":
                dest.num = param or 0
            elif word == b"outlinelevel":
                dest.outline = param
            else:
                dest.para = False
        elif kind == "pict" and word in _PICT_FORMATS:
            dest.blip = _PICT_FORMATS[word] if _PICT_FORMATS[word] != "skip" else (dest.blip or "skip")
        else:
            self.formatting(word, param, on, state, kind)

    def formatting(self, word: bytes, param: int | None, on: bool, state: _State, kind: str) -> None:
        if word == b"par":
            if kind == _BODY:
                self.end_paragraph()
        elif word in _CHAR_WORDS:
            self.emit(_CHAR_WORDS[word])
        elif word == b"line":
            self.emit("\n")
        elif word == b"u":
            if param is not None:
                self.emit_unicode(param)
                self.skip = state.uc
        elif word == b"uc":
            state.uc = max(0, min(param or 0, MAX_UC))
        elif word == b"b":
            state.bold = on
        elif word == b"i":
            state.italic = on
        elif word == b"ul" or (word.startswith(b"ul") and word not in (b"ulnone", b"ulc") and len(word) <= 10):
            state.underline = on
        elif word == b"ulnone":
            state.underline = False
        elif word in (b"strike", b"striked"):
            state.strike = on
        elif word == b"super":
            state.sup, state.sub = True, False
        elif word == b"sub":
            state.sub, state.sup = True, False
        elif word == b"nosupersub":
            state.sup = state.sub = False
        elif word == b"up":
            state.sup, state.sub = (param is None or param > 0), False
        elif word == b"dn":
            state.sub, state.sup = (param is None or param > 0), False
        elif word == b"v":
            state.hidden = on
        elif word == b"plain":
            state.bold = state.italic = state.underline = state.strike = state.sup = state.sub = state.hidden = False
            state.font = self.deff
            state.cp = self._font_codec(self.deff)
        elif word == b"f":
            if param is not None:
                state.font = param
                state.cp = self._font_codec(param)
        elif word == b"pard":
            state.intbl = False
            state.style = 0
            state.outline = None
            state.align = ""
        elif word == b"s":
            state.style = param or 0
        elif word == b"outlinelevel":
            state.outline = param if param is not None and 0 <= param <= 8 else None
        elif word == b"intbl":
            state.intbl = True
        elif word == b"itap":
            state.intbl = bool(param)
        elif word == b"qc":
            state.align = "center"
        elif word == b"qr":
            state.align = "right"
        elif word in (b"ql", b"qj", b"qd"):
            state.align = ""
        elif word in (b"cell", b"nestcell"):
            if kind == _BODY:
                if word == b"nestcell":
                    self.emit("\n")
                    return
                self.end_paragraph(in_cell=True)
                story = state.story
                story.row.append(story.cell)
                story.cell = []
        elif word in (b"row", b"nestrow"):
            if kind == _BODY and word == b"row":
                story = state.story
                if story.cell:
                    story.row.append(story.cell)
                    story.cell = []
                if story.row:
                    story.rows.append(story.row)
                    story.row = []
        elif word in (b"page", b"sect", b"column"):
            if kind == _BODY and state.story.runs:
                self.end_paragraph()
        elif word == b"ansicpg":
            if param is not None and param > 0:
                self.doc_cp = _codec_for(param)
                self.font_codec.clear()
                if state.font is None:
                    state.cp = self.doc_cp
        elif word in (b"mac", b"pc", b"pca", b"ansi"):
            self.doc_cp = {b"mac": "mac_roman", b"pc": "cp437", b"pca": "cp850", b"ansi": "cp1252"}[word]
            if state.font is None:
                state.cp = self.doc_cp
        elif word == b"deff":
            self.deff = param or 0
        elif word == b"deflang":
            self.lang = param or 0

    # ------------------------------------------------------------ driver

    def parse(self, data: bytes) -> None:
        match = _TOKEN.match
        size = len(data)
        pos = 0
        while pos < size:
            token = match(data, pos)
            if token is None:  # cannot happen with the catch-all alternatives; stay safe
                pos += 1
                continue
            pos = token.end()
            group = token.lastindex
            if group is None:  # line breaks
                continue
            if group == 6:
                state = self.state
                kind = state.dest.kind
                text = token.group(6)
                if kind == "pict":
                    dest = state.dest
                    if len(dest.data) + len(text) > 2 * MAX_PICT_BYTES:
                        dest.toolarge = True
                    elif not dest.toolarge:
                        dest.data += _HEX_ONLY.sub(b"", text)
                    continue
                if kind == "skip" or kind == "info" or kind == "stylesheet" or kind == "pictonly" or kind == "upr":
                    continue
                if state.star:
                    state.star = False
                if self.skip:
                    if len(text) <= self.skip:
                        self.skip -= len(text)
                        continue
                    text = text[self.skip:]
                    self.skip = 0
                self.add_bytes(text)
            elif group == 3:
                if self.skip:
                    self.skip -= 1
                    continue
                if self.state.dest.kind in _COLLECT or self.state.dest.kind == _BODY:
                    self.add_bytes(bytes([int(token.group(3), 16)]))
            elif group in (1, 2):
                word = token.group(1)
                param_text = token.group(2)
                param = int(param_text) if param_text is not None else None
                if word == b"bin":
                    count = max(0, min(param or 0, size - pos))
                    if self.skip:
                        self.skip = 0
                    dest = self.state.dest
                    if dest.kind == "pict" and not dest.toolarge:
                        if count > MAX_PICT_BYTES:
                            dest.toolarge = True
                        else:
                            dest.raw += data[pos:pos + count]
                    pos += count
                    continue
                if self.skip:
                    self.skip -= 1
                    continue
                self.flush()
                self.control_word(word, param)
            elif group == 5:
                if token.group(5) == b"{":
                    self.open_group()
                else:
                    self.close_group()
                    if not self.stack:
                        break  # end of the document group: ignore trailing bytes
            elif group == 4:
                symbol = token.group(4)
                if self.skip:
                    self.skip -= 1
                    continue
                self.flush()
                state = self.state
                if symbol == b"*":
                    state.star = True
                elif symbol in (b"\n", b"\r"):
                    if state.dest.kind == _BODY:
                        self.end_paragraph()
                elif symbol in _CHAR_SYMBOLS:
                    if symbol in (b"\\", b"{", b"}") and state.dest.kind == "pict":
                        continue
                    self.emit(_CHAR_SYMBOLS[symbol])
                elif symbol == b"|" or symbol == b":":
                    continue
        self.flush()
        while self.stack:
            self.close_group()
        self.finish_story(self.main)


# ---------------------------------------------------------------- rendering

_FMT_TAGS = (("strong", 0), ("em", 1), ("u", 2), ("s", 3))


def _render_runs(runs: list[tuple], heading: bool = False) -> str:
    merged: list[list] = []
    for text, fmt, image in runs:
        if heading and fmt[0]:
            fmt = (False,) + fmt[1:]  # headings are bold already
        if not image and merged and not merged[-1][2] and merged[-1][1] == fmt:
            merged[-1][0] += text
        else:
            merged.append([text, fmt, image])
    if merged and not merged[0][2]:
        merged[0][0] = merged[0][0].lstrip()
    if merged and not merged[-1][2]:
        merged[-1][0] = merged[-1][0].rstrip()
    out: list[str] = []
    for text, fmt, image in merged:
        if image:
            out.append(f'<img src="{image}" alt=""/>')
            continue
        markup = escape(text).replace("\n", "<br/>")
        if not markup:
            continue
        bold, italic, underline, strike, sup, sub, href = fmt
        if sup:
            markup = f"<sup>{markup}</sup>"
        elif sub:
            markup = f"<sub>{markup}</sub>"
        for tag, index in reversed(_FMT_TAGS):
            if fmt[index]:
                markup = f"<{tag}>{markup}</{tag}>"
        if href:
            markup = f'<a href="{escape(href)}">{markup}</a>'
        out.append(markup)
    return "".join(out)


def _paragraph_has_content(runs: list[tuple]) -> bool:
    return any(image or text.strip() for text, _fmt, image in runs)


def _plain(runs: list[tuple]) -> str:
    return re.sub(r"\s+", " ", "".join(text for text, _fmt, image in runs if not image)).strip()


def _render_blocks(blocks: list[tuple], shift: int = 0) -> str:
    out: list[str] = []
    for block in blocks:
        if block[0] == "table":
            rows = []
            for row in block[1]:
                cells = []
                for cell in row:
                    content = "".join(f"<p>{_render_runs(runs)}</p>" for _k, _l, _a, runs in cell
                                      if _paragraph_has_content(runs))
                    cells.append(f"<td>{content}</td>")
                if cells:
                    rows.append(f"<tr>{''.join(cells)}</tr>")
            if rows:
                out.append(f"<table>{''.join(rows)}</table>")
            continue
        _kind, level, align, runs = block
        if not _paragraph_has_content(runs):
            continue
        style = f' style="text-align: {align}"' if align else ""
        if level:
            tag = f"h{max(1, min(level - shift, 6))}"
        else:
            tag = "p"
        out.append(f"<{tag}{style}>{_render_runs(runs, heading=bool(level))}</{tag}>")
    return "\n".join(out)


# ---------------------------------------------------------------- converter

def convert(path: Path) -> Document:
    path = Path(path)
    data = read_limited(path)
    start = data[:64].lstrip(b"\xef\xbb\xbf \r\n\t")
    if not start.startswith(b"{\\rtf"):
        raise FormatError("not an RTF file")
    document = Document(source_format="rtf")
    parser = _RtfParser(document)
    parser.parse(data)
    blocks = parser.main.blocks
    levels = [block[1] for block in blocks if block[0] == "p" and block[1] and _paragraph_has_content(block[3])]
    shift = 0
    if levels:
        shift = min(levels) - 1
    else:
        candidates = [index for index, block in enumerate(blocks)
                      if block[0] == "p" and len(_plain(block[3])) <= 80 and _CHAPTER_LINE.match(_plain(block[3]))]
        if len(candidates) >= 2:
            for index in candidates:
                _kind, _level, align, runs = blocks[index]
                blocks[index] = ("p", 2, align, runs)
    names = {resource.name for resource in document.resources}
    keep = (lambda src: src if src in names else "")
    markup = clean_xml_text(_render_blocks(blocks, shift))
    fragment = sanitize_fragment(markup, resolve_src=keep)
    info = parser.info
    document.title = info.get("title", "")[:300]
    if info.get("author"):
        document.authors = [author.strip()[:200] for author in re.split(r"[;\n]", info["author"]) if author.strip()][:20]
    document.description = (info.get("subject") or info.get("doccomm") or "")[:5000]
    document.language = _LANGUAGES.get(parser.lang, "")
    chapters = split_at_headings(fragment, document.title or path.stem, heading_split_level(fragment))
    if parser.footnotes:
        notes = []
        for story in parser.footnotes:
            body = _render_blocks(story.blocks)
            notes.append(f'<div class="footnote" id="rtf-fn-{story.number}">{body}</div>')
        notes_html = sanitize_fragment(clean_xml_text("".join(notes)), resolve_src=keep)
        if strip_tags(notes_html) or "<img" in notes_html:
            chapters.append(Chapter("Ghi chú", notes_html))
    if not chapters:
        raise FormatError("RTF document has no readable content")
    document.chapters = chapters
    return document
