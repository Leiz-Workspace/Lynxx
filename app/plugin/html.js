// Lynxx plugin runtime: a dependency-free, lenient HTML parser with a Jsoup-compatible selector engine.
// Extensions are written against Jsoup (Element.select, :contains, :eq, [attr~=regex]...). This file gives the same
// behaviour in any JavaScript host: the sandboxed plugin renderer (plain <script>) and Node (tests, the dev CLI).
// It never executes scripts, never loads resources and never touches the network.
(function (root, factory) {
  if (typeof module === 'object' && module.exports) module.exports = factory();
  else root.LynxxHtml = factory();
}(typeof self !== 'undefined' ? self : this, () => {
  'use strict';

  const VOID = new Set(['area', 'base', 'br', 'col', 'embed', 'hr', 'img', 'input', 'keygen', 'link', 'meta', 'param', 'source', 'track', 'wbr', 'basefont', 'bgsound', 'frame']);
  const RAW_TEXT = new Set(['script', 'style', 'textarea', 'title', 'xmp', 'iframe', 'noembed', 'noframes', 'plaintext']);
  const CLOSES_P = new Set(['address', 'article', 'aside', 'blockquote', 'center', 'details', 'dialog', 'dir', 'div', 'dl', 'fieldset', 'figcaption', 'figure', 'footer', 'form', 'h1', 'h2', 'h3', 'h4', 'h5', 'h6', 'header', 'hgroup', 'hr', 'main', 'menu', 'nav', 'ol', 'p', 'pre', 'section', 'summary', 'table', 'ul', 'li', 'dd', 'dt']);
  const BLOCK = new Set([...CLOSES_P, 'br', 'tr', 'td', 'th', 'tbody', 'thead', 'tfoot', 'caption', 'option', 'body', 'html', 'head', 'title', 'legend', 'img']);
  const HEAD_TAGS = new Set(['title', 'meta', 'link', 'base', 'style', 'script', 'noscript']);
  const SCOPE_BARRIER = new Set(['table', 'td', 'th', 'caption', 'html', 'body', 'template', 'button', 'object', 'marquee', 'applet']);
  const LIST_BARRIER = new Set(['ul', 'ol', ...SCOPE_BARRIER]);
  const MAX_DEPTH = 512;

  const ENTITIES = {
    amp: '&', lt: '<', gt: '>', quot: '"', apos: "'", nbsp: ' ', copy: '©', reg: '®', trade: '™', hellip: '…', mdash: '—',
    ndash: '–', lsquo: '‘', rsquo: '’', sbquo: '‚', ldquo: '“', rdquo: '”', bdquo: '„', laquo: '«', raquo: '»', lsaquo: '‹',
    rsaquo: '›', middot: '·', bull: '•', euro: '€', pound: '£', yen: '¥', cent: '¢', sect: '§', para: '¶', deg: '°', plusmn: '±',
    times: '×', divide: '÷', frac12: '½', frac14: '¼', frac34: '¾', sup1: '¹', sup2: '²', sup3: '³', micro: 'µ', iexcl: '¡',
    iquest: '¿', ordf: 'ª', ordm: 'º', not: '¬', shy: '­', macr: '¯', acute: '´', cedil: '¸', uml: '¨', curren: '¤',
    brvbar: '¦', ensp: ' ', emsp: ' ', thinsp: ' ', zwnj: '‌', zwj: '‍', lrm: '‎', rlm: '‏',
    dagger: '†', Dagger: '‡', permil: '‰', prime: '′', Prime: '″', larr: '←', rarr: '→', uarr: '↑', darr: '↓', harr: '↔',
    hearts: '♥', spades: '♠', clubs: '♣', diams: '♦', star: '☆', infin: '∞', ne: '≠', le: '≤', ge: '≥', minus: '−', asymp: '≈',
    Agrave: 'À', Aacute: 'Á', Acirc: 'Â', Atilde: 'Ã', Auml: 'Ä', Aring: 'Å', AElig: 'Æ', Ccedil: 'Ç', Egrave: 'È', Eacute: 'É',
    Ecirc: 'Ê', Euml: 'Ë', Igrave: 'Ì', Iacute: 'Í', Icirc: 'Î', Iuml: 'Ï', ETH: 'Ð', Ntilde: 'Ñ', Ograve: 'Ò', Oacute: 'Ó',
    Ocirc: 'Ô', Otilde: 'Õ', Ouml: 'Ö', Oslash: 'Ø', Ugrave: 'Ù', Uacute: 'Ú', Ucirc: 'Û', Uuml: 'Ü', Yacute: 'Ý', THORN: 'Þ',
    szlig: 'ß', agrave: 'à', aacute: 'á', acirc: 'â', atilde: 'ã', auml: 'ä', aring: 'å', aelig: 'æ', ccedil: 'ç', egrave: 'è',
    eacute: 'é', ecirc: 'ê', euml: 'ë', igrave: 'ì', iacute: 'í', icirc: 'î', iuml: 'ï', eth: 'ð', ntilde: 'ñ', ograve: 'ò',
    oacute: 'ó', ocirc: 'ô', otilde: 'õ', ouml: 'ö', oslash: 'ø', ugrave: 'ù', uacute: 'ú', ucirc: 'û', uuml: 'ü', yacute: 'ý',
    thorn: 'þ', yuml: 'ÿ', OElig: 'Œ', oelig: 'œ', Scaron: 'Š', scaron: 'š', Yuml: 'Ÿ', fnof: 'ƒ', circ: 'ˆ', tilde: '˜',
    Alpha: 'Α', Beta: 'Β', Gamma: 'Γ', Delta: 'Δ', alpha: 'α', beta: 'β', gamma: 'γ', delta: 'δ', pi: 'π', sigma: 'σ', omega: 'ω',
    lambda: 'λ', mu: 'μ', theta: 'θ',
  };
  const LEGACY = new Set(['amp', 'lt', 'gt', 'quot', 'nbsp', 'copy', 'reg']);

  function decodeEntities(text, inAttr = false) {
    if (text.indexOf('&') < 0) return text;
    return text.replace(/&(#[xX][0-9a-fA-F]{1,8}|#[0-9]{1,8}|[A-Za-z][A-Za-z0-9]{1,31})(;?)/g, (match, body, semi) => {
      if (body[0] === '#') {
        const code = body[1] === 'x' || body[1] === 'X' ? parseInt(body.slice(2), 16) : parseInt(body.slice(1), 10);
        if (!semi && inAttr) return match;
        if (!Number.isFinite(code) || code <= 0 || code > 0x10ffff || (code >= 0xd800 && code <= 0xdfff)) return '�';
        return String.fromCodePoint(code);
      }
      if (Object.prototype.hasOwnProperty.call(ENTITIES, body) && (semi || (!inAttr && LEGACY.has(body)))) return ENTITIES[body];
      return match;
    });
  }

  // ---------------------------------------------------------------- nodes

  class Node {
    constructor(type) { this.type = type; this.parent = null; }
    remove() {
      if (!this.parent) return this;
      const i = this.parent.children.indexOf(this);
      if (i >= 0) this.parent.children.splice(i, 1);
      this.parent = null;
      return this;
    }
  }

  class TextNode extends Node {
    constructor(data) { super('text'); this.data = data; }
  }

  class CommentNode extends Node {
    constructor(data) { super('comment'); this.data = data; }
  }

  class ElementNode extends Node {
    constructor(name, attrs = []) {
      super('element');
      this.name = name;
      this.attrs = attrs; // [[name, value]] in source order, names lowercased, unique
      this.children = [];
      this.baseUri = '';
    }
    getAttr(name) {
      const key = String(name).toLowerCase();
      for (const pair of this.attrs) if (pair[0] === key) return pair[1];
      return null;
    }
    setAttr(name, value) {
      const key = String(name).toLowerCase();
      for (const pair of this.attrs) if (pair[0] === key) { pair[1] = String(value); return; }
      this.attrs.push([key, String(value)]);
    }
    removeAttr(name) {
      const key = String(name).toLowerCase();
      this.attrs = this.attrs.filter((pair) => pair[0] !== key);
    }
    append(node) {
      if (node.parent) node.remove();
      node.parent = this;
      this.children.push(node);
      return node;
    }
    insertAt(index, node) {
      if (node.parent) node.remove();
      node.parent = this;
      this.children.splice(index, 0, node);
      return node;
    }
    elementChildren() { return this.children.filter((c) => c.type === 'element'); }
  }

  class DocumentNode extends ElementNode {
    constructor() { super('#document'); this.type = 'document'; }
  }

  // ---------------------------------------------------------------- parser

  function parseAttributes(src) {
    const attrs = [];
    const seen = new Set();
    const re = /([^\s"'>\/=]+)(?:\s*=\s*(?:"([^"]*)"|'([^']*)'|([^\s>]+)))?/g;
    let m;
    while ((m = re.exec(src))) {
      const name = m[1].toLowerCase();
      if (seen.has(name)) continue;
      seen.add(name);
      const raw = m[2] ?? m[3] ?? m[4] ?? '';
      attrs.push([name, decodeEntities(raw, true)]);
    }
    return attrs;
  }

  function parse(html, baseUri = '') {
    const doc = new DocumentNode();
    doc.baseUri = baseUri;
    const stack = [doc];
    const src = String(html ?? '');
    const len = src.length;
    let i = 0;
    const current = () => stack[stack.length - 1];
    const pushText = (text) => {
      if (!text) return;
      const parent = current();
      const last = parent.children[parent.children.length - 1];
      if (last && last.type === 'text') last.data += text;
      else parent.append(new TextNode(text));
    };
    const inScope = (name, barriers = SCOPE_BARRIER) => {
      for (let k = stack.length - 1; k > 0; k--) {
        const n = stack[k].name;
        if (n === name) return k;
        if (barriers.has(n)) return -1;
      }
      return -1;
    };
    const closeTo = (index) => { if (index > 0) stack.length = index; };
    const openElement = (name, attrs, selfClosing) => {
      // content after <head> without </head> belongs to the body (as in Jsoup)
      if (!HEAD_TAGS.has(name) && name !== 'head') { const k = stack.findIndex((n) => n.name === 'head'); if (k > 0) closeTo(k); }
      // implied end tags (the common subset of the HTML5 tree builder rules)
      if (CLOSES_P.has(name)) { const k = inScope('p'); if (k > 0) closeTo(k); }
      if (name === 'li') { const k = inScope('li', LIST_BARRIER); if (k > 0) closeTo(k); }
      if (name === 'dt' || name === 'dd') { const k = Math.max(inScope('dt', LIST_BARRIER), inScope('dd', LIST_BARRIER)); if (k > 0) closeTo(k); }
      if (name === 'option') { const k = inScope('option'); if (k > 0 && k === stack.length - 1) closeTo(k); }
      if (name === 'tr') { const k = inScope('tr', new Set(['table'])); if (k > 0) closeTo(k); }
      if (name === 'td' || name === 'th') { const k = Math.max(inScope('td', new Set(['table', 'tr'])), inScope('th', new Set(['table', 'tr']))); if (k > 0) closeTo(k); }
      if (name === 'thead' || name === 'tbody' || name === 'tfoot') {
        for (const t of ['thead', 'tbody', 'tfoot']) { const k = inScope(t, new Set(['table'])); if (k > 0) closeTo(k); }
      }
      if (name === 'a') { const k = inScope('a'); if (k > 0) closeTo(k); }
      if (/^h[1-6]$/.test(current().name || '')) { if (/^h[1-6]$/.test(name)) stack.pop(); }
      // implied table structure, as browsers and Jsoup build it (selectors copied from devtools contain tbody)
      if (name === 'tr' && current().name === 'table') { const tbody = new ElementNode('tbody'); current().append(tbody); stack.push(tbody); }
      if ((name === 'td' || name === 'th') && ['table', 'tbody', 'thead', 'tfoot'].includes(current().name)) {
        if (current().name === 'table') { const tbody = new ElementNode('tbody'); current().append(tbody); stack.push(tbody); }
        const tr = new ElementNode('tr'); current().append(tr); stack.push(tr);
      }
      const el = new ElementNode(name, attrs);
      current().append(el);
      if (!VOID.has(name) && !selfClosing && stack.length < MAX_DEPTH) stack.push(el);
      return el;
    };
    while (i < len) {
      const lt = src.indexOf('<', i);
      if (lt < 0) { pushText(decodeEntities(src.slice(i))); break; }
      if (lt > i) pushText(decodeEntities(src.slice(i, lt)));
      i = lt;
      if (src.startsWith('<!--', i)) {
        const end = src.indexOf('-->', i + 4);
        const stop = end < 0 ? len : end;
        current().append(new CommentNode(src.slice(i + 4, stop)));
        i = end < 0 ? len : end + 3;
        continue;
      }
      if (src.startsWith('<![CDATA[', i)) {
        const end = src.indexOf(']]>', i + 9);
        pushText(src.slice(i + 9, end < 0 ? len : end));
        i = end < 0 ? len : end + 3;
        continue;
      }
      if (src[i + 1] === '!' || src[i + 1] === '?') {
        const end = src.indexOf('>', i + 2);
        i = end < 0 ? len : end + 1;
        continue;
      }
      if (src[i + 1] === '/') {
        const m = /^<\/\s*([A-Za-z][^\s\/>]*)[^>]*>?/.exec(src.slice(i, i + 256));
        if (!m) { pushText('<'); i += 1; continue; }
        i += m[0].length;
        const name = m[1].toLowerCase();
        if (name === 'body' || name === 'html') continue; // trailing content stays in the body
        if (name === 'br') { openElement('br', [], true); continue; }
        if (name === 'p' && inScope('p') < 0) { openElement('p', [], true); continue; }
        for (let k = stack.length - 1; k > 0; k--) {
          if (stack[k].name === name) { stack.length = k; break; }
          if (SCOPE_BARRIER.has(stack[k].name) && !['td', 'th', 'table', 'body', 'html'].includes(name)) break;
        }
        continue;
      }
      const m = /^<([A-Za-z][^\s\/>]*)/.exec(src.slice(i, i + 256));
      if (!m) { pushText('<'); i += 1; continue; }
      // find the end of the tag, honouring quoted attribute values
      let j = i + m[0].length;
      let quote = '';
      for (; j < len; j++) {
        const ch = src[j];
        if (quote) { if (ch === quote) quote = ''; } else if (ch === '"' || ch === "'") quote = ch; else if (ch === '>') break;
      }
      const inner = src.slice(i + m[0].length, j);
      i = j + 1;
      const name = m[1].toLowerCase();
      const selfClosing = /\/\s*$/.test(inner);
      const attrs = parseAttributes(selfClosing ? inner.replace(/\/\s*$/, '') : inner);
      if (name === 'html' || name === 'body' || name === 'head') {
        const existing = findFirst(doc, (n) => n.name === name);
        if (existing) { for (const [k, v] of attrs) if (existing.getAttr(k) === null) existing.setAttr(k, v); if (name !== 'html') { const k = stack.indexOf(existing); if (k < 0) stack.push(existing); } continue; }
      }
      const el = openElement(name, attrs, selfClosing);
      if (RAW_TEXT.has(name) && !selfClosing) {
        const close = new RegExp(`</${name}\\s*>`, 'i');
        const rest = src.slice(i);
        const found = close.exec(rest);
        const text = found ? rest.slice(0, found.index) : rest;
        if (text) el.append(new TextNode(name === 'textarea' || name === 'title' ? decodeEntities(text) : text));
        i += found ? found.index + found[0].length : rest.length;
        if (stack[stack.length - 1] === el) stack.pop();
      }
    }
    normalizeDocument(doc);
    return doc;
  }

  function findFirst(rootNode, pred) {
    const todo = [...rootNode.children].reverse();
    while (todo.length) {
      const n = todo.pop();
      if (n.type !== 'element') continue;
      if (pred(n)) return n;
      for (let k = n.children.length - 1; k >= 0; k--) todo.push(n.children[k]);
    }
    return null;
  }

  function normalizeDocument(doc) {
    // like Jsoup: every document has <html><head></head><body></body></html>
    let html = doc.children.find((c) => c.type === 'element' && c.name === 'html');
    if (!html) {
      html = new ElementNode('html');
      const kids = [...doc.children];
      for (const k of kids) html.append(k);
      doc.append(html);
    } else {
      for (const k of [...doc.children]) if (k !== html && k.type !== 'comment') html.append(k);
    }
    let head = html.children.find((c) => c.type === 'element' && c.name === 'head');
    let body = html.children.find((c) => c.type === 'element' && c.name === 'body');
    if (!head) { head = new ElementNode('head'); html.insertAt(0, head); }
    if (!body) {
      body = new ElementNode('body');
      const move = html.children.filter((c) => c !== head);
      let leading = true;
      for (const k of move) {
        if (leading && k.type === 'element' && HEAD_TAGS.has(k.name) && k.name !== 'noscript') head.append(k);
        else if (leading && k.type === 'text' && !k.data.trim()) k.remove();
        else { leading = false; body.append(k); }
      }
      html.append(body);
    } else {
      for (const k of [...html.children]) if (k !== head && k !== body) {
        if (k.type === 'element' && HEAD_TAGS.has(k.name) && html.children.indexOf(k) < html.children.indexOf(body)) head.append(k);
        else if (!(k.type === 'text' && !k.data.trim())) body.append(k);
        else k.remove();
      }
    }
  }

  function parseFragment(html, baseUri = '') {
    const doc = parse(`<body>${html}</body>`, baseUri);
    return doc;
  }

  // ---------------------------------------------------------------- serialization / text

  const escText = (s) => s.replace(/&/g, '&amp;').replace(/</g, '&lt;').replace(/>/g, '&gt;').replace(/ /g, '&nbsp;');
  const escAttr = (s) => s.replace(/&/g, '&amp;').replace(/"/g, '&quot;').replace(/ /g, '&nbsp;');

  function outerHtml(node, out = []) {
    if (node.type === 'text') {
      const p = node.parent;
      out.push(p && RAW_TEXT.has(p.name) && p.name !== 'textarea' && p.name !== 'title' ? node.data : escText(node.data));
    } else if (node.type === 'comment') {
      out.push(`<!--${node.data}-->`);
    } else if (node.type === 'document') {
      for (const c of node.children) outerHtml(c, out);
    } else {
      out.push(`<${node.name}`);
      for (const [k, v] of node.attrs) out.push(v === '' && /^(checked|disabled|selected|hidden|async|defer|readonly|required|multiple|autofocus|open|reversed)$/.test(k) ? ` ${k}` : ` ${k}="${escAttr(v)}"`);
      out.push('>');
      if (!VOID.has(node.name)) {
        for (const c of node.children) outerHtml(c, out);
        out.push(`</${node.name}>`);
      }
    }
    return out;
  }

  function innerHtml(node) {
    const out = [];
    for (const c of node.children || []) outerHtml(c, out);
    return out.join('');
  }

  function appendNormalized(acc, text) {
    // collapse whitespace like Jsoup's StringUtil.appendNormalisedWhitespace
    for (const ch of text) {
      const ws = ch === ' ' || ch === '\t' || ch === '\n' || ch === '\r' || ch === '\f';
      if (ws) { if (acc.s.length && !acc.lastSpace) { acc.s += ' '; acc.lastSpace = true; } } else { acc.s += ch; acc.lastSpace = false; }
    }
  }

  function textOf(node, own = false) {
    const acc = { s: '', lastSpace: false };
    const walk = (n, depth) => {
      for (const c of n.children) {
        if (c.type === 'text') appendNormalized(acc, c.data);
        else if (c.type === 'element') {
          if (c.name === 'br') { if (acc.s.length && !acc.lastSpace) { acc.s += ' '; acc.lastSpace = true; } continue; }
          if (own) continue;
          if (BLOCK.has(c.name) && acc.s.length && !acc.lastSpace) { acc.s += ' '; acc.lastSpace = true; }
          if (depth < MAX_DEPTH) walk(c, depth + 1);
          if (BLOCK.has(c.name) && acc.s.length && !acc.lastSpace) { acc.s += ' '; acc.lastSpace = true; }
        }
      }
    };
    if (node.type === 'text') appendNormalized(acc, node.data);
    else walk(node, 0);
    return acc.s.trim();
  }

  function wholeText(node) {
    if (node.type === 'text') return node.data;
    let s = '';
    for (const c of node.children || []) {
      if (c.type === 'text') s += c.data;
      else if (c.type === 'element') s += c.name === 'br' ? '\n' : wholeText(c);
    }
    return s;
  }

  function dataOf(node) {
    // script / style contents and comments (Jsoup Element.data())
    let s = '';
    for (const c of node.children || []) {
      if (c.type === 'comment') s += c.data;
      else if (c.type === 'text' && c.parent && RAW_TEXT.has(c.parent.name)) s += c.data;
      else if (c.type === 'element') s += dataOf(c);
    }
    return s;
  }

  // ---------------------------------------------------------------- selectors

  class SelectorError extends Error {}

  function tokenizeSelector(input) {
    // -> groups: [[{comb, compound}]] ; compound: {tag, ns, simple: [...]}
    const s = String(input || '').trim();
    if (!s) throw new SelectorError('empty selector');
    let pos = 0;
    const groups = [];
    let steps = [];
    let comb = ' ';
    const isIdent = (ch) => /[A-Za-z0-9_\- -￿\\]/.test(ch);
    const readIdent = () => {
      let out = '';
      while (pos < s.length) {
        const ch = s[pos];
        if (ch === '\\' && pos + 1 < s.length) { out += s[pos + 1]; pos += 2; continue; }
        if (isIdent(ch) || ch === '|' || (ch === '*' && !out)) { out += ch; pos++; continue; }
        break;
      }
      return out;
    };
    const readBalanced = () => {
      // after '(' : returns raw content until the matching ')'
      let depth = 1;
      let out = '';
      let quote = '';
      while (pos < s.length) {
        const ch = s[pos++];
        if (quote) { out += ch; if (ch === quote) quote = ''; continue; }
        if (ch === '"' || ch === "'") { quote = ch; out += ch; continue; }
        if (ch === '(') depth++;
        if (ch === ')') { depth--; if (!depth) return out; }
        out += ch;
      }
      throw new SelectorError(`unbalanced parentheses in ${input}`);
    };
    const readCompound = () => {
      const compound = { tag: '*', simple: [] };
      if (s[pos] === '*') { pos++; } else if (isIdent(s[pos])) {
        const ident = readIdent().toLowerCase();
        compound.tag = ident.replace('|', ':');
      }
      while (pos < s.length) {
        const ch = s[pos];
        if (ch === '#') { pos++; compound.simple.push({ kind: 'id', value: readIdent() }); } else if (ch === '.') { pos++; compound.simple.push({ kind: 'class', value: readIdent() }); } else if (ch === '[') {
          pos++;
          const end = (() => { let q = ''; for (let k = pos; k < s.length; k++) { const c = s[k]; if (q) { if (c === q) q = ''; } else if (c === '"' || c === "'") q = c; else if (c === ']') return k; } return -1; })();
          if (end < 0) throw new SelectorError(`unclosed [ in ${input}`);
          const body = s.slice(pos, end).trim();
          pos = end + 1;
          const am = /^(\^?)([^\s~|^$*!=]+)\s*(?:([~|^$*!]?=)\s*(?:"((?:[^"\\]|\\.)*)"|'((?:[^'\\]|\\.)*)'|([^\s\]]*))\s*(i)?)?$/i.exec(body);
          if (!am) throw new SelectorError(`bad attribute selector [${body}]`);
          if (am[1]) compound.simple.push({ kind: 'attrPrefix', name: am[2].toLowerCase() });
          else compound.simple.push({ kind: 'attr', name: am[2].toLowerCase().replace('|', ':'), op: am[3] || '', value: am[4] ?? am[5] ?? am[6] ?? '' });
        } else if (ch === ':') {
          pos++;
          if (s[pos] === ':') pos++; // ::pseudo-element: ignored names fail below
          const name = readIdent().toLowerCase();
          let arg = null;
          if (s[pos] === '(') { pos++; arg = readBalanced().trim(); }
          compound.simple.push({ kind: 'pseudo', name, arg });
        } else break;
      }
      return compound;
    };
    while (pos < s.length) {
      const ch = s[pos];
      if (/\s/.test(ch)) { pos++; if (steps.length && comb === ' ') comb = ' '; continue; }
      if (ch === ',') { if (!steps.length) throw new SelectorError(`bad selector ${input}`); groups.push(steps); steps = []; comb = ' '; pos++; continue; }
      if (ch === '>' || ch === '+' || ch === '~') { comb = ch; pos++; continue; }
      const start = pos;
      const compound = readCompound();
      if (pos === start) throw new SelectorError(`unexpected "${ch}" in selector ${input}`);
      steps.push({ comb: steps.length || comb !== ' ' ? comb : ' ', compound, leading: !steps.length ? comb : undefined });
      comb = ' ';
    }
    if (!steps.length) throw new SelectorError(`bad selector ${input}`);
    groups.push(steps);
    return groups;
  }

  const selectorCache = new Map();
  function compile(selector) {
    let groups = selectorCache.get(selector);
    if (!groups) {
      groups = tokenizeSelector(selector);
      if (selectorCache.size > 2000) selectorCache.clear();
      selectorCache.set(selector, groups);
    }
    return groups;
  }

  function unquote(v) {
    const t = String(v ?? '').trim();
    if ((t.startsWith('"') && t.endsWith('"')) || (t.startsWith("'") && t.endsWith("'"))) return t.slice(1, -1);
    return t;
  }

  function parseNth(arg) {
    const a = String(arg || '').replace(/\s+/g, '').toLowerCase();
    if (a === 'odd') return [2, 1];
    if (a === 'even') return [2, 0];
    const m = /^([+-]?\d*)n([+-]\d+)?$/.exec(a);
    if (m) {
      const k = m[1] === '' || m[1] === '+' ? 1 : m[1] === '-' ? -1 : parseInt(m[1], 10);
      return [k, m[2] ? parseInt(m[2], 10) : 0];
    }
    if (/^[+-]?\d+$/.test(a)) return [0, parseInt(a, 10)];
    throw new SelectorError(`bad nth expression ${arg}`);
  }
  const nthMatch = ([a, b], index) => (a === 0 ? index === b : (index - b) / a >= 0 && (index - b) % a === 0);

  const elementSiblings = (el) => (el.parent ? el.parent.children.filter((c) => c.type === 'element') : [el]);
  const hasClass = (el, cls) => {
    const v = el.getAttr('class');
    if (!v) return false;
    const want = cls.toLowerCase();
    return v.split(/\s+/).some((c) => c.toLowerCase() === want);
  };

  function matchSimple(el, simple, scopeRoot) {
    switch (simple.kind) {
      case 'id': return el.getAttr('id') === simple.value;
      case 'class': return hasClass(el, simple.value);
      case 'attrPrefix': return el.attrs.some(([k]) => k.startsWith(simple.name));
      case 'attr': {
        const raw = el.getAttr(simple.name);
        if (raw === null) return simple.op === '!=';
        if (!simple.op) return true;
        const value = raw.trim().toLowerCase();
        const want = simple.value.trim().toLowerCase();
        switch (simple.op) {
          case '=': return value === want;
          case '!=': return value !== want;
          case '^=': return want !== '' && value.startsWith(want);
          case '$=': return want !== '' && value.endsWith(want);
          case '*=': return want !== '' && value.includes(want);
          case '|=': return value === want || value.startsWith(`${want}-`);
          case '~=': { // Jsoup: regular expression match on the value
            try { return new RegExp(simple.value).test(raw); } catch { return raw.split(/\s+/).includes(simple.value); }
          }
          default: return false;
        }
      }
      case 'pseudo': return matchPseudo(el, simple, scopeRoot);
      default: return false;
    }
  }

  function matchPseudo(el, { name, arg }, scopeRoot) {
    const sibs = () => elementSiblings(el);
    switch (name) {
      case 'first-child': return sibs()[0] === el;
      case 'last-child': { const s = sibs(); return s[s.length - 1] === el; }
      case 'only-child': return sibs().length === 1;
      case 'first-of-type': return sibs().find((s) => s.name === el.name) === el;
      case 'last-of-type': { const s = sibs().filter((x) => x.name === el.name); return s[s.length - 1] === el; }
      case 'only-of-type': return sibs().filter((s) => s.name === el.name).length === 1;
      case 'nth-child': return nthMatch(parseNth(arg), sibs().indexOf(el) + 1);
      case 'nth-last-child': { const s = sibs(); return nthMatch(parseNth(arg), s.length - s.indexOf(el)); }
      case 'nth-of-type': return nthMatch(parseNth(arg), sibs().filter((s) => s.name === el.name).indexOf(el) + 1);
      case 'nth-last-of-type': { const s = sibs().filter((x) => x.name === el.name); return nthMatch(parseNth(arg), s.length - s.indexOf(el)); }
      case 'eq': return sibs().indexOf(el) === parseInt(arg, 10);
      case 'lt': return sibs().indexOf(el) < parseInt(arg, 10);
      case 'gt': return sibs().indexOf(el) > parseInt(arg, 10);
      case 'empty': return !el.children.some((c) => c.type === 'element' || (c.type === 'text' && c.data.length));
      case 'root': return el.parent && el.parent.type === 'document';
      case 'not': return !matchesSelector(el, arg, scopeRoot);
      case 'is': case 'where': return matchesSelector(el, arg, scopeRoot);
      case 'has': {
        const a = String(arg || '').trim();
        if (/^[>+~]/.test(a)) return select(el, `:scope ${a}`, true).length > 0;
        return select(el, a, true).some((m) => m !== el);
      }
      case 'contains': return textOf(el).toLowerCase().includes(unquote(arg).toLowerCase());
      case 'containsown': return textOf(el, true).toLowerCase().includes(unquote(arg).toLowerCase());
      case 'containswholetext': return wholeText(el).includes(unquote(arg));
      case 'containsdata': return dataOf(el).toLowerCase().includes(unquote(arg).toLowerCase());
      case 'matches': return safeRegex(unquote(arg)).test(textOf(el));
      case 'matchesown': return safeRegex(unquote(arg)).test(textOf(el, true));
      case 'matcheswholetext': return safeRegex(unquote(arg)).test(wholeText(el));
      case 'matcheswholeowntext': return safeRegex(unquote(arg)).test(el.children.filter((c) => c.type === 'text').map((c) => c.data).join(''));
      case 'scope': return el === scopeRoot;
      case 'checked': return el.getAttr('checked') !== null || el.getAttr('selected') !== null;
      case 'selected': return el.getAttr('selected') !== null;
      case 'disabled': return el.getAttr('disabled') !== null;
      case 'enabled': return el.getAttr('disabled') === null;
      case 'header': return /^h[1-6]$/.test(el.name);
      case 'first': return sibs()[0] === el;
      case 'last': { const s = sibs(); return s[s.length - 1] === el; }
      default: throw new SelectorError(`unsupported pseudo selector :${name}`);
    }
  }

  function safeRegex(pattern) {
    try { return new RegExp(pattern); } catch { throw new SelectorError(`bad regular expression ${pattern}`); }
  }

  function matchCompound(el, compound, scopeRoot) {
    if (el.type !== 'element') return false;
    if (compound.tag !== '*') {
      const tag = compound.tag;
      if (tag.startsWith('*:')) { if (!el.name.endsWith(tag.slice(1)) && el.name !== tag.slice(2)) return false; } else if (el.name !== tag) return false;
    }
    for (const simple of compound.simple) if (!matchSimple(el, simple, scopeRoot)) return false;
    return true;
  }

  function matchSteps(el, steps, index, scopeRoot) {
    // right-to-left: steps[index] must match el, then its combinator relates to steps[index - 1]
    if (!matchCompound(el, steps[index].compound, scopeRoot)) return false;
    if (index === 0) {
      const lead = steps[0].leading;
      if (lead === '>' ) return el.parent === scopeRoot;
      if (lead === '+') return prevElement(el) === scopeRoot;
      if (lead === '~') { let p = prevElement(el); while (p) { if (p === scopeRoot) return true; p = prevElement(p); } return false; }
      return true;
    }
    const comb = steps[index].comb;
    if (comb === '>') {
      const p = el.parent;
      return Boolean(p && p.type === 'element' && withinScope(p, scopeRoot) && matchSteps(p, steps, index - 1, scopeRoot));
    }
    if (comb === ' ') {
      let p = el.parent;
      while (p && p.type === 'element') {
        if (!withinScope(p, scopeRoot)) return false;
        if (matchSteps(p, steps, index - 1, scopeRoot)) return true;
        if (p === scopeRoot) return false;
        p = p.parent;
      }
      return false;
    }
    if (comb === '+') {
      const p = prevElement(el);
      return Boolean(p && matchSteps(p, steps, index - 1, scopeRoot));
    }
    if (comb === '~') {
      let p = prevElement(el);
      while (p) { if (matchSteps(p, steps, index - 1, scopeRoot)) return true; p = prevElement(p); }
      return false;
    }
    return false;
  }

  function withinScope(node, scopeRoot) {
    if (!scopeRoot || scopeRoot.type === 'document') return true;
    let n = node;
    while (n) { if (n === scopeRoot) return true; n = n.parent; }
    return false;
  }

  function prevElement(el) {
    if (!el.parent) return null;
    const sibs = el.parent.children;
    for (let k = sibs.indexOf(el) - 1; k >= 0; k--) if (sibs[k].type === 'element') return sibs[k];
    return null;
  }

  function nextElement(el) {
    if (!el.parent) return null;
    const sibs = el.parent.children;
    for (let k = sibs.indexOf(el) + 1; k < sibs.length; k++) if (sibs[k].type === 'element') return sibs[k];
    return null;
  }

  function allElements(rootNode, includeRoot) {
    const out = [];
    if (includeRoot && rootNode.type === 'element') out.push(rootNode);
    const todo = [];
    for (let k = rootNode.children.length - 1; k >= 0; k--) todo.push(rootNode.children[k]);
    while (todo.length) {
      const n = todo.pop();
      if (n.type !== 'element') continue;
      out.push(n);
      for (let k = n.children.length - 1; k >= 0; k--) todo.push(n.children[k]);
    }
    return out;
  }

  function select(rootNode, selector, includeRoot = true) {
    const groups = compile(String(selector));
    const leadingSibling = groups.some((g) => g[0].leading === '+' || g[0].leading === '~');
    let candidates = allElements(rootNode, includeRoot);
    if (leadingSibling && rootNode.parent) candidates = [...candidates, ...allElements(rootNode.parent, false).filter((e) => e.parent === rootNode.parent)];
    const seen = new Set();
    const out = [];
    for (const el of candidates) {
      if (seen.has(el)) continue;
      for (const steps of groups) {
        if (matchSteps(el, steps, steps.length - 1, rootNode)) { seen.add(el); out.push(el); break; }
      }
    }
    return out;
  }

  function matchesSelector(el, selector, scopeRoot = null) {
    const groups = compile(String(selector));
    return groups.some((steps) => matchSteps(el, steps, steps.length - 1, scopeRoot || rootOf(el)));
  }

  function rootOf(node) { let n = node; while (n.parent) n = n.parent; return n; }

  function resolveUrl(base, href) {
    if (!href) return '';
    try { return new URL(href, base || undefined).href; } catch { return ''; }
  }

  function baseUriOf(node) {
    const r = rootOf(node);
    return r.baseUri || '';
  }

  return {
    parse, parseFragment, decodeEntities, select, matchesSelector, outerHtml: (n) => outerHtml(n).join(''), innerHtml, textOf,
    wholeText, dataOf, prevElement, nextElement, resolveUrl, baseUriOf, allElements, SelectorError, ElementNode, TextNode,
    CommentNode, DocumentNode, VOID,
  };
}));
