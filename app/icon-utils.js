// Lynxx desktop: icon helpers without Electron dependencies (unit tested with plain node: app/test/icon-utils.test.js)
'use strict';

// .ico holding PNG images (Windows Vista+): 6 byte header, a 16 byte entry per image, then the PNG data
function pngsToIco(pngs) {
  const header = Buffer.alloc(6);
  header.writeUInt16LE(0, 0);
  header.writeUInt16LE(1, 2); // type: icon
  header.writeUInt16LE(pngs.length, 4);
  const entries = Buffer.alloc(16 * pngs.length);
  let offset = header.length + entries.length;
  pngs.forEach(({ size, data }, i) => {
    const o = i * 16;
    entries.writeUInt8(size >= 256 ? 0 : size, o); // width, 0 means 256
    entries.writeUInt8(size >= 256 ? 0 : size, o + 1); // height
    entries.writeUInt16LE(1, o + 4); // color planes
    entries.writeUInt16LE(32, o + 6); // bits per pixel
    entries.writeUInt32LE(data.length, o + 8);
    entries.writeUInt32LE(offset, o + 12);
    offset += data.length;
  });
  return Buffer.concat([header, entries, ...pngs.map((x) => x.data)]);
}

function parseIco(buf) {
  if (buf.readUInt16LE(0) !== 0 || buf.readUInt16LE(2) !== 1) throw new Error('not an icon');
  const count = buf.readUInt16LE(4);
  return Array.from({ length: count }, (_, i) => {
    const o = 6 + i * 16;
    const size = buf.readUInt8(o) || 256;
    const length = buf.readUInt32LE(o + 8);
    const offset = buf.readUInt32LE(o + 12);
    return { size, data: buf.subarray(offset, offset + length) };
  });
}

module.exports = { pngsToIco, parseIco };
