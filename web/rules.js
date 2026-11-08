// Small rules that depend on the options: how long a silence ends a take, and when a take "looks bad" (auto-redo).
// Pure functions, no screen code, tested from the command line (tests/run_tests.sh).
(function (root) {
  'use strict';

  var Rules = {};

  // Silence (ms) that ends a take for this item.
  //   sounds / words: the fixed numbers from the options.
  //   sentences: "auto" = base + extra for every word above `sentence_short_words`, never more than `sentence_silence_max_ms`.
  Rules.silenceMs = function (listName, item, opt) {
    if (listName === 'sentences') {
      var fixed = opt.silence_ms_sentences;
      if (typeof fixed === 'number' && fixed > 0) return fixed;
      var words = item && item.words ? item.words : opt.sentence_short_words;
      var extra = Math.max(0, words - opt.sentence_short_words) * opt.sentence_silence_per_word_ms;
      return Math.min(opt.sentence_silence_max_ms, opt.sentence_silence_base_ms + extra);
    }
    return listName === 'sounds' ? opt.silence_ms_sounds : opt.silence_ms_words;
  };

  // Does this take look bad? take = { start, end, peak_db, rms_db } (seconds, dBFS).
  // Returns null when it is fine, or { code, text } with a short message for Mike.
  Rules.autoRedoReason = function (listName, take, opt) {
    var ms = (take.end - take.start) * 1000;
    if (take.peak_db != null && take.peak_db > opt.auto_redo_clip_db) return { code: 'clip', text: 'Too loud (clipping)' };
    if (ms < opt.auto_redo_min_ms) return { code: 'short', text: 'Too short' };
    var max = listName === 'sounds' ? opt.auto_redo_max_ms_sounds : listName === 'sentences' ? opt.auto_redo_max_ms_sentences : opt.auto_redo_max_ms_words;
    if (ms > max) return { code: 'long', text: 'Too long' };
    if (take.rms_db != null && take.rms_db < opt.auto_redo_min_rms_db) return { code: 'quiet', text: 'Too quiet' };
    return null;
  };

  root.Rules = Rules;
})(typeof globalThis !== 'undefined' ? globalThis : this);
