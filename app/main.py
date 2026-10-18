import asyncio
import json
from urllib.parse import urlencode
import pickle
import secrets
import shutil
import time
from contextlib import asynccontextmanager
from datetime import datetime
from pathlib import Path
from zoneinfo import ZoneInfo

import psutil
from fastapi import Depends, FastAPI, File, Form, HTTPException, Request, UploadFile
from fastapi.responses import HTMLResponse, Response, PlainTextResponse, RedirectResponse
from fastapi.security import HTTPBasic, HTTPBasicCredentials
from fastapi.staticfiles import StaticFiles
from fastapi.templating import Jinja2Templates

from . import actions, config, crypt, db, hub, spintax, transcode, youtube
from .orchestrator import orchestrator
from .streamer import supervisor

security = HTTPBasic()
templates = Jinja2Templates(directory=str(Path(__file__).parent / "templates"))
stats = actions.stats


async def auth(request: Request):
    if request.url.path == "/healthz":
        return
    c: HTTPBasicCredentials = await security(request)
    ok = config.ADMIN_PASSWORD and secrets.compare_digest(c.username, config.ADMIN_USER) \
        and secrets.compare_digest(c.password, config.ADMIN_PASSWORD)
    if not ok:
        raise HTTPException(401, headers={"WWW-Authenticate": "Basic"})


async def net_sampler(stop):
    last = psutil.net_io_counters().bytes_sent, time.monotonic()
    while not stop.is_set():
        await asyncio.sleep(5)
        now = psutil.net_io_counters().bytes_sent, time.monotonic()
        stats["up_mbps"] = (now[0] - last[0]) * 8 / 1e6 / (now[1] - last[1])
        last = now


@asynccontextmanager
async def lifespan(app):
    db.init()
    stop = asyncio.Event()
    tasks = [asyncio.create_task(transcode.worker(stop)), asyncio.create_task(orchestrator.run(stop)),
             asyncio.create_task(net_sampler(stop)), asyncio.create_task(hub.run(stop))]
    await supervisor.restore()
    yield
    stop.set()
    await supervisor.shutdown()
    for t in tasks:
        t.cancel()


app = FastAPI(lifespan=lifespan, dependencies=[Depends(auth)])
app.mount("/static", StaticFiles(directory=str(Path(__file__).parent / "static")), name="static")


def fmt_ts(ts):
    if not ts:
        return "-"
    return datetime.fromtimestamp(ts, ZoneInfo(config.TIMEZONE)).strftime("%d/%m %H:%M")


templates.env.filters["ts"] = fmt_ts
templates.env.filters["mask"] = lambda k: crypt.mask(crypt.dec(k))


def back(url, msg=None, err=False):
    if msg:
        path, _, frag = url.partition("#")
        q = {"msg": msg, **({"err": 1} if err else {})}
        url = path + ("&" if "?" in path else "?") + urlencode(q) + (f"#{frag}" if frag else "")
    return RedirectResponse(url, status_code=303)


STATUS = {"running": "jalan", "degraded": "lambat", "reconnecting": "menyambung ulang", "starting": "memulai",
          "stopped": "berhenti", "error": "error", "creating": "disiapkan", "live": "live", "ending": "diakhiri",
          "complete": "selesai", "failed": "gagal", "queued": "antre", "processing": "diproses", "ready": "siap",
          "info": "info", "warn": "peringatan"}
templates.env.filters["st"] = lambda v: STATUS.get(v, v)
templates.env.globals["nav_channels"] = lambda: db.q("""SELECT c.id, c.title,
    (SELECT COUNT(*) FROM broadcasts b JOIN stream_keys k ON k.id=b.key_id WHERE k.channel_id=c.id AND b.state='live') live
    FROM channels c ORDER BY c.title""")


def setup_state(cid=None):
    """Progress of the beginner setup path, for one channel (latest connected by default)."""
    ch = db.one("SELECT * FROM channels WHERE id=?", cid) if cid else db.one("SELECT * FROM channels ORDER BY id DESC LIMIT 1")
    st = {"ch": ch, "secrets": youtube.oauth_ready(), "n_keys": 0, "video": 0, "audio": 0, "titles": 0, "running": 0}
    if ch:
        c = ch["id"]
        st["n_keys"] = db.one("SELECT COUNT(*) n FROM stream_keys WHERE channel_id=?", c)["n"]
        st["running"] = db.one("SELECT COUNT(*) n FROM stream_keys WHERE channel_id=? AND desired='running'", c)["n"]
        st["titles"] = db.one("SELECT COUNT(*) n FROM titles WHERE channel_id=?", c)["n"]
        for kind in ("video", "audio"):
            st[kind] = db.one("SELECT COUNT(*) n FROM media WHERE channel_id=? AND kind=? AND status='ready'", c, kind)["n"]
    st["done"] = {
        "google": st["secrets"], "channel": bool(ch), "stream": st["n_keys"] > 0,
        "content": st["video"] > 0 and st["audio"] > 0 and st["titles"] > 0, "live": st["running"] > 0,
    }
    st["current"] = next((k for k, v in st["done"].items() if not v), None)
    return st


