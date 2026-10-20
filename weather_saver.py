"""
Weather Screensaver for Windows  (version 2)
--------------------------------------------
Shows the date, time, current temperature, precipitation and the next hours'
forecast full screen on every monitor whenever the PC is idle.

What's new in version 2
  * Locks the PC (Windows lock screen) the moment anyone touches the mouse or
    keyboard, so nobody can use the PC while the weather is showing.
  * Fixed, centered layout inside a bordered panel with safe margins. Text
    sizes scale to each monitor and shrink automatically so nothing is cut off.
  * Large date and time at the top.
  * Covers all connected monitors.

Data: Open-Meteo (https://open-meteo.com) - free, no API key needed.

Windows calls a screensaver with these arguments:
    /s        -> run full screen
    /c        -> open settings (choose city, units, lock option)
    /p <hwnd> -> small preview in the Screen Saver dialog (ignored here)

Run directly for testing:   python weather_saver.py /s
Open settings:              python weather_saver.py /c
"""

import json
import os
import queue
import ssl
import sys
import threading
import tkinter as tk
import tkinter.font as tkfont
import urllib.parse
import urllib.request
from datetime import datetime
from tkinter import messagebox

IS_WINDOWS = sys.platform.startswith("win")

# ----------------------------------------------------------------- settings --
APP_DIR = os.path.join(os.environ.get("APPDATA", os.path.expanduser("~")), "WeatherSaver")
CONFIG_FILE = os.path.join(APP_DIR, "config.json")

DEFAULT_CONFIG = {
    "city": "New York",
    "units": "fahrenheit",       # "fahrenheit" or "celsius"
    "refresh_minutes": 15,
    "lock_on_exit": True,        # lock Windows when the screensaver closes
}

REFRESH_FALLBACK_SECONDS = 60     # retry sooner after a network error
MOUSE_MOVE_TOLERANCE = 10         # pixels of mouse jitter ignored before exiting
STARTUP_GRACE_MS = 1500           # ignore stray input right after starting
SAFE_HEIGHT = 0.86                # panel may use at most 86% of screen height
SAFE_WIDTH = 0.90                 # ... and 90% of screen width

WEATHER_CODES = {
    0: "Clear sky", 1: "Mainly clear", 2: "Partly cloudy", 3: "Overcast",
    45: "Fog", 48: "Freezing fog",
    51: "Light drizzle", 53: "Drizzle", 55: "Heavy drizzle",
    56: "Freezing drizzle", 57: "Freezing drizzle",
    61: "Light rain", 63: "Rain", 65: "Heavy rain",
    66: "Freezing rain", 67: "Freezing rain",
    71: "Light snow", 73: "Snow", 75: "Heavy snow", 77: "Snow grains",
    80: "Rain showers", 81: "Rain showers", 82: "Violent rain showers",
    85: "Snow showers", 86: "Heavy snow showers",
    95: "Thunderstorm", 96: "Thunderstorm with hail", 99: "Thunderstorm with hail",
}

WEATHER_ICONS = {
    0: "☀", 1: "🌤", 2: "⛅", 3: "☁", 45: "🌫", 48: "🌫",
    51: "🌦", 53: "🌦", 55: "🌧", 56: "🌧", 57: "🌧",
    61: "🌦", 63: "🌧", 65: "🌧", 66: "🌧", 67: "🌧",
    71: "🌨", 73: "🌨", 75: "❄", 77: "❄", 80: "🌦", 81: "🌧", 82: "⛈",
    85: "🌨", 86: "❄", 95: "⛈", 96: "⛈", 99: "⛈",
}


def load_config():
    config = dict(DEFAULT_CONFIG)
    try:
        with open(CONFIG_FILE, "r", encoding="utf-8") as f:
            config.update(json.load(f))
    except (OSError, ValueError):
        pass
    return config


def save_config(config):
    os.makedirs(APP_DIR, exist_ok=True)
    with open(CONFIG_FILE, "w", encoding="utf-8") as f:
        json.dump(config, f, indent=2)


