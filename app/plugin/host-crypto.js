// Lynxx plugin host (Node / Electron main): the crypto primitives behind the CryptoJS facade (app/plugin/cryptojs.js).
// Inputs and outputs are base64 so they cross the synchronous IPC bridge unchanged. Never throws: {error} instead.
'use strict';

const crypto = require('crypto');

const HASHES = new Set(['md5', 'sha1', 'sha224', 'sha256', 'sha384', 'sha512', 'sha3-512', 'ripemd160']);
const MAX_INPUT = 64 * 1024 * 1024;

const b = (s) => Buffer.from(String(s || ''), 'base64');

function padData(data, block, scheme) {
  const n = block - (data.length % block);
  if (scheme === 'zero') return data.length % block === 0 ? data : Buffer.concat([data, Buffer.alloc(n)]);
  if (scheme === 'ansix923') return Buffer.concat([data, Buffer.alloc(n - 1), Buffer.from([n])]);
  if (scheme === 'iso10126') return Buffer.concat([data, crypto.randomBytes(n - 1), Buffer.from([n])]);
  if (scheme === 'iso97971') return padData(Buffer.concat([data, Buffer.from([0x80])]), block, 'zero');
  return data;
}

function unpadData(data, scheme) {
  if (!data.length) return data;
  if (scheme === 'zero') { let end = data.length; while (end > 0 && data[end - 1] === 0) end--; return data.subarray(0, end); }
  if (scheme === 'ansix923' || scheme === 'iso10126') { const n = data[data.length - 1]; return n > 0 && n <= data.length ? data.subarray(0, data.length - n) : data; }
  if (scheme === 'iso97971') { const z = unpadData(data, 'zero'); return z.length && z[z.length - 1] === 0x80 ? z.subarray(0, z.length - 1) : z; }
  return data;
}

function cryptoOp(op, args = {}) {
  try {
    if (op === 'hash') {
      if (!HASHES.has(args.alg)) return { error: `unsupported hash ${args.alg}` };
      const data = b(args.data);
      if (data.length > MAX_INPUT) return { error: 'input too large' };
      return { data: crypto.createHash(args.alg).update(data).digest('base64') };
    }
    if (op === 'hmac') {
      if (!HASHES.has(args.alg)) return { error: `unsupported hash ${args.alg}` };
      return { data: crypto.createHmac(args.alg, b(args.key)).update(b(args.data)).digest('base64') };
    }
    if (op === 'pbkdf2') {
      const digest = String(args.digest || 'sha256');
      if (!HASHES.has(digest)) return { error: `unsupported hash ${digest}` };
      const iterations = Math.max(1, Math.min(10000000, Number(args.iterations) || 1));
      const keylen = Math.max(1, Math.min(1024, Number(args.keylen) || 16));
      return { data: crypto.pbkdf2Sync(b(args.password), b(args.salt), iterations, keylen, digest).toString('base64') };
    }
    if (op === 'cipher') {
      const name = String(args.cipher || '');
      if (!/^(aes-(128|192|256)-(cbc|ecb|ctr|cfb|ofb)|des-(cbc|ecb|cfb|ofb)|des-ede3(-cbc|-cfb|-ofb)?)$/.test(name)) return { error: `unsupported cipher ${name}` };
      const key = b(args.key);
      const iv = args.iv ? b(args.iv) : null;
      const data = b(args.data);
      if (data.length > MAX_INPUT) return { error: 'input too large' };
      const scheme = String(args.padding || 'pkcs7');
      const block = Number(args.block) || 16;
      const auto = scheme === 'pkcs7';
      if (args.direction === 'encrypt') {
        const c = crypto.createCipheriv(name, key, name.endsWith('ecb') || name === 'des-ede3' ? null : iv);
        c.setAutoPadding(auto);
        const input = auto || scheme === 'none' ? data : padData(data, block, scheme);
        return { data: Buffer.concat([c.update(input), c.final()]).toString('base64') };
      }
      const d = crypto.createDecipheriv(name, key, name.endsWith('ecb') || name === 'des-ede3' ? null : iv);
      d.setAutoPadding(auto);
      const out = Buffer.concat([d.update(data), d.final()]);
      return { data: (auto || scheme === 'none' ? out : unpadData(out, scheme)).toString('base64') };
    }
    if (op === 'gcm') {
      const key = b(args.key);
      const name = `aes-${key.length * 8}-gcm`;
      if (![16, 24, 32].includes(key.length)) return { error: 'bad AES-GCM key length' };
      const iv = b(args.iv);
      const data = b(args.data);
      if (args.direction === 'encrypt') {
        const c = crypto.createCipheriv(name, key, iv);
        if (args.aad) c.setAAD(b(args.aad));
        const out = Buffer.concat([c.update(data), c.final(), c.getAuthTag()]);
        return { data: out.toString('base64') };
      }
      if (data.length < 16) return { error: 'ciphertext too short' };
      const d = crypto.createDecipheriv(name, key, iv);
      if (args.aad) d.setAAD(b(args.aad));
      d.setAuthTag(data.subarray(data.length - 16));
      return { data: Buffer.concat([d.update(data.subarray(0, data.length - 16)), d.final()]).toString('base64') };
    }
    return { error: `unknown crypto op ${op}` };
  } catch (err) {
    return { error: String(err && err.message ? err.message : err) };
  }
}

module.exports = { cryptoOp };
