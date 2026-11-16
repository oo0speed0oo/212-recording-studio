"""R8 part 2: copy the finished files (and, if you want, the lossless cuts and raw recordings) to a server, or to another folder.

It only COPIES. It never deletes anything, here or on the server, and never uses rsync's --delete options. A file that already
exists on the server and has changed (a sound you redid) is replaced, but the old version is first kept on the server in
<folder>/_replaced/<date>/ . It uses your own `ssh` and `rsync` (both come with macOS), with an SSH key: it never asks for
or handles a password (BatchMode), and it never trusts a new server by itself (you accept the server's fingerprint once in Terminal).

Leave the host empty to copy to another folder on this computer (a mounted drive or shared folder) instead of a server.
"""
import datetime
import os
import re
import shutil
import subprocess
import time
from pathlib import Path

HOST_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._@:\[\]-]*$")          # user@host, host, an alias from ~/.ssh/config, an IP; never starts with -
REMOTE_RE = re.compile(r"^/[A-Za-z0-9._/+@=-]+$")                     # a full path on the server, no spaces or shell characters
KEY_RE = re.compile(r"^[A-Za-z0-9._/~+@=-]+$")
EXCLUDES = (".DS_Store", "*.studio-tmp", "*.tmp")
LOG_NAME = "server-copy.log"
SSH_OPTS = ("-o", "BatchMode=yes", "-o", "ConnectTimeout=10", "-o", "ServerAliveInterval=15")


class CopyError(Exception):
    pass


def validate(host, folder, key=""):
    """Check what was typed. Returns (host, folder, key) cleaned. Raises CopyError with a plain message."""
    host, folder, key = (host or "").strip(), (folder or "").strip(), (key or "").strip()
    if not folder:
        raise CopyError("Type the folder to copy into.")
    if host:
        if not HOST_RE.match(host):
            raise CopyError("The server name looks wrong. Use something like mike@100.79.65.95, or a name from your ~/.ssh/config.")
        if not REMOTE_RE.match(folder):
            raise CopyError("The folder on the server must be a full path starting with / and without spaces, for example /mnt/data/212-recordings.")
    else:
        if not (folder.startswith("~") or os.path.isabs(folder)):
            raise CopyError("The folder must be a full path (or start with ~).")
    if key:
        if not KEY_RE.match(key):
            raise CopyError("The key file path must not contain spaces or odd characters.")
        if not Path(os.path.expanduser(key)).is_file():
            raise CopyError("I cannot find the SSH key file %s." % key)
    return host, folder, key


def friendly_ssh_error(text, host):
    t = (text or "").strip()
    low = t.lower()
    if "host key verification failed" in low or "remote host identification has changed" in low:
        return ("This computer does not know that server yet (or its fingerprint changed). In Terminal type:  ssh %s  , check that "
                "the fingerprint is really your server, answer yes, then press Ctrl+D to leave. Then try again." % host)
    if "permission denied" in low:
        return ("The server did not accept the key. Set it up once in Terminal (you type the password there yourself):  "
                "ssh-copy-id %s  . Then try again." % host)
    if "could not resolve hostname" in low or "name or service not known" in low:
        return "I cannot find a server called %s. Check the name, and that Tailscale is connected on this computer." % host
    if "timed out" in low or "no route to host" in low or "network is unreachable" in low:
        return "The server did not answer. Is it switched on, and is Tailscale connected on both computers?"
    if "connection refused" in low:
        return "The server is there but SSH is not running on it (connection refused)."
    return t or "The server connection failed."


