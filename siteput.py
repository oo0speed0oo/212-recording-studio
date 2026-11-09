"""R5: put the finished files on the site.

This is the ONLY code that ever writes into the website's folders. It only COPIES new .m4a files into
portal/audio/phonics/. Before a file on the site is replaced, the old one is MOVED (never deleted) to
_extra_files/robot-voices/ in the website folder, and every change is written to replacements.log there.

Two places get the files:
  * the website folder (the git repo; this is what you commit and upload), and
  * the Local test site (optional), so you can try the new voice in the browser before uploading.
"""
import datetime
import hashlib
import os
import re
import shutil
import threading
from pathlib import Path

PHONICS = ("portal", "audio", "phonics")
BACKUP = ("_extra_files", "robot-voices")
LOG_NAME = "replacements.log"
EARLIER = "_earlier-versions"
SAFE_REL = re.compile(r"^(alone|sounds|words|sentences)/[A-Za-z0-9_-]+(\.[A-Za-z0-9_-]+)*\.m4a$")


class SiteError(Exception):
    pass


def sha(path):
    h = hashlib.sha256()
    with open(path, "rb") as f:
        for chunk in iter(lambda: f.read(1 << 20), b""):
            h.update(chunk)
    return h.hexdigest()


def expand(p):
    return Path(os.path.expanduser(str(p)))


