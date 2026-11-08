#!/usr/bin/env python3
"""Offline test of cutting, loudness, naming and encoding (step R2). Synthetic recordings, throwaway folders.

    ~/kokoro-env/bin/python -I tests/test_processing.py
"""
import http.client
import json
import math
import shutil
import subprocess
import sys
import tempfile
import threading
import time
import unittest
import wave
from http.server import ThreadingHTTPServer
from pathlib import Path

import numpy as np

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
import studio  # noqa: E402

CSV = """sound_no,sound,key_word,ipa,tip_ja
1,c,cat,k,tip
2,o,octopus,ɑ,tip
23,th,that / thumb,ð / θ,tip
24,sh,shoe,ʃ,tip
"""
HAVE_AFCONVERT = shutil.which("afconvert") is not None
T0 = time.time()


def tone(sr, dur, f0, db, fade=0.004):
    t = np.arange(int(sr * dur)) / sr
    x = np.sin(2 * math.pi * f0 * t) + 0.4 * np.sin(2 * math.pi * 2 * f0 * t)
    x = x / math.sqrt(np.mean(x ** 2)) * 10 ** (db / 20)
    n = int(sr * fade)
    w = 0.5 - 0.5 * np.cos(np.linspace(0, math.pi, n))
    x[:n] *= w
    x[-n:] *= w[::-1]
    return x


def db_of(x):
    return 10 * math.log10(float(np.mean(np.square(x, dtype=np.float64))) + 1e-18)


def decode_m4a(path):
    """Decode an .m4a back to samples with afconvert. Returns (samples float, sample_rate)."""
    tmp = Path(tempfile.mkdtemp(prefix="dec-"))
    try:
        out = tmp / "d.wav"
        subprocess.run(["afconvert", "-f", "WAVE", "-d", "LEI16", str(path), str(out)], check=True, capture_output=True)
        with wave.open(str(out)) as w:
            sr = w.getframerate()
            x = np.frombuffer(w.readframes(w.getnframes()), "<i2").astype(np.float64) / 32768
        return x, sr
    finally:
        shutil.rmtree(tmp, ignore_errors=True)


def bitrate_of(path):
    out = subprocess.run(["afinfo", str(path)], capture_output=True, text=True).stdout
    for line in out.splitlines():
        if line.startswith("bit rate"):
            return int(line.split(":")[1].split()[0])
    return 0


class Base(unittest.TestCase):
    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp(prefix="studio-proc-"))
        self.root = self.tmp / "studio"
        self.root.mkdir()
        site = self.tmp / "site" / "portal" / "phonics"
        site.mkdir(parents=True)
        (site / "phonics-sounds.csv").write_text(CSV, encoding="utf-8")
        self.site = self.tmp / "site"
        self.studio = self.new_studio()
        self.clock = T0

    def tearDown(self):
        shutil.rmtree(self.tmp, ignore_errors=True)

    def new_studio(self, **settings):
        s = {"website_folder": str(self.site)}
        s.update(settings)
        return studio.Studio(self.root, s)

    def session(self, takes, sr=48000, seconds=14.0, room_db=-60, forced=(), wall=None, extra_audio=None, list_name="sounds"):
        """takes: { key: (start, end, signal or None) }. The signal (if any) is placed at `start`."""
        rng = np.random.default_rng(len(takes) + int(seconds))
        x = rng.standard_normal(int(sr * seconds))
        x = x / np.sqrt(np.mean(x ** 2)) * 10 ** (room_db / 20)
        items = {}
        self.clock += 60
        for key, (start, end, sig) in takes.items():
            if sig is not None:
                i = int(round(start * sr))
                x[i:i + len(sig)] += sig
            items[key] = {"start": start, "end": end, "wall": wall or self.clock, "forced": key in forced}
        if extra_audio is not None:
            x = extra_audio(x, sr)
        pcm = (np.clip(x, -1, 1) * 32767).astype("<i2")
        sid = self.studio.new_session(list_name, sr)["id"]
        self.studio.append_audio(sid, 0, pcm.tobytes())
        self.studio.save_times(sid, {"items": items})
        self.studio.close_session(sid)
        return sid, pcm

    def out(self, rel):
        return self.root / "output" / "phonics" / rel

    def progress(self):
        return json.loads((self.root / "progress.json").read_text())["items"]


