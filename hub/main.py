"""YT-CUAN hub (PRD §13): users sign in with Google, pair VPS agents, monitor and fully control them.

Agents dial in over WebSocket (/agent) and push a state snapshot every few seconds; the hub forwards
operator actions as RPC. The hub never stores YouTube tokens or stream keys.
"""
import asyncio
import hashlib
import hmac
import json
import os
import secrets
import sqlite3
import threading
import time
from contextlib import asynccontextmanager
from datetime import datetime
from pathlib import Path
from urllib.parse import urlencode
from zoneinfo import ZoneInfo

from fastapi import FastAPI, HTTPException, Request, WebSocket, WebSocketDisconnect
from fastapi.responses import FileResponse, HTMLResponse, PlainTextResponse, RedirectResponse
from fastapi.staticfiles import StaticFiles
from fastapi.templating import Jinja2Templates
from google.auth.transport import requests as g_requests
from google.oauth2 import id_token

HERE = Path(__file__).parent
DATA_DIR = Path(os.environ.get("HUB_DATA_DIR", "./hub-data")).resolve()
HUB_URL = os.environ.get("HUB_URL", "http://localhost:9000").rstrip("/")
SECRET = os.environ.get("HUB_SECRET", "")
# Firebase Web App config (public values); sign-in = Google provider in Firebase Authentication
FIREBASE = {"apiKey": os.environ.get("FIREBASE_API_KEY", ""), "authDomain": os.environ.get("FIREBASE_AUTH_DOMAIN", ""),
            "projectId": os.environ.get("FIREBASE_PROJECT_ID", "")}
DEV_LOGIN = os.environ.get("HUB_DEV_LOGIN", "")  # email; local testing only, refused unless HUB_URL is localhost
AGENT_IMAGE = os.environ.get("AGENT_IMAGE", "ghcr.io/midev-id/yt-cuan-agent:latest")
INSTALL_SH = Path(os.environ.get("INSTALL_SH", HERE.parent / "install.sh"))
INSTALL_PS1 = Path(os.environ.get("INSTALL_PS1", HERE.parent / "windows" / "install.ps1"))
DIST_DIR = DATA_DIR / "dist"  # release files served at /download/<name> (Windows agent zip)
RELEASE_TOKEN = os.environ.get("RELEASE_TOKEN", "")  # CI uploads release files with this (PRD §13.9 H4)
RELEASE_FILES = {"yt-cuan-agent-win64.zip"}
STATIC_DIR = Path(os.environ.get("STATIC_DIR", HERE.parent / "app" / "static"))
RPC_TIMEOUT = 90  # channel.sync / oauth.token call YouTube from the agent
OFFLINE_AFTER = 45
SESSION_DAYS = 30
PAIR_MINUTES = 30
TZ = ZoneInfo(os.environ.get("TZ_DISPLAY", "Asia/Jakarta"))

DATA_DIR.mkdir(parents=True, exist_ok=True)

# ---------- db ----------
SCHEMA = """
CREATE TABLE IF NOT EXISTS users (id INTEGER PRIMARY KEY, email TEXT UNIQUE NOT NULL, name TEXT, created REAL);
CREATE TABLE IF NOT EXISTS agents (
  id INTEGER PRIMARY KEY, user_id INTEGER NOT NULL REFERENCES users(id), name TEXT, hostname TEXT,
  token_hash TEXT UNIQUE NOT NULL, version TEXT, created REAL, last_seen REAL, state TEXT);
CREATE TABLE IF NOT EXISTS pair_codes (code TEXT PRIMARY KEY, user_id INTEGER NOT NULL, expires REAL, agent_id INTEGER);
CREATE TABLE IF NOT EXISTS audit (id INTEGER PRIMARY KEY, ts REAL, user_id INTEGER, agent_id INTEGER, action TEXT, ok INTEGER, detail TEXT);
"""
_local = threading.local()


def conn():
    c = getattr(_local, "c", None)
    if c is None:
        c = sqlite3.connect(DATA_DIR / "hub.db", check_same_thread=False)
        c.row_factory = sqlite3.Row
        c.execute("PRAGMA journal_mode=WAL")
        c.execute("PRAGMA foreign_keys=ON")
        _local.c = c
    return c


def q(sql, *a):
    return conn().execute(sql, a).fetchall()


def one(sql, *a):
    return conn().execute(sql, a).fetchone()


