# HRtoVRC Live Studio (v1.10.2)

A **completely new application** for streaming a BLE heart-rate sensor to VRChat
over OSC. This is not the original tabbed app and not the sidebar "redesign" — it
is a new **live studio dashboard** built around real-time visualisation.

The original files and the `redesign/` build are left untouched. All BLE / OSC /
settings logic is reused unchanged from the original `HRGUI10` backend (imported
from the parent folder), so device behaviour matches the shipped app.

## What's new in the interface

- **Live dashboard** — the monitor, unified timeline and settings are reachable
  from the top command bar.
- **Animated ECG waveform** (`ECGWaveform`) — a synthetic P-QRS-T trace that scrolls
  and beats in time with the actual measured BPM.
- **Scrolling BPM history graph** (`HistoryGraph`) — the last ~2 minutes as a filled
  line chart with auto-scaled min/max axis. *(New feature — the old app never graphed
  history.)*
- **Heart-rate zone meter** (`ZoneBar`) — a colour gradient from «покой» to «максимум»
  with a live marker.
- **MIN / AVG / MAX tiles** — including a running **average BPM**. *(New stat.)*
- **Top command bar** with «Монитор» / «Таймлайн» / «Настройки», a connection pill
  and START/STOP.
- **Integrated Settings tab** (not a popup window) — switched in-place via the top-bar
  nav, with **device search** (scan + results list) as well as manual address entry,
  and correctly rendered spin-box arrows on the dark theme.
- **Collapsible log console** at the bottom (toggle with the «Журнал» button).

## New in v1.5

- Pulse history is now a zoomable timeline with a dedicated VRChat media lane.
- VRChat `output_log_*.txt` files are watched automatically, and detected player
  URLs / YouTube clips are embedded into the timeline.
- OSC output can switch between raw integer BPM and normalized float `0.0..1.0`
  using configurable minimum / maximum BPM values.

## New in v1.6

- Expanded Timeline mode opens a large synced timeline window with its own
  zoom/scroll controls, live session statistics, current VRChat media status,
  and a recent clips list.

## New in v1.7

- Pulse samples and VRChat clips are automatically saved to `history.sqlite3`.
- A new «Архив» page lets you pick any saved day and review the full-day pulse
  timeline, daily pulse stats, and detected dances/clips.

## New in v1.8

- «Архив» and Expanded Timeline are merged into one «Таймлайн» menu.
- The timeline menu has a live «Сейчас» mode plus saved days in the same selector.
- The big timeline view can be zoomed and scrolled in-place, with pulse stats and
  VRChat clips/dances shown next to the graph.

## Fixed in v1.8.1

- Fixed the timeline area fill so it follows the real pulse line instead of drawing
  a diagonal wedge across the chart.
- YouTube title detection now uses a faster fallback endpoint and updates the
  existing VRChat timeline event asynchronously when the title is found.

## Fixed in v1.8.2

- iwaSync/AVPro stop and end events now correctly clear the current playing clip.
- BDT dance URLs (`bdt.ac/dance/*.mp4`) are resolved through `bdt.ac/api/danceinfo`
  so the timeline shows the dance title instead of a hash.
- AVPro `Opening ...` lines are treated as playback start without creating duplicate
  YouTube `googlevideo` events.

## New in v1.9

- Timeline background now shows subtle heart-rate zones.
- Pulse lines are colored by zone intensity, with peak markers for active moments.
- Hovering the graph shows time, BPM and zone for the nearest sample.
- VRChat media bars use clearer source/status styling for YouTube, BDT and other clips.

## New in v1.10

- Ctrl + mouse wheel now zooms around the exact timeline point under the cursor.
- The timeline sidebar now includes «Сравнение дней» with compact daily pulse bars.
- Clicking a clip/dance on the media lane opens a «Выбранный клип» card with source,
  duration, URL and pulse stats for that clip segment.

## New & fixed in v1.10.2

- **Автообновление через GitHub Releases** (`hr_updater.py`). При запуске программа
  сравнивает свою версию с последним релизом и предлагает обновиться: скачивает
  `.exe`, показывает прогресс, подменяет текущий файл и перезапускается.
  В настройках — галочка «Проверять обновления при запуске» и кнопка
  «Проверить обновления». Репозиторий задаётся константой `GITHUB_REPO`.
- **«Сохранить настройки» now applies instantly, without stopping the pulse.**
  The BLE worker used to capture `osc_ip` / `osc_port` / `osc_param` / `send_chat` /
  `chat_throttle` / `chat_only_on_change` into local variables at connect time, so those
  six settings only took effect after a full STOP → START. They are now read from the
  live config on every heartbeat, and the OSC client is rebuilt automatically when the
  address or port changes.
- The chatbox loop is now always running (it checks `send_chat` itself), so chat output
  can be switched on **and** off mid-session; switching it off also clears the chatbox
  line instead of leaving the last message frozen in VRChat.
- The VRChat log watcher is restarted only when `vrc_log_dir` / `vrc_timeline_enabled`
  actually change, instead of on every save.
- Changing the sensor address and pressing save now reconnects to the new sensor by
  itself, and the device fields on the settings page stay in sync with the sensor that
  is actually connected (previously a stale manual-address field could be written back
  over the device you were connected to).
- Applied changes are listed in the log (`Настройки применены на лету: ...`).

## Fixed in v1.10.1

- Removed the low-value «События / клипы» side panel so «Сравнение дней» can use
  the remaining sidebar height.
- Moved the selected clip details into a copyable row below the graph; links open
  in the system browser and the row text can be selected/copied with Ctrl+C.
- Timeline summary pulse numbers now match the main dashboard colors: MIN is blue,
  MAX is red.

Every original capability is preserved: START/STOP, manual reconnect, the
"пульс = 0 на 10с" joke, OSC test, BLE scan, chat templates, MIN/MAX in chat, funny
comments, auto-start, auto-reconnect, and the stale-timeout watchdog.

## Code review fixes applied

- `format_chat_text` now substitutes `-` for `None` so templates using `{min_hr}` /
  `{max_hr}` no longer print the literal `None` before any beats arrive.
- Session statistics (min/max/avg/history) are fully reset on every START.
- `QFrame.NoShape` → `QFrame.Shape.NoFrame` (correct PySide6 enum).

## Run

```powershell
..\.buildvenv\Scripts\python.exe HRtoVRC_Studio.py
```

(or any Python with `PySide6`, `bleak`, `python-osc` installed).

Settings are shared with the original app (same `settings.json` / `last_device.txt`
in the project root), so your saved sensor and options carry over.

## Build a standalone .exe

```powershell
..\.buildvenv\Scripts\pyinstaller.exe --noconfirm --clean `
    --distpath ../dist --workpath ../build HRtoVRC_Studio_v1.10.1.spec
```

Result: **`dist/HRtoVRC_Studio_v1.10.1.exe`** — a single self-contained file (no Python
required). Because everything is packed into the one `.exe`, it can be moved or
copied anywhere on its own. (The spec is one-file on purpose: a one-folder build
shows *"Failed to load Python DLL"* if the `.exe` is separated from its `_internal`
folder.)
