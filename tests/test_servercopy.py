#!/usr/bin/env python3
"""Offline test of step R8 part 2: copy to a server or another folder. Real rsync on throwaway folders, and pretend
ssh / rsync programs for the server cases (no network, no server needed).

    ~/kokoro-env/bin/python -I tests/test_servercopy.py
"""
import http.client
import json
import os
import shutil
import stat
import sys
import tempfile
import threading
import unittest
from http.server import ThreadingHTTPServer
from pathlib import Path
from unittest import mock

sys.path.insert(0, str(Path(__file__).resolve().parent))
sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
import servercopy  # noqa: E402
import studio  # noqa: E402
import test_processing as tp  # noqa: E402

HAVE_RSYNC = shutil.which("rsync") is not None
HOST, FOLDER = "mike@100.79.65.95", "/mnt/data/212-recordings"


def write(p, text):
    p = Path(p)
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_text(text)
    return p


def tree(root):
    return {str(p.relative_to(root)): p.read_text() for p in Path(root).rglob("*") if p.is_file()}


class ValidationTest(unittest.TestCase):
    def test_01_good_and_bad_input(self):
        for host in ("mike@100.79.65.95", "home-server", "mike@home-server.tail1234.ts.net", "192.168.1.5", "user@[fe80::1]"):
            self.assertEqual(servercopy.validate(host, FOLDER)[0], host)
        for host in ("-oProxyCommand=evil", "a b", "a;b", "$(x)", "a`b`", "mike@host && rm", "a|b", "a\nb", "--help"):
            with self.assertRaises(servercopy.CopyError, msg=host):
                servercopy.validate(host, FOLDER)
        for folder in ("", "relative/dir", "/with space/x", "/a;b", "/a$(x)", "/a`b`", "/a&b", "/a|b", "/a'b"):
            with self.assertRaises(servercopy.CopyError, msg=folder):
                servercopy.validate(HOST, folder)
        with self.assertRaises(servercopy.CopyError):
            servercopy.validate("", "relative/dir")                 # a local folder must be a full path too
        self.assertEqual(servercopy.validate("", "~/Backup")[1], "~/Backup")
        with self.assertRaises(servercopy.CopyError):
            servercopy.validate(HOST, FOLDER, "/no/such/key")
        with self.assertRaises(servercopy.CopyError):
            servercopy.validate(HOST, FOLDER, "/some key with spaces")
        with tempfile.NamedTemporaryFile() as k:
            self.assertEqual(servercopy.validate(HOST, FOLDER, k.name)[2], k.name)

    def test_02_the_command_never_deletes_and_never_asks_for_a_password(self):
        sc = servercopy.ServerCopy(HOST, FOLDER, rsync="/usr/bin/rsync", ssh="/usr/bin/ssh")
        cmd = sc.rsync_cmd("/Users/me/studio/output", "output", "2026-11-16_14-00-00")
        self.assertEqual(cmd[0], "/usr/bin/rsync")
        self.assertFalse([a for a in cmd if a.startswith(("--delete", "--remove", "--del", "-D"))], cmd)
        self.assertNotIn("--force", cmd)
        self.assertIn("--backup", cmd)
        self.assertIn("--backup-dir=%s/_replaced/2026-11-16_14-00-00/output" % FOLDER, cmd)
        e = cmd[cmd.index("-e") + 1]
        self.assertIn("BatchMode=yes", e)                           # never a password prompt
        self.assertNotIn("StrictHostKeyChecking", e)               # never trusts a new server by itself
        self.assertNotIn("-n", cmd)
        i = cmd.index("--")
        self.assertEqual(cmd[i + 1:], ["/Users/me/studio/output/", "%s:%s/output/" % (HOST, FOLDER)])   # after "--": no option injection
        self.assertIn("-n", sc.rsync_cmd("/x", "output", "s", dry=True))
        with tempfile.NamedTemporaryFile() as k:
            sc = servercopy.ServerCopy(HOST, FOLDER, k.name, rsync="r", ssh="s")
            self.assertIn("-i " + k.name, sc.rsync_cmd("/x", "output", "s")[sc.rsync_cmd("/x", "output", "s").index("-e") + 1])
        sc = servercopy.ServerCopy("", "/tmp/backup", rsync="r")      # a local folder: no ssh at all
        cmd = sc.rsync_cmd("/x", "output", "s")
        self.assertNotIn("-e", cmd)
        self.assertEqual(cmd[-1], "/tmp/backup/output/")

    def test_03_itemize_parsing(self):
        lines = [">f+++++++++ phonics/alone/1.m4a", ">f.st...... phonics/alone/2.m4a", "cd+++++++++ phonics/", ".d..t...... ./",
                 "<f+++++++ x y.m4a", "sending incremental file list", ">f+++++++ b.m4a"]
        self.assertEqual(servercopy.ServerCopy.parse_itemize(lines),
                         [("phonics/alone/1.m4a", True), ("phonics/alone/2.m4a", False), ("x y.m4a", True), ("b.m4a", True)])

    def test_04_ssh_messages_are_plain(self):
        f = servercopy.friendly_ssh_error
        self.assertIn("ssh-copy-id mike@h", f("mike@h: Permission denied (publickey).", "mike@h"))
        self.assertIn("ssh mike@h", f("Host key verification failed.", "mike@h"))
        self.assertIn("Tailscale", f("ssh: Could not resolve hostname h: nodename nor servname provided", "h"))
        self.assertIn("Tailscale", f("ssh: connect to host h port 22: Operation timed out", "h"))
        self.assertIn("not running", f("ssh: connect to host h port 22: Connection refused", "h"))


