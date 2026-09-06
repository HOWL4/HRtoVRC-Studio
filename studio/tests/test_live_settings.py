# -*- coding: utf-8 -*-
"""Integration smoke test: settings must apply while the sensor stays connected.

Runs HRtoVRC Studio headless (offscreen Qt) with BLE replaced by a fake HR device
and OSC replaced by a recorder, then changes settings mid-session exactly the way
the «Сохранить настройки» button does.
"""
import os
import sys
import tempfile
import time

os.environ["QT_QPA_PLATFORM"] = "offscreen"

STUDIO = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, STUDIO)

import HRtoVRC_Studio as S  # noqa: E402

# GitHub runners give us a cp1252 stdout; the Russian strings in this output
# must not be what makes a test run fail.
try:
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")
except Exception:
    pass

FAILURES = []
CHECKS = []


def check(name, ok, detail=""):
    CHECKS.append((name, ok, detail))
    if not ok:
        FAILURES.append(name)
    print(("  OK   " if ok else "  FAIL ") + name + ((" -> " + detail) if detail else ""))


# ---------------------------------------------------------------- fake OSC ---
SENT = []


class FakeOSC:
    def __init__(self, address, port):
        self.address, self.port = address, port

    def send_message(self, param, value):
        SENT.append((self.address, self.port, param, value))


def chat_msgs():
    return [v for _, _, p, v in SENT if p == "/chatbox/input"]


def hr_msgs():
    return [s for s in SENT if s[2] != "/chatbox/input"]


# ---------------------------------------------------------------- fake BLE ---
class FakeChar:
    def __init__(self, uuid):
        self.uuid = uuid


class FakeService:
    def __init__(self):
        self.characteristics = [FakeChar(S.HR_CHAR_UUID)]


class FakeClient:
    current = None
    connects = []

    def __init__(self, address, timeout=None):
        self.address = address
        self.is_connected = False
        self.services = [FakeService()]
        self.cb = None

    async def __aenter__(self):
        self.is_connected = True
        FakeClient.current = self
        FakeClient.connects.append(self.address)
        return self

    async def __aexit__(self, *exc):
        self.is_connected = False
        return False

    async def start_notify(self, uuid, cb):
        self.cb = cb

    async def stop_notify(self, uuid):
        self.cb = None

    async def disconnect(self):
        self.is_connected = False


DEV1 = "AA:BB:CC:DD:EE:01"
DEV2 = "AA:BB:CC:DD:EE:02"

BASE_CFG = dict(S.DEFAULTS)
BASE_CFG.update(
    osc_ip="127.0.0.1", osc_port=9000, osc_param="/avatar/parameters/HR",
    osc_value_mode="int", send_chat=False, chat_throttle=0.0,
    chat_only_on_change=False, chat_template="HR {hr}",
    last_device=DEV1, auto_start=False, auto_reconnect=True,
    reconnect_delay=0.5, stale_timeout=999.0, vrc_timeline_enabled=False,
)

SAVED = []
S.SimpleUDPClient = FakeOSC
S.BleakClient = FakeClient
S.load_settings = lambda: dict(BASE_CFG)
S.load_last_device = lambda: DEV1
S.save_settings = lambda cfg: (SAVED.append(dict(cfg)) or (True, None))
S.save_last_device = lambda addr: (True, None)
S.history_db_file = lambda: os.path.join(tempfile.mkdtemp(prefix="hrtest_"), "history.sqlite3")

from PySide6.QtWidgets import QApplication  # noqa: E402

app = QApplication([])
win = S.MainWindow()


def pump():
    app.processEvents()


def beat(bpm=75):
    """Deliver one HR notification through the real notify callback."""
    client = FakeClient.current
    if client is None or client.cb is None:
        return False
    client.cb(None, bytes([0x00, bpm]))
    return True


def wait_for(pred, timeout=6.0, gap=0.03):
    end = time.time() + timeout
    while time.time() < end:
        pump()
        if pred():
            return True
        time.sleep(gap)
    pump()
    return pred()


def settle(seconds=0.8):
    end = time.time() + seconds
    while time.time() < end:
        pump()
        time.sleep(0.03)


print("\n=== 1. session start ===")
win.toggle_start()
check("BLE connected", wait_for(lambda: FakeClient.current is not None and FakeClient.current.cb is not None))
check("connected to the selected device", FakeClient.connects[-1] == DEV1, str(FakeClient.connects))
check("device widgets follow the live sensor",
      wait_for(lambda: win.manual_addr.text().strip() == DEV1), win.manual_addr.text())

SENT.clear()
beat(70)
check("baseline ip/port/param",
      wait_for(lambda: SENT) and SENT[-1][:3] == ("127.0.0.1", 9000, "/avatar/parameters/HR"),
      str(SENT[-1]) if SENT else "nothing sent")

print("\n=== 2. settings changed mid-session (direct cfg, as the worker sees it) ===")
with win.cfg_lock:
    win.cfg.update(osc_ip="127.0.0.2", osc_port=9101, osc_param="/avatar/parameters/Pulse")
