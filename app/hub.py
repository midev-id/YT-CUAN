"""Outbound link to the hub (PRD §13.3): the agent dials out, so the VPS needs no domain or open port.

Agent -> hub: {"type":"state","data":<state.get>} every STATUS_INTERVAL seconds.
Hub -> agent: {"type":"rpc","id":..,"action":..,"params":{..}}
Agent -> hub: {"type":"rpc_result","id":..,"ok":true,"result":{..}} or {"ok":false,"error":"..."}
"""
import asyncio
import json
import logging

from websockets.asyncio.client import connect
from websockets.exceptions import ConnectionClosed, InvalidStatus

from . import actions, config, db

log = logging.getLogger("ytcuan.hub")
status = {"connected": False, "error": None}


def token():
    f = config.AGENT_TOKEN_FILE
    return f.read_text().strip() if f.exists() else ""


def ws_url():
    return config.HUB_URL.replace("https://", "wss://", 1).replace("http://", "ws://", 1) + "/agent"


async def handle(msg):
    try:
        result = await actions.run(msg["action"], msg.get("params"))
        if msg["action"] not in ("state.get", "channel.detail", "key.log"):
            db.event("info", f"Dari hub: {msg['action']}", (result or {}).get("channel_id"))
        return {"type": "rpc_result", "id": msg.get("id"), "ok": True, "result": result}
    except actions.ActionError as e:
        return {"type": "rpc_result", "id": msg.get("id"), "ok": False, "error": str(e)}
    except Exception as e:  # never let one bad command drop the link
        log.exception("rpc %s", msg.get("action"))
        return {"type": "rpc_result", "id": msg.get("id"), "ok": False, "error": f"{type(e).__name__}: {e}"}


async def _session(ws):
    async def push():
        while True:
            await ws.send(json.dumps({"type": "state", "data": await actions.run("state.get")}, default=str))
            await asyncio.sleep(config.STATUS_INTERVAL)

    pusher = asyncio.create_task(push())
    try:
        async for raw in ws:
            msg = json.loads(raw)
            if msg.get("type") == "rpc":
                # each command in its own task: a slow YouTube call must not stall status pushes
                asyncio.create_task(_reply(ws, msg))
    finally:
        pusher.cancel()


async def _reply(ws, msg):
    try:
        await ws.send(json.dumps(await handle(msg), default=str))
    except ConnectionClosed:
        pass


async def run(stop):
    delay = 1
    while not stop.is_set():
        tok = token()
        if not tok:
            status.update(connected=False, error="Belum dipasangkan ke hub")
            await asyncio.sleep(30)
            continue
        try:
            async with connect(ws_url(), additional_headers={"Authorization": f"Bearer {tok}",
                                                             "X-Agent-Version": config.VERSION},
                               open_timeout=15, ping_interval=20) as ws:
                status.update(connected=True, error=None)
                delay = 1
                await _session(ws)
        except InvalidStatus as e:
            code = e.response.status_code
            status.update(connected=False, error="Token ditolak hub, pasangkan ulang" if code in (401, 403)
                          else f"Hub membalas {code}")
            delay = 300 if code in (401, 403) else min(delay * 2, 60)
        except (OSError, ConnectionClosed, asyncio.TimeoutError) as e:
            status.update(connected=False, error=f"Hub tidak terjangkau ({type(e).__name__})")
            delay = min(delay * 2, 60)
        except asyncio.CancelledError:
            raise
        except Exception as e:
            log.exception("hub link")
            status.update(connected=False, error=str(e))
            delay = min(delay * 2, 60)
        try:
            await asyncio.wait_for(stop.wait(), delay)
        except asyncio.TimeoutError:
            pass
