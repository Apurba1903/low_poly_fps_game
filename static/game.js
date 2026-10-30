import * as THREE from "three";

// ----------------------------------------------------------------- constants
// Keep in sync with game/world.py
const PLAYER_RADIUS = 0.4, EYE_HEIGHT = 1.6, CROUCH_EYE_HEIGHT = 1.0;
const WALK_SPEED = 5.0, SPRINT_SPEED = 7.5, CROUCH_SPEED = 2.5;
const GRAVITY = 20.0, JUMP_SPEED = 7.5, STEP_EPS = 1e-3;
const KEY = { W: 1, S: 2, A: 4, D: 8, SPRINT: 16, JUMP: 32, CROUCH: 64 };
const RIFLE = 0, KNIFE = 1;
const STEP = 1 / 60;            // fixed input step
const INTERP_DELAY = 110;       // ms behind server for other players
const SENS = 0.0022;
const TEAM_COLOR = { A: 0xff5c5c, B: 0x4da3ff };
const TEAM_CSS = { A: "#ff5c5c", B: "#4da3ff" };
const MINIMAP_SIZE = 200;       // px, longest side

// ----------------------------------------------------------------- DOM
const $ = (id) => document.getElementById(id);
const ui = {
  join: $("join"), name: $("name"), go: $("go"), err: $("err"), hud: $("hud"), cross: $("cross"),
  sA: $("sA"), sB: $("sB"), timer: $("timer"), feed: $("feed"), banner: $("banner"),
  hptext: $("hptext"), hpfill: $("hpfill"), ammo: $("ammo"), score: $("score"),
  scoreBody: $("score").querySelector("tbody"), lobbybar: $("lobbybar"), vignette: $("vignette"),
  dmgdir: $("dmgdir"), clickhint: $("clickhint"), minimap: $("minimap"), board: $("board"), healfx: $("healfx"),
};
let chosenTeam = "auto";
document.querySelectorAll(".teams button").forEach((b) => b.addEventListener("click", () => {
  document.querySelectorAll(".teams button").forEach((x) => x.classList.remove("sel"));
  b.classList.add("sel"); chosenTeam = b.dataset.team;
}));
ui.name.value = localStorage.getItem("fps_name") || "";
ui.name.addEventListener("keydown", (e) => { if (e.key === "Enter") ui.go.click(); });
ui.go.addEventListener("click", () => {
  const name = ui.name.value.trim();
  if (!name) { ui.err.textContent = "Enter a name first."; return; }
  localStorage.setItem("fps_name", name);
  ui.go.disabled = true; ui.err.textContent = "";
  connect(name, chosenTeam === "auto" ? null : chosenTeam);
});
ui.lobbybar.querySelectorAll("button").forEach((b) => b.addEventListener("click", () => send({ t: "team", team: b.dataset.team })));

// ----------------------------------------------------------------- three setup
const renderer = new THREE.WebGLRenderer({ antialias: true, powerPreference: "high-performance" });
renderer.setPixelRatio(Math.min(devicePixelRatio, 2));
renderer.setSize(innerWidth, innerHeight);
renderer.domElement.tabIndex = 0;
renderer.domElement.setAttribute("aria-label", "FPS Clash game view");
renderer.outputColorSpace = THREE.SRGBColorSpace;
document.body.appendChild(renderer.domElement);
const scene = new THREE.Scene();
scene.background = new THREE.Color(0x9fc3e6);
scene.fog = new THREE.Fog(0x9fc3e6, 40, 110);
const camera = new THREE.PerspectiveCamera(75, innerWidth / innerHeight, 0.05, 300);
camera.rotation.order = "YXZ";
scene.add(new THREE.HemisphereLight(0xdfe9f5, 0x55503f, 1.1));
const sun = new THREE.DirectionalLight(0xfff2d8, 1.3); sun.position.set(30, 60, 20); scene.add(sun);
addEventListener("resize", () => {
  camera.aspect = innerWidth / innerHeight; camera.updateProjectionMatrix(); renderer.setSize(innerWidth, innerHeight);
});

