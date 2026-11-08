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
    meterDb: -120, lastTake: null, statusTimer: null
  };

  // ── small helpers ───────────────────────────────────────────────────────
  function esc(s) { return String(s).replace(/[&<>"']/g, function (c) { return { '&': '&amp;', '<': '&lt;', '>': '&gt;', '"': '&quot;', "'": '&#39;' }[c]; }); }
  function db1(x) { return (Math.round(x * 10) / 10).toFixed(1).replace('-', '−'); }
  function now() { return Date.now() / 1000; }
  function recTime() { return S.rec / S.sr; }

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
    ['list', 'check', 'prompt', 'result'].forEach(function (n) { $('s-' + n).classList.toggle('hidden', n !== name); });
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
    if (S.mode !== 'record' || S.paused) return;

    // keep the raw take: 16-bit samples, queued for the 30-second autosave
    var pcm = new Int16Array(x.length);
    for (var i = 0; i < x.length; i++) { var v = x[i]; pcm[i] = v >= 1 ? 32767 : v <= -1 ? -32768 : Math.round(v * 32767); }
    S.pcm.push(pcm);
    S.rec += x.length;

    var events = S.detector.push(x);                 // always runs, so its clock stays in step with the recording
    if (S.measure) { feedMeasure(x); S.detector.cancel(); return; }
    if (S.hold) { S.detector.cancel(); return; }     // hold-to-record is in charge: ignore the detector
    if (S.prompter.finished()) { S.detector.cancel(); return; }
    for (var k = 0; k < events.length; k++) {
      var ev = events[k];
      if (ev.type === 'start') setStatus('hear', 'Hearing you…');
      else if (ev.type === 'end') takeDone({ start: ev.start, end: ev.end, peak_db: ev.peakDb, rms_db: ev.rmsDb, forced: false });
    }
  }

  // A take was found (by the detector or by hold-to-record) for the current item.
  function takeDone(t) {
    t.wall = now();
    t.level = S.prompter.level;
    var r = S.prompter.complete(t);
    if (!r) return;
    S.sessionItems[r.key] = t;
    S.lastTake = t;
    logEvent(t.forced ? 'hold' : 'take', { key: r.key, start: t.start, end: t.end });
    S.detector.cancel();
    applyLevel();
    saveTimes();
    renderList(r.index);
    flashStatus('got', 'Got it ✓', S.prompter.finished() ? 'All done: press Finish' : null);
    updateTop();
    updateControls();
    scrollToCurrent();
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
    S.detector.setSilenceMs(S.list.silence_ms);
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
      if (waiting) {
        $('pending-text').textContent = waiting.pending + ' recorded item' + (waiting.pending === 1 ? ' is' : 's are') + ' waiting to be cut into files (' + waiting.title + ').';
        $('btn-cut-now').onclick = function () { runProcess(waiting.id, false); };
      }
      r[1].lists.forEach(function (c) {
        var b = document.createElement('button');
        b.type = 'button'; b.className = 'card'; b.disabled = !c.ready;
        var count = c.ready ? (c.done + ' / ' + (c.total == null ? '?' : c.total) + ' done') : (c.total != null ? c.total + ' items' : '');
        var pct = c.ready && c.total ? Math.round(100 * c.done / c.total) : 0;
        b.innerHTML = '<div class="c-title">' + esc(c.title) + '</div><div class="c-note">' + esc(c.note) + '</div>'
          + '<div class="c-count">' + esc(count) + '</div><div class="prog"><i style="width:' + pct + '%"></i></div>';
        if (c.ready) b.addEventListener('click', function () { openList(c.id); });
        box.appendChild(b);
      });
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
    $('mic-tip').textContent = (S.config && S.config.mic_tip) || '';
    $('ns-check').checked = S.ns;
    return api('GET', '/api/list/' + name).then(function (data) {
      S.list = data;
      var earlier = {};
      Object.keys(data.done || {}).forEach(function (k) { earlier[k] = { done: true }; });
      Object.keys(data.recorded || {}).forEach(function (k) { earlier[k] = data.recorded[k]; });
      S.prompter = new Prompter(data.items, earlier);
      S.todayBase = {};
      (data.today || []).forEach(function (k) { S.todayBase[k] = true; });
      S.sessionItems = {}; S.events = [];
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
      S.detector = new Detector({ sampleRate: S.sr, silenceMs: S.list.silence_ms });
      S.mode = 'record';
      S.saveOk = true;
      $('p-title').textContent = S.list.title;
      $('btn-pause').innerHTML = '<kbd>Space</kbd> Pause';
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
      showResult(rep);
    }).catch(function (e) {
      $('busy').classList.add('hidden');
      loadListScreen('Could not cut the files: ' + e.message + ' Your recordings are safe in the takes folder. Try "Cut and file them now".');
    });
  }

  function kb(n) { return Math.max(1, Math.round(n / 1024)) + ' KB'; }
  function showResult(rep) {
    show('result');
    window.scrollTo(0, 0);
    var made = rep.made || [], sounds = {};
    made.forEach(function (f) { (sounds[f.no] = sounds[f.no] || { title: f.title, files: [] }).files.push(f); });
    var nSounds = Object.keys(sounds).length;
    var title = rep.errors.length ? 'Done, with problems' : (made.length ? 'Done' : 'Nothing new to cut');
    $('res-title').textContent = title;
    $('res-summary').textContent = made.length
      ? (rep.rebuild ? 'Rebuilt ' : 'Cut ' + rep.cut + ' recording' + (rep.cut === 1 ? '' : 's') + ' and made ') + made.length + ' file' + (made.length === 1 ? '' : 's') + ' for ' + nSounds + ' sound' + (nSounds === 1 ? '' : 's') + '.'
      : 'There were no new recordings to cut.';
    var pr = '';
    (rep.errors || []).forEach(function (e) { pr += '<div class="prob err"><b>' + esc(e.label) + ':</b> ' + esc(e.text) + '</div>'; });
    (rep.waiting || []).forEach(function (w) { pr += '<div class="prob wait"><b>' + esc(w.label) + ':</b> ' + esc(w.text) + '</div>'; });
    (rep.warnings || []).forEach(function (w) { pr += '<div class="prob warn">⚠ <b>' + esc(w.label) + ':</b> ' + esc(w.text) + '</div>'; });
    $('res-problems').innerHTML = pr;
    var h = '';
    Object.keys(sounds).forEach(function (no) {
      var g = sounds[no];
      h += '<div class="res-group"><h3>' + esc(g.title) + '</h3>';
      g.files.forEach(function (f) {
        h += '<div class="res-row"><code>' + esc(f.file) + '</code><span>' + f.seconds.toFixed(2) + ' s</span>'
          + '<span>' + db1(f.rms_db) + ' dB' + (f.limited_by_peak ? ' <span class="lim" title="Limited by the peak level, so a little quieter than the target">▼</span>' : '') + '</span>'
          + '<span>' + kb(f.bytes) + '</span></div>';
      });
      h += '</div>';
    });
    $('res-files').innerHTML = h;
    $('res-where').innerHTML = made.length ? 'Files are in <code>' + esc(rep.output_dir) + '</code>. Loudness is the sound\'s RMS level (target −20 dB). The check screen, where you can play each file, comes in the next step.' : '';
  }

  // ── teleprompter list ───────────────────────────────────────────────────
  function rowHtml(it, i) {
    var kw = it.versions > 1 ? '(as in <b>' + esc(it.key_word) + '</b>)' : esc(it.key_word);
    return '<span class="num">' + (i + 1) + '</span>'
      + '<span class="mid"><span class="big">' + esc(it.sound) + '</span><span class="kw">' + kw + '</span>'
      + '<span class="hint">' + esc(S.list.prompt) + '</span><span class="redo-tag">Redo: say it again</span></span>'
      + '<span class="chk">✓</span>';
  }

  function renderList(poppedIndex, rebuild) {
    var box = $('plist'), P = S.prompter;
    if (rebuild || box.children.length !== P.n + 1) {
      box.innerHTML = '';
      P.items.forEach(function (it, i) {
        var d = document.createElement('div');
        d.className = 'row'; d.dataset.i = i;
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

  function pause() {
    if (S.mode !== 'record' || S.hold) return;
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
      $('ns-check').checked = on;
      runCheck();
    }
  }

  // ── wiring ──────────────────────────────────────────────────────────────
  function wire() {
    function startOver(everything) {
      var msg = everything
        ? 'Start the Sounds list over COMPLETELY?\n\nThe finished files, the lossless cuts, the progress and your unfinished takes are all moved aside (into takes/_old). Nothing is deleted.'
        : 'Start the Sounds list over?\n\nYour recordings that are not cut into files yet are moved aside (into takes/_old). Nothing is deleted.';
      if (!confirm(msg)) return;
      api('POST', '/api/reset', { list: 'sounds', everything: everything }).then(function (r) {
        loadListScreen(r.folder ? 'Started over. Moved aside to ' + r.folder : 'Nothing to move.');
      }).catch(function (e) { $('msg-list').textContent = e.message; });
    }
    $('btn-reset').addEventListener('click', function () { startOver(false); });
    $('btn-reset-all').addEventListener('click', function () { startOver(true); });
    $('btn-rebuild').addEventListener('click', function () { runProcess('sounds', true); });
    $('btn-res-back').addEventListener('click', function () { loadListScreen(); });
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
      var t = e.target && e.target.tagName;
      if (t === 'INPUT' && e.target.type !== 'radio' && e.target.type !== 'checkbox') return;
      if (t === 'SELECT' || t === 'TEXTAREA') return;
      var k = e.key;
      if (k === ' ' || k === 'ArrowLeft' || k === 'ArrowRight' || k === 'Enter' || k === 'r' || k === 'R') e.preventDefault(); else return;
      if (e.repeat) return;
      if (k === ' ') pause();
      else if (k === 'ArrowLeft') redo();
      else if (k === 'ArrowRight') skip();
      else if (k === 'Enter') holdStart();
      else retry();
    });
    document.addEventListener('keyup', function (e) { if (e.key === 'Enter' && S.screen === 'prompt') holdEnd(); });
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
    window.__studio = { S: S, flush: flush, runProcess: runProcess, clickRow: clickRow, redo: redo, skip: skip, retry: retry, pause: pause, finish: finish, holdStart: holdStart, holdEnd: holdEnd };
    loadListScreen().then(function () { if (TEST.list) openList(TEST.list); });
  }
  init();
})();
