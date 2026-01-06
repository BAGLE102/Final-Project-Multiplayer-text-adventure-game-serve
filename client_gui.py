#!/usr/bin/env python3
import socket
import ssl
import json
import struct
import threading
import tkinter as tk
from tkinter import ttk, messagebox
from typing import Optional, Tuple

# ============================================================
# [SECTION 1] Global Constants / 全域常數設定
# ============================================================

# [CN] 這些設定必須與伺服器端 (server_full.py) 嚴格一致。
# [EN] These constants must strictly match the server_full.py configuration.
MCAST_GRP = "239.255.0.1"      # [CN] IPv4 組播群組位址 / [EN] IPv4 Multicast group address
MCAST_PORT = 19000             # [CN] 組播監聽埠號 / [EN] Multicast listening port
DISCOVER_TIMEOUT_SEC = 2.0     # [CN] 自動探索逾時時間（秒）/ [EN] Discovery timeout in seconds

# ============================================================
# [SECTION 2] Networking Helpers / 網路底層工具函式
# ============================================================

def send_json(conn: socket.socket, obj: dict) -> None:
    """
    [CN] 將 Python 字典轉換為 JSON 字串，並附加換行符 (\n) 作為訊息邊界後發送。
    [EN] Convert a dict to a JSON string and append a newline (\n) as a message boundary before sending.
    """
    # [CN] 加上 "\n" 是為了讓伺服器端的 recv_lines 能夠正確切分封包。
    # [EN] Adding "\n" ensures the server-side recv_lines can correctly split packets.
    conn.sendall((json.dumps(obj, ensure_ascii=False) + "\n").encode("utf-8"))


def recv_lines(conn: socket.socket):
    """
    [CN] 解決 TCP「黏包」問題。TCP 是串流導向，此函數負責將連綿的字節依照 \n 切分為獨立指令。
    [EN] Solves TCP "Sticky Packet" issues. TCP is stream-oriented; this yields discrete commands by splitting bytes at \n.
    """
    buf = b"" # [CN] 緩衝區：存儲尚未湊成完整一行的殘餘資料 / [EN] Buffer: Stores partial data
    while True:
        # [CN] 每次從 Socket 讀取 4096 Byte / [EN] Read 4096 bytes per chunk
        chunk = conn.recv(4096)
        if not chunk:
            return # [CN] Socket 已關閉 / [EN] Socket closed
        buf += chunk
        # [CN] 檢查緩衝區是否有換行符，以此切分語義完整的封包
        # [EN] Check if buffer contains a newline to split semantically complete packets
        while b"\n" in buf:
            line, buf = buf.split(b"\n", 1)
            line = line.strip()
            if line:
                # [CN] 使用產生器產出字串，節省記憶體並方便迴圈處理
                # [EN] Yield string using a generator for memory efficiency and loop processing
                yield line.decode("utf-8", errors="replace")


def discover_server() -> Optional[Tuple[str, int, bool]]:
    """
    [CN] 使用 UDP Multicast (組播) 自動發現區網內的伺服器。
    [EN] Use UDP Multicast to auto-discover the game server on the local network.
    
    [CN] 回傳值：(伺服器 IP, 埠號, 是否啟用 TLS)
    [EN] Returns: (server_ip, port, tls_enabled)
    """
    # [CN] 建立 IPv4 UDP Socket / [EN] Create IPv4 UDP socket
    s = socket.socket(socket.AF_INET, socket.SOCK_DGRAM, socket.IPPROTO_UDP)
    try:
        # [CN] SO_REUSEADDR 允許在同一埠號重複綁定（這對組播監聽很重要）
        # [EN] SO_REUSEADDR allows rebinding to the same port (critical for multicast listening)
        s.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
        s.bind(("", MCAST_PORT))
        
        # [CN] 核心：建構 IP_ADD_MEMBERSHIP 結構以加入組播群組
        # [EN] Core: Construct IP_ADD_MEMBERSHIP struct to join the multicast group
        # [CN] struct.pack 將資料轉為 C 語言結構體，inet_aton 將字串轉為二進制 IP
        # [EN] struct.pack converts data to C-struct format; inet_aton converts string to binary IP
        mreq = struct.pack("4s4s", socket.inet_aton(MCAST_GRP), socket.inet_aton("0.0.0.0"))
        s.setsockopt(socket.IPPROTO_IP, socket.IP_ADD_MEMBERSHIP, mreq)
        
        # [CN] 設置逾時以免無限等待 / [EN] Set timeout to avoid infinite waiting
        s.settimeout(DISCOVER_TIMEOUT_SEC)
        
        # [CN] 接收組播封包 / [EN] Receive multicast packet
        data, (ip, _) = s.recvfrom(1024)
        txt = data.decode("utf-8", errors="replace").strip()
        parts = txt.split()
        
        # [CN] 解析伺服器宣告格式: "GAME_SERVER <port> <tls_flag>"
        # [EN] Parse announcement: "GAME_SERVER <port> <tls_flag>"
        if len(parts) >= 3 and parts[0] == "GAME_SERVER":
            port = int(parts[1])
            tls = (parts[2] == "1")
            return (ip, port, tls)
        return None
    except Exception:
        return None
    finally:
        try:
            s.close()
        except Exception:
            pass


def connect_dualstack(host: str, port: int, tls: bool) -> socket.socket:
    """
    [CN] 建立雙疊疊 (IPv4+IPv6) TCP 連線，並視需求封裝 TLS。
    [EN] Establish a Dual-stack (IPv4+IPv6) TCP connection, optionally wrapped in TLS.
    """
    # [CN] AF_UNSPEC 允許 getaddrinfo 回傳 IPv4 或 IPv6 的位址資訊
    # [EN] AF_UNSPEC allows getaddrinfo to return both IPv4 and IPv6 address info
    infos = socket.getaddrinfo(host, port, socket.AF_UNSPEC, socket.SOCK_STREAM)
    last_err = None
    
    # [CN] 遍歷所有可能的位址（Happy Eyeballs 邏輯雛形）
    # [EN] Iterate through all candidate addresses (rudimentary Happy Eyeballs logic)
    for af, socktype, proto, canon, sa in infos:
        raw = None
        try:
            # [CN] 建立 Socket (可能是 AF_INET 或 AF_INET6)
            # [EN] Create socket (could be AF_INET or AF_INET6)
            raw = socket.socket(af, socktype, proto)
            raw.connect(sa) # [CN] 發起 TCP 三向交握 / [EN] Initiate TCP 3-way handshake
            
            if not tls:
                return raw # [CN] 一般 TCP 連線 / [EN] Plain TCP connection
            
            # [CN] 封裝 TLS 加密層 / [EN] Wrap with TLS encryption layer
            # [CN] 注意：此處設為不驗證證書 (CERT_NONE)，適合開發與演示使用。
            # [EN] Note: verify_mode is set to CERT_NONE, suitable for dev/demo.
            ctx = ssl.create_default_context()
            ctx.check_hostname = False
            ctx.verify_mode = ssl.CERT_NONE
            return ctx.wrap_socket(raw, server_hostname="game-server")
            
        except OSError as e:
            last_err = e
            try:
                if raw: raw.close() # [CN] 失敗則關閉，嘗試下一個位址 / [EN] Close on failure, try next
            except Exception: pass
            continue
            
    # [CN] 若所有位址都連不上則拋出例外 / [EN] Raise exception if all candidates fail
    raise OSError(f"Failed to connect: {last_err}")


