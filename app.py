#!/usr/bin/env python3
import json
import os
import random
import math
import time
import threading
import socketserver
import http.server

PORT = int(os.environ.get("PORT", 5000))
LOCK = threading.Lock()

# Jeda antar tembakan (detik) -- INI YANG MEMPERBAIKI BUG "BERUNTUN"
SHOOT_COOLDOWN = 0.30   # ~3-4 peluru per detik, full ranged untuk semua player

MAP_WIDTH = 1600
MAP_HEIGHT = 1200

# Kepadatan obstacle dijaga PROPORSIONAL terhadap luas map, bukan angka tetap.
# Basisnya: map default 1600x1200 (1.920.000 px^2) = 15 obstacle.
# Kalau MAP_WIDTH/MAP_HEIGHT diperbesar, MAX_OBSTACLES otomatis ikut naik
# supaya kepadatannya tetap terasa sama (tidak "kosong" di map besar).
OBSTACLE_DENSITY = (1600 * 1200) / 15   # ~128.000 px^2 per 1 obstacle
MAX_OBSTACLES = max(8, round((MAP_WIDTH * MAP_HEIGHT) / OBSTACLE_DENSITY))
MIN_OBSTACLE_DIST = 150 # Jarak minimal antar obstacle

# Player yang tidak mengirim aksi apapun (diam / koneksi mati / logout tanpa
# sempat memanggil /api/leave, misal app di-kill paksa di HP) selama sekian
# detik akan otomatis di-kick dari state, supaya karakter tidak "hantu" nyangkut di map.
IDLE_TIMEOUT = 60  # detik

# Kunci sederhana untuk fitur reset server, supaya tidak sembarang orang bisa reset.
# Ganti sesuka hati, atau override lewat environment variable RESET_KEY.
RESET_KEY = os.environ.get("RESET_KEY", "reset123")

# --- KONFIGURASI BOT (Hostile & Neutral) ---
MAX_HOSTILE_BOTS = 4        # Jumlah bot musuh (segitiga merah) yang dijaga tetap ada di map
MAX_NEUTRAL_BOTS = 6        # Jumlah bot netral (kotak kuning) yang dijaga tetap ada di map
MAX_ALLIES_PER_PLAYER = 3   # Batas maksimal ally/shield yang bisa dimiliki 1 player

HOSTILE_HP = 4
NEUTRAL_HP = 2
ALLY_HP = 3                 # HP ally setelah jadi shield (terpisah dari HP saat masih neutral)

BOT_VISION_RANGE = 420      # Jarak hostile bot mulai "notice" & mengejar player
HOSTILE_ATTACK_RANGE = 260  # Jarak hostile bot berhenti maju & mulai menembak (kiting)
HOSTILE_SHOOT_COOLDOWN = 1.1
HOSTILE_SPEED = 2.3
HOSTILE_BULLET_SPEED = 6
NEUTRAL_WANDER_SPEED = 0.6

ORBIT_RADIUS = 55           # Jarak orbit ally mengelilingi player
ORBIT_LERP = 0.18           # Kecepatan ally "mengejar" posisi orbitnya (0-1, makin besar makin cepat nempel)
ORBIT_SPIN_SPEED = 0.02     # Kecepatan rotasi orbit ally (radian/tick), biar shield terlihat "hidup"

# --- ALLY BISA MENYERANG (bot hostile & player lawan) ---
ALLY_ATTACK_RANGE = 260     # Jarak ally mulai "notice" & menembak musuh terdekat
ALLY_SHOOT_COOLDOWN = 0.9   # Lebih lambat dari player, biar ally jadi bantuan bukan senjata utama
ALLY_BULLET_SPEED = 7
ALLY_BULLET_LIFE = 30
ALLY_DAMAGE = 1

# --- HP HANYA BERTAMBAH DARI KILL (tidak ada regen pasif lagi) ---
PLAYER_MAX_HEALTH = 10
HEALTH_PER_KILL = 2   # HP kecil yang didapat player tiap berhasil mengalahkan hostile bot / player lain

state = {
    "players": {},
    "bullets": [],
    "obstacles": [],
    "bots": []
}
next_bullet_id = 0
next_bot_id = 0

def is_too_close_to_obstacles(new_x, new_y, radius):
    """Cek apakah posisi baru terlalu dekat dengan obstacle lain."""
    for obs in state["obstacles"]:
        dist = math.hypot(new_x - obs['x'], new_y - obs['y'])
        if dist < (radius + obs['radius'] + MIN_OBSTACLE_DIST):
            return True
    return False

