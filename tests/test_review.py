#!/usr/bin/env python3
"""Offline test of the check screen's server side (step R3): file list, OK / Redo, audio with byte ranges, robot files.

    ~/kokoro-env/bin/python -I tests/test_review.py
"""
import http.client
import json
import shutil
import sys
import threading
import unittest
from http.server import ThreadingHTTPServer
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
import studio  # noqa: E402
import test_processing as tp  # noqa: E402


@unittest.skipUnless(tp.HAVE_AFCONVERT, "needs the Mac's afconvert")
class ReviewTest(tp.Base):
    def setUp(self):
        super().setUp()
        sr = 48000
        self.session({
            "sound:1:v1": (2.0, 2.5, tp.tone(sr, 0.5, 300, -26)),
            "sound:23:v1": (4.0, 4.4, tp.tone(sr, 0.4, 350, -26)),
            "sound:23:v2": (6.0, 6.5, tp.tone(sr, 0.5, 450, -26)),
        })
        self.rep = self.studio.process_list("sounds")
        # the website's robot voices: only alone/1.m4a exists
        robot = self.site / "portal" / "audio" / "phonics" / "alone"
        robot.mkdir(parents=True)
        (robot / "1.m4a").write_bytes(b"ROBOT-VOICE-BYTES" * 10)

    def http(self):
        httpd = ThreadingHTTPServer(("127.0.0.1", 0), studio.make_handler(self.studio, 0))
        port = httpd.server_address[1]
        httpd.RequestHandlerClass = studio.make_handler(self.studio, port)
        httpd.daemon_threads = True
        threading.Thread(target=httpd.serve_forever, daemon=True).start()
        self.addCleanup(lambda: (httpd.shutdown(), httpd.server_close()))

        def call(method, path, body=None, headers=None):
            c = http.client.HTTPConnection("127.0.0.1", port, timeout=20)
            h = {"Host": "localhost:%d" % port}
            h.update(headers or {})
            if body is not None and not isinstance(body, bytes):
                body = json.dumps(body).encode()
                h["Content-Type"] = "application/json"
            c.request(method, path, body=body, headers=h)
            r = c.getresponse()
            data = r.read()
            hdr = dict((k.lower(), v) for k, v in r.getheaders())
            c.close()
            return r.status, hdr, data
        return call

    def test_01_file_list(self):
        r = self.studio.api_review("sounds")
        names = [f["file"] for f in r["files"]]
        self.assertEqual(names, ["alone/1.m4a", "sounds/1.m4a", "alone/23.m4a", "alone/23-2.m4a", "sounds/23.m4a"])
        by = {f["file"]: f for f in r["files"]}
        self.assertTrue(all(f["review"] is None for f in r["files"]))
        self.assertTrue(by["alone/1.m4a"]["robot"])
        self.assertFalse(by["sounds/1.m4a"]["robot"])
        self.assertFalse(by["alone/23.m4a"]["robot"])
        self.assertEqual(by["alone/23-2.m4a"]["keys"], ["sound:23:v2"])
        self.assertEqual(by["sounds/23.m4a"]["keys"], ["sound:23:v1", "sound:23:v2"])
        self.assertEqual(by["alone/23.m4a"]["title"], "th (that / thumb)")
        self.assertAlmostEqual(by["alone/1.m4a"]["seconds"], 0.74, delta=0.03)
        self.assertAlmostEqual(by["alone/1.m4a"]["rms_db"], -20.0, delta=0.2)
        lst = {c["id"]: c for c in self.studio.api_lists()["lists"]}["sounds"]
        self.assertEqual((lst["unchecked"], lst["redo"], lst["done"]), (5, 0, 3))

    def test_02_ok_and_redo(self):
        res = self.studio.set_review(["alone/1.m4a", "sounds/1.m4a"], "ok")
        self.assertEqual(res["files"], {"alone/1.m4a": "ok", "sounds/1.m4a": "ok"})
        lst = {c["id"]: c for c in self.studio.api_lists()["lists"]}["sounds"]
        self.assertEqual(lst["unchecked"], 3)
        # Redo the second version of th: only that sound goes back in the list
        self.studio.set_review(["alone/23-2.m4a"], "redo")
        d = self.studio.api_list("sounds")
        self.assertNotIn("sound:23:v2", d["done"])
        self.assertIn("sound:23:v1", d["done"])
        self.assertEqual(d["redo"], ["sound:23:v2"])
        lst = {c["id"]: c for c in self.studio.api_lists()["lists"]}["sounds"]
        self.assertEqual((lst["done"], lst["redo"]), (2, 1))
        # OK on the file that holds both versions must not hide the redo that another file asked for
        self.studio.set_review(["sounds/23.m4a"], "ok")
        self.assertEqual(self.studio.api_list("sounds")["redo"], ["sound:23:v2"])
        # clearing the Redo (changed my mind) puts it back
        self.studio.set_review(["alone/23-2.m4a"], None)
        d = self.studio.api_list("sounds")
        self.assertEqual(d["redo"], [])
        self.assertIn("sound:23:v2", d["done"])
        by = {f["file"]: f for f in self.studio.api_review("sounds")["files"]}
        self.assertIsNone(by["alone/23-2.m4a"]["review"])
        # Redo on the "x3" file sends both versions back
        self.studio.set_review(["sounds/23.m4a"], "redo")
        self.assertEqual(self.studio.api_list("sounds")["redo"], ["sound:23:v1", "sound:23:v2"])
        # it all survives a restart of the studio
        again = self.new_studio()
        self.assertEqual(again.api_list("sounds")["redo"], ["sound:23:v1", "sound:23:v2"])
        self.assertEqual({f["file"]: f["review"] for f in again.api_review("sounds")["files"]}["alone/1.m4a"], "ok")

    def test_03_rerecording_replaces_the_files_and_clears_redo(self):
        self.studio.set_review(["alone/1.m4a", "sounds/1.m4a"], "ok")
        self.studio.set_review(["alone/23.m4a"], "redo")
        self.session({"sound:23:v1": (3.0, 3.6, tp.tone(48000, 0.6, 320, -26))})
        rep = self.studio.process_list("sounds")
        self.assertEqual(rep["errors"], [])
        d = self.studio.api_list("sounds")
        self.assertEqual(d["redo"], [])
        self.assertIn("sound:23:v1", d["done"])
        by = {f["file"]: f for f in self.studio.api_review("sounds")["files"]}
        for rel in ("alone/23.m4a", "alone/23-2.m4a", "sounds/23.m4a"):
            self.assertIsNone(by[rel]["review"], rel + " starts unchecked again")
        self.assertEqual(by["alone/1.m4a"]["review"], "ok", "other sounds keep their OK")
        self.assertAlmostEqual(by["alone/23.m4a"]["seconds"], 0.6 + 0.24, delta=0.03)

    def test_04_errors(self):
        with self.assertRaises(studio.StudioError) as cm:
            self.studio.set_review(["alone/999.m4a"], "ok")
        self.assertEqual(cm.exception.status, 404)
        with self.assertRaises(studio.StudioError) as cm:
            self.studio.set_review(["alone/1.m4a"], "maybe")
        self.assertEqual(cm.exception.status, 400)
        with self.assertRaises(studio.StudioError) as cm:
            self.studio.set_review([], "ok")
        self.assertEqual(cm.exception.status, 400)
        with self.assertRaises(studio.StudioError):
            self.studio.api_review("words")

    def test_05_audio_over_http(self):
        call = self.http()
        st, h, body = call("GET", "/api/audio/out/alone/1.m4a")
        self.assertEqual(st, 200)
        self.assertEqual(h["content-type"], "audio/mp4")
        self.assertEqual(h["accept-ranges"], "bytes")
        full = (self.root / "output" / "phonics" / "alone" / "1.m4a").read_bytes()
        self.assertEqual(body, full)
        st, h, part = call("GET", "/api/audio/out/alone/1.m4a", headers={"Range": "bytes=0-99"})
        self.assertEqual((st, len(part), part), (206, 100, full[:100]))
        self.assertEqual(h["content-range"], "bytes 0-99/%d" % len(full))
        st, h, part = call("GET", "/api/audio/out/alone/1.m4a", headers={"Range": "bytes=100-"})
        self.assertEqual((st, part), (206, full[100:]))
        st, h, part = call("GET", "/api/audio/out/alone/1.m4a", headers={"Range": "bytes=-50"})
        self.assertEqual((st, part), (206, full[-50:]))
        st, h, part = call("GET", "/api/audio/out/alone/1.m4a", headers={"Range": "bytes=99999999-"})
        self.assertEqual(st, 416)
        # the robot voice, read from the website folder
        st, h, body = call("GET", "/api/audio/robot/alone/1.m4a")
        self.assertEqual((st, body), (200, b"ROBOT-VOICE-BYTES" * 10))
        # not there / not allowed
        for path in ("/api/audio/robot/alone/2.m4a", "/api/audio/out/alone/nope.m4a", "/api/audio/out/other/1.m4a",
                     "/api/audio/out/alone/..%2f..%2fprogress.json", "/api/audio/out/alone/1.wav", "/api/audio/robot/sounds/1.m4a"):
            st, h, body = call("GET", path)
            self.assertEqual(st, 404, path)
        st, h, body = call("GET", "/api/audio/out/alone/1.m4a", headers={"Host": "evil.example.com"})
        self.assertEqual(st, 403)

    def test_06_review_over_http(self):
        call = self.http()
        st, h, body = call("GET", "/api/review/sounds")
        files = json.loads(body)["files"]
        self.assertEqual(len(files), 5)
        st, h, body = call("POST", "/api/review", {"files": ["alone/1.m4a"], "review": "redo"})
        self.assertEqual((st, json.loads(body)["files"]), (200, {"alone/1.m4a": "redo"}))
        st, h, body = call("GET", "/api/list/sounds")
        self.assertEqual(json.loads(body)["redo"], ["sound:1:v1"])
        st, h, body = call("POST", "/api/review", {"files": ["alone/1.m4a"], "review": "bad"})
        self.assertEqual(st, 400)

    def test_07_start_over_completely_clears_file_list(self):
        self.studio.set_review(["alone/1.m4a"], "ok")
        self.studio.reset_list("sounds", everything=True)
        self.assertEqual(self.studio.api_review("sounds")["files"], [])
        self.assertEqual(self.studio.progress_files(), {})


if __name__ == "__main__":
    unittest.main(verbosity=2)