# ----------------- GUI / 圖形介面實作 -----------------
class GameGUI:
    """
    [CN] GameGUI 類別：管理遊戲的所有視覺組件與畫面狀態。
    [EN] GameGUI class: Manages all visual components and screen states of the game.
    """
    def __init__(self, root: tk.Tk):
        """
        [CN] 初始化 GUI：設定視窗屬性、定義畫面容器並構建初始介面。
        [EN] Initialize GUI: Set window properties, define screen containers, and build initial interfaces.
        """
        self.root = root
        self.root.title("Multi-user Online Game (GUI Client)") # [CN] 設定視窗標題 / [EN] Set window title
        self.root.geometry("980x620") # [CN] 設定視窗固定解析度 / [EN] Set window fixed resolution

        # [CN] 網路狀態變數初始化 / [EN] Network state variables initialization
        self.conn: Optional[socket.socket] = None
        self.rx_thread: Optional[threading.Thread] = None # [CN] 異步接收執行緒引用的預留位 / [EN] Placeholder for async receive thread reference
        self.connected = False

        # Screens (frames)
        # [CN] 畫面容器 (Frames)：使用不同的 Frame 作為獨立的畫面「頁面」。
        # [EN] Screen containers (Frames): Use different Frames as independent "pages" for screens.
        self.screen_start = ttk.Frame(root, padding=30)     # [CN] 開始/標題畫面 / [EN] Start/Title screen
        self.screen_connect = ttk.Frame(root, padding=20)   # [CN] 連線方式選擇 / [EN] Connection choice screen
        self.screen_manual = ttk.Frame(root, padding=20)    # [CN] 手動輸入 IP/Port / [EN] Manual IP/Port input
        self.screen_game = ttk.Frame(root, padding=10)      # [CN] 遊戲主控面板 / [EN] Main game control panel

        # [CN] 預先構建所有畫面 (但此時不顯示)
        # [EN] Pre-build all screens (but do not display them yet)
        self._build_start()
        self._build_connect_choice()
        self._build_manual()
        self._build_game()

        # [CN] 啟動程式時顯示「開始」畫面 / [EN] Display the "Start" screen when launching
        self.show_screen(self.screen_start)

    def show_screen(self, frame: ttk.Frame):
        """
        [CN] 畫面切換邏輯：隱藏所有 Frame 並僅顯示指定的 Frame（實作簡單的頁面路由）。
        [EN] Screen switching logic: Hide all Frames and show only the specified Frame (implementing simple page routing).
        """
        for f in (self.screen_start, self.screen_connect, self.screen_manual, self.screen_game):
            f.pack_forget() # [CN] 暫時移除佈局管理器中的 Frame / [EN] Temporarily remove Frame from layout manager
        frame.pack(fill="both", expand=True) # [CN] 顯示並填滿整個視窗 / [EN] Display and fill the entire window

    # -------- Screen 1: Start / 開始畫面組件 --------
    def _build_start(self):
        f = self.screen_start
        # [CN] 標題文字佈置 / [EN] Title text layout
        title = ttk.Label(f, text="Multi-user Online Game", font=("Arial", 26, "bold"))
        title.pack(pady=(40, 20))

        subtitle = ttk.Label(f, text="Client GUI", font=("Arial", 14))
        subtitle.pack(pady=(0, 30))

        # [CN] 開始按鈕：點擊後觸發 show_screen 跳轉至連線畫面
        # [EN] Start button: Triggers show_screen to jump to connection screen upon click
        btn = ttk.Button(f, text="Start Game", command=lambda: self.show_screen(self.screen_connect))
        btn.pack(ipadx=40, ipady=12)

        # [CN] 專案資訊標籤 / [EN] Project information label
        note = ttk.Label(
            f,
            text="網路程式設計 Final Project\n Group 9 614430005 况旻諭",
            foreground="#555",
            justify="center",
        )
        note.pack(pady=(30, 0))

    # -------- Screen 2: Choose connect method / 選擇連線方式組件 --------
    def _build_connect_choice(self):
        f = self.screen_connect
        ttk.Label(f, text="Choose Connection Method", font=("Arial", 18, "bold")).pack(pady=(10, 30))

        box = ttk.Frame(f) # [CN] 建立子容器以進行按鈕排版 / [EN] Create sub-container for button alignment
        box.pack(pady=10)

        # [CN] 自動探索與手動輸入按鈕佈置 / [EN] Auto-discover and manual input button layout
        btn_discover = ttk.Button(box, text="Discover (Multicast)", command=self.on_discover_connect)
        btn_manual = ttk.Button(box, text="Manual Input")

        # [CN] 使用 Grid 進行水平排列 / [EN] Use Grid for horizontal alignment
        btn_discover.grid(row=0, column=0, padx=15, ipadx=20, ipady=10)
        btn_manual.grid(row=0, column=1, padx=15, ipadx=20, ipady=10)

        # [CN] 綁定切換畫面指令 / [EN] Bind screen switching command
        btn_manual.configure(command=lambda: self.show_screen(self.screen_manual))

        # [CN] 狀態回傳字串：動態更新「探索中」或「探索失敗」等資訊
        # [EN] Status variable: Dynamically update info like "Discovering" or "Discover failed"
        self.discover_status = tk.StringVar(value="")
        ttk.Label(f, textvariable=self.discover_status, foreground="#444").pack(pady=(25, 0))

        # [CN] 回到上一頁按鈕 / [EN] Back to previous page button
        ttk.Button(f, text="Back", command=lambda: self.show_screen(self.screen_start)).pack(pady=(30, 0))

    # -------- Screen 3: Manual connect / 手動連線畫面組件 --------
    def _build_manual(self):
        f = self.screen_manual
        ttk.Label(f, text="Manual Connection", font=("Arial", 18, "bold")).pack(pady=(10, 20))

        form = ttk.Frame(f) # [CN] 表單容器 / [EN] Form container
        form.pack(pady=10)

        # [CN] 綁定 Tkinter 變數，方便後續直接讀取輸入框的值
        # [EN] Bind Tkinter variables for easy retrieval of entry values later
        self.host_var = tk.StringVar(value="fe80::4f84:8db3:ff8c:e8e7") # [CN] 預設 IPv6 地址 / [EN] Default IPv6 address
        self.port_var = tk.StringVar(value="7777")
        self.tls_var = tk.BooleanVar(value=True) # [CN] 預設開啟 TLS 加密 / [EN] Default TLS encryption enabled

        # [CN] 使用 Grid 佈局對齊標籤與輸入框
        # [EN] Use Grid layout to align labels and entries
        ttk.Label(form, text="Server Host/IP:").grid(row=0, column=0, sticky="e", padx=6, pady=6)
        ttk.Entry(form, textvariable=self.host_var, width=28).grid(row=0, column=1, padx=6, pady=6)

        ttk.Label(form, text="Port:").grid(row=1, column=0, sticky="e", padx=6, pady=6)
        ttk.Entry(form, textvariable=self.port_var, width=10).grid(row=1, column=1, sticky="w", padx=6, pady=6)

        # [CN] TLS 選擇框 / [EN] TLS Checkbutton
        ttk.Checkbutton(form, text="Use TLS", variable=self.tls_var).grid(row=2, column=1, sticky="w", padx=6, pady=6)

        btnrow = ttk.Frame(f)
        btnrow.pack(pady=15)

        # [CN] 連線確認與返回按鈕 / [EN] Connect confirm and back buttons
        ttk.Button(btnrow, text="Connect", command=self.on_manual_connect).grid(row=0, column=0, padx=10, ipadx=12, ipady=6)
        ttk.Button(btnrow, text="Back", command=lambda: self.show_screen(self.screen_connect)).grid(row=0, column=1, padx=10, ipadx=12, ipady=6)

        self.manual_status = tk.StringVar(value="")
        ttk.Label(f, textvariable=self.manual_status, foreground="#444").pack(pady=(10, 0))

    # -------- Screen 4: Game UI / 遊戲主介面組件 --------
    def _build_game(self):
        """
        [CN] 構建遊戲主畫面佈局：包含日誌、指令、玩家名單、背包與地圖資訊。
        [EN] Build main game screen layout: includes logs, commands, player list, inventory, and map info.
        """
        f = self.screen_game

        # Top bar
        # [CN] 頂部狀態列：顯示當前連線資訊與斷線按鈕。
        # [EN] Top bar: Shows current connection info and disconnect button.
        top = ttk.Frame(f)
        top.pack(fill="x", pady=(0, 8))

        self.conn_info = tk.StringVar(value="Not connected")
        ttk.Label(top, textvariable=self.conn_info, font=("Arial", 10, "bold")).pack(side="left")

        ttk.Button(top, text="Disconnect", command=self.disconnect).pack(side="right")

        # Main split
        # [CN] 主視窗分割：左側為事件日誌，右側為狀態面板。
        # [EN] Main split: Left side for event logs, right side for status panels.
        main = ttk.Frame(f)
        main.pack(fill="both", expand=True)

        left = ttk.Frame(main) # [CN] 左側垂直容器 / [EN] Left vertical container
        left.pack(side="left", fill="both", expand=True, padx=(0, 8))

        right = ttk.Frame(main) # [CN] 右側狀態欄容器 / [EN] Right sidebar container
        right.pack(side="right", fill="y")

        # --- Left: Output log ---
        # [CN] 事件輸出框：顯示遊戲事件與伺服器回報資訊。
        # [EN] Output log: Displays game events and server response info.
        out_box = ttk.LabelFrame(left, text="Output / Events", padding=8)
        out_box.pack(fill="both", expand=True)

        self.log = tk.Text(out_box, height=12, wrap="word") # [CN] 文字區域 / [EN] Text area
        self.log.pack(fill="both", expand=True)
        self.log.configure(state="disabled") # [CN] 初始設為唯讀，防止使用者修改 / [EN] Read-only initially to prevent user modification

        # --- Left bottom: command line ---
        # [CN] 指令輸入列：手動輸入字串指令。
        # [EN] Command line: For manually entering text commands.
        cmd_box = ttk.LabelFrame(left, text="Command Line", padding=8)
        cmd_box.pack(fill="x", pady=(8, 0))

        self.cmd_var = tk.StringVar()
        ent = ttk.Entry(cmd_box, textvariable=self.cmd_var)
        ent.pack(side="left", fill="x", expand=True, padx=(0, 6))
        ent.bind("<Return>", lambda e: self.on_send_command()) # [CN] 綁定 Enter 鍵直接發送 / [EN] Bind Enter key to send directly
        ttk.Button(cmd_box, text="Send", command=self.on_send_command).pack(side="left")
        
        # --- Left bottom: actions (buttons) ---
        # [CN] 快速動作按鈕面板預留容器。
        # [EN] Reserved container for quick action button panel.
        action_panel = ttk.Frame(left)
        action_panel.pack(fill="x", pady=(8, 0))
        ap_left = ttk.Frame(action_panel)
        ap_right = ttk.Frame(action_panel)
        ap_left.pack(side="left", fill="x", expand=True, padx=(0, 6))
        ap_right.pack(side="left", fill="x", expand=True, padx=(6, 0))



