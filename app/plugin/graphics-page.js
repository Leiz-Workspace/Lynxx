// Lynxx Graphics page (hidden window of app/plugin/graphics.js): decodes images and draws extension canvases.
// Called by main with executeJavaScript; receives base64 images and numbers only.
'use strict';

(() => {
  const toBytes = (b64) => {
    if (typeof Uint8Array.fromBase64 === 'function') return Uint8Array.fromBase64(b64);
    const bin = atob(b64);
    const out = new Uint8Array(bin.length);
    for (let i = 0; i < bin.length; i++) out[i] = bin.charCodeAt(i);
    return out;
  };
  const toBase64 = (blob) => new Promise((ok, fail) => {
    const r = new FileReader();
    r.onload = () => ok(String(r.result).replace(/^data:[^,]*,/, ''));
    r.onerror = () => fail(r.error || new Error('could not read the image'));
    r.readAsDataURL(blob);
  });
  const decode = async (b64) => {
    try { return await createImageBitmap(new Blob([toBytes(b64)])); } catch { throw new Error('không đọc được ảnh (định dạng không hỗ trợ hoặc file hỏng)'); }
  };

  window.lynxxImageSize = async ({ image }) => {
    const bm = await decode(image);
    try { return { width: bm.width, height: bm.height }; } finally { bm.close(); }
  };

  window.lynxxCompose = async ({ width, height, images, draws, type, quality }) => {
    const bitmaps = [];
    try {
      for (const b64 of images) bitmaps.push(await decode(b64));
      const canvas = new OffscreenCanvas(width, height);
      const g = canvas.getContext('2d', { alpha: type !== 'image/jpeg' });
      if (type === 'image/jpeg') { g.fillStyle = '#fff'; g.fillRect(0, 0, width, height); } // no black where nothing is drawn
      g.imageSmoothingQuality = 'high';
      for (const [i, sx, sy, sw, sh, dx, dy, dw, dh] of draws) {
        if (!sw || !sh || !dw || !dh) continue; // an empty rectangle draws nothing
        g.drawImage(bitmaps[i], sx, sy, sw, sh, dx, dy, dw, dh);
      }
      const blob = await canvas.convertToBlob({ type, quality });
      return { data: await toBase64(blob), type: blob.type, width, height };
    } finally {
      for (const bm of bitmaps) bm.close();
    }
  };
})();
