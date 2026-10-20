# Weather Screensaver for Windows

A full-screen Windows screensaver that shows the date, time, current weather and the next hours' forecast on every connected monitor, and locks the PC when someone touches the mouse or keyboard.

> **AI-assisted project.** I built this with Claude as a hands-on exercise in AI-assisted development. I defined the requirements, tested each version on my own Windows setup, and drove the fixes; Claude wrote most of the code.

## What it does
- Large clock and date, current temperature, "feels like", precipitation and today's high/low
- Six-hour forecast with rain chance
- Covers all monitors; layout scales to each screen and shrinks text so nothing is cut off
- Optional: locks Windows when the screensaver closes (password required to get back in)
- Settings window to choose city, °F/°C and refresh interval

Weather data comes from [Open-Meteo](https://open-meteo.com), which is free and needs no API key.

## What I worked through
| Problem found in testing | Fix |
|---|---|
| "Certificate expired" error on some PCs | Use the up-to-date `certifi` certificate list instead of the Windows store |
| Text cut off or drifting on scaled / high-resolution displays | DPI awareness plus a fixed, bordered panel that auto-fits each monitor |
| Only one monitor covered | Enumerate every monitor and open a window on each |
| PC usable right after the screensaver closed | Lock the workstation on exit |

## Run it
Requires Python 3 on Windows.

```powershell
pip install certifi
python weather_saver.py /c    # settings: city, units, lock option
python weather_saver.py /s    # run full screen
```

To install as a real screensaver, build a `.scr` with PyInstaller:

```powershell
pip install pyinstaller certifi
pyinstaller --onefile --noconsole weather_saver.py
copy dist\weather_saver.exe weather_saver.scr
```

Then right-click `weather_saver.scr` → **Install**.

**Tech:** Python · Tkinter · Open-Meteo API · Windows API (ctypes)
