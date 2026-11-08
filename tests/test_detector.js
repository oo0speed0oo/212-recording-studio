// Offline test of the voice detector with synthetic audio (no microphone).
// Run from the repo root:  tests/run_tests.sh   (uses macOS's built-in "jsc" JavaScript engine)
load('web/detector.js');

var DIR = (typeof arguments !== 'undefined' && arguments[0]) || 'tests/audio';
var fails = 0, passes = 0;
function ok(cond, msg) { if (cond) { passes++; print('  PASS  ' + msg); } else { fails++; print('  FAIL  ' + msg); } }
function near(a, b, tol) { return Math.abs(a - b) <= tol; }

function parseWav(u8) {
  var dv = new DataView(u8.buffer, u8.byteOffset, u8.byteLength);
  var sr = dv.getUint32(24, true), pos = 12, f32 = null;
  while (pos + 8 <= u8.length) {
    var id = String.fromCharCode(u8[pos], u8[pos + 1], u8[pos + 2], u8[pos + 3]);
    var size = dv.getUint32(pos + 4, true);
    if (id === 'data') {
      var n = Math.floor(Math.min(size, u8.length - pos - 8) / 2);
      f32 = new Float32Array(n);
      for (var i = 0; i < n; i++) f32[i] = dv.getInt16(pos + 8 + i * 2, true) / 32768;
      break;
    }
    pos += 8 + size + (size & 1);
  }
  return { sr: sr, x: f32 };
}

function detect(wav, thr, silenceMs, startMs, block, floor) {
  var d = new Detector({ sampleRate: wav.sr, thresholdDb: thr, releaseDb: Detector.releaseFor(floor, thr), silenceMs: silenceMs, startMs: startMs });
  var segs = [], x = wav.x;
  for (var i = 0; i < x.length; i += block) {
    var ev = d.push(x.subarray(i, Math.min(x.length, i + block)));
    for (var k = 0; k < ev.length; k++) if (ev[k].type === 'end') segs.push(ev[k]);
  }
  return segs;
}

function overlap(a, b) { return a.start < b.end && b.start < a.end; }

// ── synthetic.wav ──────────────────────────────────────────────────────────
print('synthetic.wav');
var wav = parseWav(read(DIR + '/synthetic.wav', 'binary'));
var spec = JSON.parse(read(DIR + '/synthetic.json'));
ok(wav.sr === spec.sr, 'sample rate read as ' + wav.sr);

// Quiet check: first 2 s
var q = new Detector({ sampleRate: wav.sr, thresholdDb: 999, collect: true });
q.push(wav.x.subarray(0, Math.round(2 * wav.sr)));
var floor = Detector.noiseFloor(q.hopDb);
ok(near(floor, spec.room_db, 1.5), 'quiet check reads the room noise as ' + floor.toFixed(1) + ' dB (true ' + spec.room_db + ')');
print('        detection levels: ' + [0, 1, 2, 3].map(function (l) { return l + ': ' + Detector.thresholdFor(floor, l).toFixed(1) + ' dB / ' + Detector.startMsFor(l) + ' ms'; }).join('   '));

[0, 1, 2, 3].forEach(function (L) {
  var thr = Detector.thresholdFor(floor, L), startMs = Detector.startMsFor(L);
  var segs = detect(wav, thr, 500, startMs, 480, floor);
  print('  level ' + L + ': threshold ' + thr.toFixed(1) + ' dB, must last ' + startMs + ' ms, found ' + segs.length + ' segments');
  var expectedHits = 0;
  spec.segments.forEach(function (s) {
    var want = s.detected_at[String(L)];
    var hit = null;
    for (var i = 0; i < segs.length; i++) if (overlap(segs[i], s)) { hit = segs[i]; break; }
    if (want === undefined) {                   // level 3: only check there is no crash; pop may trigger by design
      return;
    }
    ok(!!hit === want, 'level ' + L + ': "' + s.name + '" ' + (want ? 'is detected' : 'is ignored'));
    if (want) {
      expectedHits++;
      if (hit) {
        var tol = s.tol + (L > 0 ? 0.03 : 0);
        ok(near(hit.start, s.start, tol), '   start ' + hit.start.toFixed(3) + ' vs true ' + s.start.toFixed(3));
        ok(near(hit.end, s.end, tol), '   end   ' + hit.end.toFixed(3) + ' vs true ' + s.end.toFixed(3));
      }
    }
  });
  if (L <= 2) ok(segs.length === expectedHits, 'level ' + L + ': no extra or split segments (' + segs.length + ' found, ' + expectedHits + ' expected)');
  var stray = segs.filter(function (g) { return !spec.segments.some(function (s) { return overlap(g, s); }); });
  ok(stray.length === 0, 'level ' + L + ': nothing detected in the silence between sounds (' + stray.length + ' strays)');
  var loose = segs.filter(function (g) { return !spec.segments.some(function (s) { return overlap(g, s) && g.start >= s.start - 0.2 && g.end <= s.end + 0.25; }); });
  ok(loose.length === 0, 'level ' + L + ': every take ends close to where its sound ends (' + loose.length + ' too long or merged)');
  var late = segs.filter(function (g) { return g.start >= spec.noise_only_from; });
  ok(late.length === 0, 'level ' + L + ': no false triggers in ' + (spec.noise_only_to - spec.noise_only_from) + ' s of room noise');
});

