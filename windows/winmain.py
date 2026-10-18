"""Entry point of the Windows build (yt-cuan-agent.exe).

  yt-cuan-agent.exe serve        run the agent (started at boot by the "YT-CUAN Agent" scheduled task)
  yt-cuan-agent.exe pair <KODE>  pair with the hub
  yt-cuan-agent.exe tray         tray icon for the logged-in user

Everything lives in C:\\YT-CUAN (override with YTCUAN_HOME): .env, data\\, ffmpeg\\, logs\\.
"""
import os
import sys
import time
import traceback
from pathlib import Path

HOME = Path(os.environ.get("YTCUAN_HOME", r"C:\YT-CUAN"))
PORT = 8000


def load_env():
    f = HOME / ".env"
    if f.exists():
        for line in f.read_text(encoding="utf-8").splitlines():
            if "=" in line and not line.lstrip().startswith("#"):
                k, v = line.split("=", 1)
                os.environ.setdefault(k.strip(), v.strip())
    os.environ.setdefault("DATA_DIR", str(HOME / "data"))
    for name in ("ffmpeg", "ffprobe"):
        exe = HOME / "ffmpeg" / f"{name}.exe"
        if exe.exists():
            os.environ.setdefault(f"{name.upper()}_BIN", str(exe))
    ver = HOME / "VERSION"
    if ver.exists():
        os.environ.setdefault("APP_VERSION", ver.read_text().strip())


def log_to_file():
    # windowed build: there is no console, so stdout/stderr go to a file (reset when it passes 10 MB)
    logs = HOME / "logs"
    logs.mkdir(parents=True, exist_ok=True)
    f = logs / "agent.log"
    if f.exists() and f.stat().st_size > 10 * 1024 * 1024:
        f.replace(logs / "agent.old.log")
    sys.stdout = sys.stderr = open(f, "a", encoding="utf-8", buffering=1)


def attach_console():
    # windowed exe has no stdout; borrow the console of the PowerShell that ran us so messages show up
    import ctypes
    if ctypes.windll.kernel32.AttachConsole(-1):
        sys.stdout = sys.stderr = open("CONOUT$", "w", encoding="utf-8", buffering=1)


def serve():
    log_to_file()
    import uvicorn
    from app.main import app
    while True:  # the scheduled task restarts us too, but a crash inside uvicorn shouldn't wait for that
        try:
            uvicorn.run(app, host="127.0.0.1", port=PORT, log_level="info")
            return
        except SystemExit:
            return
        except Exception:
            traceback.print_exc()
            time.sleep(5)


def main():
    load_env()
    cmd = sys.argv[1] if len(sys.argv) > 1 else "tray"
    if cmd == "serve":
        serve()
    elif cmd == "pair" and len(sys.argv) == 3:
        attach_console()
        from app import pair
        d = pair.pair(sys.argv[2])
        print(f"Terpasang ke hub sebagai \"{d.get('name')}\".")
    elif cmd == "tray":
        import tray
        tray.run(HOME, PORT)
    else:
        attach_console()
        print(__doc__)
        sys.exit(2)


if __name__ == "__main__":
    main()