# --------------------------------------------------------- Windows helpers --
def enable_dpi_awareness():
    """Without this, Windows reports scaled screen sizes and the layout can
    spill off the top/bottom of high-resolution or scaled displays."""
    if not IS_WINDOWS:
        return
    import ctypes
    try:
        ctypes.windll.shcore.SetProcessDpiAwareness(2)   # per-monitor aware
    except Exception:
        try:
            ctypes.windll.user32.SetProcessDPIAware()
        except Exception:
            pass


def get_monitors():
    """Return a list of (x, y, width, height) for every connected monitor."""
    if not IS_WINDOWS:
        return None
    try:
        import ctypes
        from ctypes import wintypes
        monitors = []
        proc_type = ctypes.WINFUNCTYPE(ctypes.c_int, wintypes.HMONITOR, wintypes.HDC,
                                       ctypes.POINTER(wintypes.RECT), wintypes.LPARAM)

        def callback(hmon, hdc, rect, lparam):
            r = rect.contents
            monitors.append((r.left, r.top, r.right - r.left, r.bottom - r.top))
            return 1

        ctypes.windll.user32.EnumDisplayMonitors(None, None, proc_type(callback), 0)
        return monitors or None
    except Exception:
        return None


def lock_workstation():
    if IS_WINDOWS:
        import ctypes
        ctypes.windll.user32.LockWorkStation()


# ------------------------------------------------------------- weather API --
def make_ssl_context():
    """Use the up-to-date certificate list from the 'certifi' package, so the
    screensaver doesn't depend on an outdated Windows certificate store.
    Certificate checking stays fully enabled."""
    try:
        import certifi
        return ssl.create_default_context(cafile=certifi.where())
    except ImportError:
        return ssl.create_default_context()


SSL_CONTEXT = make_ssl_context()


def http_json(url, params):
    full_url = url + "?" + urllib.parse.urlencode(params)
    req = urllib.request.Request(full_url, headers={"User-Agent": "WeatherSaver/2.0"})
    with urllib.request.urlopen(req, timeout=15, context=SSL_CONTEXT) as resp:
        return json.loads(resp.read().decode("utf-8"))


def geocode(city):
    """Turn a city name into (display name, latitude, longitude)."""
    data = http_json(
        "https://geocoding-api.open-meteo.com/v1/search",
        {"name": city, "count": 1, "language": "en", "format": "json"},
    )
    results = data.get("results") or []
    if not results:
        raise ValueError(f"Location not found: {city}")
    r = results[0]
    parts = [r.get("name"), r.get("admin1"), r.get("country_code")]
    label = ", ".join(p for p in parts if p)
    return label, r["latitude"], r["longitude"]


def fetch_weather(lat, lon, units):
    precip_unit = "inch" if units == "fahrenheit" else "mm"
    return http_json(
        "https://api.open-meteo.com/v1/forecast",
        {
            "latitude": lat,
            "longitude": lon,
            "current": "temperature_2m,apparent_temperature,precipitation,weather_code",
            "hourly": "temperature_2m,precipitation_probability,precipitation,weather_code",
            "daily": "temperature_2m_max,temperature_2m_min,precipitation_sum,"
                     "precipitation_probability_max",
            "temperature_unit": units,
            "precipitation_unit": precip_unit,
            "timezone": "auto",
            "forecast_days": 2,
        },
    )


def summarize(label, data, units):
    """Convert the raw API response into the text shown on screen."""
    t_unit = "°F" if units == "fahrenheit" else "°C"
    p_unit = "in" if units == "fahrenheit" else "mm"
    cur, daily, hourly = data["current"], data["daily"], data["hourly"]
    code = cur.get("weather_code", 0)

    # The next 6 hours, starting after the current hour
    now_key = cur["time"][:13]  # "YYYY-MM-DDTHH"
    start = next((i for i, t in enumerate(hourly["time"]) if t[:13] >= now_key), 0)
    upcoming = []
    for i in range(start + 1, min(start + 7, len(hourly["time"]))):
        hour = datetime.fromisoformat(hourly["time"][i]).strftime("%I %p").lstrip("0")
        upcoming.append({
            "hour": hour,
            "icon": WEATHER_ICONS.get(hourly["weather_code"][i], "·"),
            "temp": f"{round(hourly['temperature_2m'][i])}°",
            "pop": f"{hourly['precipitation_probability'][i] or 0}%",
        })

    return {
        "location": label,
        "icon": WEATHER_ICONS.get(code, ""),
        "condition": WEATHER_CODES.get(code, "—"),
        "temp": f"{round(cur['temperature_2m'])}{t_unit}",
        "feels": f"Feels like {round(cur['apparent_temperature'])}{t_unit}",
        "precip_now": f"Precipitation now: {cur.get('precipitation', 0):g} {p_unit}",
        "today": (
            f"Today  High {round(daily['temperature_2m_max'][0])}{t_unit}  ·  "
            f"Low {round(daily['temperature_2m_min'][0])}{t_unit}  ·  "
            f"Rain chance {daily['precipitation_probability_max'][0] or 0}%  ·  "
            f"Total {daily['precipitation_sum'][0] or 0:g} {p_unit}"
        ),
        "hours": upcoming,
        "updated": "Weather updated " + datetime.now().strftime("%I:%M %p").lstrip("0"),
    }


