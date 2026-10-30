"""Map loading, collision, raycasting and line of sight.

A map is a JSON file with a grid of characters:
    '#'  wall  (full height, blocks movement, bullets and sight)
    'c'  crate (1.2 m block: blocks movement unless you jump on it; you can shoot/see over it)
    'A'  team A base spawn point (also floor)
    'B'  team B base spawn point (also floor)
    '.'  floor

Put each team's base spawns on opposite sides of the map, out of sight of each other.

The movement code here is mirrored in static/game.js (``moveLocal``) so the
client can predict its own movement; keep the two in sync.
"""

from __future__ import annotations

import json
import math
import random
from collections import deque
from dataclasses import dataclass, field
from pathlib import Path

CELL = 2.0  # metres per grid cell
WALL_HEIGHT = 4.0
CRATE_HEIGHT = 1.2
PLAYER_RADIUS = 0.4
PLAYER_HEIGHT = 1.8
EYE_HEIGHT = 1.6
CROUCH_HEIGHT = 1.2
CROUCH_EYE_HEIGHT = 1.0
HEAD_SIZE = 0.35  # top slice of the hitbox that counts as a headshot
WALK_SPEED = 5.0
SPRINT_SPEED = 7.5
CROUCH_SPEED = 2.5
GRAVITY = 20.0
JUMP_SPEED = 7.5  # ~1.4 m jump: enough to get on top of a crate
STEP_EPS = 1e-3  # feet within this of a box top count as standing on it
MAX_RANGE = 120.0
SIGHT_MARGIN = 0.6  # sideways slack in visibility checks, so a sliver past a corner still counts
SIDE_LIMIT = 0.6  # random spawns may use this fraction of the map, measured from your own base

# Input bitmask (shared with client)
KEY_W, KEY_S, KEY_A, KEY_D, KEY_SPRINT, KEY_JUMP, KEY_CROUCH = 1, 2, 4, 8, 16, 32, 64


def body_height(crouch: bool) -> float:
    return CROUCH_HEIGHT if crouch else PLAYER_HEIGHT


def eye_height(crouch: bool) -> float:
    return CROUCH_EYE_HEIGHT if crouch else EYE_HEIGHT


@dataclass(frozen=True)
class Box:
    """Axis-aligned box standing on the floor (y from 0 to ``h``)."""

    x0: float
    z0: float
    x1: float
    z1: float
    h: float
    kind: str  # "wall" | "crate"


