#!/usr/bin/env python3
# [CN] 期末專案：多人文字冒險遊戲伺服器（TCP + JSON Line Protocol）
# [EN] Final Project: Multiplayer text-adventure game server (TCP + JSON Line Protocol)

import os
import json
import ssl
import socket
import struct
import threading
import hashlib
import argparse
from dataclasses import dataclass, field
from collections import Counter, defaultdict
from typing import Dict, Tuple, Optional, List

# [CN] 地圖配置：固定 3x3 網格。設計小地圖是為了 Demo 時方便展示邊界與移動。
# [EN] World configuration: fixed 3x3 grid. Small map for easy demonstration of boundaries and movement.
GRID_W, GRID_H = 3, 3

# --------- Persistence paths / 持久化路徑 ----------
# [CN] 使用 JSON 檔案儲存帳號與進度。
# [EN] Persistence using JSON files for accounts and progress.
USERS_FILE = "users.json"
SAVES_DIR = "saves"

# --------- Multicast (Auto-Discovery) / 組播（自動探索） ----------
# [CN] 組播設定：讓客戶端在區網內自動發現伺服器 IP 與埠號。
# [EN] Multicast configuration: Allows clients to auto-discover server IP and port on LAN.
MCAST_GRP = "239.255.0.1"
MCAST_PORT = 19000
MCAST_INTERVAL_SEC = 1.0

# --------- Utilities / 工具函式 ----------

def ensure_dirs():
    """
    [CN] 確保存檔目錄存在。
    [EN] Ensure the save directory exists.
    """
    os.makedirs(SAVES_DIR, exist_ok=True)

def load_json_file(path: str, default):
    """
    [CN] 載入 JSON 檔案，具備防錯機制，若檔案不存在或損壞則回傳預設值。
    [EN] Load JSON file with error handling; returns default if file is missing or corrupt.
    """
    try:
        with open(path, "r", encoding="utf-8") as f:
            return json.load(f)
    except FileNotFoundError:
        return default
    except Exception:
        return default

def save_json_file(path: str, obj) -> None:
    """
    [CN] 安全寫入：先寫入臨時檔 (.tmp) 再更名，確保檔案原子性（Atomic），防止存檔損毀。
    [EN] Safe write: Write to temp (.tmp) then rename to ensure atomicity and prevent corruption.
    """
    tmp = path + ".tmp"
    with open(tmp, "w", encoding="utf-8") as f:
        json.dump(obj, f, ensure_ascii=False, indent=2)
    os.replace(tmp, path)

def make_salt() -> str:
    """ [CN] 生成 16 位元組隨機鹽值。 [EN] Generate 16 bytes random salt. """
    return os.urandom(16).hex()

def hash_pw(pw: str, salt: str) -> str:
    """ [CN] 密碼雜湊：SHA-256(鹽值 + 密碼)。 [EN] Password hashing: SHA-256(salt + password). """
    return hashlib.sha256((salt + pw).encode("utf-8")).hexdigest()

def send_json(conn: socket.socket, obj: dict) -> None:
    """
    [CN] 將 Python 字典轉換為 JSON 並加上換行符 \n 發送。
    [EN] Send Python dict as JSON string terminated by newline \n.
    """
    data = (json.dumps(obj, ensure_ascii=False) + "\n").encode("utf-8")
    conn.sendall(data)

def recv_lines(conn: socket.socket):
    """
    [CN] 關鍵：處理 TCP 成幀（Framing）。因為 TCP 是流導向，需利用 \n 切分完整指令。
    [EN] CRITICAL: Handles TCP Framing. Uses \n to split stream into discrete commands.
    """
    buf = b""
    while True:
        chunk = conn.recv(4096)
        if not chunk: # [CN] 對端關閉連線 [EN] Peer closed connection
            return
        buf += chunk
        while b"\n" in buf:
            line, buf = buf.split(b"\n", 1)
            line = line.strip()
            if line:
                yield line.decode("utf-8", errors="replace")

def in_bounds(x: int, y: int) -> bool:
    """ [CN] 邊界檢查：確保座標在地圖內。 [EN] Boundary check: Ensure (x,y) within grid. """
    return 0 <= x < GRID_W and 0 <= y < GRID_H

def dir_to_delta(direction: str) -> Optional[Tuple[int, int]]:
    """ [CN] 指令解析：將方向字串轉換為座標位移。 [EN] Command parsing: Convert direction to coordinate delta. """
    d = direction.strip().lower()
    if d in ("east", "e"):
        return (1, 0)
    if d in ("west", "w"):
        return (-1, 0)
    if d in ("north", "n"):
        return (0, -1)
    if d in ("south", "s"):
        return (0, 1)
    return None

