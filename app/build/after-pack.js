// electron-builder afterPack hook (app/package.json build.afterPack).
// The Electron release zips store world-writable modes (0666 / 0777) and extraction keeps them: an unpacked or installed copy
// must not let other users replace the app's binaries, so group/other write bits are cleared before the targets are built.
'use strict';

const fs = require('node:fs');
const path = require('node:path');

function stripWriteBits(dir) {
  for (const entry of fs.readdirSync(dir, { withFileTypes: true })) {
    if (entry.isSymbolicLink()) continue;
    const p = path.join(dir, entry.name);
    const { mode } = fs.lstatSync(p);
    if (mode & 0o022) fs.chmodSync(p, mode & 0o7755);
    if (entry.isDirectory()) stripWriteBits(p);
  }
}

exports.default = async function afterPack(context) {
  if (context.electronPlatformName === 'win32') return;
  stripWriteBits(context.appOutDir);
};
