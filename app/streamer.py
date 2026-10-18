"""One ffmpeg per stream key: concat lists of normalized media, copied to YouTube RTMP."""
import asyncio
import json
import random
import time
from collections import deque
from pathlib import Path

from . import config, crypt, db

BACKOFF = [5, 10, 30, 60]
FAST_FAIL_SEC = 5


def _concat_file(path: Path, files: list[str]):
    lines = ["file '" + f.replace("\\", "/").replace("'", "'\\''") + "'" for f in files]
    path.write_text("\n".join(lines) + "\n", encoding="utf-8")


def pick_files(key) -> tuple[list[str], list[str]]:
    """Random selection, optionally locked so restarts reuse the same set."""
    ready = lambda kind: [r["path"] for r in db.q(
        "SELECT path FROM media WHERE channel_id=? AND kind=? AND status='ready'", key["channel_id"], kind)]
    videos, audios = ready("video"), ready("audio")
    if key["locked"] and key["locked_files"]:
        locked = json.loads(key["locked_files"])
        lv = [f for f in locked["video"] if f in videos]
        la = [f for f in locked["audio"] if f in audios]
        if lv and la:
            return lv, la
    random.shuffle(videos)
    random.shuffle(audios)
    videos, audios = videos[: key["n_video"]], audios[: key["n_audio"]]
    db.run("UPDATE stream_keys SET locked_files=? WHERE id=?", json.dumps({"video": videos, "audio": audios}), key["id"])
    return videos, audios


def build_cmd(key_plain: str, vlist: Path, alist: Path) -> list[str]:
    return [config.FFMPEG, "-hide_banner", "-nostats", "-loglevel", "warning",
            "-re", "-stream_loop", "-1", "-f", "concat", "-safe", "0", "-i", str(vlist),
            "-stream_loop", "-1", "-f", "concat", "-safe", "0", "-i", str(alist),
            "-map", "0:v:0", "-map", "1:a:0", "-c", "copy",
            "-f", "flv", "-flvflags", "no_duration_filesize",
            "-progress", "pipe:1",
            config.RTMP_BASE + key_plain]


def kill_orphans():
    """ffmpeg left over from a previous run (Windows doesn't kill children with the parent) would double-stream.

    Ours are recognised by our data dir in their command line (concat lists, media paths).
    """
    import psutil
    mine = str(config.DATA_DIR)
    for p in psutil.process_iter(["name", "cmdline"]):
        try:
            if "ffmpeg" in (p.info["name"] or "").lower() and mine in " ".join(p.info["cmdline"] or []):
                p.kill()
                db.event("warn", f"ffmpeg sisa run sebelumnya dihentikan (pid {p.pid})")
        except (psutil.NoSuchProcess, psutil.AccessDenied):
            pass