# --------- Game State / 遊戲狀態 ----------

@dataclass
class Player:
    """ [CN] 玩家連線 Session：儲存線上玩家即時狀態。 [EN] Player connection session: Stores real-time online state. """
    username: str
    conn: socket.socket
    addr: Tuple[str, int]
    x: int = 0
    y: int = 0
    inventory: List[str] = field(default_factory=list)

class GameState:
    """ [CN] 全域狀態機：使用 RLock 確保多執行緒下的資料同步。 [EN] Global state: Uses RLock for data synchronization in multithreading. """
    def __init__(self):
        self.lock = threading.RLock()
        self.rooms: Dict[Tuple[int, int], Counter] = defaultdict(Counter)
        self.players: Dict[str, Player] = {}  # online players: username -> Player
        self.users_db = load_json_file(USERS_FILE, {})  # username -> {salt, hash}

    def load_map(self, path: str):
        """ [CN] 從文字檔讀取地圖初始物品。 [EN] Load initial map items from text file. """
        with open(path, "r", encoding="utf-8") as f:
            for line in f:
                line = line.strip()
                if not line:
                    continue
                parts = line.split()
                if len(parts) < 3:
                    continue
                x, y = int(parts[0]), int(parts[1])
                item = " ".join(parts[2:])
                if in_bounds(x, y):
                    self.rooms[(x, y)][item] += 1

    def save_users(self):
        """ [CN] 將帳號資料庫存入磁碟。 [EN] Save user database to disk. """
        save_json_file(USERS_FILE, self.users_db)

    def save_player_state(self, username: str, x: int, y: int, inventory: List[str]) -> None:
        """ [CN] 儲存特定玩家座標與背包。 [EN] Save specific player coordinates and inventory. """
        ensure_dirs()
        path = os.path.join(SAVES_DIR, f"{username}.json")
        save_json_file(path, {"x": x, "y": y, "inventory": inventory})

    def load_player_state(self, username: str) -> Tuple[int, int, List[str]]:
        """ [CN] 載入存檔，並進行座標與資料結構檢查。 [EN] Load save file; perform coordinate and structure validation. """
        path = os.path.join(SAVES_DIR, f"{username}.json")
        data = load_json_file(path, None)
        if not isinstance(data, dict):
            return (0, 0, [])
        x = int(data.get("x", 0))
        y = int(data.get("y", 0))
        inv = data.get("inventory", [])
        if not isinstance(inv, list):
            inv = []
        if not in_bounds(x, y):
            x, y = 0, 0
        inv = [str(i) for i in inv]
        return (x, y, inv)

    def broadcast_room_event(self, x: int, y: int, msg: str, exclude: Optional[str] = None) -> None:
        """ [CN] 同房間廣播：通知同座標的其他線上玩家。 [EN] Room broadcast: Notify other players at the same coordinates. """
        with self.lock:
            for p in self.players.values():
                if p.x == x and p.y == y and p.username != exclude:
                    try:
                        send_json(p.conn, {"type": "EVENT", "msg": msg})
                    except Exception:
                        pass

    def list_players_in_room(self, x: int, y: int) -> List[str]:
        """ [CN] 獲取房間內的玩家名單。 [EN] Get player names in a specific room. """
        with self.lock:
            names = [p.username for p in self.players.values() if p.x == x and p.y == y]
        names.sort()
        return names

    def list_room_items(self, x: int, y: int) -> List[str]:
        """ [CN] 展開 Counter 並列出房間內所有物品。 [EN] Expand Counter and list all items in the room. """
        with self.lock:
            c = self.rooms[(x, y)]
            items = []
            for item, n in c.items():
                items.extend([item] * n)
        items.sort()
        return items

STATE = GameState()

# --------- Multicast announcer / 組播公告器 ----------
def multicast_announcer(listen_port: int, tls_enabled: bool, stop_evt: threading.Event):
    """
    [CN] 週期性發送組播公告，幫助客戶端自動尋找伺服器。 TTL=1 限制在區網內。
    [EN] Periodically send multicast announcements. TTL=1 limits it to the local subnet.
    """
    msg = f"GAME_SERVER {listen_port} {1 if tls_enabled else 0}".encode("utf-8")
    sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM, socket.IPPROTO_UDP)
    try:
        sock.setsockopt(socket.IPPROTO_IP, socket.IP_MULTICAST_TTL, 1)
        while not stop_evt.is_set():
            try:
                sock.sendto(msg, (MCAST_GRP, MCAST_PORT))
            except Exception:
                pass
            stop_evt.wait(MCAST_INTERVAL_SEC)
    finally:
        try:
            sock.close()
        except Exception:
            pass