@unittest.skipUnless(HAVE_RSYNC, "needs rsync")
class RealRsyncTest(unittest.TestCase):
    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp(prefix="studio-copy-")).resolve()
        self.src = self.tmp / "output"
        write(self.src / "phonics/alone/1.m4a", "alone-1")
        write(self.src / "phonics/sounds/1.m4a", "sounds-1")
        write(self.src / "lists/c-fruit/apple.m4a", "apple")
        write(self.src / ".DS_Store", "junk")
        write(self.src / "phonics/alone/9.m4a.studio-tmp", "half")
        self.dest = self.tmp / "server-folder"
        self.sc = servercopy.ServerCopy("", str(self.dest))
        self.logs = self.tmp / "logs"

    def tearDown(self):
        shutil.rmtree(self.tmp, ignore_errors=True)

    def copy(self, **kw):
        return self.sc.copy([("output", self.src)], log_dir=self.logs, **kw)

    def test_01_first_copy_then_nothing_new(self):
        r = self.copy()
        self.assertEqual((r["new"], r["updated"], r["unchanged"]), (3, 0, 0))
        self.assertEqual(sorted(r["files"]), ["output/lists/c-fruit/apple.m4a", "output/phonics/alone/1.m4a", "output/phonics/sounds/1.m4a"])
        self.assertEqual(tree(self.dest / "output"), {"phonics/alone/1.m4a": "alone-1", "phonics/sounds/1.m4a": "sounds-1", "lists/c-fruit/apple.m4a": "apple"})
        self.assertEqual(r["bytes"], len("alone-1") + len("sounds-1") + len("apple"))
        r = self.copy()
        self.assertEqual((r["new"], r["updated"], r["unchanged"], r["files"]), (0, 0, 3, []))
        self.assertFalse((self.dest / "_replaced").exists())                    # nothing was replaced, nothing was kept aside

    def test_02_a_redone_sound_replaces_the_old_one_which_is_kept(self):
        self.copy()
        write(self.src / "phonics/alone/1.m4a", "alone-1-REDONE-longer")
        r = self.copy()
        self.assertEqual((r["new"], r["updated"], r["unchanged"]), (0, 1, 2))
        self.assertEqual((self.dest / "output/phonics/alone/1.m4a").read_text(), "alone-1-REDONE-longer")
        kept = list((self.dest / "_replaced").rglob("1.m4a"))
        self.assertEqual([k.read_text() for k in kept], ["alone-1"])            # the old version is still on the server
        self.assertIn("_replaced/%s/output/phonics/alone/1.m4a" % r["stamp"], str(kept[0]))

    def test_03_nothing_is_ever_deleted(self):
        self.copy()
        write(self.dest / "output/phonics/alone/extra-by-someone.m4a", "keep me")     # a file only the server has
        write(self.dest / "my-notes.txt", "keep me too")
        (self.src / "phonics/sounds/1.m4a").unlink()                                   # removed here
        shutil.rmtree(self.src / "lists")                                              # a whole folder removed here
        self.copy()
        self.assertEqual((self.dest / "output/phonics/alone/extra-by-someone.m4a").read_text(), "keep me")
        self.assertEqual((self.dest / "my-notes.txt").read_text(), "keep me too")
        self.assertEqual((self.dest / "output/phonics/sounds/1.m4a").read_text(), "sounds-1")
        self.assertEqual((self.dest / "output/lists/c-fruit/apple.m4a").read_text(), "apple")

    def test_04_preview_changes_nothing(self):
        r = self.copy(dry=True)
        self.assertTrue(r["dry"])
        self.assertEqual((r["new"], r["updated"]), (3, 0))
        self.assertFalse(self.dest.exists())
        self.copy()
        write(self.src / "phonics/alone/1.m4a", "changed!!")
        before = tree(self.dest)
        r = self.copy(dry=True)
        self.assertEqual((r["new"], r["updated"]), (0, 1))
        self.assertEqual(tree(self.dest), before)
        self.assertFalse((self.dest / "_replaced").exists())

    def test_05_junk_is_not_copied_and_the_log_is_written(self):
        self.copy()
        got = tree(self.dest)
        self.assertFalse([k for k in got if k.endswith((".DS_Store", ".studio-tmp"))])
        log = (self.logs / "server-copy.log").read_text()
        self.assertIn("COPY to %s: 3 new, 0 replaced, 0 unchanged" % self.dest, log)
        self.assertIn("output/phonics/alone/1.m4a", log)
        self.copy(dry=True)
        self.assertIn("PREVIEW", (self.logs / "server-copy.log").read_text())

    def test_06_several_parts_and_missing_parts(self):
        wavs, takes = self.tmp / "output-wav", self.tmp / "takes"
        write(wavs / "cuts/sounds/sound-1-v1.wav", "wav")
        write(takes / "2026-11-16_14-00-00_sounds.wav", "raw")
        r = self.sc.copy([("output", self.src), ("output-wav", wavs), ("takes", takes), ("nothing", self.tmp / "no-such-folder")])
        self.assertEqual(r["parts"], ["output", "output-wav", "takes"])
        self.assertEqual(r["new"], 5)
        self.assertEqual((self.dest / "output-wav/cuts/sounds/sound-1-v1.wav").read_text(), "wav")
        self.assertEqual((self.dest / "takes/2026-11-16_14-00-00_sounds.wav").read_text(), "raw")

    def test_07_a_failure_is_reported_in_words(self):
        bad = servercopy.ServerCopy("", "/proc/not-writable-here")
        with self.assertRaises(servercopy.CopyError):
            bad.copy([("output", self.src)])
        self.assertEqual(servercopy.ServerCopy("", str(self.dest)).test()["ok"], True)
        self.assertEqual(servercopy.ServerCopy("", "/proc/nope/x").test()["ok"], False)


