"""FPS Clash LAN server.

Run with ``python server.py`` (or ``uvicorn server:app --host 0.0.0.0``).
"""

from __future__ import annotations

import asyncio
import contextlib
import json
import logging
import os
import secrets
import socket
from collections.abc import AsyncIterator
from pathlib import Path
from typing import Any

import uvicorn
from fastapi import Cookie, FastAPI, HTTPException, Request, WebSocket, WebSocketDisconnect
from fastapi.responses import FileResponse, JSONResponse
from fastapi.staticfiles import StaticFiles

from game.state import GAME_MODES, TICK_RATE, Game, Player, admin_status
from game.world import World

ROOT = Path(__file__).resolve().parent
STATIC = ROOT / "static"
MAPS = Path(os.environ.get("LAN_FPS_MAPS_DIR") or ROOT / "maps").resolve()
PORT = int(os.environ.get("PORT", "8000"))
ADMIN_PASSWORD = os.environ.get("LAN_FPS_ADMIN_PASSWORD") or secrets.token_hex(3)
ADMIN_COOKIE = "fps_admin"

logging.basicConfig(level=logging.INFO, format="%(message)s")
log = logging.getLogger("fps")


def list_maps() -> list[str]:
    return sorted(p.stem for p in MAPS.glob("*.json"))


def load_map(name: str) -> World:
    path = MAPS / f"{name}.json"
    if not path.is_file() or path.parent != MAPS:
        raise HTTPException(404, "unknown map")
    world = World.load(path)
    if leaks := world.spawn_leaks():
        log.warning("! map %s: %d A/B spawn pairs can see each other (spawn camping risk)", world.name, len(leaks))
    return world


game = Game(world=load_map(list_maps()[0]))
sockets: dict[int, WebSocket] = {}
admin_tokens: set[str] = set()


# ----------------------------------------------------------------- game loop
async def game_loop() -> None:
    interval = 1.0 / TICK_RATE
    while True:
        await asyncio.sleep(interval)
        game.tick()
        # Take the events before any await, so shots fired mid-broadcast go out next tick
        # instead of reaching only some players.
        events = game.take_events()
        shared = game.snapshot_shared()
        dead: list[int] = []
        for pid, ws in list(sockets.items()):
            player = game.players.get(pid)
            if player is None:
                continue
            payload = game.snapshot_for(player, shared, events)
            try:
                await ws.send_text(json.dumps(payload, separators=(",", ":")))
            except Exception:  # noqa: BLE001 - any send failure means the socket is gone
                dead.append(pid)
        for pid in dead:
            drop_player(pid)


def drop_player(pid: int) -> None:
    p = game.players.get(pid)
    sockets.pop(pid, None)
    game.remove_player(pid)
    if p is not None:
        log.info("- %s left (team %s)", p.name, p.team)


@contextlib.asynccontextmanager
async def lifespan(_: FastAPI) -> AsyncIterator[None]:
    task = asyncio.create_task(game_loop())
    print_banner()
    yield
    task.cancel()


app = FastAPI(lifespan=lifespan)


# ------------------------------------------------------------------- pages
@app.get("/")
async def index() -> FileResponse:
    return FileResponse(STATIC / "index.html")


@app.get("/admin")
async def admin_page() -> FileResponse:
    return FileResponse(STATIC / "admin.html")


@app.get("/api/info")
async def info() -> dict[str, Any]:
    return {
        "phase": game.phase,
        "mode": game.mode,
        "map": game.world.name,
        "teams": {t: game.team_count(t) for t in ("A", "B")},
    }


@app.post("/api/start")
async def start_game() -> dict[str, Any]:
    if game.phase != "playing":
        game.start(300)
        log.info("Match started from lobby")
    return {"ok": True}


