// node app/test/icon-utils.test.js
'use strict';
const assert = require('assert');
const { pngsToIco, parseIco } = require('../icon-utils');

const png = (n) => Buffer.concat([Buffer.from([0x89, 0x50, 0x4e, 0x47, 0x0d, 0x0a, 0x1a, 0x0a]), Buffer.alloc(n, n % 251)]);
const imgs = [256, 48, 32, 16].map((size) => ({ size, data: png(size * 3) }));
const ico = pngsToIco(imgs);
assert.strictEqual(ico.readUInt16LE(4), 4);
const back = parseIco(ico);
assert.deepStrictEqual(back.map((x) => x.size), [256, 48, 32, 16]);
back.forEach((x, i) => assert.ok(x.data.equals(imgs[i].data), `image ${i} round trip`));
assert.strictEqual(ico.length, 6 + 16 * 4 + imgs.reduce((a, x) => a + x.data.length, 0));
assert.strictEqual(ico.readUInt8(6), 0, '256 is stored as 0');
console.log('icon-utils ok');
