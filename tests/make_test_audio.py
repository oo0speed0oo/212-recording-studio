#!/usr/bin/env python3
"""Make synthetic test audio (tones and noise bursts with silences between them), plus a JSON file
saying what the detector should find. No microphone needed.

    ~/kokoro-env/bin/python tests/make_test_audio.py

Writes tests/audio/synthetic.wav + synthetic.json, and tests/audio/flow.wav + flow.json.
(*.wav is in .gitignore: these files are made fresh each time.)
"""
import json
import math
import os
import wave

import numpy as np

SR = 48000
HERE = os.path.dirname(os.path.abspath(__file__))
OUT = os.path.join(HERE, "audio")
ROOM_DB = -58.0   # room noise, dBFS RMS


def db_to_amp(db):
    return 10 ** (db / 20.0)


def set_rms(x, db):
    r = math.sqrt(float(np.mean(x ** 2)) + 1e-18)
    return x * (db_to_amp(db) / r)


def fade(x, ms=5):
    n = min(len(x) // 2, int(SR * ms / 1000))
    if n > 0:
        w = 0.5 - 0.5 * np.cos(np.linspace(0, math.pi, n))
        x = x.copy()
        x[:n] *= w
        x[-n:] *= w[::-1]
    return x


def tone(dur, f0, db):
    t = np.arange(int(SR * dur)) / SR
    x = np.sin(2 * math.pi * f0 * t) + 0.5 * np.sin(2 * math.pi * 2 * f0 * t) + 0.25 * np.sin(2 * math.pi * 3 * f0 * t)
    return fade(set_rms(x, db))


def noise_burst(rng, dur, db, highpass=False):
    x = rng.standard_normal(int(SR * dur))
    if highpass:                      # hissy, like /s/ or /f/
        x = np.concatenate([[0], np.diff(x)])
    return fade(set_rms(x, db), ms=8)


class Track:
    def __init__(self, rng, seconds):
        self.rng = rng
        self.x = set_rms(rng.standard_normal(int(SR * seconds)), ROOM_DB)
        self.spec = []

    def put(self, start, sig, name, kind, detected_at=None, tol=0.03):
        i = int(round(start * SR))
        self.x[i:i + len(sig)] += sig
        self.spec.append({
            "name": name, "kind": kind, "start": start, "end": start + len(sig) / SR,
            "detected_at": detected_at if detected_at is not None else {"0": True, "1": True, "2": True},
            "tol": tol,
        })

    def save(self, name, extra=None):
        os.makedirs(OUT, exist_ok=True)
        x = np.clip(self.x, -1, 1)
        pcm = (x * 32767).astype("<i2")
        with wave.open(os.path.join(OUT, name + ".wav"), "wb") as w:
            w.setnchannels(1)
            w.setsampwidth(2)
            w.setframerate(SR)
            w.writeframes(pcm.tobytes())
        spec = {"sr": SR, "seconds": len(x) / SR, "room_db": ROOM_DB, "segments": self.spec}
        spec.update(extra or {})
        with open(os.path.join(OUT, name + ".json"), "w") as f:
            json.dump(spec, f, indent=1)
        print("wrote", name, "%.1f s" % (len(x) / SR), len(self.spec), "segments")


def make_synthetic():
    rng = np.random.default_rng(212)
    t = Track(rng, 30.0)
    F, T = False, True
    # 0-3 s: room noise only (the quiet check uses this). Hop-aligned starts (multiples of 10 ms) where timing matters.
    t.put(3.00, tone(0.30, 250, -26), "c: short clear tone", "tone")
    t.put(4.50, tone(0.60, 400, -24), "o: clear tone", "tone")
    t.put(6.20, noise_burst(rng, 0.80, -40, highpass=True), "f: faint hiss", "hiss", tol=0.06)
    t.put(8.00, noise_burst(rng, 0.80, -47, highpass=True), "very faint hiss (needs 1 retry)", "hiss",
          {"0": F, "1": T, "2": T}, tol=0.08)
    t.put(9.80, noise_burst(rng, 2.00, -32, highpass=True), "s: long continuous hiss (must NOT be cut)", "hiss", tol=0.06)
    # two tones 0.3 s apart: one sound with a small gap inside (gap < 500 ms silence rule)
    pair = np.concatenate([tone(0.30, 300, -27), np.zeros(int(SR * 0.30)), tone(0.30, 300, -27)])
    t.put(13.00, pair, "k-k: tone, 300 ms gap, tone (one segment)", "pair")
    # a 25 ms pop: a bump on the desk, never speech
    t.put(15.50, tone(0.025, 200, -20), "pop 25 ms (never)", "pop", {"0": F, "1": F, "2": F})
    # a 50 ms burst (like a quick /t/): too short for the default 80 ms rule; retries shorten the rule
    t.put(17.00, noise_burst(rng, 0.05, -30, highpass=True), "t: 50 ms burst (needs 2 retries)", "burst",
          {"0": F, "1": F, "2": T}, tol=0.03)
    t.put(19.00, tone(0.40, 220, -25), "g: last tone", "tone")
    # noise-only tail (20-30 s) lets the test check there are no false triggers
    t.save("synthetic", {"noise_only_from": 21.0, "noise_only_to": 30.0, "quiet_check_seconds": 2.0})


def make_flow():
    """For the full-pipeline test in the browser: five sounds, then (after the test clicks item 2) a redo of item 2."""
    rng = np.random.default_rng(8212)
    t = Track(rng, 24.0)
    for k, start in enumerate([6.0, 8.0, 10.0, 12.0, 14.0]):
        t.put(start, tone(0.40, 200 + 40 * k, -25), "sound %d" % (k + 1), "tone")
    t.put(18.0, tone(0.50, 700, -25), "redo of item 2", "tone")
    t.save("flow", {"lead_in_seconds": 5.0})


def make_autoredo():
    """For the auto-redo browser test: three clipped takes in a row for the first item (the 3rd is kept after 2 asks),
    then a good one, then a 3.5 s one (too long for a sound), then a good one."""
    rng = np.random.default_rng(4)
    t = Track(rng, 26.0)
    for start in (6.0, 8.0, 10.0):
        t.put(start, np.clip(tone(0.4, 300, -3) * 3, -1, 1), "clipped", "clip")
    t.put(12.0, tone(0.4, 330, -25), "good", "tone")
    t.put(14.0, tone(3.5, 250, -25), "too long for a sound", "long")
    t.put(20.0, tone(0.4, 360, -25), "good", "tone")
    t.save("autoredo")


def make_sentence():
    """For the sentences browser test: two sentences, each with a 0.7 s pause in the middle."""
    rng = np.random.default_rng(5)
    t = Track(rng, 24.0)
    sig = np.concatenate([tone(0.5, 300, -25), np.zeros(int(SR * 0.7)), tone(0.5, 400, -25)])
    t.put(6.0, sig, "sentence 1", "sentence")
    t.put(12.0, sig, "sentence 2", "sentence")
    t.save("sentence")


def make_quiet():
    """60 s of room noise only: lets the browser test try the buttons (hold-to-record, pause...) with nothing to detect."""
    rng = np.random.default_rng(1212)
    Track(rng, 60.0).save("quiet")


if __name__ == "__main__":
    make_synthetic()
    make_flow()
    make_autoredo()
    make_sentence()
    make_quiet()