def spawn_obstacle():
    """Membuat obstacle baru di lokasi random yang tidak berdekatan.
    Ukurannya sengaja dibuat bervariasi lebar (kecil sampai besar) biar map
    terasa lebih hidup, dan HP-nya tetap ada di server (tetap bisa dihancurkan)
    walau bar HP-nya sengaja disembunyikan di tampilan."""
    attempts = 0
    while attempts < 50: # Maksimal 50 kali percobaan
        new_x = random.randint(150, MAP_WIDTH - 150)
        new_y = random.randint(150, MAP_HEIGHT - 150)
        new_radius = random.randint(18, 60)

        if not is_too_close_to_obstacles(new_x, new_y, new_radius):
            return {
                "x": new_x,
                "y": new_y,
                "radius": new_radius,
                "hp": 2,  # 2 Bar Health (tetap ada di server, cuma tidak ditampilkan)
                "max_hp": 2,
                "rotation": random.uniform(0, math.pi * 2)
            }
        attempts += 1
    return None

# Generate Obstacle awal
for _ in range(MAX_OBSTACLES):
    obs = spawn_obstacle()
    if obs:
        state["obstacles"].append(obs)

def spawn_bot(kind):
    """Membuat bot baru (kind: 'hostile' atau 'neutral') di lokasi random,
    dijaga agar tidak muncul menindih obstacle."""
    global next_bot_id
    attempts = 0
    while attempts < 50:
        new_x = random.randint(100, MAP_WIDTH - 100)
        new_y = random.randint(100, MAP_HEIGHT - 100)
        if not is_too_close_to_obstacles(new_x, new_y, 18):
            bot = {
                "id": f"bot_{next_bot_id}",
                "kind": kind,  # 'hostile' atau 'neutral'
                "x": new_x,
                "y": new_y,
                "angle": random.uniform(0, math.pi * 2),
                "hp": HOSTILE_HP if kind == "hostile" else NEUTRAL_HP,
                "max_hp": HOSTILE_HP if kind == "hostile" else NEUTRAL_HP,
                "last_shot": 0,
                "wander_angle": random.uniform(0, math.pi * 2),
                "wander_timer": 0
            }
            next_bot_id += 1
            return bot
        attempts += 1
    return None

# Generate Bot awal (hostile & neutral)
for _ in range(MAX_HOSTILE_BOTS):
    b = spawn_bot("hostile")
    if b:
        state["bots"].append(b)
for _ in range(MAX_NEUTRAL_BOTS):
    b = spawn_bot("neutral")
    if b:
        state["bots"].append(b)

def check_collision(b, p):
    dist = math.hypot(b['x'] - p['x'], b['y'] - p['y'])
    return dist < 30

def obstacle_collision(x, y, radius):
    for obs in state["obstacles"]:
        dist = math.hypot(x - obs['x'], y - obs['y'])
        if dist < (radius + obs['radius'] - 5):
            return True
    return False

def nearest_alive_player(x, y, max_range):
    """Cari player hidup terdekat dari titik (x,y) dalam radius max_range."""
    best = None
    best_dist = max_range
    for pid, p in state["players"].items():
        if p['health'] <= 0:
            continue
        dist = math.hypot(p['x'] - x, p['y'] - y)
        if dist < best_dist:
            best = p
            best_dist = dist
    return best

def convert_neutral_to_ally(bot, owner_id):
    """Bot netral yang mati di tangan seorang player berubah jadi ally/shield
    yang mengikuti & melindungi player itu dari peluru."""
    owner = state["players"].get(owner_id)
    if not owner:
        return
    allies = owner.setdefault("allies", [])
    if len(allies) >= MAX_ALLIES_PER_PLAYER:
        return  # Sudah penuh, bot netral cukup hilang begitu saja
    allies.append({
        "id": bot["id"],
        "hp": ALLY_HP,
        "max_hp": ALLY_HP,
        "x": bot["x"],
        "y": bot["y"],
        "orbit_offset": len(allies) * (math.pi * 2 / MAX_ALLIES_PER_PLAYER),
        "last_shot": 0
    })