# ---------- overview ----------
@app.get("/", response_class=HTMLResponse)
def overview(request: Request):
    if not db.one("SELECT 1 FROM channels"):
        return RedirectResponse("/setup")
    used, exhausted = youtube.quota_state()
    keys = db.q("SELECT state, COUNT(*) n FROM stream_keys GROUP BY state")
    return templates.TemplateResponse(request, "overview.html", {
        "channels": db.q("""SELECT c.*,
              (SELECT COUNT(*) FROM stream_keys k WHERE k.channel_id=c.id AND k.state IN ('running','degraded')) keys_on,
              (SELECT COUNT(*) FROM stream_keys k WHERE k.channel_id=c.id) keys_total,
              (SELECT COUNT(*) FROM broadcasts b JOIN stream_keys k ON k.id=b.key_id
                 WHERE k.channel_id=c.id AND b.state='live') live
            FROM channels c ORDER BY c.title"""),
        "live": db.one("SELECT COUNT(*) n FROM broadcasts WHERE state='live'")["n"],
        "keys": {r["state"]: r["n"] for r in keys},
        "quota_used": used, "quota_limit": config.QUOTA_LIMIT, "quota_exhausted": exhausted,
        "cpu": psutil.cpu_percent(), "disk": psutil.disk_usage(str(config.DATA_DIR)).percent,
        "up_mbps": stats["up_mbps"],
        "events": db.q("SELECT e.*, c.title ch FROM events e LEFT JOIN channels c ON c.id=e.channel_id "
                       "ORDER BY e.id DESC LIMIT 100"),
        "hub": hub.status, "hub_url": config.HUB_URL, "paired": bool(hub.token()),
    })


@app.get("/setup", response_class=HTMLResponse)
def setup(request: Request, c: int | None = None):
    st = setup_state(c)
    return templates.TemplateResponse(request, "setup.html", {
        "st": st, "client_type": youtube.client_type() if st["secrets"] else "",
        "local": request.url.hostname in ("localhost", "127.0.0.1"),
        "callback": config.BASE_URL + "/oauth/callback",
    })


async def act(name, params, fallback="/", to=None):
    """Run a shared action and redirect back with its message (errors shown, never a bare 400 page)."""
    try:
        r = await actions.run(name, params)
    except actions.ActionError as e:
        return back(fallback, str(e), err=True)
    cid = r.get("channel_id")
    return back(to or (f"/c/{cid}" if cid else fallback), r.get("msg"))


# ---------- OAuth ----------
_states: dict[str, float] = {}


@app.get("/oauth/start")
def oauth_start():
    flow = youtube.oauth_flow()
    url, state = flow.authorization_url(access_type="offline", prompt="consent", include_granted_scopes="true")
    _states[state] = time.time()
    return RedirectResponse(url)


@app.get("/oauth/callback")
async def oauth_callback(state: str, code: str = "", error: str = ""):
    if error or _states.pop(state, None) is None:
        return back("/setup", f"Login Google gagal: {error or 'sesi login kedaluwarsa, ulangi'}", err=True)
    flow = youtube.oauth_flow(state)
    await asyncio.to_thread(flow.fetch_token, code=code)
    return await act("oauth.token", {"token": flow.credentials.to_json()}, "/setup", to="/setup")


@app.post("/oauth/client-secrets")
async def oauth_client_secrets(file: UploadFile = File(...)):
    try:
        data = json.loads(await file.read())
    except ValueError:
        data = None
    return await act("oauth.client_secrets", {"data": data}, "/setup", to="/setup")


@app.get("/c/{cid}/token.pickle")
def channel_token_pickle(cid: int):
    # same format the old tool writes after login, so the token can be carried to another install
    creds = youtube.credentials(get_channel(cid))
    return Response(pickle.dumps(creds), media_type="application/octet-stream",
                    headers={"Content-Disposition": f'attachment; filename="token-{cid}.pickle"'})


@app.post("/oauth/import")
async def oauth_import(file: UploadFile = File(...)):
    return await act("oauth.import_pickle", {"data": await file.read()}, "/setup", to="/setup")


# ---------- channel ----------
def get_channel(cid):
    ch = db.one("SELECT * FROM channels WHERE id=?", cid)
    if not ch:
        raise HTTPException(404)
    return ch


@app.get("/c/{cid}", response_class=HTMLResponse)
def channel_page(request: Request, cid: int):
    ch = get_channel(cid)
    s = db.settings(ch)
    return templates.TemplateResponse(request, "channel.html", {
        "ch": ch, "s": s,
        "keys": db.q("SELECT * FROM stream_keys WHERE channel_id=? ORDER BY name", cid),
        "broadcasts": db.q("""SELECT b.*, k.name key_name FROM broadcasts b JOIN stream_keys k ON k.id=b.key_id
                              WHERE k.channel_id=? AND (b.state IN ('creating','live','ending') OR b.created > ?)
                              ORDER BY b.id DESC LIMIT 60""", cid, time.time() - 86400),
        "titles": "\n".join(r["text"] for r in db.q("SELECT text FROM titles WHERE channel_id=? ORDER BY id", cid)),
        "title_count": db.one("SELECT COUNT(*) n FROM titles WHERE channel_id=?", cid)["n"],
        "media": db.q("SELECT * FROM media WHERE channel_id=? ORDER BY kind, id DESC", cid),
        "preview": spintax.spin(s["description_template"]) if s["description_template"] else "",
        "events": db.q("SELECT * FROM events WHERE channel_id=? ORDER BY id DESC LIMIT 50", cid),
        "st": setup_state(cid),
    })