# ------------------------------------------------------------ one monitor --
BG = "#0b1220"
PANEL = "#111a2c"
BORDER = "#2c3a55"
FG = "#f2f5fa"
DIM = "#8a97ad"
ACCENT = "#7cc4ff"

# Font sizes in pixels for a 1080-pixel-high screen; scaled per monitor.
BASE_SIZES = {
    "time": 96, "date": 32, "location": 28, "temp": 104, "condition": 32,
    "small": 22, "precip": 24, "today": 22, "hour": 18, "hour_icon": 30,
    "hour_temp": 24, "hour_pop": 17, "status": 15,
}
FONT_FAMILIES = {
    "time": "Segoe UI Light", "temp": "Segoe UI Light", "hour_icon": "Segoe UI Emoji",
}


class MonitorView:
    """The weather panel shown on one monitor."""

    def __init__(self, window, width, height):
        self.window = window
        self.width, self.height = width, height
        self.scale = min(height / 1080, width / 1920) *1.8
        self.fonts = {
            name: tkfont.Font(family=FONT_FAMILIES.get(name, "Segoe UI"), size=-1)
            for name in BASE_SIZES
        }
        self._apply_font_sizes()

        window.configure(bg=BG)
        # Bordered panel, fixed in the center of the screen
        self.panel = tk.Frame(window, bg=PANEL, highlightthickness=2,
                              highlightbackground=BORDER, highlightcolor=BORDER)
        self.panel.place(relx=0.5, rely=0.5, anchor="center")
        self.inner = tk.Frame(self.panel, bg=PANEL)
        # self.inner.pack(padx=self._px(60), pady=self._px(36))
        self.inner.pack(padx=self._px(10), pady=self._px(16))

        self.time = self._label("time", FG, pady=0)
        self.date = self._label("date", DIM, pady=(0, 18))
        tk.Frame(self.inner, bg=BORDER, height=2).pack(fill="x", pady=(0, self._px(18)))
        self.location = self._label("location", ACCENT)
        self.main = self._label("temp", FG, pady=0)
        self.condition = self._label("condition", FG)
        self.feels = self._label("small", DIM)
        self.precip = self._label("precip", ACCENT)
        self.today = self._label("today", FG, pady=(14, 8))
        self.hours_frame = tk.Frame(self.inner, bg=PANEL)
        self.hours_frame.pack(pady=self._px(8))
        self.status = self._label("status", DIM, pady=(14, 0))
        self.status.config(wraplength=int(width * 0.75))

        self.location.config(text="Loading weather…")

    def _px(self, value):
        return max(1, int(value * self.scale))

    def _apply_font_sizes(self):
        for name, base in BASE_SIZES.items():
            self.fonts[name].configure(size=-max(8, int(base * self.scale)))

    def _label(self, font_name, color, pady=4):
        if isinstance(pady, tuple):
            pady = tuple(self._px(p) for p in pady)
        else:
            pady = self._px(pady)
        lbl = tk.Label(self.inner, font=self.fonts[font_name], fg=color, bg=PANEL)
        lbl.pack(pady=pady)
        return lbl

    def set_clock(self, now):
        self.time.config(text=now.strftime("%I:%M %p").lstrip("0"))
        self.date.config(text=now.strftime("%A, %B %d, %Y").replace(" 0", " "))

    def show(self, info):
        self.location.config(text=info["location"])
        self.main.config(text=f"{info['icon']} {info['temp']}")
        self.condition.config(text=info["condition"])
        self.feels.config(text=info["feels"])
        self.precip.config(text=info["precip_now"])
        self.today.config(text=info["today"])
        self.status.config(text=info["updated"], fg=DIM)

        for child in self.hours_frame.winfo_children():
            child.destroy()
        for col, h in enumerate(info["hours"]):
            cell = tk.Frame(self.hours_frame, bg=PANEL, padx=self._px(20))
            cell.grid(row=0, column=col)
            for text, font_name, color in (
                (h["hour"], "hour", DIM),
                (h["icon"], "hour_icon", FG),
                (h["temp"], "hour_temp", FG),
                ("💧 " + h["pop"], "hour_pop", ACCENT),
            ):
                tk.Label(cell, text=text, font=self.fonts[font_name], fg=color, bg=PANEL).pack()
        self.fit()

    def show_error(self, message):
        self.status.config(text=f"Could not update weather ({message}). Retrying shortly…",
                           fg="#ffb4a8")
        self.fit()

    def fit(self):
        """Shrink text until the panel fits inside the safe area of the screen."""
        for _ in range(8):
            self.window.update_idletasks()
            need_h = self.panel.winfo_reqheight() / (self.height * SAFE_HEIGHT)
            need_w = self.panel.winfo_reqwidth() / (self.width * SAFE_WIDTH)
            overflow = max(need_h, need_w)
            if overflow <= 1.0:
                return
            self.scale *= 0.95 / overflow
            self._apply_font_sizes()


