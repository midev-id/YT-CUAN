"""Self-check for the agent <-> hub link against a fake hub: pairing, auth header, state push, RPC.

Run: python -m tests.test_hub
"""
import asyncio
import json
import os
import tempfile
import threading
from http.server import BaseHTTPRequestHandler, HTTPServer
from pathlib import Path

tmp = Path(tempfile.mkdtemp(prefix="ytcuan-hub-"))
os.environ["DATA_DIR"] = str(tmp)
os.environ["SECRET_KEY"] = "a" * 43 + "="
os.environ["HUB_URL"] = "http://127.0.0.1:18765"
os.environ["STATUS_INTERVAL"] = "1"

from websockets.asyncio.server import serve  # noqa: E402

from app import config, db, hub, pair  # noqa: E402

TOKEN = "tok-123"


class PairHandler(BaseHTTPRequestHandler):
    def do_POST(self):
        body = json.loads(self.rfile.read(int(self.headers["Content-Length"])))
        ok = body["code"] == "GOOD"
        self.send_response(200 if ok else 400)
        self.send_header("Content-Type", "application/json")
        self.end_headers()
        self.wfile.write(json.dumps({"token": TOKEN, "name": "vps-test"} if ok else {"detail": "kode kedaluwarsa"}).encode())

    def log_message(self, *a):
        pass


def test_pair():
    srv = HTTPServer(("127.0.0.1", 18765), PairHandler)
    threading.Thread(target=srv.handle_request, daemon=True).start()
    threading.Thread(target=srv.handle_request, daemon=True).start()
    try:
        pair.pair("BAD")
        raise AssertionError("bad code must fail")
    except SystemExit as e:
        assert "kode kedaluwarsa" in str(e), e
    assert pair.pair("GOOD")["name"] == "vps-test"
    assert hub.token() == TOKEN
    srv.server_close()


async def test_link():
    db.init()
    cid = db.run("INSERT INTO channels(title,token_ok) VALUES('Hub Test',1)")
    got = {"states": 0, "results": {}}
    done = asyncio.Event()

    async def handler(ws):
        assert ws.request.path == "/agent"
        if ws.request.headers.get("Authorization") != f"Bearer {TOKEN}":
            await ws.close(4401)
            return
        rpcs = [
            {"type": "rpc", "id": 1, "action": "channel.titles.set", "params": {"channel_id": cid, "titles": ["A", " ", "B"]}},
            {"type": "rpc", "id": 2, "action": "nope"},
            {"type": "rpc", "id": 3, "action": "key.start", "params": {"key_id": 999}},
            {"type": "rpc", "id": 4, "action": "channel.settings", "params": {"channel_id": cid, "settings": {"privacy": "hacked"}}},
            {"type": "rpc", "id": 5, "action": "channel.settings", "params": {"channel_id": cid, "settings": {"cycle_hours": "6", "filter_view": "on"}}},
        ]
        for r in rpcs:
            await ws.send(json.dumps(r))
        async for raw in ws:
            m = json.loads(raw)
            if m["type"] == "state":
                got["states"] += 1
                got["last_state"] = m["data"]
            else:
                got["results"][m["id"]] = m
            if got["states"] >= 2 and len(got["results"]) == len(rpcs):
                done.set()

    stop = asyncio.Event()
    async with serve(handler, "127.0.0.1", 18765):
        task = asyncio.create_task(hub.run(stop))
        await asyncio.wait_for(done.wait(), 15)
        assert hub.status["connected"]
        stop.set()
        task.cancel()

    r = got["results"]
    assert r[1]["ok"] and r[1]["result"]["msg"] == "2 judul disimpan.", r[1]
    assert [t["text"] for t in db.q("SELECT text FROM titles WHERE channel_id=?", cid)] == ["A", "B"]
    assert not r[2]["ok"] and "tidak dikenal" in r[2]["error"]
    assert not r[3]["ok"] and "tidak ditemukan" in r[3]["error"]
    assert not r[4]["ok"] and "Privacy" in r[4]["error"]
    assert r[5]["ok"]
    s = db.settings(db.one("SELECT * FROM channels WHERE id=?", cid))
    assert s["cycle_hours"] == 6.0 and s["filter_view"] is True and s["privacy"] == "public"
    st = got["last_state"]
    assert st["channels"][0]["title"] == "Hub Test" and st["channels"][0]["token_enc"] is None
    assert "system" in st and "quota" in st
    assert any(e["msg"].startswith("Dari hub: channel.titles.set") for e in db.q("SELECT msg FROM events"))


async def main():
    test_pair()
    await test_link()
    print("OK", tmp)


if __name__ == "__main__":
    asyncio.run(main())