// Same answer whatever size of audio blocks the browser hands over
var ref = detect(wav, Detector.thresholdFor(floor, 0), 500, 80, 480, floor);
[128, 1000, 2048, 4410, 7].forEach(function (b) {
  var s = detect(wav, Detector.thresholdFor(floor, 0), 500, 80, b, floor);
  var same = s.length === ref.length && s.every(function (g, i) { return near(g.start, ref[i].start, 1e-9) && near(g.end, ref[i].end, 1e-9); });
  ok(same, 'block size ' + b + ' gives the same ' + ref.length + ' segments');
});

// The metrics a take carries (used later for auto-redo)
var c = ref[0];
ok(c.peakDb < 0 && c.peakDb > -30, 'first take peak is ' + c.peakDb.toFixed(1) + ' dBFS');
ok(c.rmsDb < -15 && c.rmsDb > -40, 'first take loudness (RMS) is ' + c.rmsDb.toFixed(1) + ' dBFS');

// Shorter silence rule splits the pair, longer joins it (sounds use 500 ms)
var pair = spec.segments.filter(function (s) { return s.kind === 'pair'; })[0];
var split = detect(wav, Detector.thresholdFor(floor, 0), 200, 80, 480, floor).filter(function (g) { return overlap(g, pair); });
ok(split.length === 2, 'with a 200 ms silence rule the pair splits in two (' + split.length + ')');
var join = detect(wav, Detector.thresholdFor(floor, 0), 700, 80, 480, floor).filter(function (g) { return overlap(g, pair); });
ok(join.length === 1, 'with a 700 ms silence rule the pair is one take (' + join.length + ')');

// cancel() drops a take in progress
var d = new Detector({ sampleRate: wav.sr, thresholdDb: -45, silenceMs: 500 });
var t0 = new Float32Array(wav.sr);          // 1 s of loud tone
for (var i = 0; i < t0.length; i++) t0[i] = 0.1 * Math.sin(i * 0.1);
var ev = d.push(t0);
ok(ev.length === 1 && ev[0].type === 'start' && d.inTake(), 'a long loud sound reports "start" and is in a take');
d.cancel();
ok(!d.inTake(), 'cancel() ends the take in progress');
ev = d.push(new Float32Array(wav.sr));
ok(ev.length === 0, 'after cancel(), silence gives no "end" event');

// ── flow.wav (used by the browser test) ──────────────────────────────────
print('flow.wav');
var fw = parseWav(read(DIR + '/flow.wav', 'binary'));
var fs = JSON.parse(read(DIR + '/flow.json'));
var qf = new Detector({ sampleRate: fw.sr, thresholdDb: 999, collect: true });
qf.push(fw.x.subarray(0, Math.round(2 * fw.sr)));
var ffloor = Detector.noiseFloor(qf.hopDb);
var fseg = detect(fw, Detector.thresholdFor(ffloor, 0), 500, 80, 480, ffloor);
ok(fseg.length === fs.segments.length, 'flow.wav: ' + fseg.length + ' sounds found (' + fs.segments.length + ' expected)');

print('');
print(passes + ' passed, ' + fails + ' failed');
if (fails) quit(1);