@dataclass
class World:
    """Static geometry for one map."""

    name: str
    width: float
    depth: float
    boxes: list[Box]
    spawns: dict[str, list[tuple[float, float]]]  # base spawn cell centres per team
    rows: list[str] = field(default_factory=list)
    solid: set[tuple[int, int]] = field(default_factory=set)  # wall cells (gx, gz)
    floor: list[tuple[int, int]] = field(default_factory=list)  # reachable plain '.' cells (gx, gz)
    open_cells: list[tuple[float, float]] = field(default_factory=list)  # reachable standable cell centres
    _side: dict[str, list[tuple[float, float]]] = field(default_factory=dict, repr=False)

    # ---------------------------------------------------------------- loading
    @classmethod
    def load(cls, path: Path) -> World:
        """Parse a map JSON file into a ``World``."""
        data = json.loads(path.read_text(encoding="utf-8"))
        rows: list[str] = data["rows"]
        depth_cells = len(rows)
        width_cells = max(len(r) for r in rows)
        boxes: list[Box] = []
        spawns: dict[str, list[tuple[float, float]]] = {"A": [], "B": []}
        solid: set[tuple[int, int]] = set()
        starts: list[tuple[int, int]] = []
        for gz, row in enumerate(rows):
            for gx, ch in enumerate(row):
                x0, z0 = gx * CELL, gz * CELL
                if ch == "#":
                    boxes.append(Box(x0, z0, x0 + CELL, z0 + CELL, WALL_HEIGHT, "wall"))
                    solid.add((gx, gz))
                elif ch == "c":
                    boxes.append(Box(x0, z0, x0 + CELL, z0 + CELL, CRATE_HEIGHT, "crate"))
                elif ch in ("A", "B"):
                    spawns[ch].append((x0 + CELL / 2, z0 + CELL / 2))
                    starts.append((gx, gz))
        if not spawns["A"] or not spawns["B"]:
            raise ValueError(f"map {path.name} needs at least one A and one B spawn")
        reachable = _flood(rows, starts)
        return cls(
            name=data.get("name", path.stem),
            width=width_cells * CELL,
            depth=depth_cells * CELL,
            boxes=boxes,
            spawns=spawns,
            rows=rows,
            solid=solid,
            floor=[c for c in reachable if rows[c[1]][c[0]] == "."],
            open_cells=[((gx + 0.5) * CELL, (gz + 0.5) * CELL) for gx, gz in reachable if rows[gz][gx] != "c"],
        )

    def to_client(self) -> dict[str, object]:
        """Geometry payload sent to clients on join."""
        return {
            "name": self.name,
            "cell": CELL,
            "width": self.width,
            "depth": self.depth,
            "wallHeight": WALL_HEIGHT,
            "crateHeight": CRATE_HEIGHT,
            "boxes": [[b.x0, b.z0, b.x1, b.z1, b.h, b.kind] for b in self.boxes],
        }

    # ---------------------------------------------------------------- spawning
    def facing(self, x: float, z: float) -> float:
        """Yaw that looks from (x, z) towards the centre of the map."""
        return math.atan2(-(self.width / 2 - x), -(self.depth / 2 - z))

    def side_cells(self, team: str) -> list[tuple[float, float]]:
        """Standable cells on ``team``'s part of the map (its base side, up to SIDE_LIMIT across)."""
        if team not in self._side:
            (ax, az), (bx, bz) = _centroid(self.spawns["A"]), _centroid(self.spawns["B"])
            ux, uz = bx - ax, bz - az
            length2 = ux * ux + uz * uz
            cells = []
            for x, z in self.open_cells:
                t = ((x - ax) * ux + (z - az) * uz) / length2 if length2 else 0.5
                if (t <= SIDE_LIMIT) if team == "A" else (t >= 1 - SIDE_LIMIT):
                    cells.append((x, z))
            self._side[team] = cells or list(self.open_cells)
        return self._side[team]

    def spawn_leaks(self) -> list[tuple[tuple[float, float], tuple[float, float]]]:
        """Pairs of A/B base spawns that can see each other (should be empty)."""
        return [(a, b) for a in self.spawns["A"] for b in self.spawns["B"] if self.can_see(*a, *b)]

    def random_floor_spot(self, taken: set[tuple[int, int]]) -> tuple[int, int]:
        """A random plain floor cell not in ``taken`` (falls back to any floor cell)."""
        free = [c for c in self.floor if c not in taken] or self.floor
        return random.choice(free)  # noqa: S311 - not security related

    # --------------------------------------------------------------- movement
    def move(
        self, x: float, y: float, z: float, vy: float, yaw: float, keys: int, dt: float
    ) -> tuple[float, float, float, float]:
        """Advance a player by one input step: walk (sliding along boxes), jump, fall, land.

        ``y`` is the height of the feet; returns the new ``(x, y, z, vy)``.
        """
        crouch = bool(keys & KEY_CROUCH)
        fx, fz = -math.sin(yaw), -math.cos(yaw)  # camera forward (Three.js: -Z at yaw 0)
        rx, rz = math.cos(yaw), -math.sin(yaw)
        mx = mz = 0.0
        if keys & KEY_W:
            mx += fx
            mz += fz
        if keys & KEY_S:
            mx -= fx
            mz -= fz
        if keys & KEY_D:
            mx += rx
            mz += rz
        if keys & KEY_A:
            mx -= rx
            mz -= rz
        length = math.hypot(mx, mz)
        if length > 1e-6:
            speed = CROUCH_SPEED if crouch else SPRINT_SPEED if keys & KEY_SPRINT else WALK_SPEED
            step = speed * dt / length
            x = self._resolve_axis(x + mx * step, z, y, axis="x")
            z = self._resolve_axis(x, z + mz * step, y, axis="z")
        prev_y = y
        ground = self._support(x, z, y)
        if y <= ground + STEP_EPS:
            y = ground
            vy = JUMP_SPEED if keys & KEY_JUMP and not crouch else 0.0
        if vy != 0.0 or y > ground:
            vy -= GRAVITY * dt
            y += vy * dt
            ground = self._support(x, z, y, prev_y=prev_y)
            if y <= ground:
                y, vy = ground, 0.0
        return x, y, z, vy

    def _resolve_axis(self, x: float, z: float, y: float, axis: str) -> float:
        """Push a circle at (x, z) out of any box taller than the feet, along one axis."""
        r = PLAYER_RADIUS
        for b in self.boxes:
            if b.h <= y + STEP_EPS:
                continue  # standing on / above it
            if x + r <= b.x0 or x - r >= b.x1 or z + r <= b.z0 or z - r >= b.z1:
                continue
            if axis == "x":
                x = b.x0 - r if x < (b.x0 + b.x1) / 2 else b.x1 + r
            else:
                z = b.z0 - r if z < (b.z0 + b.z1) / 2 else b.z1 + r
        if axis == "x":
            return min(max(x, r), self.width - r)
        return min(max(z, r), self.depth - r)

    def _support(self, x: float, z: float, y: float, prev_y: float | None = None) -> float:
        """Height of the highest box top under the player that the feet are at or above (else 0)."""
        r = PLAYER_RADIUS
        ref_y = max(y, prev_y) if prev_y is not None else y
        top = 0.0
        for b in self.boxes:
            if top < b.h <= ref_y + STEP_EPS and not (x + r <= b.x0 or x - r >= b.x1 or z + r <= b.z0 or z - r >= b.z1):
                top = b.h
        return top

    # --------------------------------------------------------------- raycast
    def ray_vs_world(self, ox: float, oy: float, oz: float, dx: float, dy: float, dz: float) -> float:
        """Distance along the ray to the first blocking geometry (or MAX_RANGE)."""
        best = MAX_RANGE
        for b in self.boxes:
            t = ray_vs_aabb(ox, oy, oz, dx, dy, dz, b.x0, 0.0, b.z0, b.x1, b.h, b.z1)
            if t is not None and t < best:
                best = t
        # Floor
        if dy < -1e-6:
            best = min(best, -oy / dy)
        return best

    # ------------------------------------------------------------ visibility
    def can_see(self, ax: float, az: float, bx: float, bz: float) -> bool:
        """Could a player at A possibly see any part of a player at B (or vice versa)?

        Walls are taller than anyone's eyes, so this is a 2D test against wall cells only;
        crates are ignored because you can see over them. Lines are cast between the centre
        and both sides of each player, so the result is symmetric and generous at corners.
        """
        dx, dz = bx - ax, bz - az
        dist = math.hypot(dx, dz)
        if dist < 1e-6:
            return True
        px, pz = -dz / dist * SIGHT_MARGIN, dx / dist * SIGHT_MARGIN
        sides = (0.0, 1.0, -1.0)
        return any(
            self._clear_line(ax + px * i, az + pz * i, bx + px * j, bz + pz * j) for i in sides for j in sides
        )

    def _clear_line(self, x0: float, z0: float, x1: float, z1: float) -> bool:
        """Grid walk (Amanatides-Woo) from (x0, z0) to (x1, z1); False if any wall cell is crossed."""
        cx, cz = math.floor(x0 / CELL), math.floor(z0 / CELL)
        ex, ez = math.floor(x1 / CELL), math.floor(z1 / CELL)
        dx, dz = x1 - x0, z1 - z0
        sx = 1 if dx > 0 else -1
        sz = 1 if dz > 0 else -1
        tdx = abs(CELL / dx) if dx else math.inf
        tdz = abs(CELL / dz) if dz else math.inf
        tx = ((cx + (sx > 0)) * CELL - x0) / dx if dx else math.inf
        tz = ((cz + (sz > 0)) * CELL - z0) / dz if dz else math.inf
        for _ in range(abs(ex - cx) + abs(ez - cz) + 1):
            if (cx, cz) in self.solid:
                return False
            if tx < tz:
                tx += tdx
                cx += sx
            else:
                tz += tdz
                cz += sz
        return True


