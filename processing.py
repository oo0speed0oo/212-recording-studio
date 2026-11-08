"""Cutting, cleaning, loudness, naming and encoding (step R2).

Takes the long raw recording plus each item's start/end time and makes the small files the website needs.
Uses only numpy, Python's standard library and the Mac's own `afconvert` (for .m4a).

Where things go (all inside the studio folder, never inside the website):
    output-wav/cuts/<list>/<key>.wav     a lossless copy of every cut (raw, with 0.4 s of room noise each side)
    output-wav/cuts/<list>/index.json    what each cut is (which take, where the sound sits inside the cut)
    output-wav/_replaced/...             an older cut that was re-recorded (moved aside, never deleted)
    output/phonics/sounds/<no>.m4a       the sound 3 times, with gaps (two-version sounds: v1 x3, pause, v2 x3)
    output/phonics/alone/<no>.m4a        the single sound (version 1)
    output/phonics/alone/<no>-2.m4a      the single second version (two-version sounds only; the site uses this name)
    output/phonics/words/<word>.m4a      a challenge word or common word
    output/phonics/sentences/<id>.m4a    a sentence

Everything is rebuilt from the cuts, so changing a setting (loudness, bitrate, gap...) and pressing "Rebuild"
needs no re-recording.
"""
import datetime
import json
import math
import os
import shutil
import subprocess
import tempfile
import wave
from pathlib import Path

import numpy as np

# name: (default, smallest, biggest)
SETTINGS = {
    "pad_s":           (0.12, 0.0, 0.5),     # quiet kept before and after each sound
    "fade_ms":         (10.0, 0.0, 50.0),    # fade in / fade out of every cut
    "target_rms_db":   (-20.0, -40.0, -6.0),  # loudness of the sound itself (RMS, dBFS)
    "peak_db":         (-1.0, -6.0, -0.1),   # the loudest sample never goes above this (wins over the RMS target)
    "aac_kbps":        (64, 32, 320),        # .m4a bitrate: 64 default; try 96 or 128 for short sounds like /k/ /t/
    "sound_repeats":   (3, 1, 5),            # the sound said this many times in sounds/<no>.m4a
    "sound_gap_s":     (0.6, 0.0, 3.0),      # quiet heard between repeats
    "version_pause_s": (1.0, 0.0, 5.0),      # quiet heard between version 1 and version 2
}
CUT_MARGIN_S = 0.4                           # room noise kept around the sound in the lossless cut
MAX_BOOST_DB = 40.0
WARN_QUIET_DB = -55.0                        # raw sound quieter than this: mic probably too far away


class ProcessingError(Exception):
    pass


def config_from_settings(settings):
    """Read the processing numbers from settings.json; anything missing or silly falls back to the default."""
    cfg = {}
    for name, (default, lo, hi) in SETTINGS.items():
        v = settings.get(name, default)
        try:
            v = float(v)
            if math.isnan(v):
                raise ValueError
        except (TypeError, ValueError):
            v = default
        v = min(hi, max(lo, v))
        cfg[name] = int(round(v)) if name in ("aac_kbps", "sound_repeats") else v
    return cfg


def now_iso():
    return datetime.datetime.now().astimezone().isoformat(timespec="seconds")


def db(x):
    return 20 * math.log10(x) if x > 1e-9 else -180.0


def rms_db(x):
    if len(x) == 0:
        return -180.0
    return 10 * math.log10(float(np.mean(np.square(x, dtype=np.float64))) + 1e-18)


def peak_db(x):
    return db(float(np.max(np.abs(x)))) if len(x) else -180.0


# ── WAV files ────────────────────────────────────────────────────────────────
def wav_info(path):
    with wave.open(str(path), "rb") as w:
        return w.getframerate(), w.getnframes()


def read_wav_range(path, i0, i1):
    """Read frames i0..i1 of a 16-bit WAV as int16 (first channel only)."""
    with wave.open(str(path), "rb") as w:
        if w.getsampwidth() != 2:
            raise ProcessingError("%s is not a 16-bit WAV." % Path(path).name)
        ch = w.getnchannels()
        w.setpos(i0)
        data = np.frombuffer(w.readframes(i1 - i0), dtype="<i2")
    return data[::ch] if ch > 1 else data


def write_wav_int16(path, x, sr):
    with wave.open(str(path), "wb") as w:
        w.setnchannels(1)
        w.setsampwidth(2)
        w.setframerate(int(sr))
        w.writeframes(np.asarray(x, dtype="<i2").tobytes())