# -------------------------------------------------------------- screensaver --
class WeatherScreensaver:
    def __init__(self, config):
        self.config = config
        self.closing = False
        self.ready_for_input = False
        self.start_mouse = None
        self.results = queue.Queue()

        self.root = tk.Tk()
        self.root.withdraw()
        monitors = get_monitors() or [
            (0, 0, self.root.winfo_screenwidth(), self.root.winfo_screenheight())]

        self.windows, self.views = [], []
        for x, y, w, h in monitors:
            win = tk.Toplevel(self.root)
            win.overrideredirect(True)                    # no title bar / borders
            win.geometry(f"{w}x{h}{x:+d}{y:+d}")           # cover this monitor exactly
            win.attributes("-topmost", True)
            win.configure(cursor="none")
            self.windows.append(win)
            self.views.append(MonitorView(win, w, h))

        # Any input closes the screensaver (and locks the PC)
        for ev in ("<KeyPress>", "<Button>", "<MouseWheel>"):
            self.root.bind_all(ev, lambda e: self.quit())
        self.root.bind_all("<Motion>", self._on_motion)
        for win in self.windows:
            win.bind("<FocusOut>", self._on_focus_out)

        self.root.after(200, self._grab_focus)
        self.root.after(STARTUP_GRACE_MS, self._enable_input)
        self._tick_clock()
        self._poll_results()
        self._refresh()

    # ---- input / focus
    def _grab_focus(self):
        if self.windows:
            self.windows[0].focus_force()

    def _enable_input(self):
        self.ready_for_input = True
        self._grab_focus()

    def _on_motion(self, event):
        if not self.ready_for_input:
            return
        pos = (event.x_root, event.y_root)
        if self.start_mouse is None:
            self.start_mouse = pos
        elif (abs(pos[0] - self.start_mouse[0]) > MOUSE_MOVE_TOLERANCE or
              abs(pos[1] - self.start_mouse[1]) > MOUSE_MOVE_TOLERANCE):
            self.quit()

    def _on_focus_out(self, event):
        # If another program takes focus (Alt+Tab, Win key, etc.), close and lock
        if self.ready_for_input:
            self.root.after(150, self._check_focus)

    def _check_focus(self):
        if not self.closing and self.root.focus_get() is None:
            self.quit()

    # ---- clock and weather
    def _tick_clock(self):
        now = datetime.now()
        for view in self.views:
            view.set_clock(now)
        self.root.after(1000, self._tick_clock)

    def _refresh(self):
        threading.Thread(target=self._fetch_in_background, daemon=True).start()

    def _fetch_in_background(self):
        try:
            label, lat, lon = geocode(self.config["city"])
            data = fetch_weather(lat, lon, self.config["units"])
            self.results.put(("ok", summarize(label, data, self.config["units"])))
        except Exception as exc:  # network down, bad city, etc.
            self.results.put(("error", str(exc)))

    def _poll_results(self):
        try:
            while True:
                kind, payload = self.results.get_nowait()
                for view in self.views:
                    (view.show if kind == "ok" else view.show_error)(payload)
                delay = (int(self.config.get("refresh_minutes", 15)) * 60
                         if kind == "ok" else REFRESH_FALLBACK_SECONDS)
                self.root.after(delay * 1000, self._refresh)
        except queue.Empty:
            pass
        self.root.after(250, self._poll_results)

    # ---- exit
    def quit(self):
        if self.closing:
            return
        self.closing = True
        self.root.destroy()

    def run(self):
        self.root.mainloop()
        if self.config.get("lock_on_exit", True):
            lock_workstation()


