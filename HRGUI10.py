import asyncio
import threading
import time
import json
import os
import sys
import queue
import re
import socket
import struct

try:
    import tkinter as tk
    from tkinter import ttk, messagebox
    TK_IMPORT_ERROR = None
except Exception as e:
    tk = None
    ttk = None
    messagebox = None
    TK_IMPORT_ERROR = e

try:
    from bleak import BleakScanner, BleakClient
    BLEAK_IMPORT_ERROR = None
except Exception as e:
    BleakScanner = None
    BleakClient = None
    BLEAK_IMPORT_ERROR = e

try:
    from pythonosc.udp_client import SimpleUDPClient
    PYTHONOSC_IMPORT_ERROR = None
except Exception as e:
    PYTHONOSC_IMPORT_ERROR = e

    class SimpleUDPClient:
        def __init__(self, address, port):
            self.address = address
            self.port = int(port)
            self.socket = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)

        @staticmethod
        def _pad(data):
            return data + (b"\0" * ((4 - (len(data) % 4)) % 4))

        @classmethod
        def _string(cls, value):
            return cls._pad(str(value).encode("utf-8") + b"\0")

        @classmethod
        def _arg(cls, value):
            if isinstance(value, bool):
                return ("T" if value else "F"), b""
            if isinstance(value, int):
                return "i", struct.pack(">i", int(value))
            if isinstance(value, float):
                return "f", struct.pack(">f", float(value))
            return "s", cls._string(value)

        def send_message(self, address, value):
            values = list(value) if isinstance(value, (list, tuple)) else [value]
            tags = []
            payload = b""
            for item in values:
                tag, data = self._arg(item)
                tags.append(tag)
                payload += data

            packet = self._string(address) + self._string("," + "".join(tags)) + payload
            self.socket.sendto(packet, (self.address, self.port))

# Определяем абсолютный путь к директории запуска
if getattr(sys, 'frozen', False):
    # Если скрипт скомпилирован (например, через PyInstaller)
    BASE_DIR = os.path.dirname(sys.executable)
else:
    # Если это обычный .py скрипт
    BASE_DIR = os.path.dirname(os.path.abspath(__file__))

APP_NAME = "HRtoVRC"
STARTUP_INFOS = []
STARTUP_WARNINGS = []

if BLEAK_IMPORT_ERROR is not None:
    STARTUP_WARNINGS.append(
        "Библиотека bleak не найдена. BLE-подключение к датчику недоступно; установите bleak или используйте собранную версию с зависимостями."
    )

if PYTHONOSC_IMPORT_ERROR is not None:
    STARTUP_WARNINGS.append(
        "Библиотека python-osc не найдена. Используется встроенный OSC-отправитель; базовая отправка пульса и чата сохранена."
    )


def _user_config_dir():
    if os.name == "nt":
        root = os.environ.get("APPDATA") or os.path.expanduser("~")
    else:
        root = os.environ.get("XDG_CONFIG_HOME") or os.path.join(os.path.expanduser("~"), ".config")
    return os.path.join(root, APP_NAME)


def _is_writable_dir(path):
    try:
        os.makedirs(path, exist_ok=True)
        test_path = os.path.join(path, ".hrgui_write_test")
        with open(test_path, "w", encoding="utf-8") as f:
            f.write("ok")
        os.remove(test_path)
        return True
    except Exception:
        return False


def _unique_paths(paths):
    result = []
    seen = set()
    for path in paths:
        norm = os.path.normcase(os.path.abspath(path))
        if norm not in seen:
            result.append(path)
            seen.add(norm)
    return result


USER_CONFIG_DIR = _user_config_dir()
BASE_SETTINGS_FILE = os.path.join(BASE_DIR, "settings.json")
USER_SETTINGS_FILE = os.path.join(USER_CONFIG_DIR, "settings.json")
BASE_LAST_DEVICE_FILE = os.path.join(BASE_DIR, "last_device.txt")
USER_LAST_DEVICE_FILE = os.path.join(USER_CONFIG_DIR, "last_device.txt")

if _is_writable_dir(BASE_DIR):
    SETTINGS_FILE = BASE_SETTINGS_FILE
    LAST_DEVICE_FILE = BASE_LAST_DEVICE_FILE
else:
    SETTINGS_FILE = USER_SETTINGS_FILE
    LAST_DEVICE_FILE = USER_LAST_DEVICE_FILE
    STARTUP_WARNINGS.append(
        f"Папка программы недоступна для записи. Настройки будут сохраняться здесь: {USER_CONFIG_DIR}"
    )
    if not _is_writable_dir(USER_CONFIG_DIR):
        STARTUP_WARNINGS.append(
            f"Папка настроек тоже недоступна для записи: {USER_CONFIG_DIR}. Настройки будут работать только до закрытия программы."
        )

SETTINGS_READ_FILES = _unique_paths([SETTINGS_FILE, BASE_SETTINGS_FILE, USER_SETTINGS_FILE])
LAST_DEVICE_READ_FILES = _unique_paths([LAST_DEVICE_FILE, BASE_LAST_DEVICE_FILE, USER_LAST_DEVICE_FILE])

DEFAULTS = {
    "osc_ip": "127.0.0.1",
    "osc_port": 9000,
    "osc_param": "/avatar/parameters/HR",
    "osc_value_mode": "int",
    "osc_float_min": 0.0,
    "osc_float_max": 100.0,
    "send_chat": True,
    "chat_template": "Пульс: {hr}",
    "chat_throttle": 1.0,
    "chat_only_on_change": True,
    "last_device": None,
    "auto_start": False,
    "auto_reconnect": True,
    "stale_timeout": 12.0,
    "reconnect_delay": 3.0,
    "hide_console": True,
    "send_stats_chat": False,
    "stats_chat_template": "MIN {min_hr} / MAX {max_hr}",
    "funny_comments": False,
    "funny_comment_chance": 8,
    "funny_comment_cooldown": 90,
    "vrc_timeline_enabled": True,
    "vrc_log_dir": None,
    "media_title_lookup": True,
    "check_updates": True,
    "app_version": None,
    "author": "_howl",
    "discord": "howl64",
}

HR_CHAR_UUID = "00002a37-0000-1000-8000-00805f9b34fb"
NAME_KEYWORDS = ["coospo", "h808", "h808s", "h8", "808s", "808"]


def _to_int(value, default, min_value=None, max_value=None):
    try:
        if isinstance(value, bool):
            raise ValueError
        result = int(value)
    except Exception:
        result = int(default)

    if min_value is not None:
        result = max(min_value, result)
    if max_value is not None:
        result = min(max_value, result)
    return result


def _to_float(value, default, min_value=None, max_value=None):
    try:
        if isinstance(value, bool):
            raise ValueError
        result = float(value)
    except Exception:
        result = float(default)

    if min_value is not None:
        result = max(min_value, result)
    if max_value is not None:
        result = min(max_value, result)
    return result


def _to_bool(value, default=False):
    if isinstance(value, bool):
        return value
    if isinstance(value, str):
        val = value.strip().lower()
        if val in ("1", "true", "yes", "y", "on", "да"):
            return True
        if val in ("0", "false", "no", "n", "off", "нет"):
            return False
    return bool(default)