def run(sql, *a):
    c = conn()
    cur = c.execute(sql, a)
    c.commit()
    return cur.lastrowid


def sha(s):
    return hashlib.sha256(s.encode()).hexdigest()


# ---------- live agent links ----------
links: dict[int, WebSocket] = {}
pending: dict[str, asyncio.Future] = {}
live_state: dict[int, dict] = {}  # latest snapshot per agent, also persisted (throttled) for offline view
_saved_at: dict[int, float] = {}


class AgentOffline(Exception):
    pass


async def rpc(agent_id, action, params=None):
    ws = links.get(agent_id)
    if not ws:
        raise AgentOffline("Server sedang offline. Perintah tidak terkirim.")
    rid = secrets.token_hex(8)
    fut = asyncio.get_running_loop().create_future()
    pending[rid] = fut
    try:
        await ws.send_text(json.dumps({"type": "rpc", "id": rid, "action": action, "params": params or {}}))
        return await asyncio.wait_for(fut, RPC_TIMEOUT)
    except asyncio.TimeoutError:
        raise AgentOffline("Server tidak membalas dalam 90 detik. Cek lagi sebentar.")
    finally:
        pending.pop(rid, None)


# ---------- app ----------
@asynccontextmanager
async def lifespan(app):
    conn().executescript(SCHEMA)
    if not SECRET:
        raise RuntimeError("HUB_SECRET wajib diisi")
    if DEV_LOGIN and not HUB_URL.startswith(("http://localhost", "http://127.0.0.1")):
        raise RuntimeError("HUB_DEV_LOGIN hanya untuk HUB_URL localhost")
    yield


app = FastAPI(lifespan=lifespan)
app.mount("/static", StaticFiles(directory=str(STATIC_DIR)), name="static")
templates = Jinja2Templates(directory=str(HERE / "templates"))
STATUS = {"running": "jalan", "degraded": "lambat", "reconnecting": "menyambung ulang", "starting": "memulai",
          "stopped": "berhenti", "error": "error", "creating": "disiapkan", "live": "live", "ending": "diakhiri",
          "complete": "selesai", "failed": "gagal", "queued": "antre", "processing": "diproses", "ready": "siap",
          "info": "info", "warn": "peringatan"}
templates.env.filters["st"] = lambda v: STATUS.get(v, v)
templates.env.filters["ts"] = lambda t: datetime.fromtimestamp(float(t), TZ).strftime("%d/%m %H:%M") if t else "-"


def ago(t):
    if not t:
        return "belum pernah"
    s = int(time.time() - t)
    return "baru saja" if s < 60 else f"{s // 60} menit lalu" if s < 3600 else f"{s // 3600} jam lalu" if s < 86400 else f"{s // 86400} hari lalu"


templates.env.filters["ago"] = ago


def back(url, msg=None, err=False):
    if msg:
        path, _, frag = url.partition("#")
        url = path + ("&" if "?" in path else "?") + urlencode({"msg": msg, **({"err": 1} if err else {})}) + (f"#{frag}" if frag else "")
    return RedirectResponse(url, status_code=303)


# ---------- session ----------
def sign(v):
    return hmac.new(SECRET.encode(), v.encode(), hashlib.sha256).hexdigest()


def set_session(resp, uid):
    exp = int(time.time()) + SESSION_DAYS * 86400
    v = f"{uid}.{exp}"
    resp.set_cookie("s", f"{v}.{sign(v)}", max_age=SESSION_DAYS * 86400, httponly=True, samesite="lax",
                    secure=HUB_URL.startswith("https://"))


def current_user(request: Request):
    try:
        uid, exp, sig = request.cookies.get("s", "").split(".")
        if hmac.compare_digest(sig, sign(f"{uid}.{exp}")) and int(exp) > time.time():
            return one("SELECT * FROM users WHERE id=?", int(uid))
    except ValueError:
        pass
    return None


def need_user(request):
    u = current_user(request)
    if not u:
        raise HTTPException(303, headers={"Location": "/login"})
    return u


def own_agent(user, agent_id):
    a = one("SELECT * FROM agents WHERE id=? AND user_id=?", agent_id, user["id"])
    if not a:
        raise HTTPException(404)
    return a