def bot_ai_tick():
    """Update posisi & serangan tiap bot (hostile mengejar & menembak, neutral mengembara)."""
    global next_bullet_id
    for bot in state["bots"]:
        if bot["kind"] == "hostile":
            target = nearest_alive_player(bot["x"], bot["y"], BOT_VISION_RANGE)
            if target:
                dx, dy = target['x'] - bot['x'], target['y'] - bot['y']
                dist = math.hypot(dx, dy)
                bot["angle"] = math.atan2(dy, dx)
                if dist > HOSTILE_ATTACK_RANGE:
                    # Kejar player
                    new_x = bot["x"] + math.cos(bot["angle"]) * HOSTILE_SPEED
                    new_y = bot["y"] + math.sin(bot["angle"]) * HOSTILE_SPEED
                    if not obstacle_collision(new_x, new_y, 18):
                        bot["x"], bot["y"] = new_x, new_y
                else:
                    # Sudah dalam jarak serang -> tembak dengan cooldown
                    now = time.time()
                    if now - bot.get("last_shot", 0) >= HOSTILE_SHOOT_COOLDOWN:
                        state["bullets"].append({
                            "id": next_bullet_id,
                            "owner": bot["id"],
                            "type": "hostile",
                            "x": bot["x"] + math.cos(bot["angle"]) * 22,
                            "y": bot["y"] + math.sin(bot["angle"]) * 22,
                            "vx": math.cos(bot["angle"]) * HOSTILE_BULLET_SPEED,
                            "vy": math.sin(bot["angle"]) * HOSTILE_BULLET_SPEED,
                            "life": 35,
                            "size": 5
                        })
                        next_bullet_id += 1
                        bot["last_shot"] = now
            else:
                # Tidak ada target -> idle wander pelan
                bot["wander_timer"] -= 1
                if bot["wander_timer"] <= 0:
                    bot["wander_angle"] = random.uniform(0, math.pi * 2)
                    bot["wander_timer"] = random.randint(40, 100)
                new_x = bot["x"] + math.cos(bot["wander_angle"]) * (HOSTILE_SPEED * 0.4)
                new_y = bot["y"] + math.sin(bot["wander_angle"]) * (HOSTILE_SPEED * 0.4)
                new_x = max(40, min(MAP_WIDTH - 40, new_x))
                new_y = max(40, min(MAP_HEIGHT - 40, new_y))
                if not obstacle_collision(new_x, new_y, 18):
                    bot["x"], bot["y"] = new_x, new_y
        else:
            # Neutral: mengembara pelan & tidak menyerang siapa pun
            bot["wander_timer"] -= 1
            if bot["wander_timer"] <= 0:
                bot["wander_angle"] = random.uniform(0, math.pi * 2)
                bot["wander_timer"] = random.randint(60, 140)
            bot["angle"] = bot["wander_angle"]
            new_x = bot["x"] + math.cos(bot["wander_angle"]) * NEUTRAL_WANDER_SPEED
            new_y = bot["y"] + math.sin(bot["wander_angle"]) * NEUTRAL_WANDER_SPEED
            new_x = max(40, min(MAP_WIDTH - 40, new_x))
            new_y = max(40, min(MAP_HEIGHT - 40, new_y))
            if not obstacle_collision(new_x, new_y, 18):
                bot["x"], bot["y"] = new_x, new_y

def allies_orbit_tick():
    """Gerakkan tiap ally supaya mengorbit & mengikuti pemiliknya (efek 'tameng berjalan')."""
    for pid, p in state["players"].items():
        allies = p.get("allies")
        if not allies:
            continue
        for ally in allies:
            ally["orbit_offset"] += ORBIT_SPIN_SPEED
            desired_x = p["x"] + math.cos(ally["orbit_offset"]) * ORBIT_RADIUS
            desired_y = p["y"] + math.sin(ally["orbit_offset"]) * ORBIT_RADIUS
            ally["x"] += (desired_x - ally["x"]) * ORBIT_LERP
            ally["y"] += (desired_y - ally["y"]) * ORBIT_LERP

def nearest_enemy_for_ally(x, y, owner_id, max_range):
    """Cari musuh terdekat untuk seorang ally: bot hostile ATAU player lawan
    (bukan pemilik ally itu sendiri, dan bukan rekan satu tim/warna sama),
    dalam radius max_range."""
    best = None
    best_kind = None
    best_dist = max_range

    owner = state["players"].get(owner_id)
    owner_color = owner.get('color') if owner else None

    for bot in state["bots"]:
        if bot["kind"] != "hostile":
            continue
        dist = math.hypot(bot['x'] - x, bot['y'] - y)
        if dist < best_dist:
            best = bot
            best_kind = "bot"
            best_dist = dist

    for pid, p in state["players"].items():
        if pid == owner_id or p['health'] <= 0:
            continue
        if owner_color and p.get('color') == owner_color:
            continue  # rekan satu tim, jangan diserang
        dist = math.hypot(p['x'] - x, p['y'] - y)
        if dist < best_dist:
            best = p
            best_kind = "player"
            best_dist = dist

    return best, best_kind