def normalize_settings(raw, warn=False):
    cfg = DEFAULTS.copy()
    changed = False

    if isinstance(raw, dict):
        cfg.update(raw)
    elif warn:
        changed = True

    before = dict(cfg)
    cfg["osc_ip"] = str(cfg.get("osc_ip") or DEFAULTS["osc_ip"]).strip() or DEFAULTS["osc_ip"]
    cfg["osc_port"] = _to_int(cfg.get("osc_port"), DEFAULTS["osc_port"], 1, 65535)

    osc_param = str(cfg.get("osc_param") or DEFAULTS["osc_param"]).strip()
    if not osc_param:
        osc_param = DEFAULTS["osc_param"]
    if not osc_param.startswith("/"):
        osc_param = "/" + osc_param
    cfg["osc_param"] = osc_param
    osc_value_mode = str(cfg.get("osc_value_mode") or DEFAULTS["osc_value_mode"]).strip().lower()
    if osc_value_mode not in ("int", "float"):
        osc_value_mode = DEFAULTS["osc_value_mode"]
    cfg["osc_value_mode"] = osc_value_mode
    cfg["osc_float_min"] = _to_float(cfg.get("osc_float_min"), DEFAULTS["osc_float_min"], 0.0, 300.0)
    cfg["osc_float_max"] = _to_float(cfg.get("osc_float_max"), DEFAULTS["osc_float_max"], 1.0, 300.0)
    if cfg["osc_float_max"] <= cfg["osc_float_min"]:
        cfg["osc_float_max"] = min(300.0, cfg["osc_float_min"] + 1.0)

    cfg["send_chat"] = _to_bool(cfg.get("send_chat"), DEFAULTS["send_chat"])
    cfg["chat_template"] = str(cfg.get("chat_template") or DEFAULTS["chat_template"])
    cfg["chat_throttle"] = _to_float(cfg.get("chat_throttle"), DEFAULTS["chat_throttle"], 0.0, 3600.0)
    cfg["chat_only_on_change"] = _to_bool(cfg.get("chat_only_on_change"), DEFAULTS["chat_only_on_change"])
    cfg["last_device"] = str(cfg.get("last_device")).strip() if cfg.get("last_device") else None
    cfg["auto_start"] = _to_bool(cfg.get("auto_start"), DEFAULTS["auto_start"])
    cfg["auto_reconnect"] = _to_bool(cfg.get("auto_reconnect"), DEFAULTS["auto_reconnect"])
    cfg["stale_timeout"] = _to_float(cfg.get("stale_timeout"), DEFAULTS["stale_timeout"], 5.0, 120.0)
    cfg["reconnect_delay"] = _to_float(cfg.get("reconnect_delay"), DEFAULTS["reconnect_delay"], 0.5, 60.0)
    cfg["hide_console"] = _to_bool(cfg.get("hide_console"), DEFAULTS["hide_console"])
    cfg["send_stats_chat"] = _to_bool(cfg.get("send_stats_chat"), DEFAULTS["send_stats_chat"])
    cfg["stats_chat_template"] = str(cfg.get("stats_chat_template") or DEFAULTS["stats_chat_template"])
    cfg["funny_comments"] = _to_bool(cfg.get("funny_comments"), DEFAULTS["funny_comments"])
    cfg["funny_comment_chance"] = _to_int(
        cfg.get("funny_comment_chance"), DEFAULTS["funny_comment_chance"], 1, 50
    )
    cfg["funny_comment_cooldown"] = _to_int(
        cfg.get("funny_comment_cooldown"), DEFAULTS["funny_comment_cooldown"], 15, 600
    )
    cfg["vrc_timeline_enabled"] = _to_bool(
        cfg.get("vrc_timeline_enabled"), DEFAULTS["vrc_timeline_enabled"]
    )
    cfg["vrc_log_dir"] = str(cfg.get("vrc_log_dir")).strip() if cfg.get("vrc_log_dir") else None
    cfg["media_title_lookup"] = _to_bool(cfg.get("media_title_lookup"), DEFAULTS["media_title_lookup"])
    cfg["check_updates"] = _to_bool(cfg.get("check_updates"), DEFAULTS["check_updates"])
    cfg["app_version"] = str(cfg.get("app_version")).strip() if cfg.get("app_version") else None
    cfg["author"] = str(cfg.get("author") or DEFAULTS["author"]).strip() or DEFAULTS["author"]
    cfg["discord"] = str(cfg.get("discord") or DEFAULTS["discord"]).strip() or DEFAULTS["discord"]

    if warn and (changed or any(before.get(key) != cfg.get(key) for key in DEFAULTS)):
        STARTUP_WARNINGS.append("Некоторые настройки были некорректными и исправлены автоматически.")

    return cfg


def load_settings():
    existing = [path for path in SETTINGS_READ_FILES if os.path.exists(path)]
    existing.sort(key=lambda path: os.path.getmtime(path), reverse=True)

    for path in existing:
        try:
            with open(path, "r", encoding="utf-8-sig") as f:
                data = json.load(f)
            return normalize_settings(data, warn=True)
        except Exception as e:
            STARTUP_WARNINGS.append(f"Не удалось прочитать настройки из {path}: {e}")

    return normalize_settings(DEFAULTS.copy())


def save_settings(cfg):
    try:
        os.makedirs(os.path.dirname(SETTINGS_FILE), exist_ok=True)
        with open(SETTINGS_FILE, "w", encoding="utf-8") as f:
            json.dump(normalize_settings(cfg), f, ensure_ascii=False, indent=2)
        return True, None
    except Exception as e:
        print("Не удалось сохранить настройки:", e)
        return False, e


def save_last_device(addr):
    try:
        os.makedirs(os.path.dirname(LAST_DEVICE_FILE), exist_ok=True)
        with open(LAST_DEVICE_FILE, "w", encoding="utf-8") as f:
            f.write(addr or "")
        return True, None
    except Exception as e:
        print("Не удалось сохранить устройство:", e)
        return False, e


def load_last_device():
    existing = [path for path in LAST_DEVICE_READ_FILES if os.path.exists(path)]
    existing.sort(key=lambda path: os.path.getmtime(path), reverse=True)

    for path in existing:
        try:
            with open(path, "r", encoding="utf-8") as f:
                val = f.read().strip()
                if val:
                    return val
        except Exception as e:
            STARTUP_WARNINGS.append(f"Не удалось прочитать сохранённый датчик из {path}: {e}")
    return None


def parse_hr(data: bytearray) -> int:
    if not data or len(data) < 2:
        return 0
    flags = data[0]
    if flags & 0x01:
        if len(data) >= 3:
            return int.from_bytes(data[1:3], "little")
        return 0
    return int(data[1])


def hide_windows_console_if_needed(enabled=True):
    if not enabled or os.name != "nt" or os.environ.get("HRGUI_SHOW_CONSOLE") == "1":
        return
    try:
        import ctypes

        hwnd = ctypes.windll.kernel32.GetConsoleWindow()
        if hwnd:
            ctypes.windll.user32.ShowWindow(hwnd, 0)
    except Exception:
        pass


class QueueLogWriter:
    def __init__(self, ui_queue, level):
        self.ui_queue = ui_queue
        self.level = level
        self._buffer = ""

    def write(self, text):
        if not text:
            return

        self._buffer += str(text)
        while "\n" in self._buffer:
            line, self._buffer = self._buffer.split("\n", 1)
            line = line.rstrip()
            if line:
                self.ui_queue.put(("log", (self.level, line)))

    def flush(self):
        line = self._buffer.strip()
        if line:
            self.ui_queue.put(("log", (self.level, line)))
            self._buffer = ""


class AsyncRunner(threading.Thread):
    def __init__(self):
        super().__init__(daemon=True)
        self.loop = asyncio.new_event_loop()
        self._started = threading.Event()

    def run(self):
        asyncio.set_event_loop(self.loop)
        self._started.set()
        self.loop.run_forever()

    def submit(self, coro):
        self._started.wait()
        return asyncio.run_coroutine_threadsafe(coro, self.loop)

    def stop(self):
        def _stop():
            self.loop.stop()
        self.loop.call_soon_threadsafe(_stop)


