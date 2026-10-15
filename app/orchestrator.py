"""Keeps each running stream key filled with N live broadcasts.

Slot lifecycle: creating -> live -> (cycle ends) -> ending -> complete, then a fresh slot is created.
"""
import asyncio
import random
import time

from . import config, db, spintax
from .streamer import supervisor
from .youtube import CYCLE_COST, YT, QuotaExhausted, quota_left

TICK = 15
ACTIVE = ("creating", "live", "ending")


def next_title(channel_id, s):
    row = db.one("SELECT * FROM titles WHERE channel_id=? ORDER BY used, last_used, id LIMIT 1", channel_id)
    if row is None:
        return None
    if s["delete_title"]:
        db.run("DELETE FROM titles WHERE id=?", row["id"])
    else:
        db.run("UPDATE titles SET used=used+1, last_used=? WHERE id=?", time.time(), row["id"])
    return row["text"]


def random_thumb(channel_id):
    rows = db.q("SELECT path FROM media WHERE channel_id=? AND kind='thumb' AND status='ready'", channel_id)
    return random.choice(rows)["path"] if rows else None


def cycle_seconds(s):
    return float(s["cycle_hours"]) * 3600 if float(s["cycle_hours"]) > 0 else None


class Orchestrator:
    def __init__(self):
        self.inflight: set[int] = set()   # broadcast ids with a running task
        self.last_create = 0.0

    async def run(self, stop: asyncio.Event):
        await self._recover()
        while not stop.is_set():
            try:
                await self.tick()
            except Exception as e:  # never let one bad tick kill the loop
                db.event("error", f"Orchestrator: {e}")
            try:
                await asyncio.wait_for(stop.wait(), TICK)
            except asyncio.TimeoutError:
                pass

    async def _recover(self):
        # A slot interrupted mid-creation is unusable: delete it and let tick() make a new one.
        for b in db.q("SELECT * FROM broadcasts WHERE state='creating'"):
            await self._fail(b["id"], "Dihentikan oleh restart aplikasi", count=False)

    async def tick(self):
        now = time.time()
        # 1) end slots whose cycle is over (or whose key stopped)
        for b in db.q("""SELECT b.*, k.channel_id, k.desired FROM broadcasts b
                         JOIN stream_keys k ON k.id=b.key_id WHERE b.state IN ('live','ending')"""):
            if b["id"] in self.inflight:
                continue
            forced = b["state"] == "ending" or b["desired"] != "running"
            if forced or (b["ends"] and b["ends"] <= now):
                self._spawn(b["id"], self._end_cycle(b["id"], forced))

        # 2) fill free slots
        active_total = db.one(f"SELECT COUNT(*) n FROM broadcasts WHERE state IN {ACTIVE}")["n"]
        for key in db.q("""SELECT k.*, c.settings, c.token_ok FROM stream_keys k JOIN channels c ON c.id=k.channel_id
                           WHERE k.state IN ('running','degraded') AND k.slots_paused=0 AND c.token_ok=1"""):
            s = db.settings(key)
            active = db.one(f"SELECT COUNT(*) n FROM broadcasts WHERE key_id=? AND state IN {ACTIVE}", key["id"])["n"]
            if active >= int(s["slots_per_key"]) or active_total >= config.MAX_ACTIVE_BROADCASTS:
                continue
            if time.time() - self.last_create < config.JOB_DELAY_SEC:
                return
            if quota_left() < CYCLE_COST:
                return
            if not db.one("SELECT 1 FROM titles WHERE channel_id=?", key["channel_id"]):
                continue
            self.last_create = time.time()
            active_total += 1
            bid = db.run("INSERT INTO broadcasts(key_id,state,created) VALUES(?, 'creating', ?)", key["id"], time.time())
            self._spawn(bid, self._create(bid))

    def _spawn(self, bid, coro):
        self.inflight.add(bid)

        async def wrapped():
            try:
                await coro
            finally:
                self.inflight.discard(bid)
        asyncio.create_task(wrapped())

    def _ctx(self, bid):
        b = db.one("SELECT * FROM broadcasts WHERE id=?", bid)
        key = db.one("SELECT * FROM stream_keys WHERE id=?", b["key_id"])
        ch = db.one("SELECT * FROM channels WHERE id=?", key["channel_id"])
        return b, key, ch, db.settings(ch)

    async def _create(self, bid):
        b, key, ch, s = self._ctx(bid)
        prev = db.one("SELECT title FROM broadcasts WHERE key_id=? AND state='complete' AND title IS NOT NULL "
                      "ORDER BY id DESC LIMIT 1", key["id"])
        # Change Title = No: a key keeps reusing its last title until stopped manually.
        title = prev["title"] if (not s["change_title"] and prev) else next_title(ch["id"], s)
        desc = spintax.spin(s["description_template"])
        db.run("UPDATE broadcasts SET title=? WHERE id=?", title, bid)
        try:
            yt = await asyncio.to_thread(YT, ch)
            yt_id = await asyncio.to_thread(yt.create_broadcast, title, desc, s["privacy"])
            db.run("UPDATE broadcasts SET yt_id=? WHERE id=?", yt_id, bid)
            await asyncio.to_thread(yt.bind, yt_id, key["yt_stream_id"])
            tags = [t.strip() for t in s["tags"].split(",") if t.strip()]
            await asyncio.to_thread(yt.set_video_meta, yt_id, title, desc, s["category_id"], tags)
            thumb = random_thumb(ch["id"])
            if thumb:
                try:
                    await asyncio.to_thread(yt.set_thumbnail, yt_id, thumb)
                except QuotaExhausted:
                    raise
                except Exception as e:
                    db.event("warn", f"Thumbnail gagal (dilewati): {e}", ch["id"])
            await self._wait_stream_active(yt, key)
            await asyncio.sleep(random.uniform(float(s["jitter_min"]), float(s["jitter_max"])))
            await asyncio.to_thread(yt.transition, yt_id, "live")
        except Exception as e:
            await self._fail(bid, str(e)[:500])
            return
        now = time.time()
        cyc = cycle_seconds(s)
        db.run("UPDATE broadcasts SET state='live', started=?, ends=? WHERE id=?", now, now + cyc if cyc else None, bid)
        db.run("UPDATE stream_keys SET broadcast_fails=0 WHERE id=?", key["id"])
        db.event("info", f"LIVE: {title} (key {key['name']})", ch["id"])

    async def _wait_stream_active(self, yt, key, timeout=300):
        deadline = time.time() + timeout
        while time.time() < deadline:
            if await asyncio.to_thread(yt.stream_status, key["yt_stream_id"]) == "active":
                return
            await asyncio.sleep(10)
        raise RuntimeError("Stream key tidak aktif di YouTube dalam 5 menit (ffmpeg belum mengirim?)")

    async def _end_cycle(self, bid, forced=False):
        b, key, ch, s = self._ctx(bid)
        try:
            yt = await asyncio.to_thread(YT, ch)
            if s["filter_view"] and not forced:
                viewers = await asyncio.to_thread(yt.viewers, b["yt_id"])
                db.run("UPDATE broadcasts SET viewers=? WHERE id=?", viewers, bid)
                if viewers:
                    # Someone is watching: keep this broadcast for another cycle.
                    cyc = cycle_seconds(s) or 3600
                    db.run("UPDATE broadcasts SET ends=? WHERE id=?", time.time() + cyc, bid)
                    db.event("info", f"{viewers} penonton, lanjut: {b['title']}", ch["id"])
                    return
                if not self._restart_allowed(key, s):
                    db.run("UPDATE broadcasts SET ends=? WHERE id=?", time.time() + (cycle_seconds(s) or 3600), bid)
                    db.event("warn", f"Batas restart harian key {key['name']} tercapai, live dilanjutkan", ch["id"])
                    return
            db.run("UPDATE broadcasts SET state='ending' WHERE id=?", bid)
            await asyncio.to_thread(yt.transition, b["yt_id"], "complete")
        except QuotaExhausted:
            db.run("UPDATE broadcasts SET state='live', ends=? WHERE id=?", time.time() + 1800, bid)
            return
        except Exception as e:
            db.event("warn", f"Gagal mengakhiri '{b['title']}': {e}", ch["id"])
        db.run("UPDATE broadcasts SET state='complete', restarts=restarts+? WHERE id=?",
               1 if s["filter_view"] else 0, bid)
        db.event("info", f"Selesai: {b['title']}", ch["id"])

    def _restart_allowed(self, key, s):
        since = time.time() - 86400
        n = db.one("SELECT COUNT(*) n FROM broadcasts WHERE key_id=? AND state='complete' AND restarts>0 AND started>?",
                   key["id"], since)["n"]
        return n < int(s["fv_max_restarts"])

    async def _fail(self, bid, err, count=True):
        b, key, ch, s = self._ctx(bid)
        db.run("UPDATE broadcasts SET state='failed', error=? WHERE id=?", err, bid)
        if b["yt_id"]:
            try:
                yt = await asyncio.to_thread(YT, ch)
                await asyncio.to_thread(yt.delete_broadcast, b["yt_id"])
            except Exception:
                pass
        if count:
            fails = key["broadcast_fails"] + 1
            db.run("UPDATE stream_keys SET broadcast_fails=? WHERE id=?", fails, key["id"])
            if fails >= config.KEY_MAX_BROADCAST_FAILS:
                db.run("UPDATE stream_keys SET slots_paused=1 WHERE id=?", key["id"])
                db.event("error", f"Key {key['name']}: {fails} broadcast gagal beruntun, slot dijeda", ch["id"])
        db.event("error", f"Broadcast gagal ({b['title']}): {err}", ch["id"])


orchestrator = Orchestrator()
