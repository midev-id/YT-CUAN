"""Self-check: spintax, upload normalization, stream engine (to a local file instead of RTMP),
orchestrator slot cycle against a fake YouTube API.

Run: python -m tests.test_core   (needs ffmpeg/ffprobe on PATH)
"""
import asyncio
import os
import random
import subprocess
import sys
import tempfile
import time
from pathlib import Path

tmp = Path(tempfile.mkdtemp(prefix="ytcuan-test-"))
os.environ["DATA_DIR"] = str(tmp / "data")
os.environ["SECRET_KEY"] = "a" * 43 + "="
out_dir = tmp / "out"
out_dir.mkdir()
os.environ["RTMP_BASE"] = str(out_dir) + os.sep  # stream "key" becomes an output filename
os.environ["JOB_DELAY_SEC"] = "0"

from app import config, crypt, db, orchestrator as orch_mod, spintax, streamer, transcode  # noqa: E402


def ffmpeg(*args):
    subprocess.run([config.FFMPEG, "-y", "-hide_banner", "-loglevel", "error", *args], check=True)


def test_spintax():
    rng = random.Random(1)
    for _ in range(50):
        out = spintax.spin("{Relax|Chill} {in {Jazz|Lounge}|with Sax}!", rng)
        assert "{" not in out and "|" not in out, out
        assert out.split()[0] in ("Relax", "Chill")
    assert spintax.spin("tanpa spintax") == "tanpa spintax"


def probe(path, entries):
    return subprocess.run([config.FFPROBE, "-v", "error", "-show_entries", entries, "-of", "csv=p=0", str(path)],
                          capture_output=True, text=True, check=True).stdout.strip()


async def test_transcode_and_stream():
    db.init()
    cid = db.run("INSERT INTO channels(title,token_ok,settings) VALUES('Test',1,?)", '{"resolution":720}')
    src = tmp / "src"
    src.mkdir()
    # odd sources: 4:3 25fps video with audio, a mono 44.1k mp3
    ffmpeg("-f", "lavfi", "-i", "testsrc=size=640x480:rate=25:duration=3", "-f", "lavfi",
           "-i", "sine=duration=3", "-c:v", "mpeg4", str(src / "a.avi"))
    ffmpeg("-f", "lavfi", "-i", "testsrc2=size=1920x1080:rate=60:duration=3", str(src / "b.mp4"))
    ffmpeg("-f", "lavfi", "-i", "sine=frequency=440:duration=4:sample_rate=44100", "-ac", "1", str(src / "c.mp3"))
    for name, kind in (("a.avi", "video"), ("b.mp4", "video"), ("c.mp3", "audio")):
        db.run("INSERT INTO media(channel_id,kind,orig_name,src_path,created) VALUES(?,?,?,?,?)",
               cid, kind, name, str(src / name), time.time())
    for m in db.q("SELECT id FROM media"):
        await transcode.process(m["id"])
    rows = db.q("SELECT * FROM media")
    assert all(r["status"] == "ready" for r in rows), [(r["orig_name"], r["error"]) for r in rows]
    for r in rows:
        assert not Path(r["src_path"]).name.startswith(("a.", "b.", "c.")), "original should be deleted"
        if r["kind"] == "video":
            assert probe(r["path"], "stream=codec_name,width,height,pix_fmt,r_frame_rate") == "h264,1280,720,yuv420p,30/1"
            assert probe(r["path"], "stream=codec_type") == "video", "video must carry no audio"
        else:
            assert probe(r["path"], "stream=codec_name,sample_rate,channels") == "aac,48000,2"

    # stream engine: copy concat lists to an flv "ingest" for a few seconds
    kid = db.run("INSERT INTO stream_keys(channel_id,yt_stream_id,name,key_enc) VALUES(?,?,?,?)",
                 cid, "s1", "Key A", crypt.enc("live-test.flv"))
    sup = streamer.Supervisor()
    sup.start(kid)
    sup.start(kid)  # second start must not spawn a second ffmpeg
    assert len(sup.tasks) == 1
    for _ in range(40):
        await asyncio.sleep(0.5)
        if db.one("SELECT state FROM stream_keys WHERE id=?", kid)["state"] == "running":
            break
    assert db.one("SELECT state FROM stream_keys WHERE id=?", kid)["state"] == "running"
    await asyncio.sleep(4)
    await sup.stop(kid)
    flv = out_dir / "live-test.flv"
    assert flv.stat().st_size > 10_000
    assert probe(flv, "stream=codec_name").splitlines() == ["h264", "aac"]
    assert db.one("SELECT state FROM stream_keys WHERE id=?", kid)["state"] == "stopped"
    log = (config.LOG_DIR / f"key-{kid}.log").read_text() if (config.LOG_DIR / f"key-{kid}.log").exists() else ""
    assert "live-test.flv" not in log, "stream key must be masked in logs"
    return cid, kid