FAKE_SSH = """#!/bin/bash
echo "$@" >> "$FAKE_LOG/ssh.log"
[ -n "$FAKE_ERR" ] && { echo "$FAKE_ERR" >&2; exit ${FAKE_CODE:-255}; }
[ "$FAKE_NOWRITE" = "1" ] && exit 0
echo STUDIO-OK
"""
FAKE_RSYNC = """#!/bin/bash
echo "$@" >> "$FAKE_LOG/rsync.log"
[ -n "$FAKE_ERR" ] && { echo "$FAKE_ERR" >&2; exit ${FAKE_CODE:-255}; }
printf '%s\\n' "sending incremental file list" "cd+++++++++ phonics/" ">f+++++++++ phonics/alone/1.m4a" ">f.st...... phonics/alone/2.m4a"
"""


class PretendServerTest(unittest.TestCase):
    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp(prefix="studio-fake-")).resolve()
        for name, body in (("ssh", FAKE_SSH), ("rsync", FAKE_RSYNC)):
            p = self.tmp / name
            p.write_text(body)
            p.chmod(p.stat().st_mode | stat.S_IEXEC)
        self.env = mock.patch.dict(os.environ, {"FAKE_LOG": str(self.tmp)})
        self.env.start()
        self.src = self.tmp / "output"
        write(self.src / "phonics/alone/1.m4a", "a")
        write(self.src / "phonics/alone/2.m4a", "bb")
        self.sc = servercopy.ServerCopy(HOST, FOLDER, rsync=str(self.tmp / "rsync"), ssh=str(self.tmp / "ssh"))

    def tearDown(self):
        self.env.stop()
        shutil.rmtree(self.tmp, ignore_errors=True)

    def test_01_connection_test(self):
        r = self.sc.test()
        self.assertEqual(r["ok"], True, r)
        line = (self.tmp / "ssh.log").read_text()
        self.assertIn("BatchMode=yes", line)
        self.assertIn("-- %s mkdir -p %s && test -w %s" % (HOST, FOLDER, FOLDER), line)
        with mock.patch.dict(os.environ, {"FAKE_NOWRITE": "1"}):
            r = self.sc.test()
            self.assertFalse(r["ok"])
            self.assertIn("cannot write", r["message"])
        for err, word in (("mike@x: Permission denied (publickey).", "ssh-copy-id"), ("Host key verification failed.", "fingerprint"),
                          ("ssh: Could not resolve hostname x", "Tailscale"), ("ssh: connect to host x port 22: Connection timed out", "Tailscale")):
            with mock.patch.dict(os.environ, {"FAKE_ERR": err}):
                r = self.sc.test()
                self.assertFalse(r["ok"], err)
                self.assertIn(word, r["message"], err)

    def test_02_copy_counts_and_command(self):
        r = self.sc.copy([("output", self.src)], log_dir=self.tmp / "logs")
        self.assertEqual((r["new"], r["updated"], r["unchanged"]), (1, 1, 0))
        self.assertEqual(r["files"], ["output/phonics/alone/1.m4a", "output/phonics/alone/2.m4a"])
        self.assertEqual(r["where"], "%s:%s" % (HOST, FOLDER))
        args = (self.tmp / "rsync.log").read_text()
        self.assertIn("%s:%s/output/" % (HOST, FOLDER), args)
        self.assertNotIn("--delete", args)
        self.assertIn("--backup-dir=%s/_replaced/" % FOLDER, args)

    def test_03_copy_failure_is_in_words(self):
        with mock.patch.dict(os.environ, {"FAKE_ERR": "mike@x: Permission denied (publickey)."}):
            with self.assertRaises(servercopy.CopyError) as cm:
                self.sc.copy([("output", self.src)])
            self.assertIn("ssh-copy-id", str(cm.exception))
            self.assertIn("output part", str(cm.exception))


