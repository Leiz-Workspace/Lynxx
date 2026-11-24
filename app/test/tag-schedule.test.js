// node app/test/tag-schedule.test.js
'use strict';
const assert = require('assert');
const { CRON_BEGIN, CRON_END, shq, hasTagBlock, withoutTagBlock, withTagBlock, scheduleWhen } = require('../tag-schedule');

// quoting: spaces and single quotes survive a POSIX shell
assert.strictEqual(shq("/home/a b/it's"), "'/home/a b/it'\\''s'");

// the user's own entries are kept, before and after, and a second write replaces the block instead of adding one
const mine = '# my jobs\nMAILTO=""\n0 1 * * * /usr/bin/backup\n';
const once = withTagBlock(mine, '9 4 * * 1 cmd-a');
assert.ok(once.startsWith(mine.trimEnd() + '\n'));
assert.ok(hasTagBlock(once));
assert.strictEqual(once.split(CRON_BEGIN).length, 2);
const twice = withTagBlock(`${once}5 2 * * * /usr/bin/after\n`, '9 4 * * * cmd-b');
assert.strictEqual(twice.split(CRON_BEGIN).length, 2);
assert.ok(twice.includes('/usr/bin/after') && twice.includes('/usr/bin/backup') && twice.includes('cmd-b') && !twice.includes('cmd-a'));
// removal gives the user's crontab back exactly
assert.strictEqual(withoutTagBlock(once), mine.trimEnd());
assert.ok(!hasTagBlock(withoutTagBlock(twice)));
assert.ok(!withoutTagBlock(twice).includes(CRON_END));
// an empty crontab gets only the block, and loses it again
assert.strictEqual(withTagBlock('', 'x'), `${CRON_BEGIN}\nx\n${CRON_END}\n`);
assert.strictEqual(withoutTagBlock(withTagBlock('', 'x')), '');

// the time: stable per install, ~04:07-04:56, Monday or every day
const a = scheduleWhen('/home/u/.config/Lynxx', 'weekly');
assert.deepStrictEqual(a, scheduleWhen('/home/u/.config/Lynxx', 'weekly'));
assert.ok(a.minute >= 7 && a.minute <= 56 && a.hour === 4);
assert.match(a.cron, /^\d+ 4 \* \* 1$/);
assert.match(scheduleWhen('/home/u/.config/Lynxx', 'daily').cron, /^\d+ 4 \* \* \*$/);
console.log('tag-schedule ok');
