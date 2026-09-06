# HRtoVRC Studio

Трансляция пульса с BLE-датчика в VRChat по OSC — с живым дашбордом, ЭКГ-волной,
таймлайном сессии и архивом по дням.

![version](https://img.shields.io/badge/version-1.10.2-ff3b6b)
![python](https://img.shields.io/badge/python-3.12-blue)
![platform](https://img.shields.io/badge/platform-Windows-lightgrey)

- **Пульс в аватар** — значение уходит в OSC-параметр (целым числом или
  нормализованным `0.0..1.0`).
- **Пульс в чат** — настраиваемый шаблон сообщения, троттлинг, MIN/MAX,
  «смешные комментарии».
- **Живой дашборд** — ЭКГ-волна в такт реальному пульсу, график истории,
  зона нагрузки, MIN / AVG / MAX.
- **Таймлайн и архив** — что играло в VRChat, наложенное на график пульса,
  с сохранением по дням в `history.sqlite3`.
- **Автообновление** — программа сама проверяет GitHub Releases и умеет
  установить новую версию.

## Установка

Скачайте `HRtoVRC_Studio.exe` со страницы
[Releases](../../releases/latest) — это один самодостаточный файл, Python не нужен.
Положите его в любую папку и запустите.

При первом запуске: зайдите в «Настройки» → «Сканировать», выберите датчик,
нажмите «Сохранить настройки», затем START.

## Запуск из исходников

```powershell
python -m venv .venv
.venv\Scripts\pip install -r requirements.txt
.venv\Scripts\python studio\HRtoVRC_Studio.py
```

## Сборка .exe

```powershell
.venv\Scripts\pip install pyinstaller
cd studio
..\.venv\Scripts\pyinstaller --noconfirm --clean HRtoVRC_Studio.spec
```

Результат: `studio\dist\HRtoVRC_Studio.exe` — один портативный файл.

## Автообновление

При запуске программа обращается к
`https://api.github.com/repos/<OWNER>/<REPO>/releases/latest`, сравнивает
`tag_name` с `APP_VERSION` и, если версия новее, предлагает обновиться:
скачивает `.exe` из релиза, подменяет текущий файл и перезапускается.
Проверку можно выключить галочкой «Проверять обновления при запуске»
или запустить вручную кнопкой «Проверить обновления».

Репозиторий задаётся константой `GITHUB_REPO` в начале
[`studio/HRtoVRC_Studio.py`](studio/HRtoVRC_Studio.py). Вся логика обновления —
в [`studio/hr_updater.py`](studio/hr_updater.py), без сторонних зависимостей.

### Как выпустить новую версию

1. Поднимите `APP_VERSION` в `studio/HRtoVRC_Studio.py`.
2. Опишите изменения в `studio/README.md`.
3. Закоммитьте и поставьте тег той же версии:

   ```bash
   git tag v1.10.3 && git push origin main --tags
   ```

GitHub Actions соберёт `.exe`, проверит, что тег совпадает с `APP_VERSION`,
и приложит файл к релизу. Установленные у пользователей копии увидят его
при следующем запуске.

## Тесты

```powershell
python studio\tests\test_live_settings.py
python studio\tests\test_updater.py
```

Первый поднимает всё приложение с поддельным BLE-датчиком и перехватом OSC и
проверяет, что настройки применяются, не разрывая связь с датчиком. Второй
проверяет апдейтер, включая реальную подмену исполняемого файла.

## Структура

| Файл | Что это |
| --- | --- |
| `studio/HRtoVRC_Studio.py` | приложение: интерфейс, BLE-цикл, OSC, таймлайн, архив |
| `studio/hr_updater.py` | автообновление через GitHub Releases |
| `HRGUI10.py` | общий бэкенд: настройки, OSC-клиент, разбор BLE-пакетов |
| `studio/HRtoVRC_Studio.spec` | сборка одного портативного `.exe` |

Настройки и сохранённый датчик лежат рядом с `.exe` (`settings.json`,
`last_device.txt`), а если папка недоступна для записи — в `%APPDATA%\HRtoVRC`.

Автор: **_howl** · Discord: **howl64**
