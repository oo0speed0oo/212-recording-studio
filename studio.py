#!/usr/bin/env python3
"""212 Recording Studio: a tiny local server for recording your own voice for the 212 English Phonics pages.

Start it with:
    ~/kokoro-env/bin/python ~/Documents/212-recording-studio/studio.py

It opens http://localhost:8212 in your browser. Uses only Python's standard library.
It READS the website's CSV lists. It writes to the website folder only in "Put on the site" (siteput.py),
which copies finished .m4a files and first moves any file it replaces into _extra_files/robot-voices/.
"""
import argparse
import csv
import datetime
import json
import os
import re
import shutil
import struct
import subprocess
import sys
import threading
import wave
import webbrowser
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import urlparse, parse_qs

import processing
import siteput

VERSION = "R5"
DEFAULT_PORT = 8212
DEFAULT_MIC_TIP = ("Same distance from the microphone every time (about a fist away), "
                   "and the same gain setting on the mic. Speak across it, not straight into it.")
DEFAULT_SETTINGS = {"website_folder": "~/Documents/212english-website",
                    # where Local (the test copy of the site) keeps its files; used by "Put on the site"
                    "local_site_folder": "~/Local Sites/212english-local/app/public"}

# Every option on the Options menu: name -> (type, default, min or choices, max). Anything not valid falls back to the default.
OPTION_SPEC = {
    # recording
    "noise_suppression":          ("bool", True),
    "auto_redo":                  ("bool", True),
    "mode":                       ("enum", "quick", ["quick", "careful"]),
    "after_redo":                 ("enum", "return", ["return", "stay"]),
    "countdown":                  ("bool", False),
    "countdown_ms":               ("int", 1000, 300, 3000),
    # auto-redo thresholds
    "auto_redo_min_ms":           ("int", 60, 20, 1000),
    "auto_redo_max_ms_sounds":    ("int", 3000, 500, 15000),
    "auto_redo_max_ms_words":     ("int", 3000, 500, 15000),
    "auto_redo_max_ms_sentences": ("int", 12000, 2000, 60000),
    "auto_redo_min_rms_db":       ("float", -50.0, -75.0, -25.0),
    "auto_redo_clip_db":          ("float", -0.3, -6.0, 0.0),
    "auto_redo_max_tries":        ("int", 2, 1, 5),
    # detection
    "silence_ms_sounds":          ("int", 500, 200, 3000),
    "silence_ms_words":           ("int", 700, 200, 4000),
    "silence_ms_sentences":       ("auto_int", "auto", 500, 5000),
    "sentence_silence_base_ms":   ("int", 1000, 500, 4000),
    "sentence_silence_per_word_ms": ("int", 80, 0, 400),
    "sentence_silence_max_ms":    ("int", 2000, 800, 6000),
    "sentence_short_words":       ("int", 6, 1, 20),
    "sensitivity_margin_db":      ("float", 12.0, 6.0, 24.0),
    "retry_step_db":              ("float", 3.0, 1.0, 6.0),
    # text
    "mic_tip":                    ("str", DEFAULT_MIC_TIP, 400),
}
for _name, (_default, _lo, _hi) in processing.SETTINGS.items():      # the file settings (R2) are options too
    OPTION_SPEC[_name] = ("int" if _name in ("aac_kbps", "sound_repeats") else "float", _default, _lo, _hi)

# The lists. `kind` says how the items are cut and named.
LISTS = {
    "sounds":    {"title": "Sounds",          "prompt": "Say the sound once", "ready": True, "kind": "sounds",
                  "note": "67 sounds, 8 of them with two versions"},
    "words":     {"title": "Challenge words", "prompt": "Say the word", "ready": True, "kind": "words",
                  "note": "Rare words and names, in lesson order"},
    "sentences": {"title": "Sentences",       "prompt": "Read the sentence", "ready": True, "kind": "sentences",
                  "note": "In id order. Press Enter (Done) when you finish a sentence"},
    "common":    {"title": "Common words",    "prompt": "Say the word", "ready": True, "kind": "words",
                  "note": "Everyday words. Do these last"},
}
SAFE_NAME = re.compile(r"^[A-Za-z0-9_-]+$")

SESSION_ID = re.compile(r"^\d{4}-\d{2}-\d{2}_\d{2}-\d{2}-\d{2}(-\d+)?_[a-z0-9-]+$")
WAV_HEADER = 44
MIME = {".html": "text/html; charset=utf-8", ".js": "text/javascript; charset=utf-8", ".css": "text/css; charset=utf-8",
        ".json": "application/json", ".wav": "audio/wav", ".svg": "image/svg+xml", ".png": "image/png", ".ico": "image/x-icon"}


class StudioError(Exception):
    def __init__(self, message, status=400):
        super().__init__(message)
        self.status = status


def now_iso():
    return datetime.datetime.now().astimezone().isoformat(timespec="seconds")


def atomic_write_json(path, data):
    tmp = Path(str(path) + ".tmp")
    with open(tmp, "w", encoding="utf-8") as f:
        json.dump(data, f, ensure_ascii=False, indent=1)
        f.flush()
        os.fsync(f.fileno())
    os.replace(tmp, path)


def wav_header(sample_rate, data_bytes):
    return struct.pack("<4sI4s4sIHHIIHH4sI", b"RIFF", 36 + data_bytes, b"WAVE", b"fmt ", 16, 1, 1,
                       sample_rate, sample_rate * 2, 2, 16, b"data", data_bytes)