class HRtoVRCApp:
    MAC_REGEX = re.compile(r"([0-9A-Fa-f]{2}[:-]){5}([0-9A-Fa-f]{2})")
    UUID_REGEX = re.compile(r"[0-9a-fA-F]{8}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{12}")

    def __init__(self, root):
        self.root = root
        self.root.title("HR → VRChat (OSC)")

        self.first_run = not any(os.path.exists(path) for path in SETTINGS_READ_FILES)
        self.cfg = load_settings()
        last_addr = load_last_device()
        if last_addr and not self.cfg.get("last_device"):
            self.cfg["last_device"] = last_addr

        self.osc_client = SimpleUDPClient(self.cfg["osc_ip"], int(self.cfg["osc_port"]))
        self.async_runner = AsyncRunner()
        self.async_runner.start()

        self.ui_queue = queue.Queue()
        self.cfg_lock = threading.Lock()
        self.devices_map = {}
        self.running = False
        self.ble_task_future = None
        self.active_client = None
        self.started_at = None
        self.last_hr_at = None
        self.last_hr_value = None
        self.reconnect_count = 0

        hide_windows_console_if_needed(bool(self.cfg.get("hide_console", True)))
        self._stdout_original = sys.stdout
        self._stderr_original = sys.stderr
        self._stdout_redirect = QueueLogWriter(self.ui_queue, "INFO")
        self._stderr_redirect = QueueLogWriter(self.ui_queue, "ERR")
        sys.stdout = self._stdout_redirect
        sys.stderr = self._stderr_redirect

        if self.first_run:
            saved, error = save_settings(self.cfg)
            if saved:
                STARTUP_INFOS.append(f"Создан файл настроек: {SETTINGS_FILE}")
            else:
                STARTUP_WARNINGS.append(f"Не удалось создать файл настроек при первом запуске: {error}")

        self.joke_active = False
        self.joke_lock = threading.Lock()

        self._build_ui()
        self._apply_compatibility_state()
        self._schedule_ui_poll()
        self.root.after(200, self._report_startup_warnings)

        # Автозапуск, если включена соответствующая галочка
        if self.cfg.get("auto_start") and BleakClient is not None:
            self.root.after(500, self._do_auto_start)
        elif self.cfg.get("auto_start"):
            STARTUP_WARNINGS.append("Автостарт пропущен: BLE недоступен без библиотеки bleak.")

    def _build_ui(self):
        self.root.title("HR -> VRChat (OSC)")
        self.root.geometry("780x640")
        self.root.minsize(720, 560)
        self.root.columnconfigure(0, weight=1)
        self.root.rowconfigure(0, weight=1)

        try:
            self.root.option_add("*Font", "Segoe UI 10")
        except Exception:
            pass

        style = ttk.Style(self.root)
        try:
            style.theme_use("clam")
        except Exception:
            pass
        style.configure("Title.TLabel", font=("Segoe UI", 12, "bold"))
        style.configure("HR.TLabel", font=("Segoe UI", 52, "bold"))
        style.configure("Muted.TLabel", foreground="#666666")
        style.configure("Status.TLabel", foreground="#444444")
        style.configure("Primary.TButton", font=("Segoe UI", 10, "bold"))

        self.status_var = tk.StringVar(value="Готов")
        self.hr_var = tk.StringVar(value="-")
        self.last_pulse_var = tk.StringVar(value="нет данных")
        self.last_chat_var = tk.StringVar(value="-")
        self.reconnects_var = tk.StringVar(value="0")
        self.uptime_var = tk.StringVar(value="00:00")

        self.ip_var = tk.StringVar(value=self.cfg.get("osc_ip"))
        self.port_var = tk.StringVar(value=str(self.cfg.get("osc_port")))
        self.param_var = tk.StringVar(value=self.cfg.get("osc_param"))
        self.send_chat_var = tk.BooleanVar(value=bool(self.cfg.get("send_chat", True)))
        self.chat_template_var = tk.StringVar(value=self.cfg.get("chat_template", "Пульс: {hr}"))
        self.chat_throttle_var = tk.DoubleVar(value=float(self.cfg.get("chat_throttle", 1.0)))
        self.chat_only_on_change_var = tk.BooleanVar(value=bool(self.cfg.get("chat_only_on_change", True)))
        self.auto_start_var = tk.BooleanVar(value=bool(self.cfg.get("auto_start", False)))
        self.auto_reconnect_var = tk.BooleanVar(value=bool(self.cfg.get("auto_reconnect", True)))
        self.stale_timeout_var = tk.DoubleVar(value=float(self.cfg.get("stale_timeout", DEFAULTS["stale_timeout"])))
        self.reconnect_delay_var = tk.DoubleVar(value=float(self.cfg.get("reconnect_delay", DEFAULTS["reconnect_delay"])))
        self.manual_addr_var = tk.StringVar(value=self.cfg.get("last_device") or "")
        self.test_hr_var = tk.IntVar(value=72)

        main = ttk.Frame(self.root, padding=12)
        main.grid(row=0, column=0, sticky="nsew")
        main.columnconfigure(0, weight=1)
        main.rowconfigure(0, weight=1)

        notebook = ttk.Notebook(main)
        notebook.grid(row=0, column=0, sticky="nsew")
        self.notebook = notebook

        pulse_tab = ttk.Frame(notebook, padding=14)
        settings_tab = ttk.Frame(notebook, padding=14)
        log_tab = ttk.Frame(notebook, padding=14)
        notebook.add(pulse_tab, text="Пульс")
        notebook.add(settings_tab, text="Настройки")
        notebook.add(log_tab, text="Журнал")

        pulse_tab.columnconfigure(0, weight=1)
        pulse_tab.rowconfigure(2, weight=1)

        top = ttk.Frame(pulse_tab)
        top.grid(row=0, column=0, sticky="ew")
        top.columnconfigure(0, weight=1)
        top.columnconfigure(1, weight=1)

        hr_box = ttk.LabelFrame(top, text="Текущий пульс", padding=14)
        hr_box.grid(row=0, column=0, sticky="nsew", padx=(0, 8))
        hr_box.columnconfigure(0, weight=1)
        ttk.Label(hr_box, textvariable=self.hr_var, style="HR.TLabel").grid(row=0, column=0, sticky="w")
        ttk.Label(hr_box, text="уд/мин", style="Muted.TLabel").grid(row=0, column=1, sticky="sw", padx=(8, 0), pady=(0, 12))
        ttk.Label(hr_box, text="Последнее обновление:", style="Muted.TLabel").grid(row=1, column=0, sticky="w", pady=(6, 0))
        ttk.Label(hr_box, textvariable=self.last_pulse_var).grid(row=1, column=1, sticky="w", pady=(6, 0))

        status_box = ttk.LabelFrame(top, text="Состояние", padding=14)
        status_box.grid(row=0, column=1, sticky="nsew", padx=(8, 0))
        status_box.columnconfigure(1, weight=1)
        ttk.Label(status_box, text="Статус:", style="Muted.TLabel").grid(row=0, column=0, sticky="w")
        ttk.Label(status_box, textvariable=self.status_var, style="Status.TLabel", wraplength=320).grid(row=0, column=1, sticky="w", padx=(8, 0))
        ttk.Label(status_box, text="Переподключений:", style="Muted.TLabel").grid(row=1, column=0, sticky="w", pady=(8, 0))
        ttk.Label(status_box, textvariable=self.reconnects_var).grid(row=1, column=1, sticky="w", padx=(8, 0), pady=(8, 0))
        ttk.Label(status_box, text="Время работы:", style="Muted.TLabel").grid(row=2, column=0, sticky="w", pady=(8, 0))
        ttk.Label(status_box, textvariable=self.uptime_var).grid(row=2, column=1, sticky="w", padx=(8, 0), pady=(8, 0))
        ttk.Label(status_box, text="Чат:", style="Muted.TLabel").grid(row=3, column=0, sticky="w", pady=(8, 0))
        ttk.Label(status_box, textvariable=self.last_chat_var, wraplength=320).grid(row=3, column=1, sticky="w", padx=(8, 0), pady=(8, 0))

        actions = ttk.Frame(pulse_tab)
        actions.grid(row=1, column=0, sticky="ew", pady=(12, 10))
        actions.columnconfigure(4, weight=1)
        self.start_btn = ttk.Button(actions, text="START", command=self.toggle_start, width=14, style="Primary.TButton")
        self.start_btn.grid(row=0, column=0, sticky="w")
        self.btn_reconnect = ttk.Button(actions, text="Переподключить", command=self.reconnect_now)
        self.btn_reconnect.grid(row=0, column=1, sticky="w", padx=(8, 0))
        self.joke_btn = ttk.Button(actions, text="Пульс = 0 на 10 сек", command=self._joke_zero_pulse)
        self.joke_btn.grid(row=0, column=2, sticky="w", padx=(8, 0))
        ttk.Label(actions, text="Тест:", style="Muted.TLabel").grid(row=0, column=3, sticky="e", padx=(18, 4))
        ttk.Spinbox(actions, from_=30, to=220, increment=1, textvariable=self.test_hr_var, width=6).grid(row=0, column=4, sticky="w")
        ttk.Button(actions, text="Отправить OSC", command=self.send_test_pulse).grid(row=0, column=5, sticky="w", padx=(8, 0))

        device_box = ttk.LabelFrame(pulse_tab, text="Датчик", padding=12)
        device_box.grid(row=2, column=0, sticky="nsew")
        device_box.columnconfigure(1, weight=1)

        ttk.Label(device_box, text="BLE устройство:").grid(row=0, column=0, sticky="w")
        self.devices_combo = ttk.Combobox(device_box, values=[], state="readonly")
        self.devices_combo.grid(row=0, column=1, sticky="ew", padx=(8, 8))
        self.btn_scan = ttk.Button(device_box, text="Сканировать", command=self.scan_devices)
        self.btn_scan.grid(row=0, column=2, sticky="e")

        ttk.Label(device_box, text="Адрес вручную:").grid(row=1, column=0, sticky="w", pady=(8, 0))
        ttk.Entry(device_box, textvariable=self.manual_addr_var).grid(row=1, column=1, sticky="ew", padx=(8, 8), pady=(8, 0))
        self.btn_save_device = ttk.Button(device_box, text="Сохранить датчик", command=self.save_selected_device)
        self.btn_save_device.grid(row=1, column=2, sticky="e", pady=(8, 0))

        opts = ttk.Frame(device_box)
        opts.grid(row=2, column=0, columnspan=3, sticky="ew", pady=(12, 0))
        ttk.Checkbutton(opts, text="Автостарт при запуске", variable=self.auto_start_var).grid(row=0, column=0, sticky="w")
        ttk.Checkbutton(opts, text="Автопереподключение", variable=self.auto_reconnect_var).grid(row=0, column=1, sticky="w", padx=(18, 0))

        settings_tab.columnconfigure(1, weight=1)
        ttk.Label(settings_tab, text="OSC", style="Title.TLabel").grid(row=0, column=0, columnspan=4, sticky="w", pady=(0, 8))
        ttk.Label(settings_tab, text="IP:").grid(row=1, column=0, sticky="e", pady=4)
        ttk.Entry(settings_tab, textvariable=self.ip_var, width=18).grid(row=1, column=1, sticky="w", padx=(8, 16), pady=4)
        ttk.Label(settings_tab, text="Port:").grid(row=1, column=2, sticky="e", pady=4)
        ttk.Entry(settings_tab, textvariable=self.port_var, width=8).grid(row=1, column=3, sticky="w", padx=(8, 0), pady=4)
        ttk.Label(settings_tab, text="Параметр:").grid(row=2, column=0, sticky="e", pady=4)
        ttk.Entry(settings_tab, textvariable=self.param_var).grid(row=2, column=1, columnspan=3, sticky="ew", padx=(8, 0), pady=4)

        ttk.Separator(settings_tab).grid(row=3, column=0, columnspan=4, sticky="ew", pady=14)
        ttk.Label(settings_tab, text="Чат VRChat", style="Title.TLabel").grid(row=4, column=0, columnspan=4, sticky="w", pady=(0, 8))
        ttk.Checkbutton(settings_tab, text="Отправлять пульс в чат", variable=self.send_chat_var).grid(row=5, column=0, columnspan=4, sticky="w", pady=4)
        ttk.Label(settings_tab, text="Шаблон:").grid(row=6, column=0, sticky="e", pady=4)
        self.template_entry = ttk.Entry(settings_tab, textvariable=self.chat_template_var)
        self.template_entry.grid(row=6, column=1, columnspan=3, sticky="ew", padx=(8, 0), pady=4)
        ttk.Label(settings_tab, text="Интервал, сек:").grid(row=7, column=0, sticky="e", pady=4)
        ttk.Spinbox(settings_tab, from_=0.0, to=3600.0, increment=0.1, textvariable=self.chat_throttle_var, width=9).grid(row=7, column=1, sticky="w", padx=(8, 0), pady=4)
        ttk.Checkbutton(settings_tab, text="Только при изменении", variable=self.chat_only_on_change_var).grid(row=7, column=2, columnspan=2, sticky="w", padx=(16, 0), pady=4)

        ttk.Separator(settings_tab).grid(row=8, column=0, columnspan=4, sticky="ew", pady=14)
        ttk.Label(settings_tab, text="Надёжность", style="Title.TLabel").grid(row=9, column=0, columnspan=4, sticky="w", pady=(0, 8))
        ttk.Label(settings_tab, text="Нет пульса, сек:").grid(row=10, column=0, sticky="e", pady=4)
        ttk.Spinbox(settings_tab, from_=5.0, to=120.0, increment=1.0, textvariable=self.stale_timeout_var, width=9).grid(row=10, column=1, sticky="w", padx=(8, 0), pady=4)
        ttk.Label(settings_tab, text="Пауза переподключения, сек:").grid(row=11, column=0, sticky="e", pady=4)
        ttk.Spinbox(settings_tab, from_=0.5, to=60.0, increment=0.5, textvariable=self.reconnect_delay_var, width=9).grid(row=11, column=1, sticky="w", padx=(8, 0), pady=4)
        self.btn_save_settings = ttk.Button(settings_tab, text="Сохранить настройки", command=self.save_settings_gui, style="Primary.TButton")
        self.btn_save_settings.grid(row=12, column=0, columnspan=2, sticky="w", pady=(16, 0))

        log_tab.columnconfigure(0, weight=1)
        log_tab.rowconfigure(1, weight=1)
        log_actions = ttk.Frame(log_tab)
        log_actions.grid(row=0, column=0, sticky="ew", pady=(0, 8))
        ttk.Button(log_actions, text="Очистить", command=self.clear_log).grid(row=0, column=0, sticky="w")
        ttk.Button(log_actions, text="Копировать", command=self.copy_log).grid(row=0, column=1, sticky="w", padx=(8, 0))
        self.log_text = tk.Text(log_tab, height=16, wrap="word", state="disabled", font=("Consolas", 9), borderwidth=1, relief="solid")
        self.log_text.grid(row=1, column=0, sticky="nsew")
        log_scroll = ttk.Scrollbar(log_tab, orient="vertical", command=self.log_text.yview)
        log_scroll.grid(row=1, column=1, sticky="ns")
        self.log_text.configure(yscrollcommand=log_scroll.set)

        ld = self.cfg.get("last_device")
        if ld:
            display = f"saved - {ld}"
            self.devices_map[display] = ld
            self.devices_combo["values"] = [display]
            self.devices_combo.set(display)

    def _apply_compatibility_state(self):
        if BleakClient is None or BleakScanner is None:
            try:
                self.btn_scan.config(state="disabled")
                self.start_btn.config(state="disabled")
                self.btn_reconnect.config(state="disabled")
            except Exception:
                pass

    def _report_startup_warnings(self):
        for info in STARTUP_INFOS:
            self.ui_queue.put(("log", ("INFO", info)))

        self.ui_queue.put(("log", ("INFO", f"Настройки: {SETTINGS_FILE}")))

        if not STARTUP_WARNINGS:
            return

        for warning in STARTUP_WARNINGS:
            self.ui_queue.put(("warning", warning))

        if BleakClient is None or BleakScanner is None:
            self.status_var.set("BLE недоступен: нужна библиотека bleak. Подробности в журнале.")
        else:
            self.status_var.set("Есть предупреждения совместимости. Подробности в журнале.")

    def _build_legacy_ui(self):
        frm = ttk.Frame(self.root, padding=10)
        frm.grid(sticky="nsew")

        ttk.Label(frm, text="OSC", font=("Segoe UI", 10, "bold")).grid(row=0, column=0, sticky="w", pady=(0, 6))

        ttk.Label(frm, text="IP:").grid(row=1, column=0, sticky="e")
        self.ip_var = tk.StringVar(value=self.cfg.get("osc_ip"))
        ttk.Entry(frm, textvariable=self.ip_var, width=18).grid(row=1, column=1, sticky="w")

        ttk.Label(frm, text="Port:").grid(row=1, column=2, sticky="e")
        self.port_var = tk.StringVar(value=str(self.cfg.get("osc_port")))
        ttk.Entry(frm, textvariable=self.port_var, width=6).grid(row=1, column=3, sticky="w")

        ttk.Label(frm, text="Param:").grid(row=2, column=0, sticky="e")
        self.param_var = tk.StringVar(value=self.cfg.get("osc_param"))
        ttk.Entry(frm, textvariable=self.param_var, width=30).grid(row=2, column=1, columnspan=3, sticky="w")

        self.send_chat_var = tk.BooleanVar(value=bool(self.cfg.get("send_chat", True)))
        ttk.Checkbutton(frm, text="Отправлять в чат VRChat", variable=self.send_chat_var).grid(
            row=3, column=0, columnspan=4, sticky="w", pady=(8, 0)
        )

        ttk.Label(frm, text="Шаблон (исп. {hr}):").grid(row=4, column=0, sticky="e")
        self.chat_template_var = tk.StringVar(value=self.cfg.get("chat_template", "Пульс: {hr}"))
        self.template_entry = ttk.Entry(frm, textvariable=self.chat_template_var, width=40)
        self.template_entry.grid(row=4, column=1, columnspan=3, sticky="w")
        self.template_entry.focus_set()

        ttk.Label(frm, text="Троттлинг (сек):").grid(row=5, column=0, sticky="e")
        self.chat_throttle_var = tk.DoubleVar(value=float(self.cfg.get("chat_throttle", 1.0)))
        ttk.Spinbox(frm, from_=0.0, to=3600.0, increment=0.1, textvariable=self.chat_throttle_var, width=8).grid(
            row=5, column=1, sticky="w"
        )

        self.chat_only_on_change_var = tk.BooleanVar(value=bool(self.cfg.get("chat_only_on_change", True)))
        ttk.Checkbutton(frm, text="Только при изменении", variable=self.chat_only_on_change_var).grid(
            row=5, column=2, columnspan=2, sticky="w"
        )

        sep = ttk.Separator(frm, orient="horizontal")
        sep.grid(row=6, column=0, columnspan=4, sticky="ew", pady=8)

        ttk.Label(frm, text="BLE устройства:", font=("Segoe UI", 10, "bold")).grid(
            row=7, column=0, columnspan=4, sticky="w"
        )

        self.devices_combo = ttk.Combobox(frm, values=[], width=56, state="readonly")
        self.devices_combo.grid(row=8, column=0, columnspan=3, sticky="w")

        self.btn_scan = ttk.Button(frm, text="Сканировать", command=self.scan_devices)
        self.btn_scan.grid(row=8, column=3, sticky="w")

        ttk.Label(frm, text="Или вставьте адрес вручную:").grid(row=9, column=0, sticky="w", pady=(6, 0))
        self.manual_addr_var = tk.StringVar(value=self.cfg.get("last_device") or "")
        ttk.Entry(frm, textvariable=self.manual_addr_var, width=36).grid(row=9, column=1, columnspan=2, sticky="w")

        self.btn_save_device = ttk.Button(frm, text="Сохранить по умолчанию", command=self.save_selected_device)
        self.btn_save_device.grid(row=9, column=3, sticky="w", padx=(6, 0))

        # Галочки автозапуска и автопереподключения
        self.auto_start_var = tk.BooleanVar(value=bool(self.cfg.get("auto_start", False)))
        ttk.Checkbutton(frm, text="Авто подключение при запуске", variable=self.auto_start_var).grid(
            row=10, column=0, columnspan=2, sticky="w", pady=(8, 0)
        )

        self.auto_reconnect_var = tk.BooleanVar(value=bool(self.cfg.get("auto_reconnect", True)))
        ttk.Checkbutton(frm, text="Авто переподключение при обрыве", variable=self.auto_reconnect_var).grid(
            row=10, column=2, columnspan=2, sticky="w", pady=(8, 0)
        )

        self.start_btn = ttk.Button(frm, text="START", command=self.toggle_start, width=18)
        self.start_btn.grid(row=11, column=0, columnspan=2, pady=(12, 0), sticky="w")

        self.joke_btn = ttk.Button(frm, text="Шутка: пульс = 0 (10s)", command=self._joke_zero_pulse)
        self.joke_btn.grid(row=11, column=2, columnspan=2, pady=(12, 0), sticky="w")

        ttk.Label(frm, text="Текущий пульс:").grid(row=12, column=0, sticky="w", pady=(8, 0))
        self.hr_var = tk.StringVar(value="-")
        ttk.Label(frm, textvariable=self.hr_var, font=("Segoe UI", 12, "bold")).grid(row=12, column=1, sticky="w", pady=(8, 0))

        ttk.Label(frm, text="Последняя отправка в чат:").grid(row=13, column=0, sticky="w")
        self.last_chat_var = tk.StringVar(value="-")
        ttk.Label(frm, textvariable=self.last_chat_var).grid(row=13, column=1, columnspan=3, sticky="w")

        self.btn_save_settings = ttk.Button(frm, text="Сохранить настройки", command=self.save_settings_gui)
        self.btn_save_settings.grid(row=14, column=0, columnspan=2, sticky="w", pady=(12, 0))

        self.status_var = tk.StringVar(value="Готов")
        ttk.Label(frm, textvariable=self.status_var, foreground="gray").grid(
            row=15, column=0, columnspan=4, sticky="w", pady=(8, 0)
        )

        ld = self.cfg.get("last_device")
        if ld:
            display = f"saved — {ld}"
            self.devices_map[display] = ld
            self.devices_combo["values"] = [display]
            self.devices_combo.set(display)

    def _snapshot_ui_to_cfg(self):
        with self.cfg_lock:
            self.cfg["osc_ip"] = self.ip_var.get().strip()
            try:
                self.cfg["osc_port"] = int(self.port_var.get())
            except Exception:
                self.cfg["osc_port"] = DEFAULTS["osc_port"]
            self.cfg["osc_param"] = self.param_var.get().strip() or DEFAULTS["osc_param"]
            self.cfg["send_chat"] = bool(self.send_chat_var.get())
            self.cfg["chat_template"] = self.chat_template_var.get()
            try:
                self.cfg["chat_throttle"] = float(self.chat_throttle_var.get())
            except Exception:
                self.cfg["chat_throttle"] = DEFAULTS["chat_throttle"]
            self.cfg["chat_only_on_change"] = bool(self.chat_only_on_change_var.get())
            self.cfg["auto_start"] = bool(self.auto_start_var.get())
            self.cfg["auto_reconnect"] = bool(self.auto_reconnect_var.get())
            try:
                self.cfg["stale_timeout"] = float(self.stale_timeout_var.get())
            except Exception:
                self.cfg["stale_timeout"] = DEFAULTS["stale_timeout"]
            try:
                self.cfg["reconnect_delay"] = float(self.reconnect_delay_var.get())
            except Exception:
                self.cfg["reconnect_delay"] = DEFAULTS["reconnect_delay"]
            self.cfg = normalize_settings(self.cfg)

    def _sync_cfg_to_ui(self):
        self.ip_var.set(self.cfg["osc_ip"])
        self.port_var.set(str(self.cfg["osc_port"]))
        self.param_var.set(self.cfg["osc_param"])
        self.send_chat_var.set(bool(self.cfg["send_chat"]))
        self.chat_template_var.set(self.cfg["chat_template"])
        self.chat_throttle_var.set(float(self.cfg["chat_throttle"]))
        self.chat_only_on_change_var.set(bool(self.cfg["chat_only_on_change"]))
        self.auto_start_var.set(bool(self.cfg["auto_start"]))
        self.auto_reconnect_var.set(bool(self.cfg["auto_reconnect"]))
        self.stale_timeout_var.set(float(self.cfg["stale_timeout"]))
        self.reconnect_delay_var.set(float(self.cfg["reconnect_delay"]))

    def scan_devices(self):
        if BleakScanner is None:
            msg = "Сканирование BLE недоступно: не установлена библиотека bleak."
            self.status_var.set(msg)
            self.ui_queue.put(("warning", msg))
            return

        self.status_var.set("Сканирование BLE...")
        self.btn_scan.config(state="disabled")
        self.async_runner.submit(self._async_scan_once(5.0))

    def save_selected_device(self):
        addr = self._get_selected_address()
        if not addr:
            messagebox.showinfo("Информация", "Не удалось определить адрес. Выберите устройство или введите адрес вручную.")
            return
        with self.cfg_lock:
            self.cfg["last_device"] = addr
        device_ok, device_error = save_last_device(addr)
        settings_ok, settings_error = save_settings(self.cfg)
        if device_ok and settings_ok:
            messagebox.showinfo("Сохранено", f"Сохранён адрес: {addr}")
        else:
            err = device_error or settings_error
            self.ui_queue.put(("warning", f"Не удалось сохранить датчик: {err}"))
            messagebox.showwarning("Предупреждение", f"Адрес выбран, но сохранить его не удалось:\n{err}")

    def save_settings_gui(self):
        self._snapshot_ui_to_cfg()
        self._sync_cfg_to_ui()
        ok, err = save_settings(self.cfg)
        self.osc_client = SimpleUDPClient(self.cfg["osc_ip"], int(self.cfg["osc_port"]))
        if ok:
            messagebox.showinfo("Сохранено", "Настройки сохранены.")
        else:
            self.ui_queue.put(("warning", f"Не удалось сохранить настройки: {err}"))
            messagebox.showwarning("Предупреждение", f"Настройки применены, но сохранить их не удалось:\n{err}")

    def reconnect_now(self):
        if not self.running:
            self.toggle_start()
            return

        self._snapshot_ui_to_cfg()
        self.status_var.set("Переподключаюсь...")
        self.ui_queue.put(("log", ("INFO", "Ручное переподключение.")))
        self.async_runner.submit(self._async_request_disconnect())

    def send_test_pulse(self):
        self._snapshot_ui_to_cfg()
        try:
            hr_val = int(self.test_hr_var.get())
            client = SimpleUDPClient(self.cfg["osc_ip"], int(self.cfg["osc_port"]))
            client.send_message(self.cfg["osc_param"], hr_val)
            self.hr_var.set(str(hr_val))
            self.last_pulse_var.set(time.strftime("%H:%M:%S") + " (тест)")
            self.status_var.set(f"Тестовый пульс {hr_val} отправлен в OSC.")
            self.ui_queue.put(("log", ("INFO", f"Тестовый OSC-пульс отправлен: {hr_val}")))
        except Exception as e:
            self.status_var.set(f"Не удалось отправить тестовый пульс: {e}")
            self.ui_queue.put(("log", ("ERR", f"Ошибка тестовой OSC-отправки: {e}")))

    def clear_log(self):
        if not getattr(self, "log_text", None):
            return
        self.log_text.configure(state="normal")
        self.log_text.delete("1.0", "end")
        self.log_text.configure(state="disabled")

    def copy_log(self):
        if not getattr(self, "log_text", None):
            return
        text = self.log_text.get("1.0", "end").strip()
        self.root.clipboard_clear()
        self.root.clipboard_append(text)
        self.status_var.set("Журнал скопирован.")

    def _append_log(self, level, message):
        if not getattr(self, "log_text", None):
            return
        timestamp = time.strftime("%H:%M:%S")
        line = f"[{timestamp}] [{level}] {message}\n"
        self.log_text.configure(state="normal")
        self.log_text.insert("end", line)
        self.log_text.see("end")
        self.log_text.configure(state="disabled")

    def _do_auto_start(self):
        if self._get_selected_address():
            self.toggle_start()
        else:
            self.status_var.set("Автозапуск отменён: не найден адрес устройства.")

    def toggle_start(self):
        if not self.running:
            if BleakClient is None:
                msg = "Подключение к датчику недоступно: не установлена библиотека bleak."
                self.status_var.set(msg)
                self.ui_queue.put(("warning", msg))
                messagebox.showwarning("BLE недоступен", msg)
                return

            self._snapshot_ui_to_cfg()
            self.osc_client = SimpleUDPClient(self.cfg["osc_ip"], int(self.cfg["osc_port"]))

            addr = self._get_selected_address()
            if not addr:
                messagebox.showinfo("Информация", "Не удалось определить адрес. Выберите устройство или вставьте адрес вручную.")
                return

            with self.cfg_lock:
                self.cfg["last_device"] = addr
            saved, save_error = save_last_device(addr)
            if not saved:
                self.ui_queue.put(("warning", f"Датчик выбран, но сохранить его не удалось: {save_error}"))

            self.running = True
            self.started_at = time.time()
            self.last_hr_at = None
            self.last_hr_value = None
            self.reconnect_count = 0
            self.reconnects_var.set("0")
            self.uptime_var.set("00:00")
            self.last_pulse_var.set("ожидание данных")
            self.start_btn.config(text="STOP")
            self.status_var.set("Запускаю подписку...")
            self.ui_queue.put(("log", ("INFO", f"Старт подключения к {addr}")))
            self.ble_task_future = self.async_runner.submit(self._async_connect_and_subscribe(addr))
        else:
            self.running = False
            self.start_btn.config(text="START")
            self.status_var.set("Остановлено пользователем.")
            self.ui_queue.put(("log", ("INFO", "Остановлено пользователем.")))
            if self.ble_task_future:
                self.async_runner.submit(self._async_request_disconnect())

    def _get_selected_address(self):
        sel = self.devices_combo.get().strip()
        if sel and sel in self.devices_map:
            return self.devices_map[sel]

        manual = self.manual_addr_var.get().strip()
        if manual:
            m = self.MAC_REGEX.search(manual)
            if m:
                return m.group(0)
            m = self.UUID_REGEX.search(manual)
            if m:
                return m.group(0)
            if re.fullmatch(r"[0-9A-Fa-f:.\-]{8,}", manual):
                return manual

        if sel:
            m = self.MAC_REGEX.search(sel)
            if m:
                return m.group(0)
            m = self.UUID_REGEX.search(sel)
            if m:
                return m.group(0)

        return None

    def _format_chat_text(self, hr_val: int) -> str:
        with self.cfg_lock:
            template = self.cfg.get("chat_template", "") or ""
        if "{hr}" in template:
            txt = template.format(hr=hr_val)
        elif template.strip():
            txt = template
        else:
            txt = str(hr_val)
        return txt[:240]

    def _send_chat_text(self, client, txt: str) -> bool:
        try:
            # Прямая и безопасная отправка списка аргументов
            client.send_message("/chatbox/input", [txt, True])
            return True
        except Exception:
            try:
                # Резервный вариант
                client.send_message("/chatbox/input", txt)
                time.sleep(0.05)
                client.send_message("/chatbox/submit", 1)
                return True
            except Exception as e:
                print("[CHAT ERROR] fallback failed:", e)
                return False

    async def _async_scan_once(self, timeout=5.0):
        if BleakScanner is None:
            self.ui_queue.put(("warning", "Сканирование BLE недоступно: не установлена библиотека bleak."))
            self.ui_queue.put(("scan_done", None))
            return

        try:
            devices = await BleakScanner.discover(timeout=timeout)
            devs = []
            self.devices_map.clear()

            for d in devices:
                name = getattr(d, "name", None) or "(no name)"
                addr = getattr(d, "address", None) or ""
                mark = " (likely HR)" if any(k in name.lower() for k in NAME_KEYWORDS) else ""
                display = f"{name} — {addr}{mark}"
                base = display
                i = 1
                while display in self.devices_map:
                    i += 1
                    display = f"{base} #{i}"
                self.devices_map[display] = addr
                devs.append(display)

            last = load_last_device()
            if last and last not in self.devices_map.values():
                display = f"saved — {last}"
                self.devices_map[display] = last
                devs.insert(0, display)

            self.ui_queue.put(("scan_result", devs))
        except Exception as e:
            self.ui_queue.put(("error", f"Ошибка сканирования: {e}"))
        finally:
            self.ui_queue.put(("scan_done", None))

    async def _async_connect_and_subscribe(self, address):
        if BleakClient is None:
            self.ui_queue.put(("error", "BLE-подключение недоступно: не установлена библиотека bleak."))
            self.ui_queue.put(("stopped", None))
            return

        osc_ip = self.cfg["osc_ip"]
        osc_port = int(self.cfg["osc_port"])
        osc_param = self.cfg["osc_param"]
        send_chat = bool(self.cfg["send_chat"])
        chat_throttle = float(self.cfg["chat_throttle"])
        chat_only_on_change = bool(self.cfg["chat_only_on_change"])

        osc_client_local = SimpleUDPClient(osc_ip, osc_port)

        chat_state = {"latest_hr": None, "seq": 0}
        chat_lock = threading.Lock()
        watchdog = {"last_notify": time.monotonic()}

        def on_hr_notify(_, data: bytearray):
            watchdog["last_notify"] = time.monotonic()
            hr = parse_hr(data)
            if self.joke_active:
                self.ui_queue.put(("hr", 0))
                return

            try:
                osc_client_local.send_message(osc_param, hr)
            except Exception as e:
                print("[OSC ERROR] param send:", e)

            if send_chat:
                with chat_lock:
                    chat_state["latest_hr"] = hr
                    chat_state["seq"] += 1

            self.ui_queue.put(("hr", hr))

        async def chat_sender_loop():
            last_sent_seq = -1
            last_sent_hr = None
            last_sent_time = 0.0

            while self.running:
                await asyncio.sleep(0.1)

                if not send_chat or self.joke_active:
                    continue

                with chat_lock:
                    seq = chat_state["seq"]
                    hr_val = chat_state["latest_hr"]

                if hr_val is None or seq == last_sent_seq:
                    continue

                now = time.time()
                if now - last_sent_time < max(0.0, chat_throttle):
                    continue

                if chat_only_on_change and last_sent_hr is not None and hr_val == last_sent_hr:
                    last_sent_seq = seq
                    continue

                txt = self._format_chat_text(hr_val)
                print(f"[CHAT] send latest HR={hr_val}: '{txt}'")
                if self._send_chat_text(osc_client_local, txt):
                    last_sent_time = now
                    last_sent_hr = hr_val
                    last_sent_seq = seq
                    self.ui_queue.put(("chat_sent", (txt, time.strftime("%H:%M:%S"))))
                else:
                    last_sent_seq = seq
                    self.ui_queue.put(("chat_failed", txt))

        if not (
            self.MAC_REGEX.search(address)
            or self.UUID_REGEX.search(address)
            or re.fullmatch(r"[0-9A-Fa-f:.\-]{8,}", address)
        ):
            self.ui_queue.put(("error", f"Некорректный адрес устройства: {address}"))
            self.ui_queue.put(("stopped", None))
            return

        chat_task = None
        if send_chat:
            chat_task = asyncio.create_task(chat_sender_loop())

        try:
            connection_attempt = 0
            while self.running:
                try:
                    with self.cfg_lock:
                        stale_timeout = max(5.0, float(self.cfg.get("stale_timeout", DEFAULTS["stale_timeout"])))

                    connection_attempt += 1
                    if connection_attempt > 1:
                        self.ui_queue.put(("reconnect_count", connection_attempt - 1))
                        self.ui_queue.put(("log", ("INFO", f"Попытка переподключения #{connection_attempt - 1}")))

                    self.ui_queue.put(("status", f"Подключение к {address}..."))
                    async with BleakClient(address, timeout=15.0) as client:
                        self.active_client = client
                        if not client.is_connected:
                            self.ui_queue.put(("error", "Не удалось подключиться к устройству."))
                            raise Exception("Connection failed")

                        self.ui_queue.put(("status", f"Подключено к {address} — настраиваю подписку..."))

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
                            raise Exception("Не удалось получить services.")

                        try:
                            char_uuids = [c.uuid.lower() for s in services for c in s.characteristics]
                        except Exception:
                            char_uuids = []
                        
                        if HR_CHAR_UUID not in char_uuids:
                            self.ui_queue.put(("error", "HR-характеристика (0x2A37) не найдена на устройстве."))
                            return  # Фатальная ошибка (скорее всего не HR датчик), завершаем без реконнекта

                        max_tries = 4
                        subscribed = False
                        for attempt in range(1, max_tries + 1):
                            if not client.is_connected or not self.running:
                                break

                            try:
                                self.ui_queue.put(("status", f"Попытка подписки [{attempt}]..."))
                                await client.start_notify(HR_CHAR_UUID, on_hr_notify)
                                self.ui_queue.put(("status", "Подписка успешна — слушаем уведомления..."))
                                subscribed = True
                                break
                            except Exception as e:
                                print(f"[{attempt}] Ошибка start_notify: {e}")
                                if attempt < max_tries:
                                    await asyncio.sleep(0.7 + attempt * 0.5)
                                else:
                                    self.ui_queue.put(("error", f"Не удалось подписаться: {e}"))

                        if subscribed:
                            watchdog["last_notify"] = time.monotonic()
                            stale_detected = False
                            while client.is_connected and self.running:
                                await asyncio.sleep(0.5)
                                if self.joke_active:
                                    watchdog["last_notify"] = time.monotonic()
                                    continue
                                if time.monotonic() - watchdog["last_notify"] > stale_timeout:
                                    stale_detected = True
                                    self.ui_queue.put(("warning", f"Пульс не обновлялся {stale_timeout:.0f} сек. Переподключаюсь..."))
                                    break

                            try:
                                await asyncio.wait_for(client.stop_notify(HR_CHAR_UUID), timeout=2.0)
                            except Exception:
                                pass

                        if self.running:
                            if subscribed and stale_detected:
                                self.ui_queue.put(("status", "Поток пульса остановился. Переподключаюсь..."))
                            else:
                                self.ui_queue.put(("status", "Соединение разорвано."))

                    self.active_client = None

                except Exception as e:
                    self.active_client = None
                    self.ui_queue.put(("error", f"Ошибка BLE: {e}"))

                if not self.running:
                    break

                with self.cfg_lock:
                    auto_reconnect = bool(self.cfg.get("auto_reconnect", True))
                    reconnect_delay = max(0.5, float(self.cfg.get("reconnect_delay", DEFAULTS["reconnect_delay"])))

                if not auto_reconnect:
                    self.ui_queue.put(("status", "Остановлено (авто переподключение выключено)."))
                    break

                self.ui_queue.put(("status", f"Переподключение через {reconnect_delay:g} сек..."))
                end_time = time.monotonic() + reconnect_delay
                while self.running and time.monotonic() < end_time:
                    await asyncio.sleep(min(0.2, end_time - time.monotonic()))

        finally:
            self.active_client = None
            self.running = False
            self.ui_queue.put(("stopped", None))
            
            if chat_task is not None:
                chat_task.cancel()
                try:
                    await chat_task
                except asyncio.CancelledError:
                    pass

    async def _async_request_disconnect(self):
        client = self.active_client
        if client is not None:
            try:
                if client.is_connected:
                    await asyncio.wait_for(client.disconnect(), timeout=3.0)
            except Exception as e:
                self.ui_queue.put(("log", ("ERR", f"Не удалось быстро отключить BLE: {e}")))
        await asyncio.sleep(0.1)

    def _joke_zero_pulse(self):
        with self.joke_lock:
            if self.joke_active:
                return
            self.joke_active = True

        self.hr_var.set("0")
        self.status_var.set("Шутка: пульс = 0 (10s)...")
        self.joke_btn.config(state="disabled")

        self._snapshot_ui_to_cfg()
        osc_ip = self.cfg["osc_ip"]
        osc_port = int(self.cfg["osc_port"])
        osc_param = self.cfg["osc_param"]
        send_chat = bool(self.cfg["send_chat"])

        self.async_runner.submit(self._async_joke_blast(osc_ip, osc_port, osc_param, send_chat, 10.0, 1.0))

    async def _async_joke_blast(self, osc_ip, osc_port, osc_param, send_chat, duration=10.0, interval=1.0):
        client = SimpleUDPClient(osc_ip, osc_port)
        end = time.time() + duration

        try:
            while time.time() < end:
                try:
                    client.send_message(osc_param, 0)
                except Exception as e:
                    print("[JOKE] param send failed:", e)

                if send_chat:
                    txt = self._format_chat_text(0)
                    if self._send_chat_text(client, txt):
                        self.ui_queue.put(("chat_sent", (txt, time.strftime("%H:%M:%S"))))
                    else:
                        self.ui_queue.put(("chat_failed", txt))

                self.ui_queue.put(("hr", 0))
                await asyncio.sleep(interval)

        finally:
            with self.joke_lock:
                self.joke_active = False

            # Безопасная разблокировка UI через очередь (потокобезопасно)
            self.ui_queue.put(("joke_done", None))

    def _schedule_ui_poll(self):
        try:
            while True:
                typ, payload = self.ui_queue.get_nowait()

                if typ == "scan_result":
                    devs = payload
                    self.devices_combo["values"] = devs
                    if devs and not self.devices_combo.get():
                        self.devices_combo.set(devs[0])
                    self.status_var.set(f"Сканирование завершено — найдено {len(devs)} устройств.")

                elif typ == "scan_done":
                    self.btn_scan.config(state="normal")

                elif typ == "hr":
                    self.hr_var.set(str(payload))
                    self.last_hr_value = payload
                    self.last_hr_at = time.time()
                    self.last_pulse_var.set(time.strftime("%H:%M:%S"))

                elif typ == "status":
                    self.status_var.set(str(payload))

                elif typ == "warning":
                    self.status_var.set(str(payload))
                    self._append_log("WARN", str(payload))

                elif typ == "error":
                    self.status_var.set(str(payload))
                    self._append_log("ERR", str(payload))

                elif typ == "log":
                    level, message = payload
                    self._append_log(level, message)

                elif typ == "reconnect_count":
                    self.reconnect_count = int(payload)
                    self.reconnects_var.set(str(payload))

                elif typ == "chat_sent":
                    txt, t = payload
                    self.last_chat_var.set(f"[{t}] {txt}")

                elif typ == "chat_failed":
                    txt = payload
                    self.last_chat_var.set(f"FAILED: {txt}")
                
                elif typ == "stopped":
                    self.running = False
                    self.started_at = None
                    try:
                        self.start_btn.config(text="START")
                        # Не сбрасываем статус, если там сообщение об ошибке
                        if not self.status_var.get().startswith("Остановлено") and "Переподключение" not in self.status_var.get():
                            self.status_var.set("Остановлено.")
                    except Exception:
                        pass

                # Обработка сигнала завершения шутки в главном потоке
                elif typ == "joke_done":
                    try:
                        self.joke_btn.config(state="normal")
                        self.status_var.set("Готов")
                    except Exception:
                        pass

        except queue.Empty:
            pass

        if self.running and self.started_at:
            elapsed = max(0, int(time.time() - self.started_at))
            minutes, seconds = divmod(elapsed, 60)
            hours, minutes = divmod(minutes, 60)
            self.uptime_var.set(f"{hours:02d}:{minutes:02d}:{seconds:02d}")

        self.root.after(100, self._schedule_ui_poll)

    def close(self):
        self.running = False
        try:
            if self.ble_task_future:
                self.async_runner.submit(self._async_request_disconnect()).result(timeout=2.0)
        except Exception:
            pass
        try:
            self.async_runner.stop()
        except Exception:
            pass
        try:
            sys.stdout = self._stdout_original
            sys.stderr = self._stderr_original
        except Exception:
            pass
        self.root.quit()


def show_startup_error(title, text):
    if os.name == "nt":
        try:
            import ctypes

            ctypes.windll.user32.MessageBoxW(None, text, title, 0x10)
            return
        except Exception:
            pass
    print(f"{title}: {text}", file=sys.stderr)


def main():
    if tk is None:
        show_startup_error(
            "HR -> VRChat",
            f"Не удалось запустить интерфейс: tkinter недоступен.\n\nПодробности: {TK_IMPORT_ERROR}",
        )
        return

    root = tk.Tk()
    app = HRtoVRCApp(root)
    root.protocol("WM_DELETE_WINDOW", app.close)
    root.mainloop()


if __name__ == "__main__":
    main()
