"""Every operator action in one place, shared by the local dashboard routes and hub RPC (PRD §13.3).

Each action takes a params dict and returns a dict; "msg" is the human result line, "channel_id" tells the
dashboard where to go back to. Invalid input raises ActionError with a message safe to show the user.
"""
import asyncio
import json
import time
from pathlib import Path

import psutil
from googleapiclient.discovery import build

from . import config, crypt, db, spintax, transcode, youtube
from .streamer import supervisor

ACTIONS = {}
BLOCKING = set()  # call YouTube over the network: run off the event loop
stats = {"up_mbps": 0.0}  # filled by main.net_sampler


class ActionError(Exception):
    pass


def action(name, blocking=False):
    def deco(fn):
        ACTIONS[name] = fn
        if blocking:
            BLOCKING.add(name)
        return fn
    return deco


async def run(name, params=None):
    fn = ACTIONS.get(name)
    if not fn:
        raise ActionError(f"Aksi tidak dikenal: {name}")
    params = params or {}
    if name in BLOCKING:
        return await asyncio.to_thread(fn, params)
    res = fn(params)
    return await res if asyncio.iscoroutine(res) else res


def _int(p, k):
    try:
        return int(p[k])
    except (KeyError, TypeError, ValueError):
        raise ActionError(f"Parameter {k} wajib angka")


def _row(sql, *args, what="Data"):
    r = db.one(sql, *args)
    if not r:
        raise ActionError(f"{what} tidak ditemukan")
    return r


def rows(sql, *args):
    return [dict(r) for r in db.q(sql, *args)]


def channel(p):
    return _row("SELECT * FROM channels WHERE id=?", _int(p, "channel_id"), what="Channel")


def key(p):
    return _row("SELECT * FROM stream_keys WHERE id=?", _int(p, "key_id"), what="Stream key")


# ---------- read ----------
@action("state.get")
def state_get(p):
    """Everything a dashboard needs, in one snapshot (also what the agent pushes to the hub)."""
    used, exhausted = youtube.quota_state()
    since = time.time() - 86400
    return {
        "version": config.VERSION,
        "channels": [dict(dict(c), token_enc=None, settings=db.settings(c)) for c in db.q("SELECT * FROM channels ORDER BY title")],
        "keys": [dict(dict(k), key_enc=None, key_masked=crypt.mask(crypt.dec(k["key_enc"])), locked_files=None)
                 for k in db.q("SELECT * FROM stream_keys ORDER BY name")],
        "broadcasts": rows("""SELECT b.*, k.name key_name, k.channel_id FROM broadcasts b JOIN stream_keys k ON k.id=b.key_id
                              WHERE b.state IN ('creating','live','ending') OR b.created > ? ORDER BY b.id DESC LIMIT 200""", since),
        "media": rows("SELECT id, channel_id, kind, orig_name, status, progress, error, created FROM media ORDER BY kind, id DESC"),
        "titles": {r["channel_id"]: r["n"] for r in db.q("SELECT channel_id, COUNT(*) n FROM titles GROUP BY channel_id")},
        "quota": {"used": used, "limit": config.QUOTA_LIMIT, "exhausted": exhausted},
        "system": {"cpu": psutil.cpu_percent(), "disk": psutil.disk_usage(str(config.DATA_DIR)).percent,
                   "up_mbps": round(stats["up_mbps"], 2)},
        "oauth": {"ready": youtube.oauth_ready(), "client_type": youtube.client_type() if youtube.oauth_ready() else None},
        "events": rows("SELECT * FROM events ORDER BY id DESC LIMIT 100"),
    }


@action("channel.detail")
def channel_detail(p):
    ch = channel(p)
    s = db.settings(ch)
    return {"channel_id": ch["id"], "settings": s,
            "titles": [r["text"] for r in db.q("SELECT text FROM titles WHERE channel_id=? ORDER BY id", ch["id"])],
            "preview": spintax.spin(s["description_template"]) if s["description_template"] else ""}


@action("key.log")
def key_log(p):
    k = key(p)
    f = config.LOG_DIR / f"key-{k['id']}.log"
    return {"log": f.read_text(encoding="utf-8") if f.exists() else ""}


