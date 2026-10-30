"""Authoritative match state: players, teams, shooting, knifing, spawns, timer."""

from __future__ import annotations

import math
import random
import time
from dataclasses import dataclass, field
from typing import Any

from .world import (
    CELL,
    HEAD_SIZE,
    KEY_CROUCH,
    MAX_RANGE,
    PLAYER_RADIUS,
    World,
    body_height,
    eye_height,
    look_dir,
    ray_vs_aabb,
)

MAX_PER_TEAM = 7
MAX_PLAYERS = MAX_PER_TEAM * 2
MAX_HP = 100
DAMAGE = 25
HEADSHOT_MULT = 1.5
FIRE_INTERVAL = 0.15
MAG_SIZE = 30
RELOAD_TIME = 1.8
KNIFE_DAMAGE = 55  # two slashes from the front
KNIFE_BACKSTAB = 100  # one slash from behind
KNIFE_INTERVAL = 0.5
KNIFE_RANGE = 1.6  # metres from the eye
KNIFE_PAD = 0.25  # knife hitboxes are a bit fatter, so it doesn't need pixel aim
RIFLE, KNIFE = 0, 1
RESPAWN_TIME = 3.0
SPAWN_SAFE_DIST = 10.0  # never spawn closer than this to a living enemy, if avoidable
RANDOM_SPAWN_CHANCE = 0.3  # how often a respawn ignores the base and picks a random safe spot
SPAWN_CANDIDATES = 40  # cells tried per pool, to bound the cost of a spawn
MAX_INPUT_DT = 0.1
TICK_RATE = 20
SIGHT_GRACE = 0.3  # keep sending an enemy this long after losing sight, so corners don't flicker
HEAL_CELLS_PER_DROP = 60  # one heart per this many floor cells (minimum 2 per map)
HEAL_RADIUS = 1.0
HEAL_RESPAWN = 15.0  # seconds before a taken heart reappears somewhere else

TEAMS = ("A", "B")
TEAM_DEATHMATCH = "team_deathmatch"
FREE_FOR_ALL = "free_for_all"
GAME_MODES = (TEAM_DEATHMATCH, FREE_FOR_ALL)


@dataclass
class HealthDrop:
    """A heart pickup that restores full HP. Inactive until ``ready_at``."""

    did: int
    x: float
    z: float
    ready_at: float = 0.0

    def active(self, now: float) -> bool:
        return now >= self.ready_at


@dataclass
class Player:
    """One connected player."""

    pid: int
    name: str
    team: str
    x: float = 0.0
    y: float = 0.0  # feet height
    z: float = 0.0
    vy: float = 0.0
    yaw: float = 0.0
    pitch: float = 0.0
    crouch: bool = False
    weapon: int = RIFLE
    hp: int = MAX_HP
    alive: bool = True
    kills: int = 0
    deaths: int = 0
    ammo: int = MAG_SIZE
    reload_until: float = 0.0
    next_fire: float = 0.0
    next_knife: float = 0.0
    respawn_at: float = 0.0
    last_seq: int = 0
    # Per-tick personal events, flushed into the next snapshot.
    hit_marker: bool = False
    healed: bool = False
    damage_dirs: list[float] = field(default_factory=list)

    @property
    def reloading(self) -> bool:
        return self.reload_until > time.monotonic()

    def public(self) -> list[Any]:
        """Compact array form used in snapshots."""
        return [
            self.pid,
            self.name,
            self.team,
            round(self.x, 2),
            round(self.z, 2),
            round(self.yaw, 2),
            round(self.pitch, 2),
            self.hp,
            1 if self.alive else 0,
            self.kills,
            self.deaths,
            round(self.y, 2),
            1 if self.crouch else 0,
            self.weapon,
        ]

    def hitbox(self, pad: float = 0.0) -> tuple[float, float, float, float, float, float]:
        r = PLAYER_RADIUS + pad
        return (self.x - r, self.y, self.z - r, self.x + r, self.y + body_height(self.crouch), self.z + r)


