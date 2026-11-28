// Lynxx plugin host (Electron main): Graphics of extensions. An extension records what to draw (Graphics.createImage /
// createCanvas / drawImage in runtime.js) and canvas.capture() is one synchronous bridge call that lands here. The drawing
// happens in a hidden window of its own: Chromium's image decoders (JPEG, PNG, WebP, GIF, AVIF, BMP) and canvas, no network,
// no extension code (it only receives image bytes and numbers).
'use strict';

const path = require('path');
const { pathToFileURL } = require('url');

const MAX_SIDE = 16384;
const MAX_PIXELS = 64 * 1024 * 1024;
const MAX_IMAGES = 64;
const MAX_DRAWS = 20000;
const MAX_IMAGE_B64 = 64 * 1024 * 1024;
const MAX_TOTAL_B64 = 160 * 1024 * 1024;
const TIMEOUT_MS = 60000;
const TYPES = { jpeg: 'image/jpeg', jpg: 'image/jpeg', 'image/jpeg': 'image/jpeg', png: 'image/png', 'image/png': 'image/png', webp: 'image/webp', 'image/webp': 'image/webp' };
const RE_B64 = /^[A-Za-z0-9+/]*={0,2}$/;

function cleanBase64(value) {
  const s = String(value ?? '').replace(/^data:[^,]*,/, '').replace(/\s+/g, '').replace(/-/g, '+').replace(/_/g, '/');
  if (!s || s.length > MAX_IMAGE_B64 || !RE_B64.test(s)) throw new Error('ảnh không phải base64 hợp lệ');
  return s;
}

function outputType(p, images) {
  // the asked type, else the type of the first image when it is PNG (transparency), else JPEG (manga pages, small files)
  const asked = TYPES[String(p.type || '').toLowerCase()];
  if (asked) return asked;
  return images[0] && Buffer.from(images[0].slice(0, 16), 'base64').subarray(0, 4).toString('latin1') === '\x89PNG' ? 'image/png' : 'image/jpeg';
}

function composeJob(p) {
  const width = Math.round(Number(p.width));
  const height = Math.round(Number(p.height));
  if (!(width >= 1 && height >= 1 && width <= MAX_SIDE && height <= MAX_SIDE && width * height <= MAX_PIXELS)) throw new Error(`kích thước canvas không hợp lệ: ${p.width}x${p.height}`);
  const images = (Array.isArray(p.images) ? p.images : []).slice(0, MAX_IMAGES + 1).map(cleanBase64);
  if (images.length > MAX_IMAGES) throw new Error('quá nhiều ảnh trong một canvas');
  if (images.reduce((n, s) => n + s.length, 0) > MAX_TOTAL_B64) throw new Error('ảnh quá lớn');
  const raw = Array.isArray(p.draws) ? p.draws : [];
  if (raw.length > MAX_DRAWS) throw new Error('quá nhiều lệnh vẽ');
  const draws = raw.map((d) => {
    const n = (Array.isArray(d) ? d : []).slice(0, 9).map(Number);
    if (n.length !== 9 || !n.every(Number.isFinite) || !Number.isInteger(n[0]) || n[0] < 0 || n[0] >= images.length) throw new Error('lệnh vẽ không hợp lệ');
    return n;
  });
  const type = outputType(p, images);
  const q = Number(p.quality);
  const quality = q > 0 && q <= 1 ? q : 0.92;
  return { width, height, images, draws, type, quality };
}

class GraphicsWorker {
  /** opts: {BrowserWindow, partition (no network), isDev} */
  constructor(opts) {
    this.opts = opts;
    this.win = null;
    this.ready = null;
    this.closed = false;
  }

  window() {
    if (this.closed) return Promise.reject(new Error('Lynxx is closing'));
    if (this.win && !this.win.isDestroyed() && this.ready) return this.ready;
    const { BrowserWindow, partition, isDev } = this.opts;
    const win = new BrowserWindow({
      show: false, width: 200, height: 200,
      webPreferences: {
        sandbox: true, contextIsolation: true, nodeIntegration: false, partition, webgl: false, spellcheck: false,
        backgroundThrottling: false, devTools: Boolean(isDev),
      },
    });
    win.webContents.on('will-navigate', (e) => e.preventDefault());
    win.webContents.setWindowOpenHandler(() => ({ action: 'deny' }));
    const drop = () => { if (this.win === win) { this.win = null; this.ready = null; } if (!win.isDestroyed()) win.destroy(); };
    win.webContents.on('render-process-gone', drop);
    win.on('closed', drop);
    this.win = win;
    this.ready = win.loadURL(pathToFileURL(path.join(__dirname, 'graphics.html')).href).then(() => win, (err) => { drop(); throw err; });
    return this.ready;
  }

  async exec(fn, job) {
    const win = await this.window();
    let timer;
    const timeout = new Promise((_ok, fail) => { timer = setTimeout(() => fail(new Error('vẽ ảnh quá lâu')), TIMEOUT_MS); });
    try {
      return await Promise.race([win.webContents.executeJavaScript(`window.${fn}(${JSON.stringify(job)})`, true), timeout]);
    } catch (err) {
      // a window stuck in a huge image is replaced for the next call
      if (/quá lâu/.test(err.message)) { this.win = null; this.ready = null; if (!win.isDestroyed()) win.destroy(); }
      throw err;
    } finally {
      clearTimeout(timer);
    }
  }

  /** bridge op from the sandbox: {op: 'size', image} -> {width, height} | {op: 'compose', ...} -> {data, type} */
  async handle(p) {
    if (p?.op === 'size') return this.exec('lynxxImageSize', { image: cleanBase64(p.image) });
    if (p?.op === 'compose') return this.exec('lynxxCompose', composeJob(p));
    throw new Error(`Graphics: unknown op ${p?.op}`);
  }

  close() {
    this.closed = true;
    if (this.win && !this.win.isDestroyed()) this.win.destroy();
    this.win = null;
    this.ready = null;
  }
}

module.exports = { GraphicsWorker, composeJob };