# ---------- Google access + channels ----------
@action("oauth.client_secrets")
def oauth_client_secrets(p):
    data = p.get("data")
    try:
        info = data.get("installed") or data.get("web")
        assert info["client_id"] and info["client_secret"]
    except Exception:
        raise ActionError("File itu bukan client_secrets.json dari Google Cloud. Unduh ulang dari menu Credentials.")
    config.CLIENT_SECRETS_FILE.write_text(json.dumps(data))
    kind = "Desktop" if "installed" in data else "Web"
    db.event("info", f"client_secrets.json dipasang ({kind})")
    return {"msg": "Akses Google terpasang. Lanjut login channel.", "client_type": kind}


def save_channel(creds):
    yt = build("youtube", "v3", credentials=creds, cache_discovery=False)
    me = yt.channels().list(part="snippet", mine=True).execute()["items"][0]
    token = crypt.enc(creds.to_json())
    row = db.one("SELECT id FROM channels WHERE yt_channel_id=?", me["id"])
    if row:
        cid = row["id"]
        db.run("UPDATE channels SET token_enc=?, token_ok=1, title=? WHERE id=?", token, me["snippet"]["title"], cid)
    else:
        cid = db.run("INSERT INTO channels(yt_channel_id,title,token_enc,token_ok) VALUES(?,?,?,1)",
                     me["id"], me["snippet"]["title"], token)
    db.event("info", "Channel terhubung", cid)
    sync_keys(cid)
    return cid


@action("oauth.token", blocking=True)
def oauth_token(p):
    """Authorized-user token JSON from the hub's OAuth exchange; the hub keeps no copy (PRD §13.4)."""
    from google.oauth2.credentials import Credentials
    try:
        creds = Credentials.from_authorized_user_info(json.loads(p["token"]) if isinstance(p["token"], str) else p["token"],
                                                      youtube.SCOPES)
    except Exception as e:
        raise ActionError(f"Token tidak valid: {e}")
    cid = save_channel(creds)
    return {"msg": "Channel terhubung.", "channel_id": cid}


@action("oauth.import_pickle", blocking=True)
def oauth_import_pickle(p):
    try:
        creds = youtube.creds_from_pickle(p["data"])
    except Exception as e:
        raise ActionError(f"Impor token.pickle gagal: {e}")
    return {"msg": "Channel terhubung dari token.pickle.", "channel_id": save_channel(creds)}


def sync_keys(cid):
    ch = db.one("SELECT * FROM channels WHERE id=?", cid)
    streams = youtube.YT(ch).list_streams()
    for s in streams:
        name = s["cdn"]["ingestionInfo"]["streamName"]
        title = s["snippet"]["title"]
        row = db.one("SELECT id FROM stream_keys WHERE yt_stream_id=?", s["id"])
        if row:
            db.run("UPDATE stream_keys SET name=?, key_enc=? WHERE id=?", title, crypt.enc(name), row["id"])
        else:
            db.run("INSERT INTO stream_keys(channel_id,yt_stream_id,name,key_enc) VALUES(?,?,?,?)",
                   cid, s["id"], title, crypt.enc(name))
    return len(streams)


@action("channel.sync", blocking=True)
def channel_sync(p):
    cid = channel(p)["id"]
    n = sync_keys(cid)
    db.event("info", f"{n} stream key disinkron dari YouTube", cid)
    msg = f"{n} stream key ditemukan." if n else "Belum ada stream key di YouTube Studio. Buat dulu, lalu cek lagi."
    return {"msg": msg, "channel_id": cid, "keys": n}


# ---------- channel content ----------
@action("channel.settings")
def channel_settings(p):
    """Partial update: only keys present in p["settings"] change."""
    ch = channel(p)
    old = db.settings(ch)
    new = dict(old)
    for k, v in (p.get("settings") or {}).items():
        if k not in config.CHANNEL_DEFAULTS:
            continue
        default = config.CHANNEL_DEFAULTS[k]
        try:
            if isinstance(default, bool):
                new[k] = v if isinstance(v, bool) else str(v).lower() in ("1", "true", "on", "yes")
            else:
                new[k] = type(default)(v)
        except (TypeError, ValueError):
            raise ActionError(f"Nilai {k} tidak valid")
    if new["rate_mode"] not in ("vbr", "cbr") or int(new["resolution"]) not in (720, 1080):
        raise ActionError("Mode bitrate/resolusi tidak valid")
    if new["privacy"] not in ("public", "unlisted", "private"):
        raise ActionError("Privacy tidak valid")
    if new["filter_view"] and 0 < new["cycle_hours"] * 60 < new["fv_min_minutes"]:
        new["cycle_hours"] = new["fv_min_minutes"] / 60  # never end a live before the minimum
    db.run("UPDATE channels SET settings=? WHERE id=?", json.dumps(new), ch["id"])
    if (new["rate_mode"], new["resolution"]) != (old["rate_mode"], old["resolution"]):
        transcode.requeue_channel_videos(ch["id"])
        db.event("info", "Format video berubah: library di-encode ulang", ch["id"])
    return {"msg": "Pengaturan disimpan.", "channel_id": ch["id"]}


