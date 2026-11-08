// Voice detector. Pure JavaScript, no browser needed, so it can be tested from the command line
// (tests/run_tests.sh) with synthetic audio.
//
// How it works:
//   - The sound is cut into 10 ms pieces ("hops"); each piece gets a loudness in dBFS (0 = loudest possible).
//   - Speech STARTS when the loudness stays above the threshold for `startMs` (80 ms by default).
//   - Speech ENDS after `silenceMs` of quiet (500 ms for sounds). The quiet test uses a slightly lower
//     "release" level (threshold - 3 dB) so a fading sound is not cut early.
//   - Every start/end time is in seconds on the recording's own clock (samples given to push() / sample rate).
(function (root) {
  'use strict';

  var DEFAULTS = {
    marginDb: 12,        // detection level = room noise + this many dB
    stepDb: 3,           // each "Stop and retry" lowers the level by this much
    maxLevel: 3,         // at most 3 retries (a 4th would sit right at the room-noise level)
    minBaseDb: -70,      // never trust a room-noise reading quieter than this (noise suppression can give near-silence)
    maxBaseDb: -30,      // and never set the level above this (very loud room)
    absMinDb: -80,       // the lowest level any retry can reach
    startMs: 80,
    startStepMs: 20,     // each retry also shortens the "must last" time, so short sounds like /k/ /t/ can be caught
    minStartMs: 30
  };

  function Detector(opts) {
    opts = opts || {};
    this.sr = opts.sampleRate || 48000;
    this.hop = Math.max(1, Math.round(this.sr * (opts.hopMs || 10) / 1000));
    this.hopMs = this.hop * 1000 / this.sr;
    this.startMs = opts.startMs == null ? DEFAULTS.startMs : opts.startMs;
    this.silenceMs = opts.silenceMs == null ? 500 : opts.silenceMs;
    this.thresholdDb = opts.thresholdDb == null ? -45 : opts.thresholdDb;
    this.releaseDb = opts.releaseDb == null ? this.thresholdDb - 3 : opts.releaseDb;
    this.collect = !!opts.collect;   // keep every hop's loudness (used by the quiet check)
    this.hopDb = [];
    this.reset();
  }

  var P = Detector.prototype;

  P.reset = function () {
    this.sumsq = 0; this.peak = 0; this.fill = 0;
    this.hopIndex = 0;               // completed hops
    this.levelDb = -120;             // loudness of the last hop (for the volume bar)
    this.hopDb = [];
    this.cancel();
  };

  // Forget the take in progress (the clock keeps running).
  P.cancel = function () {
    this.state = 0;                  // 0 idle, 1 maybe (loud, not long enough yet), 2 speech
    this.runStartHop = 0; this.runCount = 0; this.lastLoudHop = 0; this.silentCount = 0;
    this.takePeak = 0; this.takeSumsq = 0; this.takeHops = 0;
  };

  P.inTake = function () { return this.state === 2; };
  P.time = function () { return this.hopIndex * this.hop / this.sr; };
  // threshold = level that starts speech; release = lower level that keeps it going (so a fading sound is not cut early)
  P.setThreshold = function (db, releaseDb) { this.thresholdDb = db; this.releaseDb = releaseDb == null ? db - 3 : releaseDb; };
  P.setSilenceMs = function (ms) { this.silenceMs = ms; };
  P.setStartMs = function (ms) { this.startMs = ms; };

  // Give it audio (Float32Array, any block size). Returns a list of events:
  //   { type: 'start', t }                                      speech began at t seconds
  //   { type: 'end', start, end, peakDb, rmsDb }                speech finished
  P.push = function (x) {
    var ev = [];
    var hop = this.hop;
    for (var i = 0; i < x.length; i++) {
      var v = x[i], a = v < 0 ? -v : v;
      this.sumsq += v * v;
      if (a > this.peak) this.peak = a;
      if (++this.fill === hop) {
        this._hop(ev);
        this.fill = 0; this.sumsq = 0; this.peak = 0;
      }
    }
    return ev;
  };

  P._hop = function (ev) {
    var hop = this.hop, h = this.hopIndex++;
    var ms = this.sumsq / hop;
    var db = ms > 1e-12 ? 10 * Math.log10(ms) : -120;
    this.levelDb = db;
    if (this.collect) this.hopDb.push(db);

    var loud = db > this.thresholdDb;
    var keep = db > this.releaseDb;                   // still counts as speech once started
    var needHops = Math.max(1, Math.ceil(this.startMs / this.hopMs - 1e-9));
    var needSilent = Math.max(1, Math.ceil(this.silenceMs / this.hopMs - 1e-9));

    if (this.state === 0) {
      if (loud) {
        this.state = 1; this.runStartHop = h; this.runCount = 1;
        this.takePeak = this.peak; this.takeSumsq = this.sumsq; this.takeHops = 1; this.lastLoudHop = h;
        if (this.runCount >= needHops) this._begin(ev);
      }
    } else if (this.state === 1) {
      if (loud) {
        this.runCount++;
        this.takePeak = Math.max(this.takePeak, this.peak); this.takeSumsq += this.sumsq; this.takeHops++; this.lastLoudHop = h;
        if (this.runCount >= needHops) this._begin(ev);
      } else {
        this.cancel();                                // too short: a click or a bump, not speech
      }
    } else {
      if (keep) {
        this.lastLoudHop = h; this.silentCount = 0;
        this.takePeak = Math.max(this.takePeak, this.peak); this.takeSumsq += this.sumsq; this.takeHops++;
      } else if (++this.silentCount >= needSilent) {
        ev.push(this._endEvent());
        this.cancel();
      }
    }
  };

  P._endEvent = function () {
    var hop = this.hop;
    var peakDb = this.takePeak > 1e-6 ? 20 * Math.log10(this.takePeak) : -120;
    var rmsDb = this.takeSumsq > 0 ? 10 * Math.log10(this.takeSumsq / (this.takeHops * hop)) : -120;
    return {
      type: 'end',
      start: this.runStartHop * hop / this.sr,
      end: (this.lastLoudHop + 1) * hop / this.sr,
      peakDb: peakDb, rmsDb: rmsDb
    };
  };

  // "Done" button: end the take in progress right now instead of waiting for the silence. Returns the 'end' event, or null
  // when no speech has started yet.
  P.forceEnd = function () {
    if (this.state !== 2) return null;
    var ev = this._endEvent();
    this.cancel();
    return ev;
  };

  P._begin = function (ev) {
    this.state = 2; this.silentCount = 0;
    ev.push({ type: 'start', t: this.runStartHop * this.hop / this.sr });
  };

  // ── helpers used by the quiet check ──────────────────────────────────────
  // Room noise = the 90th percentile of the loudness readings (a tick or a cough in the 2 s does not matter much).
  Detector.noiseFloor = function (hopDb) {
    if (!hopDb.length) return -90;
    var s = hopDb.slice().sort(function (a, b) { return a - b; });
    return s[Math.floor(0.9 * (s.length - 1))];
  };
  // Base detection level from the room noise, then lowered 3 dB for every retry.
  Detector.baseThreshold = function (floorDb) {
    return Math.min(DEFAULTS.maxBaseDb, Math.max(DEFAULTS.minBaseDb, floorDb + DEFAULTS.marginDb));
  };
  Detector.thresholdFor = function (floorDb, level) {
    level = Math.max(0, Math.min(DEFAULTS.maxLevel, level || 0));
    return Math.max(DEFAULTS.absMinDb, Detector.baseThreshold(floorDb) - DEFAULTS.stepDb * level);
  };
  // The "keep going" level: 3 dB under the threshold, but never closer than half-way to the room noise,
  // or at high sensitivity the room noise itself would keep a take from ever ending.
  Detector.releaseFor = function (floorDb, thresholdDb) {
    return thresholdDb - Math.min(3, Math.max(0.5, (thresholdDb - floorDb) / 2));
  };
  Detector.startMsFor = function (level) {
    level = Math.max(0, Math.min(DEFAULTS.maxLevel, level || 0));
    return Math.max(DEFAULTS.minStartMs, DEFAULTS.startMs - DEFAULTS.startStepMs * level);
  };
  Detector.DEFAULTS = DEFAULTS;

  root.Detector = Detector;
})(typeof globalThis !== 'undefined' ? globalThis : this);
