# -*- coding: utf-8 -*-
"""Tests for the GitHub auto-updater.

Runs against a throwaway local HTTP server that impersonates the GitHub API, so
nothing here touches the network. The executable-swap script is executed for
real against dummy files, because that is the part that must not go wrong.
"""
import hashlib
import http.server
import json
import os
import shutil
import sys
import tempfile
import threading
import time

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import hr_updater as U  # noqa: E402

FAILURES = []
CHECKS = []


def check(name, ok, detail=""):
    CHECKS.append(name)
    if not ok:
        FAILURES.append(name)
    print(("  OK   " if ok else "  FAIL ") + name + ((" -> " + detail) if detail else ""))


# ---------------------------------------------------------------- fixtures ---
ASSET = os.urandom(700_003)
ASSET_SHA = hashlib.sha256(ASSET).hexdigest()
RELEASE = {
    "tag_name": "v1.11.0",
    "name": "HRtoVRC Studio 1.11.0",
    "body": "- новая фича\n- починен баг",
    "html_url": "https://example.invalid/releases/tag/v1.11.0",
    "draft": False,
    "prerelease": False,
    "assets": [
        {"name": "notes.txt", "size": 10,
         "browser_download_url": "http://HOST/notes.txt"},
        {"name": "HRtoVRC_Studio.exe", "size": len(ASSET),
         "browser_download_url": "http://HOST/HRtoVRC_Studio.exe"},
    ],
}
STATE = {"release": RELEASE, "truncate": False}


class Handler(http.server.BaseHTTPRequestHandler):
    protocol_version = "HTTP/1.1"

    def log_message(self, *a):
        pass

    def do_GET(self):
        if self.path.endswith(".exe"):
            body = ASSET[: len(ASSET) // 2] if STATE["truncate"] else ASSET
            self.send_response(200)
            self.send_header("Content-Type", "application/octet-stream")
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)
            return
        payload = json.dumps(STATE["release"]).encode("utf-8")
        self.send_response(200)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(payload)))
        self.end_headers()
        self.wfile.write(payload)


srv = http.server.ThreadingHTTPServer(("127.0.0.1", 0), Handler)
HOST = "127.0.0.1:%d" % srv.server_address[1]
threading.Thread(target=srv.serve_forever, daemon=True).start()

for a in RELEASE["assets"]:
    a["browser_download_url"] = a["browser_download_url"].replace("HOST", HOST)
U.GITHUB_API_LATEST = "http://" + HOST + "/repos/{repo}/releases/latest"

TMP = tempfile.mkdtemp(prefix="hrupd_test_")


# ------------------------------------------------------------------- tests ---
print("\n=== version comparison ===")
check("parse_version strips the v", U.parse_version("v1.10.2") == (1, 10, 2),
      str(U.parse_version("v1.10.2")))
check("newer patch wins", U.is_newer("v1.10.3", "1.10.2"))
check("newer minor wins", U.is_newer("v1.11.0", "1.10.9"))
check("same version is not newer", not U.is_newer("v1.10.2", "1.10.2"))
check("older is not newer", not U.is_newer("v1.9.9", "1.10.0"))
check("1.10 beats 1.9 (not string order)", U.is_newer("v1.10.0", "1.9.0"))
check("short tag vs long current", not U.is_newer("v1.10", "1.10.2"))
check("garbage tag is ignored", not U.is_newer("latest", "1.10.2"))

print("\n=== repo configuration guard ===")
check("placeholder is not configured", not U.repo_is_configured("OWNER/REPO"))
check("empty is not configured", not U.repo_is_configured(""))
check("bare name is not configured", not U.repo_is_configured("HRtoVRC"))
check("owner/name is configured", U.repo_is_configured("howl/HRtoVRC-Studio"))

print("\n=== check_for_update ===")
check("skipped while unconfigured", U.check_for_update("OWNER/REPO", "1.0.0") is None)

info = U.check_for_update("a/b", "1.10.2")
check("finds the newer release", info is not None and info.tag == "v1.11.0",
      repr(info))
check("picks the .exe asset, not the .txt",
      info.asset_name == "HRtoVRC_Studio.exe", str(info.asset_name))