class FakeYT:
    calls = []
    viewers_value = 0

    def __init__(self, ch):
        pass

    def create_broadcast(self, title, desc, privacy):
        self.calls.append(("insert", title))
        return f"vid{len(self.calls)}"

    def bind(self, b, s): self.calls.append(("bind", b))
    def set_video_meta(self, *a): self.calls.append(("meta", a[0]))
    def set_thumbnail(self, *a): self.calls.append(("thumb", a[0]))
    def stream_status(self, s): return "active"
    def transition(self, b, st): self.calls.append(("transition", b, st))
    def viewers(self, v): return self.viewers_value
    def delete_broadcast(self, b): self.calls.append(("delete", b))


async def test_orchestrator(cid, kid):
    orch_mod.YT = FakeYT
    db.run("UPDATE channels SET settings=? WHERE id=?",
           '{"slots_per_key":3,"jitter_min":0,"jitter_max":0,"cycle_hours":1,"description_template":"{a|b}"}', cid)
    for t in ("Judul 1", "Judul 2", "Judul 3", "Judul 4"):
        db.run("INSERT INTO titles(channel_id,text) VALUES(?,?)", cid, t)
    db.run("UPDATE stream_keys SET state='running', desired='running' WHERE id=?", kid)
    o = orch_mod.Orchestrator()
    for _ in range(3):
        await o.tick()
        await asyncio.sleep(0.3)
    live = db.q("SELECT * FROM broadcasts WHERE state='live'")
    assert len(live) == 3, [dict(r) for r in db.q("SELECT * FROM broadcasts")]
    assert len({b["title"] for b in live}) == 3, "each slot gets its own title"
    await o.tick()
    assert db.one("SELECT COUNT(*) n FROM broadcasts")["n"] == 3, "never more than slots_per_key"

    # cycle over, Filter View on, viewers present -> extended, not ended
    db.run("UPDATE channels SET settings=json_set(settings,'$.filter_view',json('true')) WHERE id=?", cid)
    db.run("UPDATE broadcasts SET ends=? WHERE id=?", time.time() - 1, live[0]["id"])
    FakeYT.viewers_value = 5
    await o.tick(); await asyncio.sleep(0.3)
    assert db.one("SELECT state FROM broadcasts WHERE id=?", live[0]["id"])["state"] == "live"

    # zero viewers -> completed, and a new slot is created with the next title
    db.run("UPDATE broadcasts SET ends=? WHERE id=?", time.time() - 1, live[0]["id"])
    FakeYT.viewers_value = 0
    await o.tick(); await asyncio.sleep(0.3)
    assert db.one("SELECT state FROM broadcasts WHERE id=?", live[0]["id"])["state"] == "complete"
    await o.tick(); await asyncio.sleep(0.3)
    assert db.one("SELECT COUNT(*) n FROM broadcasts WHERE state='live'")["n"] == 3
    assert ("transition", live[0]["yt_id"], "complete") in FakeYT.calls

    # failures: delete the broken broadcast, pause key after KEY_MAX_BROADCAST_FAILS
    def boom(*a): raise RuntimeError("bind gagal")
    FakeYT.bind = boom
    db.run("UPDATE broadcasts SET state='ending' WHERE state='live'")  # manual end of all
    for _ in range(8):
        await o.tick(); await asyncio.sleep(0.3)
    assert db.one("SELECT slots_paused FROM stream_keys WHERE id=?", kid)["slots_paused"] == 1
    assert any(c[0] == "delete" for c in FakeYT.calls)


async def main():
    test_spintax()
    cid, kid = await test_transcode_and_stream()
    await test_orchestrator(cid, kid)
    print("OK", tmp)


if __name__ == "__main__":
    asyncio.run(main())