// Own weapon, attached to camera.
const gun = new THREE.Group();
{
  const dark = new THREE.MeshStandardMaterial({ color: 0x2a2d33, roughness: 0.6, metalness: 0.4 });
  const body = new THREE.Mesh(new THREE.BoxGeometry(0.08, 0.1, 0.5), dark); body.position.set(0, 0, -0.1);
  const barrel = new THREE.Mesh(new THREE.CylinderGeometry(0.018, 0.018, 0.35, 10), dark);
  barrel.rotation.x = Math.PI / 2; barrel.position.set(0, 0.035, -0.5);
  const grip = new THREE.Mesh(new THREE.BoxGeometry(0.06, 0.16, 0.08), new THREE.MeshStandardMaterial({ color: 0x5a3b22 }));
  grip.position.set(0, -0.12, 0.05); grip.rotation.x = 0.3;
  const stock = new THREE.Mesh(new THREE.BoxGeometry(0.06, 0.09, 0.25), new THREE.MeshStandardMaterial({ color: 0x5a3b22 }));
  stock.position.set(0, -0.02, 0.27);
  gun.add(body, barrel, grip, stock);
  gun.position.set(0.22, -0.2, -0.35);
  camera.add(gun);
}
const flash = new THREE.Mesh(new THREE.SphereGeometry(0.06, 8, 8), new THREE.MeshBasicMaterial({ color: 0xfff0a0 }));
flash.position.set(0, 0.035, -0.7); flash.visible = false; gun.add(flash);
// Knife, also on the camera; only one of gun / knife is visible at a time.
const knife = new THREE.Group();
{
  const blade = new THREE.Mesh(new THREE.BoxGeometry(0.025, 0.05, 0.28), new THREE.MeshStandardMaterial({ color: 0xd8dde3, roughness: 0.25, metalness: 0.9 }));
  blade.position.set(0, 0, -0.2);
  const guard = new THREE.Mesh(new THREE.BoxGeometry(0.06, 0.07, 0.02), new THREE.MeshStandardMaterial({ color: 0x2a2d33 }));
  guard.position.set(0, 0, -0.055);
  const handle = new THREE.Mesh(new THREE.BoxGeometry(0.035, 0.045, 0.12), new THREE.MeshStandardMaterial({ color: 0x3b2a1c }));
  handle.position.set(0, 0, 0.02);
  knife.add(blade, guard, handle);
  knife.visible = false;
  camera.add(knife);
}
scene.add(camera);

// Map geometry
let mapGroup = null, boxes = [], mapW = 0, mapD = 0;
function buildMap(m) {
  if (mapGroup) scene.remove(mapGroup);
  mapGroup = new THREE.Group();
  boxes = m.boxes; mapW = m.width; mapD = m.depth;
  // floor
  const c = document.createElement("canvas"); c.width = c.height = 128;
  const g = c.getContext("2d"); g.fillStyle = "#6f7a86"; g.fillRect(0, 0, 128, 128);
  g.fillStyle = "#657079"; g.fillRect(0, 0, 64, 64); g.fillRect(64, 64, 64, 64);
  g.strokeStyle = "#50585f"; g.lineWidth = 2; g.strokeRect(1, 1, 126, 126);
  const tex = new THREE.CanvasTexture(c); tex.wrapS = tex.wrapT = THREE.RepeatWrapping;
  tex.repeat.set(m.width / m.cell, m.depth / m.cell); tex.colorSpace = THREE.SRGBColorSpace;
  const floor = new THREE.Mesh(new THREE.PlaneGeometry(m.width, m.depth), new THREE.MeshStandardMaterial({ map: tex, roughness: 0.95 }));
  floor.rotation.x = -Math.PI / 2; floor.position.set(m.width / 2, 0, m.depth / 2); mapGroup.add(floor);
  const outer = new THREE.Mesh(new THREE.PlaneGeometry(600, 600), new THREE.MeshStandardMaterial({ color: 0x4e5a48 }));
  outer.rotation.x = -Math.PI / 2; outer.position.set(m.width / 2, -0.02, m.depth / 2); mapGroup.add(outer);
  // walls / crates as instanced boxes
  const walls = m.boxes.filter((b) => b[5] === "wall"), crates = m.boxes.filter((b) => b[5] === "crate");
  const geo = new THREE.BoxGeometry(1, 1, 1);
  const mk = (list, mat) => {
    const im = new THREE.InstancedMesh(geo, mat, list.length);
    const o = new THREE.Object3D();
    list.forEach((b, i) => {
      o.position.set((b[0] + b[2]) / 2, b[4] / 2, (b[1] + b[3]) / 2); o.scale.set(b[2] - b[0], b[4], b[3] - b[1]);
      o.updateMatrix(); im.setMatrixAt(i, o.matrix);
    });
    return im;
  };
  mapGroup.add(mk(walls, new THREE.MeshStandardMaterial({ color: 0xb9b2a5, roughness: 0.9 })));
  mapGroup.add(mk(crates, new THREE.MeshStandardMaterial({ color: 0x9a6b3a, roughness: 0.8 })));
  scene.add(mapGroup);
  buildMinimap(m);
}