SENT.clear()
beat(71)
check("new ip/port/param applied without restart",
      wait_for(lambda: SENT) and SENT[-1][:3] == ("127.0.0.2", 9101, "/avatar/parameters/Pulse"),
      str(SENT[-1]) if SENT else "nothing sent")
check("connection was NOT dropped", FakeClient.current.is_connected)

with win.cfg_lock:
    win.cfg.update(osc_value_mode="float", osc_float_min=0.0, osc_float_max=200.0)
SENT.clear()
beat(100)
check("float value mode applied live",
      wait_for(lambda: SENT) and isinstance(SENT[-1][3], float) and abs(SENT[-1][3] - 0.5) < 0.01,
      str(SENT[-1]) if SENT else "nothing sent")
with win.cfg_lock:
    win.cfg["osc_value_mode"] = "int"

with win.cfg_lock:
    win.cfg.update(send_chat=True, chat_throttle=0.0, chat_only_on_change=False)
SENT.clear()
beat(80)
chat_on = wait_for(lambda: chat_msgs(), timeout=4.0)
check("chat switched ON mid-session", bool(chat_on), str(chat_msgs()[:1]))
check("chat uses the new endpoint too",
      all(s[:2] == ("127.0.0.2", 9101) for s in SENT if s[2] == "/chatbox/input"))

with win.cfg_lock:
    win.cfg["chat_template"] = "PULSE={hr}"
SENT.clear()
beat(81)
check("chat template applied live",
      wait_for(lambda: any(str(v[0]).startswith("PULSE=") for v in chat_msgs()), timeout=4.0),
      str(chat_msgs()[:1]))

with win.cfg_lock:
    win.cfg["send_chat"] = False
settle(0.6)
SENT.clear()
beat(82)
settle(0.8)
check("chat switched OFF mid-session", not chat_msgs(), str(chat_msgs()[:2]))
check("HR keeps flowing after chat off", bool(hr_msgs()))

print("\n=== 3. the real «Сохранить настройки» button ===")
before_connects = len(FakeClient.connects)
SAVED.clear()
win.ip_edit.setText("127.0.0.9")
win.port_edit.setValue(9300)
win.param_edit.setText("/avatar/parameters/Beat")
win.send_chat.setChecked(True)
win.template_edit.setText("BTN {hr}")
win.throttle.setValue(0.0)
win.only_change.setChecked(False)
win.save_settings_gui()
settle(0.4)

check("button persisted the settings", bool(SAVED))
check("button did not stop the session", win.running and FakeClient.current.is_connected)
check("button did NOT cause a spurious reconnect",
      len(FakeClient.connects) == before_connects, str(FakeClient.connects))

SENT.clear()
beat(90)
check("button change reached OSC immediately",
      wait_for(lambda: hr_msgs()) and hr_msgs()[-1][:3] == ("127.0.0.9", 9300, "/avatar/parameters/Beat"),
      str(hr_msgs()[-1]) if hr_msgs() else "nothing sent")
check("button re-enabled chat immediately",
      wait_for(lambda: any(str(v[0]).startswith("BTN ") for v in chat_msgs()), timeout=4.0),
      str(chat_msgs()[:1]))

print("\n=== 4. saving twice in a row is a no-op for the connection ===")
before_connects = len(FakeClient.connects)
win.save_settings_gui()
settle(0.6)
check("second save keeps the same connection",
      len(FakeClient.connects) == before_connects and FakeClient.current.is_connected,
      str(FakeClient.connects))

print("\n=== 5. switching the sensor from the settings page ===")
win.manual_addr.setText(DEV2)
win.save_settings_gui()
check("reconnects to the new sensor address",
      wait_for(lambda: DEV2 in FakeClient.connects, timeout=8.0), str(FakeClient.connects))
check("session still running", win.running)
check("widgets show the new sensor", win.manual_addr.text().strip() == DEV2, win.manual_addr.text())

SENT.clear()
check("HR flows again from the new sensor", wait_for(lambda: beat(60) and hr_msgs(), timeout=5.0),
      str(hr_msgs()[-1]) if hr_msgs() else "nothing sent")

print("\n=== 6. apply_settings_live bookkeeping ===")
before = win.cfg_snapshot()
with win.cfg_lock:
    win.cfg["osc_port"] = 9200
notes = win.apply_settings_live(before)
check("reports the changed endpoint", "9200" in notes, notes)
check("quiet when nothing changed", win.apply_settings_live(win.cfg_snapshot()) == "")

win.running = False
win.async_runner.submit(win.async_request_disconnect())
settle(0.5)
try:
    win.stop_vrc_watcher()
    win.history_store.close()
    win.async_runner.stop()
except Exception:
    pass

print("\n" + "=" * 62)
print("passed %d/%d" % (len(CHECKS) - len(FAILURES), len(CHECKS)))
if FAILURES:
    print("FAILED: " + "; ".join(FAILURES))
os._exit(1 if FAILURES else 0)