def _clean_titles(titles):
    if isinstance(titles, str):
        titles = titles.splitlines()
    return [t.strip()[:100] for t in titles if t and t.strip()]


@action("channel.titles.set")
def titles_set(p):
    cid = channel(p)["id"]
    lines = _clean_titles(p.get("titles", []))
    db.run("DELETE FROM titles WHERE channel_id=?", cid)
    for t in lines:
        db.run("INSERT INTO titles(channel_id,text) VALUES(?,?)", cid, t)
    return {"msg": f"{len(lines)} judul disimpan.", "channel_id": cid}


@action("channel.titles.add")
def titles_add(p):
    cid = channel(p)["id"]
    lines = _clean_titles(p.get("titles", []))
    for t in lines:
        db.run("INSERT INTO titles(channel_id,text) VALUES(?,?)", cid, t)
    return {"msg": f"{len(lines)} judul ditambahkan.", "channel_id": cid}


@action("media.delete")
def media_delete(p):
    m = _row("SELECT * FROM media WHERE id=?", _int(p, "media_id"), what="File")
    for f in {m["path"], m["src_path"]} - {None}:
        Path(f).unlink(missing_ok=True)
    db.run("DELETE FROM media WHERE id=?", m["id"])
    return {"msg": "File dihapus.", "channel_id": m["channel_id"]}


@action("media.retry")
def media_retry(p):
    m = _row("SELECT * FROM media WHERE id=?", _int(p, "media_id"), what="File")
    db.run("UPDATE media SET status='queued', progress=0, error=NULL WHERE id=?", m["id"])
    return {"msg": "File masuk antrean lagi.", "channel_id": m["channel_id"]}


# ---------- keys + broadcasts ----------
@action("key.start")
def key_start(p):
    k = key(p)
    db.run("UPDATE stream_keys SET last_error=NULL WHERE id=?", k["id"])
    supervisor.start(k["id"])
    return {"msg": f"{k['name']} mulai mengirim. Live dibuat otomatis begitu YouTube menerima sinyal.",
            "channel_id": k["channel_id"]}


@action("key.stop")
async def key_stop(p):
    k = key(p)
    await supervisor.stop(k["id"])  # orchestrator completes this key's live broadcasts on its next tick
    return {"msg": f"{k['name']} dihentikan. Live di key ini akan diakhiri.", "channel_id": k["channel_id"]}


@action("key.config")
def key_config(p):
    k = key(p)
    db.run("UPDATE stream_keys SET n_video=?, n_audio=?, locked=?, locked_files=NULL WHERE id=?",
           max(1, _int(p, "n_video")), max(1, _int(p, "n_audio")), int(bool(p.get("locked"))), k["id"])
    return {"msg": f"Sumber {k['name']} disimpan.", "channel_id": k["channel_id"]}


@action("key.reshuffle")
def key_reshuffle(p):
    k = key(p)
    db.run("UPDATE stream_keys SET locked_files=NULL WHERE id=?", k["id"])  # applies on next (re)start
    return {"msg": "Pilihan file diacak ulang saat start berikutnya.", "channel_id": k["channel_id"]}


@action("key.resume_slots")
def key_resume(p):
    k = key(p)
    db.run("UPDATE stream_keys SET slots_paused=0, broadcast_fails=0 WHERE id=?", k["id"])
    return {"msg": "Slot dilanjutkan.", "channel_id": k["channel_id"]}


@action("key.pause_slots")
def key_pause(p):
    k = key(p)
    db.run("UPDATE stream_keys SET slots_paused=1 WHERE id=?", k["id"])
    return {"msg": "Slot dijeda. Live yang sedang jalan tidak terpengaruh.", "channel_id": k["channel_id"]}


@action("broadcast.end")
def broadcast_end(p):
    b = _row("SELECT b.*, k.channel_id FROM broadcasts b JOIN stream_keys k ON k.id=b.key_id WHERE b.id=?",
             _int(p, "broadcast_id"), what="Live")
    db.run("UPDATE broadcasts SET state='ending' WHERE id=? AND state='live'", b["id"])  # forced: skips Filter View
    return {"msg": "Live sedang diakhiri.", "channel_id": b["channel_id"]}
