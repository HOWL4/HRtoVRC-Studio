# -*- coding: utf-8 -*-
"""GitHub-release based auto-update for HRtoVRC Studio.

Pure standard library, no extra dependencies, and completely GUI-free: the app
calls :func:`check_for_update` from a worker thread and drives its own dialogs.

Flow for a frozen (PyInstaller) build:

1. :func:`check_for_update` reads ``/releases/latest`` and compares the tag with
   the running version.
2. :func:`download_asset` streams the release's ``.exe`` next to the current one
   (or into %TEMP% when that folder is read-only).
3. :func:`apply_update_and_restart` writes a small .bat that waits for this
   process to release the file lock, swaps the executable, restarts it and
   deletes itself. The app only has to quit afterwards.

Running from source there is nothing to swap, so the caller should just open
:attr:`UpdateInfo.html_url` in a browser instead.
"""

import json
import os
import re
import subprocess
import sys
import tempfile
import time
from urllib.request import Request, urlopen

GITHUB_API_LATEST = "https://api.github.com/repos/{repo}/releases/latest"
USER_AGENT = "HRtoVRC-Studio-Updater"
PLACEHOLDER_REPO = "OWNER/REPO"

# Windows process creation flags (kept literal so the module imports anywhere).
_DETACHED_PROCESS = 0x00000008
_CREATE_NEW_PROCESS_GROUP = 0x00000200
_CREATE_NO_WINDOW = 0x08000000

_CHUNK = 256 * 1024


def repo_is_configured(repo):
    """True once GITHUB_REPO has been pointed at a real ``owner/name``."""
    repo = str(repo or "").strip()
    return bool(repo) and repo != PLACEHOLDER_REPO and repo.count("/") == 1


def parse_version(text):
    """``"v1.10.2"`` -> ``(1, 10, 2)``. Unparsable input gives ``()``."""
    return tuple(int(n) for n in re.findall(r"\d+", str(text or ""))[:4])


def is_newer(remote, local):
    """True when the `remote` version string is strictly newer than `local`."""
    r = parse_version(remote)
    if not r:
        return False
    l = parse_version(local)
    width = max(len(r), len(l))
    return r + (0,) * (width - len(r)) > l + (0,) * (width - len(l))


def running_frozen():
    return bool(getattr(sys, "frozen", False))


def current_executable():
    return os.path.abspath(sys.executable)


class UpdateInfo(object):
    """One newer release, with the Windows asset to install."""

    def __init__(self, tag, version, title, notes, html_url,
                 asset_name=None, asset_url=None, asset_size=0):
        self.tag = tag
        self.version = version
        self.title = title
        self.notes = notes or ""
        self.html_url = html_url
        self.asset_name = asset_name
        self.asset_url = asset_url
        self.asset_size = int(asset_size or 0)

    @property
    def has_asset(self):
        return bool(self.asset_url)

    def __repr__(self):
        return "<UpdateInfo %s asset=%s>" % (self.tag, self.asset_name)


def _pick_asset(assets):
    """Best .exe in the release: prefer our own name, then the largest one."""
    exes = [a for a in assets if str(a.get("name", "")).lower().endswith(".exe")]
    if not exes:
        return None
    exes.sort(
        key=lambda a: ("hrtovrc" in str(a.get("name", "")).lower(), int(a.get("size") or 0)),
        reverse=True,
    )
    return exes[0]


def _get_json(url, timeout):
    req = Request(url, headers={"User-Agent": USER_AGENT,
                                "Accept": "application/vnd.github+json"})
    with urlopen(req, timeout=timeout) as resp:
        return json.loads(resp.read().decode("utf-8", "replace"))


def check_for_update(repo, current_version, timeout=10.0):
    """Return an :class:`UpdateInfo` when a newer release exists, else None.

    Network and API errors are raised to the caller, so a manual «Проверить
    обновления» can report them while the silent startup check swallows them.
    """
    if not repo_is_configured(repo):
        return None
    data = _get_json(GITHUB_API_LATEST.format(repo=repo.strip()), timeout)
    if data.get("draft") or data.get("prerelease"):
        return None
    tag = str(data.get("tag_name") or data.get("name") or "").strip()
    if not is_newer(tag, current_version):
        return None
    asset = _pick_asset(data.get("assets") or []) or {}
    return UpdateInfo(
        tag=tag,
        version=".".join(str(n) for n in parse_version(tag)),
        title=str(data.get("name") or tag),
        notes=str(data.get("body") or ""),
        html_url=str(data.get("html_url") or ""),
        asset_name=asset.get("name"),
        asset_url=asset.get("browser_download_url"),
        asset_size=asset.get("size") or 0,
    )