// ----------------------------------------------------------------- health drops (hearts)
const heartGeo = (() => {
  const s = new THREE.Shape();
  s.moveTo(25, 25);
  s.bezierCurveTo(25, 25, 20, 0, 0, 0);
  s.bezierCurveTo(-30, 0, -30, 35, -30, 35);
  s.bezierCurveTo(-30, 55, -10, 77, 25, 95);
  s.bezierCurveTo(60, 77, 80, 55, 80, 35);
  s.bezierCurveTo(80, 35, 80, 0, 50, 0);
  s.bezierCurveTo(35, 0, 25, 25, 25, 25);
  const g = new THREE.ExtrudeGeometry(s, { depth: 14, bevelEnabled: true, bevelSize: 5, bevelThickness: 5, bevelSegments: 3 });
  g.center(); g.rotateZ(Math.PI); g.scale(0.006, 0.006, 0.006);   // point down, ~0.7 m wide
  return g;
})();
const heartMat = new THREE.MeshStandardMaterial({ color: 0xff3b5c, emissive: 0xff1040, emissiveIntensity: 0.6, roughness: 0.4 });
const hearts = new Map();        // drop id -> mesh
function syncHearts(drops) {
  const live = new Set();
  for (const [id, x, z] of drops) {
    live.add(id);
    let h = hearts.get(id);
    if (!h) { h = new THREE.Mesh(heartGeo, heartMat); scene.add(h); hearts.set(id, h); }
    h.position.x = x; h.position.z = z;
  }
  for (const [id, h] of hearts) if (!live.has(id)) { scene.remove(h); hearts.delete(id); }
}
function animateHearts(now) {
  for (const [id, h] of hearts) { h.rotation.y = now / 600; h.position.y = 1.0 + Math.sin(now / 400 + id) * 0.12; }
}

// ----------------------------------------------------------------- minimap
// Map structure + you + teammates only. Enemies are never drawn here.
let mmBase = null, mmScale = 1;
function buildMinimap(m) {
  mmScale = MINIMAP_SIZE / Math.max(m.width, m.depth);
  const c = ui.minimap; c.width = Math.round(m.width * mmScale); c.height = Math.round(m.depth * mmScale);
  mmBase = document.createElement("canvas"); mmBase.width = c.width; mmBase.height = c.height;
  const g = mmBase.getContext("2d");
  g.fillStyle = "rgba(18,24,36,.8)"; g.fillRect(0, 0, c.width, c.height);
  for (const b of m.boxes) {
    g.fillStyle = b[5] === "wall" ? "#b9b2a5" : "#9a6b3a";
    g.fillRect(b[0] * mmScale, b[1] * mmScale, (b[2] - b[0]) * mmScale, (b[3] - b[1]) * mmScale);
  }
}
function drawMinimap() {
  if (!mmBase) return;
  const g = ui.minimap.getContext("2d"), s = mmScale;
  g.drawImage(mmBase, 0, 0);
  g.fillStyle = TEAM_CSS[myTeam] || "#fff";
  for (const r of remotes.values()) {
    if (r.team !== myTeam || !r.group.visible) continue;
    g.beginPath(); g.arc(r.group.position.x * s, r.group.position.z * s, 3.5, 0, Math.PI * 2); g.fill();
  }
  // You: white arrow pointing where the camera faces.
  const x = me.x * s, y = me.z * s, fx = -Math.sin(me.yaw), fy = -Math.cos(me.yaw), px = -fy, py = fx;
  g.fillStyle = "#fff"; g.beginPath();
  g.moveTo(x + fx * 7, y + fy * 7);
  g.lineTo(x - fx * 4 + px * 4, y - fy * 4 + py * 4);
  g.lineTo(x - fx * 4 - px * 4, y - fy * 4 - py * 4);
  g.closePath(); g.fill();
}

// ----------------------------------------------------------------- movement (mirrors World.move in game/world.py)
function resolveAxis(x, z, y, axis) {
  const r = PLAYER_RADIUS;
  for (const b of boxes) {
    if (b[4] <= y + STEP_EPS) continue;   // standing on / above it
    if (x + r <= b[0] || x - r >= b[2] || z + r <= b[1] || z - r >= b[3]) continue;
    if (axis === "x") x = x < (b[0] + b[2]) / 2 ? b[0] - r : b[2] + r;
    else z = z < (b[1] + b[3]) / 2 ? b[1] - r : b[3] + r;
  }
  if (axis === "x") return Math.min(Math.max(x, r), mapW - r);
  return Math.min(Math.max(z, r), mapD - r);
}
function support(x, z, y) {
  const r = PLAYER_RADIUS;
  let top = 0;
  for (const b of boxes) {
    if (top < b[4] && b[4] <= y + STEP_EPS && !(x + r <= b[0] || x - r >= b[2] || z + r <= b[1] || z - r >= b[3])) top = b[4];
  }
  return top;
}
// s = {x, y, z, vy}; advanced in place by one input step.
function moveLocal(s, yaw, keys, dt) {
  const crouch = (keys & KEY.CROUCH) !== 0;
  const fx = -Math.sin(yaw), fz = -Math.cos(yaw), rx = Math.cos(yaw), rz = -Math.sin(yaw);
  let mx = 0, mz = 0;
  if (keys & KEY.W) { mx += fx; mz += fz; }
  if (keys & KEY.S) { mx -= fx; mz -= fz; }
  if (keys & KEY.D) { mx += rx; mz += rz; }
  if (keys & KEY.A) { mx -= rx; mz -= rz; }
  const len = Math.hypot(mx, mz);
  if (len > 1e-6) {
    const step = (crouch ? CROUCH_SPEED : keys & KEY.SPRINT ? SPRINT_SPEED : WALK_SPEED) * dt / len;
    s.x = resolveAxis(s.x + mx * step, s.z, s.y, "x");
    s.z = resolveAxis(s.x, s.z + mz * step, s.y, "z");
  }
  const ground = support(s.x, s.z, s.y);
  if (s.y <= ground + STEP_EPS) {
    s.y = ground;
    s.vy = (keys & KEY.JUMP) && !crouch ? JUMP_SPEED : 0;
  }
  if (s.vy !== 0 || s.y > ground) {
    s.vy -= GRAVITY * dt;
    s.y += s.vy * dt;
    if (s.y <= ground) { s.y = ground; s.vy = 0; }
  }
}

