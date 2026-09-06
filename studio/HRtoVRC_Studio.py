"""HRtoVRC Studio.

A single-page dashboard for streaming a BLE heart-rate sensor to VRChat
over OSC. This is NOT the old tabbed UI nor the sidebar "redesign" — it is a new
application built around real-time visualisation:

  * an animated ECG-style waveform that beats in time with the measured rate,
  * a zoomable BPM/media timeline with saved day archive,
  * a heart-rate zone meter,
  * live MIN / AVG / MAX statistics,
  * in-place settings and archive navigation.

All BLE / OSC / settings logic is reused unchanged from the original ``HRGUI10``
backend (imported from the parent folder), so the device behaviour matches the
shipped app — only the interface and the visualisations are new.

Run:  python HRtoVRC_Studio.py
"""

import asyncio
import html
import json
import math
import os
import queue
import random
import re
import sqlite3
import sys
import tempfile
import threading
import time
from collections import deque
from urllib.parse import parse_qs, quote, unquote, urlparse
from urllib.request import Request, urlopen

from PySide6.QtCore import Qt, QTimer, QRectF, QPointF, QUrl
from PySide6.QtGui import (
    QDesktopServices,
    QIcon,
    QPainter,
    QPen,
    QColor,
    QFont,
    QBrush,
    QLinearGradient,
    QPainterPath,
    QPolygonF,
    QPixmap,
)
from PySide6.QtWidgets import (
    QApplication,
    QCheckBox,
    QComboBox,
    QDoubleSpinBox,
    QFormLayout,
    QFrame,
    QGridLayout,
    QHBoxLayout,
    QLabel,
    QLineEdit,
    QMainWindow,
    QMessageBox,
    QProgressDialog,
    QPushButton,
    QScrollArea,
    QSizePolicy,
    QSpinBox,
    QStackedWidget,
    QTextBrowser,
    QTextEdit,
    QToolTip,
    QVBoxLayout,
    QWidget,
)

# --- Make the original backend importable from the parent directory ---------
_THIS_DIR = os.path.dirname(os.path.abspath(__file__))
_PARENT_DIR = os.path.dirname(_THIS_DIR)
for _p in (_PARENT_DIR, _THIS_DIR):
    if _p and _p not in sys.path:
        sys.path.insert(0, _p)

from HRGUI10 import (  # noqa: E402
    BleakClient,
    BleakScanner,
    BLEAK_IMPORT_ERROR,
    DEFAULTS,
    HR_CHAR_UUID,
    LAST_DEVICE_FILE,
    NAME_KEYWORDS,
    SETTINGS_FILE,
    AsyncRunner,
    SimpleUDPClient,
    load_last_device,
    load_settings,
    normalize_settings,
    parse_hr,
    save_last_device,
    save_settings,
)

import hr_updater  # noqa: E402

APP_VERSION = "1.10.2"
# Where auto-update looks for releases. Set this to your own "owner/name" once
# the repository exists; while it is the placeholder, update checks are skipped.
GITHUB_REPO = "OWNER/REPO"
APP_AUTHOR = "_howl"
APP_DISCORD = "howl64"
ICON_FILE = "HRtoVRC.ico"

FUNNY_COMMENTS = [
    "сердце в эфире",
    "кардио DLC",
    "живой сигнал",
    "режим турбо",
    "пульс одобряет",
    "почти спорт",
    "ещё держимся",
    "сердечко онлайн",
    "ритм пойман",
    "VR выдерживает",
]

# --- Palette ----------------------------------------------------------------
COL_BG = "#0a0e14"
COL_PANEL = "#121822"
COL_PANEL2 = "#18202c"
COL_BORDER = "#26303d"
COL_TEXT = "#e8eef5"
COL_MUTED = "#7d8b9a"
COL_ACCENT = "#ff3b6b"
COL_GREEN = "#37d67a"
COL_MIN = "#4aa3ff"
COL_MAX = "#ff4d4d"
COL_GRID = "#1d2632"
COL_MEDIA = "#4cc9f0"
COL_MEDIA_DARK = "#173141"

ZONES = [
    (0, "нет сигнала", "#5a6573"),
    (40, "покой", "#4aa3ff"),
    (60, "спокойно", "#37d67a"),
    (100, "разогрев", "#e0b000"),
    (130, "кардио", "#ff8c1a"),
    (160, "интенсив", "#ff4d4d"),
    (180, "максимум", "#ff2d7a"),
]


class NoWheelComboBox(QComboBox):
    def wheelEvent(self, event):
        event.ignore()


class NoWheelSpinBox(QSpinBox):
    def wheelEvent(self, event):
        event.ignore()


class NoWheelDoubleSpinBox(QDoubleSpinBox):
    def wheelEvent(self, event):
        event.ignore()


def hr_zone(bpm):
    """Return (zone name, hex color) for a heart-rate value."""
    if bpm is None or bpm <= 0:
        return (ZONES[0][1], ZONES[0][2])
    name, color = ZONES[1][1], ZONES[1][2]
    for threshold, zname, zcolor in ZONES[1:]:
        if bpm >= threshold:
            name, color = zname, zcolor
    return (name, color)


LOG_TS_RE = re.compile(r"^(\d{4}\.\d{2}\.\d{2} \d{2}:\d{2}:\d{2})")
YOUTUBE_HOSTS = {"youtube.com", "www.youtube.com", "m.youtube.com", "youtu.be", "music.youtube.com"}


def default_vrc_log_dir():
    home = os.environ.get("USERPROFILE") or os.path.expanduser("~")
    return os.path.join(home, "AppData", "LocalLow", "VRChat", "VRChat")


def history_db_file():
    base = os.path.dirname(os.path.abspath(SETTINGS_FILE)) or _PARENT_DIR
    return os.path.join(base, "history.sqlite3")


def parse_vrc_log_time(line):
    match = LOG_TS_RE.match(line)
    if not match:
        return time.time()
    try:
        return time.mktime(time.strptime(match.group(1), "%Y.%m.%d %H:%M:%S"))
    except Exception:
        return time.time()


def find_latest_vrc_log(log_dir):
    try:
        entries = []
        for name in os.listdir(log_dir):
            if name.startswith("output_log_") and name.endswith(".txt"):
                path = os.path.join(log_dir, name)
                entries.append((os.path.getmtime(path), path))
        if entries:
            entries.sort(reverse=True)
            return entries[0][1]
    except Exception:
        return None
    return None


def read_recent_log_lines(path, max_bytes=2 * 1024 * 1024):
    try:
        size = os.path.getsize(path)
        with open(path, "rb") as f:
            start = max(0, size - max_bytes)
            f.seek(start)
            if start > 0:
                f.readline()
            raw = f.read()
        return raw.decode("utf-8", errors="replace").splitlines(), size
    except Exception:
        return [], 0


def youtube_id_from_url(url):
    try:
        parsed = urlparse(url)
        host = parsed.netloc.lower().split(":")[0]
        if host == "youtu.be":
            video_id = parsed.path.strip("/").split("/")[0]
            return video_id or None
        if host in YOUTUBE_HOSTS:
            qs = parse_qs(parsed.query)
            if qs.get("v"):
                return qs["v"][0]
            if parsed.path.startswith("/shorts/") or parsed.path.startswith("/embed/"):
                parts = [part for part in parsed.path.split("/") if part]
                return parts[1] if len(parts) > 1 else None
    except Exception:
        return None
    return None


def bdt_id_from_url(url):
    try:
        parsed = urlparse(url)
        host = parsed.netloc.lower().split(":")[0]
        if host not in {"bdt.ac", "www.bdt.ac", "bdt.media", "www.bdt.media"}:
            return None
        match = re.search(r"/(?:dance|api/danceinfo)/([0-9a-fA-F]{16,64})(?:\.mp4|\.webp)?", parsed.path)
        return match.group(1).lower() if match else None
    except Exception:
        return None


def media_key_from_url(url):
    video_id = youtube_id_from_url(url)
    if video_id:
        return f"youtube:{video_id}"
    bdt_id = bdt_id_from_url(url)
    if bdt_id:
        return f"bdt:{bdt_id}"
    try:
        parsed = urlparse(url)
        return f"{parsed.netloc.lower()}{parsed.path}".strip("/") or url
    except Exception:
        return url


def guess_media_title(url):
    video_id = youtube_id_from_url(url)
    if video_id:
        return f"YouTube · {video_id}"
    bdt_id = bdt_id_from_url(url)
    if bdt_id:
        return f"BDT · {bdt_id[:8]}"
    try:
        parsed = urlparse(url)
        host = parsed.netloc.replace("www.", "")
        path = unquote(parsed.path.strip("/").split("/")[-1]).replace("_", " ").replace("-", " ")
        if path:
            return f"{host} · {path[:70]}"
        return host or "VRChat video"
    except Exception:
        return "VRChat video"


def media_title_needs_lookup(event):
    if not event:
        return False
    key = str(event.get("key") or media_key_from_url(event.get("url", "")))
    title = str(event.get("title") or "").strip()
    if key.startswith("youtube:"):
        video_id = key.split(":", 1)[1]
        return not title or title == f"YouTube · {video_id}" or title == video_id
    if key.startswith("bdt:"):
        bdt_id = key.split(":", 1)[1]
        low = title.lower()
        return (
            not title
            or low.startswith("bdt ·")
            or low.startswith("bdt.ac ·")
            or bdt_id in low
            or low.endswith(".mp4")
        )
    return False


def _clean_remote_title(text):
    text = re.sub(r"<[^>]+>", "", str(text or ""))
    text = html.unescape(text)
    text = text.replace("\r", "\n").replace("∅", "\n")
    lines = [line.strip() for line in text.splitlines() if line.strip()]
    return lines


def resolve_bdt_title(url_or_id, timeout=4.0):
    bdt_id = bdt_id_from_url(url_or_id)
    if not bdt_id and re.fullmatch(r"[0-9a-fA-F]{16,64}", str(url_or_id or "")):
        bdt_id = str(url_or_id).lower()
    if not bdt_id:
        return None
    api = f"https://bdt.ac/api/danceinfo/{quote(bdt_id)}"
    try:
        request = Request(api, headers={"User-Agent": "Mozilla/5.0 HRtoVRC/1.0"})
        with urlopen(request, timeout=timeout) as response:
            text = response.read(256 * 1024).decode("utf-8", errors="replace")
    except Exception:
        return None
    parts = [part.strip() for part in text.split("∅") if part.strip()]
    candidates = []
    if len(parts) >= 4:
        candidates.extend(_clean_remote_title(parts[3]))
    for part in parts:
        candidates.extend(_clean_remote_title(part))
    for candidate in candidates:
        low = candidate.lower()
        if (
            candidate
            and len(candidate) >= 3
            and not low.startswith(("uploaded:", "added:", "tags", "http://", "https://"))
            and not re.search(r"\d+x\d+|mbit|fps", low)
        ):
            return candidate[:140]
    return None


def extract_duration_from_url(url):
    match = re.search(r"(?:[?&/]dur(?:=|%3D))([0-9]+(?:\.[0-9]+)?)", url, re.IGNORECASE)
    if not match:
        match = re.search(r"dur%3D([0-9]+(?:\.[0-9]+)?)", url, re.IGNORECASE)
    if match:
        try:
            return float(match.group(1))
        except Exception:
            return None
    return None


def resolve_media_title(url, timeout=3.0):
    bdt_title = resolve_bdt_title(url, timeout=min(timeout, 4.0))
    if bdt_title:
        return bdt_title
    video_id = youtube_id_from_url(url)
    if not video_id:
        return None
    target = f"https://www.youtube.com/watch?v={quote(video_id)}"
    apis = (
        f"https://noembed.com/embed?url={quote(target, safe='')}",
        f"https://www.youtube.com/oembed?format=json&url={quote(target, safe='')}",
    )
    headers = {
        "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) HRtoVRC/1.0",
        "Accept": "application/json,text/html;q=0.8,*/*;q=0.5",
    }
    for api in apis:
        try:
            request = Request(api, headers=headers)
            with urlopen(request, timeout=timeout) as response:
                data = json.loads(response.read().decode("utf-8", errors="replace"))
            title = html.unescape(str(data.get("title") or "")).strip()
            if title:
                return title
        except Exception:
            continue
    try:
        request = Request(target, headers=headers)
        with urlopen(request, timeout=timeout) as response:
            page = response.read(512 * 1024).decode("utf-8", errors="replace")
        patterns = (
            r'<meta\s+property=["\']og:title["\']\s+content=["\']([^"\']+)["\']',
            r'"title"\s*:\s*"((?:\\.|[^"\\])*)"',
            r"<title>(.*?)</title>",
        )
        for pattern in patterns:
            match = re.search(pattern, page, re.IGNORECASE | re.DOTALL)
            if not match:
                continue
            raw = match.group(1)
            try:
                raw = json.loads(f'"{raw}"') if "\\u" in raw or "\\/" in raw else raw
            except Exception:
                pass
            title = html.unescape(str(raw)).replace(" - YouTube", "").strip()
            if title and title.lower() != "youtube":
                return title
    except Exception:
        pass
    return None


def osc_value_for_hr(hr_value, cfg):
    mode = str(cfg.get("osc_value_mode", "int")).lower()
    if mode != "float":
        return int(hr_value)
    try:
        lo = float(cfg.get("osc_float_min", 0.0))
        hi = float(cfg.get("osc_float_max", 100.0))
    except Exception:
        lo, hi = 0.0, 100.0
    if hi <= lo:
        hi = lo + 1.0
    value = (float(hr_value) - lo) / (hi - lo)
    return round(max(0.0, min(1.0, value)), 4)


class HistoryStore:
    """Small SQLite archive for pulse samples and VRChat media events."""

    def __init__(self, path):
        self.path = path
        self.lock = threading.Lock()
        os.makedirs(os.path.dirname(path), exist_ok=True)
        self.conn = sqlite3.connect(path, check_same_thread=False)
        self.conn.execute("PRAGMA journal_mode=WAL")
        self.conn.execute("PRAGMA synchronous=NORMAL")
        self._init_schema()

    @staticmethod
    def day_key(ts):
        return time.strftime("%Y-%m-%d", time.localtime(float(ts)))

    def _init_schema(self):
        with self.lock:
            self.conn.executescript(
                """
                CREATE TABLE IF NOT EXISTS sessions (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    started_at REAL NOT NULL,
                    ended_at REAL,
                    app_version TEXT,
                    device TEXT
                );
                CREATE TABLE IF NOT EXISTS hr_samples (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    ts REAL NOT NULL,
                    day TEXT NOT NULL,
                    session_id INTEGER,
                    bpm INTEGER NOT NULL
                );
                CREATE TABLE IF NOT EXISTS media_events (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    external_id TEXT NOT NULL UNIQUE,
                    session_id INTEGER,
                    start_ts REAL NOT NULL,
                    end_ts REAL,
                    day TEXT NOT NULL,
                    title TEXT,
                    url TEXT,
                    media_key TEXT,
                    state TEXT,
                    source TEXT,
                    duration REAL
                );
                CREATE INDEX IF NOT EXISTS idx_hr_day_ts ON hr_samples(day, ts);
                CREATE INDEX IF NOT EXISTS idx_media_day_start ON media_events(day, start_ts);
                CREATE INDEX IF NOT EXISTS idx_sessions_started ON sessions(started_at);
                """
            )
            self.conn.commit()

    def start_session(self, app_version, device=None):
        now = time.time()
        try:
            with self.lock:
                cur = self.conn.execute(
                    "INSERT INTO sessions(started_at, app_version, device) VALUES (?, ?, ?)",
                    (now, str(app_version), device),
                )
                self.conn.commit()
                return int(cur.lastrowid)
        except Exception:
            return None

    def end_session(self, session_id):
        if session_id is None:
            return
        try:
            with self.lock:
                self.conn.execute("UPDATE sessions SET ended_at = ? WHERE id = ?", (time.time(), session_id))
                self.conn.commit()
        except Exception:
            pass

    def add_hr_sample(self, session_id, bpm, ts=None):
        ts = float(ts or time.time())
        try:
            with self.lock:
                self.conn.execute(
                    "INSERT INTO hr_samples(ts, day, session_id, bpm) VALUES (?, ?, ?, ?)",
                    (ts, self.day_key(ts), session_id, int(bpm)),
                )
                self.conn.commit()
        except Exception:
            pass

    def upsert_media_event(self, session_id, event):
        if not event:
            return
        start_ts = float(event.get("start") or time.time())
        key = str(event.get("key") or media_key_from_url(event.get("url", "")) or "media")
        external_id = f"{key}:{int(round(start_ts))}"
        end_ts = event.get("end")
        duration = event.get("duration")
        try:
            end_ts = float(end_ts) if end_ts is not None else None
        except Exception:
            end_ts = None
        try:
            duration = float(duration) if duration is not None else None
        except Exception:
            duration = None
        values = (
            external_id,
            session_id,
            start_ts,
            end_ts,
            self.day_key(start_ts),
            str(event.get("title") or guess_media_title(event.get("url", ""))),
            str(event.get("url") or ""),
            key,
            str(event.get("state") or ""),
            str(event.get("source") or "VRChat"),
            duration,
        )
        try:
            with self.lock:
                self.conn.execute(
                    """
                    INSERT INTO media_events(
                        external_id, session_id, start_ts, end_ts, day, title, url,
                        media_key, state, source, duration
                    ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                    ON CONFLICT(external_id) DO UPDATE SET
                        session_id = COALESCE(excluded.session_id, media_events.session_id),
                        end_ts = COALESCE(excluded.end_ts, media_events.end_ts),
                        title = COALESCE(NULLIF(excluded.title, ''), media_events.title),
                        url = COALESCE(NULLIF(excluded.url, ''), media_events.url),
                        state = COALESCE(NULLIF(excluded.state, ''), media_events.state),
                        source = COALESCE(NULLIF(excluded.source, ''), media_events.source),
                        duration = COALESCE(excluded.duration, media_events.duration)
                    """,
                    values,
                )
                self.conn.commit()
        except Exception:
            pass

    def update_media_title(self, media_key, title):
        media_key = str(media_key or "")
        title = str(title or "").strip()
        if not media_key or not title:
            return
        try:
            with self.lock:
                self.conn.execute(
                    "UPDATE media_events SET title = ? WHERE media_key = ?",
                    (title, media_key),
                )
                self.conn.commit()
        except Exception:
            pass

    def list_days(self):
        try:
            with self.lock:
                rows = self.conn.execute(
                    """
                    SELECT day FROM (
                        SELECT DISTINCT day FROM hr_samples
                        UNION
                        SELECT DISTINCT day FROM media_events
                    ) ORDER BY day DESC
                    """
                ).fetchall()
            return [row[0] for row in rows]
        except Exception:
            return []

    def load_day(self, day):
        try:
            with self.lock:
                samples = self.conn.execute(
                    "SELECT ts, bpm FROM hr_samples WHERE day = ? ORDER BY ts", (day,)
                ).fetchall()
                media_rows = self.conn.execute(
                    """
                    SELECT id, start_ts, end_ts, title, url, media_key, state, source, duration
                    FROM media_events WHERE day = ? ORDER BY start_ts
                    """,
                    (day,),
                ).fetchall()
        except Exception:
            return [], []
        media = []
        for row in media_rows:
            media.append(
                {
                    "id": int(row[0]),
                    "start": float(row[1]),
                    "end": float(row[2]) if row[2] is not None else None,
                    "title": row[3] or "",
                    "url": row[4] or "",
                    "key": row[5] or "",
                    "state": row[6] or "",
                    "source": row[7] or "VRChat",
                    "duration": float(row[8]) if row[8] is not None else None,
                }
            )
        return [(float(ts), int(bpm)) for ts, bpm in samples], media

    def close(self):
        try:
            with self.lock:
                self.conn.commit()
                self.conn.close()
        except Exception:
            pass


def _ecg_value(phase):
    """A synthetic ECG (P-QRS-T) sample for phase in [0, 1)."""
    v = 0.0
    v += 0.13 * math.exp(-(((phase - 0.16) / 0.028) ** 2))   # P wave
    v += -0.12 * math.exp(-(((phase - 0.30) / 0.012) ** 2))  # Q
    v += 1.00 * math.exp(-(((phase - 0.335) / 0.011) ** 2))  # R spike
    v += -0.28 * math.exp(-(((phase - 0.375) / 0.014) ** 2))  # S
    v += 0.26 * math.exp(-(((phase - 0.62) / 0.05) ** 2))    # T wave
    return v


