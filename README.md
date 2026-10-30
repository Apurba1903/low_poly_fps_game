# FPS Clash

**A LAN-party first-person shooter for the browser.** One computer runs the server; everyone on the
same Wi-Fi / LAN joins from a browser — no installs for players. Team Deathmatch and Free for All,
server-authoritative hit detection, an admin panel to pick the map, mode, and start the match.

Inspired by [Circle Clash](https://github.com/alfesuny/circle-clash), but in 3D first person.

## Setup (host computer, once)

Requires **Python 3.10+**.

```bash
python -m venv .venv

# Windows (PowerShell)
.venv\Scripts\activate
# If PowerShell refuses to run Activate.ps1 ("running scripts is disabled"), run this once:
#   Set-ExecutionPolicy -ExecutionPolicy RemoteSigned -Scope CurrentUser
# or use the batch activator instead:  .venv\Scripts\activate.bat

# macOS / Linux
source .venv/bin/activate

pip install -r requirements.txt
```

## Running the server

```bash
python server.py
```

(or `uvicorn server:app --host 0.0.0.0 --port 8000`)

On startup it prints:

```
  FPS Clash is running
  Players join at:  http://192.168.x.x:8000
  Admin panel:      http://localhost:8000/admin
  Admin password:   3fa9c2
  Maps available:   arena, warehouse
```

- **Players** open `http://<host-ip>:8000`, enter a name, pick a team (or Auto) and click **JOIN GAME**.
  They can walk around the map while waiting in the lobby.
- **Admin** (you) opens `/admin`, logs in with the printed password, chooses a map, game mode, and match length,
  then clicks **START GAME**. You can also reset to the lobby or kick players.

The admin password is random each run unless you set one:

```powershell
# Windows PowerShell
$env:LAN_FPS_ADMIN_PASSWORD = "mypassword"; python server.py
```
```bash
# macOS / Linux
LAN_FPS_ADMIN_PASSWORD=mypassword python server.py
```

Change the port with `PORT=9000` the same way.

### Finding your local IP

The server prints it. To check manually:
- Windows: `ipconfig` → "IPv4 Address" of your Wi-Fi/Ethernet adapter
- macOS: `ipconfig getifaddr en0`
- Linux: `hostname -I`

### Firewall

If other devices can't connect, your firewall is almost certainly blocking it. On Windows, allow Python
when the Defender Firewall prompt appears (tick **Private networks**), or add an inbound rule for
TCP port 8000. Make sure the network profile is *Private*, not *Public*.

### Playing without internet

Three.js (r160) is bundled at `static/three.module.min.js` and served by the game server, so players
need no internet access, and browsers/extensions that block CDNs still work. To restore the file:

```bash
# from the project folder
curl -L -o static/three.module.min.js https://cdnjs.cloudflare.com/ajax/libs/three.js/0.160.0/three.module.min.js
```

## Controls

| Action | Input |
| --- | --- |
| Move | W A S D (or arrow keys) |
| Sprint | Shift |
| Jump | Space (high enough to get on top of a crate) |
| Crouch | Hold C or Ctrl (slower, smaller target, can't jump) |
| Aim | Mouse (click the game once to capture the pointer; Esc releases it) |
| 2× scope | Hold right mouse button with the rifle equipped |
| Shoot / slash | Left click (hold for automatic fire) |
| Weapon | 1 rifle · 2 knife · Q or mouse wheel to swap |
| Reload | R |
| Scoreboard | Hold Tab |

Keyboard and mouse are required; there are no touch controls.

## Rules

- **Team Deathmatch:** 2 teams (A / B), max 7 players each, no friendly fire; most team kills wins.
- **Free for All:** up to 14 players, everyone can damage everyone, with individual kill standings.
- Team changes are allowed in the lobby in Team Deathmatch.
- 100 HP. Rifle does 25 damage (37 on a headshot), fires every 0.15 s, 30-round magazine, 1.8 s reload.
  Headshots are the top 0.35 m of the body, so they sit lower when someone is crouching.
- Knife: 55 damage (two slashes) from the front, instant kill from behind, 1.6 m reach, one slash per 0.5 s.
- No friendly fire. Walls block movement and bullets; crates block movement (unless you jump on them)
  but you can shoot over them. Crouch behind a crate to hide.
- Dead players respawn after 3 s. **No spawn camping:** you never respawn where a living enemy can see
  you or within 10 m of one. Usually that's your team's base; about 30% of the time, and always when
  your base is being camped, it's a random safe spot on your team's side of the map instead.
- Floating red hearts sit at random spots on the map (one per ~60 floor cells, at least 2). Walk into
  one while hurt to go back to full health; it then reappears somewhere else 15 s later.
- The match runs for the admin-set duration; most kills wins.
- The minimap labels visible teammates (and visible opponents in Free for All), marks the cardinal
  directions, and is paired with a heading compass at the top of the screen. Random map-seeded wall
  graffiti marks callout areas such as MID, NORTH EAST, and SOUTH WEST.

## Maps

Maps are plain JSON in `maps/`. Each character is a 2 m cell:

```
#  wall (4 m tall)
c  crate (1.2 m)
A  Team A spawn      B  Team B spawn      .  floor
```

Drop a new file in `maps/` and it shows up in the admin panel on the next server start. Every map needs
at least one `A` and one `B` spawn, and should be enclosed by `#` walls. Put the `A` spawns on one side
and the `B` spawns on the other, out of sight of each other. The bundled maps keep each team in a walled
spawn area with two openings. The server prints a warning if any A spawn can see a B spawn.

## How it works

- `server.py` — FastAPI + WebSockets. One socket per player; a 20 Hz loop broadcasts the world state.
- `game/world.py` — map loading, player–wall collision, ray casting. The movement code is mirrored in
  `static/game.js` so the client can predict its own movement and reconcile with the server.
- `game/state.py` — players, teams, shooting (server-side hitscan), respawns, match timer.
- `static/game.js` — Three.js client: first-person camera with pointer lock, input at a fixed 60 Hz,
  interpolation of other players, HUD, kill feed, scoreboard.

The server is authoritative for positions, hits and scores, so clients can't cheat by editing JS.

**No ESP / wallhacks.** Each tick the server checks line of sight between enemies (walls block it,
crates don't). A player only receives positions — and shot tracers — for teammates and for enemies
they could actually see, so there is nothing hidden in the data for a modified client to reveal.

**Minimap** (top left) shows the map layout, you, and your teammates. Enemies never appear on it.

**Team leaderboard** (top right) shows each team's points with its players ranked by kills / deaths,
leading team first, and the kill feed (who killed who) underneath.

## Tests

```bash
python tests/test_game.py      # fast, no server
pip install websockets
python tests/smoke_test.py     # end to end
```

`test_game.py` checks the bundled maps (bases on opposite sides, never in sight of each other),
jump/crouch physics, the knife and backstab, and that respawns avoid a camping enemy.

`smoke_test.py` starts a server on port 8765 using the small test maps in `tests/maps` (via
`LAN_FPS_MAPS_DIR`), joins two scripted players, starts a match, and checks movement/collision, jump,
crouch, hearts, knife and rifle kills, reset, map switching, that enemies behind a wall are not sent to
the client, and admin auth.