// ----------------------------------------------------------------- state
let ws = null, myId = 0, myTeam = null, lastRoster = [], me = { x: 0, y: 0, z: 0, vy: 0, yaw: 0, pitch: 0, alive: true, hp: 100, ammo: 30 };
let phase = "lobby", seq = 0, pending = [], keys = 0, mouseDown = false, reloadQueued = false, acc = 0, lastT = 0;
let weapon = RIFLE, eye = EYE_HEIGHT, swing = 0, reloading = false;
const remotes = new Map();       // pid -> {group, name, team, mat, label}
let snaps = [];                  // [{t, players: Map}]
let hitMarkUntil = 0, flashUntil = 0, recoil = 0, feedTimer = [];
const tracers = [];

function send(o) { if (ws && ws.readyState === 1) ws.send(JSON.stringify(o)); }

function connect(name, team) {
  const proto = location.protocol === "https:" ? "wss" : "ws";
  ws = new WebSocket(`${proto}://${location.host}/ws`);
  ws.onopen = () => send({ t: "join", name, team });
  ws.onmessage = (ev) => onMessage(JSON.parse(ev.data));
  ws.onclose = () => {
    if (ui.join.style.display !== "none") { ui.err.textContent = ui.err.textContent || "Connection closed."; ui.go.disabled = false; return; }
    document.exitPointerLock?.();
    showBanner("DISCONNECTED", "Reload the page to rejoin");
  };
  ws.onerror = () => { ui.err.textContent = "Could not reach the server."; ui.go.disabled = false; };
}

function onMessage(m) {
  if (m.t === "error") { ui.err.textContent = m.msg; ui.go.disabled = false; return; }
  if (m.t === "welcome") {
    myId = m.id; buildMap(m.map); phase = m.phase;
    ui.join.style.display = "none"; ui.hud.classList.add("on"); ui.clickhint.classList.remove("hidden");
    renderer.domElement.focus();
    lastT = performance.now(); requestAnimationFrame(frame);
    return;
  }
  if (m.t === "map") { buildMap(m.map); pending = []; snaps = []; return; }
  if (m.t === "state") onState(m);
}

