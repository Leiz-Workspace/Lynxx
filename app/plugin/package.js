// Lynxx plugin host (Node / Electron main): reading and validating extension folders.
// A package is plugin.json + optional icon.png + src/*.js (top level of src/ only).
'use strict';

const fs = require('fs');
const path = require('path');
const crypto = require('crypto');

const MAX_MANIFEST = 2 * 1024 * 1024;
const MAX_SCRIPT = 10 * 1024 * 1024;
const MAX_ICON = 5 * 1024 * 1024;
const MAX_SCRIPTS = 200;
const CONTENT_TYPES = new Set(['novel', 'comic', 'chinese_novel']);
const ALL_TYPES = new Set([...CONTENT_TYPES, 'video', 'audio', 'news', 'forum', 'tts', 'translate', 'ai']);
const SCRIPT_KEYS = ['home', 'explore', 'genre', 'detail', 'search', 'page', 'toc', 'chap', 'config', 'track', 'posts', 'article', 'gen', 'comment', 'suggest'];
const RE_SCRIPT_FILE = /^[A-Za-z0-9_.-]{1,80}\.js$/;

function str(v, max = 2000) { return typeof v === 'string' ? v.slice(0, max) : (v === undefined || v === null ? '' : String(v).slice(0, max)); }

function normalizeManifest(raw) {
  if (!raw || typeof raw !== 'object' || Array.isArray(raw)) throw new Error('plugin.json phải là một object JSON');
  const m = raw.metadata && typeof raw.metadata === 'object' ? raw.metadata : {};
  const type = str(m.type, 40).toLowerCase() || 'novel';
  const metadata = {
    name: str(m.name, 120).trim(),
    author: str(m.author, 120).trim(),
    version: Number.isInteger(m.version) ? m.version : (Number.isInteger(Number(m.version)) && String(m.version).trim() !== '' ? Number(m.version) : 0),
    source: str(m.source, 500).trim(),
    regexp: str(m.regexp, 1000),
    description: str(m.description, 2000),
    locale: str(m.locale || m.local, 20),
    type: ALL_TYPES.has(type) ? type : 'novel',
    nsfw: m.nsfw === true || str(m.tag, 40).toLowerCase() === 'nsfw',
    encrypt: m.encrypt === true,
  };
  if (!metadata.name) throw new Error('plugin.json thiếu metadata.name');
  const script = {};
  const rawScripts = raw.script && typeof raw.script === 'object' ? raw.script : {};
  for (const [key, file] of Object.entries(rawScripts)) {
    if (typeof file !== 'string' || !RE_SCRIPT_FILE.test(file)) continue;
    script[String(key).slice(0, 40)] = file;
  }
  const config = {};
  if (raw.config && typeof raw.config === 'object' && !Array.isArray(raw.config)) {
    for (const [key, entry] of Object.entries(raw.config).slice(0, 100)) {
      if (!/^[A-Za-z_$][A-Za-z0-9_$]{0,63}$/.test(key)) continue;
      if (entry && typeof entry === 'object' && !Array.isArray(entry)) {
        if (entry.mode === 'database') continue;
        config[key] = {
          title: str(entry.title, 120), subtitle: str(entry.subtitle, 300), mode: str(entry.mode, 20) || 'input',
          values: Array.isArray(entry.values) ? entry.values.slice(0, 50).map((v) => (v && typeof v === 'object' ? { title: str(v.title || v.name || v.value, 120), value: str(v.value ?? v.id ?? v.title, 500) } : { title: str(v, 120), value: str(v, 500) })) : [],
          default: entry.default === undefined || entry.default === null ? '' : str(entry.default, 2000),
        };
      } else config[key] = { title: key, subtitle: '', mode: 'value', values: [], default: str(entry, 2000) };
    }
  }
  return { metadata, script, config };
}