# ---------------- Right side / 右側側邊欄佈局 ----------------
        # [CN] 右側區域主要負責系統層級的操作（登入、狀態顯示）與進階指令容器。
        # [EN] The right area is mainly responsible for system-level operations (login, state display) and advanced command containers.

        # ===== Auth panel (your requested UI) / 身分驗證面板 =====
        # [CN] 使用 LabelFrame 建立一個有標題的框組，將「身分驗證」相關功能群組化。
        # [EN] Use LabelFrame to create a titled grouping for all "Authentication" related features.
        auth_panel = ttk.LabelFrame(right, text="Authentication", padding=10)
        auth_panel.pack(fill="x", pady=(0, 8))

        # state: which form is open
        # [CN] 狀態管理：使用 StringVar 追蹤目前的表單模式與輸入欄位，這是 Tkinter 實現資料與 UI 綁定的核心。
        # [EN] State management: Use StringVar to track current form modes and input fields, core for Tkinter's data-UI binding.
        self.auth_mode = tk.StringVar(value="")  # "", "login", "register"
        self.auth_name = tk.StringVar()          # [CN] 儲存使用者輸入的名稱 / [EN] Stores user-entered name
        self.auth_pw = tk.StringVar()            # [CN] 儲存使用者輸入的密碼 / [EN] Stores user-entered password

        # Top row buttons
        # [CN] 按鈕列容器：用於並排「登入」與「註冊」切換按鈕。
        # [EN] Button row container: Used for aligning "Login" and "Register" toggle buttons side-by-side.
        btn_row = ttk.Frame(auth_panel)
        btn_row.pack(fill="x")

        def show_login_form():
            """ [CN] 切換至登入模式並重新渲染表單。 [EN] Switch to login mode and re-render the form. """
            self.auth_mode.set("login")
            self._render_auth_form()

        def show_register_form():
            """ [CN] 切換至註冊模式並重新渲染表單。 [EN] Switch to register mode and re-render the form. """
            self.auth_mode.set("register")
            self._render_auth_form()

        # [CN] 配置 Login/Register 按鈕，使用 expand=True 確保平分空間。
        # [EN] Configure Login/Register buttons; use expand=True to ensure equal spacing.
        ttk.Button(btn_row, text="Login", command=show_login_form).pack(side="left", expand=True, fill="x", padx=(0, 6))
        ttk.Button(btn_row, text="Register", command=show_register_form).pack(side="left", expand=True, fill="x", padx=(6, 0))

        # Form container (dynamic)
        # [CN] 動態表單容器：根據上述 auth_mode 的切換，此區塊會被清空並重新填入輸入欄位。
        # [EN] Dynamic form container: Based on the auth_mode toggle, this area is cleared and refilled with input entries.
        self.auth_form_container = ttk.Frame(auth_panel)
        self.auth_form_container.pack(fill="x", pady=(10, 0))

        # hint
        # [CN] 驗證提示文字：顯示如「請先登入」或「正在驗證中...」等系統反饋。
        # [EN] Auth hint text: Displays system feedback like "Please login" or "Verifying...".
        self.auth_hint = tk.StringVar(value="Please Login or Register first.")
        ttk.Label(auth_panel, textvariable=self.auth_hint, foreground="#444").pack(anchor="w", pady=(8, 0))

        # render first time (no form)
        # [CN] 初始化渲染：剛連線時顯示預設的提示狀態。
        # [EN] Initial rendering: Shows the default hint state upon connection.
        self._render_auth_form()

        # ===== State panel / 狀態面板 =====
        # [CN] 狀態看板：用於顯示從伺服器回傳的遊戲環境即時數據（位置、人物、物品）。
        # [EN] State panel: Used to display real-time game environment data returned from the server (location, people, items).
        state_box = ttk.LabelFrame(right, text="State", padding=8)
        state_box.pack(fill="x", pady=(0, 8))

        # [CN] 顯示當前座標 (x, y)。 [EN] Displays current coordinates (x, y).
        self.loc_var = tk.StringVar(value="Location: ? ?")
        ttk.Label(state_box, textvariable=self.loc_var).pack(anchor="w")

        # [CN] 使用 Listbox 展示列表，這比單純文字更好用，因為玩家可以直接點擊列表中的項。
        # [EN] Use Listbox for lists; superior to plain text as players can directly click items in the list.
        
        ttk.Label(state_box, text="Players in room:").pack(anchor="w", pady=(6, 0))
        self.players_list = tk.Listbox(state_box, height=6)
        self.players_list.pack(fill="x")

        ttk.Label(state_box, text="Items in room:").pack(anchor="w", pady=(6, 0))
        self.room_items_list = tk.Listbox(state_box, height=7)
        self.room_items_list.pack(fill="x")

        ttk.Label(state_box, text="My Inventory:").pack(anchor="w", pady=(6, 0))
        self.inv_list = tk.Listbox(state_box, height=7)
        self.inv_list.pack(fill="x")

        # ===== Controls panel (locked until login success) / 控制項面板 =====
        # [CN] 控制區容器：這兩個容器將掛載到左側底部的 action_panel 中，實現跨區域排版。
        # [EN] Control containers: These will be mounted into the action_panel at the bottom left for cross-area layout.
        self.ctrl_container_left = ttk.Frame(ap_left)
        self.ctrl_container_right = ttk.Frame(ap_right)
        self.ctrl_container_left.pack(fill="x")
        self.ctrl_container_right.pack(fill="x")

        # [CN] 基礎控制框：作為後續按鈕（如 Look, Inventory）的父容器。
        # [EN] Basic control box: Parent container for subsequent buttons (e.g., Look, Inventory).
        ctrl_box = ctrl_box = ttk.LabelFrame(self.ctrl_container_left, text="Controls", padding=8)
        ctrl_box.pack(fill="x", pady=(0, 8))

        # ============================================================
        # [SECTION 3] Action Buttons / 遊戲操作按鈕
        # ============================================================

        # --- Basic / 基礎指令 ---
        # [CN] 建立 Look 與 Inventory 按鈕。使用 lambda 延遲執行，按下時發送 JSON 指令。
        # [EN] Create Look and Inventory buttons. Use lambda for deferred execution to send JSON commands on click.
        self.btn_look = ttk.Button(ctrl_box, text="Look", command=lambda: self.send_cmd({"cmd": "LOOK"}))
        self.btn_inv = ttk.Button(ctrl_box, text="Inventory", command=lambda: self.send_cmd({"cmd": "INVENTORY"}))
        self.btn_look.grid(row=0, column=0, padx=4, pady=4)
        self.btn_inv.grid(row=0, column=1, padx=4, pady=4)

        # --- Move / 移動控制 ---
        # [CN] 使用 LabelFrame 將移動按鈕群組化，並以網格 (Grid) 排列成十字方位。
        # [EN] Group movement buttons using LabelFrame and arrange them in a cross-directional grid.
        move_box = ttk.LabelFrame(self.ctrl_container_left, text="Move", padding=8)
        move_box.pack(fill="x", pady=(0, 8))

        # [CN] 方向按鈕：按下後發送對應的方位字串給伺服器處理。
        # [EN] Directional buttons: Sends corresponding direction strings to the server for processing.
        self.btn_n = ttk.Button(move_box, text="↑ North", command=lambda: self.send_cmd({"cmd": "MOVE", "direction": "North"}))
        self.btn_w = ttk.Button(move_box, text="← West",  command=lambda: self.send_cmd({"cmd": "MOVE", "direction": "West"}))
        self.btn_s = ttk.Button(move_box, text="↓ South", command=lambda: self.send_cmd({"cmd": "MOVE", "direction": "South"}))
        self.btn_e = ttk.Button(move_box, text="→ East",  command=lambda: self.send_cmd({"cmd": "MOVE", "direction": "East"}))

        # [CN] 十字方位排版邏輯 / [EN] D-pad style layout logic
        self.btn_n.grid(row=0, column=1, padx=4, pady=4) # [CN] 正上方 / [EN] Top center
        self.btn_w.grid(row=1, column=0, padx=4, pady=4) # [CN] 左方 / [EN] Left
        self.btn_s.grid(row=1, column=1, padx=4, pady=4) # [CN] 正下方 / [EN] Bottom center
        self.btn_e.grid(row=1, column=2, padx=4, pady=4) # [CN] 右方 / [EN] Right

        # --- Items / 物品操作 ---
        # [CN] 處理與地圖物品互動的區塊。
        # [EN] Section for interacting with items on the map.
        item_box = ttk.LabelFrame(self.ctrl_container_right, text="Items", padding=8)
        item_box.pack(fill="x", pady=(0, 8))

        self.item_var = tk.StringVar() # [CN] 背包/物品輸入變數 / [EN] Item input variable

        # [CN] Take/Deposit 按鈕：呼叫內部函數，從 Listbox 選取項中獲取物品名稱並發送。
        # [EN] Take/Deposit buttons: Call internal functions to get item names from Listbox selection and send.
        self.btn_take = ttk.Button(item_box, text="Take", command=self.on_take)
        self.btn_dep = ttk.Button(item_box, text="Deposit", command=self.on_deposit)
        self.btn_take.grid(row=0, column=2, padx=4, pady=4)
        self.btn_dep.grid(row=0, column=3, padx=4, pady=4)

        # --- Give / Tell (Social Features) / 社交功能 ---
        # [CN] 實作點對點通訊 (Tell) 與物品轉移 (Give)。
        # [EN] Implement Peer-to-Peer communication (Tell) and item transfer (Give).
        social_box = ttk.LabelFrame(self.ctrl_container_right, text="Give / Tell", padding=8)
        social_box.pack(fill="x", pady=(0, 8))

        # [CN] 目的地玩家輸入框 / [EN] Target player input field
        self.to_var = tk.StringVar()
        ttk.Label(social_box, text="To:").grid(row=0, column=0, sticky="e", padx=4, pady=4)
        ttk.Entry(social_box, textvariable=self.to_var, width=12).grid(row=0, column=1, padx=4, pady=4)

        self.btn_give = ttk.Button(social_box, text="Give (Item)", command=self.on_give)
        self.btn_give.grid(row=0, column=2, padx=4, pady=4)

        # [CN] 私聊訊息內容輸入框 / [EN] Private message text input field
        self.tell_var = tk.StringVar()
        ttk.Label(social_box, text="Msg:").grid(row=1, column=0, sticky="e", padx=4, pady=4)
        ttk.Entry(social_box, textvariable=self.tell_var, width=28).grid(row=1, column=1, columnspan=2, sticky="we", padx=4, pady=4)
        self.btn_tell = ttk.Button(social_box, text="Tell (Whisper)", command=self.on_tell)
        self.btn_tell.grid(row=1, column=3, padx=4, pady=4)

        # --- Save / Exit / 系統操作 ---
        # [CN] 管理伺服器端持久化與連線斷開。
        # [EN] Manage server-side persistence and connection termination.
        sys_box = ttk.LabelFrame(self.ctrl_container_right, text="Save / Exit", padding=8)
        sys_box.pack(fill="x")

        # [CN] SAVE: 僅存檔。SAVE_EXIT: 存檔並觸發伺服器斷連。EXIT: 直接斷連。
        # [EN] SAVE: Persistence only. SAVE_EXIT: Save and trigger disconnect. EXIT: Direct disconnect.
        self.btn_save = ttk.Button(sys_box, text="Save", command=lambda: self.send_cmd({"cmd": "SAVE"}))
        self.btn_saveexit = ttk.Button(sys_box, text="SaveExit", command=self.on_save_exit)
        self.btn_exit = ttk.Button(sys_box, text="Exit", command=self.on_exit)

        self.btn_save.grid(row=0, column=0, padx=4, pady=4)
        self.btn_saveexit.grid(row=0, column=1, padx=4, pady=4)
        self.btn_exit.grid(row=0, column=2, padx=4, pady=4)

        # [CN] 狀態初始化：登入成功前，先禁用所有操作按鈕以防邏輯錯誤。
        # [EN] Initial state: Disable all action buttons until login is successful to prevent logical errors.
        self.set_controls_enabled(False)

        # --- Status Bar / 底部狀態列 ---
        # [CN] 提供即時的單行系統提示（如：已連接、已中斷）。
        # [EN] Provides real-time single-line system prompts (e.g., Connected, Disconnected).
        self.status_var = tk.StringVar(value="")
        ttk.Label(f, textvariable=self.status_var, relief="sunken", anchor="w").pack(fill="x", pady=(8, 0))

    # ============================================================
    # [SECTION 4] UI Helpers / 介面輔助工具
    # ============================================================

    def log_append(self, text: str):
        """
        [CN] 事件日誌更新：切換文字框狀態以插入新行，並將捲軸自動滾動至底部。
        [EN] Event Log Update: Toggles textbox state to insert new line and auto-scrolls to the bottom.
        """
        self.log.configure(state="normal") # [CN] 解鎖以寫入 / [EN] Unlock for writing
        self.log.insert("end", text + "\n")
        self.log.see("end")                 # [CN] 滾動視窗 / [EN] Auto-scroll
        self.log.configure(state="disabled") # [CN] 重新鎖定為唯讀 / [EN] Relock to read-only

    def set_status(self, text: str):
        """ [CN] 更新介面最下方的狀態列文字。 [EN] Update status bar text at the bottom. """
        self.status_var.set(text)

    def set_listbox(self, lb: tk.Listbox, items):
        """
        [CN] 清單框數據填充：清空舊數據並填入新清單，若為空則顯示 (empty)。
        [EN] Listbox Data Population: Clear old data and refill, show (empty) if no items exist.
        """
        lb.delete(0, "end") # [CN] 清空 Listbox / [EN] Clear Listbox
        if not items:
            lb.insert("end", "(empty)")
            return
        for it in items:
            lb.insert("end", it)