def _writable_dir(path):
    try:
        probe = os.path.join(path, ".hrupd_write_test")
        with open(probe, "wb") as f:
            f.write(b"ok")
        os.remove(probe)
        return True
    except Exception:
        return False


def download_dir():
    """Next to the running .exe when possible, otherwise %TEMP%."""
    if running_frozen():
        base = os.path.dirname(current_executable())
        if base and _writable_dir(base):
            return base
    return tempfile.mkdtemp(prefix="hrtovrc_update_")


def download_asset(info, dest_dir=None, progress_cb=None, is_cancelled=None):
    """Stream the release asset to disk and return the finished file's path.

    `progress_cb(done_bytes, total_bytes)` is called as data arrives; `total` is
    0 when the server does not report a length. Returns None if `is_cancelled`
    starts returning True mid-download.
    """
    if not info.has_asset:
        raise ValueError("У релиза нет .exe для установки")

    dest_dir = dest_dir or download_dir()
    os.makedirs(dest_dir, exist_ok=True)
    final = os.path.join(dest_dir, info.asset_name or "HRtoVRC_Studio_update.exe")
    part = final + ".part"
    for stale in (final, part):
        try:
            os.remove(stale)
        except OSError:
            pass

    req = Request(info.asset_url, headers={"User-Agent": USER_AGENT})
    done = 0
    with urlopen(req, timeout=30.0) as resp:
        total = int(resp.headers.get("Content-Length") or info.asset_size or 0)
        if progress_cb:
            progress_cb(0, total)
        with open(part, "wb") as f:
            while True:
                if is_cancelled and is_cancelled():
                    f.close()
                    try:
                        os.remove(part)
                    except OSError:
                        pass
                    return None
                chunk = resp.read(_CHUNK)
                if not chunk:
                    break
                f.write(chunk)
                done += len(chunk)
                if progress_cb:
                    progress_cb(done, total)

    expected = int(info.asset_size or 0)
    if expected and done != expected:
        try:
            os.remove(part)
        except OSError:
            pass
        raise IOError("Файл скачан не полностью: %d из %d байт" % (done, expected))

    os.replace(part, final)
    return final


_SWAP_BAT = u"""@echo off
setlocal enableextensions
set "TARGET={target}"
set "SOURCE={source}"
set "BACKUP=%TARGET%.old"
set /a TRIES=0

:wait
set /a TRIES+=1
del "%BACKUP%" >nul 2>&1
move /y "%TARGET%" "%BACKUP%" >nul 2>&1
if not errorlevel 1 goto swap
if %TRIES% GEQ 90 goto fail
ping -n 2 127.0.0.1 >nul
goto wait

:swap
move /y "%SOURCE%" "%TARGET%" >nul 2>&1
if errorlevel 1 goto rollback
del "%BACKUP%" >nul 2>&1
start "" "%TARGET%"
goto done

:rollback
move /y "%BACKUP%" "%TARGET%" >nul 2>&1
start "" "%TARGET%"
goto done

:fail
del "%SOURCE%" >nul 2>&1
start "" "%TARGET%"

:done
(goto) 2>nul & del "%~f0"
"""


def build_swap_script(new_exe, target_exe, script_path=None):
    """Write the .bat that replaces `target_exe` with `new_exe` and restarts it."""
    script_path = script_path or os.path.join(
        tempfile.gettempdir(),
        "hrtovrc_update_%d_%d.bat" % (os.getpid(), int(time.time())),
    )
    text = _SWAP_BAT.format(target=os.path.abspath(target_exe),
                            source=os.path.abspath(new_exe))
    # cmd.exe reads .bat in the OEM/ANSI code page, not UTF-8 - matters as soon
    # as the install path contains non-Latin characters.
    for encoding in ("mbcs", "cp1251", "utf-8"):
        try:
            with open(script_path, "w", encoding=encoding, newline="\r\n") as f:
                f.write(text)
            return script_path
        except (LookupError, UnicodeEncodeError):
            continue
    with open(script_path, "w", encoding="utf-8", errors="replace", newline="\r\n") as f:
        f.write(text)
    return script_path


def apply_update_and_restart(new_exe, target_exe=None):
    """Launch the detached swap script. The caller must quit right afterwards."""
    target_exe = os.path.abspath(target_exe or current_executable())
    script = build_swap_script(new_exe, target_exe)
    flags = _DETACHED_PROCESS | _CREATE_NEW_PROCESS_GROUP | _CREATE_NO_WINDOW
    subprocess.Popen(
        ["cmd.exe", "/c", script],
        creationflags=flags,
        close_fds=True,
        cwd=tempfile.gettempdir(),
    )
    return script
