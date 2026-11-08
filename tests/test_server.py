#!/usr/bin/env python3
"""Offline test of the studio server (no browser, no microphone). Uses throwaway folders only.

    ~/kokoro-env/bin/python -I tests/test_server.py
"""
import http.client
import json
import os
import shutil
import sys
import tempfile
import threading
import unittest
import wave
from http.server import ThreadingHTTPServer
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
import studio  # noqa: E402

SOUNDS_CSV = """sound_no,sound,key_word,ipa,tip_ja
1,c,cat,k,tip
2,o,octopus,ɑ,tip
23,th,that / thumb,ð / θ,tip
24,sh,shoe,ʃ,tip
"""
WORDS_CSV = "lesson,group,word,status,note,source\n4,d,cod,common,,Mike\n4,d,cad,rare,,Mike\n8,h,hal,name,,Mike\n9,t,cad,rare,,Mike\n9,t,xx,removed,,Mike\n"


def tree_state(folder):
    out = {}
    for p in sorted(Path(folder).rglob("*")):
        out[str(p)] = (p.stat().st_mtime_ns, p.stat().st_size) if p.is_file() else None
    return out


class ServerTest(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.tmp = Path(tempfile.mkdtemp(prefix="studio-test-"))
        cls.root = cls.tmp / "studio"
        (cls.root / "web").mkdir(parents=True)
        (cls.root / "web" / "index.html").write_text("<html>hi</html>")
        (cls.root / "web" / "app.js").write_text("// js")
        (cls.root / "secret.txt").write_text("not for the web")
        cls.site = cls.tmp / "site"
        d = cls.site / "portal" / "phonics"
        d.mkdir(parents=True)
        (d / "phonics-sounds.csv").write_text(SOUNDS_CSV, encoding="utf-8")
        (d / "phonics-words.csv").write_text(WORDS_CSV, encoding="utf-8")
        cls.site_before = tree_state(cls.site)
        cls.studio = studio.Studio(cls.root, {"website_folder": str(cls.site)})
        cls.httpd = ThreadingHTTPServer(("127.0.0.1", 0), studio.make_handler(cls.studio, 0))
        cls.port = cls.httpd.server_address[1]
        # the handler's host allow-list was built for port 0; rebuild it for the real port
        cls.httpd.RequestHandlerClass = studio.make_handler(cls.studio, cls.port)
        cls.httpd.daemon_threads = True
        threading.Thread(target=cls.httpd.serve_forever, daemon=True).start()

    @classmethod
    def tearDownClass(cls):
        cls.httpd.shutdown()
        cls.httpd.server_close()
        shutil.rmtree(cls.tmp, ignore_errors=True)

    def req(self, method, path, body=None, headers=None, raw=False):
        c = http.client.HTTPConnection("127.0.0.1", self.port, timeout=10)
        h = {"Host": "localhost:%d" % self.port}
        h.update(headers or {})
        data = body
        if isinstance(body, (dict, list)):
            data = json.dumps(body).encode()
            h["Content-Type"] = "application/json"
        c.request(method, path, body=data, headers=h)
        r = c.getresponse()
        payload = r.read()
        c.close()
        if raw:
            return r.status, payload
        try:
            return r.status, json.loads(payload.decode())
        except ValueError:
            return r.status, payload

    # ── lists ──────────────────────────────────────────────────────────────
    def test_01_lists_and_items(self):
        st, cfg = self.req("GET", "/api/config")
        self.assertEqual(st, 200)
        self.assertTrue(cfg["website_ok"])
        self.assertIn("mic_tip", cfg)
        st, lists = self.req("GET", "/api/lists")
        by = {c["id"]: c for c in lists["lists"]}
        self.assertEqual(by["sounds"]["total"], 5)           # 4 rows, one with two versions
        self.assertTrue(by["sounds"]["ready"])
        self.assertFalse(by["words"]["ready"])
        self.assertEqual(by["words"]["total"], 2)            # cad (once), hal; 'removed' skipped
        self.assertEqual(by["common"]["total"], 1)
        st, lst = self.req("GET", "/api/list/sounds")
        keys = [i["key"] for i in lst["items"]]
        self.assertEqual(keys, ["sound:1:v1", "sound:2:v1", "sound:23:v1", "sound:23:v2", "sound:24:v1"])
        th1, th2 = lst["items"][2], lst["items"][3]
        self.assertEqual((th1["key_word"], th2["key_word"], th1["sound"], th1["versions"]), ("that", "thumb", "th", 2))
        self.assertEqual(lst["silence_ms"], 500)
        st, err = self.req("GET", "/api/list/words")
        self.assertEqual(st, 404)

    # ── sessions ───────────────────────────────────────────────────────────
    def test_02_session_audio_and_times(self):
        st, s = self.req("POST", "/api/session/new", {"list": "sounds", "sample_rate": 48000, "mic": {"noise_suppression": True}})
        self.assertEqual(st, 200)
        sid = s["id"]
        wav_path = self.root / "takes" / (sid + ".wav")
        # an empty WAV is already a valid WAV
        with wave.open(str(wav_path)) as w:
            self.assertEqual((w.getnchannels(), w.getsampwidth(), w.getframerate(), w.getnframes()), (1, 2, 48000, 0))
        a = (b"\x01\x00" * 1000)
        b = (b"\x02\x00" * 500)
        st, r = self.req("POST", "/api/session/%s/audio?offset=0" % sid, a, raw=False)
        self.assertEqual((st, r["samples"]), (200, 1000))
        st, r = self.req("POST", "/api/session/%s/audio?offset=1000" % sid, b)
        self.assertEqual((st, r["samples"]), (200, 1500))
        # the same upload sent twice (lost reply) is not added twice
        st, r = self.req("POST", "/api/session/%s/audio?offset=1000" % sid, b)
        self.assertEqual((st, r["samples"]), (200, 1500))
        # an overlapping upload only adds the new part
        st, r = self.req("POST", "/api/session/%s/audio?offset=1400" % sid, b"\x03\x00" * 200)
        self.assertEqual((st, r["samples"]), (200, 1600))
        # a gap is refused
        st, r = self.req("POST", "/api/session/%s/audio?offset=5000" % sid, b)
        self.assertEqual(st, 409)
        # odd byte count is refused
        st, r = self.req("POST", "/api/session/%s/audio?offset=1600" % sid, b"\x00\x00\x00")
        self.assertEqual(st, 400)
        with wave.open(str(wav_path)) as w:
            self.assertEqual(w.getnframes(), 1600)
            frames = w.readframes(1600)
        self.assertEqual(frames[:2], b"\x01\x00")
        self.assertEqual(frames[2000:2002], b"\x02\x00")
        self.assertEqual(frames[2998:3000], b"\x02\x00")      # sample 1499
        self.assertEqual(frames[3000:3002], b"\x03\x00")      # sample 1500: from the overlapping upload
        # times
        now = __import__("time").time()
        items = {"sound:1:v1": {"start": 1.0, "end": 1.5, "wall": now, "peak_db": -10, "rms_db": -25, "forced": False},
                 "sound:2:v1": {"start": 3.0, "end": 3.4, "wall": now, "forced": True}}
        st, r = self.req("POST", "/api/session/%s/times" % sid, {"items": items, "events": [{"t": 0, "type": "start"}]})
        self.assertEqual((st, r["audio_samples"]), (200, 1600))
        meta = json.loads((self.root / "takes" / (sid + ".json")).read_text())
        self.assertEqual(meta["items"]["sound:1:v1"]["start"], 1.0)
        self.assertEqual(meta["sample_rate"], 48000)
        self.assertFalse(meta["finished"])
        # the list now shows them as recorded, and as done today
        st, lst = self.req("GET", "/api/list/sounds")
        self.assertEqual(set(lst["recorded"]), {"sound:1:v1", "sound:2:v1"})
        self.assertEqual(lst["recorded"]["sound:1:v1"]["session"], sid)
        self.assertEqual(set(lst["today"]), {"sound:1:v1", "sound:2:v1"})
        st, lists = self.req("GET", "/api/lists")
        self.assertEqual({c["id"]: c for c in lists["lists"]}["sounds"]["done"], 2)
        st, r = self.req("POST", "/api/session/%s/close" % sid, {})
        self.assertEqual(st, 200)
        meta = json.loads((self.root / "takes" / (sid + ".json")).read_text())
        self.assertIsNotNone(meta["closed"])
        # a redo in a later session wins (newer wall time)
        st, s2 = self.req("POST", "/api/session/new", {"list": "sounds", "sample_rate": 44100})
        later = {"sound:1:v1": {"start": 9.0, "end": 9.5, "wall": now + 100}}
        self.req("POST", "/api/session/%s/times" % s2["id"], {"items": later})
        st, lst = self.req("GET", "/api/list/sounds")
        self.assertEqual(lst["recorded"]["sound:1:v1"]["start"], 9.0)
        self.assertEqual(lst["recorded"]["sound:2:v1"]["start"], 3.0)
        self.__class__.sids = (sid, s2["id"])

    def test_03_security(self):
        st, r = self.req("GET", "/api/config", headers={"Host": "evil.example.com"})
        self.assertEqual(st, 403)
        st, r = self.req("POST", "/api/session/new", {"list": "sounds", "sample_rate": 48000}, headers={"Origin": "http://evil.example.com"})
        self.assertEqual(st, 403)
        st, r = self.req("GET", "/../secret.txt", raw=True)
        self.assertEqual(st, 404)
        st, r = self.req("GET", "/%2e%2e/secret.txt", raw=True)
        self.assertEqual(st, 404)
        st, r = self.req("GET", "/testaudio/../../secret.txt", raw=True)
        self.assertEqual(st, 404)
        st, r = self.req("POST", "/api/session/..%2f..%2fetc/times", {})
        self.assertIn(st, (400, 404))
        st, r = self.req("POST", "/api/session/not-a-real-id/audio?offset=0", b"\x00\x00")
        self.assertEqual(st, 400)
        st, r = self.req("POST", "/api/session/new", {"list": "../x", "sample_rate": 48000})
        self.assertEqual(st, 400)
        st, r = self.req("POST", "/api/session/new", {"list": "sounds", "sample_rate": 5})
        self.assertEqual(st, 400)

    def test_04_static(self):
        st, body = self.req("GET", "/", raw=True)
        self.assertEqual((st, body), (200, b"<html>hi</html>"))
        st, body = self.req("GET", "/app.js", raw=True)
        self.assertEqual(st, 200)
        st, body = self.req("GET", "/nope.js", raw=True)
        self.assertEqual(st, 404)

    def test_05_reset_moves_not_deletes(self):
        sid, sid2 = self.__class__.sids
        st, r = self.req("POST", "/api/reset", {"list": "sounds"})
        self.assertEqual(st, 200)
        self.assertEqual(r["moved"], 2)
        self.assertFalse((self.root / "takes" / (sid + ".wav")).exists())
        moved = list((self.root / "takes" / "_old").rglob("*.wav"))
        self.assertEqual(len(moved), 2)
        st, lst = self.req("GET", "/api/list/sounds")
        self.assertEqual(lst["recorded"], {})

    def test_06_missing_csv_message(self):
        other = studio.Studio(self.root, {"website_folder": str(self.tmp / "nowhere")})
        with self.assertRaises(studio.StudioError) as cm:
            other.api_list("sounds")
        self.assertIn("settings.json", str(cm.exception))

    def test_99_website_folder_untouched(self):
        self.assertEqual(tree_state(self.site), self.site_before)


if __name__ == "__main__":
    unittest.main(verbosity=2)
