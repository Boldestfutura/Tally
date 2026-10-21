#!/usr/bin/env python3
"""Root cron worker: install an explicitly requested Tally update from GitHub."""
import fcntl
import json
import os
import re
import shutil
import subprocess
import tempfile
import time
import urllib.request
from pathlib import Path

APP_DIR = Path(__file__).resolve().parent
APP_NAME = APP_DIR.parent.name
DATA_DIR = APP_DIR.parent / "data"
REQUEST = DATA_DIR / "update.req"
STATUS = DATA_DIR / "update-status.json"
REPO = "Boldestfutura/Tally"
BRANCH = "main"
FILES = ("server.py", "VERSION", "icon.svg", "icon-180.png", "icon-192.png",
         "icon-512.png", "icon-512-maskable.png", "update-worker.py",
         "install-worker.sh", "README.md")
MAX_FILE = 2 * 1024 * 1024


def installed_version():
    try:
        src = (APP_DIR / "server.py").read_text()
        m = re.search(r'^APP_VERSION\s*=\s*["\']([^"\']+)', src, re.M)
        return m.group(1) if m else "0.0.0"
    except OSError:
        return "0.0.0"


def write_status(state, current, latest, message, available=False):
    DATA_DIR.mkdir(parents=True, exist_ok=True)
    obj = {"current": current, "latest": latest, "available": bool(available),
           "state": state, "message": message, "checked": int(time.time())}
    fd, tmp = tempfile.mkstemp(prefix=".update-status-", dir=DATA_DIR)
    try:
        with os.fdopen(fd, "w") as fh:
            json.dump(obj, fh)
            fh.flush()
            os.fsync(fh.fileno())
        os.replace(tmp, STATUS)
    except BaseException:
        try:
            os.unlink(tmp)
        except OSError:
            pass
        raise


def fetch(name):
    url = "https://raw.githubusercontent.com/%s/%s/%s" % (REPO, BRANCH, name)
    req = urllib.request.Request(url, headers={"User-Agent": "Tally-updater"})
    with urllib.request.urlopen(req, timeout=30) as r:
        data = r.read(MAX_FILE + 1)
    if not data or len(data) > MAX_FILE:
        raise ValueError("invalid size for %s" % name)
    return data


def main():
    DATA_DIR.mkdir(parents=True, exist_ok=True)
    with open(DATA_DIR / ".update-lock", "w") as lock_file:
        try:
            fcntl.flock(lock_file, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError:
            return 0
        return _run_request()


def _run_request():
    if not REQUEST.exists():
        return 0
    try:
        request = json.loads(REQUEST.read_text() or "{}")
    except (OSError, ValueError):
        request = {}
    REQUEST.unlink(missing_ok=True)

    current = installed_version()
    latest = current
    try:
        latest = fetch("VERSION").decode("ascii").strip()
        if not re.fullmatch(r"\d+\.\d+\.\d+", latest):
            raise ValueError("GitHub VERSION is not x.y.z")
        if tuple(map(int, latest.split("."))) <= tuple(map(int, current.split("."))):
            write_status("current", current, latest, "Tally is already up to date")
            return 0

        write_status("installing", current, latest, "Downloading Tally v%s" % latest, True)
        with tempfile.TemporaryDirectory(prefix=".tally-release-", dir=DATA_DIR) as td:
            stage = Path(td)
            for name in FILES:
                (stage / name).write_bytes(fetch(name))

            server = (stage / "server.py").read_text()
            version = re.search(r'^APP_VERSION\s*=\s*["\']([^"\']+)', server, re.M)
            if not version or version.group(1) != latest:
                raise ValueError("server.py and VERSION disagree")
            compile(server, "server.py", "exec")
            if (stage / "icon.svg").read_text().lstrip()[:4] != "<svg":
                raise ValueError("icon.svg is invalid")
            for name in ("icon-180.png", "icon-192.png", "icon-512.png", "icon-512-maskable.png"):
                if not (stage / name).read_bytes().startswith(b"\x89PNG\r\n\x1a\n"):
                    raise ValueError("%s is not a PNG" % name)

            backup = DATA_DIR / "update-backups" / (current + "-" + str(int(time.time())))
            backup.mkdir(parents=True, exist_ok=True)
            for name in FILES:
                old = APP_DIR / name
                if old.is_file():
                    shutil.copy2(old, backup / name)

            for name in FILES:
                target = APP_DIR / name
                temp = target.with_name(target.name + ".new")
                shutil.copyfile(stage / name, temp)
                os.replace(temp, target)
            for name in ("server.py", "update-worker.py", "install-worker.sh"):
                os.chmod(APP_DIR / name, 0o755)

        write_status("restarting", current, latest, "Redeploying Tally v%s" % latest, True)
        result = subprocess.run(["midclt", "call", "-j", "app.redeploy", APP_NAME],
                                capture_output=True, text=True, timeout=300)
        if result.returncode:
            raise RuntimeError((result.stderr or result.stdout or "app.redeploy failed")[-400:])
        write_status("done", latest, latest, "Tally v%s is installed" % latest, False)
        return 0
    except Exception as e:
        write_status("error", current, latest,
                     "Update failed: %s" % str(e)[:250], latest != current)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