@unittest.skipUnless(HAVE_AFCONVERT, "needs the Mac's afconvert")
class ProcessingTest(Base):
    def test_01_cut_name_level(self):
        sr = 48000
        # sound 2 is a hold-to-record take: 3 s held (4.0-7.0), the sound is only 0.4 s of it, at 5.2 s
        sidA, pcmA = self.session({
            "sound:1:v1": (2.0, 2.5, tone(sr, 0.5, 300, -26)),
            "sound:2:v1": (4.0, 7.0, None),
            "sound:23:v1": (9.0, 9.3, tone(sr, 0.3, 350, -28)),
            "sound:23:v2": (11.0, 11.6, tone(sr, 0.6, 450, -24)),
        }, forced={"sound:2:v1"}, extra_audio=lambda x, r: self._add(x, r, 5.2, tone(r, 0.4, 500, -26)))
        sidB, _ = self.session({"sound:24:v1": (2.0, 2.5, tone(44100, 0.5, 600, -10))}, sr=44100)   # another day, other mic, much louder

        rep = self.studio.process_list("sounds")
        self.assertEqual(rep["errors"], [], rep["errors"])
        names = sorted(f["file"] for f in rep["made"])
        self.assertEqual(names, ["alone/1.m4a", "alone/2.m4a", "alone/23-2.m4a", "alone/23.m4a", "alone/24.m4a",
                                 "sounds/1.m4a", "sounds/2.m4a", "sounds/23.m4a", "sounds/24.m4a"])
        for n in names:
            self.assertGreater(self.out(n).stat().st_size, 500, n)

        # lengths: padded sound = sound + 2 x 0.12 s; between repeats you hear 0.6 s of quiet; version pause 1.0 s
        def dur(rel):
            x, r = decode_m4a(self.out(rel))
            return len(x) / r
        self.assertAlmostEqual(dur("alone/1.m4a"), 0.5 + 0.24, delta=0.03)
        self.assertAlmostEqual(dur("sounds/1.m4a"), 3 * 0.74 + 2 * 0.36, delta=0.04)
        self.assertAlmostEqual(dur("alone/2.m4a"), 0.4 + 0.24, delta=0.06, msg="hold-to-record silence is trimmed")
        self.assertAlmostEqual(dur("alone/23.m4a"), 0.3 + 0.24, delta=0.03)
        self.assertAlmostEqual(dur("alone/23-2.m4a"), 0.6 + 0.24, delta=0.03)
        self.assertAlmostEqual(dur("sounds/23.m4a"), 3 * 0.54 + 3 * 0.84 + 4 * 0.36 + 0.76, delta=0.06)

        # loudness: the sounds are about -20 dBFS RMS whatever the recording level or day; peak under -1 dB (+ a little for AAC)
        levels = {}
        for rel in ("alone/1.m4a", "alone/24.m4a", "alone/23.m4a", "alone/23-2.m4a"):
            x, r = decode_m4a(self.out(rel))
            pad = int(0.12 * r) + 600
            levels[rel] = db_of(x[pad:len(x) - pad])
            self.assertLess(20 * math.log10(np.max(np.abs(x))), -0.3, rel + " must not clip")
            self.assertAlmostEqual(levels[rel], -20.0, delta=1.2, msg=rel)
        self.assertLess(max(levels.values()) - min(levels.values()), 1.5, "files from different days sound equally loud")

        # bitrate is really 64 kbps
        self.assertTrue(55000 <= bitrate_of(self.out("alone/1.m4a")) <= 70000)

        # lossless cuts: exact copy of the raw take, 0.4 s of room noise each side
        cut = self.root / "output-wav" / "cuts" / "sounds" / "sound-1-v1.wav"
        with wave.open(str(cut)) as w:
            got = np.frombuffer(w.readframes(w.getnframes()), "<i2")
        i0 = int(round((2.0 - 0.4) * sr))
        self.assertTrue(np.array_equal(got, pcmA[i0:i0 + len(got)]))
        self.assertAlmostEqual(len(got) / sr, 0.5 + 0.8, delta=0.002)
        idx = json.loads((self.root / "output-wav" / "cuts" / "sounds" / "index.json").read_text())["items"]
        self.assertEqual(set(idx), {"sound:1:v1", "sound:2:v1", "sound:23:v1", "sound:23:v2", "sound:24:v1"})
        self.assertTrue(idx["sound:2:v1"]["refined"])
        self.assertAlmostEqual(idx["sound:2:v1"]["region"][1] - idx["sound:2:v1"]["region"][0], 0.4, delta=0.05)

        # progress and finished sessions
        prog = self.progress()
        self.assertEqual(set(prog), set(idx))
        self.assertEqual(prog["sound:23:v1"]["files"], ["alone/23.m4a", "alone/23-2.m4a", "sounds/23.m4a"])
        self.assertTrue(prog["sound:2:v1"]["forced"])
        for sid in (sidA, sidB):
            self.assertTrue(json.loads((self.root / "takes" / (sid + ".json")).read_text())["finished"])
        lst = self.studio.api_list("sounds")
        self.assertEqual(lst["recorded"], {})
        self.assertEqual(set(lst["done"]), set(idx))
        # next time: the raw takes are untouched
        self.assertTrue((self.root / "takes" / (sidA + ".wav")).exists())

        # running it again with nothing new does nothing
        rep2 = self.studio.process_list("sounds")
        self.assertEqual((rep2["cut"], rep2["made"], rep2["errors"]), (0, [], []))

    @staticmethod
    def _add(x, sr, t, sig):
        i = int(round(t * sr))
        x[i:i + len(sig)] += sig
        return x

    def test_02_two_versions_arrive_on_different_days_and_redo(self):
        sr = 48000
        self.session({"sound:23:v1": (3.0, 3.4, tone(sr, 0.4, 350, -25))})
        rep = self.studio.process_list("sounds")
        self.assertEqual(sorted(f["file"] for f in rep["made"]), ["alone/23.m4a"])
        self.assertEqual(len(rep["waiting"]), 1)
        self.assertIn("v2", rep["waiting"][0]["text"])
        self.assertFalse(self.out("sounds/23.m4a").exists())
        self.assertEqual(set(self.progress()), {"sound:23:v1"})

        self.session({"sound:23:v2": (3.0, 3.5, tone(sr, 0.5, 450, -25))})
        rep = self.studio.process_list("sounds")
        self.assertEqual(rep["waiting"], [])
        self.assertTrue(self.out("sounds/23.m4a").exists())
        self.assertTrue(self.out("alone/23-2.m4a").exists())
        x, r = decode_m4a(self.out("sounds/23.m4a"))
        self.assertAlmostEqual(len(x) / r, 3 * 0.64 + 3 * 0.74 + 4 * 0.36 + 0.76, delta=0.06)

        # redo version 1 with a longer sound: the old cut is moved aside, the files are remade
        self.session({"sound:23:v1": (6.0, 6.8, tone(sr, 0.8, 300, -25))})
        rep = self.studio.process_list("sounds")
        self.assertEqual(rep["errors"], [])
        x, r = decode_m4a(self.out("alone/23.m4a"))
        self.assertAlmostEqual(len(x) / r, 0.8 + 0.24, delta=0.03)
        replaced = list((self.root / "output-wav" / "_replaced" / "sounds").glob("*sound-23-v1.wav"))
        self.assertEqual(len(replaced), 1, "the old cut is kept, never deleted")
        x, r = decode_m4a(self.out("sounds/23.m4a"))
        self.assertAlmostEqual(len(x) / r, 3 * 1.04 + 3 * 0.74 + 4 * 0.36 + 0.76, delta=0.06)

    def test_03_warnings_and_errors(self):
        sr = 48000
        loud = np.clip(tone(sr, 0.4, 300, -3) * 4, -1, 1)                           # clips
        sid, _ = self.session({
            "sound:1:v1": (2.0, 2.4, loud),
            "sound:2:v1": (4.0, 4.5, tone(sr, 0.5, 300, -70)),                       # almost silent
            "sound:24:v1": (6.0, 6.045, tone(sr, 0.045, 300, -20)),                  # 45 ms
        }, seconds=8.0)
        # a take whose audio was never saved (crash): start is after the end of the file
        meta = json.loads((self.root / "takes" / (sid + ".json")).read_text())
        meta["items"]["sound:23:v1"] = {"start": 50.0, "end": 50.5, "wall": T0 + 999}
        (self.root / "takes" / (sid + ".json")).write_text(json.dumps(meta))
        rep = self.studio.process_list("sounds")
        texts = {w["key"]: w["text"] for w in rep["warnings"]}
        self.assertIn("clipped", texts["sound:1:v1"])
        self.assertIn("quiet", texts["sound:2:v1"])
        self.assertIn("short", texts["sound:24:v1"])
        self.assertEqual([e["key"] for e in rep["errors"]], ["sound:23:v1"])
        self.assertIn("not saved", rep["errors"][0]["text"])
        prog = self.progress()
        self.assertNotIn("sound:23:v1", prog, "an item that could not be cut is NOT marked done")
        self.assertIn("sound:1:v1", prog)
        self.assertTrue(json.loads((self.root / "takes" / (sid + ".json")).read_text())["finished"])

    def test_04_peak_limit_wins(self):
        sr = 48000
        # a sharp click on a quiet body: very high peak compared with its RMS
        sig = tone(sr, 0.3, 400, -45)
        sig[1000:1010] += 0.7
        self.session({"sound:1:v1": (2.0, 2.3, sig)})
        rep = self.studio.process_list("sounds")
        f = [m for m in rep["made"] if m["file"] == "alone/1.m4a"][0]
        self.assertTrue(f["limited_by_peak"])
        self.assertAlmostEqual(f["peak_db"], -1.0, delta=0.05)
        self.assertLess(f["rms_db"], -20.0)
        x, r = decode_m4a(self.out("alone/1.m4a"))
        self.assertLess(20 * math.log10(np.max(np.abs(x))), 0.0, "no clipping after AAC")

    def test_05_rebuild_with_new_settings(self):
        sr = 48000
        self.session({"sound:1:v1": (2.0, 2.5, tone(sr, 0.5, 300, -26))})
        self.studio.process_list("sounds")
        before = self.out("alone/1.m4a").stat().st_size
        x, r = decode_m4a(self.out("sounds/1.m4a"))
        self.assertAlmostEqual(len(x) / r, 2.94, delta=0.05)
        # change settings: 128 kbps, the sound once, louder target
        self.studio = self.new_studio(aac_kbps=128, sound_repeats=1, target_rms_db=-16)
        rep = self.studio.process_list("sounds", rebuild=True)
        self.assertEqual(rep["errors"], [])
        self.assertEqual(rep["cut"], 0)
        self.assertGreater(self.out("alone/1.m4a").stat().st_size, before * 1.4)
        self.assertTrue(115000 <= bitrate_of(self.out("alone/1.m4a")) <= 135000)
        x, r = decode_m4a(self.out("sounds/1.m4a"))
        self.assertAlmostEqual(len(x) / r, 0.74, delta=0.03)
        pad = int(0.12 * r) + 600
        self.assertAlmostEqual(db_of(x[pad:len(x) - pad]), -16.0, delta=1.2)
        # silly settings fall back to safe values
        s = self.new_studio(aac_kbps="loud", sound_repeats=99, target_rms_db=5)
        self.assertEqual((s.proc.cfg["aac_kbps"], s.proc.cfg["sound_repeats"], s.proc.cfg["target_rms_db"]), (64, 5, -6.0))

    def test_06_start_over_moves_everything_aside(self):
        sr = 48000
        self.session({"sound:1:v1": (2.0, 2.5, tone(sr, 0.5, 300, -26))})
        self.studio.process_list("sounds")
        self.session({"sound:2:v1": (2.0, 2.5, tone(sr, 0.5, 300, -26))})            # recorded, not cut yet
        r = self.studio.reset_list("sounds", everything=True)
        self.assertEqual(r["moved"], 1)
        self.assertFalse(self.out("sounds/1.m4a").exists())
        self.assertFalse(self.out("alone/1.m4a").exists())
        self.assertEqual(self.progress(), {})
        old = Path(r["folder"])
        self.assertTrue((old / "output" / "sounds" / "1.m4a").exists())
        self.assertTrue((old / "output" / "alone" / "1.m4a").exists())
        self.assertTrue((old / "cuts" / "sound-1-v1.wav").exists())
        self.assertTrue((old / "progress-before.json").exists())
        self.assertEqual(len(list(old.glob("*.wav"))), 1, "the unfinished take was moved too")
        lst = self.studio.api_list("sounds")
        self.assertEqual((lst["recorded"], lst["done"]), ({}, {}))

    def test_07_over_http(self):
        sr = 48000
        self.session({"sound:1:v1": (2.0, 2.5, tone(sr, 0.5, 300, -26)), "sound:24:v1": (4.0, 4.4, tone(sr, 0.4, 300, -26))})
        httpd = ThreadingHTTPServer(("127.0.0.1", 0), studio.make_handler(self.studio, 0))
        port = httpd.server_address[1]
        httpd.RequestHandlerClass = studio.make_handler(self.studio, port)
        httpd.daemon_threads = True
        threading.Thread(target=httpd.serve_forever, daemon=True).start()
        try:
            def call(method, path, body=None):
                c = http.client.HTTPConnection("127.0.0.1", port, timeout=60)
                c.request(method, path, body=json.dumps(body) if body is not None else None,
                          headers={"Host": "localhost:%d" % port, "Content-Type": "application/json"})
                r = c.getresponse()
                return r.status, json.loads(r.read().decode())
            st, lists = call("GET", "/api/lists")
            self.assertEqual({c["id"]: c for c in lists["lists"]}["sounds"]["pending"], 2)
            st, rep = call("POST", "/api/process", {"list": "sounds"})
            self.assertEqual(st, 200)
            self.assertEqual(len(rep["made"]), 4)
            self.assertEqual(Path(rep["output_dir"]).resolve(), (self.root / "output" / "phonics").resolve())
            st, lists = call("GET", "/api/lists")
            sounds = {c["id"]: c for c in lists["lists"]}["sounds"]
            self.assertEqual((sounds["done"], sounds["pending"]), (2, 0))
            st, err = call("POST", "/api/process", {"list": "nope"})
            self.assertEqual(st, 404)
        finally:
            httpd.shutdown()
            httpd.server_close()


class ConfigTest(unittest.TestCase):
    def test_defaults(self):
        import processing
        cfg = processing.config_from_settings({})
        self.assertEqual((cfg["pad_s"], cfg["target_rms_db"], cfg["peak_db"], cfg["aac_kbps"], cfg["sound_repeats"],
                          cfg["sound_gap_s"], cfg["version_pause_s"], cfg["fade_ms"]), (0.12, -20.0, -1.0, 64, 3, 0.6, 1.0, 10.0))


if __name__ == "__main__":
    unittest.main(verbosity=2)
