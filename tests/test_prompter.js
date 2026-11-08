// Offline test of the teleprompter logic (Redo / Skip / click-to-redo / return / range / retry).
load('web/prompter.js');

var fails = 0, passes = 0;
function ok(cond, msg) { if (cond) { passes++; print('  PASS  ' + msg); } else { fails++; print('  FAIL  ' + msg); } }
function eq(a, b, msg) { ok(a === b, msg + ' (got ' + a + ', want ' + b + ')'); }

function make(n, earlier) {
  var items = [];
  for (var i = 0; i < n; i++) items.push({ key: 'sound:' + (i + 1) + ':v1' });
  return new Prompter(items, earlier || {});
}
function take(t) { return { start: t, end: t + 0.5 }; }

print('normal forward flow');
var p = make(10);
eq(p.pointer, 0, 'starts at the first item');
p.complete(take(1)); p.complete(take(3)); p.complete(take(5));
eq(p.pointer, 3, 'after 3 takes the pointer is on item 4');
eq(p.doneCount(), 3, '3 done');

print('Redo (←) goes back one, records, and comes back');
p.back();
eq(p.pointer, 2, '← moves to item 3');
eq(p.returnTo, 3, 'remembers item 4');
p.complete(take(7));
eq(p.pointer, 3, 'after the redo the pointer is back on item 4');
eq(p.takes['sound:3:v1'].start, 7, 'item 3 got the NEW times');
eq(p.takes['sound:2:v1'].start, 3, 'item 2 untouched');
eq(p.returnTo, null, 'return cleared');

print('click an old item (only this one)');
p.complete(take(9)); p.complete(take(11));                // items 4, 5 done; pointer on 6
eq(p.pointer, 5, 'pointer on item 6');
p.goTo(0, 'one');
eq(p.pointer, 0, 'jumped to item 1');
eq(p.returnTo, 5, 'will return to item 6');
p.complete(take(20));
eq(p.pointer, 5, 'back on item 6 after one take');
eq(p.takes['sound:1:v1'].start, 20, 'item 1 replaced');
eq(p.takes['sound:5:v1'].start, 11, 'item 5 untouched');

print('click two old items in a row keeps the first return point');
p.goTo(1, 'one'); p.goTo(0, 'one');
eq(p.returnTo, 5, 'still returns to item 6');
p.complete(take(30));
eq(p.pointer, 5, 'one take, back on item 6 (item 2 stays as it was)');
eq(p.takes['sound:2:v1'].start, 3, 'item 2 not changed by the first click');

print('Skip');
p = make(10);
p.complete(take(1)); p.skip(); p.complete(take(5));
eq(p.pointer, 3, 'skipped item 2, recorded 3, now on 4');
ok(!p.isDone(1), 'item 2 is left undone');
p.goTo(1, 'one');
eq(p.returnTo, 3, 'click the skipped one: will return to 4');
p.skip();
eq(p.pointer, 3, 'Skip during a redo goes straight back');
eq(p.returnTo, null, 'return cleared');

print('From here');
p = make(10);
for (var i = 0; i < 8; i++) p.complete(take(i));
p.goTo(3, 'from');
eq(p.pointer, 3, 'on item 4');
eq(p.returnTo, null, 'no return point');
p.complete(take(50));
eq(p.pointer, 4, 'carries on forward');
ok(p.takes['sound:5:v1'].start === 4, 'item 5 not replaced until it is recorded again');
p.complete(take(51));
eq(p.takes['sound:5:v1'].start, 51, 'item 5 replaced when recorded');

print('Range');
p = make(30);
for (i = 0; i < 25; i++) p.complete(take(i));
eq(p.pointer, 25, 'on item 26');
p.goTo(19, 'range', 22);
eq(p.pointer, 19, 'range starts at item 20');
p.complete(take(100)); eq(p.pointer, 20, 'item 21 next');
p.complete(take(101)); eq(p.pointer, 21, 'item 22 next');
p.complete(take(102)); eq(p.pointer, 22, 'item 23 next');
p.complete(take(103));
eq(p.pointer, 25, 'after the last item of the range, back on item 26');
eq(p.range, null, 'range cleared');
eq(p.takes['sound:23:v1'].start, 103, 'item 23 replaced');
eq(p.takes['sound:24:v1'].start, 23, 'item 24 untouched');

print('jump forward, and the very end');
p = make(5);
p.goTo(3, 'one');
eq(p.pointer, 3, 'forward jump');
eq(p.returnTo, null, 'no return point for a forward jump');
p.complete(take(1)); p.complete(take(2));
ok(p.finished(), 'finished after the last item');
eq(p.current(), null, 'no current item when finished');
ok(p.complete(take(9)) === null, 'a take after the end is ignored');
p.back();
eq(p.pointer, 4, '← from the end goes to the last item');
p.complete(take(60));
ok(p.finished(), 'returns to the end after the redo');

print('Stop and retry');
p = make(5);
eq(p.level, 0, 'level starts at 0');
eq(p.retry(), 1, 'retry 1'); eq(p.retry(), 2, 'retry 2'); eq(p.retry(), 3, 'retry 3');
eq(p.retry(), 3, 'stops at 3');
p.complete(take(1));
eq(p.level, 0, 'level resets for the next item');
p.retry(); p.skip();
eq(p.level, 0, 'level resets on skip');

print('earlier sessions: items already recorded show as done and the list resumes at the first one not done');
p = make(6, { 'sound:1:v1': take(1), 'sound:2:v1': take(2), 'sound:4:v1': take(4) });
eq(p.pointer, 2, 'resumes at item 3');
eq(p.doneCount(), 3, '3 done from before');
p.complete(take(5));
eq(p.pointer, 4, 'then item 5: item 4 is already done, so it is passed over');

print('forward flow passes over finished items, but From here does not');
p = make(8, { 'sound:3:v1': take(3), 'sound:4:v1': take(4), 'sound:5:v1': take(5) });
p.complete(take(1)); p.complete(take(2));
eq(p.pointer, 5, 'after items 1-2, items 3-5 are passed over');
p.goTo(1, 'from');
p.complete(take(60));
eq(p.pointer, 2, 'From here: goes to the very next item, done or not');
p.goTo(6, 'one');
p.complete(take(61));
eq(p.pointer, 7, 'normal flow again after a normal jump');

print('');
print(passes + ' passed, ' + fails + ' failed');
if (fails) quit(1);
