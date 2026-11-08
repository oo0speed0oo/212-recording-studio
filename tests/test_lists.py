#!/usr/bin/env python3
"""Offline test of the other lists (step R4): challenge words, common words and sentences are cut, levelled and named like sounds.

    ~/kokoro-env/bin/python -I tests/test_lists.py
"""
import json
import sys
import unittest
from pathlib import Path

import numpy as np

sys.path.insert(0, str(Path(__file__).resolve().parent))
sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
import test_processing as tp  # noqa: E402

WORDS = ("lesson,group,word,status,note,source\n"
         "4,d,cod,common,,Mike\n4,d,cad,rare,,Mike\n8,h,hal,name,,Mike\n9,t,tab,common,,Mike\n")
SENTENCES = "id,lesson,target,sentence\nPS1,8,hog,The hog had a log and a hat.\nPS2,8,ham,Dad had a ham.\n"


@unittest.skipUnless(tp.HAVE_AFCONVERT, "needs the Mac's afconvert")
class ListsTest(tp.Base):
    def setUp(self):
        super().setUp()
        d = self.site / "portal" / "phonics"
        (d / "phonics-words.csv").write_text(WORDS, encoding="utf-8")
        (d / "phonics-sentences.csv").write_text(SENTENCES, encoding="utf-8")
        self.sr = 48000

    def dur_and_level(self, rel):
        x, r = tp.decode_m4a(self.out(rel))
        pad = int(0.12 * r) + 600
        return len(x) / r, tp.db_of(x[pad:len(x) - pad])

    def test_01_challenge_words(self):
        sr = self.sr
        self.session({"word:cad": (2.0, 2.6, tp.tone(sr, 0.6, 300, -30)),
                      "word:hal": (4.0, 4.8, tp.tone(sr, 0.8, 250, -18))}, list_name="words")
        rep = self.studio.process_list("words")
        self.assertEqual(rep["errors"], [])
        self.assertEqual(sorted(f["file"] for f in rep["made"]), ["words/cad.m4a", "words/hal.m4a"])
        for rel, secs in (("words/cad.m4a", 0.84), ("words/hal.m4a", 1.04)):
            d, lvl = self.dur_and_level(rel)
            self.assertAlmostEqual(d, secs, delta=0.04, msg=rel)
            self.assertAlmostEqual(lvl, -20.0, delta=1.2, msg=rel + " is at the target loudness whatever the recording level")
        prog = self.progress()
        self.assertEqual(prog["word:cad"]["files"], ["words/cad.m4a"])
        cut = self.root / "output-wav" / "cuts" / "words" / "word-cad.wav"
        self.assertTrue(cut.exists())
        r = self.studio.api_review("words")
        self.assertEqual([f["file"] for f in r["files"]], ["words/cad.m4a", "words/hal.m4a"])
        self.assertEqual(r["files"][0]["title"], "cad")
        self.assertEqual(r["files"][0]["kind"], "words")
        # nothing leaks into the other lists
        self.assertEqual(self.studio.api_review("common")["files"], [])
        self.assertEqual(self.studio.api_review("sentences")["files"], [])
        lst = {c["id"]: c for c in self.studio.api_lists()["lists"]}
        self.assertEqual((lst["words"]["done"], lst["words"]["total"], lst["words"]["unchecked"]), (2, 2, 2))
        self.assertEqual((lst["common"]["done"], lst["common"]["unchecked"]), (0, 0))

    def test_02_sentences(self):
        sr = self.sr
        sig = np.concatenate([tp.tone(sr, 0.7, 300, -26), np.zeros(int(sr * 0.5)), tp.tone(sr, 0.9, 400, -26)])   # a pause inside
        self.session({"sentence:PS1": (3.0, 3.0 + len(sig) / sr, sig)}, list_name="sentences", seconds=10)
        rep = self.studio.process_list("sentences")
        self.assertEqual(rep["errors"], [])
        self.assertEqual([f["file"] for f in rep["made"]], ["sentences/PS1.m4a"])
        d, lvl = self.dur_and_level("sentences/PS1.m4a")
        self.assertAlmostEqual(d, 2.1 + 0.24, delta=0.05)
        self.assertAlmostEqual(lvl, -20.0, delta=1.5)
        r = self.studio.api_review("sentences")
        self.assertEqual(r["files"][0]["title"], "The hog had a log and a hat.")
        self.assertEqual(r["files"][0]["no"], "PS1")

    def test_03_common_and_challenge_share_the_words_folder(self):
        sr = self.sr
        self.session({"word:cad": (2.0, 2.5, tp.tone(sr, 0.5, 300, -26))}, list_name="words")
        self.session({"word:cod": (2.0, 2.5, tp.tone(sr, 0.5, 320, -26)), "word:tab": (4.0, 4.5, tp.tone(sr, 0.5, 340, -26))}, list_name="common")
        self.studio.process_list("words")
        self.studio.process_list("common")
        self.assertEqual(sorted(p.name for p in (self.root / "output" / "phonics" / "words").glob("*.m4a")), ["cad.m4a", "cod.m4a", "tab.m4a"])
        self.assertEqual([f["file"] for f in self.studio.api_review("words")["files"]], ["words/cad.m4a"])
        self.assertEqual([f["file"] for f in self.studio.api_review("common")["files"]], ["words/cod.m4a", "words/tab.m4a"])
        # start the challenge words over completely: only cad moves, the common words are untouched
        r = self.studio.reset_list("words", everything=True)
        self.assertFalse(self.out("words/cad.m4a").exists())
        self.assertTrue(self.out("words/cod.m4a").exists())
        self.assertTrue(self.out("words/tab.m4a").exists())
        self.assertTrue((Path(r["folder"]) / "output" / "words" / "cad.m4a").exists())
        prog = self.progress()
        self.assertEqual(sorted(prog), ["word:cod", "word:tab"])
        self.assertEqual(len(self.studio.api_review("common")["files"]), 2)
        self.assertTrue((self.root / "output-wav" / "cuts" / "common").exists())
        self.assertFalse((self.root / "output-wav" / "cuts" / "words").exists())

    def test_04_rebuild_words_with_new_settings(self):
        sr = self.sr
        self.session({"word:cad": (2.0, 2.5, tp.tone(sr, 0.5, 300, -26))}, list_name="words")
        self.studio.process_list("words")
        self.studio = self.new_studio(aac_kbps=128, target_rms_db=-14)
        rep = self.studio.process_list("words", rebuild=True)
        self.assertEqual((rep["errors"], rep["cut"], [f["file"] for f in rep["made"]]), ([], 0, ["words/cad.m4a"]))
        d, lvl = self.dur_and_level("words/cad.m4a")
        self.assertAlmostEqual(lvl, -14.0, delta=1.2)
        self.assertTrue(115000 <= tp.bitrate_of(self.out("words/cad.m4a")) <= 135000)

    def test_05_flag_is_kept(self):
        """A take accepted after the auto-redo tries ran out carries its flag into progress.json."""
        sr = self.sr
        sid, _ = self.session({"word:cad": (2.0, 2.5, tp.tone(sr, 0.5, 300, -26))}, list_name="words")
        meta = json.loads((self.root / "takes" / (sid + ".json")).read_text())
        meta["items"]["word:cad"]["flag"] = "quiet"
        (self.root / "takes" / (sid + ".json")).write_text(json.dumps(meta))
        self.studio.process_list("words")
        self.assertEqual(self.progress()["word:cad"]["flag"], "quiet")


if __name__ == "__main__":
    unittest.main(verbosity=2)