def _flood(rows: list[str], starts: list[tuple[int, int]]) -> list[tuple[int, int]]:
    """All non-wall cells reachable from the spawns (crates count: you can jump on them)."""
    seen = set(starts)
    queue = deque(starts)
    while queue:
        gx, gz = queue.popleft()
        for nx, nz in ((gx + 1, gz), (gx - 1, gz), (gx, gz + 1), (gx, gz - 1)):
            if (nx, nz) in seen or not (0 <= nz < len(rows) and 0 <= nx < len(rows[nz])) or rows[nz][nx] == "#":
                continue
            seen.add((nx, nz))
            queue.append((nx, nz))
    return sorted(seen)


def _centroid(points: list[tuple[float, float]]) -> tuple[float, float]:
    return sum(p[0] for p in points) / len(points), sum(p[1] for p in points) / len(points)


def ray_vs_aabb(
    ox: float,
    oy: float,
    oz: float,
    dx: float,
    dy: float,
    dz: float,
    x0: float,
    y0: float,
    z0: float,
    x1: float,
    y1: float,
    z1: float,
) -> float | None:
    """Slab test. Returns entry distance ``t >= 0`` or ``None`` if no hit."""
    tmin, tmax = 0.0, MAX_RANGE
    for o, d, lo, hi in ((ox, dx, x0, x1), (oy, dy, y0, y1), (oz, dz, z0, z1)):
        if abs(d) < 1e-9:
            if o < lo or o > hi:
                return None
            continue
        inv = 1.0 / d
        t1, t2 = (lo - o) * inv, (hi - o) * inv
        if t1 > t2:
            t1, t2 = t2, t1
        tmin = max(tmin, t1)
        tmax = min(tmax, t2)
        if tmin > tmax:
            return None
    return tmin


def look_dir(yaw: float, pitch: float) -> tuple[float, float, float]:
    """Unit vector the camera is looking along."""
    cp = math.cos(pitch)
    return -math.sin(yaw) * cp, math.sin(pitch), -math.cos(yaw) * cp