# ----------------- Connect flows / 連線與操作流程 -----------------

    def on_discover_connect(self):
        """
        [CN] 自動探索連線：啟動背景執行緒搜尋伺服器，避免組播監聽過程導致 GUI 凍結。
        [EN] Auto-discovery connect: Starts a background thread to search for the server, 
             preventing the GUI from freezing during the multicast listening process.
        """
        self.discover_status.set("Discovering server via multicast...")
        self.root.update_idletasks() # [CN] 強制更新 UI 顯示 / [EN] Force update UI display

        def work():
            # [CN] 執行組播探索 (這是一個阻塞動作)
            # [EN] Perform multicast discovery (this is a blocking action)
            found = discover_server()
            if not found:
                # [CN] 使用 root.after 將 UI 更新派發回主執行緒 (執行緒安全)
                # [EN] Use root.after to dispatch UI updates back to the main thread (thread-safe)
                self.root.after(0, lambda: self.discover_status.set("Discover failed. (Check same LAN / multicast allowed)"))
                return
            
            host, port, tls = found
            try:
                # [CN] 嘗試建立雙疊疊 TCP 連線
                # [EN] Attempt to establish a Dual-stack TCP connection
                conn = connect_dualstack(host, port, tls)
            except Exception as e:
                self.root.after(0, lambda: self.discover_status.set(f"Connect failed: {e}"))
                return

            # [CN] 連線成功，切換至遊戲畫面
            # [EN] Connection successful, switch to game screen
            self.root.after(0, lambda: self._finish_connect(conn, host, port, tls, discovered=True))

        # [CN] 啟動背景執行緒執行上述邏輯
        # [EN] Start a background thread to execute the logic above
        threading.Thread(target=work, daemon=True).start()

    def on_manual_connect(self):
        """
        [CN] 手動連線：讀取使用者輸入的 IP/Port 並在背景嘗試建立連線。
        [EN] Manual connect: Reads user-inputted IP/Port and attempts connection in the background.
        """
        host = self.host_var.get().strip()
        if not host:
            messagebox.showerror("Error", "Host/IP required")
            return
        try:
            port = int(self.port_var.get().strip())
        except ValueError:
            messagebox.showerror("Error", "Port must be a number")
            return
        tls = bool(self.tls_var.get())

        self.manual_status.set("Connecting...")
        self.root.update_idletasks()

        def work():
            try:
                conn = connect_dualstack(host, port, tls)
            except Exception as e:
                self.root.after(0, lambda: self.manual_status.set(f"Connect failed: {e}"))
                return
            self.root.after(0, lambda: self._finish_connect(conn, host, port, tls, discovered=False))

        threading.Thread(target=work, daemon=True).start()

    def _finish_connect(self, conn: socket.socket, host: str, port: int, tls: bool, discovered: bool):
        """
        [CN] 連線收尾處理：初始化 Socket、啟動接收執行緒並切換 UI。
        [EN] Post-connection handling: Initialize socket, start receiver thread, and switch UI.
        """
        self.conn = conn
        self.connected = True
        
        # [CN] 更新連線資訊標籤
        # [EN] Update connection info label
        info_str = f"Connected to {host}:{port}  TLS={'ON' if tls else 'OFF'}  ({'Discover' if discovered else 'Manual'})"
        self.conn_info.set(info_str)
        self.set_status("Connected. Please Register/Login first (mandatory for actions).")
        self.log_append(info_str)

        # [CN] 啟動異步接收迴圈，持續監聽伺服器回傳的 JSON 封包
        # [EN] Start the async receiver loop to continuously monitor incoming JSON packets from the server
        self.rx_thread = threading.Thread(target=self.rx_loop, daemon=True)
        self.rx_thread.start()

        # [CN] 跳轉至遊戲主畫面
        # [EN] Jump to the main game screen
        self.show_screen(self.screen_game)

    def set_controls_enabled(self, enabled: bool):
        """
        [CN] 控制項權限管理：批次啟用或停用按鈕，確保玩家在登入前無法進行非法遊戲操作。
        [EN] Control permission management: Batch enable or disable buttons to ensure players 
             cannot perform illegal game actions before logging in.
        """
        state = "normal" if enabled else "disabled"
        # [CN] 遍歷所有需要受控的按鈕物件
        # [EN] Iterate through all controlled button objects
        for b in [self.btn_look, self.btn_inv, self.btn_n, self.btn_w, self.btn_s, self.btn_e,
                  self.btn_take, self.btn_dep, self.btn_give, self.btn_tell,
                  self.btn_save, self.btn_saveexit, self.btn_exit]:
            try:
                b.configure(state=state)
            except Exception:
                pass

    def disconnect(self):
        """
        [CN] 斷線與資源清理：主動發送退出指令給伺服器，並關閉本機 Socket。
        [EN] Disconnect and resource cleanup: Actively send exit command to server and close local socket.
        """
        if not self.connected:
            self.show_screen(self.screen_connect)
            return
        try:
            try:
                # [CN] 告知伺服器本端即將離線 / [EN] Notify the server of imminent disconnection
                send_json(self.conn, {"cmd": "EXIT"})
            except Exception:
                pass
            try:
                # [CN] 關閉傳輸層連線 / [EN] Close the transport layer connection
                self.conn.close()
            except Exception:
                pass
        finally:
            # [CN] 重置狀態變數與 UI
            # [EN] Reset state variables and UI
            self.conn = None
            self.connected = False
            self.conn_info.set("Not connected")
            self.set_status("Disconnected.")
            self.log_append("[Client] Disconnected")
            self.show_screen(self.screen_connect)

    # ----------------- Send / Receive / 收發控制 -----------------

    def send_cmd(self, obj: dict):
        """
        [CN] 指令發送封裝：檢查連線狀態，並將資料透過 Socket 送出。
        [EN] Command transmission encapsulation: Checks connection state and sends data via socket.
        """
        if not self.connected or not self.conn:
            messagebox.showwarning("Not connected", "Please connect first.")
            return
        try:
            # [CN] 呼叫底層 send_json 進行序列化與傳輸
            # [EN] Call the low-level send_json for serialization and transmission
            send_json(self.conn, obj)
        except Exception as e:
            self.log_append(f"[Error] Send failed: {e}")
            self.disconnect() # [CN] 傳送失敗通常代表連線已中斷，執行清理 / [EN] Send failure usually means lost connection, execute cleanup