class ECGWaveform(QWidget):
    """A scrolling ECG trace whose beat rate follows the measured BPM."""

    def __init__(self, parent=None):
        super().__init__(parent)
        self.setMinimumHeight(120)
        self.setSizePolicy(QSizePolicy.Expanding, QSizePolicy.Fixed)
        self._bpm = 0
        self._phase = 0.0
        self._last = time.monotonic()
        self._buf = deque([0.0] * 260, maxlen=260)
        self._timer = QTimer(self)
        self._timer.timeout.connect(self._tick)
        self._timer.start(25)

    def set_bpm(self, value):
        try:
            self._bpm = max(0, int(value))
        except Exception:
            self._bpm = 0

    def _tick(self):
        now = time.monotonic()
        dt = min(0.1, now - self._last)
        self._last = now
        if self._bpm > 0:
            beat = 60.0 / max(30, min(220, self._bpm))
            self._phase = (self._phase + dt / beat) % 1.0
            self._buf.append(_ecg_value(self._phase))
        else:
            # Idle flatline with a touch of noise.
            self._buf.append(random.uniform(-0.015, 0.015))
        self.update()

    def paintEvent(self, _event):
        p = QPainter(self)
        p.setRenderHint(QPainter.Antialiasing, True)
        w = self.width()
        h = self.height()
        mid = h * 0.55
        amp = h * 0.36

        # Baseline grid.
        p.setPen(QPen(QColor(COL_GRID), 1))
        p.drawLine(0, int(mid), w, int(mid))

        _, zcolor = hr_zone(self._bpm)
        color = QColor(zcolor if self._bpm > 0 else COL_MUTED)
        n = len(self._buf)
        if n < 2:
            p.end()
            return
        pts = []
        for i, val in enumerate(self._buf):
            x = i / (n - 1) * w
            y = mid - val * amp
            pts.append(QPointF(x, y))
        path = QPainterPath()
        path.moveTo(pts[0])
        for pt in pts[1:]:
            path.lineTo(pt)

        glow = QColor(color)
        glow.setAlpha(70)
        p.setPen(QPen(glow, 6, Qt.SolidLine, Qt.RoundCap, Qt.RoundJoin))
        p.drawPath(path)
        p.setPen(QPen(color, 2, Qt.SolidLine, Qt.RoundCap, Qt.RoundJoin))
        p.drawPath(path)

        # Leading dot.
        p.setBrush(QColor(COL_TEXT))
        p.setPen(Qt.NoPen)
        p.drawEllipse(pts[-1], 3.2, 3.2)
        p.end()


class HistoryGraph(QWidget):
    """BPM history plus a VRChat media lane."""

    MIN_WINDOW = 30.0
    MAX_WINDOW = 86400.0
    PRESET_WINDOWS = [60.0, 120.0, 300.0, 900.0, 1800.0, 3600.0, 21600.0, 86400.0]

    def __init__(self, window=300, parent=None):
        super().__init__(parent)
        self.setMinimumHeight(190)
        self.setSizePolicy(QSizePolicy.Expanding, QSizePolicy.Expanding)
        self.window = float(window)
        self.samples = deque(maxlen=172800)
        self.media_events = []
        self.view_offset = 0.0
        self.expanded = False
        self.fixed_end_time = None
        self.on_view_changed = None
        self.on_media_selected = None
        self.selected_media_identity = None
        self._drag_x = None
        self._drag_offset = 0.0
        self._hover_pos = None
        self._hover_sample = None
        self.setMouseTracking(True)
        self._timer = QTimer(self)
        self._timer.timeout.connect(self._tick)
        self._timer.start(500)

    def _tick(self):
        if self.view_offset > self.max_offset():
            self.view_offset = self.max_offset()
            self._emit_view_changed()
        self.update()

    def _emit_view_changed(self):
        if callable(self.on_view_changed):
            self.on_view_changed()

    @staticmethod
    def _duration_label(seconds):
        seconds = int(round(seconds))
        if seconds < 60:
            return f"{seconds} сек"
        minutes = seconds // 60
        if minutes < 60:
            return f"{minutes} мин"
        hours = minutes // 60
        rem = minutes % 60
        return f"{hours} ч {rem} мин" if rem else f"{hours} ч"

    def set_expanded(self, expanded):
        self.expanded = bool(expanded)
        self.setMinimumHeight(310 if self.expanded else 190)
        self.updateGeometry()
        self.update()

    def set_window(self, seconds):
        old_window = self.window
        self.window = max(self.MIN_WINDOW, min(self.MAX_WINDOW, float(seconds)))
        if self.view_offset > 1:
            center = self.view_offset + old_window / 2
            self.view_offset = max(0.0, center - self.window / 2)
        self.view_offset = min(self.view_offset, self.max_offset())
        self._emit_view_changed()
        self.update()

    def set_window_anchored(self, seconds, anchor_x):
        old_start, _old_end, now = self._visible_span()
        pad_l, plot_r, _pad_t, _media_h, _plot_t, _plot_b = self._plot_geometry()
        plot_w = max(1.0, plot_r - pad_l)
        anchor_frac = max(0.0, min(1.0, (float(anchor_x) - pad_l) / plot_w))
        anchor_time = old_start + self.window * anchor_frac
        self.window = max(self.MIN_WINDOW, min(self.MAX_WINDOW, float(seconds)))
        new_start = anchor_time - self.window * anchor_frac
        new_end = new_start + self.window
        self.view_offset = max(0.0, min(now - new_end, self.max_offset()))
        self._emit_view_changed()
        self.update()

    def set_fixed_end_time(self, end_time):
        self.fixed_end_time = float(end_time) if end_time is not None else None
        self.view_offset = min(self.view_offset, self.max_offset())
        self.update()

    def add_sample(self, bpm):
        if bpm and bpm > 0:
            self.samples.append((time.time(), int(bpm)))
            if self.view_offset <= 0.1:
                self.view_offset = 0.0
            self.update()

    def add_media_event(self, event):
        if not event:
            return
        event = dict(event)
        try:
            event["start"] = float(event.get("start") or time.time())
            if event.get("end") is not None:
                event["end"] = max(event["start"], float(event["end"]))
        except Exception:
            event["start"] = time.time()
            event["end"] = None
        found = False
        event_id = event.get("id")
        for old in self.media_events:
            same_id = event_id is not None and old.get("id") == event_id
            same_start = (
                event_id is None
                and old.get("key")
                and old.get("key") == event.get("key")
                and abs(float(old.get("start", 0)) - event["start"]) < 2.0
            )
            if same_id or same_start:
                old.update(event)
                found = True
                break
        if not found:
            self.media_events.append(event)
        self.media_events.sort(key=lambda item: float(item.get("start", 0)))
        self.media_events = self.media_events[-300:]
        self.update()

    def reset(self):
        self.samples.clear()
        self.media_events.clear()
        self.view_offset = 0.0
        self.selected_media_identity = None
        self.update()

    def _earliest_time(self):
        times = []
        if self.samples:
            times.append(self.samples[0][0])
        times.extend(float(ev.get("start", 0)) for ev in self.media_events if ev.get("start"))
        return min(times) if times else None

    def max_offset(self):
        earliest = self._earliest_time()
        if earliest is None:
            return 0.0
        end_ref = self.fixed_end_time if self.fixed_end_time is not None else time.time()
        return max(0.0, end_ref - earliest - self.window)

    def set_view_offset(self, seconds):
        self.view_offset = max(0.0, min(float(seconds), self.max_offset()))
        self._emit_view_changed()
        self.update()

    def step_view(self, direction):
        self.set_view_offset(self.view_offset + direction * self.window * 0.72)

    def view_label(self):
        window = self._duration_label(self.window)
        if self.view_offset <= 1:
            return f"сейчас · {window}"
        ago = self._duration_label(self.view_offset)
        return f"{ago} назад · {window}"

    @staticmethod
    def _media_color(event):
        state = str(event.get("state") or "loading").lower()
        key = str(event.get("key") or "")
        url = str(event.get("url") or "").lower()
        if state == "error":
            return QColor("#ff6b6b")
        if state == "ended":
            return QColor("#6f7d8d")
        if key.startswith("bdt:") or "bdt.ac/" in url or "bdt.media/" in url:
            return QColor("#37d67a")
        if key.startswith("youtube:") or "youtu" in url:
            return QColor(COL_MEDIA)
        return QColor("#b58cff")

    @staticmethod
    def _media_source_label(event):
        key = str(event.get("key") or "")
        url = str(event.get("url") or "").lower()
        if key.startswith("bdt:") or "bdt.ac/" in url or "bdt.media/" in url:
            return "BDT"
        if key.startswith("youtube:") or "youtu" in url:
            return "YouTube"
        return "VRChat"

    @staticmethod
    def _format_hover_time(ts):
        return time.strftime("%H:%M:%S", time.localtime(float(ts)))

    def _plot_geometry(self):
        w = self.width()
        h = self.height()
        pad_l, pad_r = 42, 14
        pad_t, pad_b = 12, 32
        media_h = 46 if self.expanded else 30
        plot_t = pad_t + media_h + 8
        plot_b = max(plot_t + 44, h - pad_b)
        return pad_l, w - pad_r, pad_t, media_h, plot_t, plot_b

    def _visible_span(self):
        now = self.fixed_end_time if self.fixed_end_time is not None else time.time()
        max_offset = self.max_offset()
        if self.view_offset > max_offset:
            self.view_offset = max_offset
        end_t = now - self.view_offset
        start_t = end_t - self.window
        return start_t, end_t, now

    @staticmethod
    def _media_identity(event):
        if not event:
            return None
        key = str(event.get("key") or event.get("url") or event.get("title") or "media")
        try:
            start = round(float(event.get("start") or 0.0), 1)
        except Exception:
            start = 0.0
        return key, start

    def set_selected_media(self, event):
        self.selected_media_identity = self._media_identity(event)
        self.update()

    def _is_selected_media(self, event):
        return self.selected_media_identity is not None and self._media_identity(event) == self.selected_media_identity

    def _media_rects(self, start_t, end_t, now, pad_l, plot_r, pad_t, media_h):
        plot_w = max(1.0, plot_r - pad_l)

        def px(t):
            return pad_l + plot_w * ((t - start_t) / self.window)

        y = pad_t + (5 if self.expanded else 6)
        rect_h = media_h - (11 if self.expanded else 12)
        for event in self.media_events:
            ev_start = float(event.get("start", 0))
            ev_end = self._event_end_for_paint(event, now)
            if ev_end < start_t or ev_start > end_t:
                continue
            x1 = max(pad_l, px(max(ev_start, start_t)))
            x2 = min(plot_r, px(min(ev_end, end_t)))
            if x2 < x1 + 5:
                x2 = min(plot_r, x1 + 5)
            yield QRectF(x1, y, max(5.0, x2 - x1), rect_h), event

    def wheelEvent(self, event):
        delta = event.angleDelta().y()
        if not delta:
            return
        if event.modifiers() & Qt.ControlModifier:
            index = min(range(len(self.PRESET_WINDOWS)), key=lambda i: abs(self.PRESET_WINDOWS[i] - self.window))
            index = max(0, index - 1) if delta > 0 else min(len(self.PRESET_WINDOWS) - 1, index + 1)
            self.set_window_anchored(self.PRESET_WINDOWS[index], event.position().x())
        else:
            self.set_view_offset(self.view_offset + (-1 if delta < 0 else 1) * self.window * 0.12)
        event.accept()

    def mousePressEvent(self, event):
        if event.button() == Qt.LeftButton:
            pos = event.position()
            pad_l, plot_r, pad_t, media_h, _plot_t, _plot_b = self._plot_geometry()
            start_t, end_t, now = self._visible_span()
            media_hits = list(self._media_rects(start_t, end_t, now, pad_l, plot_r, pad_t, media_h))
            for rect, media_event in reversed(media_hits):
                if rect.adjusted(-3, -5, 3, 5).contains(pos):
                    self.set_selected_media(media_event)
                    if callable(self.on_media_selected):
                        self.on_media_selected(dict(media_event))
                    event.accept()
                    return
            self._drag_x = event.position().x()
            self._drag_offset = self.view_offset
            event.accept()

    def mouseMoveEvent(self, event):
        if self._drag_x is None:
            self._update_hover(event.position())
            event.accept()
            return
        pad_l, plot_r, _pad_t, _media_h, _plot_t, _plot_b = self._plot_geometry()
        plot_w = max(1.0, plot_r - pad_l)
        dx = event.position().x() - self._drag_x
        self.set_view_offset(self._drag_offset + dx / plot_w * self.window)
        event.accept()

    def mouseReleaseEvent(self, event):
        if event.button() == Qt.LeftButton:
            self._drag_x = None
            event.accept()

    def mouseDoubleClickEvent(self, event):
        self.set_expanded(not self.expanded)
        event.accept()

    def leaveEvent(self, event):
        self._hover_pos = None
        self._hover_sample = None
        QToolTip.hideText()
        self.update()
        super().leaveEvent(event)

    def _update_hover(self, pos):
        pad_l, plot_r, _pad_t, _media_h, plot_t, plot_b = self._plot_geometry()
        if not (pad_l <= pos.x() <= plot_r and plot_t <= pos.y() <= plot_b):
            if self._hover_sample is not None:
                self._hover_sample = None
                self._hover_pos = None
                QToolTip.hideText()
                self.update()
            return
        start_t, end_t, _now = self._visible_span()
        visible = [(wall, bpm) for wall, bpm in self.samples if start_t <= wall <= end_t]
        if not visible:
            return
        target = start_t + (pos.x() - pad_l) / max(1.0, plot_r - pad_l) * self.window
        wall, bpm = min(visible, key=lambda item: abs(item[0] - target))
        if abs(wall - target) > max(4.0, self.window * 0.035):
            QToolTip.hideText()
            self._hover_sample = None
            self._hover_pos = None
            self.update()
            return
        zone, _color = hr_zone(bpm)
        self._hover_sample = (wall, bpm, zone)
        self._hover_pos = QPointF(pos.x(), pos.y())
        QToolTip.showText(
            self.mapToGlobal(pos.toPoint()),
            f"{self._format_hover_time(wall)}\n{bpm} BPM · {zone}",
            self,
        )
        self.update()

    def _event_end_for_paint(self, event, now):
        start = float(event.get("start", now))
        end = event.get("end")
        if end is not None:
            return float(end)
        duration = event.get("duration")
        if duration:
            try:
                return max(start + 5.0, min(now, start + float(duration)))
            except Exception:
                pass
        return max(now, start + 14.0)

    def paintEvent(self, _event):
        p = QPainter(self)
        p.setRenderHint(QPainter.Antialiasing, True)
        w = self.width()
        h = self.height()
        pad_l, plot_r, pad_t, media_h, plot_t, plot_b = self._plot_geometry()
        plot_w = max(1.0, plot_r - pad_l)
        plot_h = max(1.0, plot_b - plot_t)
        start_t, end_t, now = self._visible_span()

        def px(t):
            return pad_l + plot_w * ((t - start_t) / self.window)

        data = sorted((wall, v) for wall, v in self.samples if start_t <= wall <= end_t)

        # Grid and time axis.
        p.setPen(QPen(QColor(COL_GRID), 1))
        for gy in range(1, 5):
            y = plot_t + plot_h * gy / 5
            p.drawLine(int(pad_l), int(y), int(plot_r), int(y))
        for gx in range(0, 6):
            x = pad_l + plot_w * gx / 5
            p.drawLine(int(x), int(plot_t), int(x), int(plot_b))

        af = QFont()
        af.setPointSize(8)
        p.setFont(af)
        p.setPen(QColor(COL_MUTED))
        for gx in range(0, 6):
            frac = gx / 5
            tick_wall = start_t + self.window * frac
            label = time.strftime("%H:%M:%S", time.localtime(tick_wall))
            x = pad_l + plot_w * frac
            align = Qt.AlignLeft if gx == 0 else Qt.AlignRight if gx == 5 else Qt.AlignCenter
            p.drawText(QRectF(x - 44, plot_b + 9, 88, 16), align | Qt.AlignVCenter, label)

        # Media lane.
        lane_mid = pad_t + media_h * 0.5
        p.setPen(QPen(QColor(COL_GRID), 1))
        p.drawLine(int(pad_l), int(lane_mid), int(plot_r), int(lane_mid))
        mf = QFont()
        mf.setPointSize(8 if not self.expanded else 9)
        mf.setBold(True)
        p.setFont(mf)
        for media_rect, event in self._media_rects(start_t, end_t, now, pad_l, plot_r, pad_t, media_h):
            x1 = media_rect.left()
            x2 = media_rect.right()
            y = media_rect.top()
            rect_h = media_rect.height()
            state = str(event.get("state") or "loading")
            color = self._media_color(event)
            fill = QColor(color)
            fill.setAlpha(95 if state != "ended" else 55)
            p.setBrush(fill)
            p.setPen(QPen(color, 1))
            p.drawRoundedRect(media_rect, 7, 7)
            if state in ("playing", "ready", "resolved"):
                shine = QColor("#ffffff")
                shine.setAlpha(38)
                p.setPen(QPen(shine, 1))
                p.drawLine(int(x1 + 8), int(y + 2), int(max(x1 + 8, x2 - 8)), int(y + 2))
            if self._is_selected_media(event):
                outline = QColor("#ffffff")
                outline.setAlpha(220)
                p.setBrush(Qt.NoBrush)
                p.setPen(QPen(outline, 2))
                p.drawRoundedRect(media_rect.adjusted(-1.5, -1.5, 1.5, 1.5), 8, 8)
            p.setBrush(QColor(COL_PANEL if state == "ended" else "#ffffff"))
            p.setPen(Qt.NoPen)
            p.drawEllipse(QPointF(x1 + 9, y + rect_h * 0.5), 3.0, 3.0)

            marker = QColor(color)
            marker.setAlpha(90)
            p.setPen(QPen(marker, 1))
            p.drawLine(int(x1), int(plot_t), int(x1), int(plot_b))

            label = str(event.get("title") or guess_media_title(event.get("url", "")))
            if self.expanded or (x2 - x1) > 86:
                p.setPen(QColor("#eaf8ff"))
                available = max(8, int(x2 - x1 - 30))
                source = self._media_source_label(event)
                prefix = "■ " if state != "ended" else "□ "
                text = p.fontMetrics().elidedText(f"{prefix}{source} · {label}", Qt.ElideRight, available)
                p.drawText(QRectF(x1 + 18, y, available, rect_h), Qt.AlignVCenter | Qt.AlignLeft, text)

        if len(data) < 2:
            p.setPen(QColor(COL_MUTED))
            f = QFont()
            f.setPointSize(10)
            p.setFont(f)
            label = "ожидание сигнала…" if not self.samples else "нет данных в этом участке"
            p.drawText(QRectF(pad_l, plot_t, plot_w, plot_h), Qt.AlignCenter, label)
            p.end()
            return

        vmin = min(v for _, v in data)
        vmax = max(v for _, v in data)
        lo = max(0, vmin - 6)
        hi = vmax + 6
        if hi - lo < 10:
            extra = (10 - (hi - lo)) / 2
            lo = max(0, lo - extra)
            hi += extra

        def py(v):
            return plot_t + plot_h * (1 - (v - lo) / (hi - lo))

        p.save()
        p.setClipRect(QRectF(pad_l, plot_t, plot_w, plot_h))
        for index, (_threshold, zname, zcolor) in enumerate(ZONES[1:]):
            band_lo = max(lo, ZONES[index + 1][0])
            band_hi = hi if index + 2 >= len(ZONES) else min(hi, ZONES[index + 2][0])
            if band_hi <= lo or band_lo >= hi or band_hi <= band_lo:
                continue
            y_top = py(band_hi)
            y_bottom = py(band_lo)
            band = QColor(zcolor)
            band.setAlpha(13 if not self.expanded else 18)
            p.fillRect(QRectF(pad_l, y_top, plot_w, max(1.0, y_bottom - y_top)), band)
            if self.expanded and y_bottom - y_top > 24:
                p.setPen(QColor(zcolor))
                p.setFont(af)
                p.drawText(QRectF(pad_l + 8, y_top + 3, 120, 16), Qt.AlignLeft | Qt.AlignVCenter, zname)
        p.setPen(QPen(QColor(COL_GRID), 1))
        for gy in range(1, 5):
            y = plot_t + plot_h * gy / 5
            p.drawLine(int(pad_l), int(y), int(plot_r), int(y))
        for gx in range(0, 6):
            x = pad_l + plot_w * gx / 5
            p.drawLine(int(x), int(plot_t), int(x), int(plot_b))
        p.restore()

        p.setPen(QColor(COL_MUTED))
        p.setFont(af)
        p.drawText(QRectF(0, py(hi) - 8, pad_l - 5, 16), Qt.AlignRight | Qt.AlignVCenter, str(int(hi)))
        p.drawText(QRectF(0, py(lo) - 8, pad_l - 5, 16), Qt.AlignRight | Qt.AlignVCenter, str(int(lo)))

        pts = [QPointF(px(t), py(v)) for t, v in data]
        line = QPainterPath()
        line.moveTo(pts[0])
        for pt in pts[1:]:
            line.lineTo(pt)

        p.save()
        p.setClipRect(QRectF(pad_l, plot_t, plot_w, plot_h))
        p.setBrush(Qt.NoBrush)
        for index, ((left, right), ((_lt, left_bpm), (_rt, right_bpm))) in enumerate(zip(zip(pts, pts[1:]), zip(data, data[1:]))):
            if right.x() < left.x():
                continue
            _zone_name, zone_color = hr_zone((left_bpm + right_bpm) / 2)
            area = QColor(zone_color)
            area.setAlpha(24 if self.expanded else 18)
            p.setPen(QPen(area, 1))
            x1 = max(int(math.floor(left.x())), int(math.floor(pad_l)))
            x2 = min(int(math.ceil(right.x())), int(math.ceil(plot_r)))
            if index:
                x1 += 1
            width = max(1.0, right.x() - left.x())
            for x in range(x1, x2 + 1):
                frac = max(0.0, min(1.0, (x - left.x()) / width))
                y = left.y() + (right.y() - left.y()) * frac
                p.drawLine(x, int(round(y)), x, int(plot_b))

        glow = QColor(COL_ACCENT)
        glow.setAlpha(60)
        p.setPen(QPen(glow, 6, Qt.SolidLine, Qt.RoundCap, Qt.RoundJoin))
        p.drawPath(line)
        for (left, right), ((_lt, left_bpm), (_rt, right_bpm)) in zip(zip(pts, pts[1:]), zip(data, data[1:])):
            _zone_name, zone_color = hr_zone((left_bpm + right_bpm) / 2)
            p.setPen(QPen(QColor(zone_color), 2.2, Qt.SolidLine, Qt.RoundCap, Qt.RoundJoin))
            p.drawLine(left, right)

        positive = [bpm for _ts, bpm in data if bpm > 0]
        peak_threshold = None
        if positive:
            avg_bpm = sum(positive) / len(positive)
            sorted_bpm = sorted(positive)
            p80 = sorted_bpm[int((len(sorted_bpm) - 1) * 0.8)]
            peak_threshold = max(avg_bpm + 8, p80)
        peaks = []
        if peak_threshold is not None:
            for i in range(1, len(data) - 1):
                bpm = data[i][1]
                if bpm >= peak_threshold and bpm >= data[i - 1][1] and bpm >= data[i + 1][1]:
                    peaks.append((bpm, pts[i]))
            max_i = max(range(len(data)), key=lambda i: data[i][1])
            max_peak = (data[max_i][1], pts[max_i])
            if all(abs(max_peak[1].x() - pt.x()) > 1 for _bpm, pt in peaks):
                peaks.append(max_peak)
        for bpm, pt in sorted(peaks, key=lambda item: item[0], reverse=True)[:6]:
            _zone_name, zone_color = hr_zone(bpm)
            ring = QColor(zone_color)
            p.setPen(QPen(ring, 1.7))
            dot_fill = QColor(COL_PANEL)
            dot_fill.setAlpha(230)
            p.setBrush(dot_fill)
            p.drawEllipse(pt, 5.0, 5.0)
            p.setBrush(ring)
            p.setPen(Qt.NoPen)
            p.drawEllipse(pt, 2.4, 2.4)
            if self.expanded and bpm == max(positive):
                p.setPen(QColor(COL_TEXT))
                p.setFont(af)
                p.drawText(QRectF(pt.x() + 7, pt.y() - 18, 62, 16), Qt.AlignLeft | Qt.AlignVCenter, f"{bpm} BPM")

        if self._hover_sample:
            hover_ts, hover_bpm, hover_zone = self._hover_sample
            hx = px(hover_ts)
            hy = py(hover_bpm)
            _zn, hover_color = hr_zone(hover_bpm)
            guide = QColor(hover_color)
            guide.setAlpha(140)
            p.setPen(QPen(guide, 1, Qt.DashLine))
            p.drawLine(int(hx), int(plot_t), int(hx), int(plot_b))
            p.setBrush(QColor(COL_PANEL))
            p.setPen(QPen(QColor(hover_color), 2))
            p.drawEllipse(QPointF(hx, hy), 6.0, 6.0)
        p.restore()

        _last_zone, last_color = hr_zone(data[-1][1])
        p.setBrush(QColor(last_color))
        p.setPen(Qt.NoPen)
        p.drawEllipse(pts[-1], 3.8, 3.8)
        p.end()