# --------- Client handler / 客戶端處理程序 ----------
def require_login(player: Optional[Player], conn: socket.socket) -> bool:
    """ [CN] 指令過濾器：確保後續操作需具備登入 Session。 [EN] Command gate: Ensures login session exists for operations. """
    if player is None:
        send_json(conn, {"ok": False, "msg": "Please Login first."})
        return False
    return True

def handle_client(conn: socket.socket, addr):
    """
    [CN] 客戶端執行緒邏輯：每個連線獨立處理指令。拒絕 Loopback (127.x.x.x) 是為了實網演示安全。
    [EN] Client thread logic: Handles commands per connection. Rejects loopback for real network demo safety.
    """
    # All-or-nothing rule safety: reject loopback
    ip = addr[0]
    if ip.startswith("127."):
        try:
            send_json(conn, {"ok": False, "msg": "Loopback 127.x.x.x is strictly prohibited."})
        except Exception:
            pass
        try:
            conn.close()
        except Exception:
            pass
        return

    username = None
    player: Optional[Player] = None

    try:
        for line in recv_lines(conn):
            try:
                req = json.loads(line)
            except Exception:
                send_json(conn, {"ok": False, "msg": "Invalid JSON"})
                continue

            cmd = str(req.get("cmd", "")).upper()

            # --------- Player management (Register/Login/Save/Exit) / 玩家管理 ----------
            if cmd == "REGISTER":
                """ [CN] 註冊新帳號：儲存鹽值與雜湊後的密碼。 [EN] Register account: Store salt and hashed password. """
                u = str(req.get("name", "")).strip()
                pw = str(req.get("password", "")).strip()
                if not u or not pw:
                    send_json(conn, {"ok": False, "msg": "Usage: REGISTER {name, password}"})
                    continue
                with STATE.lock:
                    if u in STATE.users_db:
                        send_json(conn, {"ok": False, "msg": "Username already exists."})
                        continue
                    salt = make_salt()
                    STATE.users_db[u] = {"salt": salt, "hash": hash_pw(pw, salt)}
                    STATE.save_users()
                send_json(conn, {"ok": True, "msg": f"Registered user: {u}"})
                continue

            if cmd == "LOGIN":
                """ [CN] 登入驗證、載入進度並建立線上 Session。 [EN] Login verification, load progress, and establish session. """
                u = str(req.get("name", "")).strip()
                pw = str(req.get("password", "")).strip()
                if not u or not pw:
                    send_json(conn, {"ok": False, "msg": "Usage: LOGIN {name, password}"})
                    continue

                with STATE.lock:
                    rec = STATE.users_db.get(u)
                    if not rec:
                        send_json(conn, {"ok": False, "msg": "User not found. Please Register first."})
                        continue
                    if rec.get("hash") != hash_pw(pw, rec.get("salt", "")):
                        send_json(conn, {"ok": False, "msg": "Wrong password."})
                        continue
                    if u in STATE.players:
                        send_json(conn, {"ok": False, "msg": "This user is already online."})
                        continue

                    x, y, inv = STATE.load_player_state(u)
                    username = u
                    player = Player(username=u, conn=conn, addr=addr, x=x, y=y, inventory=inv)
                    STATE.players[u] = player

                send_json(conn, {"ok": True, "msg": f"Login OK. Welcome {u}. Location: {player.x} {player.y}"})
                STATE.broadcast_room_event(player.x, player.y, f"{u} entered the room.", exclude=u)
                continue

            if cmd == "SAVE":
                """ [CN] 存檔：保存位置與背包至磁碟。 [EN] Save: Persist position and inventory to disk. """
                if not require_login(player, conn):
                    continue
                with STATE.lock:
                    STATE.save_player_state(player.username, player.x, player.y, list(player.inventory))
                send_json(conn, {"ok": True, "msg": "Game saved."})
                continue

            if cmd in ("EXIT", "QUIT"):
                """ [CN] 直接斷開連線。 [EN] Direct disconnect. """
                send_json(conn, {"ok": True, "msg": "Bye!"})
                break

            if cmd == "SAVE_EXIT":
                """ [CN] 存檔並斷開連線（符合期末要求）。 [EN] Save and disconnect (meets project requirement). """
                if not require_login(player, conn):
                    continue
                with STATE.lock:
                    STATE.save_player_state(player.username, player.x, player.y, list(player.inventory))
                send_json(conn, {"ok": True, "msg": "Saved. Bye!"})
                break

            # --------- Mandatory gameplay (requires login) / 遊戲功能 ----------
            if not require_login(player, conn):
                continue

            if cmd == "LOOK":
                """ [CN] 查看周圍：列出位置、同房玩家與物品。 [EN] Look: List location, players in room, and items. """
                with STATE.lock:
                    x, y = player.x, player.y
                    ps = STATE.list_players_in_room(x, y)
                    ps_display = [f"{n}(Me)" if n == player.username else n for n in ps]
                    items = STATE.list_room_items(x, y)
                send_json(conn, {"ok": True, "type": "LOOK", "location": [x, y], "players": ps_display, "items": items})
                continue

            if cmd == "INVENTORY":
                """ [CN] 檢查背包。 [EN] Check inventory. """
                with STATE.lock:
                    inv = list(player.inventory)
                send_json(conn, {"ok": True, "type": "INVENTORY", "items": inv})
                continue

            if cmd == "MOVE":
                """ [CN] 移動邏輯：更新座標並通知舊房與新房。 [EN] Move logic: update coordinates and notify rooms. """
                direction = str(req.get("direction", "")).strip()
                delta = dir_to_delta(direction)
                if not delta:
                    send_json(conn, {"ok": False, "msg": "Usage: Move North/South/East/West"})
                    continue
                dx, dy = delta
                with STATE.lock:
                    ox, oy = player.x, player.y
                    nx, ny = ox + dx, oy + dy
                    if not in_bounds(nx, ny):
                        send_json(conn, {"ok": False, "msg": "You hit the wall (out of map)."})
                        continue
                    player.x, player.y = nx, ny

                send_json(conn, {"ok": True, "msg": f"{player.username} moved to {nx} {ny}"})
                STATE.broadcast_room_event(ox, oy, f"{player.username} left the room.", exclude=player.username)
                STATE.broadcast_room_event(nx, ny, f"{player.username} entered the room.", exclude=player.username)
                continue

            if cmd == "TAKE":
                """ [CN] 拾取物品：修改地圖資料並放入背包。 [EN] Take item: Modify map data and put in inventory. """
                item = str(req.get("item", "")).strip()
                if not item:
                    send_json(conn, {"ok": False, "msg": 'Usage: Take <ItemName>'})
                    continue
                with STATE.lock:
                    x, y = player.x, player.y
                    if STATE.rooms[(x, y)][item] <= 0:
                        send_json(conn, {"ok": False, "msg": f'No such item "{item}" in this room.'})
                        continue
                    STATE.rooms[(x, y)][item] -= 1
                    if STATE.rooms[(x, y)][item] == 0:
                        del STATE.rooms[(x, y)][item]
                    player.inventory.append(item)

                send_json(conn, {"ok": True, "msg": f'{player.username} took "{item}"'})
                STATE.broadcast_room_event(x, y, f'{player.username} took "{item}".', exclude=player.username)
                continue

            if cmd == "DEPOSIT":
                """ [CN] 丟棄物品。 [EN] Deposit item. """
                item = str(req.get("item", "")).strip()
                if not item:
                    send_json(conn, {"ok": False, "msg": 'Usage: Deposit <ItemName>'})
                    continue
                with STATE.lock:
                    if item not in player.inventory:
                        send_json(conn, {"ok": False, "msg": f'You do not have "{item}".'})
                        continue
                    player.inventory.remove(item)
                    x, y = player.x, player.y
                    STATE.rooms[(x, y)][item] += 1

                send_json(conn, {"ok": True, "msg": f"{player.username} deposited {item}"})
                STATE.broadcast_room_event(x, y, f'{player.username} deposited "{item}".', exclude=player.username)
                continue

            # --------- Bonus: Give (same room) / 加分：轉交 ----------
            if cmd == "GIVE":
                to = str(req.get("to", "")).strip()
                item = str(req.get("item", "")).strip()
                if not to or not item:
                    send_json(conn, {"ok": False, "msg": 'Usage: Give <player> <item>'})
                    continue
                with STATE.lock:
                    if item not in player.inventory:
                        send_json(conn, {"ok": False, "msg": f'You do not have "{item}".'})
                        continue
                    target = STATE.players.get(to)
                    if not target:
                        send_json(conn, {"ok": False, "msg": "Target not found or not online."})
                        continue
                    if (target.x, target.y) != (player.x, player.y):
                        send_json(conn, {"ok": False, "msg": "Target is not in the same room."})
                        continue
                    player.inventory.remove(item)
                    target.inventory.append(item)

                send_json(conn, {"ok": True, "msg": f'You gave "{item}" to {to}.'})
                send_json(target.conn, {"type": "EVENT", "msg": f'{player.username} gave you "{item}".'})
                STATE.broadcast_room_event(player.x, player.y, f'{player.username} gave "{item}" to {to}.', exclude=None)
                continue

            # --------- Bonus: Tell (private whisper) / 加分：私聊 ----------
            if cmd == "TELL":
                to = str(req.get("to", "")).strip()
                text = str(req.get("msg", "")).strip()
                if not to or not text:
                    send_json(conn, {"ok": False, "msg": 'Usage: Tell <player> <message>'})
                    continue
                with STATE.lock:
                    target = STATE.players.get(to)
                if not target:
                    send_json(conn, {"ok": False, "msg": "Target not found or not online."})
                    continue
                send_json(conn, {"ok": True, "msg": f'(whisper to {to}) {text}'})
                send_json(target.conn, {"type": "EVENT", "msg": f'(whisper from {player.username}) {text}'})
                continue

            send_json(conn, {"ok": False, "msg": "Unknown command."})

    except Exception:
        pass
    finally:
        # [CN] 斷線清理：將玩家移出線上列表。 [EN] Disconnect cleanup: Remove player from online registry.
        try:
            conn.close()
        except Exception:
            pass

        if player is not None:
            with STATE.lock:
                if STATE.players.get(player.username) is player:
                    del STATE.players[player.username]
            STATE.broadcast_room_event(player.x, player.y, f"{player.username} disconnected.", exclude=player.username)