# ----------------- Message Receiving Loop / 訊息接收迴圈 -----------------

    def rx_loop(self):
        """
        [CN] 背景接收執行緒的核心迴圈：負責在不阻塞 UI 的情況下持續讀取 TCP 串流。
        [EN] Core loop of the background receiving thread: responsible for continuously reading 
             the TCP stream without blocking the UI.
        """
        try:
            # [CN] 使用 recv_lines 產生器逐行獲取資料。這確保了應用層能收到完整的 JSON 字串（定界符成幀）。
            # [EN] Use the recv_lines generator to fetch data line by line. This ensures the 
            #      application layer receives complete JSON strings (delimiter-based framing).
            for line in recv_lines(self.conn):
                try:
                    # [CN] 嘗試將接收到的位元組流解碼並轉化為 Python 字典。
                    # [EN] Attempt to decode the received byte stream and convert it into a Python dictionary.
                    msg = json.loads(line)
                except Exception:
                    # [CN] 容錯處理：若收到損毀或格式錯誤的 JSON，將原始內容顯示在日誌中以便除錯。
                    # [EN] Fault tolerance: If a corrupted or malformed JSON is received, display 
                    #      the raw content in the log for debugging purposes.
                    self.root.after(0, lambda l=line: self.log_append(f"[RAW] {l}"))
                    continue
                
                # [CN] 重要：Tkinter 元件並非執行緒安全 (Thread-safe)。
                #      必須透過 .after(0, ...) 將解析後的訊息排程回主執行緒執行，避免導致 GUI 當機。
                # [EN] IMPORTANT: Tkinter components are not thread-safe.
                #      Parsed messages must be scheduled back to the main thread via .after(0, ...)
                #      to prevent the GUI from crashing.
                self.root.after(0, lambda m=msg: self.handle_msg(m))

        except Exception as e:
            # [CN] 異常處理：偵測連線異常中斷（例如伺服器端強迫關閉連線）。
            # [EN] Exception handling: Detects abnormal connection drops (e.g., server-side closure).
            self.root.after(0, lambda: self.log_append(f"[Error] Connection lost: {e}"))
        finally:
            # [CN] 無論接收迴圈如何結束，皆需執行斷線清理並引導使用者回到連線畫面。
            # [EN] Regardless of how the receiving loop ends, perform disconnect cleanup and 
            #      guide the user back to the connection screen.
            self.root.after(0, self.disconnect)

    def handle_msg(self, msg: dict):
        """
        [CN] 訊息路由處理器：解析應用層協定中的指令類型，並觸發相對應的 UI 更新。
        [EN] Message routing handler: parses command types in the application-layer protocol 
             and triggers corresponding UI updates.
        """
        # --- Events / 即時廣播事件 ---
        # [CN] 處理來自伺服器的非對稱通知（如：其他玩家進入房間、或私訊）。
        # [EN] Handle asymmetric notifications from the server (e.g., players entering, or whispers).
        if msg.get("type") == "EVENT":
            self.log_append(f"* {msg.get('msg')}")
            return

        # --- Look / 環境狀態回報 ---
        # [CN] 當伺服器回傳 LOOK 指令結果時，更新地圖座標標籤與房間內的實體列表。
        # [EN] When the server returns LOOK command results, update the coordinate label 
        #      and the list of entities within the room.
        if msg.get("type") == "LOOK" and msg.get("ok"):
            x, y = msg.get("location", ["?", "?"])
            self.loc_var.set(f"Location: {x} {y}")
            # [CN] 同步更新玩家名單、物品名單與顯示日誌。
            # [EN] Synchronously update player list, item list, and display logs.
            self.set_listbox(self.players_list, msg.get("players", []))
            self.set_listbox(self.room_items_list, msg.get("items", []))
            self.log_append("[Look] updated")
            return

        # --- Inventory / 背包狀態回報 ---
        # [CN] 更新玩家當前持有的物品清單。
        # [EN] Update the list of items currently held by the player.
        if msg.get("type") == "INVENTORY" and msg.get("ok"):
            self.set_listbox(self.inv_list, msg.get("items", []))
            self.log_append("[Inventory] updated")
            return

        # --- Generic OK/Error / 一般指令回應處理 ---
        if msg.get("ok"):
            # [CN] 在日誌中顯示指令執行的成功訊息。
            # [EN] Display success messages of command execution in the log.
            self.log_append(msg.get("msg", "OK"))

            m = (msg.get("msg", "") or "").lower()
            
            # [CN] 狀態遷移邏輯：登入成功後觸發一系列介面初始化。
            # [EN] State transition logic: Trigger a series of UI initializations after successful login.
            if "login ok" in m:
                # [CN] 解鎖所有原本禁用的控制按鈕。
                # [EN] Unlock all previously disabled control buttons.
                self.set_controls_enabled(True)
                self.auth_hint.set("Login success. Controls unlocked.")
                # [CN] 收起登入/註冊表單，提供更乾淨的介面。
                # [EN] Collapse the Login/Register form for a cleaner interface.
                self.auth_mode.set("")
                self._render_auth_form()

                # [CN] 主動發送第一次 LOOK 與 INVENTORY 指令以同步初始世界狀態。
                # [EN] Actively send initial LOOK and INVENTORY commands to sync initial world state.
                self.send_cmd({"cmd": "LOOK"})
                self.send_cmd({"cmd": "INVENTORY"})
                return

            # [CN] 事件連動：若位置變更或道具存取成功，主動刷新環境資訊。
            # [EN] Event coupling: If movement or item access is successful, actively refresh environmental info.
            if any(k in m for k in ("moved", "took", "deposited", "gave")):
                self.send_cmd({"cmd": "LOOK"})
                self.send_cmd({"cmd": "INVENTORY"})
            return
        else:
            # [CN] 將來自伺服器的邏輯錯誤（如：密碼錯誤、背包已滿）轉發至 UI 日誌。
            # [EN] Forward logic errors from the server (e.g., wrong password, inventory full) to the UI log.
            self.log_append("[Error] " + msg.get("msg", "Unknown error"))

    # ----------------- Dynamic UI Generation / 動態 UI 渲染邏輯 -----------------

    def _render_auth_form(self):
        """
        [CN] 動態介面建構器：根據目前的 auth_mode (登入或註冊) 重繪身分驗證面板。
        [EN] Dynamic UI builder: Redraws the authentication panel based on current auth_mode (Login or Register).
        """
        # [CN] 清理舊元件：遞迴刪除容器內的所有子元件，確保佈局不會重疊。
        # [EN] Cleanup old widgets: Recursively delete all child widgets in the container to prevent layout overlapping.
        for w in self.auth_form_container.winfo_children():
            w.destroy()

        mode = self.auth_mode.get()

        # [CN] 狀態檢查：若未選擇模式，顯示提示文字並中止渲染。
        # [EN] State check: If no mode is selected, show hint text and abort rendering.
        if mode == "":
            ttk.Label(self.auth_form_container, text="(Choose Login or Register)", foreground="#666").pack(anchor="w")
            return

        # [CN] 依據模式動態設定標題 (Login / Register)。
        # [EN] Dynamically set the title (Login / Register) based on mode.
        title = "Login" if mode == "login" else "Register"
        ttk.Label(self.auth_form_container, text=title, font=("Arial", 11, "bold")).pack(anchor="w", pady=(0, 6))

        # [CN] 建立子容器以承載輸入表單元件。
        # [EN] Create sub-container to hold input form components.
        form = ttk.Frame(self.auth_form_container)
        form.pack(fill="x")

        # [CN] 名稱輸入區域配置 / [EN] Name entry configuration
        ttk.Label(form, text="Name").grid(row=0, column=0, sticky="w", padx=2, pady=(2, 2))
        name_entry = ttk.Entry(form, textvariable=self.auth_name)
        name_entry.grid(row=1, column=0, sticky="we", padx=2, pady=(0, 8))

        # [CN] 密碼輸入區域配置：使用 show="*" 遮罩隱私。
        # [EN] Password entry configuration: Use show="*" to mask privacy.
        ttk.Label(form, text="Password").grid(row=2, column=0, sticky="w", padx=2, pady=(2, 2))
        pw_entry = ttk.Entry(form, textvariable=self.auth_pw, show="*")
        pw_entry.grid(row=3, column=0, sticky="we", padx=2, pady=(0, 8))

        # [CN] 設定權重，讓 Entry 元件能隨視窗寬度伸縮。
        # [EN] Set weight to allow Entry components to stretch with window width.
        form.columnconfigure(0, weight=1)

        # [CN] 按鈕控制區域 / [EN] Button control area
        btns = ttk.Frame(self.auth_form_container)
        btns.pack(fill="x")

        # [CN] 依據模式綁定不同的回調函數。
        # [EN] Bind different callback functions based on mode.
        if mode == "login":
            ttk.Button(btns, text="Submit Login", command=self.on_login_form).pack(side="left", expand=True, fill="x", padx=(0, 6))
        else:
            ttk.Button(btns, text="Submit Register", command=self.on_register_form).pack(side="left", expand=True, fill="x", padx=(0, 6))

        # [CN] 取消按鈕邏輯 / [EN] Cancel button logic
        ttk.Button(btns, text="Cancel", command=self._auth_cancel).pack(side="left", expand=True, fill="x", padx=(6, 0))

        # [CN] 互動優化：自動將鍵盤焦點設在名稱輸入框中。
        # [EN] Interaction optimization: Automatically set keyboard focus to the name entry box.
        name_entry.focus_set()