def ally_attack_tick():
    """Setiap ally aktif mencari & menembak musuh terdekat (bot hostile atau
    player lawan) di sekitarnya, membantu pemiliknya menyerang -- bukan cuma
    jadi tameng pasif lagi."""
    global next_bullet_id
    for pid, p in state["players"].items():
        allies = p.get("allies")
        if not allies:
            continue
        for ally in allies:
            target, kind = nearest_enemy_for_ally(ally["x"], ally["y"], pid, ALLY_ATTACK_RANGE)
            if not target:
                continue
            now = time.time()
            if now - ally.get("last_shot", 0) < ALLY_SHOOT_COOLDOWN:
                continue
            dx, dy = target['x'] - ally['x'], target['y'] - ally['y']
            angle = math.atan2(dy, dx)
            state["bullets"].append({
                "id": next_bullet_id,
                "owner": pid,          # dianggap peluru milik player pemilik ally (skor & proteksi konsisten)
                "type": "ally",
                "x": ally["x"] + math.cos(angle) * 16,
                "y": ally["y"] + math.sin(angle) * 16,
                "vx": math.cos(angle) * ALLY_BULLET_SPEED,
                "vy": math.sin(angle) * ALLY_BULLET_SPEED,
                "life": ALLY_BULLET_LIFE,
                "size": 4
            })
            next_bullet_id += 1
            ally["last_shot"] = now