function onState(s) {
  const prevPhase = phase; phase = s.phase;
  const mine = s.me;
  // --- reconcile own position
  const srv = s.players.find((p) => p[0] === myId);
  const wasAlive = me.alive;
  if (srv) { me.alive = !!srv[8]; myTeam = srv[2]; }
  me.hp = mine.hp; me.ammo = mine.ammo;
  pending = pending.filter((i) => i.s > mine.ack);
  me.x = mine.x; me.y = mine.y; me.z = mine.z; me.vy = mine.vy;
  if (me.alive) for (const i of pending) moveLocal(me, i.y, i.k, i.dt);
  if (!wasAlive && me.alive && srv) { me.yaw = srv[5]; me.pitch = 0; pending = []; }
  reloading = !!mine.rel;
  if (mine.hm) hitMarkUntil = performance.now() + 110;
  for (const d of mine.dmg) damageArrow(d);
  if (mine.dmg.length) { ui.vignette.style.opacity = 1; setTimeout(() => (ui.vignette.style.opacity = 0), 150); }
  if (mine.heal) { ui.healfx.style.opacity = 1; setTimeout(() => (ui.healfx.style.opacity = 0), 300); }
  syncHearts(s.drops);
  // --- others (the server only sends teammates and enemies you have line of sight to)
  const map = new Map();
  for (const p of s.players) if (p[0] !== myId) map.set(p[0], p);
  snaps.push({ t: performance.now(), players: map });
  if (snaps.length > 30) snaps.shift();
  const inGame = new Set(s.roster.map((p) => p[0]));
  for (const [pid, p] of map) if (!remotes.has(pid)) remotes.set(pid, makeRemote(p[1], p[2]));
  for (const [pid, r] of remotes) {
    if (!inGame.has(pid)) { scene.remove(r.group); remotes.delete(pid); continue; }
    const p = map.get(pid);
    if (p && p[2] !== r.team) { r.team = p[2]; r.mat.color.setHex(TEAM_COLOR[p[2]]); r.setLabel(p[1], p[2]); }
    // Teammate tags show through walls; enemy tags are hidden by walls like the enemy.
    r.label.material.depthTest = r.team !== myTeam;
  }
  // --- events
  for (const e of s.evts) {
    if (e.e === "shot") onShot(e, map);
    else if (e.e === "slash") onSlash(e, map);
    else if (e.e === "kill") addFeed(`<span class="${e.kt}">${esc(e.k)}</span> ${KILL_ICON[e.w] || "☠"} <span class="${e.vt}">${esc(e.v)}</span>`);
    else if (e.e === "start") { ui.feed.innerHTML = ""; addFeed("Match started!"); }
    else if (e.e === "end") { /* banner handled by phase */ }
  }
  // --- HUD
  ui.sA.textContent = s.scores.A; ui.sB.textContent = s.scores.B;
  ui.timer.textContent = fmt(s.left);
  ui.hptext.textContent = me.hp; ui.hpfill.style.width = me.hp + "%";
  ui.hpfill.style.background = me.hp > 50 ? "#6ddc6d" : me.hp > 25 ? "#ffcc4d" : "#ff5c5c";
  renderAmmo();
  ui.lobbybar.classList.toggle("on", phase === "lobby");
  if (!me.alive) showBanner("YOU DIED", `Respawning in ${Math.ceil(mine.resp)}…`);
  else if (phase === "lobby") showBanner("LOBBY", "Walk around · the admin will start the match");
  else if (phase === "ended") {
    const w = s.scores.A === s.scores.B ? "DRAW" : s.scores.A > s.scores.B ? "TEAM A WINS" : "TEAM B WINS";
    showBanner(w, `${s.scores.A} – ${s.scores.B}`);
  } else showBanner("", "");
  lastRoster = s.roster;
  if (ui.score.classList.contains("on") || prevPhase !== phase) renderScore(lastRoster);
  renderBoard(s.roster, s.scores);
}

// Top-right team leaderboard: leading team first, players by kills.
let lastBoard = "";
function renderBoard(roster, scores) {
  const teams = scores.B > scores.A ? ["B", "A"] : ["A", "B"];
  const html = teams.map((t) => {
    const rows = roster.filter((p) => p[2] === t).sort((a, b) => b[3] - a[3] || a[4] - b[4]);
    const list = rows.map((p) => `<div class="pl${p[0] === myId ? " me" : ""}"><span>${esc(p[1])}${p[0] === myId ? " (you)" : ""}</span><span>${p[3]} / ${p[4]}</span></div>`).join("")
      || `<div class="pl empty">no players</div>`;
    return `<div class="team ${t}"><span>TEAM ${t}${t === myTeam ? "<small>YOUR TEAM</small>" : ""}</span><span>${scores[t]}</span></div>${list}`;
  }).join("");
  if (html !== lastBoard) { ui.board.innerHTML = html; lastBoard = html; }
}

function makeRemote(name, team) {
  const group = new THREE.Group();
  const mat = new THREE.MeshStandardMaterial({ color: TEAM_COLOR[team], roughness: 0.7 });
  const skin = new THREE.MeshStandardMaterial({ color: 0xe8c39e, roughness: 0.8 });
  const body = new THREE.Mesh(new THREE.CapsuleGeometry(0.3, 0.7, 4, 10), mat); body.position.y = 0.85;
  const head = new THREE.Mesh(new THREE.SphereGeometry(0.2, 12, 12), skin); head.position.y = 1.55;
  const visor = new THREE.Mesh(new THREE.BoxGeometry(0.3, 0.08, 0.1), new THREE.MeshStandardMaterial({ color: 0x111 }));
  visor.position.set(0, 1.58, -0.17);
  const rifle = new THREE.Mesh(new THREE.BoxGeometry(0.08, 0.08, 0.6), new THREE.MeshStandardMaterial({ color: 0x2a2d33 }));
  rifle.position.set(0.22, 1.2, -0.3);
  const blade = new THREE.Mesh(new THREE.BoxGeometry(0.03, 0.05, 0.3), new THREE.MeshStandardMaterial({ color: 0xd8dde3, metalness: 0.9, roughness: 0.3 }));
  blade.position.set(0.25, 1.15, -0.3); blade.visible = false;
  // Everything but the name tag goes in `model` so crouching can squash it.
  const model = new THREE.Group(); model.add(body, head, visor, rifle, blade); group.add(model);
  const label = new THREE.Sprite(new THREE.SpriteMaterial({ depthTest: false, transparent: true }));
  label.position.y = 2.15; label.scale.set(1.6, 0.4, 1); group.add(label);
  const r = { group, model, rifle, blade, name, team, mat, label, setLabel: null };
  r.setLabel = (n, t) => {
    const c = document.createElement("canvas"); c.width = 256; c.height = 64; const g = c.getContext("2d");
    g.fillStyle = "rgba(0,0,0,.45)"; g.roundRect(0, 8, 256, 48, 12); g.fill();
    g.font = "bold 30px system-ui, sans-serif"; g.textAlign = "center"; g.textBaseline = "middle";
    g.fillStyle = t === "A" ? "#ff8a8a" : "#8ac3ff"; g.fillText(n, 128, 32);
    const tex = new THREE.CanvasTexture(c); tex.colorSpace = THREE.SRGBColorSpace;
    if (label.material.map) label.material.map.dispose(); label.material.map = tex; label.material.needsUpdate = true;
  };
  r.setLabel(name, team);
  scene.add(group);
  return r;
}