def atomic_write_json(path, data):
    tmp = Path(str(path) + ".tmp")
    with open(tmp, "w", encoding="utf-8") as f:
        json.dump(data, f, ensure_ascii=False, indent=1)
        f.flush()
        os.fsync(f.fileno())
    os.replace(tmp, path)


# ── signal helpers ───────────────────────────────────────────────────────────
def resample(x, sr_from, sr_to):
    """Change the sample rate (FFT method; fine for short speech clips)."""
    if sr_from == sr_to or len(x) == 0:
        return x
    n_out = int(round(len(x) * sr_to / float(sr_from)))
    X = np.fft.rfft(x)
    Y = np.zeros(n_out // 2 + 1, dtype=complex)
    m = min(len(X), len(Y))
    Y[:m] = X[:m]
    return (np.fft.irfft(Y, n_out) * (n_out / float(len(x)))).astype(np.float32)


def fade_edges(x, sr, ms):
    n = min(len(x) // 2, int(round(sr * ms / 1000.0)))
    if n < 2:
        return x
    w = 0.5 - 0.5 * np.cos(np.linspace(0, math.pi, n))
    x = x.copy()
    x[:n] *= w
    x[-n:] *= w[::-1]
    return x


def refine_region(x, sr, region):
    """Hold-to-record takes start when the key goes down and end when it comes up, so they can hold a lot of quiet.
    Find where the sound really is: loud enough (10 dB over the room noise in the margins) for at least 20 ms."""
    hop = max(1, int(sr * 0.01))
    n = len(x) // hop
    if n < 8:
        return region, False
    frames = x[:n * hop].reshape(n, hop).astype(np.float64)
    d = 10 * np.log10(np.mean(frames ** 2, axis=1) + 1e-12)
    a, b = int(region[0] * sr / hop), int(math.ceil(region[1] * sr / hop))
    margin = np.concatenate([d[:max(0, a)], d[min(n, b):]])
    floor = np.percentile(margin, 30) if len(margin) >= 10 else np.percentile(d, 10)
    loud = d > floor + 10.0
    lo, hi = max(0, a), min(n, b)
    first = last = None
    for i in range(lo, hi - 1):
        if loud[i] and loud[i + 1]:
            if first is None:
                first = i
            last = i + 1
    if first is None:
        return region, False
    for i in range(hi - 1, lo - 1, -1):          # last loud frame (it can be a single frame at the very end)
        if loud[i]:
            last = max(last, i)
            break
    return (first * hop / float(sr), (last + 1) * hop / float(sr)), True


# ── the processor ────────────────────────────────────────────────────────────
class Processor:
    def __init__(self, root, cfg):
        self.root = Path(root)
        self.cfg = cfg
        self.out = self.root / "output" / "phonics"
        self.wavs = self.root / "output-wav"

    # -- cuts (lossless copies) --
    def cut_dir(self, list_name):
        return self.wavs / "cuts" / list_name

    def load_index(self, list_name):
        p = self.cut_dir(list_name) / "index.json"
        if p.exists():
            try:
                with open(p, encoding="utf-8") as f:
                    d = json.load(f)
                if isinstance(d.get("items"), dict):
                    return d
            except (OSError, ValueError):
                pass
        return {"version": 1, "items": {}}

    def save_index(self, list_name, idx):
        d = self.cut_dir(list_name)
        d.mkdir(parents=True, exist_ok=True)
        atomic_write_json(d / "index.json", idx)

    @staticmethod
    def cut_name(key):
        return key.replace(":", "-") + ".wav"

    def store_cut(self, list_name, key, raw_path, take, session_id):
        """Cut one item out of the long raw take and keep it as a lossless WAV. Returns (index entry, is_new)."""
        idx = self.load_index(list_name)
        old = idx["items"].get(key)
        if old and old.get("session") == session_id and old.get("start") == take["start"] and old.get("end") == take["end"]:
            return old, False                               # this exact take is already cut

        sr, nframes = wav_info(raw_path)
        start, end = float(take["start"]), float(take["end"])
        if not (start >= 0 and end > start):
            raise ProcessingError("the saved start/end times make no sense")
        if int(round(start * sr)) >= nframes:
            raise ProcessingError("the audio for this take was not saved (the recording stopped before it was written)")
        end = min(end, nframes / float(sr))                 # a crash may have lost the very end
        if end - start < 0.03:
            raise ProcessingError("only %d ms of this take was saved" % int((end - start) * 1000))
        i0 = max(0, int(round((start - CUT_MARGIN_S) * sr)))
        i1 = min(nframes, int(round((end + CUT_MARGIN_S) * sr)))
        raw = read_wav_range(raw_path, i0, i1)
        x = raw.astype(np.float32) / 32768.0
        region = (start - i0 / float(sr), end - i0 / float(sr))
        refined = False
        if take.get("forced"):
            region, refined = refine_region(x, sr, region)
        a, b = int(region[0] * sr), int(region[1] * sr)
        reg = x[a:b]

        warnings = []
        dur = region[1] - region[0]
        if dur < 0.06:
            warnings.append("very short (%d ms): a quick sound like /k/ may be cut. Check it, or record it again" % int(dur * 1000))
        if dur > 4.0:
            warnings.append("long (%.1f s)" % dur)
        if len(reg) and int(np.max(np.abs(raw[a:b]))) >= 32700:
            warnings.append("clipped: too loud for the mic. Turn the mic gain down and record it again")
        r = rms_db(reg)
        if r < WARN_QUIET_DB:
            warnings.append("very quiet (%.0f dB): the mic may be too far away, or the sound was missed" % r)
        if take.get("forced") and not refined:
            warnings.append("hold-to-record: no clear sound found, so the whole held time was kept")

        d = self.cut_dir(list_name)
        d.mkdir(parents=True, exist_ok=True)
        fname = self.cut_name(key)
        if (d / fname).exists():                            # re-recorded: keep the old cut aside
            rep = self.wavs / "_replaced" / list_name
            rep.mkdir(parents=True, exist_ok=True)
            shutil.move(str(d / fname), str(rep / (datetime.datetime.now().strftime("%Y%m%d-%H%M%S-") + fname)))
        write_wav_int16(d / fname, raw, sr)
        entry = {"file": fname, "sr": sr, "frames": int(len(raw)), "region": [round(region[0], 4), round(region[1], 4)],
                 "refined": refined, "forced": bool(take.get("forced")), "session": session_id,
                 "start": take["start"], "end": take["end"], "wall": take.get("wall"),
                 "raw_rms_db": round(r, 1), "raw_peak_db": round(peak_db(reg), 1), "warnings": warnings, "cut_at": now_iso()}
        idx["items"][key] = entry
        self.save_index(list_name, idx)
        return entry, True

    def load_cut(self, list_name, key, entry=None):
        entry = entry or self.load_index(list_name)["items"].get(key)
        if not entry:
            raise ProcessingError("no saved cut for %s" % key)
        p = self.cut_dir(list_name) / entry["file"]
        if not p.exists():
            raise ProcessingError("the cut file %s is missing" % entry["file"])
        sr, n = wav_info(p)
        x = read_wav_range(p, 0, n).astype(np.float32) / 32768.0
        return x, sr, entry["region"]

    # -- turning one cut into a clean, level segment --
    def make_segment(self, x, sr, region):
        c = self.cfg
        x = x - float(np.mean(x))                          # remove any DC offset
        a, b = int(round(region[0] * sr)), int(round(region[1] * sr))
        pad = int(round(c["pad_s"] * sr))
        i0, i1 = max(0, a - pad), min(len(x), b + pad)
        seg = x[i0:i1].copy()
        reg = x[a:b]
        r = rms_db(reg)
        p = peak_db(seg)
        if p < -90 or r < -90:
            raise ProcessingError("this cut is silent")
        gain_db = min(c["target_rms_db"] - r, c["peak_db"] - p, MAX_BOOST_DB)   # peak limit wins over the loudness target
        seg = seg * (10 ** (gain_db / 20.0))
        seg = fade_edges(seg, sr, c["fade_ms"])
        stats = {"gain_db": round(gain_db, 1), "rms_db": round(r + gain_db, 1), "peak_db": round(p + gain_db, 1),
                 "limited_by_peak": bool(c["peak_db"] - p < c["target_rms_db"] - r)}
        return seg.astype(np.float32), stats

    # -- encoding --
    def encode_m4a(self, x, sr, out_path):
        exe = shutil.which("afconvert")
        if not exe:
            raise ProcessingError("afconvert was not found (it comes with macOS), so .m4a files cannot be made")
        out_path = Path(out_path)
        out_path.parent.mkdir(parents=True, exist_ok=True)
        pcm = np.clip(np.round(x * 32767.0), -32768, 32767).astype("<i2")
        with tempfile.TemporaryDirectory(prefix="studio-enc-") as tmp:
            src, dst = Path(tmp) / "in.wav", Path(tmp) / "out.m4a"
            write_wav_int16(src, pcm, sr)
            # -s 0 = constant bitrate, so "64 kbps" really is 64 (the default mode goes far lower on tiny clips)
            r = subprocess.run([exe, "-f", "m4af", "-d", "aac", "-b", str(self.cfg["aac_kbps"] * 1000), "-s", "0", "-q", "127",
                                str(src), str(dst)], capture_output=True, text=True, timeout=120)
            if r.returncode != 0 or not dst.exists() or dst.stat().st_size == 0:
                raise ProcessingError("afconvert failed: %s" % (r.stderr.strip() or r.stdout.strip() or "no message"))
            os.replace(str(dst), str(out_path))
        return out_path.stat().st_size

    # -- one word or sentence -> its file --
    def build_single(self, list_name, key, rel, kind):
        """Make words/<word>.m4a or sentences/<id>.m4a from the saved cut."""
        x, sr, region = self.load_cut(list_name, key)
        seg, stats = self.make_segment(x, sr, region)
        size = self.encode_m4a(seg, sr, self.out / rel)
        return {"file": rel, "kind": kind, "version": 1, "seconds": round(len(seg) / float(sr), 3),
                "rms_db": stats["rms_db"], "peak_db": stats["peak_db"], "gain_db": stats["gain_db"],
                "limited_by_peak": stats["limited_by_peak"], "bytes": size, "kbps": self.cfg["aac_kbps"]}

    # -- one sound -> its files --
    def build_sound(self, no, versions, list_name="sounds"):
        """Make alone/<no>.m4a (+ alone/<no>-2.m4a) and sounds/<no>.m4a from the saved cuts.
        Returns {"files": [...], "waiting": text or None}. Needs version 1 at least."""
        c = self.cfg
        idx = self.load_index(list_name)
        keys = ["sound:%s:v%d" % (no, v) for v in range(1, versions + 1)]
        missing = [k for k in keys if k not in idx["items"]]
        if keys[0] in missing:
            raise ProcessingError("version 1 of sound %s has not been recorded" % no)
        segs, sr0 = [], None
        for k in keys:
            if k in missing:
                continue
            x, sr, region = self.load_cut(list_name, k, idx["items"][k])
            if sr0 is None:
                sr0 = sr
            elif sr != sr0:
                x = resample(x, sr, sr0)
                region = [region[0], region[1]]
            seg, stats = self.make_segment(x, sr0, region)
            segs.append((k, seg, stats))

        files = []

        def emit(rel, samples, stats, kind, ver):
            size = self.encode_m4a(samples, sr0, self.out / rel)
            files.append({"file": rel, "kind": kind, "no": no, "version": ver, "seconds": round(len(samples) / float(sr0), 3),
                          "rms_db": stats["rms_db"], "peak_db": stats["peak_db"], "gain_db": stats["gain_db"],
                          "limited_by_peak": stats["limited_by_peak"], "bytes": size, "kbps": c["aac_kbps"]})

        for i, (k, seg, stats) in enumerate(segs):
            emit("alone/%s.m4a" % (no if i == 0 else "%s-%d" % (no, i + 1)), seg, stats, "alone", i + 1)

        waiting = None
        if missing:
            waiting = "waiting for the other version of sound %s (%s) before sounds/%s.m4a can be made" % (
                no, ", ".join(m.split(":")[-1] for m in missing), no)
        else:
            # the quiet between repeats counts the padding already inside each segment, so what you hear is `gap`
            pad_each = int(round(c["pad_s"] * sr0))
            gap = np.zeros(max(0, int(round(c["sound_gap_s"] * sr0)) - 2 * pad_each), dtype=np.float32)
            pause = np.zeros(max(0, int(round(c["version_pause_s"] * sr0)) - 2 * pad_each), dtype=np.float32)
            parts = []
            for vi, (k, seg, stats) in enumerate(segs):
                if vi:
                    parts.append(pause)
                for r in range(c["sound_repeats"]):
                    if r:
                        parts.append(gap)
                    parts.append(seg)
            whole = np.concatenate(parts)
            emit("sounds/%s.m4a" % no, whole, segs[0][2], "sounds", 0)
        return {"files": files, "waiting": waiting}
