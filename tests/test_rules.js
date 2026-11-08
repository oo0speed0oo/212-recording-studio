// Offline test of the option-dependent rules: sentence silence length and auto-redo.
load('web/rules.js');
var fails = 0, passes = 0;
function ok(c, m) { if (c) { passes++; print('  PASS  ' + m); } else { fails++; print('  FAIL  ' + m); } }
function eq(a, b, m) { ok(a === b, m + ' (got ' + a + ', want ' + b + ')'); }

var opt = {
  silence_ms_sounds: 500, silence_ms_words: 700, silence_ms_sentences: 'auto',
  sentence_silence_base_ms: 1000, sentence_silence_per_word_ms: 80, sentence_silence_max_ms: 2000, sentence_short_words: 6,
  auto_redo_min_ms: 60, auto_redo_max_ms_sounds: 3000, auto_redo_max_ms_words: 3000, auto_redo_max_ms_sentences: 12000,
  auto_redo_min_rms_db: -50, auto_redo_clip_db: -0.3
};

print('silence length');
eq(Rules.silenceMs('sounds', null, opt), 500, 'sounds 500 ms');
eq(Rules.silenceMs('words', null, opt), 700, 'challenge words 700 ms');
eq(Rules.silenceMs('common', null, opt), 700, 'common words 700 ms');
eq(Rules.silenceMs('sentences', { words: 4 }, opt), 1000, 'short sentence (4 words) 1000 ms');
eq(Rules.silenceMs('sentences', { words: 6 }, opt), 1000, '6 words still 1000 ms');
eq(Rules.silenceMs('sentences', { words: 7 }, opt), 1080, '7 words 1080 ms');
eq(Rules.silenceMs('sentences', { words: 10 }, opt), 1320, '10 words 1320 ms');
eq(Rules.silenceMs('sentences', { words: 40 }, opt), 2000, 'a very long sentence is capped at 2000 ms');
eq(Rules.silenceMs('sentences', null, opt), 1000, 'no word count: the short-sentence value');
opt.silence_ms_sentences = 1500;
eq(Rules.silenceMs('sentences', { words: 4 }, opt), 1500, 'a fixed number in the options wins over auto');
opt.silence_ms_sentences = 'auto';

print('auto-redo');
function take(ms, peak, rms) { return { start: 1, end: 1 + ms / 1000, peak_db: peak, rms_db: rms }; }
eq(Rules.autoRedoReason('sounds', take(400, -10, -25), opt), null, 'a normal take is fine');
eq(Rules.autoRedoReason('sounds', take(40, -10, -25), opt).code, 'short', '40 ms is too short');
eq(Rules.autoRedoReason('sounds', take(60, -10, -25), opt), null, 'exactly 60 ms is fine');
eq(Rules.autoRedoReason('sounds', take(3500, -10, -25), opt).code, 'long', '3.5 s is too long for a sound');
eq(Rules.autoRedoReason('sounds', take(2900, -10, -25), opt), null, '2.9 s is fine for a long /s/');
eq(Rules.autoRedoReason('words', take(3500, -10, -25), opt).code, 'long', '3.5 s is too long for a word');
eq(Rules.autoRedoReason('sentences', take(3500, -10, -25), opt), null, '3.5 s is fine for a sentence');
eq(Rules.autoRedoReason('sentences', take(13000, -10, -25), opt).code, 'long', '13 s is too long for a sentence');
eq(Rules.autoRedoReason('sounds', take(400, -10, -52), opt).code, 'quiet', 'RMS -52 dB is too quiet');
eq(Rules.autoRedoReason('sounds', take(400, -10, -49), opt), null, 'RMS -49 dB is fine');
eq(Rules.autoRedoReason('sounds', take(400, -0.1, -20), opt).code, 'clip', 'peak -0.1 dB is clipping');
eq(Rules.autoRedoReason('sounds', take(400, -0.5, -20), opt), null, 'peak -0.5 dB is fine');
eq(Rules.autoRedoReason('sounds', take(40, -0.1, -60), opt).code, 'clip', 'clipping is reported first');
eq(Rules.autoRedoReason('sounds', { start: 1, end: 1.4, peak_db: null, rms_db: null }, opt), null, 'a take without measurements (hold-to-record) is not judged');

print('');
print(passes + ' passed, ' + fails + ' failed');
if (fails) quit(1);
