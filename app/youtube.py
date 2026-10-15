"""YouTube Data API v3 wrapper: per-channel credentials + quota accounting.

All calls are blocking (google-api-python-client); callers run them via asyncio.to_thread.
"""
import io
import json
import pickle
import time
from datetime import datetime, timezone
from zoneinfo import ZoneInfo

from google.auth.transport.requests import Request
from google.oauth2.credentials import Credentials
from google_auth_oauthlib.flow import Flow
from googleapiclient.discovery import build
from googleapiclient.errors import HttpError
from googleapiclient.http import MediaFileUpload

from . import config, crypt, db

SCOPES = ["https://www.googleapis.com/auth/youtube.force-ssl"]

# Documented YouTube Data API v3 costs (list=1, write=50).
COST = {"list": 1, "insert": 50, "update": 50, "bind": 50, "transition": 50, "delete": 50, "set": 50}
CYCLE_COST = 300  # insert + bind + thumbnail + videos.update + live + complete


class QuotaExhausted(Exception):
    pass


def quota_day():
    return datetime.now(ZoneInfo("America/Los_Angeles")).strftime("%Y-%m-%d")


def quota_state():
    row = db.one("SELECT units, exhausted FROM quota WHERE day=?", quota_day())
    return (row["units"], bool(row["exhausted"])) if row else (0, False)


def quota_left():
    used, exhausted = quota_state()
    return 0 if exhausted else config.QUOTA_LIMIT - used


def _charge(units, exhausted=False):
    db.run(
        "INSERT INTO quota(day,units,exhausted) VALUES(?,?,?) "
        "ON CONFLICT(day) DO UPDATE SET units=units+excluded.units, exhausted=MAX(exhausted,excluded.exhausted)",
        quota_day(), units, int(exhausted),
    )


def _client_config():
    """client_secrets.json (as downloaded from Google Cloud, type web or installed) in DATA_DIR wins over env."""
    if config.CLIENT_SECRETS_FILE.exists():
        return json.loads(config.CLIENT_SECRETS_FILE.read_text())
    return {"web": {
        "client_id": config.GOOGLE_CLIENT_ID,
        "client_secret": config.GOOGLE_CLIENT_SECRET,
        "auth_uri": "https://accounts.google.com/o/oauth2/auth",
        "token_uri": "https://oauth2.googleapis.com/token",
        "redirect_uris": [config.BASE_URL + "/oauth/callback"],
    }}


def oauth_ready():
    cfg = _client_config()
    return bool(config.SECRET_KEY and next(iter(cfg.values())).get("client_id"))


def client_type():
    return "Desktop" if "installed" in _client_config() else "Web"


def oauth_flow(state=None):
    # installed (Desktop) clients accept any localhost redirect, so this also works with the old tool's file
    return Flow.from_client_config(
        _client_config(), scopes=SCOPES, state=state,
        redirect_uri=config.BASE_URL + "/oauth/callback",
        autogenerate_code_verifier=False,
    )


class _CredsUnpickler(pickle.Unpickler):
    # token.pickle from the old tool holds a google Credentials object; refuse anything else (pickle = code exec)
    def find_class(self, module, name):
        if module.split(".")[0] == "google" or module == "datetime":
            return super().find_class(module, name)
        raise pickle.UnpicklingError(f"token.pickle berisi {module}.{name}, ditolak")


def creds_from_pickle(data: bytes) -> Credentials:
    creds = _CredsUnpickler(io.BytesIO(data)).load()
    if not isinstance(creds, Credentials) or not creds.refresh_token:
        raise ValueError("token.pickle tidak berisi Credentials dengan refresh token")
    if not creds.valid:
        creds.refresh(Request())
    return creds