function updateRemotes(now) {
  const t = now - INTERP_DELAY;
  let a = null, b = null;
  for (let i = snaps.length - 1; i >= 0; i--) { if (snaps[i].t <= t) { a = snaps[i]; b = snaps[i + 1] || null; break; } }
  if (!a) a = snaps[0];
  if (!a) return;
  const f = b ? Math.min(1, (t - a.t) / Math.max(1, b.t - a.t)) : 0;
  for (const [pid, r] of remotes) {
    const pa = a.players.get(pid), pb = b ? b.players.get(pid) : null;
    // Missing from the newer snapshot = the server stopped sending them (out of sight).
    const p = b ? pb : pa;
    if (!p) { r.group.visible = false; continue; }
    let x = p[3], z = p[4], yaw = p[5], pitch = p[6], y = p[11];
    if (pa && pb) {
      x = pa[3] + (pb[3] - pa[3]) * f; z = pa[4] + (pb[4] - pa[4]) * f; y = pa[11] + (pb[11] - pa[11]) * f;
      yaw = lerpAngle(pa[5], pb[5], f); pitch = pa[6] + (pb[6] - pa[6]) * f;
    }
    r.group.visible = !!p[8];
    r.group.position.set(x, y, z); r.group.rotation.y = yaw;
    r.model.scale.y = p[12] ? 1.2 / 1.8 : 1;      // crouched hitbox is 1.2 m
    r.label.position.y = p[12] ? 1.55 : 2.15;
    r.rifle.visible = p[13] !== KNIFE; r.blade.visible = p[13] === KNIFE;
    r.rifle.rotation.x = pitch; r.blade.rotation.x = pitch;
  }
}

// ----------------------------------------------------------------- shooting fx
function onShot(e, players) {
  let from;
  if (e.id === myId) {
    flashUntil = performance.now() + 40; recoil = 1; playShot(1.0);
    from = flash.getWorldPosition(new THREE.Vector3());
  } else {
    const p = players.get(e.id); if (!p) return;
    const r = remotes.get(e.id);
    from = r ? r.rifle.getWorldPosition(new THREE.Vector3()) : new THREE.Vector3(p[3], p[11] + EYE_HEIGHT, p[4]);
    const d = Math.hypot(p[3] - me.x, p[4] - me.z); playShot(Math.max(0.08, 1 - d / 60));
  }
  const geo = new THREE.BufferGeometry().setFromPoints([from, new THREE.Vector3(e.to[0], e.to[1], e.to[2])]);
  const line = new THREE.Line(geo, new THREE.LineBasicMaterial({ color: 0xffe08a, transparent: true, opacity: 0.9 }));
  scene.add(line); tracers.push({ line, until: performance.now() + 70 });
  // impact puff
  const puff = new THREE.Mesh(new THREE.SphereGeometry(0.06, 6, 6), new THREE.MeshBasicMaterial({ color: e.hit ? 0xff3030 : 0xdddddd }));
  puff.position.set(e.to[0], e.to[1], e.to[2]); scene.add(puff); tracers.push({ line: puff, until: performance.now() + 120 });
}
const KILL_ICON = { rifle: "☠", hs: "🎯", knife: "🔪", back: "🔪💀" };
function onSlash(e, players) {
  let d = 0;
  if (e.id === myId) swing = 1;
  else { const p = players.get(e.id); if (!p) return; d = Math.hypot(p[3] - me.x, p[4] - me.z); }
  playSwish(Math.max(0.1, 1 - d / 25), !!e.hit);
}
function playSwish(vol, hit) {
  try {
    actx = actx || new (window.AudioContext || window.webkitAudioContext)();
    const n = actx.sampleRate * 0.18, buf = actx.createBuffer(1, n, actx.sampleRate), d = buf.getChannelData(0);
    for (let i = 0; i < n; i++) d[i] = (Math.random() * 2 - 1) * Math.sin(Math.PI * i / n);
    const src = actx.createBufferSource(); src.buffer = buf;
    const f = actx.createBiquadFilter(); f.type = "bandpass"; f.frequency.value = hit ? 700 : 2600; f.Q.value = 1.2;
    const g = actx.createGain(); g.gain.value = (hit ? 0.5 : 0.25) * vol;
    src.connect(f).connect(g).connect(actx.destination); src.start();
  } catch { /* audio is optional */ }
}
function renderAmmo() {
  if (weapon === KNIFE) ui.ammo.innerHTML = `🔪 <small>KNIFE</small>`;
  else ui.ammo.innerHTML = reloading ? `<small>RELOADING</small>` : `${me.ammo} <small>/ 30</small>`;
}
function setWeapon(w) { if (w !== weapon) { weapon = w; swing = 0; renderAmmo(); } }

