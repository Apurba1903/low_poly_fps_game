"""Fast in-process checks (no server): maps, jump/crouch physics, knife, anti-spawn-camp spawning.

Run with:  python tests/test_game.py
"""
import math, sys, time
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))
from game.state import FREE_FOR_ALL, KNIFE, MAX_PLAYERS, SPAWN_SAFE_DIST, Game  # noqa: E402
from game.world import CRATE_HEIGHT, KEY_CROUCH, KEY_JUMP, KEY_W, World  # noqa: E402

maps = {p.stem: World.load(p) for p in sorted((ROOT / "maps").glob("*.json"))}

# --- maps: bases on opposite sides, never in sight of each other
for name, w in maps.items():
    ax = sum(x for x, _ in w.spawns["A"]) / len(w.spawns["A"])
    bx = sum(x for x, _ in w.spawns["B"]) / len(w.spawns["B"])
    assert not w.spawn_leaks(), f"{name}: A and B spawns can see each other: {w.spawn_leaks()[:3]}"
    assert ax < w.width / 3 and bx > w.width * 2 / 3, f"{name}: A should be left, B right ({ax:.0f}, {bx:.0f})"
    print(f"{name}: {len(w.spawns['A'])}+{len(w.spawns['B'])} spawns, none visible across, A left / B right")

# --- physics: jump onto a crate, walk off, crouch is slower and can't jump
w = maps["arena"]
# A crate with open floor directly north and south of it; start south of it, facing north (-z).
crate = next(
    b for b in w.boxes
    if b.kind == "crate" and all(w.rows[int(b.z0 // 2) + d][int(b.x0 // 2)] == "." for d in (-1, 1))
)
cx = (crate.x0 + crate.x1) / 2
x, z, yaw = cx, crate.z1 + 0.5, 0.0
y = vy = 0.0
for _ in range(30):  # walking into it: blocked
    x, y, z, vy = w.move(x, y, z, vy, yaw, KEY_W, 1 / 60)
assert y == 0 and not (crate.z0 < z < crate.z1), (y, z)
for i in range(40):  # jump while walking forward: land on top
    x, y, z, vy = w.move(x, y, z, vy, yaw, KEY_W | (KEY_JUMP if i == 0 else 0), 1 / 60)
assert abs(y - CRATE_HEIGHT) < 1e-9 and crate.z0 < z < crate.z1, ("not on crate", y, z)
for _ in range(60):  # keep walking: fall off the far side back to the floor
    x, y, z, vy = w.move(x, y, z, vy, yaw, KEY_W, 1 / 60)
assert y == 0, y
x0 = x; z0 = z
x1, y1, z1, _ = w.move(x0, 0.0, z0, 0.0, yaw + math.pi, KEY_W | KEY_CROUCH | KEY_JUMP, 0.1)
assert y1 == 0 and abs(math.hypot(x1 - x0, z1 - z0) - 0.25) < 1e-6, "crouch: 2.5 m/s, no jump"
print("physics: crate jump-on / walk-off, crouch speed + no crouch-jump ok")

# --- knife: two front slashes or one backstab
g = Game(world=maps["arena"])
a, b = g.add_player("A1", "A"), g.add_player("B1", "B")
g.phase = "playing"
ox, oz = g.world.spawns["A"][0]
a.x, a.z, a.yaw, a.pitch, a.weapon = ox, oz + 1.2, 0.0, 0.0, KNIFE  # a faces -z, towards b
b.x, b.z, b.yaw = ox, oz, math.pi  # b faces +z, towards a (front)
g.try_knife(a, time.monotonic())
assert b.hp == 45 and b.alive, b.hp
g.try_knife(a, time.monotonic())  # too soon: cooldown
assert b.hp == 45
a.next_knife = 0
g.try_knife(a, time.monotonic())
assert not b.alive and g.events[-1]["w"] == "knife", g.events[-1]
g.spawn(b); b.x, b.z, b.yaw = ox, oz, 0.0  # now b faces away from a
a.next_knife = 0
g.try_knife(a, time.monotonic())
assert not b.alive and g.events[-1]["w"] == "back", g.events[-1]
a.next_knife = 0; g.spawn(b); b.x, b.z = ox, oz - 2.5  # out of reach
g.try_knife(a, time.monotonic())
assert b.hp == 100
print("knife: 2 front slashes / 1 backstab / cooldown / reach ok")

# --- spawns: never in sight of (or close to) a living enemy, and not always the same place
for name, w in maps.items():
    g = Game(world=w)
    a, b = g.add_player("A1", "A"), g.add_player("B1", "B")
    base = set(w.spawns["A"])
    for camp in [w.spawns["A"][0], (w.width / 2, w.depth / 2), w.spawns["A"][-1]]:
        b.x, b.z = camp  # B camps A's base / the middle
        at_base = 0
        for _ in range(300):
            g.spawn(a)
            assert not w.can_see(a.x, a.z, b.x, b.z), f"{name}: spawned in sight of camper at {camp}"
            assert math.hypot(a.x - b.x, a.z - b.z) >= SPAWN_SAFE_DIST, f"{name}: spawned next to camper"
            at_base += (a.x, a.z) in base
        print(f"{name}: camper at ({camp[0]:.0f},{camp[1]:.0f}) -> {at_base}/300 base spawns, rest random safe spots")
        if camp == (w.width / 2, w.depth / 2):
            assert 0 < at_base < 300, "with the base safe, spawns should mix base and random"

# --- free for all: no team cap or friendly fire; individual kills are scored
ffa = Game(world=maps["arena"], mode=FREE_FOR_ALL)
players = [ffa.add_player(f"FFA{i}", "A") for i in range(MAX_PLAYERS)]
assert all(players), "FFA should allow up to 14 players regardless of team assignment"
assert ffa.add_player("OverLimit", "A") is None
ffa.start(60, FREE_FOR_ALL)
a, b = players[:2]
ox, oz = ffa.world.spawns["A"][0]
a.x, a.z, a.yaw, a.pitch = ox, oz + 1.2, 0.0, 0.0
b.x, b.z, b.yaw = ox, oz, math.pi
for p in players[2:]:
    p.x, p.z = ffa.world.width - 3, ffa.world.depth - 3
ffa._update_sight(time.monotonic())
assert b in ffa.visible_to(a), "FFA opponents in sight should be visible even on the same team"
ffa.try_fire(a, time.monotonic())
assert b.hp == 63, "FFA must allow attacks against players assigned the same team"
b.hp = 25
a.next_fire = 0
ffa.try_fire(a, time.monotonic())
assert not b.alive and a.kills == 1 and ffa.scores == {"A": 0, "B": 0}, (b.alive, a.kills, ffa.scores)
print("free for all: 14-player cap, same-team combat, visibility, individual scoring ok")

print("\nALL GAME TESTS PASSED")
