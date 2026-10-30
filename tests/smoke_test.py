"""End-to-end smoke test: boots the server and plays a scripted 1v1.

Uses the small maps in tests/maps so spawns and line of sight are deterministic:
  duel  - one open corridor, A and B can see each other
  split - same, with a wall between them (checks enemies are hidden)
"""
from pathlib import Path
import asyncio, json, math, os, subprocess, sys, time, urllib.request, http.cookiejar
import websockets

ROOT = Path(__file__).resolve().parent.parent
os.environ["LAN_FPS_ADMIN_PASSWORD"] = "testpw"
os.environ["PORT"] = "8765"
os.environ["LAN_FPS_MAPS_DIR"] = str(ROOT / "tests" / "maps")
srv = subprocess.Popen([sys.executable, str(ROOT / "server.py")], cwd=str(ROOT), stdout=subprocess.PIPE, stderr=subprocess.STDOUT, text=True)
time.sleep(2.0)

cj = http.cookiejar.CookieJar()
opener = urllib.request.build_opener(urllib.request.HTTPCookieProcessor(cj))
def post(path, body):
    req = urllib.request.Request(f"http://127.0.0.1:8765{path}", data=json.dumps(body).encode(), headers={"Content-Type": "application/json"})
    return json.loads(opener.open(req).read())
def get(path):
    return json.loads(opener.open(f"http://127.0.0.1:8765{path}").read())