let actx = null;
function playShot(vol) {
  try {
    actx = actx || new (window.AudioContext || window.webkitAudioContext)();
    const n = actx.sampleRate * 0.12, buf = actx.createBuffer(1, n, actx.sampleRate), d = buf.getChannelData(0);
    for (let i = 0; i < n; i++) d[i] = (Math.random() * 2 - 1) * Math.pow(1 - i / n, 3);
    const src = actx.createBufferSource(); src.buffer = buf;
    const g = actx.createGain(); g.gain.value = 0.35 * vol;
    const f = actx.createBiquadFilter(); f.type = "lowpass"; f.frequency.value = 1800;
    src.connect(f).connect(g).connect(actx.destination); src.start();
  } catch { /* audio is optional */ }
}

// ----------------------------------------------------------------- input
addEventListener("keydown", (e) => {
  if (ui.join.style.display !== "none") return;
  if (["Tab", "ArrowUp", "ArrowDown", "ArrowLeft", "ArrowRight", "Space"].includes(e.code)) e.preventDefault();
  if (e.code === "Tab") { e.preventDefault(); ui.score.classList.add("on"); renderScore(lastRoster); }
  if (e.code === "KeyW" || e.code === "ArrowUp") keys |= KEY.W;
  if (e.code === "KeyS" || e.code === "ArrowDown") keys |= KEY.S;
  if (e.code === "KeyA" || e.code === "ArrowLeft") keys |= KEY.A;
  if (e.code === "KeyD" || e.code === "ArrowRight") keys |= KEY.D;
  if (e.code === "ShiftLeft" || e.code === "ShiftRight") keys |= KEY.SPRINT;
  if (e.code === "Space") { e.preventDefault(); keys |= KEY.JUMP; }
  if (e.code === "KeyC") keys |= KEY.CROUCH;   // not Ctrl: Ctrl+W would close the tab
  if (e.code === "KeyR") reloadQueued = true;
  if (e.code === "Digit1") setWeapon(RIFLE);
  if (e.code === "Digit2") setWeapon(KNIFE);
  if (e.code === "KeyQ") setWeapon(weapon === RIFLE ? KNIFE : RIFLE);
});
addEventListener("wheel", (e) => { if (document.pointerLockElement === renderer.domElement && e.deltaY) setWeapon(weapon === RIFLE ? KNIFE : RIFLE); });
addEventListener("keyup", (e) => {
  if (e.code === "Tab") ui.score.classList.remove("on");
  if (e.code === "KeyW" || e.code === "ArrowUp") keys &= ~KEY.W;
  if (e.code === "KeyS" || e.code === "ArrowDown") keys &= ~KEY.S;
  if (e.code === "KeyA" || e.code === "ArrowLeft") keys &= ~KEY.A;
  if (e.code === "KeyD" || e.code === "ArrowRight") keys &= ~KEY.D;
  if (e.code === "ShiftLeft" || e.code === "ShiftRight") keys &= ~KEY.SPRINT;
  if (e.code === "Space") keys &= ~KEY.JUMP;
  if (e.code === "KeyC") keys &= ~KEY.CROUCH;
});
addEventListener("blur", () => { keys = 0; mouseDown = false; });
const lock = () => {
  renderer.domElement.focus();
  renderer.domElement.requestPointerLock();
};
ui.clickhint.addEventListener("click", lock);
renderer.domElement.addEventListener("mousedown", (e) => {
  if (document.pointerLockElement !== renderer.domElement) { lock(); return; }
  if (e.button === 0) mouseDown = true;
});
addEventListener("mouseup", (e) => { if (e.button === 0) mouseDown = false; });
addEventListener("contextmenu", (e) => e.preventDefault());
document.addEventListener("pointerlockchange", () => {
  const locked = document.pointerLockElement === renderer.domElement;
  ui.clickhint.classList.toggle("hidden", locked || ui.join.style.display !== "none");
  if (!locked) { keys = 0; mouseDown = false; }
});
addEventListener("mousemove", (e) => {
  if (document.pointerLockElement !== renderer.domElement) return;
  me.yaw -= e.movementX * SENS;
  me.pitch = Math.max(-1.5, Math.min(1.5, me.pitch - e.movementY * SENS));
});