class DayComparisonWidget(QWidget):
    """Compact day-to-day pulse comparison for the timeline sidebar."""

    def __init__(self, parent=None):
        super().__init__(parent)
        self.rows = []
        self.selected_day = None
        self.setMinimumHeight(142)
        self.setSizePolicy(QSizePolicy.Expanding, QSizePolicy.Fixed)

    def set_rows(self, rows, selected_day=None):
        self.rows = list(rows or [])
        self.selected_day = selected_day
        self.setMinimumHeight(max(142, 8 + len(self.rows) * 22))
        self.update()
        self.updateGeometry()

    @staticmethod
    def _short_day(day):
        text = str(day or "")
        if re.fullmatch(r"\d{4}-\d{2}-\d{2}", text):
            return text[8:10] + "." + text[5:7]
        return text[:5] or "день"

    def paintEvent(self, _event):
        p = QPainter(self)
        p.setRenderHint(QPainter.Antialiasing, True)
        w = self.width()
        h = self.height()
        if not self.rows:
            p.setPen(QColor(COL_MUTED))
            f = QFont()
            f.setPointSize(9)
            p.setFont(f)
            p.drawText(QRectF(0, 0, w, h), Qt.AlignCenter, "нет сохранённых дней")
            p.end()
            return

        positive_peaks = [row["max"] for row in self.rows if row.get("max")]
        bpm_lo = 50
        bpm_hi = max(150, (max(positive_peaks) if positive_peaks else 120) + 8)
        row_h = max(18.0, (h - 8.0) / max(1, len(self.rows)))
        bar_x = 50.0
        value_w = 54.0
        bar_w = max(38.0, w - bar_x - value_w - 8.0)

        label_font = QFont()
        label_font.setPointSize(8)
        label_font.setBold(True)
        small_font = QFont()
        small_font.setPointSize(8)

        for index, row in enumerate(self.rows):
            y = 4.0 + index * row_h
            center_y = y + row_h * 0.5
            day = str(row.get("day") or "")
            if day == self.selected_day:
                selected = QColor(COL_ACCENT)
                selected.setAlpha(24)
                p.setBrush(selected)
                p.setPen(Qt.NoPen)
                p.drawRoundedRect(QRectF(0, y + 1, w, max(14.0, row_h - 2)), 7, 7)

            p.setFont(label_font)
            p.setPen(QColor(COL_TEXT if day == self.selected_day else COL_MUTED))
            p.drawText(QRectF(0, y, 44, row_h), Qt.AlignLeft | Qt.AlignVCenter, self._short_day(day))

            p.setBrush(QColor(COL_BG))
            p.setPen(QPen(QColor(COL_BORDER), 1))
            p.drawRoundedRect(QRectF(bar_x, center_y - 4, bar_w, 8), 4, 4)

            avg = float(row.get("avg") or 0)
            peak = float(row.get("max") or 0)
            avg_frac = max(0.0, min(1.0, (avg - bpm_lo) / max(1.0, bpm_hi - bpm_lo)))
            peak_frac = max(0.0, min(1.0, (peak - bpm_lo) / max(1.0, bpm_hi - bpm_lo)))
            _zone, color = hr_zone(avg)
            fill = QColor(color)
            fill.setAlpha(170)
            p.setBrush(fill)
            p.setPen(Qt.NoPen)
            p.drawRoundedRect(QRectF(bar_x, center_y - 4, max(2.0, bar_w * avg_frac), 8), 4, 4)

            peak_x = bar_x + bar_w * peak_frac
            p.setPen(QPen(QColor(COL_TEXT), 1))
            p.drawLine(int(peak_x), int(center_y - 6), int(peak_x), int(center_y + 6))

            p.setFont(small_font)
            p.setPen(QColor(COL_TEXT))
            if avg:
                value = f"{int(avg)}/{int(peak)}"
            else:
                value = f"{int(row.get('media') or 0)} клип"
            p.drawText(QRectF(bar_x + bar_w + 6, y, value_w, row_h), Qt.AlignLeft | Qt.AlignVCenter, value)
        p.end()


class ZoneBar(QWidget):
    """Horizontal heart-rate zone meter (40–200 bpm) with a live marker."""

    LO, HI = 40, 200

    def __init__(self, parent=None):
        super().__init__(parent)
        self.setMinimumHeight(54)
        self.setSizePolicy(QSizePolicy.Expanding, QSizePolicy.Fixed)
        self._bpm = 0

    def set_bpm(self, value):
        try:
            self._bpm = max(0, int(value))
        except Exception:
            self._bpm = 0
        self.update()

    def paintEvent(self, _event):
        p = QPainter(self)
        p.setRenderHint(QPainter.Antialiasing, True)
        w = self.width()
        bar_h = 14
        bar_y = 10
        rect = QRectF(0, bar_y, w, bar_h)

        grad = QLinearGradient(0, 0, w, 0)
        span = self.HI - self.LO
        for threshold, _name, color in ZONES[1:]:
            pos = max(0.0, min(1.0, (threshold - self.LO) / span))
            grad.setColorAt(pos, QColor(color))
        grad.setColorAt(1.0, QColor(ZONES[-1][2]))
        p.setPen(Qt.NoPen)
        p.setBrush(QBrush(grad))
        p.drawRoundedRect(rect, 7, 7)

        # Marker.
        if self._bpm > 0:
            frac = max(0.0, min(1.0, (self._bpm - self.LO) / span))
            x = frac * w
            name, color = hr_zone(self._bpm)
            p.setBrush(QColor(COL_TEXT))
            tri = QPolygonF([QPointF(x, bar_y + bar_h + 2),
                             QPointF(x - 6, bar_y + bar_h + 12),
                             QPointF(x + 6, bar_y + bar_h + 12)])
            p.drawPolygon(tri)
            p.setPen(QColor(color))
            f = QFont()
            f.setPointSize(10)
            f.setBold(True)
            p.setFont(f)
            label = f"{name.upper()}"
            tw = p.fontMetrics().horizontalAdvance(label)
            tx = max(0, min(w - tw, x - tw / 2))
            p.drawText(QRectF(tx, bar_y + bar_h + 14, tw + 4, 18), Qt.AlignLeft, label)
        else:
            p.setPen(QColor(COL_MUTED))
            f = QFont()
            f.setPointSize(10)
            p.setFont(f)
            p.drawText(QRectF(0, bar_y + bar_h + 14, w, 18), Qt.AlignCenter, "нет сигнала")
        p.end()


class StatTile(QFrame):
    def __init__(self, title, accent=COL_TEXT, parent=None):
        super().__init__(parent)
        self.setObjectName("tile")
        lay = QVBoxLayout(self)
        lay.setContentsMargins(14, 10, 14, 12)
        lay.setSpacing(2)
        t = QLabel(title)
        t.setObjectName("tileTitle")
        self.value = QLabel("–")
        self.value.setObjectName("tileValue")
        self.value.setStyleSheet(f"color: {accent};")
        lay.addWidget(t)
        lay.addWidget(self.value)

    def set_value(self, v):
        self.value.setText(str(v))


def _draw_chevron(path, color, direction):
    """Render a small up/down chevron PNG used for the spin/combo arrows."""
    w, h = 12, 8
    pm = QPixmap(w, h)
    pm.fill(Qt.transparent)
    p = QPainter(pm)
    p.setRenderHint(QPainter.Antialiasing, True)
    pen = QPen(QColor(color), 1.6)
    pen.setCapStyle(Qt.RoundCap)
    pen.setJoinStyle(Qt.RoundJoin)
    p.setPen(pen)
    if direction == "up":
        p.drawLine(QPointF(2.5, 5.5), QPointF(6.0, 2.5))
        p.drawLine(QPointF(6.0, 2.5), QPointF(9.5, 5.5))
    else:
        p.drawLine(QPointF(2.5, 2.5), QPointF(6.0, 5.5))
        p.drawLine(QPointF(6.0, 5.5), QPointF(9.5, 2.5))
    p.end()
    pm.save(path, "PNG")


def make_spin_icons():
    """Generate arrow icons into a temp folder; return {name: forward-slash path}."""
    base = os.path.join(tempfile.gettempdir(), "hrtovrc_studio_assets")
    os.makedirs(base, exist_ok=True)
    icons = {}
    for name, color, direction in (
        ("up", COL_TEXT, "up"),
        ("down", COL_TEXT, "down"),
        ("up_dis", COL_MUTED, "up"),
        ("down_dis", COL_MUTED, "down"),
    ):
        path = os.path.join(base, f"{name}.png")
        try:
            _draw_chevron(path, color, direction)
        except Exception:
            pass
        icons[name] = path.replace("\\", "/")
    return icons


def build_stylesheet(icons):
    return f"""
    * {{ font-family: 'Segoe UI', 'Inter', sans-serif; }}
    QWidget {{ color: {COL_TEXT}; font-size: 14px; }}
    QMainWindow, #root {{ background: {COL_BG}; }}
    QScrollArea#settingsScroll {{ background: {COL_BG}; border: 0; }}
    QWidget#settingsPage, QWidget#settingsBody, QWidget#settingsViewport {{ background: {COL_BG}; }}
    #topbar {{ background: {COL_PANEL}; border-bottom: 1px solid {COL_BORDER}; }}
    #brand {{ font-size: 18px; font-weight: 800; color: {COL_ACCENT}; }}
    #navTab {{ border: 0; border-radius: 10px; padding: 8px 20px; background: transparent;
               color: {COL_MUTED}; font-weight: 800; }}
    #navTab:hover {{ color: {COL_TEXT}; background: {COL_PANEL2}; }}
    #navTab:checked {{ color: #ffffff; background: {COL_ACCENT}; }}
    #pageTitle {{ font-size: 20px; font-weight: 800; }}
    #panel {{ background: {COL_PANEL}; border: 1px solid {COL_BORDER}; border-radius: 16px; }}
    #tile {{ background: {COL_PANEL2}; border: 1px solid {COL_BORDER}; border-radius: 12px; }}
    #tileTitle {{ color: {COL_MUTED}; font-size: 11px; font-weight: 700; letter-spacing: 1px; }}
    #tileValue {{ font-size: 26px; font-weight: 900; }}
    #bpmBig {{ font-size: 76px; font-weight: 900; color: {COL_TEXT}; }}
    #bpmUnit {{ color: {COL_MUTED}; font-size: 14px; font-weight: 700; letter-spacing: 2px; }}
    #zoneChip {{ font-size: 13px; font-weight: 800; padding: 6px 14px; border-radius: 12px;
                 color: #ffffff; background: {COL_MUTED}; }}
    #sectionTitle {{ color: {COL_MUTED}; font-size: 12px; font-weight: 800; letter-spacing: 1px; }}
    #kvKey {{ color: {COL_MUTED}; font-weight: 600; }}
    #kvVal {{ color: {COL_TEXT}; font-weight: 800; }}
    #statusVal {{ color: {COL_GREEN}; font-weight: 700; }}
    #muted {{ color: {COL_MUTED}; }}
    #comment {{ color: {COL_ACCENT}; font-size: 15px; font-weight: 800; }}
    #primaryBtn {{ border: 0; border-radius: 12px; padding: 9px 22px; background: {COL_ACCENT};
                   color: #ffffff; font-size: 15px; font-weight: 800; }}
    #primaryBtn:hover {{ background: #ff5c84; }}
    #primaryBtn:pressed {{ background: #e22c59; }}
    #primaryBtn:disabled {{ background: #4a2b35; color: #9a838b; }}
    #ghostBtn {{ border: 1px solid {COL_BORDER}; border-radius: 11px; padding: 8px 16px;
                 background: {COL_PANEL2}; color: {COL_TEXT}; font-weight: 700; }}
    #ghostBtn:hover {{ background: #20293568; border-color: #36424f; }}
    #ghostBtn:disabled {{ color: #58616d; border-color: #1d2530; }}
    #iconBtn {{ border: 1px solid {COL_BORDER}; border-radius: 11px; padding: 8px 14px;
                background: {COL_PANEL2}; color: {COL_TEXT}; font-weight: 700; }}
    #iconBtn:hover {{ background: #20293568; }}
    #statusPill {{ color: {COL_MUTED}; font-size: 14px; font-weight: 800; padding: 8px 16px;
                   border: 1px solid {COL_BORDER}; border-radius: 11px; background: {COL_PANEL2}; }}
    QLineEdit, QComboBox, QSpinBox, QDoubleSpinBox {{
        min-height: 32px; border: 1px solid {COL_BORDER}; border-radius: 10px;
        padding: 4px 10px; background: {COL_PANEL2}; color: {COL_TEXT};
        selection-background-color: {COL_ACCENT}; }}
    QLineEdit:focus, QComboBox:focus, QSpinBox:focus, QDoubleSpinBox:focus {{ border-color: {COL_ACCENT}; }}
    QComboBox::drop-down {{ subcontrol-origin: padding; subcontrol-position: center right;
        border: 0; width: 24px; }}
    QComboBox::down-arrow {{ image: url("{icons['down']}"); width: 12px; height: 8px; }}
    QComboBox QAbstractItemView {{ background: {COL_PANEL2}; border: 1px solid {COL_BORDER};
        selection-background-color: rgba(255,59,107,0.35); outline: 0; }}
    QSpinBox, QDoubleSpinBox {{ padding-right: 26px; }}
    QSpinBox::up-button, QDoubleSpinBox::up-button {{
        subcontrol-origin: border; subcontrol-position: top right; width: 22px;
        border-left: 1px solid {COL_BORDER}; border-top-right-radius: 10px;
        background: {COL_PANEL2}; }}
    QSpinBox::down-button, QDoubleSpinBox::down-button {{
        subcontrol-origin: border; subcontrol-position: bottom right; width: 22px;
        border-left: 1px solid {COL_BORDER}; border-bottom-right-radius: 10px;
        background: {COL_PANEL2}; }}
    QSpinBox::up-button:hover, QDoubleSpinBox::up-button:hover,
    QSpinBox::down-button:hover, QDoubleSpinBox::down-button:hover {{ background: #233040; }}
    QSpinBox::up-button:pressed, QDoubleSpinBox::up-button:pressed,
    QSpinBox::down-button:pressed, QDoubleSpinBox::down-button:pressed {{ background: {COL_ACCENT}; }}
    QSpinBox::up-arrow, QDoubleSpinBox::up-arrow {{ image: url("{icons['up']}"); width: 12px; height: 8px; }}
    QSpinBox::down-arrow, QDoubleSpinBox::down-arrow {{ image: url("{icons['down']}"); width: 12px; height: 8px; }}
    QSpinBox::up-arrow:disabled, QDoubleSpinBox::up-arrow:disabled {{ image: url("{icons['up_dis']}"); }}
    QSpinBox::down-arrow:disabled, QDoubleSpinBox::down-arrow:disabled {{ image: url("{icons['down_dis']}"); }}
    QCheckBox {{ color: {COL_TEXT}; spacing: 8px; }}
    QCheckBox::indicator {{ width: 18px; height: 18px; border-radius: 5px;
        border: 1px solid {COL_BORDER}; background: {COL_PANEL2}; }}
    QCheckBox::indicator:checked {{ background: {COL_ACCENT}; border-color: {COL_ACCENT}; }}
    QToolTip {{ color: {COL_TEXT}; background: {COL_PANEL2}; border: 1px solid {COL_BORDER};
        border-radius: 8px; padding: 6px; }}
    #clipRow {{ border: 1px solid {COL_BORDER}; border-radius: 10px; background: {COL_BG};
        padding: 7px 10px; color: {COL_TEXT}; }}
    #clipRow a {{ color: {COL_MEDIA}; text-decoration: none; font-weight: 800; }}
    #feed, #logBox {{ border: 1px solid {COL_BORDER}; border-radius: 12px; background: {COL_BG};
        padding: 8px; color: {COL_TEXT}; }}
    #logBox {{ font-family: 'Cascadia Code', Consolas, monospace; font-size: 12px; }}
    QScrollBar:vertical {{ background: transparent; width: 10px; margin: 2px; }}
    QScrollBar::handle:vertical {{ background: {COL_BORDER}; border-radius: 5px; min-height: 30px; }}
    QScrollBar::handle:vertical:hover {{ background: #36424f; }}
    QScrollBar::add-line, QScrollBar::sub-line {{ height: 0; }}
    """


