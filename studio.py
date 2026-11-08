#!/usr/bin/env python3
"""212 Recording Studio: a tiny local server for recording your own voice for the 212 English Phonics pages.

Start it with:
    ~/kokoro-env/bin/python ~/Documents/212-recording-studio/studio.py

It opens http://localhost:8212 in your browser. Uses only Python's standard library.
It only READS the website's CSV lists. It never writes to the website folder.
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

VERSION = "R2"
DEFAULT_PORT = 8212
DEFAULT_SETTINGS = {
    "website_folder": "~/Documents/212english-website",
    "mic_tip": "Same distance from the microphone every time (about a fist away), "
               "and the same gain setting on the mic. Speak across it, not straight into it.",
}

# The lists. Only Sounds works in R1; the others come in later steps (the cards show "coming soon").
LISTS = {
    "sounds":    {"title": "Sounds",          "silence_ms": 500, "prompt": "Say the sound once", "ready": True,
                  "note": "67 sounds, 8 of them with two versions", "prefix": "sound:", "output_dirs": ["sounds", "alone"]},
    "words":     {"title": "Challenge words", "silence_ms": 700, "prompt": "Say the word", "ready": False,
                  "note": "Rare words and names. Coming in a later step."},
    "sentences": {"title": "Sentences",       "silence_ms": 1000, "prompt": "Read the sentence", "ready": False,
                  "note": "Coming in a later step."},
    "common":    {"title": "Common words",    "silence_ms": 700, "prompt": "Say the word", "ready": False,
                  "note": "Later."},
}

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

    def list_items(self, name):
        if name == "sounds":
            return self.sounds_items()
        raise StudioError("That list is not ready yet.", 404)

    def list_total(self, name):
        try:
            if name == "sounds":
                return len(self.sounds_items())
            if name == "words":
                rows = self.read_csv("phonics-words.csv")
                return len({r["word"] for r in rows if r["status"] in ("rare", "name")})
            if name == "common":
                rows = self.read_csv("phonics-words.csv")
                return len({r["word"] for r in rows if r["status"] == "common"})
            if name == "sentences":
                return len(self.read_csv("phonics-sentences.csv"))
        except StudioError:
            return None
        return None

    # ── progress and takes on disk ─────────────────────────────────────────
    def progress_items(self):
        """progress.json: { "items": { key: {at, session, start, end, forced, cut, files} } }. An item is done once it is cut."""
        p = self.root / "progress.json"
        if not p.exists():
            return {}
        try:
            with open(p, encoding="utf-8") as f:
                data = json.load(f)
            items = data.get("items", {}) if isinstance(data, dict) else {}
            return {k: v for k, v in items.items() if isinstance(v, dict)}
        except (OSError, ValueError):
            return {}

    def progress_done(self):
        return self.progress_items()

    def write_progress(self, items):
        atomic_write_json(self.root / "progress.json", {"version": 1, "updated": now_iso(), "items": items})

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
            "mic_tip": self.settings.get("mic_tip", ""),
            "website_folder": self.settings["website_folder"],
            "website_ok": self.csv_path("phonics-sounds.csv").exists(),
            "settings_error": self.settings_error,
        }

    def api_lists(self):
        done = self.progress_done()
        cards = []
        for name, conf in LISTS.items():
            total = self.list_total(name)
            count = pending = 0
            if conf["ready"]:
                try:
                    keys = [i["key"] for i in self.list_items(name)]
                    recorded, _ = self.recorded_and_today(name)
                    count = sum(1 for k in keys if k in done or k in recorded)
                    pending = sum(1 for k in keys if k in recorded)
                except StudioError:
                    pass
            cards.append({"id": name, "title": conf["title"], "ready": conf["ready"], "note": conf["note"],
                          "total": total, "done": count, "pending": pending})
        return {"lists": cards}

    def api_list(self, name):
        if name not in LISTS or not LISTS[name]["ready"]:
            raise StudioError("That list is not ready yet.", 404)
        conf = LISTS[name]
        items = self.list_items(name)
        recorded, today = self.recorded_and_today(name)
        return {"list": name, "title": conf["title"], "silence_ms": conf["silence_ms"], "prompt": conf["prompt"],
                "items": items, "done": self.progress_done(), "recorded": recorded, "today": today}

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
        if list_name != "sounds":
            raise StudioError("That list is not ready yet.", 404)
        if not self.proc_lock.acquire(blocking=False):
            raise StudioError("The studio is already cutting. Wait a moment.", 409)
        try:
            return self._process_sounds(rebuild)
        finally:
            self.proc_lock.release()

    def _process_sounds(self, rebuild):
        items = self.sounds_items()
        order = {it["key"]: n for n, it in enumerate(items)}
        by_key = {it["key"]: it for it in items}
        versions = {str(it["no"]): it["versions"] for it in items}

        def label(key):
            it = by_key.get(key)
            if not it:
                return key
            return it["sound"] + (" (as in %s)" % it["key_word"] if it["versions"] > 1 else "")

        def title(no):
            first = [it for it in items if str(it["no"]) == str(no)]
            return first[0]["sound"] + (" (%s)" % " / ".join(i["key_word"] for i in first) if len(first) > 1 else "") if first else str(no)

        sessions = [m for m in self.sessions() if m.get("list") == "sounds" and not m.get("finished")]
        latest = {}
        for meta in sessions:
            for key, t in (meta.get("items") or {}).items():
                if not isinstance(t, dict) or t.get("start") is None or t.get("end") is None:
                    continue
                wall = float(t.get("wall") or 0)
                if key not in latest or wall >= latest[key][1]:
                    latest[key] = (meta, wall, t)

        report = {"cut": 0, "made": [], "waiting": [], "warnings": [], "errors": [], "output_dir": str(self.proc.out),
                  "rebuild": bool(rebuild), "sessions": len(sessions)}
        progress = self.progress_items()
        touched, cut_keys = set(), []

        for key in sorted(latest, key=lambda k: order.get(k, 10 ** 9)):
            meta, wall, t = latest[key]
            it = by_key.get(key)
            if not it:
                report["errors"].append({"key": key, "label": key, "text": "this item is not in the sounds list any more, so it was left alone"})
                continue
            try:
                entry, new = self.proc.store_cut("sounds", key, self.takes / meta["audio_file"], t, meta["id"])
            except (processing.ProcessingError, OSError, wave.Error, EOFError) as e:
                report["errors"].append({"key": key, "label": label(key), "text": str(e)})
                continue
            report["cut"] += 1 if new else 0
            touched.add(str(it["no"]))
            cut_keys.append(key)
            progress[key] = {"at": now_iso(), "session": meta["id"], "start": t["start"], "end": t["end"],
                             "forced": bool(t.get("forced")), "cut": entry["file"]}

        idx = self.proc.load_index("sounds")
        if rebuild:
            touched = {k.split(":")[1] for k in idx["items"] if k in by_key}
            cut_keys = [k for k in idx["items"] if k in by_key]
        for key in cut_keys:
            for w in idx["items"].get(key, {}).get("warnings", []):
                report["warnings"].append({"key": key, "label": label(key), "text": w})

        for no in sorted(touched, key=lambda n: min(order[k] for k in by_key if str(by_key[k]["no"]) == n)):
            try:
                res = self.proc.build_sound(no, versions[no])
            except (processing.ProcessingError, OSError, wave.Error, subprocess.SubprocessError) as e:
                report["errors"].append({"key": "sound:%s" % no, "label": title(no), "text": str(e)})
                continue
            names = [f["file"] for f in res["files"]]
            for f in res["files"]:
                f["title"] = title(no)
            report["made"].extend(res["files"])
            if res["waiting"]:
                report["waiting"].append({"no": no, "label": title(no), "text": res["waiting"]})
            for v in range(1, versions[no] + 1):
                k = "sound:%s:v%d" % (no, v)
                if k in progress:
                    progress[k]["files"] = names

        self.write_progress(progress)
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
                conf = LISTS[list_name]
                for d in conf.get("output_dirs", []):
                    src = self.proc.out / d
                    if src.exists() and any(src.iterdir()):
                        (dest / "output").mkdir(parents=True, exist_ok=True)
                        shutil.move(str(src), str(dest / "output" / d))
                        files += 1
                cuts = self.proc.cut_dir(list_name)
                if cuts.exists():
                    dest.mkdir(parents=True, exist_ok=True)
                    shutil.move(str(cuts), str(dest / "cuts"))
                    files += 1
                prog = self.progress_items()
                mine = {k: v for k, v in prog.items() if k.startswith(conf.get("prefix", "\0"))}
                if mine:
                    dest.mkdir(parents=True, exist_ok=True)
                    atomic_write_json(dest / "progress-before.json", {"version": 1, "items": prog})
                    self.write_progress({k: v for k, v in prog.items() if k not in mine})
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
                m = re.match(r"^/api/list/([a-z]+)$", path)
                if m:
                    return self.send_json(studio.api_list(m.group(1)))
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
