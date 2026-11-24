// Lynxx desktop: pure helpers of the scheduled tag list update (main.js: tagScheduleStatus / applyTagSchedule).
// No Electron here, so app/test/tag-schedule.test.js runs them with plain node.
'use strict';
const crypto = require('node:crypto');

const CRON_BEGIN = '# BEGIN lynxx-tags (managed by the Lynxx app: Tags A-Z > Danh sách tag)';
const CRON_END = '# END lynxx-tags';

// POSIX shell single quotes
const shq = (s) => `'${String(s).replace(/'/g, `'\\''`)}'`;

function hasTagBlock(text) {
  return String(text).split('\n').some((l) => l.trim() === CRON_BEGIN);
}

// the crontab without Lynxx's block (every other line kept as it is, trailing blank lines dropped)
function withoutTagBlock(text) {
  const out = [];
  let inside = false;
  for (const line of String(text).split('\n')) {
    if (line.trim() === CRON_BEGIN) { inside = true; continue; }
    if (line.trim() === CRON_END) { inside = false; continue; }
    if (!inside) out.push(line);
  }
  return out.join('\n').replace(/\n+$/, '');
}

// the crontab with Lynxx's block (one entry), replacing an older block
function withTagBlock(text, entry) {
  const rest = withoutTagBlock(text);
  return `${rest ? `${rest}\n` : ''}${CRON_BEGIN}\n${entry}\n${CRON_END}\n`;
}

// ~04:xx with a minute that differs between installs (clients do not all hit the sites at :00); weekly: Monday
function scheduleWhen(seed, frequency) {
  const minute = 7 + (parseInt(crypto.createHash('sha256').update(String(seed)).digest('hex').slice(0, 6), 16) % 50);
  return { minute, hour: 4, cron: `${minute} 4 * * ${frequency === 'daily' ? '*' : '1'}`, time: `04:${String(minute).padStart(2, '0')}` };
}

module.exports = { CRON_BEGIN, CRON_END, shq, hasTagBlock, withoutTagBlock, withTagBlock, scheduleWhen };