def agent_view(a):
    st = live_state.get(a["id"]) or (json.loads(a["state"]) if a["state"] else None)
    online = a["id"] in links and time.time() - (a["last_seen"] or 0) < OFFLINE_AFTER
    v = {"id": a["id"], "name": a["name"] or a["hostname"], "hostname": a["hostname"], "version": a["version"],
         "last_seen": a["last_seen"], "online": online, "state": st}
    if st:
        v["live"] = sum(1 for b in st["broadcasts"] if b["state"] == "live")
        v["problems"] = sum(1 for k in st["keys"] if k["state"] in ("error", "degraded", "reconnecting"))
        v["running"] = sum(1 for k in st["keys"] if k["state"] in ("running", "degraded"))
    return v


# ---------- sign-in (Firebase Auth, Google provider) ----------
def login_user(email, name):
    row = one("SELECT id FROM users WHERE email=?", email)
    uid = row["id"] if row else run("INSERT INTO users(email,name,created) VALUES(?,?,?)", email, name, time.time())
    resp = RedirectResponse("/", status_code=303)
    set_session(resp, uid)
    return resp


@app.get("/login", response_class=HTMLResponse)
def login_page(request: Request):
    return templates.TemplateResponse(request, "login.html", {
        "firebase": FIREBASE if FIREBASE["projectId"] else None, "dev": DEV_LOGIN})


@app.post("/auth/firebase")
async def auth_firebase(request: Request):
    form = await request.form()
    try:
        info = await asyncio.to_thread(id_token.verify_firebase_token, str(form.get("id_token", "")),
                                       g_requests.Request(), FIREBASE["projectId"])
    except ValueError:
        info = None
    if not info or not info.get("email") or not info.get("email_verified"):
        return back("/login", "Login gagal atau email Google belum terverifikasi. Coba lagi.", err=True)
    return login_user(info["email"].lower(), info.get("name"))


@app.post("/auth/dev")
def auth_dev():
    if not DEV_LOGIN:
        raise HTTPException(404)
    return login_user(DEV_LOGIN, "Dev")


@app.post("/logout")
def logout():
    resp = RedirectResponse("/login", status_code=303)
    resp.delete_cookie("s")
    return resp


# ---------- pages ----------
@app.get("/", response_class=HTMLResponse)
def home(request: Request):
    u = need_user(request)
    agents = [agent_view(a) for a in q("SELECT * FROM agents WHERE user_id=? ORDER BY name, hostname", u["id"])]
    if not agents:
        return RedirectResponse("/add")
    return templates.TemplateResponse(request, "home.html", {"user": u, "agents": agents, "nav": agents,
                                                              "latest": latest_version()})


def nav(u):
    return [agent_view(a) for a in q("SELECT * FROM agents WHERE user_id=? ORDER BY name, hostname", u["id"])]


@app.get("/add", response_class=HTMLResponse)
def add_page(request: Request, code: str = ""):
    u = need_user(request)
    row = one("SELECT * FROM pair_codes WHERE code=? AND user_id=?", code, u["id"]) if code else None
    if row and row["agent_id"]:
        return back(f"/v/{row['agent_id']}", "Server baru tersambung.")
    if not row or row["expires"] < time.time():
        code = "-".join(secrets.token_hex(2).upper() for _ in range(3))
        run("INSERT INTO pair_codes(code,user_id,expires) VALUES(?,?,?)", code, u["id"], time.time() + PAIR_MINUTES * 60)
        return RedirectResponse(f"/add?code={code}")
    return templates.TemplateResponse(request, "add.html", {
        "user": u, "nav": nav(u), "code": code, "minutes": int((row["expires"] - time.time()) / 60),
        "cmd": f"curl -fsSL {HUB_URL}/install | sudo bash -s -- {code}",
        "cmd_win": f"irm {HUB_URL}/install.ps1 | iex; Install-YtCuan {code}",
        "win_ready": (DIST_DIR / "yt-cuan-agent-win64.zip").exists()})


@app.get("/install", response_class=PlainTextResponse)
def install_script():
    s = INSTALL_SH.read_text(encoding="utf-8")
    s = s.replace("https://ytcuan.autopreneur.id", HUB_URL).replace("ghcr.io/midev-id/yt-cuan-agent:latest", AGENT_IMAGE)
    return PlainTextResponse(s, headers={"Cache-Control": "no-store"})


_latest = {"mtime": None, "v": None}


