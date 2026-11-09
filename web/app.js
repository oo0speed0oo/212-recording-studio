// 212 Recording Studio: screens, microphone, detection, controls, autosave.
// The "brains" live in detector.js (finds speech) and prompter.js (which item is next), both tested from the command line.
(function () {
  'use strict';

  var $ = function (id) { return document.getElementById(id); };
  var qs = new URLSearchParams(location.search);
  // Test hooks (used by the automatic tests, not by Mike): ?testaudio=flow.wav plays a WAV file instead of the microphone,
  // &list=sounds opens that list straight away, &autostart=1 starts recording as soon as the quiet check is done.
  var TEST = { audio: qs.get('testaudio'), list: qs.get('list'), autostart: qs.get('autostart') === '1' };

  var SAVE_EVERY_MS = (Number(qs.get('saveevery')) || 30) * 1000;   // autosave every 30 s (&saveevery=5 is a test hook)
  var HOLD_TAIL_S = 0.15;      // hold-to-record: keep a little after the key is released (the browser delivers audio slightly late)
  var HOLD_MIN_S = 0.10;       // a shorter tap is ignored

  var S = {
    screen: 'list', config: null, list: null, prompter: null, detector: null, engine: null,
    ns: true, deviceId: null, sr: 48000, floorDb: -60,
    mode: 'idle',               // idle | check | record
    measure: null, paused: false, hold: null,
    sessionId: null, rec: 0, sent: 0, pcm: [], flushing: false, saveOk: true, timer: null,
    sessionItems: {}, events: [], todayBase: {}, offsetSamples: 0,
    meterDb: -120, lastTake: null, statusTimer: null,
    opt: null, spec: null,                       // the options (settings.json) and what each may be
    recent: [], playback: false, playSrc: null,  // the last 20 s of audio, for Careful mode playback
    autoTries: {}, beatTimer: null
  };

  // ── small helpers ───────────────────────────────────────────────────────
  function esc(s) { return String(s).replace(/[&<>"']/g, function (c) { return { '&': '&amp;', '<': '&lt;', '>': '&gt;', '"': '&quot;', "'": '&#39;' }[c]; }); }
  function db1(x) { return (Math.round(x * 10) / 10).toFixed(1).replace('-', '−'); }
  function now() { return Date.now() / 1000; }
  function recTime() { return S.rec / S.sr; }

  // ── options: the server keeps them in settings.json ─────────────────────
  function applyOptions(o) {
    S.opt = o;
    Detector.DEFAULTS.marginDb = o.sensitivity_margin_db;
    Detector.DEFAULTS.stepDb = o.retry_step_db;
    if (S.prompter) S.prompter.stayAfterRedo = o.after_redo === 'stay';
    if (S.config) S.config.mic_tip = o.mic_tip;
    if ($('mic-tip')) $('mic-tip').textContent = o.mic_tip;
    if (S.mode === 'record' && S.detector) applyLevel();
  }
  function loadOptions() {
    return api('GET', '/api/settings').then(function (r) { S.spec = r.spec; S.ns = r.values.noise_suppression; applyOptions(r.values); });
  }
  function saveOptions(values) {
    return api('POST', '/api/settings', { values: values }).then(function (r) { S.spec = r.spec; applyOptions(r.values); return r.values; });
  }

  function api(method, url, body, raw) {
    var opts = { method: method, headers: {} };
    if (body !== undefined) {
      if (raw) { opts.body = body; } else { opts.body = JSON.stringify(body); opts.headers['Content-Type'] = 'application/json'; }
    }
    return fetch(url, opts).then(function (r) {
      return r.json().catch(function () { return {}; }).then(function (j) {
        if (!r.ok) throw new Error(j.error || ('Error ' + r.status));
        return j;
      });
    });
  }

  function show(name) {
    S.screen = name;
    ['list', 'check', 'prompt', 'review', 'site'].forEach(function (n) { $('s-' + n).classList.toggle('hidden', n !== name); });
  }

  // ── audio engine: microphone (or a test WAV) -> AudioWorklet -> blocks of raw samples ──────────
  function createEngine() {
    var Ctx = window.AudioContext || window.webkitAudioContext;
    var ctx = new Ctx();
    var E = { ctx: ctx, node: null, stream: null, source: null, ready: null, onblock: null, delivered: 0, testStarted: false, testEnded: false };
    // Browsers keep audio paused until the page has been clicked or a key pressed once.
    ['click', 'keydown', 'pointerdown'].forEach(function (ev) {
      document.addEventListener(ev, function () { if (ctx.state === 'suspended') ctx.resume(); }, true);
    });

    E.ready = ctx.audioWorklet.addModule('worklet.js').then(function () {
      E.node = new AudioWorkletNode(ctx, 'capture', { numberOfInputs: 1, numberOfOutputs: 1, channelCount: 1, channelCountMode: 'explicit' });
      var mute = ctx.createGain();
      mute.gain.value = 0;                       // never play the microphone back through the speakers
      E.node.connect(mute); mute.connect(ctx.destination);
      E.node.port.onmessage = function (e) {
        E.delivered += e.data.length;
        if (E.onblock) E.onblock(e.data);
      };
    });

    function stopTracks() {
      if (E.source) { try { E.source.disconnect(); } catch (e) {} E.source = null; }
      if (E.stream) { E.stream.getTracks().forEach(function (t) { t.stop(); }); E.stream = null; }
    }

    function getStream(deviceId, ns) {
      var audio = { echoCancellation: false, autoGainControl: false, noiseSuppression: !!ns, channelCount: 1 };
      if (deviceId) audio.deviceId = { exact: deviceId };
      return navigator.mediaDevices.getUserMedia({ audio: audio }).catch(function (err) {
        if (deviceId && err && (err.name === 'OverconstrainedError' || err.name === 'NotFoundError')) {
          delete audio.deviceId;                 // that microphone is gone: use the default one
          return navigator.mediaDevices.getUserMedia({ audio: audio });
        }
        throw err;
      });
    }

    // Open (or re-open) the microphone. Used again when noise suppression is switched or another mic is picked.
    E.openMic = function (deviceId, ns) {
      return E.ready.then(function () {
        if (ctx.state === 'suspended') ctx.resume();
        return getStream(deviceId, ns);
      }).then(function (stream) {
        stopTracks();
        E.stream = stream;
        E.source = ctx.createMediaStreamSource(stream);
        E.source.connect(E.node);
        return stream;
      });
    };

    // Test mode: play a WAV file from tests/audio into the same pipeline instead of the microphone.
    E.openTest = function (name) {
      return E.ready.then(function () {
        if (ctx.state === 'suspended') ctx.resume();
        return fetch('/testaudio/' + encodeURIComponent(name));
      }).then(function (r) {
        if (!r.ok) throw new Error('Test audio not found: ' + name);
        return r.arrayBuffer();
      }).then(function (buf) { return ctx.decodeAudioData(buf); }).then(function (audio) {
        stopTracks();
        var src = ctx.createBufferSource();
        src.buffer = audio;
        src.connect(E.node);
        src.onended = function () { E.testEnded = true; };
        E.source = src; E.testStarted = true;
        src.start();
      });
    };

    E.release = function () { stopTracks(); };
    return E;
  }

  function friendlyMicError(err) {
    var n = err && err.name;
    if (n === 'NotAllowedError' || n === 'SecurityError') {
      return 'The microphone is blocked. Click the lock or camera icon in the address bar and allow the microphone. '
        + 'If it still fails: Apple menu → System Settings → Privacy & Security → Microphone → switch your browser on. Then press "Check again".';
    }
    if (n === 'NotFoundError') return 'No microphone found. Plug in the Blue microphone (or pick another one above) and press "Check again".';
    if (n === 'NotReadableError') return 'Another program is using the microphone. Close it and press "Check again".';
    return 'The microphone would not start: ' + ((err && err.message) || err);
  }

  function ensureEngine() {
    if (!S.engine) S.engine = createEngine();           // made inside the click, so the browser lets it run
    else if (S.engine.ctx.state === 'suspended') S.engine.ctx.resume();
    return S.engine;
  }

  function startInput() {
    var E = ensureEngine();
    E.onblock = onBlock;
    var p = TEST.audio ? (E.testStarted ? Promise.resolve() : E.openTest(TEST.audio)) : E.openMic(S.deviceId, S.ns);
    return p.then(function () { S.sr = E.ctx.sampleRate; });
  }

  function fillDevices() {
    if (TEST.audio || !navigator.mediaDevices.enumerateDevices) return;
    navigator.mediaDevices.enumerateDevices().then(function (list) {
      var sel = $('mic-select'), cur = S.engine && S.engine.stream && S.engine.stream.getAudioTracks()[0];
      var curId = cur && cur.getSettings && cur.getSettings().deviceId;
      sel.innerHTML = '';
      list.filter(function (d) { return d.kind === 'audioinput'; }).forEach(function (d, i) {
        var o = document.createElement('option');
        o.value = d.deviceId; o.textContent = d.label || ('Microphone ' + (i + 1));
        if (d.deviceId === curId) o.selected = true;
        sel.appendChild(o);
      });
    }).catch(function () {});
  }

  // ── level meter and room measuring ──────────────────────────────────────
  function meter(x) {
    var s = 0;
    for (var i = 0; i < x.length; i++) s += x[i] * x[i];
    var db = s > 0 ? 10 * Math.log10(s / x.length) : -120;
    S.meterDb = db > S.meterDb ? db : Math.max(db, S.meterDb - 4);   // quick up, slower down
  }
  function drawMeter() {
    var pct = Math.max(0, Math.min(100, (S.meterDb + 80) / 80 * 100));
    var fill = $('meter-fill');
    if (fill) { fill.style.width = pct + '%'; }
    var f2 = $('check-meter-fill');
    if (f2) f2.style.width = pct + '%';
    $('s-prompt').querySelector('.meter').classList.toggle('hot', S.meterDb > -3);
    requestAnimationFrame(drawMeter);
  }

  // Measure the room noise for `seconds`. Resolves with the room noise in dBFS.
  function measureRoom(seconds, onProgress) {
    return new Promise(function (resolve) {
      S.measure = {
        det: new Detector({ sampleRate: S.sr, thresholdDb: 999, collect: true }),
        samples: 0, need: Math.round(seconds * S.sr), onProgress: onProgress,
        resolve: function () { var f = Detector.noiseFloor(S.measure.det.hopDb); S.measure = null; resolve(f); }
      };
    });
  }
  function feedMeasure(x) {
    var M = S.measure;
    if (!M) return;
    M.det.push(x);
    M.samples += x.length;
    if (M.onProgress) M.onProgress(Math.min(1, M.samples / M.need));
    if (M.samples >= M.need) M.resolve();
  }

  // ── block handler: the heart of recording ───────────────────────────────
  function onBlock(x) {
    meter(x);
    if (S.mode === 'check') { if (S.measure) feedMeasure(x); return; }
    if (S.mode !== 'record' || S.paused || S.playback) return;

    // keep the raw take: 16-bit samples, queued for the 30-second autosave
    var pcm = new Int16Array(x.length);
    for (var i = 0; i < x.length; i++) { var v = x[i]; pcm[i] = v >= 1 ? 32767 : v <= -1 ? -32768 : Math.round(v * 32767); }
    S.pcm.push(pcm);
    S.recent.push({ pos: S.rec, data: pcm });         // the last 20 s stay in memory for Careful mode
    S.rec += x.length;
    while (S.recent.length > 1 && S.rec - (S.recent[0].pos + S.recent[0].data.length) > 20 * S.sr) S.recent.shift();

    var events = S.detector.push(x);                 // always runs, so its clock stays in step with the recording
    if (S.measure) { feedMeasure(x); S.detector.cancel(); return; }
    if (S.hold) { S.detector.cancel(); return; }     // hold-to-record is in charge: ignore the detector
    if (S.prompter.finished()) { S.detector.cancel(); return; }
    for (var k = 0; k < events.length; k++) {
      var ev = events[k];
      if (ev.type === 'start') setStatus('hear', 'Hearing you…');
      else if (ev.type === 'end') handleEnd(ev);
    }
  }

  // The detector found the end of a take (or Done was pressed). Auto-redo may refuse it.
  function handleEnd(ev) {
    var cur = S.prompter.current();
    if (!cur) return;
    var t = { start: ev.start, end: ev.end, peak_db: ev.peakDb, rms_db: ev.rmsDb, forced: false };
    var why = S.opt.auto_redo ? Rules.autoRedoReason(S.list.list, t, S.opt) : null;
    if (why) {
      var tries = S.autoTries[cur.key] || 0;
      if (tries < S.opt.auto_redo_max_tries) {                 // not marked done: ask for it again
        S.autoTries[cur.key] = tries + 1;
        logEvent('auto_redo', { key: cur.key, why: why.code, ms: Math.round((t.end - t.start) * 1000), rms_db: Math.round(t.rms_db * 10) / 10, peak_db: Math.round(t.peak_db * 10) / 10 });
        updateLast(t);
        setStatus('warn', why.text + ': say it again');
        var row = $('plist').children[S.prompter.pointer];
        if (row) { row.classList.remove('bad'); void row.offsetWidth; row.classList.add('bad'); }
        saveTimes();
        return;
      }
      t.flag = why.code;                                       // asked enough times: keep this one, but remember the doubt
    }
    takeDone(t);
  }

  var FLAG_TEXT = { clip: 'too loud', short: 'very short', long: 'very long', quiet: 'very quiet' };

  // A take was found (by the detector or by hold-to-record) for the current item.
  function takeDone(t) {
    t.wall = now();
    t.level = S.prompter.level;
    var r = S.prompter.complete(t);
    if (!r) return;
    delete S.autoTries[r.key];
    S.sessionItems[r.key] = t;
    S.lastTake = t;
    logEvent(t.forced ? 'hold' : 'take', { key: r.key, start: t.start, end: t.end, flag: t.flag });
    S.detector.cancel();
    applyLevel();
    saveTimes();
    renderList(r.index);
    flashStatus(t.flag ? 'warn' : 'got', t.flag ? 'Kept (' + FLAG_TEXT[t.flag] + ')' : 'Got it ✓');
    updateLast(t);
    updateTop();
    updateControls();
    scrollToCurrent();
    if (S.opt.mode === 'careful') playback(t, afterTake); else afterTake();
  }
  function afterTake() { if (S.opt.countdown && S.mode === 'record' && !S.prompter.finished()) beat(); }

  function updateLast(t) {
    var el = $('lasttake');
    if (!el) return;
    el.textContent = 'Last: ' + (t.end - t.start).toFixed(2) + ' s' + (t.rms_db != null ? ', ' + db1(t.rms_db) + ' dB' : '') + (t.flag ? ' (' + FLAG_TEXT[t.flag] + ')' : '');
  }

  // Careful mode: play the take back right after it is found. The recording clock stops while it plays,
  // so the playback is never recorded.
  function playback(t, then) {
    var E = S.engine, sr = S.sr;
    var a = Math.max(0, Math.floor((t.start - 0.15) * sr)), b = Math.min(S.rec, Math.ceil((t.end + 0.15) * sr));
    var x = new Float32Array(Math.max(0, b - a));
    S.recent.forEach(function (c) {
      var s0 = Math.max(a, c.pos), s1 = Math.min(b, c.pos + c.data.length);
      for (var i = s0; i < s1; i++) x[i - a] = c.data[i - c.pos] / 32768;
    });
    if (!E || x.length < sr * 0.05) { then(); return; }
    S.playback = true;
    S.detector.cancel();
    setStatus('play', 'Playing back…');
    var buf = E.ctx.createBuffer(1, x.length, sr);
    buf.copyToChannel(x, 0);
    var src = E.ctx.createBufferSource();
    src.buffer = buf;
    src.connect(E.ctx.destination);
    S.playSrc = src;
    src.onended = function () {
      if (S.playSrc !== src) return;                           // stopped early by a button: already handled
      S.playSrc = null;
      setTimeout(function () {                                 // let the room go quiet before listening again
        if (S.playSrc || !S.playback) return;
        S.playback = false;
        if (S.detector) S.detector.cancel();
        restoreStatus();
        then();
      }, 350);
    };
    src.start();
  }
  function endPlayback() {
    if (!S.playback) return;
    var src = S.playSrc; S.playSrc = null; S.playback = false;
    if (src) { try { src.stop(); } catch (e) {} }
    if (S.detector) S.detector.cancel();
  }

  // Countdown beat (option): the next item blinks for about a second before you speak.
  function beat() {
    var row = $('plist').children[S.prompter.pointer];
    if (!row) return;
    var ms = S.opt.countdown_ms;
    row.style.animationDuration = Math.round(ms / 3) + 'ms';
    row.classList.remove('beat'); void row.offsetWidth; row.classList.add('beat');
    setStatus('ready', 'Get ready…');
    clearTimeout(S.beatTimer);
    S.beatTimer = setTimeout(function () {
      row.classList.remove('beat');
      if (S.mode === 'record' && !S.paused && !S.hold && !S.playback) restoreStatus();
    }, ms);
  }

  function logEvent(type, extra) {
    var e = { t: Math.round(recTime() * 1000) / 1000, type: type };
    if (extra) for (var k in extra) e[k] = extra[k];
    S.events.push(e);
  }

  function applyLevel() {
    if (!S.detector) return;
    var L = S.prompter.level;
    var thr = Detector.thresholdFor(S.floorDb, L);
    S.detector.setThreshold(thr, Detector.releaseFor(S.floorDb, thr));
    S.detector.setStartMs(Detector.startMsFor(L));
    S.detector.setSilenceMs(Rules.silenceMs(S.list.list, S.prompter.current(), S.opt));
    var pct = Math.max(0, Math.min(100, (thr + 80) / 80 * 100));
    $('meter-thr').style.left = pct + '%';
    $('level-note').textContent = L === 0 ? 'Detection level ' + db1(thr) + ' dB'
      : 'More sensitive (step ' + L + ' of ' + Prompter.MAX_LEVEL + '): level ' + db1(thr) + ' dB, sound must last ' + Detector.startMsFor(L) + ' ms';
  }

  // ── status chip ─────────────────────────────────────────────────────────
  function setStatus(kind, text) {
    var el = $('status');
    el.className = 'status s-' + kind;
    el.textContent = text;
  }
  function baseStatus() {
    if (S.playback) return ['play', 'Playing back…'];
    if (S.paused) return ['pause', 'Paused'];
    if (S.hold) return ['hold', 'Recording (holding)…'];
    if (S.prompter && S.prompter.finished()) return ['got', 'All done: press Finish'];
    return ['listen', 'Listening…'];
  }
  function restoreStatus() { var b = baseStatus(); setStatus(b[0], b[1]); }
  function flashStatus(kind, text, after) {
    setStatus(kind, text);
    clearTimeout(S.statusTimer);
    S.statusTimer = setTimeout(function () { restoreStatus(); }, 900);
  }

  // ── list screen ─────────────────────────────────────────────────────────
  function loadListScreen(message) {
    show('list');
    $('msg-list').textContent = message || '';
    S.mode = 'idle';
    return Promise.all([api('GET', '/api/config'), api('GET', '/api/lists')]).then(function (r) {
      S.config = r[0];
      var warn = $('warn');
      if (!S.config.website_ok || S.config.settings_error) {
        warn.classList.remove('hidden');
        warn.textContent = S.config.settings_error ||
          'I cannot find the website\'s CSV lists in ' + S.config.website_folder + '. Copy settings.example.json to settings.json and put the right folder in it.';
      } else warn.classList.add('hidden');
      var box = $('cards');
      box.innerHTML = '';
      var waiting = r[1].lists.filter(function (c) { return c.pending > 0; })[0];
      $('pending').classList.toggle('hidden', !waiting);
      var tocheck = r[1].lists.filter(function (c) { return c.unchecked > 0; })[0];
      $('tocheck').classList.toggle('hidden', !tocheck || !!waiting);
      if (tocheck) {
        $('tocheck-text').textContent = tocheck.unchecked + ' file' + (tocheck.unchecked === 1 ? ' is' : 's are') + ' waiting for you to check (' + tocheck.title + ').';
        $('btn-check-files').onclick = function () { showReview(null, tocheck.id); };
      }
      if (waiting) {
        $('pending-text').textContent = waiting.pending + ' recorded item' + (waiting.pending === 1 ? ' is' : 's are') + ' waiting to be cut into files (' + waiting.title + ').';
        $('btn-cut-now').onclick = function () { runProcess(waiting.id, false); };
      }
      r[1].lists.forEach(function (c) {
        var b = document.createElement('button');
        b.type = 'button'; b.className = 'card'; b.disabled = !c.ready;
        var count = c.ready ? (c.done + ' / ' + (c.total == null ? '?' : c.total) + ' done') : (c.total != null ? c.total + ' items' : '');
        var redoNote = c.ready && c.redo ? ' <span class="c-redo">· ' + c.redo + ' to redo</span>' : '';
        var pct = c.ready && c.total ? Math.round(100 * c.done / c.total) : 0;
        b.innerHTML = '<div class="c-title">' + esc(c.title) + '</div><div class="c-note">' + esc(c.note) + '</div>'
          + '<div class="c-count">' + esc(count) + redoNote + '</div><div class="prog"><i style="width:' + pct + '%"></i></div>';
        if (c.ready) b.addEventListener('click', function () { openList(c.id); });
        box.appendChild(b);
      });
      var sel = $('tools-list'), keep = sel.value;
      sel.innerHTML = r[1].lists.filter(function (c) { return c.ready; }).map(function (c) { return '<option value="' + c.id + '">' + esc(c.title) + '</option>'; }).join('');
      if (keep) sel.value = keep;
      siteHint();
    }).catch(function (e) {
      $('warn').classList.remove('hidden');
      $('warn').textContent = 'The studio is not answering (' + e.message + '). Is the Terminal window still running studio.py?';
    });
  }

  function openList(name) {
    ensureEngine();                                  // inside the click
    show('check');
    $('btn-start').disabled = true;
    $('check-error').classList.add('hidden');
    $('check-title').textContent = 'Starting the microphone…';
    $('check-result').textContent = '';
    $('check-bar').style.width = '0%';
    $('mic-tip').textContent = (S.opt && S.opt.mic_tip) || '';
    $('ns-check').checked = S.ns;
    return api('GET', '/api/list/' + name).then(function (data) {
      S.list = data;
      var earlier = {};
      Object.keys(data.done || {}).forEach(function (k) { earlier[k] = { done: true }; });
      Object.keys(data.recorded || {}).forEach(function (k) { earlier[k] = data.recorded[k]; });
      S.prompter = new Prompter(data.items, earlier);
      S.prompter.stayAfterRedo = S.opt.after_redo === 'stay';
      S.autoTries = {};
      S.todayBase = {};
      (data.today || []).forEach(function (k) { S.todayBase[k] = true; });
      S.sessionItems = {}; S.events = [];
      S.redoKeys = {};
      (data.redo || []).forEach(function (k) { S.redoKeys[k] = true; });
      return runCheck();
    }).catch(function (e) {
      showCheckError(e.message);
    });
  }

  // ── quiet check screen ──────────────────────────────────────────────────
  function showCheckError(msg) {
    $('check-title').textContent = 'Cannot start';
    var el = $('check-error');
    el.textContent = msg;
    el.classList.remove('hidden');
  }

  function runCheck() {
    S.mode = 'check';
    $('btn-start').disabled = true;
    $('check-error').classList.add('hidden');
    $('check-title').textContent = 'Starting the microphone…';
    return startInput().then(function () {
      fillDevices();
      $('check-title').textContent = 'Stay quiet…';
      $('check-result').textContent = 'Measuring the room for 2 seconds.';
      return measureRoom(2, function (p) { $('check-bar').style.width = Math.round(p * 100) + '%'; });
    }).then(function (floor) {
      S.floorDb = floor;
      var thr = Detector.thresholdFor(floor, 0);
      $('check-title').textContent = 'Ready';
      var msg = 'Room noise <b>' + db1(floor) + ' dB</b>. Your voice must rise above <b>' + db1(thr) + ' dB</b> to count.';
      if (floor > -40) msg += ' <br>The room is quite loud. Turn the mic gain down, or move closer and speak softer.';
      $('check-result').innerHTML = msg;
      $('btn-start').disabled = false;
      if (TEST.autostart) return startRecording();
    }).catch(function (err) {
      showCheckError(friendlyMicError(err));
    });
  }

  // ── recording session ───────────────────────────────────────────────────
  function micMeta() {
    var E = S.engine, track = E && E.stream && E.stream.getAudioTracks()[0];
    var meta = { noise_suppression: S.ns, echo_cancellation: false, auto_gain_control: false, device: track ? track.label : (TEST.audio ? 'test file ' + TEST.audio : ''),
      sample_rate: S.sr, room_noise_db: Math.round(S.floorDb * 10) / 10 };
    if (TEST.audio) meta.test_offset_seconds = S.offsetSamples / S.sr;
    return meta;
  }

  function startRecording() {
    $('btn-start').disabled = true;
    return api('POST', '/api/session/new', { list: S.list.list, sample_rate: S.sr, mic: { noise_suppression: S.ns } }).then(function (res) {
      S.sessionId = res.id;
      S.rec = 0; S.sent = 0; S.pcm = []; S.paused = false; S.hold = null; S.lastTake = null;
      S.offsetSamples = S.engine.delivered;
      S.detector = new Detector({ sampleRate: S.sr });
      S.recent = []; S.playback = false; S.playSrc = null; S.autoTries = {};
      S.mode = 'record';
      S.saveOk = true;
      $('p-title').textContent = S.list.title;
      $('btn-pause').innerHTML = '<kbd>Space</kbd> Pause';
      var sent = S.list.kind === 'sentences';          // sentences: Enter = Done, and the hold-to-record key is H
      $('btn-done').classList.toggle('hidden', !sent);
      $('btn-hold').innerHTML = '<kbd>' + (sent ? 'H' : 'Enter') + '</kbd> Hold to record';
      $('p-save').textContent = '';
      $('lasttake').textContent = '';
      applyLevel();
      renderList(-1, true);
      updateNs(); updateTop(); updateControls(); restoreStatus();
      show('prompt');
      scrollToCurrent(true);
      clearInterval(S.timer);
      S.timer = setInterval(function () { flush(); }, SAVE_EVERY_MS);
      saveTimes();
    }).catch(function (e) { showCheckError(e.message); $('btn-start').disabled = false; });
  }

  // Autosave: send the new raw audio (and the times) to the studio, which appends it to takes/<session>.wav
  function flush() {
    if (S.flushing || !S.sessionId) return Promise.resolve();
    var chunks = S.pcm;
    if (!chunks.length) { return saveTimes(); }
    S.pcm = [];
    var n = 0; chunks.forEach(function (c) { n += c.length; });
    var all = new Int16Array(n), o = 0;
    chunks.forEach(function (c) { all.set(c, o); o += c.length; });
    S.flushing = true;
    return api('POST', '/api/session/' + S.sessionId + '/audio?offset=' + S.sent, all.buffer, true).then(function (r) {
      S.sent = r.samples;
      S.saveOk = true;
      $('p-save').textContent = 'Saved ' + new Date().toLocaleTimeString([], { hour: '2-digit', minute: '2-digit', second: '2-digit' });
      $('p-save').style.color = '';
    }).catch(function (e) {
      S.pcm = chunks.concat(S.pcm);                  // keep it, try again next time
      S.saveOk = false;
      $('p-save').textContent = '⚠ Not saved: ' + e.message + ' (will retry)';
      $('p-save').style.color = 'var(--amber)';
    }).then(function () { S.flushing = false; return saveTimes(); });
  }

  function saveTimes() {
    if (!S.sessionId) return Promise.resolve();
    return api('POST', '/api/session/' + S.sessionId + '/times', { items: S.sessionItems, events: S.events, mic: micMeta() }).catch(function () {});
  }

  function waitIdle() {
    return new Promise(function (resolve) {
      (function check() { if (!S.flushing) resolve(); else setTimeout(check, 40); })();
    });
  }

  function finish() {
    if (S.mode !== 'record') return;
    endPlayback();
    if (S.hold) holdEnd();
    S.mode = 'idle';
    clearInterval(S.timer);
    var count = Object.keys(S.sessionItems).length;
    waitIdle().then(function () { return flush(); }).then(waitIdle).then(function () {
      return api('POST', '/api/session/' + S.sessionId + '/close', {});
    }).catch(function () {}).then(function () {
      if (S.engine) S.engine.release();
      S.sessionId = null;
      return runProcess(S.list.list, false);
    });
  }

  // Cut every unfinished recording into files (or, with rebuild, remake all files from the saved cuts).
  function runProcess(listName, rebuild) {
    $('busy-title').textContent = rebuild ? 'Rebuilding the files…' : 'Cutting and filing…';
    $('busy').classList.remove('hidden');
    return api('POST', '/api/process', { list: listName, rebuild: !!rebuild }).then(function (rep) {
      $('busy').classList.add('hidden');
      showReview(rep, listName);
    }).catch(function (e) {
      $('busy').classList.add('hidden');
      loadListScreen('Could not cut the files: ' + e.message + ' Your recordings are safe in the takes folder. Try "Cut and file them now".');
    });
  }

  // ── check screen: play every file, OK or Redo ───────────────────────────
  var R = { files: [], sticky: {}, seq: 0, resolveCur: null, playing: null, outDir: '' };
  var player = new Audio();                                 // one audio element for everything (Safari only lets a "touched" one play)

  function kb(n) { return Math.max(1, Math.round(n / 1024)) + ' KB'; }
  function fileUrl(f, robot) { return '/api/audio/' + (robot ? 'robot' : 'out') + '/' + f.file + '?v=' + encodeURIComponent(f.built || ''); }
  function wait(ms) { return new Promise(function (r) { setTimeout(r, ms); }); }
  function rowFor(rel) { return document.querySelector('.rv-row[data-f="' + rel + '"]'); }

  function showReview(rep, listName) {
    R.list = listName || (rep && rep.list) || (S.list && S.list.list) || 'sounds';
    show('review');
    window.scrollTo(0, 0);
    stopAll();
    R.sticky = {};
    $('rv-showall').checked = false;
    var pr = '';
    if (rep) {
      (rep.errors || []).forEach(function (e) { pr += '<div class="prob err"><b>' + esc(e.label) + ':</b> ' + esc(e.text) + '</div>'; });
      (rep.waiting || []).forEach(function (w) { pr += '<div class="prob wait"><b>' + esc(w.label) + ':</b> ' + esc(w.text) + '</div>'; });
      (rep.warnings || []).forEach(function (w) { pr += '<div class="prob warn">⚠ <b>' + esc(w.label) + ':</b> ' + esc(w.text) + '</div>'; });
    }
    $('rv-problems').innerHTML = pr;
    $('rv-title').textContent = rep && rep.errors && rep.errors.length ? 'Check your files (some problems)' : 'Check your files';
    return api('GET', '/api/review/' + R.list).then(function (r) {
      R.files = r.files; R.outDir = r.output_dir;
      R.files.forEach(function (f) { if (!f.review) R.sticky[f.file] = true; });
      var made = rep ? (rep.made || []).length : 0;
      $('rv-summary').textContent = rep && made
        ? (rep.rebuild ? 'Rebuilt ' : 'Cut ' + rep.cut + ' recording' + (rep.cut === 1 ? '' : 's') + ' and made ') + made + ' file' + (made === 1 ? '' : 's') + '. Listen to each one, then press OK or Redo.'
        : (rep ? 'There were no new recordings to cut. ' : '') + 'Listen to each file, then press OK or Redo. Redo puts that sound back in the list to record again.';
      $('rv-where').innerHTML = R.files.length ? 'Files are in <code>' + esc(R.outDir) + '</code>. ▼ = a little quieter than the −20 dB target because its loudest peak hit the −1 dB limit.' : '';
      renderReview();
    }).catch(function (e) { $('rv-files').innerHTML = '<div class="prob err">' + esc(e.message) + '</div>'; });
  }

  function visibleFiles() {
    var show = $('rv-showall').checked;
    return R.files.filter(function (f) { return show || R.sticky[f.file]; });
  }

  function renderReview() {
    var vis = visibleFiles(), h = '', last = null;
    vis.forEach(function (f) {
      if (f.no !== last) { if (last !== null) h += '</div>'; h += '<div class="rv-group"><h3>' + esc(f.title) + '</h3>'; last = f.no; }
      var what = f.kind === 'words' ? 'the word' : f.kind === 'sentences' ? 'the sentence'
        : f.kind === 'sounds' ? ((f.keys || []).length > 1 ? 'both versions, 3 times each' : 'the sound × 3')
        : (f.version > 1 ? 'version 2 alone' : ((f.title || '').indexOf('(') > -1 ? 'version 1 alone' : 'the sound alone'));
      var meta = (f.seconds != null ? f.seconds.toFixed(2) + ' s' : '') + (f.rms_db != null ? ' · ' + db1(f.rms_db) + ' dB' + (f.limited_by_peak ? ' ▼' : '') : '') + (f.bytes ? ' · ' + kb(f.bytes) : '');
      h += '<div class="rv-row' + (f.review ? ' ' + f.review : '') + '" data-f="' + esc(f.file) + '">'
        + '<button class="play" type="button" data-act="play" title="Play">▶</button>'
        + '<div><code>' + esc(f.file) + '</code><span class="what">' + esc(what) + '</span></div>'
        + '<span class="meta">' + esc(meta) + '</span>'
        + '<span class="acts">'
        + (f.robot ? '<button class="bt robot" type="button" data-act="cmp" title="Hear the old robot voice, then yours">Compare with robot</button>' : '')
        + '<button class="bt ok" type="button" data-act="ok">OK</button>'
        + '<button class="bt rd" type="button" data-act="redo">Redo</button></span></div>';
    });
    if (last !== null) h += '</div>';
    if (!vis.length) h = '<div class="rv-empty">' + (R.files.length ? 'Nothing left to check ✓' : 'There are no files yet. Record some sounds first.') + '</div>';
    $('rv-files').innerHTML = h;
    var ok = 0, rd = 0, nw = 0;
    R.files.forEach(function (f) { if (f.review === 'ok') ok++; else if (f.review === 'redo') rd++; else nw++; });
    $('btn-playall').disabled = !vis.length;
    $('btn-okall').disabled = !vis.some(function (f) { return !f.review; });
    $('rv-status').textContent = R.files.length ? ok + ' OK · ' + rd + ' to redo · ' + nw + ' not checked yet' : '';
    setPlaying(R.playing);
  }

  function setPlaying(rel) {
    R.playing = rel;
    Array.prototype.forEach.call(document.querySelectorAll('.rv-row'), function (r) {
      var on = rel && r.dataset.f === rel;
      r.classList.toggle('playing', !!on);
      r.querySelector('.play').textContent = on ? '■' : '▶';
    });
  }

  function stopAll() {
    R.seq++;
    try { player.pause(); } catch (e) {}
    if (R.resolveCur) { var r = R.resolveCur; R.resolveCur = null; r(); }
    setPlaying(null);
    $('btn-stopall').disabled = true;
    var st = $('rv-status');
    if (st && R.files.length) renderStatusCounts();
  }
  function renderStatusCounts() {
    var ok = 0, rd = 0, nw = 0;
    R.files.forEach(function (f) { if (f.review === 'ok') ok++; else if (f.review === 'redo') rd++; else nw++; });
    $('rv-status').textContent = ok + ' OK · ' + rd + ' to redo · ' + nw + ' not checked yet';
  }

  function playUrl(url) {
    return new Promise(function (resolve) {
      R.resolveCur = resolve;
      player.onended = function () { R.resolveCur = null; resolve(); };
      player.onerror = function () { R.resolveCur = null; resolve('error'); };
      player.src = url;
      var p = player.play();
      if (p && p.catch) p.catch(function () { R.resolveCur = null; resolve('blocked'); });
    });
  }

  // steps: [{ file, url, label, gap }]  played one after the other
  function runSeq(steps) {
    stopAll();
    var my = ++R.seq;
    $('btn-stopall').disabled = false;
    return (async function () {
      for (var i = 0; i < steps.length; i++) {
        if (R.seq !== my) return;
        var s = steps[i];
        setPlaying(s.file);
        $('rv-status').textContent = s.label;
        var r = await playUrl(s.url);
        if (R.seq !== my) return;
        if (r) { $('rv-status').textContent = 'Could not play ' + s.file + (r === 'blocked' ? ' (the browser blocked it: click the page and try again)' : ''); await wait(900); }
        if (s.gap && i < steps.length - 1) await wait(s.gap);
      }
      if (R.seq === my) { setPlaying(null); $('btn-stopall').disabled = true; renderStatusCounts(); }
    })();
  }

  function playFile(f) {
    if (R.playing === f.file) { stopAll(); return; }
    runSeq([{ file: f.file, url: fileUrl(f), label: 'Playing ' + f.file }]);
  }
  function compareFile(f) {
    runSeq([{ file: f.file, url: fileUrl(f, true), label: 'Robot voice: ' + f.file, gap: 500 },
            { file: f.file, url: fileUrl(f), label: 'Your voice: ' + f.file }]);
  }
  function playAll() {
    var vis = visibleFiles();
    runSeq(vis.map(function (f, i) { return { file: f.file, url: fileUrl(f), label: 'Playing ' + (i + 1) + ' of ' + vis.length + ': ' + f.file, gap: 400 }; }));
  }

  function mark(rels, review) {
    return api('POST', '/api/review', { files: rels, review: review }).then(function () {
      rels.forEach(function (rel) {
        R.files.forEach(function (f) { if (f.file === rel) f.review = review; });
        R.sticky[rel] = true;                               // stays on screen, now showing OK / Redo
      });
      renderReview();
    }).catch(function (e) { $('rv-status').textContent = e.message; });
  }

  function reviewClick(e) {
    var b = e.target.closest('[data-act]');
    if (!b) return;
    var rowEl = b.closest('.rv-row'), rel = rowEl.dataset.f;
    var f = R.files.filter(function (x) { return x.file === rel; })[0];
    if (!f) return;
    var act = b.dataset.act;
    if (act === 'play') playFile(f);
    else if (act === 'cmp') compareFile(f);
    else mark([rel], f.review === act ? null : act);        // pressing the same button again takes it back
  }

  // ── teleprompter list ───────────────────────────────────────────────────
  function rowHtml(it, i) {
    var isText = it.text !== undefined;                 // words and sentences are shown as text; sounds as letters + key word
    var big = isText ? esc(it.text) : esc(it.sound);
    var kw = isText ? '' : (it.versions > 1 ? '(as in <b>' + esc(it.key_word) + '</b>)' : esc(it.key_word));
    return '<span class="num">' + (i + 1) + '</span>'
      + '<span class="mid"><span class="big">' + big + '</span><span class="kw">' + kw + '</span>'
      + '<span class="hint">' + esc(S.list.prompt) + '</span><span class="redo-tag">Redo: say it again</span></span>'
      + '<span class="chk">✓</span>';
  }

  function renderList(poppedIndex, rebuild) {
    var box = $('plist'), P = S.prompter;
    if (rebuild || box.children.length !== P.n + 1) {
      box.innerHTML = '';
      P.items.forEach(function (it, i) {
        var d = document.createElement('div');
        d.className = 'row ' + S.list.kind; d.dataset.i = i;
        d.innerHTML = rowHtml(it, i);
        d.addEventListener('click', function () { clickRow(i); });
        box.appendChild(d);
      });
      var end = document.createElement('div');
      end.className = 'end-banner hidden'; end.id = 'end-banner';
      end.textContent = 'All items recorded ✓  Press Finish when you are ready.';
      box.appendChild(end);
    }
    for (var i = 0; i < P.n; i++) {
      var row = box.children[i];
      row.classList.toggle('done', P.isDone(i));
      row.classList.toggle('cur', i === P.pointer);
      row.classList.toggle('redoing', i === P.pointer && P.redoing());
      var flag = !P.isDone(i) && !!(S.redoKeys && S.redoKeys[P.items[i].key]);     // sent back from the check screen
      row.classList.toggle('flag', flag);
      row.querySelector('.chk').textContent = flag ? '↻' : '✓';
      if (i === poppedIndex) { row.classList.remove('pop'); void row.offsetWidth; row.classList.add('pop'); }
    }
    $('end-banner').classList.toggle('hidden', !P.finished());
  }

  function scrollToCurrent(instant) {
    var box = $('plist'), P = S.prompter;
    var el = P.finished() ? $('end-banner') : box.children[P.pointer];
    if (!el) return;
    box.style.scrollBehavior = instant ? 'auto' : 'smooth';
    // centre the current item in the list area
    var target = el.offsetTop - (box.clientHeight - el.offsetHeight) / 2;
    box.scrollTo({ top: target, behavior: instant ? 'auto' : 'smooth' });
  }

  function todayCount() {
    var keys = {};
    Object.keys(S.todayBase).forEach(function (k) { keys[k] = true; });
    Object.keys(S.sessionItems).forEach(function (k) { keys[k] = true; });
    return Object.keys(keys).length;
  }
  function updateTop() {
    var P = S.prompter;
    $('p-progress').textContent = P.doneCount() + ' of ' + P.n + ' recorded';
    $('p-today').textContent = todayCount() + ' of ' + P.n + ' done today';
  }
  function updateNs() {
    var b = $('btn-ns');
    b.textContent = 'Noise suppression: ' + (S.ns ? 'on' : 'off');
    b.classList.toggle('on', !S.ns);
  }
  function updateControls() {
    var P = S.prompter, rec = S.mode === 'record';
    $('btn-redo').disabled = !rec || P.pointer <= 0;
    $('btn-skip').disabled = !rec || P.finished();
    $('btn-retry').disabled = !rec || P.finished() || S.paused;
    $('btn-hold').disabled = !rec || P.finished() || S.paused;
    $('btn-range').disabled = !(scope() === 'range');
    $('range-from').disabled = $('range-to').disabled = scope() !== 'range';
    $('range-from').max = $('range-to').max = P.n;
  }

  // ── controls ────────────────────────────────────────────────────────────
  function scope() {
    var r = document.querySelector('input[name=scope]:checked');
    return r ? r.value : 'one';
  }
  function moved(what, extra) {
    endPlayback();
    logEvent(what, extra);
    if (S.detector) S.detector.cancel();
    applyLevel();
    renderList();
    updateTop(); updateControls(); restoreStatus();
    scrollToCurrent();
  }

  function redo() { if (S.mode !== 'record' || S.hold) return; if (S.prompter.back()) moved('redo', { pointer: S.prompter.pointer }); }
  function skip() { if (S.mode !== 'record' || S.hold || S.prompter.finished()) return; S.prompter.skip(); moved('skip', { pointer: S.prompter.pointer }); }

  function clickRow(i) {
    if (S.mode !== 'record' || S.hold) return;
    var P = S.prompter, sc = scope();
    if (i === P.pointer && sc !== 'range') return;
    if (sc === 'range') {                           // a click picks the start of the range; "Redo range" starts it
      $('range-from').value = i + 1;
      if (Number($('range-to').value) < i + 1) $('range-to').value = Math.min(P.n, i + 11);
      return;
    }
    P.goTo(i, sc);
    moved('goto', { pointer: i, scope: sc });
  }
  function goRange() {
    var P = S.prompter, a = Math.max(1, Math.min(P.n, Number($('range-from').value) || 1)), b = Math.max(a, Math.min(P.n, Number($('range-to').value) || a));
    $('range-from').value = a; $('range-to').value = b;
    P.goTo(a - 1, 'range', b - 1);
    moved('range', { from: a - 1, to: b - 1 });
  }

  // "Done" (sentences): end the take now instead of waiting for the silence
  function done() {
    if (S.mode !== 'record' || S.paused || S.hold || S.playback || S.prompter.finished()) return;
    var ev = S.detector.forceEnd();
    if (ev) handleEnd(ev); else flashStatus('warn', 'Nothing heard yet');
  }

  function pause() {
    if (S.mode !== 'record' || S.hold) return;
    endPlayback();
    S.paused = !S.paused;
    if (S.detector) S.detector.cancel();
    logEvent(S.paused ? 'pause' : 'resume');
    $('btn-pause').innerHTML = S.paused ? '<kbd>Space</kbd> Resume' : '<kbd>Space</kbd> Pause';
    if (S.paused) flush();
    updateControls(); restoreStatus();
  }

  // "Stop and retry": the sound was missed or cut. Drop what the detector has heard of this item and listen again, more sensitive.
  function retry() {
    if (S.mode !== 'record' || S.paused || S.hold || S.prompter.finished()) return;
    endPlayback();
    delete S.autoTries[S.prompter.current().key];
    S.detector.cancel();
    var L = S.prompter.retry();
    logEvent('retry', { key: S.prompter.current().key, level: L });
    applyLevel();
    flashStatus('warn', L >= Prompter.MAX_LEVEL ? 'Most sensitive: say it again' : 'More sensitive: say it again');
    setTimeout(function () { if (S.prompter.level === L) setStatus('listen', 'Listening…'); }, 1800);
  }

  // Hold-to-record fallback: while the key/button is held, the take is forced; no detection needed.
  function holdStart() {
    if (S.mode !== 'record' || S.paused || S.hold || S.prompter.finished()) return;
    endPlayback();
    S.hold = { start: recTime() };
    S.detector.cancel();
    $('btn-hold').classList.add('down');
    restoreStatus();
  }
  function holdEnd() {
    if (!S.hold) return;
    var h = S.hold; S.hold = null;
    $('btn-hold').classList.remove('down');
    var end = recTime() + HOLD_TAIL_S;
    if (end - h.start < HOLD_MIN_S + HOLD_TAIL_S) { restoreStatus(); return; }     // just a tap
    takeDone({ start: h.start, end: end, peak_db: null, rms_db: null, forced: true });
  }

  function toggleNs(on) {
    if (S.mode === 'record') {
      if (S.hold) return;
      S.ns = on;
      updateNs();
      saveOptions({ noise_suppression: on }).catch(function () {});
      logEvent('noise_suppression', { on: on });
      S.detector.cancel();
      $('overlay').classList.remove('hidden');
      $('overlay-bar').style.width = '0%';
      var p = TEST.audio ? Promise.resolve() : S.engine.openMic(S.deviceId, S.ns);
      p.then(function () {
        return measureRoom(2, function (f) { $('overlay-bar').style.width = Math.round(f * 100) + '%'; });
      }).then(function (floor) {
        S.floorDb = floor; applyLevel(); saveTimes();
      }).catch(function (err) {
        $('p-save').textContent = friendlyMicError(err);
      }).then(function () { $('overlay').classList.add('hidden'); restoreStatus(); });
    } else {
      S.ns = on;                                    // on the quiet check screen: re-open the mic and measure again
      saveOptions({ noise_suppression: on }).catch(function () {});
      $('ns-check').checked = on;
      runCheck();
    }
  }

  // ── Options menu (the gear) ─────────────────────────────────────────────
  var OPT_SECTIONS = [
    { title: 'Recording', fields: [
      { k: 'noise_suppression', t: 'bool', label: 'Noise suppression', help: 'It can clip faint consonants like f and th. Use "Test the mic" below to decide by ear.' },
      { k: 'mode', t: 'enum', label: 'Mode', choices: [['quick', 'Quick: record many in a row, check later'], ['careful', 'Careful: play each take back right after it is found']] },
      { k: 'after_redo', t: 'enum', label: 'After redoing an old item', choices: [['return', 'Return to where I was'], ['stay', 'Stay on the redone item']] },
      { k: 'countdown', t: 'bool', label: 'Countdown beat', help: 'The next item blinks before you speak.' },
      { k: 'countdown_ms', t: 'num', label: 'Countdown length', unit: 'ms', step: 100 }
    ] },
    { title: 'Auto-redo', fields: [
      { k: 'auto_redo', t: 'bool', label: 'Auto-redo', help: 'If a take looks bad, it is not marked done: the item turns amber and asks you to say it again.' },
      { k: 'auto_redo_min_ms', t: 'num', label: 'Too short if under', unit: 'ms', step: 10 },
      { k: 'auto_redo_max_ms_sounds', t: 'num', label: 'Too long for a sound if over', unit: 'ms', step: 100 },
      { k: 'auto_redo_max_ms_words', t: 'num', label: 'Too long for a word if over', unit: 'ms', step: 100 },
      { k: 'auto_redo_max_ms_sentences', t: 'num', label: 'Too long for a sentence if over', unit: 'ms', step: 500 },
      { k: 'auto_redo_min_rms_db', t: 'num', label: 'Too quiet if under', unit: 'dB', step: 1, help: 'The loudness of the take itself. Normal speech is around −35 to −20.' },
      { k: 'auto_redo_clip_db', t: 'num', label: 'Clipping if the peak is over', unit: 'dB', step: 0.1 },
      { k: 'auto_redo_max_tries', t: 'num', label: 'Ask at most', unit: 'times per item', step: 1, help: 'After that the take is kept anyway (with a warning), so you are never stuck.' }
    ] },
    { title: 'Listening', fields: [
      { k: 'silence_ms_sounds', t: 'num', label: 'Silence that ends a sound', unit: 'ms', step: 50 },
      { k: 'silence_ms_words', t: 'num', label: 'Silence that ends a word', unit: 'ms', step: 50 },
      { k: 'silence_ms_sentences', t: 'autonum', label: 'Silence that ends a sentence', unit: 'ms', step: 100, help: 'Auto: longer sentences wait a little longer (settings below).' },
      { k: 'sentence_silence_base_ms', t: 'num', label: 'Auto: silence for a short sentence', unit: 'ms', step: 100 },
      { k: 'sentence_short_words', t: 'num', label: 'Auto: a short sentence has up to', unit: 'words', step: 1 },
      { k: 'sentence_silence_per_word_ms', t: 'num', label: 'Auto: extra for each word above that', unit: 'ms', step: 10 },
      { k: 'sentence_silence_max_ms', t: 'num', label: 'Auto: never more than', unit: 'ms', step: 100 },
      { k: 'sensitivity_margin_db', t: 'num', label: 'Detection level above the room noise', unit: 'dB', step: 1, help: 'Lower = more sensitive (picks up softer sounds, but also noise).' },
      { k: 'retry_step_db', t: 'num', label: 'Each Stop & retry lowers the level by', unit: 'dB', step: 0.5 }
    ] },
    { title: 'Files (for sounds you make from now on; press "Rebuild all files" on the lists screen to redo old ones)', fields: [
      { k: 'pad_s', t: 'num', label: 'Quiet kept each side of a sound', unit: 's', step: 0.01 },
      { k: 'fade_ms', t: 'num', label: 'Fade in and out', unit: 'ms', step: 1 },
      { k: 'target_rms_db', t: 'num', label: 'Loudness of each sound (RMS)', unit: 'dB', step: 1, help: 'Higher = louder. The loudest peak still never goes over the limit below.' },
      { k: 'peak_db', t: 'num', label: 'Loudest peak', unit: 'dB', step: 0.1 },
      { k: 'aac_kbps', t: 'select', label: 'Quality (.m4a bitrate)', choices: [[64, '64 kbps (default)'], [96, '96 kbps'], [128, '128 kbps'], [160, '160 kbps'], [192, '192 kbps']], help: 'Try 96 or 128 if short sounds like k and t sound rough.' },
      { k: 'sound_repeats', t: 'enum', label: 'Sound said', choices: [[3, '3 times (you record it once)'], [1, 'once']] },
      { k: 'sound_gap_s', t: 'num', label: 'Quiet between repeats', unit: 's', step: 0.1 },
      { k: 'version_pause_s', t: 'num', label: 'Quiet between the two versions (th, ew…)', unit: 's', step: 0.1 }
    ] },
    { title: 'Microphone reminder', fields: [
      { k: 'mic_tip', t: 'text', label: 'Shown on the quiet-check screen' }
    ] }
  ];
  var optGet = {}, optSet = {};

  function el(tag, cls, html) { var e = document.createElement(tag); if (cls) e.className = cls; if (html !== undefined) e.innerHTML = html; return e; }

  function buildOptForm() {
    var form = $('opt-form');
    form.innerHTML = '';
    optGet = {}; optSet = {};
    OPT_SECTIONS.forEach(function (sec) {
      form.appendChild(el('h3', '', esc(sec.title)));
      sec.fields.forEach(function (f) {
        var sp = S.spec[f.k], v = S.opt[f.k];
        var row = el('div', 'of'), ctl = el('div', 'ctl');
        row.appendChild(el('div', 'lbl', esc(f.label)));
        if (f.t === 'bool') {
          var cb = el('input'); cb.type = 'checkbox'; cb.checked = !!v;
          var l = el('label'); l.appendChild(cb); l.appendChild(el('span', '', 'On'));
          ctl.appendChild(l);
          optGet[f.k] = function () { return cb.checked; }; optSet[f.k] = function (x) { cb.checked = !!x; };
        } else if (f.t === 'enum') {
          f.choices.forEach(function (c) {
            var rb = el('input'); rb.type = 'radio'; rb.name = 'opt-' + f.k; rb.value = String(c[0]); rb.checked = String(c[0]) === String(v);
            var l = el('label'); l.appendChild(rb); l.appendChild(el('span', '', esc(c[1])));
            ctl.appendChild(l);
          });
          optGet[f.k] = function () {
            var r = ctl.querySelector('input:checked');
            var c = r && f.choices.filter(function (c) { return String(c[0]) === r.value; })[0];
            return c ? c[0] : undefined;
          };
        } else if (f.t === 'select') {
          var sel = el('select');
          f.choices.forEach(function (c) { var o = el('option', '', esc(c[1])); o.value = String(c[0]); if (String(c[0]) === String(v)) o.selected = true; sel.appendChild(o); });
          ctl.appendChild(sel);
          optGet[f.k] = function () { var c = f.choices.filter(function (c) { return String(c[0]) === sel.value; })[0]; return c ? c[0] : undefined; };
        } else if (f.t === 'num' || f.t === 'autonum') {
          var inp = el('input'); inp.type = 'number'; inp.min = sp.min; inp.max = sp.max; inp.step = f.step || 1;
          var line = el('div');
          var auto = null;
          if (f.t === 'autonum') {
            auto = el('input'); auto.type = 'checkbox'; auto.checked = v === 'auto';
            var al = el('label'); al.appendChild(auto); al.appendChild(el('span', '', 'Auto'));
            ctl.appendChild(al);
            inp.value = v === 'auto' ? 2000 : v;
            inp.disabled = v === 'auto';
            auto.addEventListener('change', function () { inp.disabled = auto.checked; });
          } else inp.value = v;
          line.appendChild(inp); line.appendChild(el('span', 'unit', esc(f.unit || '')));
          ctl.appendChild(line);
          optGet[f.k] = function () {
            if (auto && auto.checked) return 'auto';
            var n = Number(inp.value);
            return inp.value === '' || !isFinite(n) ? undefined : n;
          };
        } else if (f.t === 'text') {
          var ta = el('textarea'); ta.value = v; ta.maxLength = sp.max;
          row.appendChild(ta);
          optGet[f.k] = function () { return ta.value; };
        }
        if (f.t !== 'text') row.appendChild(ctl);
        if (f.help) row.appendChild(el('div', 'help', esc(f.help)));
        form.appendChild(row);
        if (f.k === 'noise_suppression') {
          var b = el('button', 'btn small', 'Test the mic (3 seconds, with and without)');
          b.type = 'button'; b.id = 'btn-mt'; b.addEventListener('click', micTest);
          var wrap = el('div', 'of'); wrap.appendChild(b);
          wrap.appendChild(el('div', 'help', S.mode === 'record' ? 'Open Options from the lists screen to test the mic.' : 'You say the same thing twice, then listen to both and choose.'));
          form.appendChild(wrap);
          b.disabled = S.mode === 'record';
        }
      });
    });
  }

  function optMsg(text, isErr) { var m = $('opt-msg'); m.textContent = text; m.className = 'small' + (isErr ? ' err' : ''); }
  function optOpen() {
    if (!S.opt) return;
    optMsg('');
    buildOptForm();
    $('mt').classList.add('hidden');
    $('opt').classList.remove('hidden');
  }
  function optClose() { stopMt(); $('opt').classList.add('hidden'); }

  function optSave() {
    var vals = {};
    OPT_SECTIONS.forEach(function (sec) {
      sec.fields.forEach(function (f) {
        var nv = optGet[f.k] && optGet[f.k]();
        if (nv !== undefined && nv !== S.opt[f.k]) vals[f.k] = nv;
      });
    });
    if (!Object.keys(vals).length) { optClose(); return; }
    saveOptions(vals).then(function () {
      optMsg('Saved ✓');
      if ('noise_suppression' in vals) {
        if (S.mode === 'record' || S.screen === 'check') toggleNs(S.opt.noise_suppression);
        else S.ns = S.opt.noise_suppression;
        updateNs();
        $('ns-check').checked = S.ns;
      }
      setTimeout(optClose, 450);
    }).catch(function (e) { optMsg(e.message, true); });
  }

  function optReset() {
    if (!confirm('Put ALL options back to the defaults?')) return;
    api('POST', '/api/settings', { reset: true }).then(function (r) {
      S.spec = r.spec; applyOptions(r.values);
      buildOptForm();
      optMsg('Back to the defaults ✓');
    }).catch(function (e) { optMsg(e.message, true); });
  }

  // ── Test the mic: record 3 s with noise suppression ON, then 3 s OFF, then play both ────────────
  var MT = { run: 0, play: null };
  function stopMt() {
    MT.run++;
    if (MT.play) { try { MT.play.stop(); } catch (e) {} MT.play = null; }
    if (S.engine && S.mode === 'idle') { S.engine.onblock = onBlock; if (!TEST.audio) S.engine.release(); }
  }
  function mtStats(x, sr) {
    var hop = Math.round(sr * 0.01), d = [], pk = 0;
    for (var i = 0; i + hop <= x.length; i += hop) {
      var s = 0;
      for (var j = i; j < i + hop; j++) { s += x[j] * x[j]; var a = Math.abs(x[j]); if (a > pk) pk = a; }
      d.push(10 * Math.log10(s / hop + 1e-12));
    }
    d.sort(function (a, b) { return a - b; });
    var q = function (p) { return d.length ? d[Math.floor(p * (d.length - 1))] : -120; };
    return { peak: pk > 0 ? 20 * Math.log10(pk) : -120, back: q(0.2), voice: q(0.95) };
  }

  function micTest() {
    if (S.mode === 'record') return;
    var E = ensureEngine(), my = ++MT.run, sr;
    $('mt').classList.remove('hidden');
    $('mt-results').innerHTML = '';
    $('mt-bar').style.width = '0%';
    var say = function (title, text) { $('mt-title').textContent = title; $('mt-text').textContent = text || ''; };
    function capture(ns) {
      var open = TEST.audio ? (E.testStarted ? Promise.resolve() : E.openTest(TEST.audio)) : E.openMic(S.deviceId, ns);
      return open.then(function () { sr = E.ctx.sampleRate; S.sr = sr; return wait(700); }).then(function () {
        return new Promise(function (resolve) {
          var chunks = [], n = 0, need = Math.round(3 * sr);
          say('Speak now (noise suppression ' + (ns ? 'ON' : 'OFF') + ')', 'Say the same thing both times, for example: "sh… f… k… s… th…"');
          E.onblock = function (x) {
            meter(x);
            if (MT.run !== my) return;
            chunks.push(new Float32Array(x)); n += x.length;
            $('mt-bar').style.width = Math.min(100, Math.round(100 * n / need)) + '%';
            if (n >= need) {
              E.onblock = function (y) { meter(y); };
              var all = new Float32Array(need), o = 0;
              chunks.forEach(function (c) { var m = Math.min(c.length, need - o); all.set(c.subarray(0, m), o); o += m; });
              resolve(all);
            }
          };
        });
      });
    }
    say('Test the mic', 'Starting the microphone…');
    capture(true).then(function (on) {
      if (MT.run !== my) return;
      $('mt-bar').style.width = '0%';
      return capture(false).then(function (off) { return { on: on, off: off }; });
    }).then(function (r) {
      if (!r || MT.run !== my) return;
      if (!TEST.audio) E.release();
      say('Listen to both', 'Which one sounds better: clearer consonants, no clipped starts? Then press "Use" under it and Save.');
      var res = $('mt-results');
      res.innerHTML = '';
      [['on', 'With noise suppression', true], ['off', 'Without noise suppression', false]].forEach(function (d) {
        var st = mtStats(r[d[0]], sr);
        var row = el('div', 'mt-row');
        var pb = el('button', 'btn small', '▶ ' + d[1]); pb.type = 'button';
        pb.addEventListener('click', function () {
          if (MT.play) { try { MT.play.stop(); } catch (e) {} MT.play = null; }
          var buf = E.ctx.createBuffer(1, r[d[0]].length, sr); buf.copyToChannel(r[d[0]], 0);
          var src = E.ctx.createBufferSource(); src.buffer = buf; src.connect(E.ctx.destination); MT.play = src; src.start();
        });
        var use = el('button', 'btn small', 'Use ' + (d[2] ? 'noise suppression' : 'no noise suppression')); use.type = 'button';
        use.addEventListener('click', function () { optSet.noise_suppression(d[2]); optMsg('Now press Save to keep it.'); });
        row.appendChild(pb);
        row.appendChild(el('span', 'meta', 'Voice ' + db1(st.voice) + ' dB · background ' + db1(st.back) + ' dB · peak ' + db1(st.peak) + ' dB'));
        row.appendChild(use);
        res.appendChild(row);
      });
    }).catch(function (err) {
      say('Cannot test', friendlyMicError(err));
    });
  }

  // ── wiring ──────────────────────────────────────────────────────────────

  // ── 5. Put on the site (R5) ────────────────────────────────────────────────────────────────
  var SITE = { plan: null, unchecked: false, busy: false };
  var ACTION_TEXT = { add: 'new file', robot: 'replaces the robot voice', earlier: 'replaces an earlier recording of yours', same: 'already on the site' };

  function showSite() {
    show('site');
    window.scrollTo(0, 0);
    $('site-result').classList.add('hidden');
    return loadSitePlan();
  }

  function loadSitePlan() {
    return api('GET', '/api/site/plan?unchecked=' + (SITE.unchecked ? 1 : 0)).then(function (p) {
      SITE.plan = p;
      renderSite();
    }).catch(function (e) {
      $('site-msg').className = 'prob err';
      $('site-msg').textContent = e.message;
    });
  }

  function copyText(text, done) {
    var ok = function () { if (done) done(true); }, fail = function () { if (done) done(false); };
    if (navigator.clipboard && navigator.clipboard.writeText) navigator.clipboard.writeText(text).then(ok, fail);
    else fail();
  }

  function renderSite() {
    var p = SITE.plan, c = p.counts, sm = p.summary, msg = $('site-msg');
    $('site-places').innerHTML =
      '<div class="place"><b>Website folder</b> <code>' + esc(p.website_folder) + '</code> ' + (p.website_ok ? '<span class="yes">found</span>' : '<span class="no">portal/audio/phonics not found</span>') + '</div>'
      + '<div class="place"><b>Local test site</b> <code>' + esc(p.local_folder || '(not set)') + '</code> ' + (p.local_ok ? '<span class="yes">found</span>' : '<span class="no">not found: it will be skipped</span>') + '</div>'
      + '<div class="place"><b>Robot voices go to</b> <code>' + esc(p.robot_folder) + '</code></div>';
    if (!p.website_ok) {
      msg.className = 'prob err';
      msg.textContent = 'I cannot find portal/audio/phonics inside ' + p.website_folder + '. Check website_folder in settings.json. Nothing can be put on the site until that is right.';
      $('site-plan').innerHTML = '';
      renderPending();
      return;
    }
    msg.className = 'prob hidden';
    var h = '';
    if (c.unchecked > 0) {
      h += '<label class="inline small"><input type="checkbox" id="site-unchecked"' + (SITE.unchecked ? ' checked' : '') + '> Also send the ' + c.unchecked
        + ' file' + (c.unchecked === 1 ? '' : 's') + ' I have not checked yet</label>';
    }
    var n = sm.add + sm.robot + sm.earlier;
    h += '<div class="site-sum">'
      + '<span class="chip"><b>' + sm.add + '</b> new</span>'
      + '<span class="chip"><b>' + sm.robot + '</b> replace a robot voice (saved first)</span>'
      + (sm.earlier ? '<span class="chip"><b>' + sm.earlier + '</b> replace an earlier recording (saved first)</span>' : '')
      + '<span class="chip"><b>' + sm.same + '</b> already on the site</span>'
      + '</div>';
    var skipped = [];
    if (c.redo) skipped.push(c.redo + ' marked Redo');
    if (c.unchecked && !SITE.unchecked) skipped.push(c.unchecked + ' not checked yet');
    if (skipped.length) h += '<p class="tiny">Not sent: ' + esc(skipped.join(', ')) + '.</p>';
    if (!p.files.length) {
      h += '<div class="rv-empty" style="color:var(--muted)">Nothing is ready. On the check screen, press <b>OK</b> on the files that sound good, then come back.</div>';
    } else {
      h += '<div class="site-files">' + p.files.map(function (f) {
        return '<div class="sf"><span><code>' + esc(f.file) + '</code><span class="what">' + esc(f.title) + '</span></span><span class="badge ' + f.action + '">' + esc(ACTION_TEXT[f.action]) + '</span></div>';
      }).join('') + '</div>';
      h += '<div class="site-actions"><button id="btn-site-put" class="btn primary" type="button"' + (n ? '' : ' disabled') + '>'
        + (n ? 'Put ' + n + ' file' + (n === 1 ? '' : 's') + ' on the site' : 'Everything is already on the site') + '</button>'
        + '<span class="tiny">Takes a moment. You can run it again any time.</span></div>';
    }
    $('site-plan').innerHTML = h;
    var cb = $('site-unchecked');
    if (cb) cb.addEventListener('change', function () { SITE.unchecked = cb.checked; $('site-result').classList.add('hidden'); loadSitePlan(); });
    var b = $('btn-site-put');
    if (b) b.addEventListener('click', function () { putOnSite(n); });
    renderPending();
  }

  function putOnSite(n) {
    var p = SITE.plan;
    var text = 'Put ' + n + ' file' + (n === 1 ? '' : 's') + ' on the site?\n\n'
      + 'They are copied into:\n  ' + p.website_folder + (p.local_ok ? '\n  ' + p.local_folder : '') + '\n\n'
      + (p.summary.robot + p.summary.earlier ? 'The ' + (p.summary.robot + p.summary.earlier) + ' file(s) they replace are moved (not deleted) to:\n  ' + p.robot_folder + '\n\n' : '')
      + 'Nothing goes to the live site.';
    if (!window.confirm(text)) return;
    if (SITE.busy) return;
    SITE.busy = true;
    var b = $('btn-site-put'); if (b) { b.disabled = true; b.textContent = 'Copying…'; }
    api('POST', '/api/site/put', { include_unchecked: SITE.unchecked }).then(function (r) {
      var box = $('site-result'), h = '<div class="site-done"><h3>Done</h3>'
        + '<div><b>' + r.added + '</b> new, <b>' + r.replaced + '</b> replaced, <b>' + r.same + '</b> already there'
        + ' · copied to ' + esc(r.places.map(function (x) { return x === 'local' ? 'the Local test site' : 'the website folder'; }).join(' and ')) + '.</div>'
        + '<div class="tiny">Replaced files were saved in <code>' + esc(r.robot_folder) + '</code>. What was done is in <code>' + esc(r.log_file) + '</code>.</div></div>';
      (r.errors || []).forEach(function (e) { h += '<div class="prob err"><b>' + esc(e.file) + '</b> (' + esc(e.place) + '): ' + esc(e.text) + '</div>'; });
      box.innerHTML = h;
      box.classList.remove('hidden');
      return loadSitePlan();
    }).catch(function (e) {
      $('site-result').innerHTML = '<div class="prob err">' + esc(e.message) + '</div>';
      $('site-result').classList.remove('hidden');
      return loadSitePlan();
    }).then(function () { SITE.busy = false; });
  }

  function renderPending() {
    var p = SITE.plan, box = $('site-pending');
    if (!p.pending.length) { box.classList.add('hidden'); box.innerHTML = ''; return; }
    box.classList.remove('hidden');
    box.innerHTML = '<div class="site-pend"><h3>Waiting to be uploaded (' + p.pending.length + ' file' + (p.pending.length === 1 ? '' : 's') + ')</h3>'
      + '<p class="small">Test them on your Local site first. Then upload exactly these files to the live site (no whole folders):</p>'
      + '<textarea id="site-list" readonly></textarea>'
      + '<div class="site-actions"><button id="btn-site-copy" class="btn small" type="button">Copy the list</button>'
      + '<button id="btn-site-cmd" class="btn small" type="button">Copy the git command</button>'
      + '<button id="btn-site-uploaded" class="btn small" type="button">I uploaded them: clear the list</button>'
      + '<span id="site-copied" class="tiny"></span></div>'
      + '<div class="tiny">The same list is saved in <code>' + esc(p.upload_list_file) + '</code>. To commit them (portal audio only), tell Claude Code, or run this first:</div>'
      + '<pre>' + esc(p.git_add) + '</pre></div>';
    $('site-list').value = p.pending.join('\n');
    var said = function (ok) { $('site-copied').textContent = ok ? 'Copied.' : 'Could not copy: select the text and press Cmd+C.'; };
    $('btn-site-copy').addEventListener('click', function () { copyText(p.pending.join('\n') + '\n', said); });
    $('btn-site-cmd').addEventListener('click', function () { copyText(p.git_add, said); });
    $('btn-site-uploaded').addEventListener('click', function () {
      if (!window.confirm('Clear the list? Only do this once the files are uploaded to the live site. The files themselves stay where they are.')) return;
      api('POST', '/api/site/uploaded', {}).then(function () { return loadSitePlan(); }).catch(function (e) { $('site-msg').className = 'prob err'; $('site-msg').textContent = e.message; });
    });
  }

  function siteHint() {
    api('GET', '/api/site/plan').then(function (p) {
      var t = [];
      var n = p.summary.add + p.summary.robot + p.summary.earlier;
      if (n) t.push(n + ' file' + (n === 1 ? '' : 's') + ' marked OK can go on the site');
      if (p.pending.length) t.push(p.pending.length + ' waiting to be uploaded');
      $('site-hint').textContent = t.join(' · ');
    }).catch(function () { $('site-hint').textContent = ''; });
  }

  function wire() {
    function toolsList() { var sel = $('tools-list'); return { id: sel.value, title: sel.options[sel.selectedIndex] ? sel.options[sel.selectedIndex].text : sel.value }; }
    function startOver(everything) {
      var L = toolsList();
      if (!L.id) return;
      var msg = everything
        ? 'Start "' + L.title + '" over COMPLETELY?\n\nIts finished files, lossless cuts, progress and unfinished takes are all moved aside (into takes/_old). Nothing is deleted.'
        : 'Start "' + L.title + '" over?\n\nRecordings that are not cut into files yet are moved aside (into takes/_old). Nothing is deleted.';
      if (!confirm(msg)) return;
      api('POST', '/api/reset', { list: L.id, everything: everything }).then(function (r) {
        loadListScreen(r.folder ? 'Started over. Moved aside to ' + r.folder : 'Nothing to move.');
      }).catch(function (e) { $('msg-list').textContent = e.message; });
    }
    $('btn-reset').addEventListener('click', function () { startOver(false); });
    $('btn-reset-all').addEventListener('click', function () { startOver(true); });
    $('btn-rebuild').addEventListener('click', function () { var L = toolsList(); if (L.id) runProcess(L.id, true); });
    ['btn-gear-list', 'btn-gear-check', 'btn-gear'].forEach(function (id) { $(id).addEventListener('click', function () { optOpen(); this.blur(); }); });
    $('opt-x').addEventListener('click', optClose);
    $('opt-cancel').addEventListener('click', optClose);
    $('opt-save').addEventListener('click', optSave);
    $('opt-reset').addEventListener('click', optReset);
    $('btn-done').addEventListener('click', function () { done(); this.blur(); });
    $('btn-rv-back').addEventListener('click', function () { stopAll(); loadListScreen(); });
    $('btn-rv-site').addEventListener('click', function () { stopAll(); showSite(); });
    $('btn-site').addEventListener('click', function () { showSite(); });
    $('btn-site-back').addEventListener('click', function () { loadListScreen(); });
    $('rv-files').addEventListener('click', reviewClick);
    $('btn-playall').addEventListener('click', playAll);
    $('btn-stopall').addEventListener('click', stopAll);
    $('rv-showall').addEventListener('change', function () { stopAll(); renderReview(); });
    $('btn-okall').addEventListener('click', function () {
      var rels = visibleFiles().filter(function (f) { return !f.review; }).map(function (f) { return f.file; });
      if (rels.length) mark(rels, 'ok');
    });
    $('btn-check-back').addEventListener('click', function () { S.mode = 'idle'; S.measure = null; if (S.engine) S.engine.release(); loadListScreen(); });
    $('btn-recheck').addEventListener('click', function () { runCheck(); });
    $('btn-start').addEventListener('click', function () { startRecording(); });
    $('ns-check').addEventListener('change', function (e) { toggleNs(e.target.checked); });
    $('mic-select').addEventListener('change', function (e) { S.deviceId = e.target.value; try { localStorage.setItem('studio.mic', S.deviceId); } catch (x) {} runCheck(); });

    $('btn-ns').addEventListener('click', function () { toggleNs(!S.ns); this.blur(); });
    $('btn-finish').addEventListener('click', finish);
    $('btn-redo').addEventListener('click', function () { redo(); this.blur(); });
    $('btn-skip').addEventListener('click', function () { skip(); this.blur(); });
    $('btn-pause').addEventListener('click', function () { pause(); this.blur(); });
    $('btn-retry').addEventListener('click', function () { retry(); this.blur(); });
    $('btn-range').addEventListener('click', function () { goRange(); this.blur(); });
    Array.prototype.forEach.call(document.querySelectorAll('input[name=scope]'), function (r) { r.addEventListener('change', updateControls); });

    var hb = $('btn-hold');
    hb.addEventListener('pointerdown', function (e) { e.preventDefault(); try { hb.setPointerCapture(e.pointerId); } catch (x) {} holdStart(); });
    hb.addEventListener('pointerup', holdEnd);
    hb.addEventListener('pointercancel', holdEnd);
    hb.addEventListener('lostpointercapture', holdEnd);

    document.addEventListener('keydown', function (e) {
      if (S.screen !== 'prompt' || e.metaKey || e.ctrlKey || e.altKey) return;
      if (!$('opt').classList.contains('hidden')) return;            // the Options menu is open
      var t = e.target && e.target.tagName;
      if (t === 'INPUT' && e.target.type !== 'radio' && e.target.type !== 'checkbox') return;
      if (t === 'SELECT' || t === 'TEXTAREA') return;
      var k = e.key, lk = k.length === 1 ? k.toLowerCase() : k, sent = S.list && S.list.kind === 'sentences';
      if (k === ' ' || k === 'ArrowLeft' || k === 'ArrowRight' || k === 'Enter' || lk === 'r' || (sent && lk === 'h')) e.preventDefault(); else return;
      if (e.repeat) return;
      if (k === ' ') pause();
      else if (k === 'ArrowLeft') redo();
      else if (k === 'ArrowRight') skip();
      else if (k === 'Enter') { if (sent) done(); else holdStart(); }      // sentences: Enter = Done, hold H to record
      else if (lk === 'h') holdStart();
      else retry();
    });
    document.addEventListener('keyup', function (e) {
      if (S.screen !== 'prompt') return;
      var sent = S.list && S.list.kind === 'sentences';
      if (sent ? (e.key === 'h' || e.key === 'H') : e.key === 'Enter') holdEnd();
    });
    window.addEventListener('blur', function () { if (S.hold) holdEnd(); });
    document.addEventListener('visibilitychange', function () { if (document.hidden && S.mode === 'record') flush(); });
    window.addEventListener('beforeunload', function (e) {
      if (S.mode === 'record' && S.rec > 0) { e.preventDefault(); e.returnValue = ''; }
    });
  }

  function init() {
    try { S.deviceId = localStorage.getItem('studio.mic') || null; } catch (e) {}
    wire();
    requestAnimationFrame(drawMeter);
    window.__studio = { S: S, done: done, optOpen: optOpen, micTest: micTest, flush: flush, runProcess: runProcess, showReview: showReview, showSite: showSite, SITE: SITE, R: R, player: player, clickRow: clickRow, redo: redo, skip: skip, retry: retry, pause: pause, finish: finish, holdStart: holdStart, holdEnd: holdEnd };
    loadOptions().then(loadListScreen).then(function () { if (TEST.list) openList(TEST.list); }).catch(function (e) {
      $('warn').classList.remove('hidden');
      $('warn').textContent = 'The studio is not answering (' + e.message + '). Is the Terminal window still running studio.py?';
    });
  }
  init();
})();