def credentials(channel) -> Credentials:
    info = json.loads(crypt.dec(channel["token_enc"]))
    creds = Credentials.from_authorized_user_info(info, SCOPES)
    if not creds.valid:
        try:
            creds.refresh(Request())
        except Exception:
            db.run("UPDATE channels SET token_ok=0 WHERE id=?", channel["id"])
            db.event("error", "Token kedaluwarsa/dicabut, hubungkan ulang channel", channel["id"])
            raise
        db.run("UPDATE channels SET token_enc=?, token_ok=1 WHERE id=?", crypt.enc(creds.to_json()), channel["id"])
    return creds


class YT:
    def __init__(self, channel):
        self.channel = channel
        self.api = build("youtube", "v3", credentials=credentials(channel), cache_discovery=False)

    def _exec(self, req, kind, retries=4):
        if quota_left() < COST[kind]:
            raise QuotaExhausted()
        for attempt in range(retries):
            try:
                res = req.execute()
                _charge(COST[kind])
                return res
            except HttpError as e:
                _charge(COST[kind])
                reason = str(e)
                if "quotaExceeded" in reason or "dailyLimitExceeded" in reason:
                    _charge(0, exhausted=True)
                    db.event("error", "Kuota YouTube API habis hari ini", self.channel["id"])
                    raise QuotaExhausted() from e
                retryable = e.resp.status in (429, 500, 503) or "RateLimit" in reason
                if not retryable or attempt == retries - 1:
                    raise
                time.sleep(2 ** attempt * 5)

    # --- streams ---
    def list_streams(self):
        items, token = [], None
        while True:
            res = self._exec(self.api.liveStreams().list(
                part="id,snippet,cdn,status", mine=True, maxResults=50, pageToken=token), "list")
            items += res.get("items", [])
            token = res.get("nextPageToken")
            if not token:
                return items

    def stream_status(self, stream_id):
        res = self._exec(self.api.liveStreams().list(part="status", id=stream_id), "list")
        items = res.get("items", [])
        return items[0]["status"]["streamStatus"] if items else None

    # --- broadcasts ---
    def create_broadcast(self, title, description, privacy):
        body = {
            "snippet": {"title": title[:100], "description": description[:5000],
                        "scheduledStartTime": datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")},
            "status": {"privacyStatus": privacy, "selfDeclaredMadeForKids": False},
            "contentDetails": {"enableAutoStart": False, "enableAutoStop": False,
                               "monitorStream": {"enableMonitorStream": False}},
        }
        return self._exec(self.api.liveBroadcasts().insert(
            part="snippet,status,contentDetails", body=body), "insert")["id"]

    def bind(self, broadcast_id, stream_id):
        self._exec(self.api.liveBroadcasts().bind(
            part="id,contentDetails", id=broadcast_id, streamId=stream_id), "bind")

    def set_thumbnail(self, video_id, path):
        self._exec(self.api.thumbnails().set(videoId=video_id, media_body=MediaFileUpload(path)), "set")

    def set_video_meta(self, video_id, title, description, category_id, tags):
        snippet = {"title": title[:100], "description": description[:5000], "categoryId": category_id}
        if tags:
            snippet["tags"] = tags
        self._exec(self.api.videos().update(part="snippet", body={"id": video_id, "snippet": snippet}), "update")

    def transition(self, broadcast_id, status):
        self._exec(self.api.liveBroadcasts().transition(
            part="status", id=broadcast_id, broadcastStatus=status), "transition")

    def broadcast(self, broadcast_id):
        res = self._exec(self.api.liveBroadcasts().list(part="status", id=broadcast_id), "list")
        items = res.get("items", [])
        return items[0] if items else None

    def viewers(self, video_id):
        res = self._exec(self.api.videos().list(part="liveStreamingDetails", id=video_id), "list")
        items = res.get("items", [])
        if not items:
            return None
        return int(items[0].get("liveStreamingDetails", {}).get("concurrentViewers", 0))

    def delete_broadcast(self, broadcast_id):
        self._exec(self.api.liveBroadcasts().delete(id=broadcast_id), "delete")