class MainWindow(QMainWindow):
    MAC_REGEX = re.compile(r"([0-9A-Fa-f]{2}[:-]){5}([0-9A-Fa-f]{2})")
    UUID_REGEX = re.compile(r"[0-9a-fA-F]{8}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{12}")

    def __init__(self):
        super().__init__()
        self.setWindowTitle(f"HRtoVRC v{APP_VERSION}")
        self.resize(1120, 760)
        self.setMinimumSize(1000, 680)
        icon_path = self.resource_path(ICON_FILE)
        if os.path.exists(icon_path):
            self.setWindowIcon(QIcon(icon_path))

        self.cfg = load_settings()
        self.cfg.setdefault("app_version", APP_VERSION)
        self.cfg.setdefault("author", APP_AUTHOR)
        self.cfg.setdefault("discord", APP_DISCORD)
        self.cfg.setdefault("send_stats_chat", False)
        self.cfg.setdefault("stats_chat_template", "MIN {min_hr} / MAX {max_hr}")
        self.cfg.setdefault("funny_comments", False)
        self.cfg.setdefault("funny_comment_chance", 8)
        self.cfg.setdefault("funny_comment_cooldown", 90)
        self.cfg.setdefault("osc_value_mode", "int")
        self.cfg.setdefault("osc_float_min", 0.0)
        self.cfg.setdefault("osc_float_max", 100.0)
        self.cfg.setdefault("vrc_timeline_enabled", True)
        self.cfg.setdefault("vrc_log_dir", None)
        self.cfg.setdefault("media_title_lookup", True)
        self.cfg.setdefault("check_updates", True)
        last_device = load_last_device()
        if last_device and not self.cfg.get("last_device"):
            self.cfg["last_device"] = last_device

        self.history_store = HistoryStore(history_db_file())
        self.archive_session_id = self.history_store.start_session(APP_VERSION, self.cfg.get("last_device"))
        self.ui_queue = queue.Queue()
        self.cfg_lock = threading.Lock()
        self.devices_map = {}
        self.running = False
        self.started_at = None
        self.last_hr_at = None
        self.reconnect_count = 0
        self.active_client = None
        self.active_address = None
        self._update_busy = False
        self.ble_future = None
        self.joke_active = False
        self.joke_lock = threading.Lock()
        self.min_hr = None
        self.max_hr = None
        self.hr_sum = 0
        self.hr_count = 0
        self.current_comment = ""
        self.last_funny_comment_at = 0.0
        self.chat_history = []
        self.last_hr_value = None
        self.manual_chat_lock = threading.Lock()
        self.manual_chat_message = ""
        self.manual_chat_expires_at = 0.0
        self.toast_label = None
        self.toast_token = 0
        self.timeline_menu_last_sync = 0.0
        self.day_compare_last_refresh = 0.0
        self.selected_timeline_event = None
        self.vrc_watcher_thread = None
        self.vrc_watcher_stop = None
        self._vrc_current_media = None
        self._vrc_media_seq = 0
        self._vrc_title_cache = {}
        self._vrc_title_inflight = set()
        self._vrc_title_lock = threading.Lock()

        self.async_runner = AsyncRunner()
        self.async_runner.start()

        self._build_ui()
        self._apply_start_state()

        self.timer = QTimer(self)
        self.timer.timeout.connect(self.poll_queue)
        self.timer.start(100)

        self.log("INFO", f"HRtoVRC v{APP_VERSION}")
        self.log("INFO", f"Автор: {APP_AUTHOR}, Discord: {APP_DISCORD}")
        self.log("INFO", f"Настройки: {SETTINGS_FILE}")
        self.log("INFO", f"Сохранённый датчик: {LAST_DEVICE_FILE}")
        self.log("INFO", f"Архив истории: {self.history_store.path}")
        if BLEAK_IMPORT_ERROR is not None:
            self.warn(f"BLE недоступен: {BLEAK_IMPORT_ERROR}")
        self.start_vrc_watcher()

        if self.cfg.get("auto_start") and BLEAK_IMPORT_ERROR is None:
            QTimer.singleShot(500, self.toggle_start)

        if self.cfg.get("check_updates", True):
            QTimer.singleShot(2500, lambda: self.start_update_check(silent=True))

    @staticmethod
    def resource_path(name):
        if getattr(sys, "frozen", False):
            candidates = [
                os.path.join(os.path.dirname(sys.executable), name),
                os.path.join(getattr(sys, "_MEIPASS", ""), name),
            ]
        else:
            candidates = [os.path.join(_THIS_DIR, name), os.path.join(_PARENT_DIR, name)]
        for path in candidates:
            if path and os.path.exists(path):
                return path
        return candidates[0]

    # ------------------------------------------------------------------ UI
    def _build_ui(self):
        root = QWidget()
        root.setObjectName("root")
        v = QVBoxLayout(root)
        v.setContentsMargins(0, 0, 0, 0)
        v.setSpacing(0)
        v.addWidget(self._build_topbar())

        self.stack = QStackedWidget()
        v.addWidget(self.stack, 1)
        self.stack.addWidget(self._build_dashboard_page())
        self.stack.addWidget(self._build_archive_page())
        self.stack.addWidget(self._build_settings_page())

        self.setCentralWidget(root)
        self._refresh_stats()
        self._set_zone_chip(0)
        self._select_page(0)

    def _build_topbar(self):
        bar = QFrame()
        bar.setObjectName("topbar")
        bar.setFixedHeight(62)
        lay = QHBoxLayout(bar)
        lay.setContentsMargins(18, 0, 14, 0)
        lay.setSpacing(10)

        brand = QLabel("♥ HRtoVRC")
        brand.setObjectName("brand")
        lay.addWidget(brand)
        lay.addSpacing(18)

        self.nav_monitor = QPushButton("Монитор")
        self.nav_monitor.setObjectName("navTab")
        self.nav_monitor.setCheckable(True)
        self.nav_monitor.setCursor(Qt.PointingHandCursor)
        self.nav_monitor.clicked.connect(lambda: self._select_page(0))
        self.nav_archive = QPushButton("Таймлайн")
        self.nav_archive.setObjectName("navTab")
        self.nav_archive.setCheckable(True)
        self.nav_archive.setCursor(Qt.PointingHandCursor)
        self.nav_archive.clicked.connect(lambda: self._select_page(1))
        self.nav_settings = QPushButton("Настройки")
        self.nav_settings.setObjectName("navTab")
        self.nav_settings.setCheckable(True)
        self.nav_settings.setCursor(Qt.PointingHandCursor)
        self.nav_settings.clicked.connect(lambda: self._select_page(2))
        lay.addWidget(self.nav_monitor)
        lay.addWidget(self.nav_archive)
        lay.addWidget(self.nav_settings)

        lay.addStretch(1)

        self.status_pill = QLabel("Отключено")
        self.status_pill.setObjectName("statusPill")
        self.status_pill.setAlignment(Qt.AlignCenter)
        self.status_pill.setFixedHeight(40)
        self.status_pill.setMinimumWidth(128)
        lay.addWidget(self.status_pill)

        self.start_btn = QPushButton("▶  START")
        self.start_btn.setObjectName("primaryBtn")
        self.start_btn.setMinimumWidth(140)
        self.start_btn.setMinimumHeight(40)
        self.start_btn.setCursor(Qt.PointingHandCursor)
        self.start_btn.clicked.connect(self.toggle_start)
        lay.addWidget(self.start_btn)
        return bar

    def _select_page(self, index):
        self.stack.setCurrentIndex(index)
        self.nav_monitor.setChecked(index == 0)
        self.nav_archive.setChecked(index == 1)
        self.nav_settings.setChecked(index == 2)
        if index == 1:
            self.refresh_archive_dates()

    def _build_dashboard_page(self):
        page = QWidget()
        body_lay = QVBoxLayout(page)
        body_lay.setContentsMargins(18, 16, 18, 14)
        body_lay.setSpacing(14)

        cols = QHBoxLayout()
        cols.setSpacing(14)
        body_lay.addLayout(cols, 1)
        cols.addWidget(self._build_main_panel(), 3)
        cols.addLayout(self._build_side_column(), 2)

        body_lay.addWidget(self._build_actionbar())

        self.log_box = QTextEdit()
        self.log_box.setReadOnly(True)
        self.log_box.setObjectName("logBox")
        self.log_box.setFixedHeight(150)
        self.log_box.setVisible(False)
        body_lay.addWidget(self.log_box)
        return page

    def _build_archive_page(self):
        page = QWidget()
        page.setObjectName("archivePage")
        body = QVBoxLayout(page)
        body.setContentsMargins(18, 16, 18, 14)
        body.setSpacing(14)

        header = QFrame()
        header.setObjectName("panel")
        hl = QHBoxLayout(header)
        hl.setContentsMargins(16, 10, 16, 10)
        hl.setSpacing(10)
        title = QLabel("ТАЙМЛАЙН")
        title.setObjectName("sectionTitle")
        hl.addWidget(title)
        hl.addStretch(1)
        self.archive_date_combo = NoWheelComboBox()
        self.archive_date_combo.setMinimumWidth(150)
        self.archive_date_combo.currentIndexChanged.connect(self.load_selected_archive_day)
        hl.addWidget(self.archive_date_combo)
        self.timeline_zoom_combo = NoWheelComboBox()
        self.timeline_zoom_combo.setFixedWidth(92)
        for label, seconds in (
            ("1 мин", 60),
            ("2 мин", 120),
            ("5 мин", 300),
            ("15 мин", 900),
            ("30 мин", 1800),
            ("1 ч", 3600),
            ("6 ч", 21600),
            ("День", 86400),
        ):
            self.timeline_zoom_combo.addItem(label, seconds)
        self.timeline_zoom_combo.setCurrentIndex(2)
        self.timeline_zoom_combo.currentIndexChanged.connect(self.timeline_menu_zoom_changed)
        hl.addWidget(self.timeline_zoom_combo)
        self.timeline_back_btn = QPushButton("‹")
        self.timeline_back_btn.setObjectName("iconBtn")
        self.timeline_back_btn.setFixedSize(34, 32)
        self.timeline_back_btn.clicked.connect(self.timeline_menu_back)
        hl.addWidget(self.timeline_back_btn)
        self.timeline_live_btn = QPushButton("Сейчас")
        self.timeline_live_btn.setObjectName("ghostBtn")
        self.timeline_live_btn.setFixedHeight(32)
        self.timeline_live_btn.clicked.connect(self.timeline_menu_live)
        hl.addWidget(self.timeline_live_btn)
        self.timeline_forward_btn = QPushButton("›")
        self.timeline_forward_btn.setObjectName("iconBtn")
        self.timeline_forward_btn.setFixedSize(34, 32)
        self.timeline_forward_btn.clicked.connect(self.timeline_menu_forward)
        hl.addWidget(self.timeline_forward_btn)
        self.archive_refresh_btn = QPushButton("Обновить")
        self.archive_refresh_btn.setObjectName("ghostBtn")
        self.archive_refresh_btn.setFixedHeight(32)
        self.archive_refresh_btn.clicked.connect(self.refresh_archive_dates)
        hl.addWidget(self.archive_refresh_btn)
        body.addWidget(header)

        content = QHBoxLayout()
        content.setSpacing(14)
        body.addLayout(content, 1)

        left = QFrame()
        left.setObjectName("panel")
        ll = QVBoxLayout(left)
        ll.setContentsMargins(18, 16, 18, 18)
        ll.setSpacing(10)
        self.archive_range_label = QLabel("нет данных")
        self.archive_range_label.setObjectName("muted")
        ll.addWidget(self.archive_range_label)
        self.archive_history = HistoryGraph(window=86400)
        self.archive_history.set_expanded(True)
        self.archive_history.on_view_changed = self.update_timeline_menu_controls
        self.archive_history.on_media_selected = self.timeline_media_selected
        ll.addWidget(self.archive_history, 1)
        self.clip_card = QTextBrowser()
        self.clip_card.setObjectName("clipRow")
        self.clip_card.setReadOnly(True)
        self.clip_card.setOpenExternalLinks(True)
        self.clip_card.setFrameShape(QFrame.Shape.NoFrame)
        self.clip_card.setMinimumHeight(72)
        self.clip_card.setMaximumHeight(92)
        self.clip_card.setLineWrapMode(QTextEdit.WidgetWidth)
        self.clip_card.setFocusPolicy(Qt.StrongFocus)
        self.clip_card.document().setDocumentMargin(2)
        self.clip_card.setTextInteractionFlags(
            Qt.TextSelectableByMouse
            | Qt.TextSelectableByKeyboard
            | Qt.LinksAccessibleByMouse
            | Qt.LinksAccessibleByKeyboard
        )
        ll.addWidget(self.clip_card)
        self.update_selected_clip_card(None)
        content.addWidget(left, 4)

        right = QVBoxLayout()
        right.setSpacing(14)
        content.addLayout(right, 2)

        summary = QFrame()
        summary.setObjectName("panel")
        sl = QVBoxLayout(summary)
        sl.setContentsMargins(16, 14, 16, 16)
        sl.setSpacing(8)
        st = QLabel("ИТОГИ")
        st.setObjectName("sectionTitle")
        sl.addWidget(st)
        self.archive_summary = QLabel("нет данных")
        self.archive_summary.setObjectName("kvVal")
        self.archive_summary.setTextFormat(Qt.RichText)
        self.archive_summary.setWordWrap(True)
        self.archive_summary.setMinimumHeight(112)
        sl.addWidget(self.archive_summary)
        right.addWidget(summary)

        compare = QFrame()
        compare.setObjectName("panel")
        cl = QVBoxLayout(compare)
        cl.setContentsMargins(16, 14, 16, 14)
        cl.setSpacing(8)
        ct = QLabel("СРАВНЕНИЕ ДНЕЙ")
        ct.setObjectName("sectionTitle")
        cl.addWidget(ct)
        self.day_compare_summary = QLabel("нет данных")
        self.day_compare_summary.setObjectName("muted")
        self.day_compare_summary.setWordWrap(True)
        cl.addWidget(self.day_compare_summary)
        self.day_compare_scroll = QScrollArea()
        self.day_compare_scroll.setWidgetResizable(True)
        self.day_compare_scroll.setFrameShape(QFrame.Shape.NoFrame)
        self.day_compare_scroll.setHorizontalScrollBarPolicy(Qt.ScrollBarAlwaysOff)
        self.day_compare_scroll.viewport().setAttribute(Qt.WA_StyledBackground, True)
        self.day_compare_widget = DayComparisonWidget()
        self.day_compare_scroll.setWidget(self.day_compare_widget)
        cl.addWidget(self.day_compare_scroll, 1)
        right.addWidget(compare, 1)
        return page

    def _build_settings_page(self):
        page = QWidget()
        page.setObjectName("settingsPage")
        page.setAttribute(Qt.WA_StyledBackground, True)
        outer = QVBoxLayout(page)
        outer.setContentsMargins(18, 16, 18, 14)
        outer.setSpacing(12)

        title = QLabel("Настройки")
        title.setObjectName("pageTitle")
        outer.addWidget(title)

        scroll = QScrollArea()
        scroll.setObjectName("settingsScroll")
        scroll.setWidgetResizable(True)
        scroll.setFrameShape(QFrame.Shape.NoFrame)
        scroll.setHorizontalScrollBarPolicy(Qt.ScrollBarAlwaysOff)
        scroll.viewport().setObjectName("settingsViewport")
        scroll.viewport().setAttribute(Qt.WA_StyledBackground, True)
        outer.addWidget(scroll, 1)
        body = QWidget()
        body.setObjectName("settingsBody")
        body.setAttribute(Qt.WA_StyledBackground, True)
        scroll.setWidget(body)
        lay = QVBoxLayout(body)
        lay.setContentsMargins(2, 2, 10, 2)
        lay.setSpacing(14)

        def label(text):
            o = QLabel(text)
            o.setObjectName("kvKey")
            return o

        def card(name):
            c = QFrame()
            c.setObjectName("panel")
            form = QFormLayout(c)
            form.setContentsMargins(18, 14, 18, 16)
            form.setSpacing(10)
            tl = QLabel(name)
            tl.setObjectName("sectionTitle")
            form.addRow(tl)
            return c, form

        # --- Connection (with device search) ---
        conn = QFrame()
        conn.setObjectName("panel")
        cg = QVBoxLayout(conn)
        cg.setContentsMargins(18, 14, 18, 16)
        cg.setSpacing(10)
        ct = QLabel("ПОДКЛЮЧЕНИЕ")
        ct.setObjectName("sectionTitle")
        cg.addWidget(ct)
        grid = QGridLayout()
        grid.setHorizontalSpacing(10)
        grid.setVerticalSpacing(10)
        self.device_combo = NoWheelComboBox()
        self.device_combo.setMinimumWidth(280)
        self.scan_btn = QPushButton("Сканировать")
        self.scan_btn.setObjectName("ghostBtn")
        self.scan_btn.clicked.connect(self.scan_devices)
        self.manual_addr = QLineEdit(self.cfg.get("last_device") or "")
        self.manual_addr.setPlaceholderText("MAC / UUID, напр. EB:59:B4:B6:A9:56")
        self.save_device_btn = QPushButton("Сохранить датчик")
        self.save_device_btn.setObjectName("ghostBtn")
        self.save_device_btn.clicked.connect(self.save_selected_device)
        self.auto_start = QCheckBox("Автостарт при запуске")
        self.auto_start.setChecked(bool(self.cfg.get("auto_start")))
        self.auto_reconnect = QCheckBox("Автопереподключение")
        self.auto_reconnect.setChecked(bool(self.cfg.get("auto_reconnect")))
        grid.addWidget(label("Найти датчик"), 0, 0)
        grid.addWidget(self.device_combo, 0, 1)
        grid.addWidget(self.scan_btn, 0, 2)
        grid.addWidget(label("Адрес вручную"), 1, 0)
        grid.addWidget(self.manual_addr, 1, 1)
        grid.addWidget(self.save_device_btn, 1, 2)
        grid.addWidget(self.auto_start, 2, 1)
        grid.addWidget(self.auto_reconnect, 3, 1)
        grid.setColumnStretch(1, 1)
        cg.addLayout(grid)
        lay.addWidget(conn)

        # --- Two columns of cards ---
        two = QHBoxLayout()
        two.setSpacing(14)
        left = QVBoxLayout()
        left.setSpacing(14)
        right = QVBoxLayout()
        right.setSpacing(14)
        two.addLayout(left, 1)
        two.addLayout(right, 1)
        lay.addLayout(two)

        osc_card, osc = card("OSC")
        self.ip_edit = QLineEdit(self.cfg["osc_ip"])
        self.port_edit = NoWheelSpinBox()
        self.port_edit.setRange(1, 65535)
        self.port_edit.setValue(int(self.cfg["osc_port"]))
        self.param_edit = QLineEdit(self.cfg["osc_param"])
        self.osc_mode_combo = NoWheelComboBox()
        self.osc_mode_combo.addItem("Int BPM", "int")
        self.osc_mode_combo.addItem("Float 0..1", "float")
        mode_index = self.osc_mode_combo.findData(self.cfg.get("osc_value_mode", "int"))
        self.osc_mode_combo.setCurrentIndex(max(0, mode_index))
        self.osc_mode_combo.currentIndexChanged.connect(self._sync_osc_mode_ui)
        self.osc_float_min = NoWheelDoubleSpinBox()
        self.osc_float_min.setRange(0.0, 300.0)
        self.osc_float_min.setDecimals(1)
        self.osc_float_min.setSingleStep(1.0)
        self.osc_float_min.setValue(float(self.cfg.get("osc_float_min", 0.0)))
        self.osc_float_max = NoWheelDoubleSpinBox()
        self.osc_float_max.setRange(1.0, 300.0)
        self.osc_float_max.setDecimals(1)
        self.osc_float_max.setSingleStep(1.0)
        self.osc_float_max.setValue(float(self.cfg.get("osc_float_max", 100.0)))
        osc.addRow("IP", self.ip_edit)
        osc.addRow("Порт", self.port_edit)
        osc.addRow("Параметр", self.param_edit)
        osc.addRow("Значение", self.osc_mode_combo)
        osc.addRow("Float минимум", self.osc_float_min)
        osc.addRow("Float максимум", self.osc_float_max)
        left.addWidget(osc_card)

        chat_card, chat = card("ЧАТ VRCHAT")
        self.send_chat = QCheckBox("Отправлять пульс в чат")
        self.send_chat.setChecked(bool(self.cfg["send_chat"]))
        self.template_edit = QLineEdit(self.cfg["chat_template"])
        self.throttle = NoWheelDoubleSpinBox()
        self.throttle.setRange(0.0, 3600.0)
        self.throttle.setSingleStep(0.1)
        self.throttle.setValue(float(self.cfg["chat_throttle"]))
        self.only_change = QCheckBox("Только при изменении")
        self.only_change.setChecked(bool(self.cfg["chat_only_on_change"]))
        self.send_stats_chat = QCheckBox("Добавлять MIN/MAX в чат")
        self.send_stats_chat.setChecked(bool(self.cfg.get("send_stats_chat", False)))
        self.stats_template_edit = QLineEdit(self.cfg.get("stats_chat_template", "MIN {min_hr} / MAX {max_hr}"))
        chat.addRow(self.send_chat)
        chat.addRow("Шаблон", self.template_edit)
        chat.addRow("Интервал, сек", self.throttle)
        chat.addRow(self.only_change)
        chat.addRow(self.send_stats_chat)
        chat.addRow("Шаблон MIN/MAX", self.stats_template_edit)
        left.addWidget(chat_card)
        left.addStretch(1)

        fun_card, fun = card("КОММЕНТАРИИ")
        self.funny_comments = QCheckBox("Иногда добавлять короткий комментарий")
        self.funny_comments.setChecked(bool(self.cfg.get("funny_comments", False)))
        self.funny_chance = NoWheelSpinBox()
        self.funny_chance.setRange(1, 50)
        self.funny_chance.setSuffix("%")
        self.funny_chance.setValue(int(self.cfg.get("funny_comment_chance", 8)))
        self.funny_cooldown = NoWheelSpinBox()
        self.funny_cooldown.setRange(15, 600)
        self.funny_cooldown.setSuffix(" сек")
        self.funny_cooldown.setValue(int(self.cfg.get("funny_comment_cooldown", 90)))
        fun.addRow(self.funny_comments)
        fun.addRow("Шанс комментария", self.funny_chance)
        fun.addRow("Пауза между ними", self.funny_cooldown)
        right.addWidget(fun_card)

        rel_card, rel = card("НАДЁЖНОСТЬ")
        self.stale_timeout = NoWheelDoubleSpinBox()
        self.stale_timeout.setRange(5.0, 120.0)
        self.stale_timeout.setSingleStep(1.0)
        self.stale_timeout.setValue(float(self.cfg["stale_timeout"]))
        self.reconnect_delay = NoWheelDoubleSpinBox()
        self.reconnect_delay.setRange(0.5, 60.0)
        self.reconnect_delay.setSingleStep(0.5)
        self.reconnect_delay.setValue(float(self.cfg["reconnect_delay"]))
        rel.addRow("Нет пульса, сек", self.stale_timeout)
        rel.addRow("Пауза переподключения, сек", self.reconnect_delay)
        right.addWidget(rel_card)

        vrc_card, vrc = card("VRCHAT TIMELINE")
        self.vrc_timeline_enabled = QCheckBox("Песни и клипы на таймлайне")
        self.vrc_timeline_enabled.setChecked(bool(self.cfg.get("vrc_timeline_enabled", True)))
        self.vrc_log_dir_edit = QLineEdit(self.cfg.get("vrc_log_dir") or default_vrc_log_dir())
        self.vrc_title_lookup = QCheckBox("Определять названия YouTube")
        self.vrc_title_lookup.setChecked(bool(self.cfg.get("media_title_lookup", True)))
        vrc.addRow(self.vrc_timeline_enabled)
        vrc.addRow("Логи VRChat", self.vrc_log_dir_edit)
        vrc.addRow(self.vrc_title_lookup)
        right.addWidget(vrc_card)

        upd_card, upd = card("ОБНОВЛЕНИЯ")
        self.check_updates = QCheckBox("Проверять обновления при запуске")
        self.check_updates.setChecked(bool(self.cfg.get("check_updates", True)))
        self.update_btn = QPushButton("Проверить обновления")
        self.update_btn.setCursor(Qt.PointingHandCursor)
        self.update_btn.clicked.connect(lambda: self.start_update_check(silent=False))
        upd.addRow(self.check_updates)
        upd.addRow(self.update_btn)
        right.addWidget(upd_card)

        about = QLabel(f"v{APP_VERSION} · Автор {APP_AUTHOR} · Discord {APP_DISCORD}")
        about.setObjectName("muted")
        right.addWidget(about)
        right.addStretch(1)

        save_btn = QPushButton("Сохранить настройки")
        save_btn.setObjectName("primaryBtn")
        save_btn.setMinimumHeight(46)
        save_btn.setCursor(Qt.PointingHandCursor)
        save_btn.clicked.connect(self.save_settings_gui)
        outer.addWidget(save_btn)

        if self.cfg.get("last_device"):
            display = f"saved - {self.cfg['last_device']}"
            self.devices_map[display] = self.cfg["last_device"]
            self.device_combo.addItem(display)
        self._sync_osc_mode_ui()
        return page

    def _build_main_panel(self):
        panel = QFrame()
        panel.setObjectName("panel")
        lay = QVBoxLayout(panel)
        lay.setContentsMargins(20, 18, 20, 18)
        lay.setSpacing(12)

        head = QHBoxLayout()
        num_row = QHBoxLayout()
        num_row.setSpacing(8)
        self.bpm_label = QLabel("––")
        self.bpm_label.setObjectName("bpmBig")
        unit = QLabel("уд/мин")
        unit.setObjectName("bpmUnit")
        unit.setAlignment(Qt.AlignBottom)
        num_row.addWidget(self.bpm_label)
        num_row.addWidget(unit)
        head.addLayout(num_row)
        head.addStretch(1)
        self.zone_chip = QLabel("НЕТ СИГНАЛА")
        self.zone_chip.setObjectName("zoneChip")
        self.zone_chip.setAlignment(Qt.AlignCenter)
        head.addWidget(self.zone_chip, 0, Qt.AlignTop)
        lay.addLayout(head)

        self.ecg = ECGWaveform()
        lay.addWidget(self.ecg)

        self.zonebar = ZoneBar()
        lay.addWidget(self.zonebar)

        hist_head = QHBoxLayout()
        hist_lbl = QLabel("ТАЙМЛАЙН ПУЛЬСА")
        hist_lbl.setObjectName("sectionTitle")
        hist_head.addWidget(hist_lbl)
        hist_head.addStretch(1)
        self.history_range_label = QLabel("сейчас")
        self.history_range_label.setObjectName("muted")
        hist_head.addWidget(self.history_range_label)
        self.history_zoom = NoWheelComboBox()
        self.history_zoom.setFixedWidth(92)
        for label, seconds in (
            ("1 мин", 60),
            ("2 мин", 120),
            ("5 мин", 300),
            ("15 мин", 900),
            ("30 мин", 1800),
            ("1 ч", 3600),
            ("6 ч", 21600),
            ("День", 86400),
        ):
            self.history_zoom.addItem(label, seconds)
        self.history_zoom.setCurrentIndex(2)
        self.history_zoom.currentIndexChanged.connect(self.history_zoom_changed)
        hist_head.addWidget(self.history_zoom)
        self.history_back_btn = QPushButton("‹")
        self.history_back_btn.setObjectName("iconBtn")
        self.history_back_btn.setFixedSize(34, 30)
        self.history_back_btn.setToolTip("Назад по истории")
        self.history_back_btn.clicked.connect(self.history_back)
        hist_head.addWidget(self.history_back_btn)
        self.history_live_btn = QPushButton("Сейчас")
        self.history_live_btn.setObjectName("ghostBtn")
        self.history_live_btn.setFixedHeight(30)
        self.history_live_btn.clicked.connect(self.history_live)
        hist_head.addWidget(self.history_live_btn)
        self.history_forward_btn = QPushButton("›")
        self.history_forward_btn.setObjectName("iconBtn")
        self.history_forward_btn.setFixedSize(34, 30)
        self.history_forward_btn.setToolTip("Вперёд по истории")
        self.history_forward_btn.clicked.connect(self.history_forward)
        hist_head.addWidget(self.history_forward_btn)
        self.history_expand_btn = QPushButton("⤢")
        self.history_expand_btn.setObjectName("iconBtn")
        self.history_expand_btn.setFixedSize(34, 30)
        self.history_expand_btn.setToolTip("Открыть expanded timeline")
        self.history_expand_btn.clicked.connect(self.history_toggle_expand)
        hist_head.addWidget(self.history_expand_btn)
        lay.addLayout(hist_head)
        self.history = HistoryGraph(window=300)
        self.history.on_view_changed = self._update_history_controls
        lay.addWidget(self.history, 1)
        self._update_history_controls()

        self.comment_label = QLabel("")
        self.comment_label.setObjectName("comment")
        self.comment_label.setAlignment(Qt.AlignCenter)
        self.comment_label.setWordWrap(True)
        lay.addWidget(self.comment_label)
        return panel

    def _build_side_column(self):
        col = QVBoxLayout()
        col.setSpacing(14)

        tiles = QHBoxLayout()
        tiles.setSpacing(12)
        self.min_tile = StatTile("МИН", "#4aa3ff")
        self.avg_tile = StatTile("СРЕДНИЙ", COL_TEXT)
        self.max_tile = StatTile("МАКС", "#ff4d4d")
        tiles.addWidget(self.min_tile)
        tiles.addWidget(self.avg_tile)
        tiles.addWidget(self.max_tile)
        col.addLayout(tiles)

        session = QFrame()
        session.setObjectName("panel")
        sg = QGridLayout(session)
        sg.setContentsMargins(18, 16, 18, 16)
        sg.setHorizontalSpacing(12)
        sg.setVerticalSpacing(10)
        sec = QLabel("СЕССИЯ")
        sec.setObjectName("sectionTitle")
        sg.addWidget(sec, 0, 0, 1, 2)
        self.status_label = QLabel("Готов")
        self.status_label.setObjectName("statusVal")
        self.status_label.setWordWrap(True)
        self.uptime_label = QLabel("00:00:00")
        self.uptime_label.setObjectName("kvVal")
        self.reconnects_label = QLabel("0")
        self.reconnects_label.setObjectName("kvVal")
        self.last_update_label = QLabel("нет данных")
        self.last_update_label.setObjectName("kvVal")
        self.media_now_label = QLabel("нет клипа")
        self.media_now_label.setObjectName("kvVal")
        self.media_now_label.setWordWrap(True)

        def kv(row, key, widget):
            k = QLabel(key)
            k.setObjectName("kvKey")
            sg.addWidget(k, row, 0)
            sg.addWidget(widget, row, 1)

        kv(1, "Статус", self.status_label)
        kv(2, "Время работы", self.uptime_label)
        kv(3, "Переподключений", self.reconnects_label)
        kv(4, "Обновлено", self.last_update_label)
        kv(5, "VRChat", self.media_now_label)
        sg.setColumnStretch(1, 1)
        col.addWidget(session)

        feed = QFrame()
        feed.setObjectName("panel")
        fl = QVBoxLayout(feed)
        fl.setContentsMargins(18, 14, 18, 16)
        fl.setSpacing(8)
        fsec = QLabel("ВЫВОДЫ В ЧАТ VRCHAT")
        fsec.setObjectName("sectionTitle")
        fl.addWidget(fsec)
        self.feed_box = QTextEdit()
        self.feed_box.setReadOnly(True)
        self.feed_box.setObjectName("feed")
        self.feed_box.setPlaceholderText("Здесь появятся последние сообщения VRChat")
        fl.addWidget(self.feed_box, 1)
        manual_row = QHBoxLayout()
        manual_row.setSpacing(8)
        self.manual_chat_input = QLineEdit()
        self.manual_chat_input.setPlaceholderText("Сообщение в чат")
        self.manual_chat_input.setFixedHeight(36)
        self.manual_chat_input.returnPressed.connect(self.send_manual_chat_message)
        manual_row.addWidget(self.manual_chat_input, 1)
        self.manual_chat_lifetime = NoWheelComboBox()
        for label, seconds in (("10 сек", 10), ("30 сек", 30), ("1 мин", 60), ("2 мин", 120)):
            self.manual_chat_lifetime.addItem(label, seconds)
        self.manual_chat_lifetime.setFixedWidth(82)
        self.manual_chat_lifetime.setFixedHeight(36)
        manual_row.addWidget(self.manual_chat_lifetime)
        self.manual_chat_btn = QPushButton("↵")
        self.manual_chat_btn.setObjectName("ghostBtn")
        self.manual_chat_btn.setFixedSize(42, 36)
        self.manual_chat_btn.clicked.connect(self.send_manual_chat_message)
        manual_row.addWidget(self.manual_chat_btn)
        fl.addLayout(manual_row)
        col.addWidget(feed, 1)
        return col

    def _build_actionbar(self):
        bar = QFrame()
        bar.setObjectName("panel")
        lay = QHBoxLayout(bar)
        lay.setContentsMargins(16, 10, 16, 10)
        lay.setSpacing(10)

        self.reconnect_btn = QPushButton("Переподключить")
        self.reconnect_btn.setObjectName("ghostBtn")
        self.reconnect_btn.clicked.connect(self.reconnect_now)
        self.zero_btn = QPushButton("Пульс 0 / 10с")
        self.zero_btn.setObjectName("ghostBtn")
        self.zero_btn.clicked.connect(self.joke_zero_pulse)
        lay.addWidget(self.reconnect_btn)
        lay.addWidget(self.zero_btn)
        lay.addStretch(1)

        test_lbl = QLabel("Тест OSC")
        test_lbl.setObjectName("muted")
        lay.addWidget(test_lbl)
        self.test_hr = NoWheelSpinBox()
        self.test_hr.setRange(30, 220)
        self.test_hr.setValue(72)
        self.test_hr.setMinimumWidth(76)
        lay.addWidget(self.test_hr)
        self.test_btn = QPushButton("Отправить")
        self.test_btn.setObjectName("ghostBtn")
        self.test_btn.clicked.connect(self.send_test_pulse)
        lay.addWidget(self.test_btn)

        self.log_btn = QPushButton("Журнал")
        self.log_btn.setObjectName("ghostBtn")
        self.log_btn.setCheckable(True)
        self.log_btn.toggled.connect(self._toggle_log)
        lay.addWidget(self.log_btn)
        return bar

    def _toggle_log(self, shown):
        self.log_box.setVisible(shown)
        self.log_btn.setText("Журнал ▾" if shown else "Журнал")

    def refresh_archive_dates(self):
        if not hasattr(self, "archive_date_combo"):
            return
        current = self.archive_date_combo.currentData() or self.archive_date_combo.currentText()
        days = self.history_store.list_days()
        was_blocked = self.archive_date_combo.blockSignals(True)
        self.archive_date_combo.clear()
        self.archive_date_combo.addItem("Сейчас", "__live__")
        for day in days:
            self.archive_date_combo.addItem(day, day)
        index = self.archive_date_combo.findData(current)
        self.archive_date_combo.setCurrentIndex(index if index >= 0 else 0)
        self.archive_date_combo.blockSignals(was_blocked)
        self.refresh_day_comparison(days, self.archive_date_combo.currentData())
        self.load_selected_archive_day()

    def load_selected_archive_day(self, *_args):
        if not hasattr(self, "archive_history"):
            return
        day = self.archive_date_combo.currentData()
        if day == "__live__":
            self.sync_live_timeline_menu(reset_view=True)
            return
        if not day:
            self.archive_history.samples = deque(maxlen=1)
            self.archive_history.media_events = []
            self.archive_history.set_fixed_end_time(None)
            self.clear_selected_clip_card()
            self.archive_summary.setText("нет сохранённых дней")
            self.archive_range_label.setText("нет данных")
            self.refresh_day_comparison(selected_day=None)
            self.archive_history.update()
            return
        self._set_timeline_zoom_combo(86400)
        self.clear_selected_clip_card()
        samples, media = self.history_store.load_day(day)
        try:
            day_start = time.mktime(time.strptime(day, "%Y-%m-%d"))
        except Exception:
            day_start = min([ts for ts, _bpm in samples] or [time.time()])
        day_end = day_start + 86400.0
        now = time.time()
        view_end = min(day_end, max(now, day_start + 30.0)) if day == HistoryStore.day_key(now) else day_end
        view_window = max(30.0, min(86400.0, view_end - day_start))

        self.archive_history.samples = deque(samples, maxlen=max(1000, len(samples) + 1))
        self.archive_history.media_events = [dict(event) for event in media]
        for event in self.archive_history.media_events:
            if media_title_needs_lookup(event):
                self._queue_media_title_lookup(event)
        self.archive_history.window = view_window
        self.archive_history.view_offset = 0.0
        self.archive_history.set_fixed_end_time(view_end)
        self.archive_history.update()

        self.archive_range_label.setText(
            f"{day} · {time.strftime('%H:%M', time.localtime(day_start))}"
            f" – {time.strftime('%H:%M', time.localtime(view_end))}"
        )
        self.archive_summary.setText(self._archive_summary_text(samples, media))
        self.refresh_day_comparison(selected_day=day)
        self.update_timeline_menu_controls()

    def sync_live_timeline_menu(self, reset_view=False):
        if not hasattr(self, "archive_history"):
            return
        if self.archive_date_combo.currentData() != "__live__":
            return
        self.archive_history.samples = deque(self.history.samples, maxlen=self.history.samples.maxlen)
        self.archive_history.media_events = [dict(event) for event in self.history.media_events]
        self.archive_history.set_fixed_end_time(None)
        if reset_view:
            self.clear_selected_clip_card()
            seconds = self.timeline_zoom_combo.currentData() or self.history.window
            self.archive_history.window = float(seconds)
            self.archive_history.view_offset = 0.0
        self.archive_history.update()
        self.archive_range_label.setText("Сейчас · " + self.archive_history.view_label())
        self.archive_summary.setText(self._live_timeline_summary_text())
        self._refresh_selected_clip_from_timeline()
        self.update_timeline_menu_controls()

    def clear_selected_clip_card(self):
        self.selected_timeline_event = None
        if hasattr(self, "archive_history"):
            self.archive_history.set_selected_media(None)
        self.update_selected_clip_card(None)

    def timeline_media_selected(self, event):
        self.selected_timeline_event = dict(event) if event else None
        if hasattr(self, "archive_history"):
            self.archive_history.set_selected_media(event)
        self.update_selected_clip_card(event)

    def _refresh_selected_clip_from_timeline(self):
        if not hasattr(self, "archive_history") or not self.archive_history.selected_media_identity:
            return
        for event in self.archive_history.media_events:
            if self.archive_history._is_selected_media(event):
                self.selected_timeline_event = dict(event)
                self.update_selected_clip_card(event)
                return

    @staticmethod
    def _state_label(state):
        state = str(state or "").lower()
        return {
            "playing": "играет",
            "ready": "готово",
            "resolved": "найдено",
            "loading": "загрузка",
            "ended": "завершено",
            "error": "ошибка",
        }.get(state, state or "событие")

    def _clip_event_span(self, event):
        start = float(event.get("start") or time.time())
        end = event.get("end")
        if end is not None:
            try:
                return start, max(start, float(end))
            except Exception:
                return start, start
        duration = event.get("duration")
        if duration:
            try:
                return start, max(start, start + float(duration))
            except Exception:
                pass
        if hasattr(self, "archive_history") and self.archive_history.fixed_end_time is not None:
            return start, max(start, self.archive_history.fixed_end_time)
        return start, max(start, time.time())

    def _clip_pulse_stats(self, event):
        if not hasattr(self, "archive_history") or not event:
            return None
        start, end = self._clip_event_span(event)
        values = [bpm for ts, bpm in self.archive_history.samples if start <= ts <= end and bpm > 0]
        if not values:
            return None
        return {
            "min": min(values),
            "avg": round(sum(values) / len(values)),
            "max": max(values),
            "count": len(values),
        }

    def update_selected_clip_card(self, event):
        if not hasattr(self, "clip_card"):
            return
        if not event:
            self.clip_card.setHtml(
                f"<span style='color:{COL_MUTED};font-weight:700;'>Выбранный клип:</span> "
                f"<span style='color:{COL_MUTED};'>кликни по полосе клипа или танца на таймлайне, "
                "чтобы увидеть пульс, длительность и ссылку.</span>"
            )
            return

        title = str(event.get("title") or guess_media_title(event.get("url", "")) or "Без названия")
        title_html = html.escape(title)
        source = html.escape(HistoryGraph._media_source_label(event))
        state = html.escape(self._state_label(event.get("state")))
        start, end = self._clip_event_span(event)
        start_label = time.strftime("%H:%M:%S", time.localtime(start))
        end_label = time.strftime("%H:%M:%S", time.localtime(end))
        duration = max(0.0, end - start)
        pulse = self._clip_pulse_stats(event)
        if pulse:
            pulse_line = (
                f"{pulse['min']} / {pulse['avg']} / {pulse['max']} BPM"
                f" · {pulse['count']} точек"
            )
        else:
            pulse_line = "нет точек пульса в этом отрезке"
        url = str(event.get("url") or "")
        if url:
            href = html.escape(url, quote=True)
            visible_url = html.escape(url[:112] + ("..." if len(url) > 112 else ""))
            url_line = f"<a href=\"{href}\">{visible_url}</a>"
        else:
            url_line = f"<span style='color:{COL_MUTED};'>без URL</span>"
        accent = HistoryGraph._media_color(event).name()
        self.clip_card.setHtml(
            f"<div style='font-size:13px;line-height:1.25;'>"
            f"<span style='color:{COL_MUTED};font-weight:800;'>Выбранный клип:</span> "
            f"<span style='font-weight:900;color:{COL_TEXT};'>{title_html}</span>"
            f"</div>"
            f"<div style='font-size:12px;line-height:1.25;color:{COL_TEXT};'>"
            f"<span style='color:{accent};font-weight:900;'>{source}</span> · {state} · "
            f"{start_label} - {end_label} · {HistoryGraph._duration_label(duration)} · "
            f"<span style='color:{accent};font-weight:900;'>Пульс:</span> {html.escape(pulse_line)}"
            f"</div>"
            f"<div style='font-size:12px;line-height:1.25;color:{COL_MUTED};'>{url_line}</div>"
        )

    def refresh_day_comparison(self, days=None, selected_day=None):
        if not hasattr(self, "day_compare_widget"):
            return
        self.day_compare_last_refresh = time.time()
        if days is None:
            days = self.history_store.list_days()
        rows = []
        for day in list(days or []):
            samples, media = self.history_store.load_day(day)
            positive = [bpm for _ts, bpm in samples if bpm > 0]
            rows.append(
                {
                    "day": day,
                    "min": min(positive) if positive else 0,
                    "avg": round(sum(positive) / len(positive)) if positive else 0,
                    "max": max(positive) if positive else 0,
                    "media": len(media),
                    "samples": len(samples),
                }
            )
        self.day_compare_widget.set_rows(rows, selected_day if selected_day != "__live__" else HistoryStore.day_key(time.time()))
        if not rows:
            self.day_compare_summary.setText("Архив пока пуст")
            return
        best_peak = max(rows, key=lambda row: row.get("max") or 0)
        best_media = max(rows, key=lambda row: row.get("media") or 0)
        if best_peak.get("max"):
            summary = f"Всего {len(rows)} дн. · пик {best_peak['max']} BPM ({best_peak['day']})"
        else:
            summary = f"Всего {len(rows)} дн. · пульса пока нет"
        if best_media.get("media"):
            summary += f"\nБольше всего клипов: {best_media['media']} ({best_media['day']})"
        self.day_compare_summary.setText(summary)

    def refresh_day_comparison_if_visible(self, interval=6.0):
        if not hasattr(self, "day_compare_widget") or not hasattr(self, "stack"):
            return
        if self.stack.currentIndex() != 1:
            return
        now = time.time()
        if now - self.day_compare_last_refresh < interval:
            return
        selected_day = self.archive_date_combo.currentData() if hasattr(self, "archive_date_combo") else None
        self.refresh_day_comparison(selected_day=selected_day)

    def _set_timeline_zoom_combo(self, seconds):
        if not hasattr(self, "timeline_zoom_combo"):
            return
        index = self.timeline_zoom_combo.findData(int(round(seconds)))
        if index >= 0 and self.timeline_zoom_combo.currentIndex() != index:
            was_blocked = self.timeline_zoom_combo.blockSignals(True)
            self.timeline_zoom_combo.setCurrentIndex(index)
            self.timeline_zoom_combo.blockSignals(was_blocked)

    def timeline_menu_zoom_changed(self):
        if not hasattr(self, "archive_history"):
            return
        seconds = self.timeline_zoom_combo.currentData()
        if seconds:
            self.archive_history.set_window(float(seconds))
            self.update_timeline_menu_controls()

    def timeline_menu_back(self):
        self.archive_history.step_view(1)
        self.update_timeline_menu_controls()

    def timeline_menu_forward(self):
        self.archive_history.step_view(-1)
        self.update_timeline_menu_controls()

    def timeline_menu_live(self):
        self.archive_history.set_view_offset(0)
        self.update_timeline_menu_controls()

    def update_timeline_menu_controls(self):
        if not hasattr(self, "archive_history"):
            return
        self._set_timeline_zoom_combo(self.archive_history.window)
        max_offset = self.archive_history.max_offset()
        self.timeline_back_btn.setEnabled(max_offset > self.archive_history.view_offset + 1)
        self.timeline_forward_btn.setEnabled(self.archive_history.view_offset > 1)
        self.timeline_live_btn.setEnabled(self.archive_history.view_offset > 1)
        is_live = self.archive_date_combo.currentData() == "__live__"
        self.timeline_live_btn.setText("Сейчас" if is_live else "К концу")
        if is_live:
            self.archive_range_label.setText("Сейчас · " + self.archive_history.view_label())
        else:
            self.archive_range_label.setText(str(self.archive_date_combo.currentData() or "нет данных") + " · " + self.archive_history.view_label())

    @staticmethod
    def _pulse_triplet_html(min_hr, avg_hr, max_hr):
        def part(value, color):
            return f"<span style='color:{color};font-weight:900;'>{html.escape(str(value))}</span>"

        sep = f"<span style='color:{COL_MUTED};'> / </span>"
        return (
            f"{part(min_hr, COL_MIN)}{sep}"
            f"{part(avg_hr, COL_TEXT)}{sep}"
            f"{part(max_hr, COL_MAX)}"
            f" <span style='color:{COL_MUTED};'>BPM</span>"
        )

    def _live_timeline_summary_text(self):
        avg = round(self.hr_sum / self.hr_count) if self.hr_count else "–"
        current = self.last_hr_value if self.last_hr_value is not None else "–"
        min_hr = self.min_hr if self.min_hr is not None else "–"
        max_hr = self.max_hr if self.max_hr is not None else "–"
        elapsed = self.uptime_label.text() if hasattr(self, "uptime_label") else "00:00:00"
        return (
            f"Сейчас: <b>{html.escape(str(current))}</b> <span style='color:{COL_MUTED};'>BPM</span><br>"
            f"Пульс: {self._pulse_triplet_html(min_hr, avg, max_hr)}<br>"
            f"Точек в сессии: <b>{len(self.history.samples)}</b><br>"
            f"Время работы: <b>{html.escape(str(elapsed))}</b><br>"
            f"Клипов/танцев: <b>{len(self.history.media_events)}</b>"
        )

    def _live_timeline_media_text(self):
        events = list(self.history.media_events)[-24:]
        if not events:
            return "нет событий"
        lines = []
        for event in reversed(events):
            start = float(event.get("start", time.time()))
            title = str(event.get("title") or guess_media_title(event.get("url", "")))
            state = str(event.get("state") or "loading")
            lines.append(f"{time.strftime('%H:%M:%S', time.localtime(start))} · {state}\n{title}")
        return "\n\n".join(lines)

    def _archive_summary_text(self, samples, media):
        positive = [bpm for _ts, bpm in samples if bpm > 0]
        if positive:
            avg = round(sum(positive) / len(positive))
            pulse = f"Пульс: {self._pulse_triplet_html(min(positive), avg, max(positive))}<br>"
            points = f"Точек пульса: <b>{len(samples)}</b><br>"
        else:
            pulse = f"Пульс: {self._pulse_triplet_html('–', '–', '–')}<br>"
            points = "Точек пульса: <b>0</b><br>"
        if samples:
            start = time.strftime("%H:%M:%S", time.localtime(samples[0][0]))
            end = time.strftime("%H:%M:%S", time.localtime(samples[-1][0]))
            span = f"Запись: <b>{html.escape(start)}</b> - <b>{html.escape(end)}</b><br>"
        else:
            span = "Запись: <b>нет</b><br>"
        total_media = len(media)
        played = sum(1 for event in media if str(event.get("state")) in ("playing", "ended", "ready", "resolved"))
        return f"{pulse}{points}{span}Клипов/танцев: <b>{total_media}</b><br>Запускались: <b>{played}</b>"

    def _archive_media_text(self, media):
        if not media:
            return "нет событий"
        lines = []
        for event in media:
            start = float(event.get("start", time.time()))
            end = event.get("end")
            title = str(event.get("title") or guess_media_title(event.get("url", "")))
            state = str(event.get("state") or "loading")
            suffix = ""
            if end:
                suffix = f" – {time.strftime('%H:%M:%S', time.localtime(float(end)))}"
            elif event.get("duration"):
                suffix = f" · {HistoryGraph._duration_label(float(event['duration']))}"
            lines.append(f"{time.strftime('%H:%M:%S', time.localtime(start))}{suffix} · {state}\n{title}")
        return "\n\n".join(lines)

    def _update_history_controls(self):
        if not hasattr(self, "history"):
            return
        max_offset = self.history.max_offset()
        self.history_range_label.setText(self.history.view_label())
        if hasattr(self, "history_zoom"):
            zoom_index = self.history_zoom.findData(int(round(self.history.window)))
            if zoom_index >= 0 and self.history_zoom.currentIndex() != zoom_index:
                was_blocked = self.history_zoom.blockSignals(True)
                self.history_zoom.setCurrentIndex(zoom_index)
                self.history_zoom.blockSignals(was_blocked)
        self.history_back_btn.setEnabled(max_offset > self.history.view_offset + 1)
        self.history_forward_btn.setEnabled(self.history.view_offset > 1)
        self.history_live_btn.setEnabled(self.history.view_offset > 1)

    def history_back(self):
        self.history.step_view(1)
        self._update_history_controls()

    def history_forward(self):
        self.history.step_view(-1)
        self._update_history_controls()

    def history_live(self):
        self.history.set_view_offset(0)
        self._update_history_controls()

    def history_zoom_changed(self):
        if not hasattr(self, "history"):
            return
        seconds = self.history_zoom.currentData()
        if seconds:
            self.history.set_window(float(seconds))
            self._update_history_controls()

    def history_toggle_expand(self):
        self.open_expanded_timeline()

    def open_expanded_timeline(self):
        self._select_page(1)
        if hasattr(self, "archive_date_combo"):
            index = self.archive_date_combo.findData("__live__")
            if index >= 0:
                self.archive_date_combo.setCurrentIndex(index)
            self.sync_live_timeline_menu(reset_view=True)

    def _apply_start_state(self):
        if BleakClient is None or BleakScanner is None:
            self.scan_btn.setEnabled(False)
            self.start_btn.setEnabled(False)
            self.reconnect_btn.setEnabled(False)
            self.status_label.setText("BLE недоступен. Подробности в журнале.")

    def _sync_osc_mode_ui(self):
        if not hasattr(self, "osc_mode_combo"):
            return
        enabled = self.osc_mode_combo.currentData() == "float"
        self.osc_float_min.setEnabled(enabled)
        self.osc_float_max.setEnabled(enabled)

    # ------------------------------------------------------------- helpers
    def _set_conn(self, connected):
        if connected:
            self.status_pill.setText("Включено")
            self.status_pill.setStyleSheet(f"color: {COL_GREEN}; border-color: {COL_GREEN};")
        else:
            self.status_pill.setText("Отключено")
            self.status_pill.setStyleSheet("")

    def _set_zone_chip(self, bpm):
        name, color = hr_zone(bpm)
        self.zone_chip.setText(name.upper())
        self.zone_chip.setStyleSheet(
            f"font-size: 13px; font-weight: 800; padding: 6px 14px; border-radius: 12px;"
            f"color: #ffffff; background: {color};"
        )

    def _refresh_stats(self):
        self.min_tile.set_value(self.min_hr if self.min_hr is not None else "–")
        self.max_tile.set_value(self.max_hr if self.max_hr is not None else "–")
        avg = round(self.hr_sum / self.hr_count) if self.hr_count else "–"
        self.avg_tile.set_value(avg)

    def _on_new_hr(self, val):
        self.last_hr_value = val
        self.history_store.add_hr_sample(self.archive_session_id, val)
        self.ecg.set_bpm(val)
        self.zonebar.set_bpm(val)
        self._set_zone_chip(val)
        self.bpm_label.setText(str(val) if val > 0 else "0")
        self.last_hr_at = time.time()
        self.last_update_label.setText(time.strftime("%H:%M:%S"))
        if val > 0:
            self.min_hr = val if self.min_hr is None else min(self.min_hr, val)
            self.max_hr = val if self.max_hr is None else max(self.max_hr, val)
            self.hr_sum += val
            self.hr_count += 1
            self.history.add_sample(val)
            self._refresh_stats()
            self._update_history_controls()
            self.refresh_day_comparison_if_visible(10.0)

    def _reset_session_stats(self):
        self.min_hr = None
        self.max_hr = None
        self.hr_sum = 0
        self.hr_count = 0
        self.history.reset()
        self._refresh_stats()

    def selected_address(self):
        selected = self.device_combo.currentText().strip()
        if selected in self.devices_map:
            return self.devices_map[selected]
        for value in (selected, self.cfg.get("last_device") or ""):
            if not value:
                continue
            mac = self.MAC_REGEX.search(value)
            if mac:
                return mac.group(0)
            uuid = self.UUID_REGEX.search(value)
            if uuid:
                return uuid.group(0)
            if re.fullmatch(r"[0-9A-Fa-f:.\-]{8,}", value):
                return value
        return None

    def log(self, level, text):
        self.log_box.append(f"[{time.strftime('%H:%M:%S')}] [{level}] {text}")

    def warn(self, text):
        self.status_label.setText(str(text))
        self.log("WARN", str(text))

    def show_toast(self, text, error=False, timeout=2200):
        self.toast_token += 1
        token = self.toast_token
        if self.toast_label is None:
            self.toast_label = QLabel(self)
            self.toast_label.setAlignment(Qt.AlignCenter)
            self.toast_label.setAttribute(Qt.WA_TransparentForMouseEvents, True)
        bg = "#ff4d4d" if error else COL_ACCENT
        self.toast_label.setStyleSheet(
            f"background: {bg}; color: #ffffff; border-radius: 12px;"
            "padding: 10px 18px; font-weight: 800;"
        )
        self.toast_label.setText(text)
        self.toast_label.adjustSize()
        width = min(max(self.toast_label.width(), 260), self.width() - 48)
        self.toast_label.setFixedWidth(width)
        self.toast_label.adjustSize()
        x = max(18, (self.width() - self.toast_label.width()) // 2)
        y = 76
        self.toast_label.move(x, y)
        self.toast_label.raise_()
        self.toast_label.show()

        def hide_if_current():
            if self.toast_label is not None and token == self.toast_token:
                self.toast_label.hide()

        QTimer.singleShot(timeout, hide_if_current)

    def add_chat_history(self, text, sent_at=None, failed=False):
        sent_at = sent_at or time.strftime("%H:%M:%S")
        prefix = "FAILED" if failed else sent_at
        self.chat_history.append(f"[{prefix}] {text}")
        self.chat_history = self.chat_history[-8:]
        self.feed_box.setPlainText("\n".join(self.chat_history))
        self.feed_box.verticalScrollBar().setValue(self.feed_box.verticalScrollBar().maximum())

    def has_active_pulse_for_chat(self):
        if not self.running or self.last_hr_value is None or self.last_hr_at is None:
            return False
        stale_timeout = max(5.0, float(self.cfg.get("stale_timeout", DEFAULTS["stale_timeout"])))
        return (time.time() - self.last_hr_at) <= stale_timeout

    def active_manual_chat_message(self):
        now = time.time()
        with self.manual_chat_lock:
            if self.manual_chat_message and self.manual_chat_expires_at > now:
                return self.manual_chat_message
            if self.manual_chat_message and self.manual_chat_expires_at <= now:
                self.manual_chat_message = ""
                self.manual_chat_expires_at = 0.0
        return ""

    def clear_manual_chat_message(self, expected_message):
        with self.manual_chat_lock:
            if self.manual_chat_message != expected_message or time.time() < self.manual_chat_expires_at:
                return
            self.manual_chat_message = ""
            self.manual_chat_expires_at = 0.0
        try:
            client = SimpleUDPClient(self.cfg["osc_ip"], int(self.cfg["osc_port"]))
            if bool(self.cfg.get("send_chat")) and self.has_active_pulse_for_chat():
                text = self.format_chat_text(self.last_hr_value, min_hr=self.min_hr, max_hr=self.max_hr)
                self.send_chat_text(client, text)
                self.add_chat_history(text, time.strftime("%H:%M:%S"))
            else:
                self.send_chat_text(client, "")
        except Exception as e:
            self.warn(f"Не удалось убрать сообщение из чата: {e}")

    def clear_standalone_chat_message(self):
        try:
            client = SimpleUDPClient(self.cfg["osc_ip"], int(self.cfg["osc_port"]))
            if bool(self.cfg.get("send_chat")) and self.has_active_pulse_for_chat():
                text = self.format_chat_text(self.last_hr_value, min_hr=self.min_hr, max_hr=self.max_hr)
                self.send_chat_text(client, text)
                self.add_chat_history(text, time.strftime("%H:%M:%S"))
            else:
                self.send_chat_text(client, "")
        except Exception as e:
            self.warn(f"Не удалось очистить чат: {e}")

    # ------------------------------------------------------------ settings
    def cfg_snapshot(self):
        """Consistent copy of the live settings, safe from any thread."""
        with self.cfg_lock:
            return dict(self.cfg)

    def cfg_value(self, key, default=None):
        """Read one live setting; safe from the BLE/asyncio worker thread."""
        with self.cfg_lock:
            return self.cfg.get(key, default)

    def snapshot_settings(self):
        """Read every widget on the settings page back into self.cfg."""
        with self.cfg_lock:
            self.cfg["osc_ip"] = self.ip_edit.text().strip() or DEFAULTS["osc_ip"]
            self.cfg["osc_port"] = int(self.port_edit.value())
            self.cfg["osc_param"] = self.param_edit.text().strip() or DEFAULTS["osc_param"]
            self.cfg["osc_value_mode"] = self.osc_mode_combo.currentData() or "int"
            self.cfg["osc_float_min"] = float(self.osc_float_min.value())
            self.cfg["osc_float_max"] = float(self.osc_float_max.value())
            self.cfg["send_chat"] = self.send_chat.isChecked()
            self.cfg["chat_template"] = self.template_edit.text()
            self.cfg["chat_throttle"] = float(self.throttle.value())
            self.cfg["chat_only_on_change"] = self.only_change.isChecked()
            self.cfg["send_stats_chat"] = self.send_stats_chat.isChecked()
            self.cfg["stats_chat_template"] = self.stats_template_edit.text()
            self.cfg["funny_comments"] = self.funny_comments.isChecked()
            self.cfg["funny_comment_chance"] = int(self.funny_chance.value())
            self.cfg["funny_comment_cooldown"] = int(self.funny_cooldown.value())
            self.cfg["auto_start"] = self.auto_start.isChecked()
            self.cfg["auto_reconnect"] = self.auto_reconnect.isChecked()
            self.cfg["stale_timeout"] = float(self.stale_timeout.value())
            self.cfg["reconnect_delay"] = float(self.reconnect_delay.value())
            self.cfg["vrc_timeline_enabled"] = self.vrc_timeline_enabled.isChecked()
            log_dir = self.vrc_log_dir_edit.text().strip()
            self.cfg["vrc_log_dir"] = log_dir or None
            self.cfg["media_title_lookup"] = self.vrc_title_lookup.isChecked()
            self.cfg["check_updates"] = self.check_updates.isChecked()
            manual = self.manual_addr.text().strip()
            if manual:
                self.cfg["last_device"] = manual
            keep = {k: self.cfg.get(k) for k in (
                "send_stats_chat", "stats_chat_template", "funny_comments",
                "funny_comment_chance", "funny_comment_cooldown", "last_device")}
            self.cfg = normalize_settings(self.cfg)
            self.cfg.update(keep)
            self.cfg["app_version"] = APP_VERSION
            self.cfg["author"] = APP_AUTHOR
            self.cfg["discord"] = APP_DISCORD

    def clear_chatbox_now(self):
        """Wipe the VRChat chatbox line (used when chat output is switched off)."""
        try:
            client = SimpleUDPClient(self.cfg["osc_ip"], int(self.cfg["osc_port"]))
            self.send_chat_text(client, "")
        except Exception as e:
            self.warn(f"Не удалось очистить чат: {e}")

    def apply_settings_live(self, before):
        """Push freshly saved settings into the running session, without a restart.

        ``before`` is a :meth:`cfg_snapshot` taken *before* the widgets were read
        back by :meth:`snapshot_settings`. The BLE/OSC worker re-reads ``self.cfg``
        on every use, so most keys apply on their own; only the ones that need an
        extra action are handled here.
        """
        after = self.cfg_snapshot()
        changed = sorted(k for k in set(before) | set(after) if before.get(k) != after.get(k))
        if not changed:
            return ""

        notes = []
        if "osc_ip" in changed or "osc_port" in changed:
            notes.append(f"OSC {after.get('osc_ip')}:{after.get('osc_port')}")
        if "osc_param" in changed:
            notes.append(f"параметр {after.get('osc_param')}")

        if "vrc_timeline_enabled" in changed or "vrc_log_dir" in changed:
            self.start_vrc_watcher()
            notes.append("вотчер VRChat перезапущен")
        elif after.get("vrc_timeline_enabled") and self.vrc_watcher_thread is None:
            self.start_vrc_watcher()

        if before.get("send_chat") and not after.get("send_chat"):
            self.clear_chatbox_now()
            notes.append("чат выключен")

        if self.running:
            # Compare against the address the BLE loop is really on, not against the
            # config value: the device widgets can hold a stale address.
            target = (self.selected_address() or str(after.get("last_device") or "")).strip()
            if target and self._valid_ble_address(target) and target != (self.active_address or ""):
                with self.cfg_lock:
                    self.cfg["last_device"] = target
                self.set_device_widgets(target)
                self.status_label.setText("Переключаюсь на новый датчик...")
                self.log("INFO", f"Датчик изменён на {target} — переподключаюсь.")
                notes.append("смена датчика")
                self.async_runner.submit(self.async_request_disconnect())

        self.log("INFO", "Настройки применены на лету: " + ", ".join(changed))
        return "; ".join(notes)

    def save_settings_gui(self):
        before = self.cfg_snapshot()
        self.snapshot_settings()
        manual = self.manual_addr.text().strip()
        if manual and manual not in self.devices_map.values():
            display = f"saved - {manual}"
            self.devices_map[display] = manual
            self.device_combo.insertItem(0, display)
            self.device_combo.setCurrentIndex(0)
            save_last_device(manual)
        self.apply_settings_live(before)
        ok, err = save_settings(self.cfg)
        if ok:
            self.status_label.setText("Настройки сохранены и применены.")
            self.show_toast("Настройки применены")
        else:
            self.status_label.setText("Настройки применены, но не сохранены.")
            self.log("ERR", f"Не удалось сохранить настройки: {err}")
            self.show_toast("Применены, но не сохранены", error=True)

    def save_selected_device(self):
        address = self.selected_address()
        if not address:
            QMessageBox.information(self, "Информация", "Найдите датчик кнопкой «Сканировать» или введите адрес вручную.")
            return
        self.cfg["last_device"] = address
        ok1, err1 = save_last_device(address)
        ok2, err2 = save_settings(self.cfg)
        if ok1 and ok2:
            QMessageBox.information(self, "Сохранено", f"Сохранён датчик: {address}")
        else:
            QMessageBox.warning(self, "Предупреждение", f"Не удалось сохранить:\n{err1 or err2}")

    def set_device_widgets(self, address):
        """Show `address` in the manual field and the combo (main thread only)."""
        address = str(address or "").strip()
        if not address:
            return
        if self.manual_addr.text().strip() != address:
            self.manual_addr.setText(address)
        if self.selected_address() != address:
            display = f"saved - {address}"
            if display not in self.devices_map:
                self.devices_map[display] = address
                self.device_combo.insertItem(0, display)
            self.device_combo.setCurrentText(display)

    # ------------------------------------------------------------- actions
    def scan_devices(self):
        if BleakScanner is None:
            self.warn("BLE-сканирование недоступно.")
            return
        self.scan_btn.setEnabled(False)
        self.status_label.setText("Сканирование BLE...")
        self.async_runner.submit(self.async_scan_once())

    def toggle_start(self):
        if not self.running:
            if BleakClient is None:
                QMessageBox.warning(self, "BLE недоступен", "Для подключения датчика нужна библиотека bleak.")
                return
            address = self.selected_address()
            if not address:
                self._select_page(2)
                QMessageBox.information(self, "Информация", "Найдите датчик во вкладке «Настройки» (Сканировать) или введите адрес вручную.")
                return
            self.cfg["last_device"] = address
            self.active_address = address
            self.set_device_widgets(address)
            save_last_device(address)
            self.running = True
            self.started_at = time.time()
            self.last_hr_at = None
            self.reconnect_count = 0
            self._reset_session_stats()
            self.current_comment = ""
            self.comment_label.setText("")
            self.last_funny_comment_at = 0.0
            self.reconnects_label.setText("0")
            self.uptime_label.setText("00:00:00")
            self.last_update_label.setText("ожидание…")
            self.start_btn.setText("■  STOP")
            self.status_label.setText("Запускаю подключение...")
            self.log("INFO", f"Старт подключения к {address}")
            self.ble_future = self.async_runner.submit(self.async_connect_and_subscribe(address))
        else:
            self.running = False
            self.start_btn.setText("▶  START")
            self.status_label.setText("Остановлено пользователем.")
            self.log("INFO", "Остановлено пользователем.")
            self.async_runner.submit(self.async_request_disconnect())

    def reconnect_now(self):
        if not self.running:
            self.toggle_start()
            return
        self.status_label.setText("Переподключаюсь...")
        self.log("INFO", "Ручное переподключение.")
        self.async_runner.submit(self.async_request_disconnect())

    def send_test_pulse(self):
        try:
            value = int(self.test_hr.value())
            osc_value = osc_value_for_hr(value, self.cfg)
            SimpleUDPClient(self.cfg["osc_ip"], int(self.cfg["osc_port"])).send_message(self.cfg["osc_param"], osc_value)
            self._on_new_hr(value)
            self.last_update_label.setText(time.strftime("%H:%M:%S") + " (тест)")
            self.status_label.setText(f"Тестовый пульс {value} отправлен.")
            self.log("INFO", f"Тестовый OSC-пульс отправлен: {value} -> {osc_value}")
        except Exception as e:
            self.warn(f"Не удалось отправить тестовый пульс: {e}")

    def send_manual_chat_message(self):
        message = self.manual_chat_input.text().strip()
        if not message:
            self.show_toast("Введите сообщение", error=True, timeout=1600)
            return
        duration = int(self.manual_chat_lifetime.currentData() or 10)
        combine_with_pulse = bool(self.cfg.get("send_chat")) and self.has_active_pulse_for_chat()
        with self.manual_chat_lock:
            if combine_with_pulse:
                self.manual_chat_message = message
                self.manual_chat_expires_at = time.time() + duration
            else:
                self.manual_chat_message = ""
                self.manual_chat_expires_at = 0.0
        try:
            client = SimpleUDPClient(self.cfg["osc_ip"], int(self.cfg["osc_port"]))
            if combine_with_pulse:
                outgoing = self.format_chat_text(self.last_hr_value, min_hr=self.min_hr, max_hr=self.max_hr)
            else:
                outgoing = message[:240]
            if self.send_chat_text(client, outgoing):
                self.add_chat_history(outgoing, time.strftime("%H:%M:%S"))
                self.status_label.setText("Сообщение отправлено в чат.")
                self.manual_chat_input.clear()
                self.show_toast("Сообщение отправлено", timeout=1600)
                if combine_with_pulse:
                    QTimer.singleShot(duration * 1000, lambda text=message: self.clear_manual_chat_message(text))
                else:
                    QTimer.singleShot(duration * 1000, self.clear_standalone_chat_message)
            else:
                self.show_toast("Не удалось отправить сообщение", error=True)
        except Exception as e:
            self.warn(f"Не удалось отправить сообщение в чат: {e}")
            self.show_toast("Не удалось отправить сообщение", error=True)

    def joke_zero_pulse(self):
        with self.joke_lock:
            if self.joke_active:
                return
            self.joke_active = True
        self.zero_btn.setEnabled(False)
        self._on_new_hr(0)
        self.status_label.setText("Пульс = 0 на 10 сек...")
        self.async_runner.submit(
            self.async_joke_blast(
                self.cfg["osc_ip"],
                int(self.cfg["osc_port"]),
                self.cfg["osc_param"],
                bool(self.cfg["send_chat"]),
            )
        )

    def maybe_funny_comment(self):
        if not bool(self.cfg.get("funny_comments", False)):
            return ""
        now = time.time()
        cooldown = max(15, int(self.cfg.get("funny_comment_cooldown", 90)))
        if now - self.last_funny_comment_at < cooldown:
            return ""
        chance = max(1, min(50, int(self.cfg.get("funny_comment_chance", 8))))
        if random.randint(1, 100) > chance:
            return ""
        self.last_funny_comment_at = now
        return random.choice(FUNNY_COMMENTS)

    def format_stats_text(self, hr_value, comment="", min_hr=None, max_hr=None):
        if not bool(self.cfg.get("send_stats_chat", False)):
            return ""
        min_value = min_hr if min_hr is not None else self.min_hr
        max_value = max_hr if max_hr is not None else self.max_hr
        if min_value is None and hr_value > 0:
            min_value = hr_value
        if max_value is None and hr_value > 0:
            max_value = hr_value
        template = self.cfg.get("stats_chat_template") or "MIN {min_hr} / MAX {max_hr}"
        try:
            return template.format(
                hr=hr_value,
                min_hr=min_value if min_value is not None else "-",
                max_hr=max_value if max_value is not None else "-",
                comment=comment or "",
            ).strip()
        except Exception:
            return f"MIN {min_value if min_value is not None else '-'} / MAX {max_value if max_value is not None else '-'}"

    def format_chat_text(self, hr_value, comment="", min_hr=None, max_hr=None):
        with self.cfg_lock:
            template = self.cfg.get("chat_template") or ""
        comment = comment or ""
        min_val = min_hr if min_hr is not None else self.min_hr
        max_val = max_hr if max_hr is not None else self.max_hr
        if "{" in template and "}" in template:
            try:
                text = template.format(
                    hr=hr_value,
                    comment=comment,
                    min_hr=min_val if min_val is not None else "-",
                    max_hr=max_val if max_val is not None else "-",
                )
            except Exception:
                text = f"{hr_value}"
        elif template.strip():
            text = template
        else:
            text = str(hr_value)
        if comment and "{comment}" not in template:
            text = f"{text} — {comment}"
        stats_text = self.format_stats_text(hr_value, comment, min_hr, max_hr)
        if stats_text:
            text = f"{text} | {stats_text}"
        manual_text = self.active_manual_chat_message()
        if manual_text:
            text = f"{text}\n{manual_text}"
        return text[:240]

    def send_chat_text(self, client, text):
        try:
            client.send_message("/chatbox/input", [text, True])
            return True
        except Exception:
            try:
                client.send_message("/chatbox/input", text)
                time.sleep(0.05)
                client.send_message("/chatbox/submit", 1)
                return True
            except Exception as e:
                self.ui_queue.put(("log", ("ERR", f"Ошибка отправки чата: {e}")))
                return False

    # ------------------------------------------------------------- updates
    def start_update_check(self, silent=True):
        """Ask GitHub for a newer release, off the UI thread."""
        if self._update_busy:
            return
        if not hr_updater.repo_is_configured(GITHUB_REPO):
            if not silent:
                QMessageBox.information(
                    self, "Обновления",
                    "Репозиторий обновлений ещё не задан.\n\n"
                    "Впишите свой «owner/name» в GITHUB_REPO в начале HRtoVRC_Studio.py.")
            return
        self._update_busy = True
        if not silent:
            self.status_label.setText("Проверяю обновления...")
        threading.Thread(target=self._update_check_worker, args=(silent,), daemon=True).start()

    def _update_check_worker(self, silent):
        try:
            info = hr_updater.check_for_update(GITHUB_REPO, APP_VERSION)
        except Exception as e:
            self.ui_queue.put(("update_error", (silent, str(e))))
            return
        self.ui_queue.put(("update_found", (silent, info)))

    def _on_update_found(self, silent, info):
        self._update_busy = False
        if info is None:
            self.log("INFO", f"Обновлений нет, установлена последняя версия {APP_VERSION}.")
            if not silent:
                self.status_label.setText(f"У вас последняя версия ({APP_VERSION}).")
                self.show_toast(f"Последняя версия ({APP_VERSION})")
            return

        self.log("INFO", f"Доступна новая версия: {info.tag}")
        self.status_label.setText(f"Доступна новая версия {info.tag}.")
        notes = info.notes.strip()
        if len(notes) > 1200:
            notes = notes[:1200] + "\n…"

        box = QMessageBox(self)
        box.setWindowTitle("Доступно обновление")
        box.setIcon(QMessageBox.Question)
        box.setText(f"Установлена v{APP_VERSION}, доступна {info.tag}.\nОбновить сейчас?")
        if notes:
            box.setDetailedText(notes)
        yes_btn = box.addButton("Обновить", QMessageBox.AcceptRole)
        box.addButton("Позже", QMessageBox.RejectRole)
        box.exec()
        if box.clickedButton() is yes_btn:
            self._download_and_apply(info)

    def _on_update_error(self, silent, err):
        self._update_busy = False
        self.log("WARN", f"Проверка обновлений не удалась: {err}")
        if not silent:
            self.status_label.setText("Не удалось проверить обновления.")
            QMessageBox.warning(self, "Обновления", f"Не удалось проверить обновления:\n{err}")

    def _open_release_page(self, info, reason):
        QMessageBox.information(self, "Обновление", reason)
        if info.html_url:
            QDesktopServices.openUrl(QUrl(info.html_url))

    def _download_and_apply(self, info):
        """Download the release .exe with a progress dialog, then swap it in."""
        if not hr_updater.running_frozen():
            self._open_release_page(
                info,
                "Программа запущена из исходников, подменять нечего.\n"
                "Обновите репозиторий через git — страница релиза откроется в браузере.")
            return
        if not info.has_asset:
            self._open_release_page(
                info, "В релизе нет .exe — страница релиза откроется в браузере.")
            return

        state = {"done": 0, "total": int(info.asset_size or 0), "path": None,
                 "error": None, "finished": False, "cancel": False}

        dlg = QProgressDialog("Скачиваю обновление…", "Отмена", 0, 100, self)
        dlg.setWindowTitle(f"Обновление до {info.tag}")
        dlg.setWindowModality(Qt.ApplicationModal)
        dlg.setAutoClose(False)
        dlg.setAutoReset(False)
        dlg.setMinimumDuration(0)

        def worker():
            try:
                state["path"] = hr_updater.download_asset(
                    info,
                    progress_cb=lambda done, total: state.update(done=done, total=total),
                    is_cancelled=lambda: state["cancel"],
                )
            except Exception as e:
                state["error"] = str(e)
            finally:
                state["finished"] = True

        threading.Thread(target=worker, daemon=True).start()

        timer = QTimer(self)

        def tick():
            done, total = state["done"], state["total"]
            mb = 1024.0 * 1024.0
            if total:
                dlg.setValue(min(100, int(done * 100 / total)))
                dlg.setLabelText("Скачиваю обновление… %.1f / %.1f МБ" % (done / mb, total / mb))
            else:
                dlg.setLabelText("Скачиваю обновление… %.1f МБ" % (done / mb,))
            if dlg.wasCanceled():
                state["cancel"] = True
            if not state["finished"]:
                return

            timer.stop()
            dlg.close()
            if state["error"]:
                self.log("ERR", f"Не удалось скачать обновление: {state['error']}")
                QMessageBox.warning(self, "Обновление",
                                    f"Не удалось скачать обновление:\n{state['error']}")
                return
            if not state["path"]:
                self.log("INFO", "Обновление отменено.")
                self.status_label.setText("Обновление отменено.")
                return
            self._install_downloaded(info, state["path"])

        timer.timeout.connect(tick)
        timer.start(150)
        dlg.show()

    def _install_downloaded(self, info, path):
        try:
            hr_updater.apply_update_and_restart(path)
        except Exception as e:
            self.log("ERR", f"Не удалось запустить установку: {e}")
            QMessageBox.warning(self, "Обновление", f"Не удалось запустить установку:\n{e}")
            return
        self.log("INFO", f"Устанавливаю {info.tag} — программа перезапустится.")
        self.running = False
        QTimer.singleShot(200, self.close)

    # --------------------------------------------------------- VRChat logs
    def start_vrc_watcher(self):
        self.stop_vrc_watcher()
        if not bool(self.cfg.get("vrc_timeline_enabled", True)):
            if hasattr(self, "media_now_label"):
                self.media_now_label.setText("выключено")
            return
        stop_event = threading.Event()
        self.vrc_watcher_stop = stop_event
        self._vrc_current_media = None
        thread = threading.Thread(target=self._vrc_log_watch_loop, args=(stop_event,), daemon=True)
        self.vrc_watcher_thread = thread
        thread.start()

    def stop_vrc_watcher(self):
        stop_event = self.vrc_watcher_stop
        if stop_event is not None:
            stop_event.set()
        thread = self.vrc_watcher_thread
        if thread is not None and thread.is_alive() and threading.current_thread() is not thread:
            thread.join(timeout=1.0)
        self.vrc_watcher_thread = None
        self.vrc_watcher_stop = None

    def _vrc_log_watch_loop(self, stop_event):
        log_dir = self.cfg.get("vrc_log_dir") or default_vrc_log_dir()
        last_path = None
        pos = 0
        last_missing_notice = 0.0
        while not stop_event.is_set():
            latest = find_latest_vrc_log(log_dir)
            if not latest:
                now = time.time()
                if now - last_missing_notice > 10:
                    self.ui_queue.put(("vrc_status", "логи не найдены"))
                    self.ui_queue.put(("log", ("WARN", f"VRChat logs не найдены: {log_dir}")))
                    last_missing_notice = now
                stop_event.wait(2.0)
                continue

            if latest != last_path:
                self._vrc_current_media = None
                lines, pos = read_recent_log_lines(latest)
                last_path = latest
                self.ui_queue.put(("vrc_log", latest))
                for line in lines:
                    if stop_event.is_set():
                        break
                    self._handle_vrc_log_line(line)
                stop_event.wait(0.3)
                continue

            try:
                size = os.path.getsize(latest)
                if size < pos:
                    pos = 0
                if size > pos:
                    with open(latest, "rb") as f:
                        f.seek(pos)
                        raw = f.read()
                        pos = f.tell()
                    for line in raw.decode("utf-8", errors="replace").splitlines():
                        if stop_event.is_set():
                            break
                        self._handle_vrc_log_line(line)
            except Exception as e:
                self.ui_queue.put(("log", ("WARN", f"Не удалось прочитать VRChat log: {e}")))
                stop_event.wait(1.5)
                continue
            stop_event.wait(0.7)

    def _handle_vrc_log_line(self, line):
        if not line:
            return
        lower = line.lower()
        ts = parse_vrc_log_time(line)

        match = re.search(r"\[Video Playback\] URL '([^']+)' resolved to '([^']*)'", line)
        if match:
            event = self._ensure_media_event(ts, match.group(1), "resolved")
            duration = extract_duration_from_url(match.group(2))
            if duration:
                event["duration"] = duration
            event["resolved_url"] = match.group(2)
            self._emit_media_event(event)
            return

        match = re.search(r"\[Video Playback\] Attempting to resolve URL '([^']+)'", line)
        if match:
            self._ensure_media_event(ts, match.group(1), "loading")
            return

        match = re.search(r"\[VideoTXL:[^\]]+\] (?:Load Url|Play video):\s+(\S+)", line)
        if match:
            self._ensure_media_event(ts, match.group(1), "loading")
            return

        match = re.search(r"Video ready, duration:\s*([0-9]+(?:\.[0-9]+)?)", line)
        if match and self._vrc_current_media:
            try:
                self._vrc_current_media["duration"] = float(match.group(1))
            except Exception:
                pass
            if self._vrc_current_media.get("state") != "playing":
                self._vrc_current_media["state"] = "ready"
            self._emit_media_event(self._vrc_current_media)
            return

        match = re.search(r"\[AVProVideo\] Opening\s+(\S+)", line)
        if match:
            opened_url = match.group(1)
            opened_key = media_key_from_url(opened_url)
            current = self._vrc_current_media
            if current and (
                current.get("key") == opened_key
                or current.get("url") == opened_url
                or current.get("resolved_url") == opened_url
            ):
                event = current
                event["last_seen"] = ts
            else:
                event = self._ensure_media_event(ts, opened_url, "playing")
            event["state"] = "playing"
            self._emit_media_event(event)
            return

        if "video start event" in lower or "has started playing" in lower or re.search(r"\[VideoTXL:[^\]]+\] Play\s*$", line):
            if self._vrc_current_media:
                self._vrc_current_media["state"] = "playing"
                self._emit_media_event(self._vrc_current_media)
            return

        if (
            "video end event" in lower
            or "event of video end" in lower
            or "the video has reached the end" in lower
            or "player stop" in lower
            or "event of player stop" in lower
            or "trigger a stop event" in lower
            or "stop video" in lower
            or "[avprovideo] shutdown" in lower
            or "[avprovideo] closing" in lower
        ):
            self._finish_current_media(ts, "ended")
            return

        if "[video playback] error:" in lower or "video error" in lower or "ratelimited" in lower:
            if self._vrc_current_media and self._vrc_current_media.get("state") != "playing":
                self._finish_current_media(ts, "error")

    def _ensure_media_event(self, ts, url, state):
        key = media_key_from_url(url)
        current = self._vrc_current_media
        if current and current.get("key") == key and current.get("end") is None:
            if current.get("state") != "playing" or state == "playing":
                current["state"] = state
            current["last_seen"] = ts
            cached_title = self._vrc_title_cache.get(key)
            if cached_title:
                current["title"] = cached_title
            else:
                self._queue_media_title_lookup(current)
            self._emit_media_event(current)
            return current
        if current and current.get("end") is None:
            self._finish_current_media(ts, "ended")

        self._vrc_media_seq += 1
        title = self._vrc_title_cache.get(key) or guess_media_title(url)
        event = {
            "id": self._vrc_media_seq,
            "start": ts,
            "end": None,
            "url": url,
            "key": key,
            "title": title,
            "state": state,
            "source": "VRChat",
        }
        self._vrc_current_media = event
        self._emit_media_event(event)

        self._queue_media_title_lookup(event)
        return event

    def _queue_media_title_lookup(self, event):
        if not bool(self.cfg.get("media_title_lookup", True)):
            return
        key = str(event.get("key") or media_key_from_url(event.get("url", "")))
        if not (key.startswith("youtube:") or key.startswith("bdt:")):
            return
        cached = self._vrc_title_cache.get(key)
        if cached:
            self.ui_queue.put(("media_title_resolved", {"key": key, "id": event.get("id"), "title": cached}))
            return
        if not media_title_needs_lookup(event):
            return
        with self._vrc_title_lock:
            if key in self._vrc_title_inflight:
                return
            self._vrc_title_inflight.add(key)
        event_id = event.get("id")
        url = str(event.get("url") or "")
        thread = threading.Thread(
            target=self._media_title_lookup_worker,
            args=(key, url, event_id),
            daemon=True,
        )
        thread.start()

    def _media_title_lookup_worker(self, key, url, event_id):
        try:
            if not url and key.startswith("youtube:"):
                url = "https://www.youtube.com/watch?v=" + key.split(":", 1)[1]
            elif not url and key.startswith("bdt:"):
                url = "https://bdt.ac/dance/" + key.split(":", 1)[1] + ".mp4"
            title = resolve_media_title(url, timeout=5.0)
            if title:
                self.ui_queue.put(("media_title_resolved", {"key": key, "id": event_id, "title": title}))
        finally:
            with self._vrc_title_lock:
                self._vrc_title_inflight.discard(key)

    def _on_media_title_resolved(self, payload):
        key = str(payload.get("key") or "")
        title = str(payload.get("title") or "").strip()
        if not key or not title:
            return
        self._vrc_title_cache[key] = title
        self.history_store.update_media_title(key, title)
        event = None
        current = self._vrc_current_media
        if current and current.get("key") == key:
            current["title"] = title
            event = dict(current)
        elif hasattr(self, "history"):
            for old in reversed(self.history.media_events):
                if old.get("key") == key:
                    old["title"] = title
                    event = dict(old)
                    break
        if hasattr(self, "archive_history"):
            archive_changed = False
            for old in self.archive_history.media_events:
                if old.get("key") == key:
                    old["title"] = title
                    archive_changed = True
            if archive_changed:
                self.archive_history.update()
                self._refresh_selected_clip_from_timeline()
        if event:
            self._on_media_event(event)

    def _finish_current_media(self, ts, state):
        event = self._vrc_current_media
        if not event:
            return
        event["end"] = max(float(event.get("start", ts)), ts)
        event["state"] = state
        self._emit_media_event(event)
        self._vrc_current_media = None

    def _emit_media_event(self, event):
        self.ui_queue.put(("media_event", dict(event)))

    def _on_media_event(self, event):
        self.history_store.upsert_media_event(self.archive_session_id, event)
        if hasattr(self, "history"):
            self.history.add_media_event(event)
            self._update_history_controls()
        self.refresh_day_comparison_if_visible(2.0)
        title = str(event.get("title") or guess_media_title(event.get("url", "")))
        state = str(event.get("state") or "loading")
        if state in ("ended", "error"):
            current = "ошибка видео" if state == "error" else "нет клипа"
            self.media_now_label.setText(current)
            return
        prefix = "▶" if state == "playing" else "…"
        self.media_now_label.setText(f"{prefix} {title[:90]}")

    # --------------------------------------------------------- async core
    async def async_scan_once(self):
        try:
            devices = await BleakScanner.discover(timeout=5.0)
            items = []
            self.devices_map.clear()
            last = load_last_device()
            if last:
                display = f"saved - {last}"
                self.devices_map[display] = last
                items.append(display)
            for device in devices:
                name = getattr(device, "name", None) or "(no name)"
                address = getattr(device, "address", None) or ""
                mark = " (likely HR)" if any(k in name.lower() for k in NAME_KEYWORDS) else ""
                display = f"{name} - {address}{mark}"
                base = display
                index = 2
                while display in self.devices_map:
                    display = f"{base} #{index}"
                    index += 1
                self.devices_map[display] = address
                if address != last:
                    items.append(display)
            self.ui_queue.put(("scan_result", items))
        except Exception as e:
            self.ui_queue.put(("error", f"Ошибка сканирования: {e}"))
        finally:
            self.ui_queue.put(("scan_done", None))

    def _valid_ble_address(self, address):
        address = str(address or "")
        return bool(
            self.MAC_REGEX.search(address)
            or self.UUID_REGEX.search(address)
            or re.fullmatch(r"[0-9A-Fa-f:.\-]{8,}", address)
        )

    async def async_connect_and_subscribe(self, address):
        if not self._valid_ble_address(address):
            self.ui_queue.put(("error", f"Некорректный адрес устройства: {address}"))
            self.ui_queue.put(("stopped", None))
            return

        # Nothing is captured up front any more: every OSC / chat setting is read
        # from self.cfg on each use, so «Сохранить» applies without a restart.
        osc_state = {"ip": None, "port": None, "client": None}
        osc_state_lock = threading.Lock()

        def osc_endpoint():
            """OSC client for the current ip/port, rebuilt when they change."""
            cfg = self.cfg_snapshot()
            ip = cfg.get("osc_ip") or DEFAULTS["osc_ip"]
            try:
                port = int(cfg.get("osc_port") or DEFAULTS["osc_port"])
            except (TypeError, ValueError):
                port = int(DEFAULTS["osc_port"])
            with osc_state_lock:
                if osc_state["client"] is None or (ip, port) != (osc_state["ip"], osc_state["port"]):
                    if osc_state["client"] is not None:
                        self.ui_queue.put(("log", ("INFO", f"OSC переключён на {ip}:{port}")))
                    osc_state.update(ip=ip, port=port, client=SimpleUDPClient(ip, port))
                return osc_state["client"]

        chat_state = {"latest_hr": None, "seq": 0, "min_hr": None, "max_hr": None}
        chat_lock = threading.Lock()
        watchdog = {"last_notify": time.monotonic()}

        def on_hr_notify(_, data):
            watchdog["last_notify"] = time.monotonic()
            hr_value = parse_hr(data)
            if self.joke_active:
                self.ui_queue.put(("hr", 0))
                return
            cfg = self.cfg_snapshot()
            try:
                osc_endpoint().send_message(
                    cfg.get("osc_param") or DEFAULTS["osc_param"],
                    osc_value_for_hr(hr_value, cfg),
                )
            except Exception as e:
                self.ui_queue.put(("log", ("ERR", f"Ошибка OSC: {e}")))
            if cfg.get("send_chat"):
                with chat_lock:
                    chat_state["latest_hr"] = hr_value
                    if hr_value > 0:
                        chat_state["min_hr"] = (
                            hr_value if chat_state["min_hr"] is None else min(chat_state["min_hr"], hr_value)
                        )
                        chat_state["max_hr"] = (
                            hr_value if chat_state["max_hr"] is None else max(chat_state["max_hr"], hr_value)
                        )
                    chat_state["seq"] += 1
            self.ui_queue.put(("hr", hr_value))

        async def chat_sender_loop():
            last_seq = -1
            last_hr = None
            last_sent = 0.0
            while self.running:
                await asyncio.sleep(0.1)
                cfg = self.cfg_snapshot()
                if not cfg.get("send_chat") or self.joke_active:
                    continue
                with chat_lock:
                    seq = chat_state["seq"]
                    hr_value = chat_state["latest_hr"]
                    min_hr = chat_state["min_hr"]
                    max_hr = chat_state["max_hr"]
                if hr_value is None or seq == last_seq:
                    continue
                now = time.time()
                try:
                    chat_throttle = float(cfg.get("chat_throttle", DEFAULTS["chat_throttle"]))
                except (TypeError, ValueError):
                    chat_throttle = float(DEFAULTS["chat_throttle"])
                if now - last_sent < max(0.0, chat_throttle):
                    continue
                if cfg.get("chat_only_on_change") and last_hr is not None and hr_value == last_hr:
                    last_seq = seq
                    continue
                comment = self.maybe_funny_comment()
                text = self.format_chat_text(hr_value, comment, min_hr, max_hr)
                if self.send_chat_text(osc_endpoint(), text):
                    if comment:
                        self.ui_queue.put(("comment", comment))
                    last_sent = now
                    last_hr = hr_value
                    last_seq = seq
                    self.ui_queue.put(("chat_sent", (text, time.strftime("%H:%M:%S"))))
                else:
                    last_seq = seq

        # Always started: the loop re-checks send_chat itself, so chat output can be
        # switched on and off while the sensor stays connected.
        chat_task = asyncio.create_task(chat_sender_loop())
        attempt = 0
        try:
            while self.running:
                try:
                    target = str(self.cfg_value("last_device") or "").strip()
                    if target and target != address:
                        if self._valid_ble_address(target):
                            address = target
                            attempt = 0
                            self.ui_queue.put(("log", ("INFO", f"Новый датчик из настроек: {address}")))
                        else:
                            self.ui_queue.put(("log", ("WARN", f"Некорректный адрес датчика в настройках: {target}")))
                    self.active_address = address
                    self.ui_queue.put(("device", address))
                    attempt += 1
                    if attempt > 1:
                        self.ui_queue.put(("reconnect_count", attempt - 1))
                        self.ui_queue.put(("log", ("INFO", f"Попытка переподключения #{attempt - 1}")))
                    self.ui_queue.put(("status", f"Подключение к {address}..."))

                    async with BleakClient(address, timeout=15.0) as client:
                        self.active_client = client
                        if not client.is_connected:
                            raise RuntimeError("Connection failed")
                        self.ui_queue.put(("connected", True))
                        self.ui_queue.put(("status", "Подключено. Настраиваю подписку..."))

                        services = None
                        get_services = getattr(client, "get_services", None)
                        if callable(get_services):
                            try:
                                services = await client.get_services()
                            except Exception:
                                services = getattr(client, "services", None)
                        else:
                            services = getattr(client, "services", None)
                        if not services:
                            for _ in range(6):
                                await asyncio.sleep(0.5)
                                services = getattr(client, "services", None)
                                if services:
                                    break
                        if not services:
                            raise RuntimeError("Не удалось получить BLE services.")

                        try:
                            char_uuids = [c.uuid.lower() for s in services for c in s.characteristics]
                        except Exception:
                            char_uuids = []
                        if HR_CHAR_UUID not in char_uuids:
                            self.ui_queue.put(("error", "HR-характеристика 0x2A37 не найдена на устройстве."))
                            return

                        subscribed = False
                        for subscribe_try in range(1, 5):
                            try:
                                self.ui_queue.put(("status", f"Подписка на пульс [{subscribe_try}]..."))
                                await client.start_notify(HR_CHAR_UUID, on_hr_notify)
                                subscribed = True
                                self.ui_queue.put(("status", "Пульс слушается."))
                                break
                            except Exception as e:
                                self.ui_queue.put(("log", ("ERR", f"Ошибка подписки [{subscribe_try}]: {e}")))
                                await asyncio.sleep(0.7 + subscribe_try * 0.5)

                        if subscribed:
                            watchdog["last_notify"] = time.monotonic()
                            stale_detected = False
                            while client.is_connected and self.running:
                                await asyncio.sleep(0.5)
                                if self.joke_active:
                                    watchdog["last_notify"] = time.monotonic()
                                    continue
                                stale_timeout = max(5.0, float(self.cfg.get("stale_timeout", DEFAULTS["stale_timeout"])))
                                if time.monotonic() - watchdog["last_notify"] > stale_timeout:
                                    stale_detected = True
                                    self.ui_queue.put(("warning", f"Пульс не обновлялся {stale_timeout:.0f} сек. Переподключаюсь..."))
                                    break
                            try:
                                await asyncio.wait_for(client.stop_notify(HR_CHAR_UUID), timeout=2.0)
                            except Exception:
                                pass
                            if self.running and stale_detected:
                                self.ui_queue.put(("status", "Поток пульса остановился. Переподключаюсь..."))
                            elif self.running:
                                self.ui_queue.put(("status", "Соединение разорвано."))
                    self.active_client = None
                    self.ui_queue.put(("connected", False))
                except Exception as e:
                    self.active_client = None
                    self.ui_queue.put(("connected", False))
                    self.ui_queue.put(("error", f"Ошибка BLE: {e}"))

                if not self.running:
                    break
                auto_reconnect = bool(self.cfg.get("auto_reconnect", True))
                reconnect_delay = max(0.5, float(self.cfg.get("reconnect_delay", DEFAULTS["reconnect_delay"])))
                if not auto_reconnect:
                    self.ui_queue.put(("status", "Остановлено: автопереподключение выключено."))
                    break
                self.ui_queue.put(("status", f"Переподключение через {reconnect_delay:g} сек..."))
                end_time = time.monotonic() + reconnect_delay
                while self.running and time.monotonic() < end_time:
                    await asyncio.sleep(min(0.2, end_time - time.monotonic()))
        finally:
            self.active_client = None
            self.active_address = None
            self.running = False
            self.ui_queue.put(("stopped", None))
            if chat_task is not None:
                chat_task.cancel()
                try:
                    await chat_task
                except asyncio.CancelledError:
                    pass

    async def async_request_disconnect(self):
        client = self.active_client
        if client is not None:
            try:
                if client.is_connected:
                    await asyncio.wait_for(client.disconnect(), timeout=3.0)
            except Exception as e:
                self.ui_queue.put(("log", ("ERR", f"Не удалось отключить BLE: {e}")))
        await asyncio.sleep(0.1)

    async def async_joke_blast(self, osc_ip, osc_port, osc_param, send_chat, duration=10.0, interval=1.0):
        client = SimpleUDPClient(osc_ip, osc_port)
        end_time = time.time() + duration
        try:
            while time.time() < end_time:
                try:
                    client.send_message(osc_param, osc_value_for_hr(0, self.cfg))
                except Exception as e:
                    self.ui_queue.put(("log", ("ERR", f"Ошибка OSC-шутки: {e}")))
                if send_chat:
                    text = self.format_chat_text(0)
                    if self.send_chat_text(client, text):
                        self.ui_queue.put(("chat_sent", (text, time.strftime("%H:%M:%S"))))
                self.ui_queue.put(("hr", 0))
                await asyncio.sleep(interval)
        finally:
            with self.joke_lock:
                self.joke_active = False
            self.ui_queue.put(("joke_done", None))

    # ------------------------------------------------------------- polling
    def poll_queue(self):
        try:
            while True:
                typ, payload = self.ui_queue.get_nowait()
                if typ == "scan_result":
                    self.device_combo.clear()
                    for item in payload:
                        self.device_combo.addItem(item)
                    self.status_label.setText(f"Найдено устройств: {len(payload)}.")
                elif typ == "scan_done":
                    self.scan_btn.setEnabled(BleakScanner is not None)
                elif typ == "hr":
                    self._on_new_hr(int(payload))
                elif typ == "comment":
                    self.current_comment = str(payload)
                    self.comment_label.setText(self.current_comment)
                elif typ == "device":
                    self.set_device_widgets(str(payload))
                elif typ == "update_found":
                    self._on_update_found(payload[0], payload[1])
                elif typ == "update_error":
                    self._on_update_error(payload[0], payload[1])
                elif typ == "connected":
                    self._set_conn(bool(payload))
                elif typ == "status":
                    self.status_label.setText(str(payload))
                elif typ == "warning":
                    self.warn(str(payload))
                elif typ == "error":
                    self.status_label.setText(str(payload))
                    self.log("ERR", str(payload))
                elif typ == "log":
                    level, text = payload
                    self.log(level, text)
                elif typ == "reconnect_count":
                    self.reconnect_count = int(payload)
                    self.reconnects_label.setText(str(payload))
                elif typ == "chat_sent":
                    text, sent_at = payload
                    self.add_chat_history(text, sent_at)
                elif typ == "chat_failed":
                    self.add_chat_history(str(payload), failed=True)
                elif typ == "media_event":
                    self._on_media_event(payload)
                elif typ == "media_title_resolved":
                    self._on_media_title_resolved(payload)
                elif typ == "vrc_status":
                    self.media_now_label.setText(str(payload))
                elif typ == "vrc_log":
                    self.log("INFO", f"VRChat log: {payload}")
                elif typ == "stopped":
                    self.running = False
                    self.started_at = None
                    self.start_btn.setText("▶  START")
                    self._set_conn(False)
                    self.bpm_label.setText("––")
                    self.ecg.set_bpm(0)
                    self.zonebar.set_bpm(0)
                    self._set_zone_chip(0)
                    if not self.status_label.text().startswith("Остановлено"):
                        self.status_label.setText("Остановлено.")
                elif typ == "joke_done":
                    self.zero_btn.setEnabled(True)
                    self.status_label.setText("Готов")
        except queue.Empty:
            pass

        if self.running and self.started_at:
            elapsed = max(0, int(time.time() - self.started_at))
            minutes, seconds = divmod(elapsed, 60)
            hours, minutes = divmod(minutes, 60)
            self.uptime_label.setText(f"{hours:02d}:{minutes:02d}:{seconds:02d}")
        if (
            hasattr(self, "archive_date_combo")
            and self.stack.currentIndex() == 1
            and self.archive_date_combo.currentData() == "__live__"
        ):
            now = time.time()
            if now - self.timeline_menu_last_sync > 0.5:
                self.timeline_menu_last_sync = now
                self.sync_live_timeline_menu(reset_view=False)

    def closeEvent(self, event):
        self.running = False
        self.stop_vrc_watcher()
        self.history_store.end_session(self.archive_session_id)
        try:
            if self.ble_future:
                self.async_runner.submit(self.async_request_disconnect()).result(timeout=2.0)
        except Exception:
            pass
        try:
            self.async_runner.stop()
        except Exception:
            pass
        self.history_store.close()
        event.accept()


def main():
    app = QApplication(sys.argv)
    app.setStyle("Fusion")
    icons = make_spin_icons()
    app.setStyleSheet(build_stylesheet(icons))
    window = MainWindow()
    window.show()
    sys.exit(app.exec())


if __name__ == "__main__":
    main()