check("carries size and notes", info.asset_size == len(ASSET) and "фича" in info.notes)
check("carries the release page url", info.html_url.endswith("v1.11.0"))

check("no update when already current", U.check_for_update("a/b", "1.11.0") is None)
check("no update when ahead", U.check_for_update("a/b", "2.0.0") is None)

STATE["release"] = dict(RELEASE, prerelease=True)
check("prereleases are skipped", U.check_for_update("a/b", "1.10.2") is None)
STATE["release"] = dict(RELEASE, draft=True)
check("drafts are skipped", U.check_for_update("a/b", "1.10.2") is None)
STATE["release"] = RELEASE

print("\n=== download_asset ===")
seen = []
dest = os.path.join(TMP, "dl")
path = U.download_asset(info, dest_dir=dest, progress_cb=lambda d, t: seen.append((d, t)))
check("file downloaded", bool(path) and os.path.isfile(path), str(path))
check("content matches byte for byte",
      hashlib.sha256(open(path, "rb").read()).hexdigest() == ASSET_SHA)
check("progress was reported", len(seen) > 2 and seen[-1][0] == len(ASSET), str(seen[-1:]))
check("progress knew the total size", seen[-1][1] == len(ASSET))
check("no .part left behind", not os.path.exists(path + ".part"))

STATE["truncate"] = True
try:
    U.download_asset(info, dest_dir=os.path.join(TMP, "dl2"))
    check("short download is rejected", False, "no error raised")
except Exception as e:
    check("short download is rejected", "не полностью" in str(e), str(e))
check("no half file kept after a short download",
      not os.path.exists(os.path.join(TMP, "dl2", "HRtoVRC_Studio.exe")))
STATE["truncate"] = False

cancel_after = {"n": 0}


def cancelling():
    cancel_after["n"] += 1
    return cancel_after["n"] > 1


out = U.download_asset(info, dest_dir=os.path.join(TMP, "dl3"), is_cancelled=cancelling)
check("cancelling returns None", out is None, str(out))
check("cancelling leaves nothing on disk",
      not os.listdir(os.path.join(TMP, "dl3")), str(os.listdir(os.path.join(TMP, "dl3"))))

print("\n=== executable swap script (executed for real) ===")
swap_dir = os.path.join(TMP, "swap")
os.makedirs(swap_dir)
target = os.path.join(swap_dir, "app.bat")
source = os.path.join(swap_dir, "new_app.bat")
marker = os.path.join(swap_dir, "restarted.txt")

with open(target, "w", encoding="ascii", newline="\r\n") as f:
    f.write("@echo off\r\necho OLD\r\n")
with open(source, "w", encoding="ascii", newline="\r\n") as f:
    f.write('@echo off\r\necho NEW> "%s"\r\n' % marker)

# Inspect a throwaway copy first, then let the updater build and run its own.
preview = U.build_swap_script(source, target, os.path.join(swap_dir, "preview.bat"))
check("swap script written", os.path.isfile(preview))
raw = open(preview, "rb").read()
text = raw.decode("mbcs", "replace")
check("script targets the right files", target in text and source in text)
check("script uses CRLF", b"\r\n" in raw)
os.remove(preview)

script = U.apply_update_and_restart(source, target)
check("swap script path returned",
      bool(script) and script.lower().endswith(".bat"), str(script))

deadline = time.time() + 30
while time.time() < deadline and not os.path.exists(marker):
    time.sleep(0.2)

check("new build replaced the old one",
      os.path.exists(target) and "NEW" in open(target, encoding="ascii").read(),
      open(target, encoding="ascii").read().strip() if os.path.exists(target) else "gone")
check("downloaded file was consumed", not os.path.exists(source))
check("backup cleaned up", not os.path.exists(target + ".old"))
check("app was restarted", os.path.exists(marker))

for _ in range(40):
    if not os.path.exists(script):
        break
    time.sleep(0.2)
check("swap script deleted itself", not os.path.exists(script))

srv.shutdown()
shutil.rmtree(TMP, ignore_errors=True)

print("\n" + "=" * 62)
print("passed %d/%d" % (len(CHECKS) - len(FAILURES), len(CHECKS)))
if FAILURES:
    print("FAILED: " + "; ".join(FAILURES))
sys.exit(1 if FAILURES else 0)
