// `node --check` on every script of the app (npm run check). A Node script rather than a shell loop: npm runs scripts with
// cmd.exe on Windows, which has no `for f in *.js`.
'use strict';

const fs = require('node:fs');
const path = require('node:path');
const { spawnSync } = require('node:child_process');

const app = path.resolve(__dirname, '..');
const inDir = (dir) => fs.readdirSync(path.join(app, dir)).filter((f) => f.endsWith('.js')).sort().map((f) => path.join(dir, f));
const bundled = fs.readdirSync(path.join(app, 'extensions')).sort().flatMap((ext) => inDir(path.join('extensions', ext, 'src')));
const files = ['main.js', 'preload.js', 'icon-utils.js', ...inDir('renderer'), ...inDir('plugin'), ...bundled];

let failed = 0;
for (const file of files) {
  const r = spawnSync(process.execPath, ['--check', path.join(app, file)], { encoding: 'utf8' });
  if (r.status !== 0) {
    failed += 1;
    process.stderr.write(`${file}\n${r.stderr}\n`);
  }
}
console.log(`${failed ? 'not ok' : 'ok'} - node --check on ${files.length} files${failed ? ` (${failed} failed)` : ''}`);
process.exit(failed ? 1 : 0);