@dataclass
class Game:
    """The whole match: lobby -> playing -> ended."""

    world: World
    phase: str = "lobby"
    mode: str = TEAM_DEATHMATCH
    duration: float = 300.0
    ends_at: float = 0.0
    scores: dict[str, int] = field(default_factory=lambda: {"A": 0, "B": 0})
    players: dict[int, Player] = field(default_factory=dict)
    events: list[dict[str, Any]] = field(default_factory=list)
    # Last time each enemy pair had line of sight, keyed by (lower pid, higher pid).
    _seen: dict[tuple[int, int], float] = field(default_factory=dict)
    drops: list[HealthDrop] = field(default_factory=list)
    _next_pid: int = 1
    _next_did: int = 1

    def __post_init__(self) -> None:
        self.place_drops()

    # ------------------------------------------------------------ players
    def team_count(self, team: str) -> int:
        return sum(1 for p in self.players.values() if p.team == team)

    def smallest_team(self) -> str:
        return "A" if self.team_count("A") <= self.team_count("B") else "B"

    def add_player(self, name: str, team: str | None) -> Player | None:
        """Create a player, respecting the active mode's player limit."""
        if team not in TEAMS:
            team = self.smallest_team()
        if self.mode == FREE_FOR_ALL:
            if len(self.players) >= MAX_PLAYERS:
                return None
        elif self.team_count(team) >= MAX_PER_TEAM:
            return None
        pid = self._next_pid
        self._next_pid += 1
        p = Player(pid=pid, name=name, team=team)
        self.players[pid] = p
        self.spawn(p)
        return p

    def remove_player(self, pid: int) -> None:
        self.players.pop(pid, None)
        self._forget_sight(pid)

    def change_team(self, p: Player, team: str) -> bool:
        if self.phase == "playing" or team not in TEAMS or team == p.team:
            return False
        if self.team_count(team) >= MAX_PER_TEAM:
            return False
        p.team = team
        self._forget_sight(p.pid)
        self.spawn(p)
        return True

    # ------------------------------------------------------------ spawning
    def spawn(self, p: Player, at_base: bool = False) -> None:
        p.x, p.z = self.pick_spawn(p, at_base)
        p.yaw = self.world.facing(p.x, p.z)
        p.y = p.vy = 0.0
        p.pitch = 0.0
        p.crouch = False
        p.hp = MAX_HP
        p.alive = True
        p.ammo = MAG_SIZE
        p.reload_until = 0.0
        p.next_fire = 0.0
        p.next_knife = 0.0

    def pick_spawn(self, p: Player, at_base: bool = False) -> tuple[float, float]:
        """Choose where ``p`` (re)spawns.

        ``at_base`` (match start / reset / map change): a free base spawn of their team.
        Otherwise the spot must be out of sight of every living enemy and at least
        SPAWN_SAFE_DIST from them. Usually that's a base spawn, but RANDOM_SPAWN_CHANCE of
        the time (and whenever the base is unsafe, i.e. being camped) it's a random safe
        cell on the team's side of the map. If nothing is safe, take the least-exposed spot.
        """
        if self.mode == FREE_FOR_ALL:
            others = [(q.x, q.z) for q in self.players.values() if q is not p and q.alive]
            cells = self.world.open_cells
            if not others:
                return random.choice(cells)  # noqa: S311
            best, best_score = cells[0], (False, -math.inf)
            for c in random.sample(cells, min(len(cells), SPAWN_CANDIDATES)):
                nearest = min(math.hypot(c[0] - x, c[1] - z) for x, z in others)
                hidden = not any(self.world.can_see(c[0], c[1], x, z) for x, z in others)
                if hidden and nearest >= SPAWN_SAFE_DIST:
                    return c
                if (hidden, nearest) > best_score:
                    best, best_score = c, (hidden, nearest)
            return best

        base = self.world.spawns[p.team]
        mates = [(q.x, q.z) for q in self.players.values() if q.team == p.team and q is not p and q.alive]
        enemies = [] if at_base else [(q.x, q.z) for q in self.players.values() if q.team != p.team and q.alive]

        def free(c: tuple[float, float]) -> bool:
            return all(math.hypot(c[0] - m[0], c[1] - m[1]) > 1.0 for m in mates)

        if not enemies:
            return random.choice([c for c in base if free(c)] or base)  # noqa: S311
        pools = [base, self.world.side_cells(p.team)]
        if random.random() < RANDOM_SPAWN_CHANCE:  # noqa: S311
            pools.reverse()
        best, best_score = base[0], (False, -math.inf)
        for pool in pools:
            for c in random.sample(pool, min(len(pool), SPAWN_CANDIDATES)):
                nearest = min(math.hypot(c[0] - e[0], c[1] - e[1]) for e in enemies)
                hidden = not any(self.world.can_see(c[0], c[1], e[0], e[1]) for e in enemies)
                if hidden and nearest >= SPAWN_SAFE_DIST and free(c):
                    return c
                if (hidden, nearest) > best_score:
                    best, best_score = c, (hidden, nearest)
        return best

    # ------------------------------------------------------------ match
    def start(self, duration: float, mode: str | None = None) -> None:
        if mode is not None:
            if mode not in GAME_MODES:
                raise ValueError(f"unknown game mode: {mode}")
            self.mode = mode
        self.duration = duration
        self.phase = "playing"
        self.ends_at = time.monotonic() + duration
        self.scores = {"A": 0, "B": 0}
        self._seen.clear()
        self.place_drops()
        for p in self.players.values():
            p.kills = p.deaths = 0
            self.spawn(p, at_base=True)
        self.events.append({"e": "start", "duration": duration, "mode": self.mode})

    def reset(self) -> None:
        self.phase = "lobby"
        self.scores = {"A": 0, "B": 0}
        self._seen.clear()
        self.place_drops()
        for p in self.players.values():
            p.kills = p.deaths = 0
            self.spawn(p, at_base=True)
        self.events.append({"e": "reset"})

    def set_world(self, world: World) -> None:
        self.world = world
        self._seen.clear()
        self.place_drops()
        for p in self.players.values():
            self.spawn(p, at_base=True)

    # ------------------------------------------------------------ health drops
    def place_drops(self) -> None:
        """Scatter a fresh set of hearts over random floor cells."""
        self.drops = []
        for _ in range(max(2, len(self.world.floor) // HEAL_CELLS_PER_DROP)):
            drop = HealthDrop(did=self._next_did, x=0.0, z=0.0)
            self._next_did += 1
            self._move_drop(drop)
            self.drops.append(drop)

    def _move_drop(self, drop: HealthDrop) -> None:
        taken = {(int(d.x // CELL), int(d.z // CELL)) for d in self.drops if d is not drop}
        gx, gz = self.world.random_floor_spot(taken)
        drop.x, drop.z = (gx + 0.5) * CELL, (gz + 0.5) * CELL

    def _pick_up_drops(self, p: Player, now: float) -> None:
        """Called after every movement step, so a burst of inputs can't skip over a heart."""
        if not p.alive or p.hp >= MAX_HP:
            return
        for drop in self.drops:
            if drop.active(now) and math.hypot(p.x - drop.x, p.z - drop.z) <= HEAL_RADIUS:
                p.hp = MAX_HP
                p.healed = True
                drop.ready_at = now + HEAL_RESPAWN
                self._move_drop(drop)  # reappears somewhere else
                return

    def time_left(self) -> float:
        if self.phase != "playing":
            return self.duration
        return max(0.0, self.ends_at - time.monotonic())

    # ------------------------------------------------------------ input
    def apply_input(self, p: Player, msg: dict[str, Any]) -> None:
        """Apply one client input packet (movement + look + weapon + fire/reload)."""
        seq = int(msg.get("s", 0))
        if seq <= p.last_seq:
            return
        p.last_seq = seq
        p.yaw = _num(msg, "y", p.yaw)
        p.pitch = max(-1.55, min(1.55, _num(msg, "p", p.pitch)))
        p.weapon = KNIFE if msg.get("w") == KNIFE else RIFLE
        if not p.alive:
            return
        keys = int(msg.get("k", 0))
        dt = max(0.0, min(MAX_INPUT_DT, _num(msg, "dt", 0.0)))
        p.crouch = bool(keys & KEY_CROUCH)
        p.x, p.y, p.z, p.vy = self.world.move(p.x, p.y, p.z, p.vy, p.yaw, keys, dt)
        now = time.monotonic()
        self._pick_up_drops(p, now)
        if msg.get("r") and p.ammo < MAG_SIZE and not p.reloading:
            p.reload_until = now + RELOAD_TIME
        if msg.get("f"):
            if p.weapon == KNIFE:
                self.try_knife(p, now)
            else:
                self.try_fire(p, now)

    def _trace(
        self, attacker: Player, max_t: float, pad: float = 0.0
    ) -> tuple[tuple[float, float, float], tuple[float, float, float], float, Player | None]:
        """Cast the attacker's view ray; return origin, direction, hit distance and enemy hit (if any)."""
        ox, oy, oz = attacker.x, attacker.y + eye_height(attacker.crouch), attacker.z
        dx, dy, dz = look_dir(attacker.yaw, attacker.pitch)
        best_t = min(max_t, self.world.ray_vs_world(ox, oy, oz, dx, dy, dz))
        victim: Player | None = None
        for p in self.players.values():
            if p.pid == attacker.pid or not self._are_enemies(attacker, p) or not p.alive:
                continue
            t = ray_vs_aabb(ox, oy, oz, dx, dy, dz, *p.hitbox(pad))
            if t is not None and t < best_t:
                best_t, victim = t, p
        return (ox, oy, oz), (dx, dy, dz), best_t, victim

    def try_fire(self, shooter: Player, now: float) -> None:
        if now < shooter.next_fire:
            return
        if shooter.reloading:
            return
        if shooter.reload_until and shooter.reload_until <= now:
            shooter.ammo = MAG_SIZE
            shooter.reload_until = 0.0
        if shooter.ammo <= 0:
            shooter.reload_until = now + RELOAD_TIME
            return
        shooter.ammo -= 1
        shooter.next_fire = now + FIRE_INTERVAL
        if shooter.ammo == 0:
            shooter.reload_until = now + RELOAD_TIME

        (ox, oy, oz), (dx, dy, dz), best_t, victim = self._trace(shooter, MAX_RANGE)
        hit_y = oy + dy * best_t
        hit_id = victim.pid if (victim and self.phase == "playing") else 0
        self.events.append(
            {
                "e": "shot",
                "id": shooter.pid,
                "to": [round(ox + dx * best_t, 2), round(hit_y, 2), round(oz + dz * best_t, 2)],
                "hit": hit_id,
            }
        )
        if self.phase != "playing" or victim is None:
            return
        headshot = hit_y > victim.y + body_height(victim.crouch) - HEAD_SIZE
        self.damage(shooter, victim, int(DAMAGE * (HEADSHOT_MULT if headshot else 1.0)), "hs" if headshot else "rifle")

    def try_knife(self, attacker: Player, now: float) -> None:
        if now < attacker.next_knife:
            return
        attacker.next_knife = now + KNIFE_INTERVAL
        _, _, _, victim = self._trace(attacker, KNIFE_RANGE, pad=KNIFE_PAD)
        hit_id = victim.pid if (victim and self.phase == "playing") else 0
        self.events.append({"e": "slash", "id": attacker.pid, "hit": hit_id})
        if self.phase != "playing" or victim is None:
            return
        # Behind = the attacker is on the far side of the way the victim is facing.
        fx, fz = -math.sin(victim.yaw), -math.cos(victim.yaw)
        ax, az = attacker.x - victim.x, attacker.z - victim.z
        dist = math.hypot(ax, az) or 1.0
        backstab = (fx * ax + fz * az) / dist < -0.3
        self.damage(attacker, victim, KNIFE_BACKSTAB if backstab else KNIFE_DAMAGE, "back" if backstab else "knife")

    def damage(self, attacker: Player, victim: Player, amount: int, how: str) -> None:
        victim.hp = max(0, victim.hp - amount)
        victim.damage_dirs.append(math.atan2(attacker.x - victim.x, attacker.z - victim.z))
        attacker.hit_marker = True
        if victim.hp == 0:
            self.kill(attacker, victim, how)

    def kill(self, killer: Player, victim: Player, how: str) -> None:
        """``how``: "rifle" | "hs" (headshot) | "knife" | "back" (backstab)."""
        victim.alive = False
        victim.deaths += 1
        victim.respawn_at = time.monotonic() + RESPAWN_TIME
        killer.kills += 1
        if self.mode == TEAM_DEATHMATCH:
            self.scores[killer.team] += 1
        self.events.append(
            {"e": "kill", "k": killer.name, "kt": killer.team, "v": victim.name, "vt": victim.team, "w": how}
        )

    # ------------------------------------------------------------ tick
    def tick(self) -> None:
        """Called at TICK_RATE: respawns, reload completion, match timer."""
        now = time.monotonic()
        for p in self.players.values():
            if not p.alive and now >= p.respawn_at:
                self.spawn(p)
            if p.reload_until and p.reload_until <= now:
                p.ammo = MAG_SIZE
                p.reload_until = 0.0
        if self.phase == "playing" and now >= self.ends_at:
            self.phase = "ended"
            self.events.append({"e": "end", "scores": dict(self.scores)})
        self._update_sight(now)

    # ------------------------------------------------------------ visibility
    def _update_sight(self, now: float) -> None:
        """Record which living enemy pairs currently have line of sight."""
        alive = [p for p in self.players.values() if p.alive]
        for i, a in enumerate(alive):
            for b in alive[i + 1 :]:
                if not self._are_enemies(a, b):
                    continue
                if self.world.can_see(a.x, a.z, b.x, b.z):
                    self._seen[_pair(a.pid, b.pid)] = now

    def _are_enemies(self, a: Player, b: Player) -> bool:
        return self.mode == FREE_FOR_ALL or a.team != b.team

    def _forget_sight(self, pid: int) -> None:
        for key in [k for k in self._seen if pid in k]:
            del self._seen[key]

    def visible_to(self, viewer: Player) -> list[Player]:
        """Players whose position ``viewer`` may receive: teammates, plus enemies in sight.

        Hidden enemies are never sent, so a modified client has nothing to draw (no ESP).
        """
        now = time.monotonic()
        return [
            p
            for p in self.players.values()
            if p.pid == viewer.pid
            or (
                not self._are_enemies(viewer, p)
                or (p.alive and now - self._seen.get(_pair(viewer.pid, p.pid), -math.inf) <= SIGHT_GRACE)
            )
        ]

    # ------------------------------------------------------------ snapshots
    def take_events(self) -> list[dict[str, Any]]:
        """Hand over this tick's events and start a fresh list."""
        events, self.events = self.events, []
        return events

    def snapshot_shared(self) -> dict[str, Any]:
        """The part of the snapshot that is the same for everyone (no positions)."""
        return {
            "t": "state",
            "phase": self.phase,
            "mode": self.mode,
            "left": round(self.time_left(), 1),
            "scores": self.scores,
            "roster": [[p.pid, p.name, p.team, p.kills, p.deaths] for p in self.players.values()],
            "drops": [[d.did, d.x, d.z] for d in self.drops if d.active(time.monotonic())],
        }

    def snapshot_for(self, viewer: Player, shared: dict[str, Any], events: list[dict[str, Any]]) -> dict[str, Any]:
        """Full snapshot for one player: only what they are allowed to see."""
        shown = self.visible_to(viewer)
        ids = {p.pid for p in shown}
        payload = dict(shared)
        payload["players"] = [p.public() for p in shown]
        payload["evts"] = [e for e in events if e["e"] not in ("shot", "slash") or e["id"] in ids]
        payload["me"] = self.personal(viewer)
        return payload

    def personal(self, p: Player) -> dict[str, Any]:
        """Per-player fields appended to the snapshot, then cleared."""
        me = {
            "ack": p.last_seq,
            "x": round(p.x, 3),
            "y": round(p.y, 4),
            "z": round(p.z, 3),
            "vy": round(p.vy, 4),
            "hp": p.hp,
            "ammo": p.ammo,
            "rel": round(max(0.0, p.reload_until - time.monotonic()), 2) if p.reloading else 0,
            "resp": round(max(0.0, p.respawn_at - time.monotonic()), 1) if not p.alive else 0,
            "hm": p.hit_marker,
            "heal": p.healed,
            "dmg": [round(d, 2) for d in p.damage_dirs],
        }
        p.hit_marker = False
        p.healed = False
        p.damage_dirs.clear()
        return me


def _pair(a: int, b: int) -> tuple[int, int]:
    return (a, b) if a < b else (b, a)


def _num(msg: dict[str, Any], key: str, default: float) -> float:
    """A finite float from the message, else ``default`` (JSON allows NaN/Infinity, which would poison state)."""
    try:
        value = float(msg.get(key, default))
    except (TypeError, ValueError):
        return default
    return value if math.isfinite(value) else default


def admin_status(game: Game) -> dict[str, Any]:
    """Status payload for the admin panel."""
    return {
        "phase": game.phase,
        "mode": game.mode,
        "map": game.world.name,
        "left": round(game.time_left()),
        "scores": game.scores,
        "players": [
            {"id": p.pid, "name": p.name, "team": p.team, "k": p.kills, "d": p.deaths} for p in game.players.values()
        ],
    }


__all__ = ["Game", "Player", "admin_status", "MAX_RANGE", "TICK_RATE"]
