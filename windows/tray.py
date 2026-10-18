"""Tray icon: shows agent status at a glance and the few actions a Windows operator needs.

Status comes from /healthz (counts only, no password). Service actions need admin, so they go through a
UAC prompt that runs C:\\YT-CUAN\\yt-cuan.ps1.
"""
import ctypes
import json
import threading
import time
import urllib.request
import webbrowser

import pystray
from PIL import Image, ImageDraw

# same meaning as the dashboard: red = on air, pine = running, amber = needs attention, grey = agent down
COLORS = {"air": (200, 55, 45), "ok": (35, 96, 77), "warn": (191, 128, 24), "down": (120, 130, 125)}


def icon_image(kind):
    img = Image.new("RGBA", (64, 64), (0, 0, 0, 0))
    d = ImageDraw.Draw(img)
    d.ellipse((4, 4, 60, 60), fill=(23, 35, 30))
    d.ellipse((16, 16, 48, 48), fill=COLORS[kind])
    return img


def read_status(port):
    try:
        with urllib.request.urlopen(f"http://127.0.0.1:{port}/healthz", timeout=3) as r:
            return json.load(r)
    except Exception:
        return None


def describe(h):
    if not h:
        return "down", "Agent tidak jalan"
    parts = [f"{h['live']} live", f"{h['keys_running']} key jalan"]
    if h["keys_error"]:
        parts.append(f"{h['keys_error']} key error")
    hub = h.get("hub") or {}
    parts.append("hub tersambung" if hub.get("connected") else f"hub: {hub.get('error') or 'belum tersambung'}")
    hub_down = not hub.get("connected") and hub.get("error") != "Belum dipasangkan ke hub"
    kind = "air" if h["live"] and not h["keys_error"] else "warn" if h["keys_error"] or hub_down else "ok"
    return kind, " · ".join(parts)


def admin_ps(home, arg):
    # runs elevated; the UAC prompt is the confirmation
    ctypes.windll.shell32.ShellExecuteW(
        None, "runas", "powershell.exe",
        f'-NoProfile -ExecutionPolicy Bypass -File "{home / "yt-cuan.ps1"}" {arg} -Pause', None, 1)


def run(home, port):
    state = {"text": "Memeriksa…", "kind": "down"}

    def status_text(_):
        return state["text"]

    menu = pystray.Menu(
        pystray.MenuItem(status_text, None, enabled=False),
        pystray.Menu.SEPARATOR,
        pystray.MenuItem("Buka dashboard", lambda: webbrowser.open(f"http://localhost:{port}"), default=True),
        pystray.MenuItem("Lihat password dashboard", lambda: admin_ps(home, "password")),
        pystray.Menu.SEPARATOR,
        pystray.MenuItem("Mulai ulang agent", lambda: admin_ps(home, "restart")),
        pystray.MenuItem("Perbarui ke versi terbaru", lambda: admin_ps(home, "update")),
        pystray.Menu.SEPARATOR,
        pystray.MenuItem("Tutup ikon (agent tetap jalan)", lambda icon: icon.stop()),
    )
    icon = pystray.Icon("yt-cuan", icon_image("down"), "YT-CUAN", menu)

    def poll():
        while True:
            kind, text = describe(read_status(port))
            if (kind, text) != (state["kind"], state["text"]):
                state.update(kind=kind, text=text)
                icon.icon = icon_image(kind)
                icon.title = f"YT-CUAN · {text}"[:127]
                icon.update_menu()
            time.sleep(5)

    threading.Thread(target=poll, daemon=True).start()
    icon.run()
