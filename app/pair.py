"""Exchange a one-time pairing code from the hub for a permanent agent token (PRD §13.2).

Run: python -m app.pair <CODE>
"""
import socket
import sys

import requests

from . import config


def pair(code):
    try:
        r = requests.post(f"{config.HUB_URL}/api/agents/pair", timeout=20,
                          json={"code": code.strip(), "hostname": socket.gethostname(), "version": config.VERSION})
    except requests.RequestException as e:
        raise SystemExit(f"Hub {config.HUB_URL} tidak bisa dihubungi ({type(e).__name__}). Cek koneksi internet VPS.")
    if r.status_code != 200:
        detail = r.json().get("detail") if r.headers.get("content-type", "").startswith("application/json") else r.text
        raise SystemExit(f"Pairing gagal ({r.status_code}): {detail or 'kode salah atau kedaluwarsa'}")
    data = r.json()
    config.AGENT_TOKEN_FILE.write_text(data["token"])
    config.AGENT_TOKEN_FILE.chmod(0o600)
    return data


if __name__ == "__main__":
    if len(sys.argv) != 2:
        raise SystemExit("Pakai: python -m app.pair <KODE>")
    d = pair(sys.argv[1])
    print(f"Terpasang ke hub sebagai \"{d.get('name', socket.gethostname())}\".")
