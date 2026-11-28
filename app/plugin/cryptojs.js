// Lynxx plugin runtime: a CryptoJS 4.2-compatible facade for extensions (`load('crypto.js')` always means the
// bundled CryptoJS). The data model (WordArray, CipherParams, encoders, OpenSSL "Salted__" format, EvpKDF) is
// implemented here; the primitives (hashes, HMAC, PBKDF2, block ciphers) are delegated to the synchronous host, which
// uses Node's crypto module. Covered: MD5 SHA1 SHA224 SHA256 SHA384 SHA512 SHA3 RIPEMD160, Hmac*, PBKDF2, EvpKDF,
// AES / DES / TripleDES with CBC ECB CTR CFB OFB and Pkcs7 / NoPadding / ZeroPadding / Iso10126 / AnsiX923, plus the
// the extension API's additions AES.encryptGcm / decryptGcm.
(function (root, factory) {
  if (typeof module === 'object' && module.exports) module.exports = factory();
  else root.LynxxCryptoJS = factory();
}(typeof self !== 'undefined' ? self : this, () => {
  'use strict';

  function bytesToB64(bytes) {
    if (typeof Buffer !== 'undefined') return Buffer.from(bytes).toString('base64');
    let s = '';
    for (let i = 0; i < bytes.length; i++) s += String.fromCharCode(bytes[i]);
    return btoa(s);
  }
  function b64ToBytes(b64) {
    if (typeof Buffer !== 'undefined') return new Uint8Array(Buffer.from(String(b64 || ''), 'base64'));
    const bin = atob(String(b64 || ''));
    const out = new Uint8Array(bin.length);
    for (let i = 0; i < bin.length; i++) out[i] = bin.charCodeAt(i);
    return out;
  }

  function createCryptoJS(host) {
    class WordArray {
      constructor(words, sigBytes) {
        this.words = words || [];
        this.sigBytes = sigBytes !== undefined ? sigBytes : this.words.length * 4;
      }
      static create(words, sigBytes) {
        if (words instanceof Uint8Array || words instanceof ArrayBuffer || ArrayBuffer.isView(words)) return fromBytes(new Uint8Array(words.buffer ? words.buffer.slice(words.byteOffset, words.byteOffset + words.byteLength) : words));
        return new WordArray(words, sigBytes);
      }
      static random(n) {
        const bytes = new Uint8Array(n);
        if (typeof crypto !== 'undefined' && crypto.getRandomValues) crypto.getRandomValues(bytes);
        else for (let i = 0; i < n; i++) bytes[i] = Math.floor(Math.random() * 256);
        return fromBytes(bytes);
      }
      toString(encoder) { return (encoder || Hex).stringify(this); }
      concat(other) {
        const a = toBytes(this);
        const b = toBytes(other);
        const out = new Uint8Array(a.length + b.length);
        out.set(a); out.set(b, a.length);
        const merged = fromBytes(out);
        this.words = merged.words; this.sigBytes = merged.sigBytes;
        return this;
      }
      clamp() { const m = fromBytes(toBytes(this)); this.words = m.words; this.sigBytes = m.sigBytes; return this; }
      clone() { return new WordArray(this.words.slice(), this.sigBytes); }
    }

    function fromBytes(bytes) {
      const words = [];
      for (let i = 0; i < bytes.length; i++) words[i >>> 2] |= bytes[i] << (24 - (i % 4) * 8);
      return new WordArray(words, bytes.length);
    }
    function toBytes(wa) {
      if (wa instanceof Uint8Array) return wa;
      if (typeof wa === 'string') return Utf8.parse(wa).bytes();
      const n = Math.max(0, wa.sigBytes | 0);
      const out = new Uint8Array(n);
      for (let i = 0; i < n; i++) out[i] = (wa.words[i >>> 2] >>> (24 - (i % 4) * 8)) & 0xff;
      return out;
    }
    WordArray.prototype.bytes = function bytes() { return toBytes(this); };

    const Hex = {
      stringify: (wa) => Array.from(toBytes(wa), (b) => b.toString(16).padStart(2, '0')).join(''),
      parse: (hex) => { const s = String(hex); const out = new Uint8Array(s.length >>> 1); for (let i = 0; i < out.length; i++) out[i] = parseInt(s.substr(i * 2, 2), 16); return fromBytes(out); },
    };
    const Latin1 = {
      stringify: (wa) => Array.from(toBytes(wa), (b) => String.fromCharCode(b)).join(''),
      parse: (str) => { const s = String(str); const out = new Uint8Array(s.length); for (let i = 0; i < s.length; i++) out[i] = s.charCodeAt(i) & 0xff; return fromBytes(out); },
    };
    const Utf8 = {
      stringify: (wa) => new TextDecoder('utf-8', { fatal: true }).decode(toBytes(wa)),
      parse: (str) => fromBytes(new TextEncoder().encode(String(str))),
    };
    const Utf16LE = {
      stringify: (wa) => { const b = toBytes(wa); let s = ''; for (let i = 0; i + 1 < b.length; i += 2) s += String.fromCharCode(b[i] | (b[i + 1] << 8)); return s; },
      parse: (str) => { const s = String(str); const out = new Uint8Array(s.length * 2); for (let i = 0; i < s.length; i++) { const c = s.charCodeAt(i); out[i * 2] = c & 0xff; out[i * 2 + 1] = c >> 8; } return fromBytes(out); },
    };
    const Utf16BE = {
      stringify: (wa) => { const b = toBytes(wa); let s = ''; for (let i = 0; i + 1 < b.length; i += 2) s += String.fromCharCode((b[i] << 8) | b[i + 1]); return s; },
      parse: (str) => { const s = String(str); const out = new Uint8Array(s.length * 2); for (let i = 0; i < s.length; i++) { const c = s.charCodeAt(i); out[i * 2] = c >> 8; out[i * 2 + 1] = c & 0xff; } return fromBytes(out); },
    };
    const Base64 = {
      stringify: (wa) => bytesToB64(toBytes(wa)),
      parse: (str) => fromBytes(b64ToBytes(String(str).replace(/\s+/g, ''))),
    };
    const Base64url = {
      stringify: (wa, urlSafe = true) => { const s = bytesToB64(toBytes(wa)); return urlSafe ? s.replace(/\+/g, '-').replace(/\//g, '_').replace(/=+$/, '') : s; },
      parse: (str) => { let s = String(str).replace(/-/g, '+').replace(/_/g, '/'); while (s.length % 4) s += '='; return fromBytes(b64ToBytes(s)); },
    };

    const asWA = (v) => (v instanceof WordArray ? v : (v && v.words ? new WordArray(v.words, v.sigBytes) : Utf8.parse(v === undefined || v === null ? '' : String(v))));
    const call = (op, args) => {
      const r = host.crypto(op, args);
      if (!r || r.error) throw new Error(`CryptoJS ${op}: ${r ? r.error : 'unavailable'}`);
      return fromBytes(b64ToBytes(r.data));
    };
    const hash = (alg) => {
      const fn = (msg) => call('hash', { alg, data: bytesToB64(toBytes(asWA(msg))) });
      fn.algName = alg;
      return fn;
    };
    const hmac = (alg) => (msg, key) => call('hmac', { alg, key: bytesToB64(toBytes(asWA(key))), data: bytesToB64(toBytes(asWA(msg))) });

    // CryptoJS.SHA3 is Keccak with the pre-FIPS padding: neither Node's sha3-512 nor Electron's BoringSSL computes it
    const noSha3 = () => { throw new Error('CryptoJS.SHA3 (Keccak) is not supported by Lynxx'); };
    const algos = { MD5: hash('md5'), SHA1: hash('sha1'), SHA224: hash('sha224'), SHA256: hash('sha256'), SHA384: hash('sha384'), SHA512: hash('sha512'), SHA3: noSha3, RIPEMD160: hash('ripemd160') };
    const hasherName = (h) => (h && h.algName) || (typeof h === 'string' ? h : 'sha256');

    function evpKdf(password, salt, keySizeWords, ivSizeWords, hasher = algos.MD5, iterations = 1) {
      const pass = toBytes(asWA(password));
      const saltBytes = salt ? toBytes(salt) : new Uint8Array(0);
      const need = (keySizeWords + ivSizeWords) * 4;
      let derived = new Uint8Array(0);
      let block = new Uint8Array(0);
      while (derived.length < need) {
        let input = new Uint8Array(block.length + pass.length + saltBytes.length);
        input.set(block); input.set(pass, block.length); input.set(saltBytes, block.length + pass.length);
        block = toBytes(hasher(fromBytes(input)));
        for (let i = 1; i < iterations; i++) block = toBytes(hasher(fromBytes(block)));
        const next = new Uint8Array(derived.length + block.length);
        next.set(derived); next.set(block, derived.length);
        derived = next;
        input = null;
      }
      return { key: fromBytes(derived.slice(0, keySizeWords * 4)), iv: fromBytes(derived.slice(keySizeWords * 4, need)) };
    }

    class CipherParams {
      constructor(cfg) { Object.assign(this, cfg); }
      toString(formatter) { return (formatter || OpenSSL).stringify(this); }
    }
    const OpenSSL = {
      stringify: (cp) => {
        const ct = toBytes(cp.ciphertext);
        if (!cp.salt) return bytesToB64(ct);
        const salt = toBytes(cp.salt);
        const out = new Uint8Array(16 + ct.length);
        out.set([0x53, 0x61, 0x6c, 0x74, 0x65, 0x64, 0x5f, 0x5f]); out.set(salt.slice(0, 8), 8); out.set(ct, 16);
        return bytesToB64(out);
      },
      parse: (str) => {
        const raw = b64ToBytes(String(str).replace(/\s+/g, ''));
        if (raw.length >= 16 && String.fromCharCode(...raw.slice(0, 8)) === 'Salted__') return new CipherParams({ ciphertext: fromBytes(raw.slice(16)), salt: fromBytes(raw.slice(8, 16)) });
        return new CipherParams({ ciphertext: fromBytes(raw) });
      },
    };
    const HexFormat = { stringify: (cp) => Hex.stringify(cp.ciphertext), parse: (s) => new CipherParams({ ciphertext: Hex.parse(s) }) };

    const mode = { CBC: { name: 'cbc' }, ECB: { name: 'ecb' }, CTR: { name: 'ctr' }, CFB: { name: 'cfb' }, OFB: { name: 'ofb' }, CTRGladman: { name: 'ctr' } };
    const pad = { Pkcs7: { name: 'pkcs7' }, NoPadding: { name: 'none' }, ZeroPadding: { name: 'zero' }, Iso10126: { name: 'iso10126' }, AnsiX923: { name: 'ansix923' }, Iso97971: { name: 'iso97971' } };

    function makeCipher(family, defaultKeyWords, blockBytes) {
      const cipherName = (keyBytes, m) => {
        if (family === 'aes') return `aes-${keyBytes * 8}-${m}`;
        if (family === 'des') return m === 'ecb' ? 'des-ecb' : `des-${m}`;
        return m === 'ecb' ? 'des-ede3' : `des-ede3-${m}`;
      };
      const run = (direction, data, key, cfg) => {
        const m = (cfg.mode || mode.CBC).name;
        const p = (cfg.padding || pad.Pkcs7).name;
        const keyBytes = toBytes(key);
        return call('cipher', {
          direction, cipher: cipherName(keyBytes.length, m), key: bytesToB64(keyBytes), iv: m === 'ecb' ? '' : bytesToB64(toBytes(cfg.iv || fromBytes(new Uint8Array(blockBytes)))),
          data: bytesToB64(toBytes(data)), padding: m === 'ctr' || m === 'cfb' || m === 'ofb' ? 'none' : p, block: blockBytes,
        });
      };
      return {
        encrypt(message, key, cfg = {}) {
          let k = key;
          let iv = cfg.iv;
          let salt = null;
          if (typeof key === 'string') {
            salt = cfg.salt ? asWA(cfg.salt) : WordArray.random(8);
            const d = evpKdf(key, salt, defaultKeyWords, blockBytes / 4, cfg.hasher || algos.MD5);
            k = d.key; iv = d.iv;
          }
          const ciphertext = run('encrypt', asWA(message), asWA(k), { ...cfg, iv });
          return new CipherParams({ ciphertext, key: k, iv, salt, algorithm: family, mode: cfg.mode, padding: cfg.padding, blockSize: blockBytes / 4, formatter: cfg.format || OpenSSL });
        },
        decrypt(ciphertext, key, cfg = {}) {
          const params = typeof ciphertext === 'string' ? (cfg.format || OpenSSL).parse(ciphertext) : ciphertext;
          let k = key;
          let iv = cfg.iv;
          if (typeof key === 'string') {
            const d = evpKdf(key, params.salt, defaultKeyWords, blockBytes / 4, cfg.hasher || algos.MD5);
            k = d.key; iv = d.iv;
          }
          return run('decrypt', params.ciphertext, asWA(k), { ...cfg, iv });
        },
      };
    }

    const AES = makeCipher('aes', 8, 16);
    AES.encryptGcm = (message, key, iv, aad) => {
      const r = call('gcm', { direction: 'encrypt', key: bytesToB64(toBytes(asWA(key))), iv: bytesToB64(toBytes(asWA(iv))), aad: aad ? bytesToB64(toBytes(asWA(aad))) : '', data: bytesToB64(toBytes(asWA(message))) });
      return r; // ciphertext followed by the 16-byte tag
    };
    AES.decryptGcm = (ciphertext, key, iv, aad) => call('gcm', { direction: 'decrypt', key: bytesToB64(toBytes(asWA(key))), iv: bytesToB64(toBytes(asWA(iv))), aad: aad ? bytesToB64(toBytes(asWA(aad))) : '', data: bytesToB64(toBytes(typeof ciphertext === 'string' ? Base64.parse(ciphertext) : asWA(ciphertext))) });

    const CryptoJS = {
      lib: { WordArray, CipherParams, Base: Object },
      enc: { Hex, Latin1, Utf8, Utf16: Utf16BE, Utf16BE, Utf16LE, Base64, Base64url },
      format: { OpenSSL, Hex: HexFormat },
      mode,
      pad,
      algo: { MD5: algos.MD5, SHA1: algos.SHA1, SHA256: algos.SHA256, SHA512: algos.SHA512, SHA224: algos.SHA224, SHA384: algos.SHA384, SHA3: algos.SHA3, RIPEMD160: algos.RIPEMD160, AES, DES: makeCipher('des', 2, 8), TripleDES: makeCipher('des3', 6, 8) },
      ...algos,
      HmacMD5: hmac('md5'), HmacSHA1: hmac('sha1'), HmacSHA224: hmac('sha224'), HmacSHA256: hmac('sha256'), HmacSHA384: hmac('sha384'), HmacSHA512: hmac('sha512'), HmacSHA3: noSha3, HmacRIPEMD160: hmac('ripemd160'),
      PBKDF2: (password, salt, cfg = {}) => call('pbkdf2', {
        password: bytesToB64(toBytes(asWA(password))), salt: bytesToB64(toBytes(asWA(salt))), iterations: Math.max(1, Math.min(10000000, cfg.iterations || 250000)),
        keylen: (cfg.keySize || 4) * 4, digest: hasherName(cfg.hasher || algos.SHA256),
      }),
      EvpKDF: (password, salt, cfg = {}) => evpKdf(password, asWA(salt), cfg.keySize || 4, 0, cfg.hasher || algos.MD5, cfg.iterations || 1).key,
      AES,
      DES: makeCipher('des', 2, 8),
      TripleDES: makeCipher('des3', 6, 8),
      kdf: { OpenSSL: { execute: (password, keySize, ivSize, salt) => { const s = salt ? asWA(salt) : WordArray.random(8); const d = evpKdf(password, s, keySize, ivSize); return new CipherParams({ key: d.key, iv: d.iv, salt: s }); } } },
    };
    return CryptoJS;
  }

  return { createCryptoJS, bytesToB64, b64ToBytes };
}));