async def main():
    a = await websockets.connect("ws://127.0.0.1:8765/ws")
    b = await websockets.connect("ws://127.0.0.1:8765/ws")
    await a.send(json.dumps({"t": "join", "name": "Alice", "team": "A"}))
    await b.send(json.dumps({"t": "join", "name": "Bob", "team": "B"}))
    wa, wb = json.loads(await a.recv()), json.loads(await b.recv())
    assert wa["t"] == "welcome" and wb["t"] == "welcome", (wa, wb)
    alice_id, bob_id = wa["id"], wb["id"]
    assert wa["map"]["name"] == "duel", wa["map"]["name"]
    print("welcome ok; map", wa["map"]["name"], "boxes", len(wa["map"]["boxes"]))

    async def latest(ws, want_evts=False):
        s = None; evts = []; deadline = time.monotonic() + 0.3
        while time.monotonic() < deadline:
            try:
                m = json.loads(await asyncio.wait_for(ws.recv(), timeout=0.3))
            except asyncio.TimeoutError:
                break
            if m["t"] == "state":
                s = m; evts += m["evts"]
        if want_evts: return s, evts
        return s
    s = await latest(a)
    print("phase", s["phase"], "roster", [p[1] for p in s["roster"]], "visible", [p[1] for p in s["players"]])
    assert {p[0] for p in s["players"]} == {alice_id, bob_id}, "open corridor: Alice should see Bob"

    # admin login + start
    post("/admin/api/login", {"password": "testpw"})
    st = get("/admin/api/status"); print("admin status", st["phase"], st["maps"], [p["name"] for p in st["players"]])
    assert st["maps"] == ["duel", "split"], st["maps"]
    post("/admin/api/start", {"map": "duel", "duration": 60})
    s = await latest(a); assert s["phase"] == "playing", s["phase"]; print("match started, left", s["left"])

    # Movement: walk Alice north (yaw 0 = -z) into the wall; she must stop 0.4 m from it.
    me = s["me"]; x0, z0 = me["x"], me["z"]
    seq = 0
    for _ in range(120):
        seq += 1
        await a.send(json.dumps({"t": "in", "s": seq, "k": 1, "y": 0.0, "p": 0.0, "dt": 1/60}))
    await asyncio.sleep(0.3)
    s = await latest(a); me = s["me"]
    print(f"moved from ({x0:.2f},{z0:.2f}) to ({me['x']:.2f},{me['z']:.2f}) ack={me['ack']}")
    assert me["ack"] == seq
    assert abs(me["z"] - 2.4) < 1e-3, me["z"]

    # Jump: one step with Space, then idle steps -> airborne, then back on the floor.
    async def step(ws, k, n=1, **extra):
        nonlocal seq
        for _ in range(n):
            seq += 1
            await ws.send(json.dumps({"t": "in", "s": seq, "k": k, "y": 0.0, "p": 0.0, "dt": 1/60, **extra}))
    await step(a, 32); await step(a, 0, 9)
    await asyncio.sleep(0.15); s = await latest(a); peak = s["me"]["y"]
    await step(a, 0, 60); await asyncio.sleep(0.15); s = await latest(a)
    print(f"jump: y {peak:.2f} mid-air -> {s['me']['y']:.2f} landed")
    assert peak > 0.5 and s["me"]["y"] == 0, (peak, s["me"]["y"])
    # Crouch: Bob sees Alice's crouch flag (row index 12).
    await step(a, 64); await asyncio.sleep(0.15); sb = await latest(b)
    assert next(p for p in sb["players"] if p[0] == alice_id)[12] == 1, "crouch not visible"
    await step(a, 0); print("crouch ok")

    # Hearts: Bob wounds Alice with one shot, Alice walks the corridor and picks up a heart.
    assert len(s["drops"]) == 2, s["drops"]
    sb = await latest(b)
    alice = next(p for p in sb["players"] if p[0] == alice_id); bob = sb["me"]
    yaw = math.atan2(-(alice[3] - bob["x"]), -(alice[4] - bob["z"]))
    bseq = 1
    await b.send(json.dumps({"t": "in", "s": bseq, "k": 0, "y": yaw, "p": 0.0, "dt": 1/60, "f": True}))
    await asyncio.sleep(0.2); s = await latest(a)
    hurt = s["me"]["hp"]; assert hurt < 100, hurt
    for _ in range(150):  # walk east (yaw -pi/2 = +x) along the whole corridor
        seq += 1
        await a.send(json.dumps({"t": "in", "s": seq, "k": 1, "y": -math.pi / 2, "p": 0.0, "dt": 1/60}))
        await asyncio.sleep(1/120)
    await asyncio.sleep(0.3); s = await latest(a)
    print(f"heart: hp {hurt} -> {s['me']['hp']}, hearts left {len(s['drops'])}, alice at ({s['me']['x']:.2f},{s['me']['z']:.2f}) drops {s['drops']}")
    assert s["me"]["hp"] == 100 and len(s["drops"]) == 1, (s["me"]["hp"], s["drops"])

    # Knife: Alice is now next to Bob; two slashes from the front kill him.
    me = s["me"]; sb = await latest(b); bob = sb["me"]
    kyaw = math.atan2(-(bob["x"] - me["x"]), -(bob["z"] - me["z"]))
    for _ in range(2):
        seq += 1
        await a.send(json.dumps({"t": "in", "s": seq, "k": 0, "y": kyaw, "p": 0.0, "dt": 1/60, "w": 1, "f": True}))
        await asyncio.sleep(0.55)
    s, evts = await latest(a, True)
    kills = [e for e in evts if e["e"] == "kill"]
    print("knife:", kills, "dist", round(math.hypot(bob["x"] - me["x"], bob["z"] - me["z"]), 2))
    assert kills and kills[0]["w"] in ("knife", "back") and s["scores"]["A"] == 1, (kills, s["scores"])
    await asyncio.sleep(3.2)  # Bob respawns

    # Bob shoots Alice down the corridor.
    sb = await latest(b)
    alice = next(p for p in sb["players"] if p[0] == alice_id); bob = sb["me"]
    dx, dz = alice[3] - bob["x"], alice[4] - bob["z"]
    yaw = math.atan2(-dx, -dz)
    print(f"bob respawned at ({bob['x']:.1f},{bob['z']:.1f}), alice at ({alice[3]:.1f},{alice[4]:.1f})")
    for _ in range(60):
        bseq += 1
        await b.send(json.dumps({"t": "in", "s": bseq, "k": 0, "y": yaw, "p": 0.0, "dt": 1/60, "f": True}))
        await asyncio.sleep(1/60)
    await asyncio.sleep(0.3)
    sb, evts = await latest(b, True)
    kill = any(e["e"] == "kill" for e in evts); shots = sum(e["e"] == "shot" for e in evts)
    print("shots seen", shots, "kill", kill, "scores", sb["scores"], "dist", round(math.hypot(dx, dz), 1))
    assert kill and sb["scores"]["B"] == 1, "Bob never killed Alice"

    # team change blocked during play
    await b.send(json.dumps({"t": "team", "team": "A"}))
    sb = await latest(b); assert next(p for p in sb["roster"] if p[0] == bob_id)[2] == "B"
    post("/admin/api/reset", {})
    s = await latest(a); assert s["phase"] == "lobby"; print("reset ok")

    # Switch to the split map: a wall separates the teams, so neither may see the other.
    post("/admin/api/start", {"map": "split", "duration": 45})
    msgs = []; dl = time.monotonic() + 0.4
    while time.monotonic() < dl:
        try: msgs.append(json.loads(await asyncio.wait_for(a.recv(), timeout=0.3)))
        except asyncio.TimeoutError: break
    assert any(m["t"] == "map" and m["map"]["name"] == "split" for m in msgs), [m["t"] for m in msgs]
    print("map switch ok")
    for _ in range(20):  # Bob fires into the wall; Alice must not get those shot events
        bseq += 1
        await b.send(json.dumps({"t": "in", "s": bseq, "k": 0, "y": math.pi / 2, "p": 0.0, "dt": 1/60, "f": True}))
        await asyncio.sleep(1/60)
    s, evts = await latest(a, True)
    sb, bevts = await latest(b, True)
    print("split: Alice sees", [p[1] for p in s["players"]], "roster", [p[1] for p in s["roster"]])
    assert [p[0] for p in s["players"]] == [alice_id], "ESP leak: hidden enemy was sent"
    assert {p[0] for p in s["roster"]} == {alice_id, bob_id}, s["roster"]
    assert not any(e["e"] == "shot" and e["id"] == bob_id for e in evts), "ESP leak: hidden enemy's shots were sent"
    print("enemy hidden behind wall ok")

    await a.close(); await b.close(); await asyncio.sleep(0.3)
    st = get("/admin/api/status"); assert st["players"] == [], st["players"]; print("disconnect cleanup ok")
    # wrong password
    try: post("/admin/api/login", {"password": "nope"}); raise SystemExit("bad login accepted")
    except urllib.error.HTTPError as e: print("bad login rejected", e.code)

try:
    asyncio.run(main())
    print("\nALL SMOKE TESTS PASSED")
finally:
    srv.terminate()
    out = srv.communicate(timeout=5)[0]
    print("--- server log ---"); print(out[-1500:])