def game_logic_loop():
    global state
    ticker = threading.Event()
    while not ticker.wait(0.05):
        with LOCK:
            bot_ai_tick()
            allies_orbit_tick()
            ally_attack_tick()

            active_bullets = []
            for b in state["bullets"]:
                b['x'] += b['vx']
                b['y'] += b['vy']
                b['life'] -= 1

                hit = False

                # Warna player pemilik peluru ini menentukan TIM-nya. Player lain dengan
                # warna yang SAMA otomatis dianggap satu tim/ally -> tidak bisa saling bunuh.
                owner_color = None
                if b['owner'] in state["players"]:
                    owner_color = state["players"][b['owner']].get('color')

                # 1. Cek tabrakan dengan ALLY (shield) lebih dulu -- ally melindungi
                #    pemiliknya dari peluru siapa pun KECUALI peluru milik pemilik itu sendiri
                #    atau peluru dari rekan satu tim (warna sama).
                for pid, p in state["players"].items():
                    if hit:
                        break
                    if b['owner'] == pid:
                        continue
                    if owner_color and p.get('color') == owner_color:
                        continue
                    for ally in p.get("allies", []):
                        dist = math.hypot(b['x'] - ally['x'], b['y'] - ally['y'])
                        if dist < 20:
                            ally['hp'] -= 1
                            hit = True
                            if ally['hp'] <= 0:
                                p["allies"].remove(ally)
                            break

                # 2. Cek tabrakan dengan PLAYER (rekan satu tim/warna sama tidak bisa saling melukai)
                if not hit:
                    for p_id, p in state["players"].items():
                        if p_id != b['owner'] and not (owner_color and p.get('color') == owner_color):
                            if check_collision(b, p):
                                dmg = ALLY_DAMAGE if b['type'] == 'ally' else 1
                                p['health'] -= dmg
                                hit = True
                                if p['health'] <= 0:
                                    p['health'] = PLAYER_MAX_HEALTH
                                    p['score'] = max(0, p['score'] - 1)
                                    p['deaths'] = p.get('deaths', 0) + 1
                                    p['x'] = random.randint(100, MAP_WIDTH - 100)
                                    p['y'] = random.randint(100, MAP_HEIGHT - 100)
                                    if b['owner'] in state["players"]:
                                        killer = state["players"][b['owner']]
                                        killer['score'] += 2
                                        killer['health'] = min(PLAYER_MAX_HEALTH, killer['health'] + HEALTH_PER_KILL)
                                break

                # 3. Cek tabrakan dengan BOT (hostile / neutral)
                if not hit:
                    for bot in state["bots"]:
                        if bot["id"] == b['owner']:
                            continue  # bot tidak menembak dirinya sendiri
                        dist = math.hypot(b['x'] - bot['x'], b['y'] - bot['y'])
                        if dist < 24:
                            hit = True
                            # Peluru sesama hostile bot tidak saling melukai
                            if bot["kind"] == "hostile" and b['owner'].startswith("bot_"):
                                break
                            bot['hp'] -= ALLY_DAMAGE if b['type'] == 'ally' else 1
                            if bot['hp'] <= 0:
                                state["bots"].remove(bot)
                                owner_id = b['owner']
                                if bot["kind"] == "neutral" and owner_id in state["players"]:
                                    # Bot netral yang kalah jadi ALLY milik penembaknya
                                    convert_neutral_to_ally(bot, owner_id)
                                    state["players"][owner_id]['score'] += 1
                                elif bot["kind"] == "hostile" and owner_id in state["players"]:
                                    killer = state["players"][owner_id]
                                    killer['score'] += 3
                                    killer['health'] = min(PLAYER_MAX_HEALTH, killer['health'] + HEALTH_PER_KILL)
                                # Respawn bot baru sejenis supaya populasi tetap terjaga
                                new_bot = spawn_bot(bot["kind"])
                                if new_bot:
                                    state["bots"].append(new_bot)
                            break

                # 4. Tabrak Obstacle
                if not hit:
                    for obs in state["obstacles"]:
                        dist = math.hypot(b['x'] - obs['x'], b['y'] - obs['y'])
                        if dist < (6 + obs['radius']):
                            obs['hp'] -= 1
                            hit = True
                            if obs['hp'] <= 0:
                                state["obstacles"].remove(obs)
                                # Respawn obstacle baru
                                new_obs = spawn_obstacle()
                                if new_obs and len(state["obstacles"]) < MAX_OBSTACLES:
                                    state["obstacles"].append(new_obs)
                            break

                if not hit and b['life'] > 0:
                    active_bullets.append(b)
            state["bullets"] = active_bullets

            # --- AUTO-KICK PLAYER IDLE / GHOST ---
            # Ini yang memperbaiki bug "karakter tetap stay & respawn terus setelah logout":
            # kalau sudah IDLE_TIMEOUT detik tidak ada request /api/action masuk dari
            # player tsb (artinya koneksi/tab/app-nya sudah mati), paksa kick dari state.
            now_check = time.time()
            idle_ids = [
                pid for pid, pl in state["players"].items()
                if now_check - pl.get('last_active', now_check) > IDLE_TIMEOUT
            ]
            for pid in idle_ids:
                del state["players"][pid]