class Studio:
    def __init__(self, root, settings_override=None):
        self.root = Path(root).resolve()
        self.web = self.root / "web"
        self.takes = self.root / "takes"
        self.test_audio = self.root / "tests" / "audio"
        self.lock = threading.Lock()
        self.settings_error = None
        self.settings = dict(DEFAULT_SETTINGS)
        sfile = self.root / "settings.json"
        if sfile.exists():
            try:
                with open(sfile, encoding="utf-8") as f:
                    loaded = json.load(f)
                if isinstance(loaded, dict):
                    self.settings.update(loaded)
            except (OSError, ValueError) as e:
                self.settings_error = "settings.json could not be read (%s). Using the defaults." % e
        if settings_override:
            self.settings.update(settings_override)
        self.takes.mkdir(exist_ok=True)
        self.proc = processing.Processor(self.root, processing.config_from_settings(self.settings))
        self.proc_lock = threading.Lock()

    # ── website CSVs (read only) ───────────────────────────────────────────
    def website(self):
        return Path(os.path.expanduser(self.settings["website_folder"]))

    def csv_path(self, name):
        return self.website() / "portal" / "phonics" / name

    def read_csv(self, name):
        p = self.csv_path(name)
        if not p.exists():
            raise StudioError("I cannot find %s. Check the website folder in settings.json (now: %s)."
                              % (p, self.settings["website_folder"]), 500)
        with open(p, encoding="utf-8-sig", newline="") as f:
            return list(csv.DictReader(f))

    def sounds_items(self):
        """One item per sound; the 8 two-version sounds ("that / thumb") become two items."""
        items = []
        for r in self.read_csv("phonics-sounds.csv"):
            no = str(r["sound_no"]).strip()
            sound = r["sound"].strip()
            words = [w.strip() for w in r["key_word"].split(" / ") if w.strip()]
            for idx, kw in enumerate(words):
                items.append({
                    "key": "sound:%s:v%d" % (no, idx + 1),
                    "no": int(no) if no.isdigit() else no,
                    "sound": sound,
                    "key_word": kw,
                    "version": idx + 1,
                    "versions": len(words),
                })
        return items

    @staticmethod
    def _num(text, fallback=10 ** 9):
        digits = re.sub(r"\D", "", str(text))
        return int(digits) if digits else fallback

    def words_items(self, statuses):
        """Challenge words (rare + names) or common words, each word once, in lesson order."""
        rows = list(enumerate(self.read_csv("phonics-words.csv")))
        rows.sort(key=lambda t: (self._num(t[1].get("lesson", "")), t[0]))
        seen, items = set(), []
        for _, r in rows:
            w = (r.get("word") or "").strip()
            if r.get("status") not in statuses or not w or w in seen or not SAFE_NAME.match(w):
                continue
            seen.add(w)
            items.append({"key": "word:%s" % w, "text": w, "word": w, "lesson": self._num(r.get("lesson", ""), 0)})
        return items

    def sentences_items(self):
        rows = list(enumerate(self.read_csv("phonics-sentences.csv")))
        rows.sort(key=lambda t: (self._num(t[1].get("id", "")), t[0]))
        items = []
        for _, r in rows:
            sid, text = (r.get("id") or "").strip(), (r.get("sentence") or "").strip()
            if not sid or not text or not SAFE_NAME.match(sid):
                continue
            items.append({"key": "sentence:%s" % sid, "text": text, "id": sid, "words": len(text.split())})
        return items

    def list_items(self, name):
        if name not in LISTS or not LISTS[name]["ready"]:
            raise StudioError("That list is not ready yet.", 404)
        if name == "sounds":
            return self.sounds_items()
        if name == "words":
            return self.words_items(("rare", "name"))
        if name == "common":
            return self.words_items(("common",))
        return self.sentences_items()

    def list_total(self, name):
        try:
            return len(self.list_items(name))
        except StudioError:
            return None

    # ── progress and takes on disk ─────────────────────────────────────────
    def read_progress(self):
        """progress.json: { "items": { key: {at, session, start, end, forced, cut, files, redo?} },
                            "files": { "alone/1.m4a": {kind, no, title, keys, seconds, rms_db, peak_db, bytes, built, review?} } }
        An item is done once it is cut, unless the check screen sent it back with Redo."""
        p = self.root / "progress.json"
        if not p.exists():
            return {}, {}
        try:
            with open(p, encoding="utf-8") as f:
                data = json.load(f)
            if not isinstance(data, dict):
                return {}, {}
            items = {k: v for k, v in (data.get("items") or {}).items() if isinstance(v, dict)}
            files = {k: v for k, v in (data.get("files") or {}).items() if isinstance(v, dict)}
            return items, files
        except (OSError, ValueError):
            return {}, {}

    def progress_items(self):
        return self.read_progress()[0]

    def progress_files(self):
        return self.read_progress()[1]

    def progress_done(self):
        return {k: v for k, v in self.progress_items().items() if not v.get("redo")}

    def write_progress(self, items, files=None):
        if files is None:
            files = self.progress_files()
        atomic_write_json(self.root / "progress.json", {"version": 2, "updated": now_iso(), "items": items, "files": files})

    def sessions(self):
        out = []
        for p in sorted(self.takes.glob("*.json")):
            try:
                with open(p, encoding="utf-8") as f:
                    meta = json.load(f)
                if isinstance(meta, dict) and meta.get("id"):
                    out.append(meta)
            except (OSError, ValueError):
                continue
        return out

    def recorded_and_today(self, list_name):
        """recorded: the newest take of each item from sessions that have not been cut yet. today: keys recorded today."""
        recorded, today = {}, set()
        today_date = datetime.date.today()
        for meta in self.sessions():
            if meta.get("list") != list_name:
                continue
            for key, t in (meta.get("items") or {}).items():
                if not isinstance(t, dict) or t.get("start") is None or t.get("end") is None:
                    continue
                wall = float(t.get("wall") or 0)
                if wall and datetime.date.fromtimestamp(wall) == today_date:
                    today.add(key)
                if meta.get("finished"):
                    continue
                if key not in recorded or wall >= recorded[key]["wall"]:
                    recorded[key] = {"session": meta["id"], "start": t["start"], "end": t["end"], "wall": wall}
        return recorded, sorted(today)

    def api_config(self):
        return {
            "version": VERSION,
            "mic_tip": self.options()["mic_tip"],
            "website_folder": self.settings["website_folder"],
            "local_site_folder": self.settings.get("local_site_folder"),
            "website_ok": self.csv_path("phonics-sounds.csv").exists(),
            "settings_error": self.settings_error,
        }

    # ── options (the gear menu) ────────────────────────────────────────────
    @staticmethod
    def _coerce(name, value, strict=False):
        """Return a valid value for this option. strict=True raises on the wrong type; otherwise falls back to the default."""
        spec = OPTION_SPEC[name]
        kind, default = spec[0], spec[1]

        def bad(msg):
            if strict:
                raise StudioError("%s: %s" % (name, msg), 400)
            return default
        if kind == "bool":
            return value if isinstance(value, bool) else bad("must be true or false")
        if kind == "enum":
            return value if value in spec[2] else bad("must be one of %s" % ", ".join(spec[2]))
        if kind == "str":
            return value[:spec[2]] if isinstance(value, str) else bad("must be text")
        if kind == "auto_int" and value == "auto":
            return "auto"
        if isinstance(value, bool) or not isinstance(value, (int, float)) or value != value:
            return bad("must be a number")
        lo, hi = spec[2], spec[3]
        v = min(hi, max(lo, value))
        return int(round(v)) if kind in ("int", "auto_int") else float(v)

    def options(self):
        """Every option with its current value (settings.json on top of the defaults)."""
        return {name: self._coerce(name, self.settings.get(name, spec[1])) for name, spec in OPTION_SPEC.items()}

    def api_settings(self):
        spec = {}
        for name, sp in OPTION_SPEC.items():
            e = {"type": sp[0], "default": sp[1]}
            if sp[0] == "enum":
                e["choices"] = sp[2]
            elif sp[0] == "str":
                e["max"] = sp[2]
            else:
                if sp[0] != "bool":
                    e["min"], e["max"] = sp[2], sp[3]
            spec[name] = e
        return {"values": self.options(), "spec": spec}

    def set_options(self, values, reset=False):
        """Save options into settings.json (only the ones that differ from the default). reset=True goes back to the defaults."""
        if not reset and not isinstance(values, dict):
            raise StudioError("No values given.", 400)
        clean = {} if reset else {name: self._coerce(name, v, strict=True) for name, v in values.items() if name in OPTION_SPEC}
        with self.lock:
            path = self.root / "settings.json"
            current = {}
            if path.exists():
                try:
                    with open(path, encoding="utf-8") as f:
                        loaded = json.load(f)
                    if isinstance(loaded, dict):
                        current = loaded
                except (OSError, ValueError):
                    current = {}
            if reset:
                for name in OPTION_SPEC:
                    current.pop(name, None)
                    self.settings.pop(name, None)
            for name, v in clean.items():
                if v == OPTION_SPEC[name][1]:
                    current.pop(name, None)
                    self.settings.pop(name, None)
                else:
                    current[name] = v
                    self.settings[name] = v
            atomic_write_json(path, current)
            self.settings_error = None
            self.proc.cfg = processing.config_from_settings(self.settings)
        return self.api_settings()

    def api_lists(self):
        done = self.progress_done()
        all_items, files = self.read_progress()
        recorded_by_list = {}
        cards = []
        for name, conf in LISTS.items():
            count = pending = redo = unchecked = 0
            total = None
            if conf["ready"]:
                try:
                    items = self.list_items(name)
                    total = len(items)
                    keys = {i["key"] for i in items}
                    recorded, _ = self.recorded_and_today(name)
                    count = sum(1 for k in keys if k in done or k in recorded)
                    pending = sum(1 for k in keys if k in recorded)
                    redo = sum(1 for k in keys if all_items.get(k, {}).get("redo") and k not in recorded)
                    unchecked = sum(1 for rel, f in files.items() if not f.get("review") and keys & set(f.get("keys", []))
                                    and (self.proc.out / rel).exists())
                except StudioError:
                    pass
            cards.append({"id": name, "title": conf["title"], "ready": conf["ready"], "note": conf["note"],
                          "total": total, "done": count, "pending": pending, "redo": redo, "unchecked": unchecked})
        return {"lists": cards}

    def api_list(self, name):
        if name not in LISTS or not LISTS[name]["ready"]:
            raise StudioError("That list is not ready yet.", 404)
        conf = LISTS[name]
        items = self.list_items(name)
        recorded, today = self.recorded_and_today(name)
        return {"list": name, "title": conf["title"], "kind": conf["kind"], "prompt": conf["prompt"],
                "items": items, "done": self.progress_done(), "recorded": recorded, "today": today,
                "redo": sorted(k for k, v in self.progress_items().items() if v.get("redo") and k not in recorded)}

    # ── the check screen (R3) ──────────────────────────────────────────────
    @staticmethod
    def _file_rank(rel):
        kind, _, name = rel.partition("/")
        stem = name[:-4] if name.endswith(".m4a") else name
        return (0 if kind == "alone" and not stem.endswith("-2") else 1 if kind == "alone" else 2)

    def api_review(self, list_name):
        """Every file the studio made for this list, with its check state (null = not checked yet)."""
        if list_name not in LISTS or not LISTS[list_name]["ready"]:
            raise StudioError("That list is not ready yet.", 404)
        order = {it["key"]: n for n, it in enumerate(self.list_items(list_name))}
        _, files = self.read_progress()
        site = self.website() / "portal" / "audio" / "phonics"
        out = []
        for rel, f in files.items():
            if not (self.proc.out / rel).exists() or not (set(f.get("keys", [])) & set(order)):
                continue
            e = dict(f)
            e["file"] = rel
            e["robot"] = (site / rel).is_file()
            e["review"] = f.get("review") or None
            out.append(e)
        group_pos = {}                              # files of one sound/word stay together, in the list's order
        for e in out:
            pos = min(order.get(k, 10 ** 9) for k in e.get("keys", []))
            group_pos[str(e.get("no"))] = min(pos, group_pos.get(str(e.get("no")), 10 ** 9))
        out.sort(key=lambda e: (group_pos[str(e.get("no"))], self._file_rank(e["file"]), e["file"]))
        return {"list": list_name, "files": out, "output_dir": str(self.proc.out)}

    def set_review(self, rels, review):
        """Mark files OK or Redo (or clear with None). Redo puts the sounds in those files back in the list as not done."""
        if review not in ("ok", "redo", None):
            raise StudioError("Bad review value.", 400)
        if not isinstance(rels, list) or not rels:
            raise StudioError("No files given.", 400)
        with self.lock:
            items, files = self.read_progress()
            for rel in rels:
                if rel not in files:
                    raise StudioError("Unknown file: %s" % rel, 404)
            touched = set()
            for rel in rels:
                if review:
                    files[rel]["review"] = review
                    files[rel]["reviewed_at"] = now_iso()
                else:
                    files[rel].pop("review", None)
                    files[rel].pop("reviewed_at", None)
                touched.update(files[rel].get("keys", []))
            for k in touched:                      # a sound is "to redo" while any file made from it says Redo
                if k in items:
                    if any(f.get("review") == "redo" and k in f.get("keys", []) for f in files.values()):
                        items[k]["redo"] = True
                    else:
                        items[k].pop("redo", None)
            self.write_progress(items, files)
        return {"ok": True, "files": {rel: review for rel in rels}}

    def audio_path(self, source, kind, name):
        if kind not in ("sounds", "alone", "words", "sentences") or not re.match(r"^[A-Za-z0-9._-]+\.m4a$", name) or ".." in name:
            raise StudioError("Not found.", 404)
        base = self.proc.out if source == "out" else self.website() / "portal" / "audio" / "phonics"
        p = base / kind / name
        if not p.is_file():
            raise StudioError("Not found.", 404)
        return p

    # ── putting the files on the site (R5) ─────────────────────────────────
    def _site(self):
        return siteput.Site(self.settings["website_folder"], self.settings.get("local_site_folder"))

    def _site_state_path(self):
        return self.root / "site-state.json"

    def _site_pending(self):
        try:
            with open(self._site_state_path(), encoding="utf-8") as f:
                p = json.load(f).get("pending") or {}
            return p if isinstance(p, dict) else {}
        except (OSError, ValueError, AttributeError):
            return {}

    def _site_write_pending(self, pending):
        atomic_write_json(self._site_state_path(), {"version": 1, "pending": pending})
        lst = self.root / "site-upload-list.txt"
        lines = sorted("/".join(siteput.PHONICS + (rel,)) for rel in pending)
        if lines:
            lst.write_text("\n".join(lines) + "\n", encoding="utf-8")
        elif lst.exists():
            lst.unlink()                                  # this list is made by the studio itself; it is not a robot file

    @staticmethod
    def _site_order(rel):
        kind, _, name = rel.partition("/")
        nums = [int(n) if n.isdigit() else 0 for n in re.findall(r"\d+", name)]
        return (["alone", "sounds", "words", "sentences"].index(kind), nums, name)

    def _site_files(self, include_unchecked):
        """The finished files that may go on the site: marked OK on the check screen (and, if asked, not checked yet).
        Files marked Redo never go."""
        _, fmeta = self.read_progress()
        chosen, counts = [], {"ok": 0, "unchecked": 0, "redo": 0, "missing": 0}
        for rel, f in fmeta.items():
            src = self.proc.out / rel
            if not siteput.SAFE_REL.match(rel) or not src.is_file():
                counts["missing"] += 1
                continue
            r = f.get("review")
            counts["ok" if r == "ok" else "redo" if r == "redo" else "unchecked"] += 1
            if r == "ok" or (r is None and include_unchecked):
                chosen.append((rel, src, f))
        chosen.sort(key=lambda t: self._site_order(t[0]))
        return chosen, counts

    def _site_paths(self):
        lst = self.root / "site-upload-list.txt"
        site = self._site()
        return {"upload_list_file": str(lst), "website_folder": str(site.website),
                "local_folder": str(site.local) if site.local else None,
                "robot_folder": str(site.backup_dir()), "log_file": str(site.backup_dir() / siteput.LOG_NAME),
                "git_add": 'cd "%s" && git add --pathspec-from-file="%s"' % (site.website, lst)}

    def api_site_plan(self, include_unchecked=False):
        """What 'Put on the site' would do right now. Changes nothing."""
        site = self._site()
        chosen, counts = self._site_files(include_unchecked)
        acts = site.plan([(rel, src) for rel, src, _ in chosen])
        files, summary = [], {"add": 0, "robot": 0, "earlier": 0, "same": 0}
        for rel, src, f in chosen:
            a = acts[rel]
            main = a.get("website", "add")
            main = "robot" if main == "known" else main          # a robot voice that is already saved is still a robot voice
            summary[main] += 1
            files.append({"file": rel, "title": f.get("title") or rel, "review": f.get("review") or None,
                          "action": main, "local": a.get("local")})
        pending = sorted(self._site_pending())
        out = {"files": files, "counts": counts, "summary": summary, "include_unchecked": bool(include_unchecked),
               "pending": ["/".join(siteput.PHONICS + (rel,)) for rel in pending]}
        out.update(site.status())
        out.update(self._site_paths())
        return out

    def site_put(self, include_unchecked=False):
        chosen, counts = self._site_files(include_unchecked)
        if not chosen:
            raise StudioError("There is nothing to put on the site yet. Files go on the site once you mark them OK on "
                              "the check screen.", 400)
        if not self.proc_lock.acquire(blocking=False):
            raise StudioError("The studio is busy cutting files. Try again in a moment.", 409)
        try:
            site = self._site()
            try:
                rep = site.put([(rel, src) for rel, src, _ in chosen])
            except siteput.SiteError as e:
                raise StudioError(str(e), 500)
            with self.lock:
                pending = self._site_pending()
                stamp = rep["at"]
                for rel in rep["changed"]:
                    pending[rel] = stamp
                self._site_write_pending(pending)
        finally:
            self.proc_lock.release()
        rep.pop("lines", None)
        rep.update(self._site_paths())
        rep["pending"] = ["/".join(siteput.PHONICS + (rel,)) for rel in sorted(self._site_pending())]
        rep["tried"] = len(chosen)
        return rep

    def site_uploaded(self):
        """Mike says he uploaded the files: empty the upload list."""
        with self.lock:
            n = len(self._site_pending())
            self._site_write_pending({})
        if n:
            self._site().log_note("Marked %d file(s) as uploaded to the live site." % n)
        return {"cleared": n}

    # ── recording sessions ─────────────────────────────────────────────────
    def _paths(self, sid):
        if not SESSION_ID.match(sid):
            raise StudioError("Bad session id.", 400)
        return self.takes / (sid + ".wav"), self.takes / (sid + ".json")

    def _load_meta(self, sid):
        wav, js = self._paths(sid)
        if not js.exists():
            raise StudioError("Unknown session.", 404)
        with open(js, encoding="utf-8") as f:
            return json.load(f)

    def new_session(self, list_name, sample_rate, mic=None):
        if list_name not in LISTS:
            raise StudioError("Unknown list.", 400)
        if not isinstance(sample_rate, int) or not (8000 <= sample_rate <= 192000):
            raise StudioError("Bad sample rate.", 400)
        with self.lock:
            base = datetime.datetime.now().strftime("%Y-%m-%d_%H-%M-%S")
            sid, n = "%s_%s" % (base, list_name), 1
            while (self.takes / (sid + ".json")).exists():
                n += 1
                sid = "%s-%d_%s" % (base, n, list_name)
            wav, js = self._paths(sid)
            with open(wav, "wb") as f:
                f.write(wav_header(sample_rate, 0))
            meta = {"format": 1, "id": sid, "list": list_name, "sample_rate": sample_rate, "channels": 1,
                    "sample_format": "int16", "created": now_iso(), "audio_file": sid + ".wav",
                    "audio_samples": 0, "finished": False, "closed": None, "mic": mic or {}, "items": {}, "events": []}
            atomic_write_json(js, meta)
        return {"id": sid}

    def append_audio(self, sid, offset, data):
        """Add raw 16-bit audio to the session's WAV. `offset` = samples the browser believes are already saved,
        so a repeated upload is never added twice. The WAV header is kept correct after every chunk."""
        if len(data) % 2:
            raise StudioError("Audio must be 16-bit samples.", 400)
        wav, _ = self._paths(sid)
        with self.lock:
            if not wav.exists():
                raise StudioError("Unknown session.", 404)
            have = (os.path.getsize(wav) - WAV_HEADER) // 2
            if offset > have:
                raise StudioError("Gap in the audio: the server has %d samples, upload starts at %d." % (have, offset), 409)
            data = data[(have - offset) * 2:]
            if data:
                with open(wav, "r+b") as f:
                    f.seek(WAV_HEADER + have * 2)
                    f.write(data)
                    size = have * 2 + len(data)
                    f.seek(4)
                    f.write(struct.pack("<I", 36 + size))
                    f.seek(40)
                    f.write(struct.pack("<I", size))
                    f.flush()
                    os.fsync(f.fileno())
                have += len(data) // 2
        return {"samples": have}

    def save_times(self, sid, payload):
        wav, js = self._paths(sid)
        with self.lock:
            meta = self._load_meta(sid)
            if isinstance(payload.get("items"), dict):
                meta["items"] = payload["items"]
            if isinstance(payload.get("events"), list):
                meta["events"] = payload["events"][-2000:]
            if isinstance(payload.get("mic"), dict):
                meta["mic"] = payload["mic"]
            meta["audio_samples"] = (os.path.getsize(wav) - WAV_HEADER) // 2
            meta["updated"] = now_iso()
            atomic_write_json(js, meta)
        return {"ok": True, "audio_samples": meta["audio_samples"]}

    def close_session(self, sid):
        wav, js = self._paths(sid)
        with self.lock:
            meta = self._load_meta(sid)
            meta["closed"] = now_iso()
            meta["audio_samples"] = (os.path.getsize(wav) - WAV_HEADER) // 2
            atomic_write_json(js, meta)
        return {"ok": True}

    # ── cutting and filing (R2) ────────────────────────────────────────────
    def process_list(self, list_name, rebuild=False):
        """Cut every unfinished recording of this list into files. rebuild=True remakes ALL files from the saved
        cuts (after changing a setting such as the loudness or the bitrate)."""
        if list_name not in LISTS or not LISTS[list_name]["ready"]:
            raise StudioError("That list is not ready yet.", 404)
        if not self.proc_lock.acquire(blocking=False):
            raise StudioError("The studio is already cutting. Wait a moment.", 409)
        try:
            return self._process(list_name, rebuild)
        finally:
            self.proc_lock.release()

    @staticmethod
    def _simple_rel(kind, item):
        return "words/%s.m4a" % item["word"] if kind == "words" else "sentences/%s.m4a" % item["id"]

    def _process(self, list_name, rebuild):
        kind = LISTS[list_name]["kind"]
        items = self.list_items(list_name)
        order = {it["key"]: n for n, it in enumerate(items)}
        by_key = {it["key"]: it for it in items}
        versions = {str(it["no"]): it["versions"] for it in items} if kind == "sounds" else {}

        def label(key):
            it = by_key.get(key)
            if not it:
                return key
            if kind != "sounds":
                return it["text"]
            return it["sound"] + (" (as in %s)" % it["key_word"] if it["versions"] > 1 else "")

        def title(no):
            first = [it for it in items if str(it["no"]) == str(no)]
            return first[0]["sound"] + (" (%s)" % " / ".join(i["key_word"] for i in first) if len(first) > 1 else "") if first else str(no)

        sessions = [m for m in self.sessions() if m.get("list") == list_name and not m.get("finished")]
        latest = {}
        for meta in sessions:
            for key, t in (meta.get("items") or {}).items():
                if not isinstance(t, dict) or t.get("start") is None or t.get("end") is None:
                    continue
                wall = float(t.get("wall") or 0)
                if key not in latest or wall >= latest[key][1]:
                    latest[key] = (meta, wall, t)

        report = {"cut": 0, "made": [], "waiting": [], "warnings": [], "errors": [], "output_dir": str(self.proc.out),
                  "rebuild": bool(rebuild), "sessions": len(sessions), "list": list_name}
        progress, files_meta = self.read_progress()
        touched, cut_keys = set(), []        # touched: sound numbers (sounds) or item keys (words, sentences)

        for key in sorted(latest, key=lambda k: order.get(k, 10 ** 9)):
            meta, wall, t = latest[key]
            it = by_key.get(key)
            if not it:
                report["errors"].append({"key": key, "label": key, "text": "this item is not in the list any more, so it was left alone"})
                continue
            try:
                entry, new = self.proc.store_cut(list_name, key, self.takes / meta["audio_file"], t, meta["id"])
            except (processing.ProcessingError, OSError, wave.Error, EOFError) as e:
                report["errors"].append({"key": key, "label": label(key), "text": str(e)})
                continue
            report["cut"] += 1 if new else 0
            touched.add(str(it["no"]) if kind == "sounds" else key)
            cut_keys.append(key)
            progress[key] = {"at": now_iso(), "session": meta["id"], "start": t["start"], "end": t["end"],
                             "forced": bool(t.get("forced")), "cut": entry["file"]}
            if t.get("flag"):
                progress[key]["flag"] = t["flag"]

        idx = self.proc.load_index(list_name)
        if rebuild:
            cut_keys = [k for k in idx["items"] if k in by_key]
            touched = {str(by_key[k]["no"]) for k in cut_keys} if kind == "sounds" else set(cut_keys)
        for key in cut_keys:
            for w in idx["items"].get(key, {}).get("warnings", []):
                report["warnings"].append({"key": key, "label": label(key), "text": w})

        if kind == "sounds":
            for no in sorted(touched, key=lambda n: min(order[k] for k in by_key if str(by_key[k]["no"]) == n)):
                try:
                    res = self.proc.build_sound(no, versions[no])
                except (processing.ProcessingError, OSError, wave.Error, subprocess.SubprocessError) as e:
                    report["errors"].append({"key": "sound:%s" % no, "label": title(no), "text": str(e)})
                    continue
                names = [f["file"] for f in res["files"]]
                all_keys = ["sound:%s:v%d" % (no, v) for v in range(1, versions[no] + 1)]
                for f in res["files"]:
                    f["title"] = title(no)
                    keys = all_keys if f["kind"] == "sounds" else ["sound:%s:v%d" % (no, f["version"])]
                    files_meta[f["file"]] = self._file_meta(f, no, title(no), keys)       # a freshly built file starts unchecked
                report["made"].extend(res["files"])
                if res["waiting"]:
                    report["waiting"].append({"no": no, "label": title(no), "text": res["waiting"]})
                for v in range(1, versions[no] + 1):
                    k = "sound:%s:v%d" % (no, v)
                    if k in progress:
                        progress[k]["files"] = names
        else:
            for key in sorted(touched, key=lambda k: order[k]):
                it = by_key[key]
                rel = self._simple_rel(kind, it)
                try:
                    f = self.proc.build_single(list_name, key, rel, kind)
                except (processing.ProcessingError, OSError, wave.Error, subprocess.SubprocessError) as e:
                    report["errors"].append({"key": key, "label": label(key), "text": str(e)})
                    continue
                f["title"] = it["text"]
                files_meta[rel] = self._file_meta(f, it.get("word") or it.get("id"), it["text"], [key])
                report["made"].append(f)
                if key in progress:
                    progress[key]["files"] = [rel]

        self.write_progress(progress, files_meta)
        for meta in sessions:                       # these recordings are now cut: they are finished
            try:
                with self.lock:
                    m = self._load_meta(meta["id"])
                    m["finished"] = True
                    m["processed_at"] = now_iso()
                    atomic_write_json(self.takes / (meta["id"] + ".json"), m)
            except (OSError, ValueError, StudioError):
                pass
        return report

    @staticmethod
    def _file_meta(f, group, title, keys):
        return {"kind": f["kind"], "no": group, "version": f["version"], "title": title, "keys": keys,
                "seconds": f["seconds"], "rms_db": f["rms_db"], "peak_db": f["peak_db"], "limited_by_peak": f["limited_by_peak"],
                "bytes": f["bytes"], "kbps": f["kbps"], "built": now_iso()}

    def reset_list(self, list_name, everything=False):
        """Start a list over. Unfinished takes move to takes/_old/<date>/. With everything=True the finished files,
        the lossless cuts and the progress of this list move there too. Nothing is ever deleted."""
        if list_name not in LISTS:
            raise StudioError("Unknown list.", 400)
        dest = self.takes / "_old" / datetime.datetime.now().strftime("%Y-%m-%d_%H-%M-%S")
        moved = 0
        with self.lock:
            for meta in self.sessions():
                if meta.get("list") != list_name or meta.get("finished"):
                    continue
                wav, js = self._paths(meta["id"])
                dest.mkdir(parents=True, exist_ok=True)
                for p in (wav, js):
                    if p.exists():
                        shutil.move(str(p), str(dest / p.name))
                moved += 1
            files = 0
            if everything:
                try:
                    keyset = {i["key"] for i in self.list_items(list_name)}
                except StudioError:
                    keyset = set()
                prog, fmeta = self.read_progress()
                mine_files = {rel for rel, f in fmeta.items() if keyset & set(f.get("keys", []))}
                mine_items = {k for k in prog if k in keyset}
                for rel in sorted(mine_files):                   # only this list's files (words/ is shared with Common words)
                    src = self.proc.out / rel
                    if src.exists():
                        (dest / "output" / rel).parent.mkdir(parents=True, exist_ok=True)
                        shutil.move(str(src), str(dest / "output" / rel))
                        files += 1
                cuts = self.proc.cut_dir(list_name)
                if cuts.exists():
                    dest.mkdir(parents=True, exist_ok=True)
                    shutil.move(str(cuts), str(dest / "cuts"))
                    files += 1
                if mine_items or mine_files:
                    dest.mkdir(parents=True, exist_ok=True)
                    atomic_write_json(dest / "progress-before.json", {"version": 2, "items": prog, "files": fmeta})
                    self.write_progress({k: v for k, v in prog.items() if k not in mine_items},
                                        {k: v for k, v in fmeta.items() if k not in mine_files})
                    files += 1
        return {"moved": moved, "folder": str(dest) if (moved or files) else None, "everything": bool(everything)}