@app.post("/c/{cid}/sync")
async def channel_sync(cid: int, next: str = Form("")):
    to = next if next.startswith("/") and not next.startswith("//") else None
    return await act("channel.sync", {"channel_id": cid}, f"/c/{cid}", to=to)


@app.post("/c/{cid}/settings")
async def channel_settings(request: Request, cid: int):
    form = await request.form()
    # unchecked checkboxes are absent from a form post, so every bool is sent explicitly
    settings = {k: (k in form) if isinstance(v, bool) else form[k]
                for k, v in config.CHANNEL_DEFAULTS.items() if isinstance(v, bool) or k in form}
    return await act("channel.settings", {"channel_id": cid, "settings": settings}, f"/c/{cid}#settings",
                     to=f"/c/{cid}#settings")


@app.post("/c/{cid}/titles")
async def channel_titles(cid: int, titles: str = Form("")):
    return await act("channel.titles.set", {"channel_id": cid, "titles": titles}, f"/c/{cid}#titles", to=f"/c/{cid}#titles")


@app.post("/c/{cid}/titles/upload")
async def channel_titles_upload(cid: int, file: UploadFile = File(...)):
    text = (await file.read()).decode("utf-8-sig", errors="replace")
    return await act("channel.titles.add", {"channel_id": cid, "titles": text}, f"/c/{cid}#titles", to=f"/c/{cid}#titles")


@app.post("/c/{cid}/media")
async def channel_media(cid: int, kind: str = Form(...), files: list[UploadFile] = File(...)):
    get_channel(cid)
    if kind not in ("video", "audio", "thumb"):
        raise HTTPException(400)
    src_dir = config.MEDIA_DIR / str(cid) / "src"
    src_dir.mkdir(parents=True, exist_ok=True)
    for f in files:
        mid = db.run("INSERT INTO media(channel_id,kind,orig_name,created) VALUES(?,?,?,?)",
                     cid, kind, f.filename, time.time())
        dest = src_dir / f"{mid}{Path(f.filename).suffix.lower()}"
        with dest.open("wb") as out:
            await asyncio.to_thread(shutil.copyfileobj, f.file, out, 1024 * 1024)
        db.run("UPDATE media SET src_path=? WHERE id=?", str(dest), mid)
    return back(f"/c/{cid}#media", f"{len(files)} file diunggah dan masuk antrean konversi.")


@app.post("/media/{mid}/{op}")
async def media_op(mid: int, op: str):
    if op not in ("delete", "retry"):
        raise HTTPException(404)
    m = db.one("SELECT channel_id FROM media WHERE id=?", mid)
    to = f"/c/{m['channel_id']}#media" if m else "/"
    return await act(f"media.{op}", {"media_id": mid}, to, to=to)


# ---------- keys + broadcasts ----------
KEY_OPS = {"start": "key.start", "stop": "key.stop", "reshuffle": "key.reshuffle",
           "resume-slots": "key.resume_slots", "pause-slots": "key.pause_slots"}


@app.post("/k/{kid}/config")
async def key_config(kid: int, n_video: int = Form(...), n_audio: int = Form(...), locked: str = Form("")):
    return await act("key.config", {"key_id": kid, "n_video": n_video, "n_audio": n_audio, "locked": bool(locked)})


@app.post("/k/{kid}/{op}")
async def key_op(kid: int, op: str):
    if op not in KEY_OPS:
        raise HTTPException(404)
    return await act(KEY_OPS[op], {"key_id": kid})


@app.get("/k/{kid}/log", response_class=PlainTextResponse)
async def key_log(kid: int):
    try:
        return (await actions.run("key.log", {"key_id": kid}))["log"] or "(belum ada log)"
    except actions.ActionError:
        raise HTTPException(404)


@app.post("/b/{bid}/end")
async def broadcast_end(bid: int):
    return await act("broadcast.end", {"broadcast_id": bid})


@app.get("/healthz")
def healthz():
    # counts only (no names/keys): read by the Windows tray without the dashboard password
    return {"ok": True, "hub": hub.status, "version": config.VERSION,
            "live": db.one("SELECT COUNT(*) n FROM broadcasts WHERE state='live'")["n"],
            "keys_running": db.one("SELECT COUNT(*) n FROM stream_keys WHERE state IN ('running','degraded')")["n"],
            "keys_error": db.one("SELECT COUNT(*) n FROM stream_keys WHERE state='error'")["n"]}
