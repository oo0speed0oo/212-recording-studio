// Teleprompter logic: which item is current, what is done, and how Redo / Skip / click-to-redo move around.
// No screen code in here, so it can be tested from the command line (tests/run_tests.sh).
//
// Rules (from the plan):
//   - Progress follows the item key, never the list position.
//   - Going BACK to redo an item remembers where you were (`returnTo`). After ONE new take the pointer goes back there.
//   - "From here" does not return: it carries on forward from the chosen item and redoes everything after it.
//   - "Range" redoes items from..to, then returns.
//   - Redoing an item replaces only that item's start/end. Nothing else moves.
(function (root) {
  'use strict';

  var MAX_LEVEL = 3;

  // items: [{ key, ... }]   earlier: { key: take }  (takes from earlier recording sessions, not yet cut)
  function Prompter(items, earlier) {
    this.items = items;
    this.n = items.length;
    this.takes = {};
    this.sessionKeys = {};          // keys recorded in THIS session
    var self = this;
    Object.keys(earlier || {}).forEach(function (k) { self.takes[k] = earlier[k]; });
    this.pointer = this.firstUndone();
    this.returnTo = null;
    this.range = null;              // { from, to }
    this.level = 0;                 // "Stop and retry" steps used on the current item
    this.stayAfterRedo = false;     // option for later: stay on the redone item instead of returning
    this.sequential = false;        // "From here": go item by item, even over ones already done
  }

  var P = Prompter.prototype;

  P.isDone = function (i) { return !!this.takes[this.items[i].key]; };
  P.doneCount = function () { var c = 0; for (var i = 0; i < this.n; i++) if (this.isDone(i)) c++; return c; };
  P.firstUndone = function () {
    for (var i = 0; i < this.n; i++) if (!this.isDone(i)) return i;
    return this.n;
  };
  P.finished = function () { return this.pointer >= this.n; };
  P.current = function () { return this.pointer < this.n ? this.items[this.pointer] : null; };
  P.redoing = function () { return this.returnTo !== null || this.range !== null; };

  P._set = function (i) {
    i = Math.max(0, Math.min(this.n, i));
    if (i !== this.pointer) this.level = 0;
    this.pointer = i;
  };

  // The pointer moves on (after a take, or Skip).
  P._advance = function () {
    if (this.range) {
      if (this.pointer < this.range.to) { this._set(this.pointer + 1); return; }
      var back = this.returnTo !== null ? this.returnTo : this.pointer + 1;
      this.range = null; this.returnTo = null;
      this._set(back);
    } else if (this.returnTo !== null) {
      var r = this.returnTo; this.returnTo = null;
      this._set(r);
    } else {
      var i = this.pointer + 1;
      if (!this.sequential) while (i < this.n && this.isDone(i)) i++;   // normal flow: next item still to do
      this._set(i);
    }
  };

  // A take was detected (or forced) for the current item. take = { start, end, peakDb, rmsDb, forced, wall }.
  // Returns { index, key } of the item that got the take, or null when there is nothing to record.
  P.complete = function (take) {
    if (this.pointer >= this.n) return null;
    var i = this.pointer, key = this.items[i].key;
    this.takes[key] = take;
    this.sessionKeys[key] = take;
    if (this.stayAfterRedo && this.redoing() && !this.range) {
      this.returnTo = null;           // stay on the redone item
      this.level = 0;
    } else {
      this._advance();
    }
    return { index: i, key: key };
  };

  // Skip (→): leave the item undone and move on. In a redo it goes back to where you were.
  P.skip = function () {
    if (this.pointer >= this.n) return;
    this._advance();
  };

  // Redo (←): go back one item.
  P.back = function () {
    if (this.pointer <= 0) return false;
    this.goTo(this.pointer - 1, 'one');
    return true;
  };

  // Click an item. scope: 'one' (only this one), 'from' (from here onward), 'range' (this item .. `to`).
  P.goTo = function (i, scope, to) {
    if (i < 0 || i >= this.n) return;
    scope = scope || 'one';
    this.sequential = (scope === 'from');
    if (scope === 'from') {
      this.returnTo = null; this.range = null;
    } else if (scope === 'range') {
      var b = Math.max(i, Math.min(this.n - 1, to == null ? i : to));
      var base = this.returnTo !== null ? this.returnTo : this.pointer;
      this.range = { from: i, to: b };
      this.returnTo = i < base ? base : null;
    } else {
      this.range = null;
      if (i < this.pointer) {
        if (this.returnTo === null) this.returnTo = this.pointer;
      } else if (i > this.pointer) {
        if (this.returnTo !== null && i >= this.returnTo) this.returnTo = null;
      }
    }
    this._set(i);
  };

  // "Stop and retry": the detector missed or cut a faint sound. Listen again, more sensitive.
  // The caller throws away the take in progress; this makes the level one step more sensitive.
  P.retry = function () {
    if (this.level < MAX_LEVEL) this.level++;
    return this.level;
  };

  P.snapshot = function () {
    return { pointer: this.pointer, returnTo: this.returnTo, range: this.range ? { from: this.range.from, to: this.range.to } : null, level: this.level };
  };

  Prompter.MAX_LEVEL = MAX_LEVEL;
  root.Prompter = Prompter;
})(typeof globalThis !== 'undefined' ? globalThis : this);
