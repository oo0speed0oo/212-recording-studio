#!/usr/bin/env python3
"""Offline test of "Put on the site" (step R5) on throwaway folders. Nothing real is touched.

    ~/kokoro-env/bin/python -I tests/test_site.py
"""
import http.client
import json
import os
import shutil
import subprocess
import sys
import threading
import unittest
from http.server import ThreadingHTTPServer
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
import studio  # noqa: E402
import siteput  # noqa: E402
import test_processing as tp  # noqa: E402

ROBOT = {"alone/1.m4a": b"ROBOT-ALONE-1" * 20, "alone/24.m4a": b"ROBOT-ALONE-24" * 20,
         "sounds/24.m4a": b"ROBOT-SOUNDS-24" * 20, "words/cat.m4a": b"ROBOT-CAT" * 20}


def tree(root):
    """Every file under root as {relative path: bytes}."""
    out = {}
    for p in Path(root).rglob("*"):
        if p.is_file():
            out[str(p.relative_to(root))] = p.read_bytes()
    return out


@unittest.skipUnless(tp.HAVE_AFCONVERT, "needs the Mac's afconvert")
class SiteTest(tp.Base):
    def setUp(self):
        super().setUp()
        self.local = self.tmp / "local"
        for root in (self.site, self.local):                       # the website folder and the Local test copy
            for rel, data in ROBOT.items():
                p = root / "portal" / "audio" / "phonics" / rel
                p.parent.mkdir(parents=True, exist_ok=True)
                p.write_bytes(data)
                os.chmod(p, 0o600)
        self.studio = self.new_studio(local_site_folder=str(self.local))
        sr = 48000
        self.session({
            "sound:1:v1": (2.0, 2.5, tp.tone(sr, 0.5, 300, -26)),
            "sound:23:v1": (4.0, 4.4, tp.tone(sr, 0.4, 350, -26)),
            "sound:23:v2": (6.0, 6.5, tp.tone(sr, 0.5, 450, -26)),
            "sound:24:v1": (8.0, 8.4, tp.tone(sr, 0.4, 500, -26)),
        })
        self.rep = self.studio.process_list("sounds")
        self.assertEqual(self.rep["errors"], [])
        self.live = self.site / "portal" / "audio" / "phonics"
        self.loc = self.local / "portal" / "audio" / "phonics"
        self.robots = self.site / "_extra_files" / "robot-voices"

    def ok(self, *rels):
        self.studio.set_review(list(rels), "ok")

    # ── the plan (changes nothing) ─────────────────────────────────────────
    def test_01_plan_changes_nothing_and_only_ok_files_count(self):
        before = (tree(self.site), tree(self.local))
        p = self.studio.api_site_plan()
        self.assertEqual(p["files"], [])
        self.assertEqual(p["counts"]["unchecked"], 7)
        self.assertTrue(p["website_ok"] and p["local_ok"])
        self.ok("alone/1.m4a", "sounds/1.m4a", "alone/24.m4a")
        self.studio.set_review(["alone/23.m4a"], "redo")
        p = self.studio.api_site_plan()
        got = {f["file"]: f["action"] for f in p["files"]}
        self.assertEqual(got, {"alone/1.m4a": "robot", "sounds/1.m4a": "add", "alone/24.m4a": "robot"})
        self.assertEqual(p["summary"], {"add": 1, "robot": 2, "earlier": 0, "same": 0})
        self.assertEqual((p["counts"]["ok"], p["counts"]["redo"], p["counts"]["unchecked"]), (3, 1, 3))
        self.assertEqual(p["pending"], [])
        p2 = self.studio.api_site_plan(include_unchecked=True)       # unchecked files too, but never the Redo one
        self.assertEqual(len(p2["files"]), 6)
        self.assertNotIn("alone/23.m4a", [f["file"] for f in p2["files"]])
        self.assertEqual((tree(self.site), tree(self.local)), before)  # looking changed nothing

    def test_02_nothing_ok_nothing_put(self):
        with self.assertRaises(studio.StudioError) as cm:
            self.studio.site_put()
        self.assertEqual(cm.exception.status, 400)
        self.assertFalse(self.robots.exists())                        # nothing created on the website side
        self.assertEqual(tree(self.site)["portal/audio/phonics/alone/1.m4a"], ROBOT["alone/1.m4a"])

    # ── putting files on ───────────────────────────────────────────────────
    def test_03_put_backs_up_robot_copies_new_and_logs(self):
        self.ok("alone/1.m4a", "sounds/1.m4a", "alone/24.m4a")
        self.studio.set_review(["alone/23.m4a"], "redo")
        before = tree(self.site)
        rep = self.studio.site_put()
        self.assertEqual((rep["added"], rep["replaced"], rep["same"], rep["errors"]), (1, 2, 0, []))
        self.assertEqual(rep["places"], ["website", "local"])
        # new voice in both places, byte for byte
        for rel in ("alone/1.m4a", "sounds/1.m4a", "alone/24.m4a"):
            new = self.out(rel).read_bytes()
            self.assertEqual((self.live / rel).read_bytes(), new, rel)
            self.assertEqual((self.loc / rel).read_bytes(), new, rel)
            self.assertEqual((self.live / rel).stat().st_mode & 0o777, 0o644, rel)
        # the robot voices were MOVED, not deleted
        self.assertEqual((self.robots / "alone" / "1.m4a").read_bytes(), ROBOT["alone/1.m4a"])
        self.assertEqual((self.robots / "alone" / "24.m4a").read_bytes(), ROBOT["alone/24.m4a"])
        self.assertFalse((self.robots / "sounds" / "1.m4a").exists())     # there was no robot file to save
        # files that were not chosen are untouched, in both places
        for rel in ("sounds/24.m4a", "words/cat.m4a"):
            self.assertEqual((self.live / rel).read_bytes(), ROBOT[rel])
            self.assertEqual((self.loc / rel).read_bytes(), ROBOT[rel])
        self.assertFalse((self.live / "alone" / "23.m4a").exists())       # the Redo one was not sent
        # nothing but expected paths changed in the website folder; no temp files left
        after = tree(self.site)
        changed = {k for k in set(before) | set(after) if before.get(k) != after.get(k)}
        self.assertEqual(changed, {
            "portal/audio/phonics/alone/1.m4a", "portal/audio/phonics/alone/24.m4a", "portal/audio/phonics/sounds/1.m4a",
            "_extra_files/robot-voices/alone/1.m4a", "_extra_files/robot-voices/alone/24.m4a",
            "_extra_files/robot-voices/replacements.log"})
        self.assertFalse([k for k in after if k.endswith(".studio-tmp")])
        self.assertFalse([k for k in tree(self.local) if k.endswith(".studio-tmp")])
        # the log says what was done
        log = (self.robots / "replacements.log").read_text(encoding="utf-8")
        self.assertIn("Put 3 file(s) on the site: 1 new, 2 replaced", log)
        self.assertIn("[website] REPLACED alone/1.m4a", log)
        self.assertIn("robot voice saved to _extra_files/robot-voices/alone/1.m4a", log)
        self.assertIn("[website] ADDED  sounds/1.m4a", log)
        self.assertIn("[local]", log)
        # the upload list: exact files, no folders
        lst = (self.root / "site-upload-list.txt").read_text().splitlines()
        self.assertEqual(lst, ["portal/audio/phonics/alone/1.m4a", "portal/audio/phonics/alone/24.m4a",
                               "portal/audio/phonics/sounds/1.m4a"])
        self.assertEqual(rep["pending"], lst)

    def test_04_put_again_changes_nothing_new(self):
        self.ok("alone/1.m4a", "sounds/1.m4a")
        self.studio.site_put()
        snap = tree(self.site)
        logsize = len((self.robots / "replacements.log").read_text())
        rep = self.studio.site_put()
        self.assertEqual((rep["added"], rep["replaced"], rep["same"]), (0, 0, 2))
        after = tree(self.site)
        self.assertEqual({k for k in after if k != "_extra_files/robot-voices/replacements.log"},
                         {k for k in snap if k != "_extra_files/robot-voices/replacements.log"})
        for k in snap:
            if k != "_extra_files/robot-voices/replacements.log":
                self.assertEqual(snap[k], after[k], k)
        self.assertGreater(len((self.robots / "replacements.log").read_text()), logsize)
        self.assertEqual(rep["pending"], ["portal/audio/phonics/alone/1.m4a", "portal/audio/phonics/sounds/1.m4a"])

    def test_05_a_new_version_keeps_the_robot_and_the_earlier_version(self):
        self.ok("alone/1.m4a")
        self.studio.site_put()
        v1 = (self.live / "alone" / "1.m4a").read_bytes()
        self.studio.set_options({"aac_kbps": 96})                  # a different build of the same cut
        self.studio.process_list("sounds", rebuild=True)
        self.ok("alone/1.m4a")
        v2 = self.out("alone/1.m4a").read_bytes()
        self.assertNotEqual(v1, v2)
        p = self.studio.api_site_plan()
        self.assertEqual([f["action"] for f in p["files"] if f["file"] == "alone/1.m4a"], ["earlier"])
        rep = self.studio.site_put()
        self.assertEqual(rep["errors"], [])
        self.assertEqual((self.live / "alone" / "1.m4a").read_bytes(), v2)
        self.assertEqual((self.loc / "alone" / "1.m4a").read_bytes(), v2)
        self.assertEqual((self.robots / "alone" / "1.m4a").read_bytes(), ROBOT["alone/1.m4a"])   # robot still safe
        kept = list((self.robots / "_earlier-versions").rglob("1.m4a"))
        self.assertEqual([k.read_bytes() for k in kept], [v1])      # the website's and Local's v1 were the same: kept once
        self.assertIn("earlier version saved to", (self.robots / "replacements.log").read_text())

    def test_06_include_unchecked_option(self):
        self.ok("alone/1.m4a")
        rep = self.studio.site_put(include_unchecked=False)
        self.assertEqual(rep["tried"], 1)
        rep = self.studio.site_put(include_unchecked=True)
        self.assertEqual(rep["tried"], 7)                          # all 7 files (none is marked Redo here)
        self.assertEqual(rep["errors"], [])
        self.assertTrue((self.live / "alone" / "23-2.m4a").is_file())
        self.assertTrue((self.live / "sounds" / "23.m4a").is_file())

    def test_07_uploaded_clears_the_list(self):
        self.ok("alone/1.m4a", "sounds/1.m4a")
        self.studio.site_put()
        self.assertTrue((self.root / "site-upload-list.txt").exists())
        self.assertEqual(self.studio.site_uploaded(), {"cleared": 2})
        self.assertFalse((self.root / "site-upload-list.txt").exists())
        self.assertEqual(self.studio.api_site_plan()["pending"], [])
        self.assertIn("Marked 2 file(s) as uploaded", (self.robots / "replacements.log").read_text())
        self.assertTrue((self.live / "alone" / "1.m4a").is_file())  # the files themselves stay

    # ── safety ─────────────────────────────────────────────────────────────
    def test_08_no_local_site_is_fine_and_a_wrong_website_folder_changes_nothing(self):
        self.studio = self.new_studio(local_site_folder=str(self.tmp / "no-such-local"))
        self.ok("alone/1.m4a")
        p = self.studio.api_site_plan()
        self.assertTrue(p["website_ok"])
        self.assertFalse(p["local_ok"])
        rep = self.studio.site_put()
        self.assertEqual(rep["places"], ["website"])
        self.assertEqual(tree(self.local)["portal/audio/phonics/alone/1.m4a"], ROBOT["alone/1.m4a"])
        self.assertFalse((self.tmp / "no-such-local").exists())
        # a website folder without portal/audio/phonics: refuse, create nothing
        empty = self.tmp / "empty-site"
        empty.mkdir()
        self.studio = self.new_studio(website_folder=str(empty), local_site_folder=str(self.tmp / "no-such-local"))
        with self.assertRaises(studio.StudioError):
            self.studio.site_put()
        self.assertEqual(list(empty.iterdir()), [])

    def test_09_only_safe_names(self):
        s = siteput.Site(self.site, self.local)
        src = self.out("alone/1.m4a")
        for bad in ("../x.m4a", "alone/../../x.m4a", "alone/x.mp3", "other/x.m4a", "alone/a b.m4a", "/etc/passwd"):
            with self.assertRaises(siteput.SiteError, msg=bad):
                s.plan([(bad, src)])
            with self.assertRaises(siteput.SiteError, msg=bad):
                s.put([(bad, src)])

    def test_10_a_failed_copy_leaves_the_old_file(self):
        self.ok("alone/1.m4a")
        s = siteput.Site(self.site, self.local)
        orig = siteput.shutil.copyfile
        def broken(src, dst, *a, **k):
            Path(dst).write_bytes(b"half")
            return dst
        siteput.shutil.copyfile = broken
        try:
            rep = s.put([("alone/1.m4a", self.out("alone/1.m4a"))])
        finally:
            siteput.shutil.copyfile = orig
        self.assertEqual(len(rep["errors"]), 2)                    # website and local both refused
        self.assertEqual((self.live / "alone" / "1.m4a").read_bytes(), ROBOT["alone/1.m4a"])
        self.assertEqual((self.loc / "alone" / "1.m4a").read_bytes(), ROBOT["alone/1.m4a"])
        self.assertFalse([k for k in tree(self.site) if k.endswith(".studio-tmp")])
        self.assertFalse((self.robots / "alone" / "1.m4a").exists())  # nothing was moved either

    # ── through the web server ─────────────────────────────────────────────
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

    def test_11_routes(self):
        call = self.http()
        self.ok("alone/1.m4a", "sounds/1.m4a")
        st, p = call("GET", "/api/site/plan")
        self.assertEqual(st, 200)
        self.assertEqual(sorted(f["file"] for f in p["files"]), ["alone/1.m4a", "sounds/1.m4a"])
        st, p = call("GET", "/api/site/plan?unchecked=1")
        self.assertEqual(len(p["files"]), 7)
        st, r = call("POST", "/api/site/put", {"include_unchecked": False})
        self.assertEqual((st, r["added"], r["replaced"]), (200, 1, 1))
        self.assertIn("git add --pathspec-from-file", r["git_add"])
        self.assertTrue(r["log_file"].endswith("replacements.log"))
        st, r = call("POST", "/api/site/uploaded", {})
        self.assertEqual((st, r["cleared"]), (200, 2))
        st, r = call("POST", "/api/site/put", {}, headers={"Origin": "http://evil.example"})     # another website cannot do it
        self.assertEqual(st, 403)
        st, r = call("POST", "/api/site/put", {}, headers={"Host": "evil.example"})
        self.assertEqual(st, 403)
        st, r = call("GET", "/api/site/plan", headers={"Host": "evil.example"})
        self.assertEqual(st, 403)

    @unittest.skipUnless(shutil.which("git"), "needs git")
    def test_12_git_add_from_the_upload_list(self):
        def git(*a):
            return subprocess.run(["git", "-C", str(self.site)] + list(a), capture_output=True, text=True, check=True).stdout
        git("init", "-q")
        git("config", "user.email", "t@example.com")
        git("config", "user.name", "t")
        (self.site / ".gitignore").write_text("_extra_files/\n")
        git("add", "-A")
        git("commit", "-qm", "base")
        self.ok("alone/1.m4a", "sounds/1.m4a")
        rep = self.studio.site_put()
        subprocess.run(["git", "-C", str(self.site), "add", "--pathspec-from-file=" + rep["upload_list_file"]],
                       check=True, capture_output=True)
        staged = sorted(git("diff", "--cached", "--name-only").split())
        self.assertEqual(staged, ["portal/audio/phonics/alone/1.m4a", "portal/audio/phonics/sounds/1.m4a"])
        self.assertEqual(git("status", "--porcelain", "--", "_extra_files").strip(), "")    # robot backups stay out of git


if __name__ == "__main__":
    unittest.main(verbosity=1)