def latest_version():
    """Version inside the newest published Windows zip; CI publishes it on every v* tag."""
    import zipfile
    f = DIST_DIR / "yt-cuan-agent-win64.zip"
    if not f.exists():
        return None
    m = f.stat().st_mtime
    if _latest["mtime"] != m:
        with zipfile.ZipFile(f) as z:
            _latest.update(mtime=m, v=z.read("VERSION").decode().strip())
    return _latest["v"]


@app.get("/install.ps1", response_class=PlainTextResponse)
def install_ps1():
    s = INSTALL_PS1.read_text(encoding="utf-8").replace("https://ytcuan.autopreneur.id", HUB_URL)
    return PlainTextResponse(s, headers={"Cache-Control": "no-store"})


@app.get("/download/{name}")
def download(name: str):
    f = DIST_DIR / name
    if "/" in name or "\\" in name or name.startswith(".") or not f.is_file():
        raise HTTPException(404)
    # no-store: Cloudflare caches .zip for 4 hours by default, which would hand out an old release
    return FileResponse(f, filename=name, headers={"Cache-Control": "no-store"})


@app.put("/api/releases/{name}")
async def release_upload(request: Request, name: str):
    auth = request.headers.get("authorization", "")
    if not RELEASE_TOKEN or not hmac.compare_digest(auth, f"Bearer {RELEASE_TOKEN}"):
        raise HTTPException(401)
    if name not in RELEASE_FILES:
        raise HTTPException(404)
    DIST_DIR.mkdir(parents=True, exist_ok=True)
    part = DIST_DIR / f".{name}.part"
    size = 0
    with part.open("wb") as f:
        async for chunk in request.stream():
            size += len(chunk)
            if size > 200 * 1024 * 1024:
                part.unlink()
                raise HTTPException(413)
            f.write(chunk)
    part.replace(DIST_DIR / name)  # atomic: installers never download a half-written zip
    return {"ok": True, "name": name, "bytes": size}


@app.get("/v/{agent_id}", response_class=HTMLResponse)
async def vps_page(request: Request, agent_id: int, c: int | None = None):
    u = need_user(request)
    v = agent_view(own_agent(u, agent_id))
    st = v["state"] or {}
    channels = st.get("channels", [])
    ch = next((x for x in channels if x["id"] == c), channels[0] if channels else None)
    detail = None
    if ch and v["online"]:
        try:
            detail = await rpc(agent_id, "channel.detail", {"channel_id": ch["id"]})
            detail = detail["result"] if detail["ok"] else None
        except AgentOffline:
            pass
    return templates.TemplateResponse(request, "vps.html", {
        "user": u, "nav": nav(u), "v": v, "st": st, "ch": ch, "channels": channels, "detail": detail,
        "keys": [k for k in st.get("keys", []) if ch and k["channel_id"] == ch["id"]],
        "broadcasts": [b for b in st.get("broadcasts", []) if ch and b["channel_id"] == ch["id"]],
        "media": [m for m in st.get("media", []) if ch and m["channel_id"] == ch["id"]],
        "titles_n": (st.get("titles") or {}).get(str(ch["id"]) if ch else "", 0),
        "latest": latest_version(),
    })


@app.post("/v/{agent_id}/rename")
async def vps_rename(request: Request, agent_id: int):
    u = need_user(request)
    own_agent(u, agent_id)
    name = str((await request.form()).get("name", "")).strip()[:60]
    run("UPDATE agents SET name=? WHERE id=?", name or None, agent_id)
    return back(f"/v/{agent_id}", "Nama server disimpan.")


@app.post("/v/{agent_id}/remove")
async def vps_remove(request: Request, agent_id: int):
    u = need_user(request)
    own_agent(u, agent_id)
    ws = links.pop(agent_id, None)
    if ws:
        await ws.close(4401)
    run("DELETE FROM agents WHERE id=?", agent_id)
    return back("/", "Server dilepas dari hub. Live di server itu tetap jalan.")


# Form field -> param conversion per action. Everything else passes through as strings; the agent validates.
INT_FIELDS = {"channel_id", "key_id", "media_id", "broadcast_id", "n_video", "n_audio"}