# ============================================================
    # [SECTION 5] Authentication Callbacks / 身分驗證回調函式
    # ============================================================

    def _auth_cancel(self):
        """
        [CN] 取消驗證：重置表單模式為空，重新渲染介面並顯示預設提示。
        [EN] Cancel Auth: Resets the form mode to empty, re-renders the UI, and shows the default hint.
        """
        self.auth_mode.set("")          # [CN] 清空模式變數 / [EN] Clear mode variable
        self._render_auth_form()         # [CN] 觸發 UI 重繪以移除輸入框 / [EN] Trigger UI redraw to remove entry fields
        self.auth_hint.set("Please Login or Register first.") # [CN] 恢復提示文字 / [EN] Restore hint text

    def on_login_form(self):
        """
        [CN] 登入表單提交：讀取 GUI 變數、執行基礎驗證，並發送登入指令。
        [EN] Login Form Submit: Reads GUI variables, performs basic validation, and sends the LOGIN command.
        """
        u = self.auth_name.get().strip() # [CN] 讀取並去除了前後空格的名稱 / [EN] Get stripped name from Entry
        pw = self.auth_pw.get().strip()   # [CN] 讀取並去除了前後空格的密碼 / [EN] Get stripped password from Entry
        
        # [CN] 前端防呆：確保欄位非空以免發送無效請求。
        # [EN] Frontend Guard: Ensure fields are not empty to avoid sending invalid requests.
        if not u or not pw:
            messagebox.showinfo("Login", "Need Name and Password.")
            return
            
        self.auth_hint.set("Logging in...") # [CN] 即時回饋狀態 / [EN] Real-time status feedback
        # [CN] 封裝為符合協議的 JSON 字典並發送。
        # [EN] Encapsulate into a protocol-compliant JSON dict and send.
        self.send_cmd({"cmd": "LOGIN", "name": u, "password": pw})

    def on_register_form(self):
        """
        [CN] 註冊表單提交：執行註冊邏輯，將新帳號資訊傳送至伺服器持久化儲存。
        [EN] Register Form Submit: Executes registration logic, sending new account info to the server for persistence.
        """
        u = self.auth_name.get().strip()
        pw = self.auth_pw.get().strip()
        
        if not u or not pw:
            messagebox.showinfo("Register", "Need Name and Password.")
            return
            
        self.auth_hint.set("Registering...")
        # [CN] 伺服器收到後會進行鹽值生成與雜湊處理。
        # [EN] The server will perform salt generation and hashing upon receipt.
        self.send_cmd({"cmd": "REGISTER", "name": u, "password": pw})

    # ----------------- Game actions (buttons/CLI) / 遊戲操作邏輯 -----------------

    def on_send_command(self):
        """
        [CN] 指令列發送回調：擷取輸入框文字、清空介面並呼叫解析器。
        [EN] Command Line Send Callback: Captures entry text, clears the UI field, and calls the parser.
        """
        s = self.cmd_var.get().strip() # [CN] 取得指令列字串 / [EN] Get string from command entry
        if not s:
            return
        self.cmd_var.set("") # [CN] 立即清空輸入框，提升使用者操作體驗 / [EN] Immediately clear entry to enhance user experience
        # [CN] 將原始字串交給解析器處理 / [EN] Pass raw string to the parser
        self.parse_and_send_text_command(s)

    def parse_and_send_text_command(self, s: str):
        """
        [CN] 文字指令解析器 (CLI Parser)：將非結構化文字轉換為結構化 JSON 封包。
        這是模擬早期 MUD (Multi-User Dungeon) 遊戲的輸入方式。
        [EN] Text Command Parser: Converts unstructured text into structured JSON packets.
        This simulates the input style of classic MUD (Multi-User Dungeon) games.
        """
        parts = s.split()     # [CN] 依照空格切割字串 / [EN] Split string by spaces
        cmd = parts[0].lower() # [CN] 取得第一個單字作為核心指令 (轉小寫) / [EN] Take first word as core command (lowercase)

        # --- Help / 說明指令 ---
        if cmd == "help":
            self.log_append("Commands: Register/Login/Look/Inventory/Move/Take/Deposit/Give/Tell/Save/SaveExit/Exit")
            return

        # --- Account Commands (Manual) / 帳號指令手動輸入模式 ---
        # [CN] 支援在指令列直接打 "register <name> <pw>"。
        # [EN] Supports "register <name> <pw>" directly in the command line.
        if cmd == "register" and len(parts) >= 3:
            self.send_cmd({"cmd": "REGISTER", "name": parts[1], "password": parts[2]})
            return

        if cmd == "login" and len(parts) >= 3:
            self.send_cmd({"cmd": "LOGIN", "name": parts[1], "password": parts[2]})
            return

        # --- Basic Gameplay / 基礎遊戲指令 ---
        if cmd == "look":
            self.send_cmd({"cmd": "LOOK"})
            return

        if cmd == "inventory":
            self.send_cmd({"cmd": "INVENTORY"})
            return

        # --- Movement / 位移指令 ---
        if cmd == "move":
            direction = parts[1] if len(parts) >= 2 else ""
            self.send_cmd({"cmd": "MOVE", "direction": direction})
            return

        # --- Item Interaction / 物品互動 ---
        # [CN] 注意：使用切片取得完整物品名稱，以支援名稱中帶有空格的道具。
        # [EN] Note: Use slicing to get the full item name to support items with spaces in their names.
        if cmd == "take":
            item = s[len(parts[0]):].strip() # [CN] 排除 "take" 後的剩餘部分 / [EN] Strip everything after "take"
            self.send_cmd({"cmd": "TAKE", "item": item})
            return

        if cmd in ("deposit", "drop"):
            item = s[len(parts[0]):].strip()
            self.send_cmd({"cmd": "DEPOSIT", "item": item})
            return

        # --- Social & Peer-to-Peer / 社交與點對點通訊 ---
        # [CN] 格式：give <player> <item>
        # [EN] Format: give <player> <item>
        if cmd == "give" and len(parts) >= 3:
            to = parts[1]
            item = s.split(None, 2)[2] # [CN] 切分出第三個參數開始的所有文字 / [EN] Split to get all text starting from 3rd param
            self.send_cmd({"cmd": "GIVE", "to": to, "item": item})
            return

        # [CN] 格式：tell <player> <message>
        # [EN] Format: tell <player> <message>
        if cmd == "tell" and len(parts) >= 3:
            to = parts[1]
            msg = s.split(None, 2)[2]
            self.send_cmd({"cmd": "TELL", "to": to, "msg": msg})
            return

        # --- System Commands / 系統指令 ---
        if cmd == "save":
            self.send_cmd({"cmd": "SAVE"})
            return

        if cmd in ("saveexit", "save&exit"):
            self.on_save_exit()
            return

        if cmd in ("exit", "quit"):
            self.on_exit()
            return

        # [CN] 指令不匹配時的回饋 / [EN] Feedback for unrecognized commands
        self.log_append("Unknown command. Type Help.")

    # ============================================================
    # [SECTION 6] Listbox Interaction Logic / 清單選取與互動邏輯
    # ============================================================

    def on_take(self):
        """
        [CN] 拾取物件：從「房間物品列表」獲取選取項的索引與名稱，並發送 TAKE 指令。
        [EN] Take Item: Retrieves the index and name of the selected item from the "Room Items List" and dispatches the TAKE command.
        """
        if not self.connected:
            return
        # [CN] curselection() 回傳一個包含選取索引的元組 / [EN] curselection() returns a tuple of selected indices
        sel = self.room_items_list.curselection()
        if not sel:
            messagebox.showinfo("Take", "Please select an item in the room list.")
            return
        
        # [CN] 根據索引取得清單中的字串內容 / [EN] Get the string content from the listbox based on the index
        item = self.room_items_list.get(sel[0])
        if item == "(empty)":
            messagebox.showinfo("Take", "No item to take.")
            return
            
        # [CN] 封裝為 JSON 並透過 TCP 發送 / [EN] Encapsulate into JSON and send via TCP
        self.send_cmd({"cmd": "TAKE", "item": item})

    def on_deposit(self):
        """
        [CN] 丟棄物件：從「個人背包列表」選取物件並發送 DEPOSIT 指令，將物件從 Player 實體轉移回 Room 實體。
        [EN] Deposit Item: Selects an item from the "Inventory List" and sends a DEPOSIT command, 
             transferring the item from the Player entity back to the Room entity.
        """
        if not self.connected:
            return
        sel = self.inv_list.curselection()
        if not sel:
            messagebox.showinfo("Deposit", "Please select an item in your inventory list.")
            return
        
        item = self.inv_list.get(sel[0])
        if item == "(empty)":
            messagebox.showinfo("Deposit", "No item to deposit.")
            return
            
        self.send_cmd({"cmd": "DEPOSIT", "item": item})

    def on_give(self):
        """
        [CN] 物品餽贈：實作複雜的雙重選取邏輯（目標玩家 + 背包物件）。
        [EN] Give Item: Implements complex dual-selection logic (Target Player + Inventory Item).
        """
        if not self.connected:
            return
            
        # --- Step 1: 確定目標玩家 (Determine Target Player) ---
        psel = self.players_list.curselection()
        target = ""
        if psel:
            # [CN] 讀取列表選取的玩家名稱 / [EN] Read player name from list selection
            target = self.players_list.get(psel[0]).replace("(Me)", "").strip()
        else:
            # [CN] 若列表沒選，則讀取手動輸入框 / [EN] If no list selection, read from manual input field
            target = self.to_var.get().strip()

        if not target:
            messagebox.showinfo("Give", "Please select a target player (or type in To:).")
            return
            
        # [CN] 字串清理：移除 UI 標籤 "(Me)" 確保發送的是原始使用者名稱
        # [EN] String Sanitization: Strip UI tag "(Me)" to ensure raw username is sent.
        target = target.replace("(Me)", "").strip()
        if not target:
            return

        # --- Step 2: 確定要給予的物件 (Determine Item to Give) ---
        isel = self.inv_list.curselection()
        if not isel:
            messagebox.showinfo("Give", "Please select an item from your inventory to give.")
            return
        
        item = self.inv_list.get(isel[0])
        if item == "(empty)":
            messagebox.showinfo("Give", "No item to give.")
            return

        # [CN] 發送給予指令，伺服器會處理兩位線上玩家的背包原子交換
        # [EN] Send GIVE command; the server handles the atomic inventory swap between two online players.
        self.send_cmd({"cmd": "GIVE", "to": target, "item": item})

    def on_tell(self):
        """
        [CN] 私訊功能：實現應用層的點對點 (Unicast) 通訊模擬。
        [EN] Private Message: Implements application-layer Unicast communication simulation.
        """
        if not self.connected:
            return
        psel = self.players_list.curselection()
        if not psel:
            messagebox.showinfo("Tell", "Please select a target player in the room player list.")
            return
            
        # [CN] 獲取目標名稱並過濾 / [EN] Retrieve and filter target name
        target = self.players_list.get(psel[0])
        target = target.replace("(Me)", "").strip()
        if not target:
            return

        # [CN] 獲取私訊內容 / [EN] Retrieve message content
        text = self.tell_var.get().strip()
        if not text:
            messagebox.showinfo("Tell", "Please enter a message.")
            return

        # [CN] 發送 TELL 指令，由伺服器根據 username 路由至對應的 Socket
        # [EN] Send TELL command; the server routes it to the specific socket based on username.
        self.send_cmd({"cmd": "TELL", "to": target, "msg": text})
        self.tell_var.set("") # [CN] 發送後重置輸入框 / [EN] Reset input field after sending

    # ============================================================
    # [SECTION 7] Fallback Authentication / 備用身分驗證函式
    # ============================================================

    def on_register(self):
        """
        [CN] 註冊功能 (備用)：讀取 user_var 變數並發送註冊請求。
        [EN] Register (Fallback): Reads from user_var and dispatches registration request.
        """
        u = self.user_var.get().strip()
        pw = self.pw_var.get().strip()
        if not u or not pw:
            messagebox.showinfo("Register", "Need username and password.")
            return
        self.send_cmd({"cmd": "REGISTER", "name": u, "password": pw})

    def on_login(self):
        """
        [CN] 登入功能 (備用)：讀取 user_var 變數並發送登入請求。
        [EN] Login (Fallback): Reads from user_var and dispatches login request.
        """
        u = self.user_var.get().strip()
        pw = self.pw_var.get().strip()
        if not u or not pw:
            messagebox.showinfo("Login", "Need username and password.")
            return
        self.send_cmd({"cmd": "LOGIN", "name": u, "password": pw})

    # ============================================================
    # [SECTION 8] System Termination / 系統終止流程
    # ============================================================

    def on_save_exit(self):
        """
        [CN] 存檔並退出：發送 SAVE_EXIT。此動作會觸發伺服器端的 Socket 關閉 (FIN)。
        [EN] Save & Exit: Sends SAVE_EXIT. This triggers a socket closure (FIN) on the server side.
        """
        # [CN] 伺服器處理完存檔後會主動中斷連線
        # [EN] The server will actively terminate the connection after processing the save.
        self.send_cmd({"cmd": "SAVE_EXIT"})

    def on_exit(self):
        """
        [CN] 強制退出：發送 EXIT 指令。
        [EN] Forced Exit: Sends the EXIT command.
        """
        self.send_cmd({"cmd": "EXIT"})
        # [CN] rx_loop 偵測到 Socket 關閉後會自動呼叫 disconnect 更新 UI
        # [EN] rx_loop will detect the socket closure and automatically call disconnect to update the UI.


def main():
    root = tk.Tk()
    try:
        style = ttk.Style()
        style.theme_use("clam")
    except Exception:
        pass
    GameGUI(root)
    root.mainloop()


if __name__ == "__main__":
    main()