def make_handler(studio, port):
    allowed_hosts = {"localhost:%d" % port, "127.0.0.1:%d" % port, "localhost", "127.0.0.1"}
    allowed_origins = {"http://localhost:%d" % port, "http://127.0.0.1:%d" % port}

    class Handler(BaseHTTPRequestHandler):
        server_version = "212Studio"

        def log_message(self, fmt, *args):
            if args and str(args[1]).startswith(("4", "5")):
                sys.stderr.write("  [%s] %s\n" % (self.log_date_time_string(), fmt % args))

        # -- helpers
        def send_bytes(self, status, body, ctype):
            self.send_response(status)
            self.send_header("Content-Type", ctype)
            self.send_header("Content-Length", str(len(body)))
            self.send_header("Cache-Control", "no-store")
            self.end_headers()
            self.wfile.write(body)

        def send_json(self, obj, status=200):
            self.send_bytes(status, json.dumps(obj, ensure_ascii=False).encode("utf-8"), "application/json")

        def read_body(self):
            n = int(self.headers.get("Content-Length") or 0)
            if n > 64 * 1024 * 1024:
                raise StudioError("Too much data.", 413)
            return self.rfile.read(n) if n else b""

        def read_json(self):
            raw = self.read_body()
            try:
                return json.loads(raw.decode("utf-8")) if raw else {}
            except ValueError:
                raise StudioError("Bad JSON.", 400)

        def guard(self, post):
            # Only this computer's own pages may talk to the studio (stops other websites from writing files).
            if (self.headers.get("Host") or "") not in allowed_hosts:
                raise StudioError("Wrong host.", 403)
            origin = self.headers.get("Origin")
            if post and origin and origin not in allowed_origins:
                raise StudioError("Wrong origin.", 403)

        def send_audio(self, path):
            # Safari will not play audio unless the server understands byte ranges ("give me bytes 0-1")
            data = path.read_bytes()
            size = len(data)
            start, end, status = 0, size - 1, 200
            rng = self.headers.get("Range") or ""
            if rng.startswith("bytes=") and "," not in rng:
                a, _, b = rng[6:].partition("-")
                try:
                    if a == "":
                        start = max(0, size - int(b))
                    else:
                        start = int(a)
                        end = min(size - 1, int(b)) if b else size - 1
                except ValueError:
                    start, end = 0, size - 1
                if start > end or start >= size:
                    self.send_response(416)
                    self.send_header("Content-Range", "bytes */%d" % size)
                    self.send_header("Content-Length", "0")
                    self.end_headers()
                    return
                status = 206
            body = data[start:end + 1]
            self.send_response(status)
            self.send_header("Content-Type", "audio/mp4")
            self.send_header("Accept-Ranges", "bytes")
            self.send_header("Content-Length", str(len(body)))
            if status == 206:
                self.send_header("Content-Range", "bytes %d-%d/%d" % (start, end, size))
            self.send_header("Cache-Control", "no-store")
            self.end_headers()
            self.wfile.write(body)

        def static(self, base, rel):
            base = Path(base).resolve()
            target = (base / rel).resolve()
            if not str(target).startswith(str(base) + os.sep) or not target.is_file():
                raise StudioError("Not found.", 404)
            self.send_bytes(200, target.read_bytes(), MIME.get(target.suffix.lower(), "application/octet-stream"))

        # -- routes
        def do_GET(self):
            try:
                self.guard(False)
                path = urlparse(self.path).path
                if path in ("/", "/index.html"):
                    return self.static(studio.web, "index.html")
                if path == "/api/config":
                    return self.send_json(studio.api_config())
                if path == "/api/lists":
                    return self.send_json(studio.api_lists())
                if path == "/api/settings":
                    return self.send_json(studio.api_settings())
                m = re.match(r"^/api/list/([a-z]+)$", path)
                if m:
                    return self.send_json(studio.api_list(m.group(1)))
                m = re.match(r"^/api/audio/(out|robot)/([a-z]+)/([^/]+)$", path)
                if m:
                    return self.send_audio(studio.audio_path(m.group(1), m.group(2), m.group(3)))
                m = re.match(r"^/api/review/([a-z]+)$", path)
                if m:
                    return self.send_json(studio.api_review(m.group(1)))
                if path == "/api/site/plan":
                    q = parse_qs(urlparse(self.path).query)
                    return self.send_json(studio.api_site_plan((q.get("unchecked") or ["0"])[0] == "1"))
                if path.startswith("/testaudio/"):
                    return self.static(studio.test_audio, path[len("/testaudio/"):])
                if path.startswith("/api/"):
                    raise StudioError("Not found.", 404)
                return self.static(studio.web, path.lstrip("/"))
            except StudioError as e:
                self.send_json({"error": str(e)}, e.status)
            except (BrokenPipeError, ConnectionResetError):
                pass
            except Exception as e:  # keep the server alive whatever happens
                sys.stderr.write("  ERROR %r\n" % (e,))
                self.send_json({"error": "Something went wrong in the studio: %s" % e}, 500)

        def do_POST(self):
            try:
                self.guard(True)
                url = urlparse(self.path)
                path, q = url.path, parse_qs(url.query)
                if path == "/api/session/new":
                    b = self.read_json()
                    return self.send_json(studio.new_session(b.get("list", ""), b.get("sample_rate"), b.get("mic")))
                m = re.match(r"^/api/session/([^/]+)/(audio|times|close)$", path)
                if m:
                    sid, what = m.group(1), m.group(2)
                    if what == "audio":
                        offset = int((q.get("offset") or ["0"])[0])
                        return self.send_json(studio.append_audio(sid, offset, self.read_body()))
                    if what == "times":
                        return self.send_json(studio.save_times(sid, self.read_json()))
                    return self.send_json(studio.close_session(sid))
                if path == "/api/reset":
                    b = self.read_json()
                    return self.send_json(studio.reset_list(b.get("list", ""), bool(b.get("everything"))))
                if path == "/api/settings":
                    b = self.read_json()
                    return self.send_json(studio.set_options(b.get("values"), bool(b.get("reset"))))
                if path == "/api/review":
                    b = self.read_json()
                    return self.send_json(studio.set_review(b.get("files"), b.get("review")))
                if path == "/api/site/put":
                    b = self.read_json()
                    return self.send_json(studio.site_put(bool(b.get("include_unchecked"))))
                if path == "/api/site/uploaded":
                    self.read_json()
                    return self.send_json(studio.site_uploaded())
                if path == "/api/process":
                    b = self.read_json()
                    return self.send_json(studio.process_list(b.get("list", ""), bool(b.get("rebuild"))))
                raise StudioError("Not found.", 404)
            except StudioError as e:
                self.send_json({"error": str(e)}, e.status)
            except ValueError:
                self.send_json({"error": "Bad request."}, 400)
            except (BrokenPipeError, ConnectionResetError):
                pass
            except Exception as e:
                sys.stderr.write("  ERROR %r\n" % (e,))
                self.send_json({"error": "Something went wrong in the studio: %s" % e}, 500)

    return Handler