def create_listen_socket_dualstack(host_port: int) -> socket.socket:
    """
    [CN] 建立雙疊疊 (Dual-stack) Socket。同時監聽 IPv4 與 IPv6。
    [EN] Create dual-stack socket. Handles both IPv4 and IPv6 connections.
    """
    s = socket.socket(socket.AF_INET6, socket.SOCK_STREAM)
    s.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
    try:
        s.setsockopt(socket.IPPROTO_IPV6, socket.IPV6_V6ONLY, 0)  # dual-stack
    except Exception:
        pass
    s.bind(("::", host_port))
    s.listen(100)
    return s

def wrap_server_tls_if_needed(raw_conn: socket.socket, tls_ctx: Optional[ssl.SSLContext]) -> socket.socket:
    """ [CN] 若啟用則包裝 TLS。 [EN] Wrap connection with TLS if enabled. """
    if tls_ctx is None:
        return raw_conn
    return tls_ctx.wrap_socket(raw_conn, server_side=True)

def serve(port: int, map_path: str, tls_enabled: bool, cert: str, key: str):
    """ [CN] 伺服器主循環：處理 Multicast、建立監聽與分發執行緒。 [EN] Main loop: Handle multicast, setup listening, and dispatch threads. """
    ensure_dirs()
    STATE.load_map(map_path)
    print(f"[Server] Map loaded from {map_path}")
    print(f"[Server] Dual-stack listening on :::{port}  (IPv4+IPv6)")

    tls_ctx = None
    if tls_enabled:
        tls_ctx = ssl.SSLContext(ssl.PROTOCOL_TLS_SERVER)
        tls_ctx.load_cert_chain(certfile=cert, keyfile=key)
        print("[Server] TLS enabled")

    # multicast announcer
    stop_evt = threading.Event()
    t = threading.Thread(target=multicast_announcer, args=(port, tls_enabled, stop_evt), daemon=True)
    t.start()
    print(f"[Server] Multicast auto-discovery: {MCAST_GRP}:{MCAST_PORT}")

    s = create_listen_socket_dualstack(port)

    try:
        while True:
            raw_conn, addr = s.accept()
            try:
                conn = wrap_server_tls_if_needed(raw_conn, tls_ctx)
            except Exception:
                try:
                    raw_conn.close()
                except Exception:
                    pass
                continue
            th = threading.Thread(target=handle_client, args=(conn, addr), daemon=True)
            th.start()
    finally:
        stop_evt.set()
        try:
            s.close()
        except Exception:
            pass

if __name__ == "__main__":
    # [CN] 命令列參數解析。 [EN] CLI Argument parsing.
    ap = argparse.ArgumentParser()
    ap.add_argument("--port", type=int, default=7777)
    ap.add_argument("--map", type=str, default="map.txt")
    ap.add_argument("--tls", action="store_true", help="Enable TLS")
    ap.add_argument("--cert", type=str, default="cert.pem")
    ap.add_argument("--key", type=str, default="key.pem")
    args = ap.parse_args()

    serve(port=args.port, map_path=args.map, tls_enabled=args.tls, cert=args.cert, key=args.key)
