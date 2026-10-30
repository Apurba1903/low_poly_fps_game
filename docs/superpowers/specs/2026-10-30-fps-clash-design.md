# FPS Clash — design (v1)

**Goal.** A browser FPS for LAN parties, hosted from one machine, in the spirit of Circle Clash
(Python server, players join from a browser, admin starts the match).

## Architecture
- **Server** (`server.py`, FastAPI + uvicorn): serves static files, one WebSocket per player,
  admin HTTP API, 20 Hz game loop. Authoritative for position, hits, scores, phases.
- **World** (`game/world.py`): map JSON → list of AABBs; circle-vs-AABB sliding movement; slab raycast.
  Movement is mirrored in the client (`moveLocal`) and verified identical by a differential test.
- **State** (`game/state.py`): `Player`, `Game` (lobby → playing → ended), input application,
  hitscan firing, respawns, snapshots.
- **Client** (`static/game.js`, Three.js r160 via import map): pointer-lock FPS camera, fixed 60 Hz
  input steps with client-side prediction + reconciliation (server acks last seq), 110 ms interpolation
  for other players, tracers/hit markers/damage arrows/kill feed/scoreboard HUD.

## Protocol (JSON)
- C→S `join{name,team}`, `in{s,k,y,p,dt,w,f?,r?}` (`k` bits: W1 S2 A4 D8 sprint16 jump32 crouch64; `w`: 0 rifle, 1 knife), `team{team}`
- S→C `welcome{id,map,phase}`, `map{map}`, `state{phase,left,scores,roster[],players[],evts[],me{...}}`, `error{msg}`
- `roster` row (everyone, no positions): `[id,name,team,kills,deaths]`
- `players` row (only self, teammates and enemies in line of sight): `[id,name,team,x,z,yaw,pitch,hp,alive,kills,deaths,y,crouch,weapon]`
- `me` includes `y`/`vy` (feet height, vertical speed) for prediction of jumps.
- `shot` and `slash` events from enemies the recipient can't see are dropped (no ESP).
- `kill` events carry `w`: `rifle` | `hs` | `knife` | `back`.
- `drops` row (active hearts only): `[id,x,z]`; `me.heal` is true on the snapshot after you picked one up.

## Health drops
Hearts on random `.` floor cells (max(2, floor cells / 60)). Touching one while below 100 HP restores
full HP; checked after every movement step so input bursts can't skip it. A taken heart moves to a new
random cell and reappears after 15 s. Re-scattered on match start, reset and map change.

## Rules
Two teams ×7, 100 HP, 25 dmg (×1.5 head), 0.15 s fire interval, 30-round mag, 1.8 s reload,
3 s respawn, no friendly fire, timer-based match, most kills wins.
Jump (7.5 m/s, gravity 20: ~1.4 m, lands on crates) and crouch (1.2 m hitbox, 1.0 m eye, 2.5 m/s).
Knife: 55 front / 100 backstab (attacker behind the victim's facing), 1.6 m reach, 0.5 s.

## Spawns (anti spawn-camp)
Maps place A bases on one side and B bases on the other, walled so no A spawn sees a B spawn (server
warns otherwise). Match start/reset/map change: free base spawn. Respawn/join: a spot hidden from every
living enemy and ≥10 m away; base first, but 30% of the time (and whenever the base is unsafe) a random
reachable cell on the team's 60% of the map. If nothing is safe, the least-exposed candidate wins.

## Out of scope for v1
Player–player collision, touch controls, lag compensation (LAN only),
multiple weapons, persistent stats.