function pluginId(metadata) {
  // stable per (name, source): an update of the same extension keeps its id, storage and library collections
  const slug = metadata.name.normalize('NFD').replace(/[̀-ͯ]/g, '').replace(/đ/gi, 'd').toLowerCase().replace(/[^a-z0-9]+/g, '-').replace(/^-+|-+$/g, '').slice(0, 32) || 'plugin';
  const hash = crypto.createHash('sha256').update(`${metadata.name}\n${metadata.source}`).digest('hex').slice(0, 8);
  return `${slug}-${hash}`;
}

function readPackageDir(dir) {
  const root = path.resolve(dir);
  const manifestPath = path.join(root, 'plugin.json');
  const st = fs.lstatSync(manifestPath);
  if (!st.isFile() || st.size > MAX_MANIFEST) throw new Error('plugin.json không hợp lệ hoặc quá lớn');
  let raw;
  try { raw = JSON.parse(fs.readFileSync(manifestPath, 'utf8').replace(/^﻿/, '')); } catch (err) { throw new Error(`plugin.json không đọc được: ${err.message}`); }
  const manifest = normalizeManifest(raw);
  const files = {};
  const srcDir = path.join(root, 'src');
  if (fs.existsSync(srcDir) && fs.lstatSync(srcDir).isDirectory()) {
    for (const name of fs.readdirSync(srcDir).slice(0, 2000)) {
      if (!RE_SCRIPT_FILE.test(name)) continue;
      const file = path.join(srcDir, name);
      const s = fs.lstatSync(file);
      if (!s.isFile() || s.size > MAX_SCRIPT) continue;
      files[name] = fs.readFileSync(file, 'utf8').replace(/^﻿/, '');
      if (Object.keys(files).length >= MAX_SCRIPTS) break;
    }
  }
  let icon = '';
  const iconPath = path.join(root, 'icon.png');
  if (fs.existsSync(iconPath)) {
    const s = fs.lstatSync(iconPath);
    if (s.isFile() && s.size <= MAX_ICON) icon = iconPath;
  }
  const missing = Object.values(manifest.script).filter((f) => !(f in files));
  return { root, manifest, files, icon, missing, id: pluginId(manifest.metadata) };
}

function capabilities(manifest, files) {
  const has = (key) => Boolean(manifest.script[key] && files[manifest.script[key]]);
  const type = manifest.metadata.type;
  const content = CONTENT_TYPES.has(type);
  // Graphics: the extension draws images again (ex. pages a site serves cut into shuffled strips), see app/plugin/graphics.js
  const graphics = Object.values(files || {}).some((src) => /\bGraphics\s*\./.test(String(src)));
  return {
    content, graphics,
    home: has('home'), genre: has('genre'), search: has('search'), detail: has('detail'), page: has('page'), toc: has('toc'), chap: has('chap'),
    downloadable: content && has('detail') && has('toc') && has('chap') && !manifest.metadata.encrypt,
    kind: type === 'comic' ? 'comic' : 'novel',
  };
}

function regexMatches(manifest, url) {
  const pattern = manifest.metadata.regexp;
  if (!pattern) return false;
  let re;
  try { re = new RegExp(`^(?:${pattern})$`); } catch { return false; }
  const s = String(url || '').trim();
  const bare = s.replace(/^https?:\/\//i, '').replace(/\/+$/, '');
  return re.test(s) || re.test(bare) || re.test(s.replace(/\/+$/, ''));
}

function hostMatches(manifest, url) {
  try {
    const want = new URL(manifest.metadata.source).hostname.replace(/^www\./, '');
    const got = new URL(url).hostname.replace(/^www\./, '');
    return Boolean(want) && (got === want || got.endsWith(`.${want}`));
  } catch { return false; }
}

module.exports = { normalizeManifest, readPackageDir, pluginId, capabilities, regexMatches, hostMatches, CONTENT_TYPES, SCRIPT_KEYS, RE_SCRIPT_FILE, MAX_SCRIPT };