// ----------------------------------------------------------------- frame loop
function frame(now) {
  requestAnimationFrame(frame);
  const dt = Math.min(0.1, (now - lastT) / 1000); lastT = now; acc += dt;
  while (acc >= STEP) {
    acc -= STEP;
    const inp = { t: "in", s: ++seq, k: keys, y: round(me.yaw), p: round(me.pitch), dt: STEP, w: weapon, f: mouseDown && me.alive && phase === "playing" };
    if (reloadQueued) { inp.r = true; reloadQueued = false; }
    if (me.alive) moveLocal(me, me.yaw, keys, STEP);
    pending.push(inp); if (pending.length > 240) pending.shift();
    send(inp);
  }
  // Ease the camera down/up when crouching so it doesn't snap.
  const eyeTarget = keys & KEY.CROUCH ? CROUCH_EYE_HEIGHT : EYE_HEIGHT;
  eye += (eyeTarget - eye) * Math.min(1, dt * 14);
  camera.position.set(me.x, me.y + eye, me.z);
  camera.rotation.y = me.yaw; camera.rotation.x = me.pitch;
  // weapon bob + recoil / knife swing
  const moving = keys & (KEY.W | KEY.S | KEY.A | KEY.D);
  const bob = moving ? Math.sin(now / 90) * 0.012 : Math.sin(now / 600) * 0.004;
  recoil = Math.max(0, recoil - dt * 10);
  swing = Math.max(0, swing - dt * 4);
  gun.visible = weapon === RIFLE; knife.visible = weapon === KNIFE;
  gun.position.set(0.22, -0.2 + bob, -0.35 + recoil * 0.08);
  gun.rotation.x = recoil * 0.12;
  const s = Math.sin(swing * Math.PI);           // 0 -> 1 -> 0 over one slash
  knife.position.set(0.2 - s * 0.18, -0.2 + bob + s * 0.05, -0.35 - s * 0.25);
  knife.rotation.set(-0.3 - s * 0.6, 0.35 + s * 0.9, s * 0.4);
  flash.visible = now < flashUntil;
  ui.cross.classList.toggle("hit", now < hitMarkUntil);
  for (let i = tracers.length - 1; i >= 0; i--) if (now > tracers[i].until) { scene.remove(tracers[i].line); tracers[i].line.geometry.dispose(); tracers.splice(i, 1); }
  updateRemotes(now);
  animateHearts(now);
  drawMinimap();
  renderer.render(scene, camera);
}

// ----------------------------------------------------------------- helpers
function renderScore(roster) {   // roster rows: [id, name, team, kills, deaths]
  const rows = [...roster].sort((a, b) => a[2].localeCompare(b[2]) || b[3] - a[3]);
  ui.scoreBody.innerHTML = rows.map((p) => `<tr class="${p[2]}"><td>${esc(p[1])}${p[0] === myId ? " (you)" : ""}</td><td>${p[2]}</td><td>${p[3]}</td><td>${p[4]}</td></tr>`).join("");
}
function addFeed(html) {
  const d = document.createElement("div"); d.innerHTML = html; ui.feed.prepend(d);
  while (ui.feed.children.length > 6) ui.feed.lastChild.remove();
  setTimeout(() => d.remove(), 6000);
}
function damageArrow(angle) {
  const a = document.createElement("div"); a.className = "arrow";
  a.style.transform = `rotate(${me.yaw - (angle + Math.PI)}rad)`;
  ui.dmgdir.appendChild(a); setTimeout(() => a.remove(), 800);
}
function showBanner(h, p) { ui.banner.innerHTML = h ? `<h2>${h}</h2><p>${p}</p>` : ""; }
function fmt(s) { s = Math.max(0, Math.round(s)); return `${Math.floor(s / 60)}:${String(s % 60).padStart(2, "0")}`; }
function esc(s) { return String(s).replace(/[&<>"']/g, (c) => ({ "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#39;" }[c])); }
function round(v) { return Math.round(v * 1000) / 1000; }
function lerpAngle(a, b, f) { let d = b - a; while (d > Math.PI) d -= 2 * Math.PI; while (d < -Math.PI) d += 2 * Math.PI; return a + d * f; }
