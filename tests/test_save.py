#!/usr/bin/env python3
"""Offline test of step R8 part 1: the ffmpeg encoder and the "where to save" settings. Throwaway folders only.

    ~/kokoro-env/bin/python -I tests/test_save.py
"""
import http.client
import json
import os
import shutil
import sys
import threading
import unittest
from http.server import ThreadingHTTPServer
from pathlib import Path
from unittest import mock

sys.path.insert(0, str(Path(__file__).resolve().parent))
sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
import processing  # noqa: E402
import studio  # noqa: E402
import test_processing as tp  # noqa: E402

SR = 48000
HAVE_FFMPEG = shutil.which("ffmpeg") is not None
HAVE_ENCODER = tp.HAVE_AFCONVERT or HAVE_FFMPEG


class EncoderTest(tp.Base):
    def setUp(self):
        super().setUp()
        self.root = self.root.resolve()                      # macOS: /var is really /private/var

    def record_one(self, st):
        sid, _ = self.session({"sound:1:v1": (2.0, 2.5, tp.tone(SR, 0.5, 300, -26))})
        return sid

    def test_01_find_encoder(self):
        with mock.patch.object(processing.shutil, "which", side_effect=lambda n: "/x/" + n if n in ("afconvert", "ffmpeg") else None):
            self.assertEqual(processing.find_encoder("auto"), ("afconvert", "/x/afconvert"))     # the Mac's own one comes first
            self.assertEqual(processing.find_encoder("ffmpeg"), ("ffmpeg", "/x/ffmpeg"))
        with mock.patch.object(processing.shutil, "which", side_effect=lambda n: "/x/ffmpeg" if n == "ffmpeg" else None):
            self.assertEqual(processing.find_encoder("auto"), ("ffmpeg", "/x/ffmpeg"))            # Linux: ffmpeg
            self.assertEqual(processing.find_encoder("afconvert"), (None, None))
        with mock.patch.object(processing.shutil, "which", return_value=None):
            self.assertEqual(processing.find_encoder("auto"), (None, None))

    def test_02_no_encoder_says_what_to_install(self):
        p = processing.Processor(self.root, {"aac_kbps": 64})
        import numpy as np
        with mock.patch.object(processing.shutil, "which", return_value=None):
            with self.assertRaises(processing.ProcessingError) as cm:
                p.encode_m4a(np.zeros(4800, dtype="float32"), SR, self.tmp / "x.m4a")
            self.assertIn("ffmpeg", str(cm.exception))
            self.assertFalse((self.tmp / "x.m4a").exists())
            self.assertIsNone(self.studio.api_config()["encoder"])

    @unittest.skipUnless(HAVE_FFMPEG and tp.HAVE_AFCONVERT, "needs ffmpeg and the Mac's afconvert (to compare and to decode)")
    def test_03_ffmpeg_files_match_afconvert_files(self):
        sid, pcm = self.session({
            "sound:1:v1": (2.0, 2.5, tp.tone(SR, 0.5, 300, -26)),
            "sound:23:v1": (4.0, 4.4, tp.tone(SR, 0.4, 350, -26)),
            "sound:23:v2": (6.0, 6.5, tp.tone(SR, 0.5, 450, -26)),
        })
        got = {}
        for enc in ("afconvert", "ffmpeg"):
            out = self.tmp / ("out-" + enc)
            st = self.new_studio(output_folder=str(out), encoder=enc)
            self.assertEqual(st.api_config()["encoder"], enc)
            rep = st.process_list("sounds", rebuild=True)
            self.assertEqual(rep["errors"], [], (enc, rep["errors"]))
            names = sorted(f["file"] for f in rep["made"])
            self.assertEqual(names, ["alone/1.m4a", "alone/23-2.m4a", "alone/23.m4a", "sounds/1.m4a", "sounds/23.m4a"])
            got[enc] = {n: out / "phonics" / n for n in names}
        for n, pa in got["afconvert"].items():
            pf = got["ffmpeg"][n]
            xa, sra = tp.decode_m4a(pa)
            xf, srf = tp.decode_m4a(pf)
            self.assertEqual(sra, srf, n)
            self.assertAlmostEqual(len(xa) / sra, len(xf) / srf, delta=0.05, msg=n)          # same length
            self.assertAlmostEqual(tp.db_of(xa), tp.db_of(xf), delta=1.0, msg=n)            # same loudness
            self.assertTrue(40 <= tp.bitrate_of(pf) / 1000 <= 80, (n, tp.bitrate_of(pf)))   # about 64 kbps
            self.assertEqual(pf.read_bytes()[4:8], b"ftyp", n)                                # a real mp4/m4a file