def main():
    ap = argparse.ArgumentParser(description="212 Recording Studio")
    ap.add_argument("--port", type=int, default=DEFAULT_PORT)
    ap.add_argument("--no-browser", action="store_true", help="do not open the browser")
    args = ap.parse_args()

    studio = Studio(Path(__file__).resolve().parent)
    url = "http://localhost:%d" % args.port
    try:
        httpd = ThreadingHTTPServer(("127.0.0.1", args.port), make_handler(studio, args.port))
    except OSError:
        print("\nThe studio seems to be running already (port %d is busy)." % args.port)
        print("Opening %s for you.\n" % url)
        if not args.no_browser:
            webbrowser.open(url)
        return 0
    httpd.daemon_threads = True

    print("\n  212 Recording Studio (%s) is running." % VERSION)
    print("  Open %s  (it should open by itself)" % url)
    print("  Website folder: %s" % studio.settings["website_folder"])
    if studio.settings_error:
        print("  NOTE: " + studio.settings_error)
    if not studio.csv_path("phonics-sounds.csv").exists():
        print("  WARNING: cannot find the website's CSV lists there. Check settings.json.")
    print("  To stop: press Ctrl+C in this window.\n")
    if not args.no_browser:
        threading.Timer(0.6, lambda: webbrowser.open(url)).start()
    try:
        httpd.serve_forever()
    except KeyboardInterrupt:
        print("\n  Stopped. Your recordings are in the takes/ folder.\n")
    return 0


if __name__ == "__main__":
    sys.exit(main())