@app.post("/v/{agent_id}/do/{action}")
async def vps_do(request: Request, agent_id: int, action: str):
    u = need_user(request)
    own_agent(u, agent_id)
    form = await request.form()
    to = str(form.get("_back") or f"/v/{agent_id}")
    if not to.startswith(f"/v/{agent_id}"):
        to = f"/v/{agent_id}"
    params = {}
    for k, v in form.multi_items():
        if k.startswith("_"):
            continue
        if hasattr(v, "read"):  # uploaded file: only client_secrets.json goes through the hub
            try:
                params["data"] = json.loads(await v.read())
            except ValueError:
                params["data"] = None
            continue
        params[k] = int(v) if k in INT_FIELDS and str(v).isdigit() else v
    if action == "channel.settings":
        st = live_state.get(agent_id) or {}
        ch = next((c for c in st.get("channels", []) if c["id"] == params.get("channel_id")), None)
        if not ch:
            return back(to, "Channel tidak ditemukan. Muat ulang halaman.", err=True)
        bools = [k for k, val in ch["settings"].items() if isinstance(val, bool)]
        params = {"channel_id": ch["id"], "settings": {
            **{k: v for k, v in params.items() if k in ch["settings"] and k not in bools},
            **{k: k in params for k in bools}}}
    if action == "key.config":
        params["locked"] = "locked" in params
    try:
        r = await rpc(agent_id, action, params)
    except AgentOffline as e:
        return back(to, str(e), err=True)
    run("INSERT INTO audit(ts,user_id,agent_id,action,ok,detail) VALUES(?,?,?,?,?,?)",
        time.time(), u["id"], agent_id, action, int(r["ok"]), (r.get("error") or "")[:300])
    if not r["ok"]:
        return back(to, r["error"], err=True)
    return back(to, (r.get("result") or {}).get("msg") or "Selesai.")


@app.get("/v/{agent_id}/log/{key_id}", response_class=PlainTextResponse)
async def vps_key_log(request: Request, agent_id: int, key_id: int):
    u = need_user(request)
    own_agent(u, agent_id)
    try:
        r = await rpc(agent_id, "key.log", {"key_id": key_id})
    except AgentOffline as e:
        return str(e)
    return (r.get("result") or {}).get("log") or r.get("error") or "(belum ada log)"


# ---------- agent API ----------
@app.post("/api/agents/pair")
async def api_pair(request: Request):
    body = await request.json()
    code = str(body.get("code", "")).strip().upper()
    row = one("SELECT * FROM pair_codes WHERE code=?", code)
    if not row or row["agent_id"] or row["expires"] < time.time():
        raise HTTPException(400, "Kode pairing salah, sudah dipakai, atau kedaluwarsa. Buat kode baru di hub.")
    token = secrets.token_urlsafe(32)
    hostname = str(body.get("hostname", ""))[:80]
    aid = run("INSERT INTO agents(user_id,hostname,token_hash,version,created) VALUES(?,?,?,?,?)",
              row["user_id"], hostname, sha(token), str(body.get("version", ""))[:40], time.time())
    run("UPDATE pair_codes SET agent_id=? WHERE code=?", aid, code)
    return {"token": token, "agent_id": aid, "name": hostname}


@app.websocket("/agent")
async def agent_ws(ws: WebSocket):
    auth = ws.headers.get("authorization", "")
    a = one("SELECT * FROM agents WHERE token_hash=?", sha(auth[7:])) if auth.startswith("Bearer ") else None
    if not a:
        await ws.close(4401)
        return
    await ws.accept()
    aid = a["id"]
    old = links.get(aid)
    links[aid] = ws
    if old:
        await old.close(4000)  # newer connection from the same agent wins
    run("UPDATE agents SET last_seen=?, version=? WHERE id=?", time.time(), ws.headers.get("x-agent-version", ""), aid)
    try:
        while True:
            msg = json.loads(await ws.receive_text())
            now = time.time()
            if msg.get("type") == "state":
                live_state[aid] = msg["data"]
                if now - _saved_at.get(aid, 0) > 60:
                    run("UPDATE agents SET last_seen=?, state=? WHERE id=?", now, json.dumps(msg["data"]), aid)
                    _saved_at[aid] = now
                else:
                    run("UPDATE agents SET last_seen=? WHERE id=?", now, aid)
            elif msg.get("type") == "rpc_result":
                fut = pending.get(msg.get("id"))
                if fut and not fut.done():
                    fut.set_result(msg)
    except (WebSocketDisconnect, RuntimeError):
        pass
    finally:
        if links.get(aid) is ws:
            links.pop(aid, None)


@app.get("/healthz")
def healthz():
    return {"ok": True, "agents_online": len(links), "agents": one("SELECT COUNT(*) n FROM agents")["n"]}