class GameHandler(http.server.SimpleHTTPRequestHandler):
    def log_message(self, format, *args):
        # Sengaja di-nonaktifkan: default-nya http.server mencetak SETIAP request
        # (tiap POST /api/action & GET /api/state, yang jalan puluhan kali/detik)
        # ke terminal, bikin terminal berat & susah dibaca. Kalau butuh log error
        # asli (mis. exception di server), itu tetap tercetak lewat jalur lain,
        # bukan lewat log_message ini.
        pass
    def _send_json(self, data, status=200):
        self.send_response(status)
        self.send_header("Content-Type", "application/json")
        self.send_header("Access-Control-Allow-Origin", "*")
        self.end_headers()
        self.wfile.write(json.dumps(data).encode("utf-8"))

    def do_GET(self):
        if self.path == "/" or self.path == "/index.html":
            self.send_response(200)
            self.send_header("Content-Type", "text/html")
            self.end_headers()
            html_path = os.path.join("templates", "index.html")
            if os.path.exists(html_path):
                with open(html_path, "rb") as f:
                    self.wfile.write(f.read())
            else:
                self.wfile.write(b"File templates/index.html tidak ditemukan.")
            return

        if self.path == "/api/state":
            with LOCK:
                self._send_json(state)
            return

        # AUDIO PATH (suara tembakan sekarang disintesis di client, tidak pakai file lagi)
        if self.path in ["/adventure.mp3"]:
            file_name = self.path.lstrip("/")
            audio_path = os.path.join("static", file_name)
            if os.path.exists(audio_path):
                self.send_response(200)
                self.send_header("Content-Type", "audio/mpeg")
                self.send_header("Cache-Control", "no-cache")
                self.end_headers()
                with open(audio_path, "rb") as f:
                    self.wfile.write(f.read())
                return

        return http.server.SimpleHTTPRequestHandler.do_GET(self)

    def do_POST(self):
        global next_bullet_id, state
        content_length = int(self.headers.get('Content-Length', 0))
        body = self.rfile.read(content_length).decode('utf-8')
        try: data = json.loads(body) if body else {}
        except: data = {}

        if self.path == "/api/join":
            p_id = data.get("id")
            if not p_id:
                self._send_json({"ok": False, "error": "Missing ID"}, 400)
                return
            with LOCK:
                state["players"][p_id] = {
                    "id": p_id,
                    "name": data.get("name", "Player")[:10],
                    "shape": data.get("shape", "circle"),
                    "color": data.get("color", "#ff0000"),
                    "x": random.randint(200, MAP_WIDTH - 200),
                    "y": random.randint(200, MAP_HEIGHT - 200),
                    "health": PLAYER_MAX_HEALTH,
                    "score": 0,
                    "deaths": 0,
                    "angle": 0,
                    "last_shot": 0,
                    "last_active": time.time(),
                    "allies": []
                }
            self._send_json({"ok": True})
            return

        if self.path == "/api/action":
            p_id = data.get("id")
            if not p_id or p_id not in state["players"]:
                self._send_json({"ok": False, "error": "Player not found"}, 404)
                return
                
            with LOCK:
                p = state["players"][p_id]
                p['last_active'] = time.time()  # heartbeat, dipakai buat auto-kick idle/ghost
                speed = 5
                keys = data.get("keys", {})
                new_x, new_y = p['x'], p['y']
                
                if keys.get('w'): new_y -= speed
                if keys.get('s'): new_y += speed
                if keys.get('a'): new_x -= speed
                if keys.get('d'): new_x += speed
                
                if not obstacle_collision(new_x, new_y, 18):
                    p['x'] = max(18, min(MAP_WIDTH - 18, new_x))
                    p['y'] = max(18, min(MAP_HEIGHT - 18, new_y))
                
                p['angle'] = data.get('angle', 0)
                
                # --- LOGIKA COOLDOWN TEMBAK (SERVER-SIDE, ANTI SPAM) ---
                if data.get("shoot"):
                    now = time.time()
                    if now - p.get('last_shot', 0) >= SHOOT_COOLDOWN:
                        b_speed = 8
                        life = 30
                        b_type = "ranged"
                        size = 5

                        state["bullets"].append({
                            "id": next_bullet_id,
                            "owner": p_id,
                            "type": b_type,
                            "x": p['x'] + math.cos(p['angle']) * 25,
                            "y": p['y'] + math.sin(p['angle']) * 25,
                            "vx": math.cos(p['angle']) * b_speed,
                            "vy": math.sin(p['angle']) * b_speed,
                            "life": life,
                            "size": size
                        })
                        next_bullet_id += 1
                        p['last_shot'] = now
                    
            self._send_json({"ok": True})
            return

        if self.path == "/api/reset":
            if data.get("key") != RESET_KEY:
                self._send_json({"ok": False, "error": "Key reset salah"}, 403)
                return
            with LOCK:
                state["players"] = {}
                state["bullets"] = []
                state["obstacles"] = []
                state["bots"] = []
                for _ in range(MAX_OBSTACLES):
                    obs = spawn_obstacle()
                    if obs:
                        state["obstacles"].append(obs)
                for _ in range(MAX_HOSTILE_BOTS):
                    b = spawn_bot("hostile")
                    if b:
                        state["bots"].append(b)
                for _ in range(MAX_NEUTRAL_BOTS):
                    b = spawn_bot("neutral")
                    if b:
                        state["bots"].append(b)
            self._send_json({"ok": True, "message": "Server berhasil di-reset"})
            return

        if self.path == "/api/leave":
            p_id = data.get("id")
            with LOCK:
                if p_id in state["players"]:
                    del state["players"][p_id]
            self._send_json({"ok": True})
            return

if __name__ == "__main__":
    server_dir = os.path.dirname(os.path.abspath(__file__))
    if server_dir: os.chdir(server_dir)
    threading.Thread(target=game_logic_loop, daemon=True).start()
    socketserver.ThreadingTCPServer.allow_reuse_address = True
    with socketserver.ThreadingTCPServer(("0.0.0.0", PORT), GameHandler) as httpd:
        print(f"✅ Gelutank (by DHAMAS) berjalan di port {PORT}")
        httpd.serve_forever()