# -------------------------------------------------------------- player ws
@app.websocket("/ws")
async def player_ws(ws: WebSocket) -> None:
    await ws.accept()
    player: Player | None = None
    try:
        raw = await asyncio.wait_for(ws.receive_text(), timeout=15)
        hello = json.loads(raw)
        if hello.get("t") != "join":
            await ws.close(code=4000)
            return
        name = str(hello.get("name", "")).strip()[:16] or f"Player{game._next_pid}"  # noqa: SLF001
        player = game.add_player(name, hello.get("team"))
        if player is None:
            await ws.send_text(json.dumps({"t": "error", "msg": "That team is full."}))
            await ws.close(code=4001)
            return
        sockets[player.pid] = ws
        log.info("+ %s joined (team %s)", player.name, player.team)
        await ws.send_text(
            json.dumps(
                {"t": "welcome", "id": player.pid, "map": game.world.to_client(), "phase": game.phase, "mode": game.mode}
            )
        )
        while True:
            msg = json.loads(await ws.receive_text())
            kind = msg.get("t")
            if kind == "in":
                game.apply_input(player, msg)
            elif kind == "team":
                game.change_team(player, str(msg.get("team", "")))
    except (TimeoutError, WebSocketDisconnect, json.JSONDecodeError, ValueError, TypeError):
        pass
    finally:
        if player is not None:
            drop_player(player.pid)


# --------------------------------------------------------------- admin api
def require_admin(token: str | None) -> None:
    if not token or token not in admin_tokens:
        raise HTTPException(401, "not logged in")


@app.post("/admin/api/login")
async def admin_login(req: Request) -> JSONResponse:
    body = await req.json()
    if not secrets.compare_digest(str(body.get("password", "")), ADMIN_PASSWORD):
        raise HTTPException(403, "wrong password")
    token = secrets.token_urlsafe(24)
    admin_tokens.add(token)
    resp = JSONResponse({"ok": True})
    resp.set_cookie(ADMIN_COOKIE, token, httponly=True, samesite="strict")
    return resp


@app.get("/admin/api/status")
async def admin_get_status(fps_admin: str | None = Cookie(default=None)) -> dict[str, Any]:
    require_admin(fps_admin)
    status = admin_status(game)
    status["maps"] = list_maps()
    return status


@app.post("/admin/api/start")
async def admin_start(req: Request, fps_admin: str | None = Cookie(default=None)) -> dict[str, Any]:
    require_admin(fps_admin)
    body = await req.json()
    map_name = str(body.get("map", game.world.name))
    mode = str(body.get("mode", game.mode))
    if mode not in GAME_MODES:
        raise HTTPException(400, "unknown game mode")
    duration = max(30, min(3600, int(body.get("duration", 300))))
    if map_name != game.world.name:
        game.set_world(load_map(map_name))
        await broadcast({"t": "map", "map": game.world.to_client()})
    game.start(duration, mode)
    log.info("Match started on %s in %s for %ss", map_name, mode, duration)
    return {"ok": True}


@app.post("/admin/api/reset")
async def admin_reset(fps_admin: str | None = Cookie(default=None)) -> dict[str, Any]:
    require_admin(fps_admin)
    game.reset()
    log.info("Match reset to lobby")
    return {"ok": True}


@app.post("/admin/api/kick")
async def admin_kick(req: Request, fps_admin: str | None = Cookie(default=None)) -> dict[str, Any]:
    require_admin(fps_admin)
    body = await req.json()
    pid = int(body.get("id", 0))
    ws = sockets.get(pid)
    if ws is not None:
        with contextlib.suppress(Exception):
            await ws.close(code=4002)
    drop_player(pid)
    return {"ok": True}


async def broadcast(payload: dict[str, Any]) -> None:
    text = json.dumps(payload, separators=(",", ":"))
    for ws in list(sockets.values()):
        with contextlib.suppress(Exception):
            await ws.send_text(text)


app.mount("/static", StaticFiles(directory=STATIC), name="static")


# ------------------------------------------------------------------ startup
def local_ip() -> str:
    """Best-effort LAN IP (no packets are actually sent)."""
    s = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    try:
        s.connect(("10.255.255.255", 1))
        return str(s.getsockname()[0])
    except OSError:
        return "127.0.0.1"
    finally:
        s.close()


def print_banner() -> None:
    log.info("")
    log.info("  FPS Clash is running")
    log.info("  Players join at:  http://%s:%s", local_ip(), PORT)
    log.info("  Admin panel:      http://localhost:%s/admin", PORT)
    log.info("  Admin password:   %s", ADMIN_PASSWORD)
    log.info("  Maps available:   %s", ", ".join(list_maps()))
    log.info("")


if __name__ == "__main__":
    uvicorn.run("server:app", host="0.0.0.0", port=PORT, log_level="warning")  # noqa: S104 - LAN server by design
