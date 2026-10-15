"""Normalize uploads so live streams can use `-c copy` and concat safely."""
import asyncio
import os
import sys
from pathlib import Path

from PIL import Image

from . import config, db

BITRATE = {1080: 6000, 720: 4000}  # kbps video target/cap


def video_args(resolution: int, rate_mode: str) -> list[str]:
    h = resolution
    w = 1920 if h == 1080 else 1280
    br = BITRATE[h]
    vf = (f"scale={w}:{h}:force_original_aspect_ratio=decrease,"
          f"pad={w}:{h}:(ow-iw)/2:(oh-ih)/2,setsar=1,fps=30,format=yuv420p")
    args = ["-vf", vf, "-c:v", "libx264", "-preset", "veryfast", "-profile:v", "high",
            "-g", "60", "-keyint_min", "60", "-sc_threshold", "0"]
    if rate_mode == "cbr":
        args += ["-b:v", f"{br}k", "-minrate", f"{br}k", "-maxrate", f"{br}k",
                 "-bufsize", f"{br * 2}k", "-x264-params", "nal-hrd=cbr"]
    else:
        args += ["-crf", "21", "-maxrate", f"{br}k", "-bufsize", f"{br * 2}k"]
    # Audio comes from the audio library, so video files carry no audio track.
    return args + ["-an", "-movflags", "+faststart"]


AUDIO_ARGS = ["-vn", "-c:a", "aac", "-b:a", "192k", "-ar", "48000", "-ac", "2"]


async def probe_duration(path) -> float:
    p = await asyncio.create_subprocess_exec(
        config.FFPROBE, "-v", "error", "-show_entries", "format=duration", "-of", "csv=p=0", str(path),
        stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.DEVNULL)
    out, _ = await p.communicate()
    try:
        return float(out.strip())
    except ValueError:
        return 0.0


def _nice(cmd):
    # Live ffmpeg always wins the CPU over transcodes.
    return ["nice", "-n", "19", *cmd] if sys.platform != "win32" else cmd


async def _run(media, channel):
    s = db.settings(channel)
    src = Path(media["src_path"])
    out_dir = config.MEDIA_DIR / str(channel["id"]) / media["kind"]
    out_dir.mkdir(parents=True, exist_ok=True)

    if media["kind"] == "thumb":
        out = out_dir / f"{media['id']}.jpg"
        img = Image.open(src).convert("RGB")
        img.thumbnail((1280, 720))
        quality = 90
        img.save(out, "JPEG", quality=quality)
        while out.stat().st_size > 2_000_000 and quality > 40:
            quality -= 10
            img.save(out, "JPEG", quality=quality)
        return out

    if media["kind"] == "video":
        out = out_dir / f"{media['id']}.mp4"
        args = video_args(int(s["resolution"]), s["rate_mode"])
    else:
        out = out_dir / f"{media['id']}.m4a"
        args = AUDIO_ARGS
    tmp = out.with_suffix(".part" + out.suffix)
    duration = await probe_duration(src) or 1.0

    cmd = _nice([config.FFMPEG, "-y", "-hide_banner", "-nostats", "-i", str(src), *args,
                 "-progress", "pipe:1", str(tmp)])
    p = await asyncio.create_subprocess_exec(*cmd, stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.PIPE)
    err_task = asyncio.create_task(p.stderr.read())
    last = 0.0
    async for line in p.stdout:
        k, _, v = line.decode(errors="replace").strip().partition("=")
        if k == "out_time_us" and v.isdigit():
            pct = min(99.0, int(v) / 1e6 / duration * 100)
            if pct - last >= 2:
                last = pct
                db.run("UPDATE media SET progress=? WHERE id=?", pct, media["id"])
    rc = await p.wait()
    stderr = (await err_task).decode(errors="replace")
    if rc != 0:
        tmp.unlink(missing_ok=True)
        raise RuntimeError(stderr[-2000:] or f"ffmpeg exit {rc}")
    os.replace(tmp, out)
    return out


async def process(media_id):
    media = db.one("SELECT * FROM media WHERE id=?", media_id)
    channel = db.one("SELECT * FROM channels WHERE id=?", media["channel_id"])
    db.run("UPDATE media SET status='processing', progress=0, error=NULL WHERE id=?", media_id)
    try:
        out = await _run(media, channel)
    except Exception as e:
        db.run("UPDATE media SET status='failed', error=? WHERE id=?", str(e)[-2000:], media_id)
        db.event("error", f"Normalisasi gagal: {media['orig_name']}", channel["id"])
        return
    old = media["path"]
    db.run("UPDATE media SET status='ready', progress=100, path=? WHERE id=?", str(out), media_id)
    src = Path(media["src_path"])
    if src != out and not db.settings(channel)["keep_original"]:
        src.unlink(missing_ok=True)
        db.run("UPDATE media SET src_path=? WHERE id=?", str(out), media_id)
    if old and old != str(out):
        Path(old).unlink(missing_ok=True)


async def worker(stop: asyncio.Event):
    db.run("UPDATE media SET status='queued' WHERE status='processing'")  # resume after restart
    sem = asyncio.Semaphore(config.TRANSCODE_CONCURRENCY)
    running = set()

    async def one(mid):
        async with sem:
            await process(mid)
        running.discard(mid)

    while not stop.is_set():
        free = config.TRANSCODE_CONCURRENCY - len(running)
        if free > 0:
            for row in db.q("SELECT id FROM media WHERE status='queued' ORDER BY id LIMIT ?", free):
                if row["id"] not in running:
                    running.add(row["id"])
                    asyncio.create_task(one(row["id"]))
        try:
            await asyncio.wait_for(stop.wait(), 2)
        except asyncio.TimeoutError:
            pass


def requeue_channel_videos(channel_id):
    """Rate mode / resolution changed: re-encode from the best source we still have."""
    db.run("UPDATE media SET status='queued', progress=0 WHERE channel_id=? AND kind='video' AND status IN ('ready','failed')",
           channel_id)