class ServerCopy:
    def __init__(self, host, folder, key="", rsync=None, ssh=None):
        self.host, self.folder, self.key = validate(host, folder, key)
        self.remote = bool(self.host)
        self.rsync = rsync or shutil.which("rsync")
        self.ssh = ssh or shutil.which("ssh")

    # ── commands ───────────────────────────────────────────────────────────
    def ssh_args(self):
        a = [self.ssh or "ssh"] + list(SSH_OPTS)
        if self.key:
            a += ["-i", os.path.expanduser(self.key)]
        return a

    def dest_root(self):
        """Where things go: the server folder (a path on the server) or the local folder."""
        return self.folder if self.remote else os.path.expanduser(self.folder)

    def target(self, part):
        path = "%s/%s" % (self.dest_root().rstrip("/"), part)
        return ("%s:%s/" % (self.host, path)) if self.remote else path + "/"

    def rsync_cmd(self, src, part, stamp, dry=False):
        backup = "%s/_replaced/%s/%s" % (self.dest_root().rstrip("/"), stamp, part)
        cmd = [self.rsync or "rsync", "-rt", "--partial", "--itemize-changes", "--backup", "--backup-dir=" + backup]
        cmd += ["--exclude=" + e for e in EXCLUDES]
        if dry:
            cmd.append("-n")
        if self.remote:
            cmd += ["-e", " ".join(self.ssh_args())]
        cmd += ["--", str(src).rstrip("/") + "/", self.target(part)]
        return cmd

    # ── test ───────────────────────────────────────────────────────────────
    def test(self):
        """{"ok": bool, "message": text}. Remote: logs in with the key (no password) and makes sure the folder can be written."""
        if not self.rsync:
            return {"ok": False, "message": "rsync was not found on this computer."}
        if not self.remote:
            try:
                p = Path(self.dest_root())
                p.mkdir(parents=True, exist_ok=True)
                probe = p / (".studio-write-test-%d" % os.getpid())
                probe.write_bytes(b"ok")
                probe.unlink()
            except OSError as e:
                return {"ok": False, "message": "I cannot write in %s (%s)." % (p, e.strerror or e)}
            return {"ok": True, "message": "This folder works: %s" % p}
        if not self.ssh:
            return {"ok": False, "message": "ssh was not found on this computer."}
        cmd = self.ssh_args() + ["--", self.host, "mkdir -p %s && test -w %s && echo STUDIO-OK" % (self.folder, self.folder)]
        try:
            r = subprocess.run(cmd, capture_output=True, text=True, timeout=40)
        except (OSError, subprocess.SubprocessError) as e:
            return {"ok": False, "message": "The connection test did not run (%s)." % e}
        if r.returncode == 0 and "STUDIO-OK" in r.stdout:
            return {"ok": True, "message": "Connected to %s and %s can be written." % (self.host, self.folder)}
        if r.returncode == 0:
            return {"ok": False, "message": "Connected, but I cannot write in %s on the server." % self.folder}
        return {"ok": False, "message": friendly_ssh_error(r.stderr or r.stdout, self.host)}

    # ── copy ───────────────────────────────────────────────────────────────
    @staticmethod
    def _walk(src):
        n = 0
        for p in Path(src).rglob("*"):
            if p.is_file() and p.name != ".DS_Store" and not p.name.endswith((".studio-tmp", ".tmp")):
                n += 1
        return n

    @staticmethod
    def parse_itemize(lines):
        """rsync --itemize-changes lines -> [(path, is_new)] for the files that are (or would be) sent."""
        out = []
        for line in lines:
            m = re.match(r"^(\S{2})(\S{7,9}) (.+)$", line.rstrip("\n"))
            if not m or m.group(1)[1] != "f" or m.group(1)[0] not in "<>":
                continue
            flags = set(m.group(2))
            # new files show +++++++ ; a preview into a folder that does not exist yet shows only dots (nothing there to compare with)
            out.append((m.group(3), flags <= {"+"} or flags <= {"."}))
        return out

    def copy(self, parts, dry=False, log_dir=None):
        """parts: [(name, source folder)]. Sends what is new or changed. dry=True only says what would be sent."""
        if not self.rsync:
            raise CopyError("rsync was not found on this computer.")
        stamp = datetime.datetime.now().strftime("%Y-%m-%d_%H-%M-%S")
        started = time.time()
        rep = {"dry": bool(dry), "stamp": stamp, "new": 0, "updated": 0, "unchanged": 0, "bytes": 0, "files": [],
               "parts": [], "where": ("%s:%s" % (self.host, self.folder)) if self.remote else os.path.expanduser(self.folder),
               "backup_folder": "%s/_replaced/%s" % (self.dest_root().rstrip("/"), stamp)}
        for name, src in parts:
            src = Path(src)
            if not src.is_dir():
                continue
            total = self._walk(src)
            if not self.remote and not dry:
                try:
                    Path(self.target(name)).mkdir(parents=True, exist_ok=True)
                except OSError as e:
                    raise CopyError("I cannot write in %s (%s)." % (self.target(name), e.strerror or e))
            try:
                r = subprocess.run(self.rsync_cmd(src, name, stamp, dry), capture_output=True, text=True, timeout=3600)
            except (OSError, subprocess.SubprocessError) as e:
                raise CopyError("The copy did not run (%s)." % e)
            if r.returncode != 0:
                msg = friendly_ssh_error(r.stderr or r.stdout, self.host) if self.remote else (r.stderr.strip() or "rsync failed")
                raise CopyError("%s (the %s part; rsync code %d)" % (msg, name, r.returncode))
            sent = self.parse_itemize(r.stdout.splitlines())
            for rel, is_new in sent:
                rep["new" if is_new else "updated"] += 1
                try:
                    rep["bytes"] += (src / rel).stat().st_size
                except OSError:
                    pass
                if len(rep["files"]) < 500:
                    rep["files"].append("%s/%s" % (name, rel))
            rep["unchanged"] += max(0, total - len(sent))
            rep["parts"].append(name)
        rep["seconds"] = round(time.time() - started, 1)
        if log_dir is not None:
            self._log(Path(log_dir), rep)
        return rep

    def _log(self, d, rep):
        d.mkdir(parents=True, exist_ok=True)
        with open(d / LOG_NAME, "a", encoding="utf-8") as f:
            f.write("== %s  %s to %s: %d new, %d replaced, %d unchanged, %.1f MB, %s s. Parts: %s\n" % (
                datetime.datetime.now().astimezone().isoformat(timespec="seconds"), "PREVIEW" if rep["dry"] else "COPY",
                rep["where"], rep["new"], rep["updated"], rep["unchanged"], rep["bytes"] / 1048576.0, rep["seconds"], ", ".join(rep["parts"])))
            if not rep["dry"]:
                for rel in rep["files"]:
                    f.write("   " + rel + "\n")