@unittest.skipUnless(HAVE_RSYNC, "needs rsync")
class StudioTest(tp.Base):
    def setUp(self):
        super().setUp()
        self.dest = self.tmp / "my-server"
        out = self.studio.proc.out_root
        write(out / "phonics/alone/1.m4a", "alone-1")
        write(out / "phonics/sounds/1.m4a", "sounds-1")
        write(self.studio.takes / "2026-11-16_14-00-00_sounds.wav", "raw")

    def http(self):
        httpd = ThreadingHTTPServer(("127.0.0.1", 0), studio.make_handler(self.studio, 0))
        port = httpd.server_address[1]
        httpd.RequestHandlerClass = studio.make_handler(self.studio, port)
        httpd.daemon_threads = True
        threading.Thread(target=httpd.serve_forever, daemon=True).start()
        self.addCleanup(lambda: (httpd.shutdown(), httpd.server_close()))

        def call(method, path, body=None, headers=None):
            c = http.client.HTTPConnection("127.0.0.1", port, timeout=30)
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

    def test_01_settings_are_checked_before_saving(self):
        for bad in ({"server_host": "-oProxyCommand=x", "server_folder": FOLDER}, {"server_host": HOST, "server_folder": "rel/dir"},
                    {"server_host": HOST}, {"server_folder": "relative"}, {"server_host": HOST, "server_folder": FOLDER, "server_key": "/no/key"},
                    {"server_what": "everything"}):
            with self.assertRaises(studio.StudioError, msg=bad) as cm:
                self.studio.set_options(bad)
            self.assertEqual(cm.exception.status, 400, bad)
        self.assertFalse((self.root / "settings.json").exists())
        self.assertFalse(self.studio.api_config()["server_ready"])
        r = self.studio.set_options({"server_host": HOST, "server_folder": FOLDER})
        self.assertEqual((r["values"]["server_host"], r["values"]["server_what"]), (HOST, "finished"))
        c = self.studio.api_config()
        self.assertEqual((c["server_ready"], c["server_where"]), (True, "%s:%s" % (HOST, FOLDER)))
        self.assertEqual(self.new_studio().api_config()["server_where"], "%s:%s" % (HOST, FOLDER))   # remembered
        self.studio.set_options(None, reset=True)
        self.assertFalse(self.studio.api_config()["server_ready"])

    def test_02_not_set_up(self):
        with self.assertRaises(studio.StudioError) as cm:
            self.studio.server_copy()
        self.assertEqual(cm.exception.status, 400)
        self.assertIn("Copy to my server", str(cm.exception))

    def test_03_copy_to_another_folder_preview_then_copy(self):
        self.studio.set_options({"server_folder": str(self.dest)})
        r = self.studio.server_copy(dry=True)
        self.assertEqual((r["dry"], r["new"]), (True, 2))
        self.assertFalse(self.dest.exists())
        r = self.studio.server_copy()
        self.assertEqual((r["new"], r["updated"], r["parts"]), (2, 0, ["output"]))
        self.assertEqual(tree(self.dest), {"output/phonics/alone/1.m4a": "alone-1", "output/phonics/sounds/1.m4a": "sounds-1"})
        self.assertTrue((self.root / "server-copy.log").is_file())
        self.studio.set_options({"server_what": "all"})
        r = self.studio.server_copy()
        self.assertEqual(r["parts"], ["output", "takes"])                       # output-wav has nothing yet: skipped
        self.assertEqual((r["new"], r["unchanged"]), (1, 2))
        self.assertEqual((self.dest / "takes/2026-11-16_14-00-00_sounds.wav").read_text(), "raw")

    def test_04_only_one_copy_at_a_time(self):
        self.studio.set_options({"server_folder": str(self.dest)})
        self.assertTrue(self.studio.copy_lock.acquire(blocking=False))
        try:
            with self.assertRaises(studio.StudioError) as cm:
                self.studio.server_copy()
            self.assertEqual(cm.exception.status, 409)
        finally:
            self.studio.copy_lock.release()
        self.assertTrue(self.studio.proc_lock.acquire(blocking=False))          # the studio is cutting files
        try:
            with self.assertRaises(studio.StudioError) as cm:
                self.studio.server_copy()
            self.assertEqual(cm.exception.status, 409)
        finally:
            self.studio.proc_lock.release()
        self.assertTrue(self.studio.copy_lock.acquire(blocking=False))          # and the lock was given back each time
        self.studio.copy_lock.release()

    def test_05_routes(self):
        call = self.http()
        st, r = call("POST", "/api/server/copy", {})
        self.assertEqual(st, 400)
        st, r = call("POST", "/api/server/test", {"host": "", "folder": str(self.dest)})        # typed values, not saved yet
        self.assertEqual((st, r["ok"]), (200, True))
        st, r = call("POST", "/api/server/test", {"host": "bad host", "folder": FOLDER})
        self.assertEqual(st, 400)
        st, r = call("POST", "/api/settings", {"values": {"server_folder": str(self.dest)}})
        self.assertEqual(st, 200)
        st, r = call("POST", "/api/server/copy", {"dry": True})
        self.assertEqual((st, r["dry"], r["new"]), (200, True, 2))
        st, r = call("POST", "/api/server/copy", {})
        self.assertEqual((st, r["new"]), (200, 2))
        for hdr in ({"Origin": "http://evil.example"}, {"Host": "evil.example"}):
            self.assertEqual(call("POST", "/api/server/copy", {}, headers=hdr)[0], 403)
            self.assertEqual(call("POST", "/api/server/test", {}, headers=hdr)[0], 403)


if __name__ == "__main__":
    unittest.main(verbosity=1)