class Supervisor:
    def __init__(self):
        self.tasks: dict[int, asyncio.Task] = {}
        self.procs: dict[int, asyncio.subprocess.Process] = {}

    def is_running(self, key_id):
        t = self.tasks.get(key_id)
        return t is not None and not t.done()

    def start(self, key_id):
        db.run("UPDATE stream_keys SET desired='running' WHERE id=?", key_id)
        if not self.is_running(key_id):  # one ffmpeg per key, never two
            self.tasks[key_id] = asyncio.create_task(self._loop(key_id))

    async def stop(self, key_id):
        db.run("UPDATE stream_keys SET desired='stopped' WHERE id=?", key_id)
        p = self.procs.get(key_id)
        if p and p.returncode is None:
            p.terminate()
            try:
                await asyncio.wait_for(p.wait(), 10)
            except asyncio.TimeoutError:
                p.kill()
        t = self.tasks.pop(key_id, None)
        if t:
            t.cancel()
        db.run("UPDATE stream_keys SET state='stopped', speed=NULL WHERE id=?", key_id)

    async def restore(self):
        kill_orphans()
        for row in db.q("SELECT id FROM stream_keys WHERE desired='running'"):
            self.start(row["id"])

    async def shutdown(self):
        for key_id in list(self.procs):
            p = self.procs[key_id]
            if p.returncode is None:
                p.terminate()
        await asyncio.sleep(1)

    async def _loop(self, key_id):
        fails = 0
        while True:
            key = db.one("SELECT * FROM stream_keys WHERE id=?", key_id)
            if key is None or key["desired"] != "running":
                return
            ch = key["channel_id"]
            videos, audios = pick_files(key)
            if not videos or not audios:
                db.run("UPDATE stream_keys SET state='error', desired='stopped', last_error=? WHERE id=?",
                       "Library belum punya video & audio yang siap", key_id)
                db.event("error", f"Key {key['name']}: library video/audio kosong", ch)
                return
            work = config.DATA_DIR / "run" / str(key_id)
            work.mkdir(parents=True, exist_ok=True)
            vlist, alist = work / "videos.txt", work / "audios.txt"
            _concat_file(vlist, videos)
            _concat_file(alist, audios)

            db.run("UPDATE stream_keys SET state=? WHERE id=?", "starting" if fails == 0 else "reconnecting", key_id)
            started = time.monotonic()
            rc, tail = await self._run_ffmpeg(key, vlist, alist)
            ran = time.monotonic() - started

            key = db.one("SELECT * FROM stream_keys WHERE id=?", key_id)
            if key is None or key["desired"] != "running":
                return
            fails = 1 if ran > 300 else fails + 1  # a long healthy run resets the counter
            err = tail[-1] if tail else f"ffmpeg exit {rc}"
            if ran < FAST_FAIL_SEC:
                err = "ffmpeg mati < 5 detik (cek file/stream key): " + err
            if fails > config.MAX_RECONNECTS:
                db.run("UPDATE stream_keys SET state='error', desired='stopped', last_error=? WHERE id=?", err, key_id)
                db.event("error", f"Key {key['name']} dihentikan setelah {config.MAX_RECONNECTS} reconnect gagal: {err}", ch)
                return
            delay = BACKOFF[min(fails - 1, len(BACKOFF) - 1)]
            db.run("UPDATE stream_keys SET state='reconnecting', last_error=? WHERE id=?", err, key_id)
            db.event("warn", f"Key {key['name']} terputus, reconnect #{fails} dalam {delay}s", ch)
            await asyncio.sleep(delay)

    async def _run_ffmpeg(self, key, vlist, alist):
        key_id = key["id"]
        plain = crypt.dec(key["key_enc"])
        p = await asyncio.create_subprocess_exec(
            *build_cmd(plain, vlist, alist),
            stdin=asyncio.subprocess.DEVNULL, stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.PIPE)
        self.procs[key_id] = p
        tail = deque(maxlen=20)
        log_path = config.LOG_DIR / f"key-{key_id}.log"

        async def read_err():
            async for line in p.stderr:
                text = line.decode(errors="replace").rstrip().replace(plain, crypt.mask(plain))
                tail.append(text)
            # ponytail: keep only the last 20 stderr lines on disk, no rotation needed
            log_path.write_text("\n".join(tail), encoding="utf-8")

        err_task = asyncio.create_task(read_err())
        state = None
        async for line in p.stdout:
            k, _, v = line.decode(errors="replace").strip().partition("=")
            if k == "speed" and v.endswith("x"):
                try:
                    speed = float(v[:-1])
                except ValueError:
                    continue
                new_state = "running" if speed >= 0.9 else "degraded"
                if new_state != state:
                    state = new_state
                    db.run("UPDATE stream_keys SET state=? WHERE id=?", state, key_id)
                    if state == "degraded":
                        db.event("warn", f"Key {key['name']} lambat (speed {speed}x): bandwidth/CPU kurang", key["channel_id"])
                db.run("UPDATE stream_keys SET speed=? WHERE id=?", speed, key_id)
        rc = await p.wait()
        await err_task
        self.procs.pop(key_id, None)
        return rc, list(tail)


supervisor = Supervisor()