class Site:
    def __init__(self, website_folder, local_folder=None):
        self.website = expand(website_folder)
        self.local = expand(local_folder) if local_folder else None
        self.lock = threading.Lock()

    # ── places ─────────────────────────────────────────────────────────────
    def phonics_dir(self, root):
        return Path(root).joinpath(*PHONICS)

    def backup_dir(self):
        return self.website.joinpath(*BACKUP)

    def targets(self):
        """[(name, phonics dir)] for the places that exist. The website folder is the main one."""
        out = []
        if self.phonics_dir(self.website).is_dir():
            out.append(("website", self.phonics_dir(self.website)))
        if self.local and self.phonics_dir(self.local).is_dir():
            out.append(("local", self.phonics_dir(self.local)))
        return out

    def status(self):
        return {"website_ok": self.phonics_dir(self.website).is_dir(),
                "local_ok": bool(self.local and self.phonics_dir(self.local).is_dir())}

    # ── what would happen to one file ──────────────────────────────────────
    def classify(self, rel, src, phonics):
        """add = nothing there yet · same = already the same bytes · robot = a robot voice (or any first version)
        will be saved to robot-voices and replaced · known = the live file is a copy of what is already saved
        in robot-voices, so it is just replaced · earlier = a different earlier version, saved before replacing."""
        live = phonics / rel
        if not live.is_file():
            return "add"
        if sha(live) == sha(src):
            return "same"
        saved = self.backup_dir() / rel
        if not saved.is_file():
            return "robot"
        return "known" if sha(live) == sha(saved) else "earlier"

    @staticmethod
    def _check_rel(rel):
        if not SAFE_REL.match(rel) or ".." in rel:
            raise SiteError("Not a file name I will put on the site: %s" % rel)

    def plan(self, files):
        """files: [(rel, source Path)]. Returns {rel: {"website": action, "local": action}} (only for places that exist)."""
        t = self.targets()
        out = {}
        for rel, src in files:
            self._check_rel(rel)
            out[rel] = {name: self.classify(rel, src, ph) for name, ph in t}
        return out

    # ── doing it ───────────────────────────────────────────────────────────
    def _place(self, rel, src, phonics, action, stamp):
        """Copy src to phonics/rel. Returns a note for the log. The new file is copied next to the old one first,
        the old one is moved away (never deleted), then the new one is renamed into place."""
        live = phonics / rel
        live.parent.mkdir(parents=True, exist_ok=True)
        tmp = live.with_name(live.name + ".studio-tmp")
        shutil.copyfile(src, tmp)
        os.chmod(tmp, 0o644)
        if sha(tmp) != sha(src):
            tmp.unlink()
            raise SiteError("The copy of %s did not match. Nothing was changed." % rel)
        moved, note = None, ""
        try:
            if action == "robot":
                moved = self.backup_dir() / rel
                note = "robot voice saved to %s" % "/".join(BACKUP + (rel,))
            elif action == "earlier":
                moved = self.backup_dir() / EARLIER / stamp / rel
                if moved.exists():
                    # already saved in this run (the same earlier version in the other place) or a different one: never overwrite
                    if sha(moved) == sha(live):
                        note = "earlier version already saved in %s" % "/".join(BACKUP + (EARLIER, stamp))
                        moved = None
                    else:
                        n = 2
                        while moved.with_name("%s-%d%s" % (moved.stem, n, moved.suffix)).exists():
                            n += 1
                        moved = moved.with_name("%s-%d%s" % (moved.stem, n, moved.suffix))
                if moved is not None:
                    note = "earlier version saved to %s" % "/".join(BACKUP + (EARLIER, stamp, rel))
            elif action == "known":
                note = "robot voice already saved in %s" % "/".join(BACKUP)
            if moved is not None:
                moved.parent.mkdir(parents=True, exist_ok=True)
                shutil.move(str(live), str(moved))
            try:
                os.replace(tmp, live)
            except OSError:
                if moved is not None and moved.exists() and not live.exists():
                    shutil.move(str(moved), str(live))      # put the old file back
                raise
        finally:
            if tmp.exists():
                tmp.unlink()
        if sha(live) != sha(src):
            raise SiteError("%s was copied but does not match. Check it." % rel)
        return note

    def put(self, files):
        """files: [(rel, source Path)]. Copies them to every place that exists. Returns a report."""
        with self.lock:
            targets = self.targets()
            if not targets or targets[0][0] != "website":
                raise SiteError("I cannot find %s. Check website_folder in settings.json."
                                % "/".join(("<website folder>",) + PHONICS))
            now = datetime.datetime.now().astimezone()
            stamp = now.strftime("%Y-%m-%d_%H-%M-%S")
            rep = {"stamp": stamp, "at": now.isoformat(timespec="seconds"), "places": [n for n, _ in targets],
                   "added": 0, "replaced": 0, "same": 0, "errors": [], "changed": [], "lines": []}
            for rel, _ in files:
                self._check_rel(rel)
            for name, phonics in targets:
                for rel, src in files:
                    try:
                        action = self.classify(rel, src, phonics)         # judged just now, not from an earlier look
                        if action == "same":
                            note = "already the same"
                            if name == "website":
                                rep["same"] += 1
                        else:
                            note = self._place(rel, src, phonics, action, stamp) or "new file"
                            if name == "website":
                                if action == "add":
                                    rep["added"] += 1
                                else:
                                    rep["replaced"] += 1
                                rep["changed"].append(rel)
                        rep["lines"].append("[%s] %-6s %s  (%s)" % (name, "ADDED" if action == "add" else "SAME" if action == "same" else "REPLACED", rel, note))
                    except (SiteError, OSError) as e:
                        rep["errors"].append({"file": rel, "place": name, "text": str(e)})
                        rep["lines"].append("[%s] ERROR  %s  (%s)" % (name, rel, e))
            self._log(rep, files)
            return rep

    def _log(self, rep, files):
        d = self.backup_dir()
        d.mkdir(parents=True, exist_ok=True)
        head = "== %s  Put %d file(s) on the site: %d new, %d replaced, %d already there, %d problem(s). Places: %s" % (
            rep["at"], len(files), rep["added"], rep["replaced"], rep["same"], len(rep["errors"]), ", ".join(rep["places"]))
        with open(d / LOG_NAME, "a", encoding="utf-8") as f:
            f.write(head + "\n")
            for line in rep["lines"]:
                f.write("   " + line + "\n")
        rep["log_file"] = str(d / LOG_NAME)

    def log_note(self, text):
        d = self.backup_dir()
        d.mkdir(parents=True, exist_ok=True)
        with open(d / LOG_NAME, "a", encoding="utf-8") as f:
            f.write("== %s  %s\n" % (datetime.datetime.now().astimezone().isoformat(timespec="seconds"), text))