class FoldersTest(tp.Base):
    def setUp(self):
        super().setUp()
        self.root = self.root.resolve()

    @unittest.skipUnless(HAVE_ENCODER, "needs an encoder")
    def test_01_finished_files_go_where_you_choose(self):
        self.session({"sound:1:v1": (2.0, 2.5, tp.tone(SR, 0.5, 300, -26))})
        dest = self.tmp / "Desktop" / "My recordings"                     # does not exist yet
        r = self.studio.set_options({"output_folder": str(dest)})
        self.assertTrue(dest.is_dir())
        self.assertEqual(r["values"]["output_folder"], str(dest))
        self.assertEqual(len(r["notes"]), 1)
        self.assertIn("Rebuild all files", r["notes"][0])
        rep = self.studio.process_list("sounds")
        self.assertEqual(rep["errors"], [])
        self.assertTrue((dest / "phonics" / "alone" / "1.m4a").is_file())
        self.assertFalse((self.root / "output").exists())
        self.assertEqual(rep["output_dir"], str(dest / "phonics"))
        self.assertEqual(self.studio.api_config()["output_folder"], str(dest))
        # it is remembered
        self.assertEqual(json.loads((self.root / "settings.json").read_text())["output_folder"], str(dest))
        again = self.new_studio()
        self.assertEqual(again.api_config()["output_folder"], str(dest))
        # ~ works, and empty goes back to the studio's own folder
        home = self.studio.set_options({"output_folder": "~/studio-test-nonexistent-" + str(os.getpid())})
        path = Path(os.path.expanduser(home["values"]["output_folder"]))
        self.assertTrue(path.is_dir())
        path.rmdir()
        self.studio.set_options({"output_folder": ""})
        self.assertEqual(self.studio.api_config()["output_folder"], str(self.root / "output"))
        self.assertNotIn("output_folder", json.loads((self.root / "settings.json").read_text()))

    @unittest.skipUnless(HAVE_ENCODER, "needs an encoder")
    def test_02_rebuild_makes_the_files_again_in_the_new_folder(self):
        self.session({"sound:1:v1": (2.0, 2.5, tp.tone(SR, 0.5, 300, -26))})
        self.studio.process_list("sounds")
        old = self.root / "output" / "phonics" / "alone" / "1.m4a"
        self.assertTrue(old.is_file())
        dest = self.tmp / "elsewhere"
        self.studio.set_options({"output_folder": str(dest)})
        self.assertEqual(self.studio.api_review("sounds")["files"], [])          # the new folder has none yet
        self.assertTrue(old.is_file())                                           # the old files were not touched
        rep = self.studio.process_list("sounds", rebuild=True)
        self.assertEqual(rep["errors"], [])
        self.assertTrue((dest / "phonics" / "alone" / "1.m4a").is_file())
        self.assertEqual(len(self.studio.api_review("sounds")["files"]), 2)

    def test_03_bad_folders_are_refused_and_nothing_changes(self):
        f = self.tmp / "a-file.txt"
        f.write_text("x")
        ro = self.tmp / "read-only"
        ro.mkdir()
        os.chmod(ro, 0o555)
        self.addCleanup(lambda: os.path.exists(ro) and os.chmod(ro, 0o755))
        for bad in ("relative/folder", "Desktop", str(f), str(f / "inside"), str(ro / "sub")):
            with self.assertRaises(studio.StudioError, msg=bad) as cm:
                self.studio.set_options({"output_folder": bad})
            self.assertEqual(cm.exception.status, 400, bad)
        with self.assertRaises(studio.StudioError):
            self.studio.set_options({"output_folder": 123})
        self.assertFalse((self.root / "settings.json").exists())
        self.assertEqual(self.studio.api_config()["output_folder"], str(self.root / "output"))
        self.assertEqual(list(ro.iterdir()), [])                                  # nothing was written there

    def test_04_unusable_folder_at_startup_falls_back(self):
        (self.root / "settings.json").write_text(json.dumps(
            {"output_folder": "/nonexistent-drive/Recordings", "takes_folder": "not/absolute"}))
        st = studio.Studio(self.root, {"website_folder": str(self.site)})
        self.assertEqual(st.api_config()["output_folder"], str(self.root / "output"))
        self.assertEqual(st.takes, self.root / "takes")
        self.assertIn("could not be used", st.settings_error or "")
        self.assertEqual(st.api_lists()["lists"][0]["id"], "sounds")              # and the studio still works

    def test_05_raw_recordings_folder(self):
        raw = self.tmp / "raw-takes"
        self.studio.set_options({"takes_folder": str(raw)})
        self.assertEqual(self.studio.takes, raw)
        sid = self.studio.new_session("sounds", SR)["id"]
        self.assertTrue((raw / (sid + ".wav")).is_file())
        self.assertFalse((self.root / "takes" / (sid + ".wav")).exists())
        self.studio.append_audio(sid, 0, b"\0\0" * 4800)
        self.studio.save_times(sid, {"items": {}})
        # an unfinished recording blocks moving the folder (it would disappear from view)
        with self.assertRaises(studio.StudioError) as cm:
            self.studio.set_options({"takes_folder": str(self.tmp / "raw-2")})
        self.assertEqual(cm.exception.status, 409)
        self.assertIn("Cut and file them now", str(cm.exception))
        self.assertEqual(self.studio.takes, raw)
        self.assertFalse((self.tmp / "raw-2").exists() and any((self.tmp / "raw-2").iterdir()))
        # once it is closed and finished, the folder can change
        self.studio.close_session(sid)
        meta = self.studio._load_meta(sid)
        meta["finished"] = True
        studio.atomic_write_json(raw / (sid + ".json"), meta)
        r = self.studio.set_options({"takes_folder": str(self.tmp / "raw-2")})
        self.assertEqual(self.studio.takes, self.tmp / "raw-2")
        self.assertIn("stay in the old folder", r["notes"][0])
        self.assertTrue((raw / (sid + ".wav")).is_file())                          # nothing moved or deleted

    def test_06_reset_goes_back_to_the_studio_folders(self):
        self.studio.set_options({"output_folder": str(self.tmp / "o1"), "takes_folder": str(self.tmp / "t1")})
        r = self.studio.set_options(None, reset=True)
        self.assertEqual(self.studio.takes, self.root / "takes")
        self.assertEqual(self.studio.api_config()["output_folder"], str(self.root / "output"))
        self.assertEqual(len(r["notes"]), 2)

    # ── the folder chooser ─────────────────────────────────────────────────
    def test_07_choose_folder(self):
        class R:
            def __init__(self, code, out="", err=""):
                self.returncode, self.stdout, self.stderr = code, out, err
        with mock.patch.object(studio.platform, "system", return_value="Darwin"), \
                mock.patch.object(studio.shutil, "which", side_effect=lambda n: "/usr/bin/" + n if n == "osascript" else None):
            with mock.patch.object(studio.subprocess, "run", return_value=R(0, "/Users/me/Desktop/Recordings/\n")) as run:
                self.assertEqual(self.studio.choose_folder("output"), {"cancelled": False, "path": "/Users/me/Desktop/Recordings"})
                cmd = run.call_args.args[0]
                self.assertEqual(cmd[0], "osascript")
                self.assertIn("finished sound files", " ".join(cmd))
            with mock.patch.object(studio.subprocess, "run", return_value=R(1, "", "execution error: User canceled. (-128)")):
                self.assertEqual(self.studio.choose_folder("takes"), {"cancelled": True, "path": None})
            with mock.patch.object(studio.subprocess, "run", return_value=R(0, "/\n")):
                self.assertEqual(self.studio.choose_folder("output")["path"], "/")
            for bad in ("/etc", "output; rm -rf ~", None, 5):
                with self.assertRaises(studio.StudioError, msg=bad):
                    self.studio.choose_folder(bad)                                   # only the two fixed prompts exist
        with mock.patch.object(studio.platform, "system", return_value="Linux"), \
                mock.patch.object(studio.shutil, "which", side_effect=lambda n: "/usr/bin/zenity" if n == "zenity" else None):
            with mock.patch.object(studio.subprocess, "run", return_value=R(0, "/home/mike/rec\n")) as run:
                self.assertEqual(self.studio.choose_folder("output")["path"], "/home/mike/rec")
                self.assertEqual(run.call_args.args[0][0], "zenity")
        with mock.patch.object(studio.platform, "system", return_value="Linux"), mock.patch.object(studio.shutil, "which", return_value=None):
            with self.assertRaises(studio.StudioError) as cm:
                self.studio.choose_folder("output")
            self.assertIn("Type the folder's full path", str(cm.exception))

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
            if body is not None:
                body = json.dumps(body).encode()
                h["Content-Type"] = "application/json"
            c.request(method, path, body=body, headers=h)
            r = c.getresponse()
            data = r.read()
            c.close()
            return r.status, json.loads(data.decode() or "{}")
        return call

    def test_08_routes(self):
        call = self.http()
        dest = self.tmp / "via-http"
        st, r = call("POST", "/api/settings", {"values": {"output_folder": str(dest)}})
        self.assertEqual((st, r["values"]["output_folder"]), (200, str(dest)))
        self.assertEqual(len(r["notes"]), 1)
        st, r = call("GET", "/api/settings")
        self.assertEqual(r["spec"]["output_folder"]["type"], "path")
        st, r = call("POST", "/api/settings", {"values": {"output_folder": "nope"}})
        self.assertEqual(st, 400)
        self.assertIn("full path", r["error"])
        st, cfg = call("GET", "/api/config")
        self.assertEqual((cfg["output_folder"], cfg["takes_folder"]), (str(dest), str(self.root / "takes")))
        self.assertEqual(call("POST", "/api/choose-folder", {"what": "x"})[0], 400)
        self.assertEqual(call("POST", "/api/choose-folder", {"what": "output"}, headers={"Origin": "http://evil.example"})[0], 403)
        self.assertEqual(call("POST", "/api/choose-folder", {"what": "output"}, headers={"Host": "evil.example"})[0], 403)


if __name__ == "__main__":
    unittest.main(verbosity=1)
