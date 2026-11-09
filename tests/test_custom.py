#!/usr/bin/env python3
"""Offline test of your own lists, the optional website folder and the output folder (step R6). Throwaway folders.

    ~/kokoro-env/bin/python -I tests/test_custom.py
"""
import http.client
import json
import sys
import threading
import unittest
from http.server import ThreadingHTTPServer
from pathlib import Path
from unittest import mock

sys.path.insert(0, str(Path(__file__).resolve().parent))
sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
import studio  # noqa: E402
import test_processing as tp  # noqa: E402

SR = 48000


class CustomTest(tp.Base):
    def lists_dir(self):
        d = self.root / "lists"
        d.mkdir(exist_ok=True)
        return d

    def write(self, name, text, bom=False):
        (self.lists_dir() / name).write_bytes((("﻿" if bom else "") + text).encode("utf-8"))

    # ── reading CSV files ──────────────────────────────────────────────────
    def test_01_csv_shapes(self):
        self.write("a-name-text.csv", "name,text\nhello,Hello there\nbye,Goodbye\n", bom=True)
        self.write("b-text-only.csv", "text\nOne apple\nTwo pears\n\n,\nThree plums\n")
        self.write("c-no-header-one-col.csv", "red\ngreen\nblue\n")
        self.write("d-no-header-two-col.csv", "r,Red one\ng,Green one\n")
        self.write("e-dupes.csv", "text\nHello!\nHello?\nhello\n")
        self.write("f-japanese.csv", "text\nこんにちは\nありがとう\n")
        self.write("g-bad-names.csv", "name,text\n../evil,One\nspace name!,Two\n,Three\n")
        L = self.studio.custom_lists()
        ids = {v["file"]: k for k, v in L.items()}
        self.assertEqual(sorted(ids), ["a-name-text.csv", "b-text-only.csv", "c-no-header-one-col.csv", "d-no-header-two-col.csv",
                                       "e-dupes.csv", "f-japanese.csv", "g-bad-names.csv"])
        names = lambda f: [(i["name"], i["text"]) for i in L[ids[f]]["items"]]
        self.assertEqual(names("a-name-text.csv"), [("hello", "Hello there"), ("bye", "Goodbye")])
        self.assertEqual(names("b-text-only.csv"), [("one-apple", "One apple"), ("two-pears", "Two pears"), ("three-plums", "Three plums")])
        self.assertEqual([n for n, _ in names("c-no-header-one-col.csv")], ["red", "green", "blue"])
        self.assertEqual(names("d-no-header-two-col.csv"), [("r", "Red one"), ("g", "Green one")])
        self.assertEqual([n for n, _ in names("e-dupes.csv")], ["hello", "hello-2", "hello-3"])
        self.assertEqual([n for n, _ in names("f-japanese.csv")], ["item-001", "item-002"])
        self.assertEqual([n for n, _ in names("g-bad-names.csv")], ["evil", "space-name", "three"])
        for conf in L.values():                                     # every file name is safe, every key unique
            self.assertTrue(all(studio.SAFE_NAME.match(i["name"]) for i in conf["items"]))
        allkeys = [i["key"] for c in L.values() for i in c["items"]]
        self.assertEqual(len(allkeys), len(set(allkeys)))
        self.assertEqual(L[ids["a-name-text.csv"]]["items"][0]["key"], "item:%s:hello" % ids["a-name-text.csv"])
        self.assertEqual(L[ids["a-name-text.csv"]]["items"][0]["rel"], "lists/%s/hello.m4a" % ids["a-name-text.csv"])
        self.assertEqual(L[ids["a-name-text.csv"]]["title"], "a-name-text")

    def test_02_problems_and_hidden_files(self):
        self.write("empty.csv", "")
        self.write("no-text.csv", "text\n\n\n")
        self.write("_example.csv", "text\nhidden\n")
        self.write(".hidden.csv", "text\nhidden\n")
        self.write("notes.txt", "text\nnot a list\n")
        self.write("good.csv", "text\nfine\n")
        L = self.studio.custom_lists()
        self.assertEqual([v["file"] for v in L.values()], ["good.csv"])
        self.assertEqual(len(self.studio.list_problems), 2)
        self.assertTrue(any("empty.csv" in p for p in self.studio.list_problems))
        self.assertTrue(any("no-text.csv" in p for p in self.studio.list_problems))
        self.assertEqual(len(self.studio.api_lists()["problems"]), 2)

    def test_03_behaves_like_words_or_sentences(self):
        self.write("short.csv", "text\nred\ngreen apple\nblue\n")
        self.write("long.csv", "text\nI like red apples a lot\nShe has two small cats\nHe runs\n")
        L = {v["file"]: v for v in self.studio.custom_lists().values()}
        self.assertEqual(L["short.csv"]["kind"], "words")
        self.assertEqual(L["long.csv"]["kind"], "sentences")
        self.assertEqual(L["long.csv"]["items"][0]["words"], 6)

    # ── the website folder is optional ─────────────────────────────────────
    def test_04_without_the_website(self):
        self.write("mine.csv", "text\nhello\n")
        lid = next(iter(self.studio.custom_lists()))
        ids = lambda st: [c["id"] for c in st.api_lists()["lists"]]
        self.assertEqual(ids(self.studio), ["sounds", "words", "sentences", "common", lid])
        self.assertTrue(self.studio.api_lists()["website_ok"])
        friend = self.new_studio(website_folder=str(self.tmp / "no-website-here"))
        self.assertEqual(ids(friend), [lid])
        self.assertFalse(friend.api_lists()["website_ok"])
        cfg = friend.api_config()
        self.assertFalse(cfg["website_ok"])
        self.assertFalse(cfg["website_explicit"])
        self.assertEqual(friend.api_list(lid)["kind"], "words")
        # nothing at all: an empty list of lists, not an error
        for p in (self.root / "lists").glob("*.csv"):
            p.unlink()
        self.assertEqual(friend.api_lists()["lists"], [])

    def test_05_website_explicit_flag(self):
        (self.root / "settings.json").write_text(json.dumps({"website_folder": str(self.tmp / "gone")}))
        st = studio.Studio(self.root)
        self.assertTrue(st.api_config()["website_explicit"])
        (self.root / "settings.json").write_text("{}")
        self.assertFalse(studio.Studio(self.root).api_config()["website_explicit"])

    # ── recording, cutting, filing ─────────────────────────────────────────
    def record_two(self, st=None):
        self.write("My words.csv", "name,text\nhello,Hello\nworld,World\n")
        st = st or self.studio
        lid = next(iter(st.custom_lists()))
        sid = st.new_session(lid, SR)["id"]
        import numpy as np
        rng = np.random.default_rng(9)
        x = rng.standard_normal(SR * 10)
        x = x / np.sqrt(np.mean(x ** 2)) * 10 ** (-60 / 20)
        for start, dur, f in ((2.0, 0.6, 300), (5.0, 0.7, 400)):
            sig = tp.tone(SR, dur, f, -26)
            x[int(start * SR):int(start * SR) + len(sig)] += sig
        st.append_audio(sid, 0, (np.clip(x, -1, 1) * 32767).astype("<i2").tobytes())
        st.save_times(sid, {"items": {"item:%s:hello" % lid: {"start": 2.0, "end": 2.6, "wall": tp.T0 + 1},
                                      "item:%s:world" % lid: {"start": 5.0, "end": 5.7, "wall": tp.T0 + 2}}})
        st.close_session(sid)
        return lid

    @unittest.skipUnless(tp.HAVE_AFCONVERT, "needs the Mac's afconvert")
    def test_06_cut_and_file_your_own_list(self):
        lid = self.record_two()
        self.assertEqual(lid, "c-my-words")
        rep = self.studio.process_list(lid)
        self.assertEqual(rep["errors"], [], rep["errors"])
        self.assertEqual(sorted(f["file"] for f in rep["made"]), ["lists/c-my-words/hello.m4a", "lists/c-my-words/world.m4a"])
        out = self.root / "output" / "lists" / "c-my-words"
        self.assertTrue((out / "hello.m4a").stat().st_size > 500 and (out / "world.m4a").stat().st_size > 500)
        self.assertEqual(rep["output_dir"], str(out.resolve()))
        self.assertFalse((self.root / "output" / "phonics" / "lists").exists())      # the website's layout is not mixed up
        self.assertEqual(sorted(self.studio.progress_done()), ["item:c-my-words:hello", "item:c-my-words:world"])
        self.assertEqual(self.studio.api_list(lid)["done"].keys(), self.studio.progress_done().keys())
        card = {c["id"]: c for c in self.studio.api_lists()["lists"]}[lid]
        self.assertEqual((card["total"], card["done"], card["unchecked"], card["custom"]), (2, 2, 2, True))
        r = self.studio.api_review(lid)
        self.assertEqual([f["file"] for f in r["files"]], ["lists/c-my-words/hello.m4a", "lists/c-my-words/world.m4a"])
        self.assertTrue(all(f["robot"] is False for f in r["files"]))
        self.assertEqual(r["files"][0]["title"], "Hello")
        self.studio.set_review(["lists/c-my-words/world.m4a"], "redo")
        self.assertEqual(self.studio.api_list(lid)["redo"], ["item:c-my-words:world"])
        # rebuild from the saved cuts
        rep = self.studio.process_list(lid, rebuild=True)
        self.assertEqual((rep["errors"], len(rep["made"])), ([], 2))

    @unittest.skipUnless(tp.HAVE_AFCONVERT, "needs the Mac's afconvert")
    def test_07_own_output_folder(self):
        friend_out = self.tmp / "friend files"
        st = self.new_studio(output_folder=str(friend_out))
        lid = self.record_two(st)
        rep = st.process_list(lid)
        self.assertEqual(rep["errors"], [])
        self.assertTrue((friend_out / "lists" / lid / "hello.m4a").is_file())
        self.assertFalse((self.root / "output").exists())
        self.assertEqual(st.api_config()["output_folder"], str(friend_out))
        self.assertEqual(st.api_review(lid)["files"][0]["file"], "lists/%s/hello.m4a" % lid)

    @unittest.skipUnless(tp.HAVE_AFCONVERT, "needs the Mac's afconvert")
    def test_08_start_over_and_the_site_ignore_your_lists(self):
        lid = self.record_two()
        self.studio.process_list(lid)
        self.studio.set_review(["lists/c-my-words/hello.m4a"], "ok")
        p = self.studio.api_site_plan()
        self.assertEqual((p["files"], p["counts"]["missing"]), ([], 0))     # never offered to the site
        # the website's own sounds are untouched by starting over this list
        sound_session = self.session({"sound:1:v1": (2.0, 2.5, tp.tone(SR, 0.5, 300, -26))})
        self.studio.process_list("sounds")
        r = self.studio.reset_list(lid, everything=True)
        self.assertTrue(r["folder"])
        self.assertFalse((self.root / "output" / "lists" / lid / "hello.m4a").exists())
        old = Path(r["folder"]) / "output" / "lists" / lid / "hello.m4a"
        self.assertTrue(old.is_file())                                      # moved, not deleted
        self.assertTrue((self.root / "output" / "phonics" / "alone" / "1.m4a").is_file())
        self.assertEqual(list(self.studio.progress_done()), ["sound:1:v1"])

    # ── making a list from pasted lines ────────────────────────────────────
    def test_09_new_list(self):
        r = self.studio.new_list("Fruit & veg", "apple\nred pepper\n\n  green beans  \nりんご\n")
        self.assertEqual((r["ok"], r["items"], r["list"]), (True, 4, "c-fruit-veg"))
        f = self.root / "lists" / "Fruit & veg.csv"
        self.assertTrue(f.is_file())
        self.assertEqual(f.read_text(encoding="utf-8-sig").splitlines(),
                         ["name,text", "apple,apple", "red-pepper,red pepper", "green-beans,green beans", "item-004,りんご"])
        items = self.studio.custom_lists()["c-fruit-veg"]["items"]
        self.assertEqual([i["name"] for i in items], ["apple", "red-pepper", "green-beans", "item-004"])
        with self.assertRaises(studio.StudioError) as cm:
            self.studio.new_list("Fruit & veg", "x")
        self.assertEqual(cm.exception.status, 409)
        for title, text in (("", "x"), ("   ", "x"), ("_hidden", "x"), ("ok", ""), ("ok", "\n  \n")):
            with self.assertRaises(studio.StudioError, msg=(title, text)) as cm:
                self.studio.new_list(title, text)
            self.assertEqual(cm.exception.status, 400)
        r = self.studio.new_list("../../escape", "one")                 # a name can never leave the lists folder
        self.assertEqual(Path(r["file"]).parent, (self.root / "lists").resolve())
        self.assertFalse((self.tmp / "escape.csv").exists())
        # editing the text later keeps the file name (it is written in the CSV)
        f.write_text("name,text\napple,An apple\nred-pepper,red pepper\n", encoding="utf-8-sig")
        self.assertEqual(self.studio.custom_lists()["c-fruit-veg"]["items"][0]["key"], "item:c-fruit-veg:apple")

    def test_10_open_folder_is_fixed(self):
        with mock.patch.object(studio.subprocess, "Popen") as popen, mock.patch.object(studio.shutil, "which", return_value="/usr/bin/open"):
            self.studio.open_folder("output")
            self.studio.open_folder("lists")
            self.assertEqual([c.args[0] for c in popen.call_args_list],
                             [["/usr/bin/open", str(self.root.resolve() / "output")], ["/usr/bin/open", str(self.root.resolve() / "lists")]])
            with self.assertRaises(studio.StudioError):
                self.studio.open_folder("/etc")

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
            return r.status, data
        return call

    @unittest.skipUnless(tp.HAVE_AFCONVERT, "needs the Mac's afconvert")
    def test_11_routes(self):
        call = self.http()
        st, d = call("POST", "/api/lists/new", {"title": "Hello list", "text": "hello\nworld"})
        self.assertEqual(st, 200)
        self.assertEqual(json.loads(d)["list"], "c-hello-list")
        st, d = call("POST", "/api/lists/new", {"title": "Hello list", "text": "x"})
        self.assertEqual(st, 409)
        st, d = call("POST", "/api/lists/new", {"title": "Other", "text": "x"}, headers={"Origin": "http://evil.example"})
        self.assertEqual(st, 403)
        st, d = call("GET", "/api/lists")
        self.assertIn("c-hello-list", [c["id"] for c in json.loads(d)["lists"]])
        st, d = call("GET", "/api/list/c-hello-list")
        self.assertEqual((st, len(json.loads(d)["items"])), (200, 2))
        st, d = call("GET", "/api/review/c-hello-list")
        self.assertEqual(st, 200)
        # audio of a finished file of your own list
        sid = self.studio.new_session("c-hello-list", SR)["id"]
        import numpy as np
        x = np.zeros(SR * 4)
        sig = tp.tone(SR, 0.6, 300, -26)
        x[SR * 2:SR * 2 + len(sig)] += sig
        self.studio.append_audio(sid, 0, (x * 32767).astype("<i2").tobytes())
        self.studio.save_times(sid, {"items": {"item:c-hello-list:hello": {"start": 2.0, "end": 2.6, "wall": tp.T0 + 1}}})
        self.studio.close_session(sid)
        st, d = call("POST", "/api/process", {"list": "c-hello-list"})
        self.assertEqual(st, 200)
        st, d = call("GET", "/api/audio/out/lists/c-hello-list/hello.m4a")
        self.assertEqual(st, 200)
        self.assertGreater(len(d), 500)
        for bad in ("/api/audio/out/lists/c-hello-list/nope.m4a", "/api/audio/out/lists/c-nope/hello.m4a",
                    "/api/audio/out/lists/c-hello-list/..%2f..%2fsettings.json", "/api/audio/out/lists/c-hello-list/hello.wav",
                    "/api/audio/robot/lists/c-hello-list/hello.m4a"):
            self.assertEqual(call("GET", bad)[0], 404, bad)
        with mock.patch.object(studio.subprocess, "Popen"), mock.patch.object(studio.shutil, "which", return_value="/usr/bin/open"):
            self.assertEqual(call("POST", "/api/open", {"what": "output"})[0], 200)
            self.assertEqual(call("POST", "/api/open", {"what": "../.."})[0], 400)
            self.assertEqual(call("POST", "/api/open", {"what": "lists"}, headers={"Host": "evil.example"})[0], 403)


if __name__ == "__main__":
    unittest.main(verbosity=1)