# ----------------------------------------------------------------- settings --
def open_settings():
    config = load_config()
    win = tk.Tk()
    win.title("Weather Screensaver Settings")
    win.resizable(False, False)

    tk.Label(win, text="City or town:").grid(row=0, column=0, sticky="w", padx=12, pady=(14, 4))
    city_var = tk.StringVar(value=config["city"])
    tk.Entry(win, textvariable=city_var, width=32).grid(row=0, column=1, padx=12, pady=(14, 4))

    tk.Label(win, text="Units:").grid(row=1, column=0, sticky="w", padx=12, pady=4)
    units_var = tk.StringVar(value=config["units"])
    units = tk.Frame(win)
    units.grid(row=1, column=1, sticky="w", padx=12)
    tk.Radiobutton(units, text="°F / inches", variable=units_var, value="fahrenheit").pack(side="left")
    tk.Radiobutton(units, text="°C / mm", variable=units_var, value="celsius").pack(side="left")

    tk.Label(win, text="Refresh every (minutes):").grid(row=2, column=0, sticky="w", padx=12, pady=4)
    refresh_var = tk.StringVar(value=str(config["refresh_minutes"]))
    tk.Spinbox(win, from_=5, to=120, textvariable=refresh_var, width=6).grid(
        row=2, column=1, sticky="w", padx=12, pady=4)

    lock_var = tk.BooleanVar(value=bool(config.get("lock_on_exit", True)))
    tk.Checkbutton(win, text="Lock the PC when the screensaver closes (password required)",
                   variable=lock_var).grid(row=3, column=0, columnspan=2, sticky="w",
                                           padx=12, pady=(8, 4))

    def on_save():
        city = city_var.get().strip()
        try:
            label, _, _ = geocode(city)
        except Exception as exc:
            messagebox.showerror("Location", f"Couldn't find that location.\n\n{exc}")
            return
        try:
            minutes = max(5, int(refresh_var.get()))
        except ValueError:
            minutes = 15
        save_config({"city": city, "units": units_var.get(),
                     "refresh_minutes": minutes, "lock_on_exit": lock_var.get()})
        messagebox.showinfo("Saved", f"Weather will show for:\n{label}")
        win.destroy()

    buttons = tk.Frame(win)
    buttons.grid(row=4, column=0, columnspan=2, pady=14)
    tk.Button(buttons, text="Save", width=10, command=on_save).pack(side="left", padx=6)
    tk.Button(buttons, text="Cancel", width=10, command=win.destroy).pack(side="left", padx=6)
    win.mainloop()


# --------------------------------------------------------------------- main --
def main():
    enable_dpi_awareness()
    arg = sys.argv[1].lower() if len(sys.argv) > 1 else "/c"
    mode = arg[:2]  # Windows may pass "/c:1234" or "/p 1234"
    if mode == "/s":
        WeatherScreensaver(load_config()).run()
    elif mode == "/p":
        return  # preview pane in Windows dialog: not supported, exit quietly
    else:
        open_settings()


if __name__ == "__main__":
    main()
