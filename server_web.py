"""
server_web.py - Modern Web & WebSocket Backend Server for Computer Lab Monitoring
Features:
- High-performance FastAPI server with asynchronous WebSockets
- In-memory agent state with SQLite historical telemetry & audit logs
- Dual WebSocket channels: /ws/agent (student PCs) and /ws/admin (web dashboard)
- Command dispatch: lock, unlock, broadcast popup, shutdown, restart, launch app, kill process
- Automatic client offline detection and real-time dashboard state synchronization
"""

import asyncio
import base64
import datetime
import hashlib
import json
import os
import platform
import secrets
import socket
import sqlite3
import subprocess
import sys
import threading
import time
import urllib.parse
from contextlib import asynccontextmanager
from typing import Any, Dict, List, Optional, Set

# Ensure stdout and stderr are valid streams (e.g. when run via pythonw.exe or headless)
if sys.stdout is None:
    try:
        sys.stdout = open(os.path.join(os.path.dirname(os.path.abspath(__file__)), "server_web.log"), "a", encoding="utf-8", buffering=1)
    except Exception:
        sys.stdout = open(os.devnull, "w")
if sys.stderr is None:
    try:
        sys.stderr = sys.stdout if sys.stdout is not None else open(os.devnull, "w")
    except Exception:
        sys.stderr = open(os.devnull, "w")


from network_engine import NetworkEngine

if platform.system() == "Windows":
    try:
        asyncio.set_event_loop_policy(asyncio.WindowsSelectorEventLoopPolicy())
    except Exception:
        pass

import uvicorn
from fastapi import FastAPI, File, Form, HTTPException, Request, Response, UploadFile, WebSocket, WebSocketDisconnect
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import FileResponse, HTMLResponse, JSONResponse, RedirectResponse
from fastapi.staticfiles import StaticFiles

def get_lan_ip() -> str:
    """Detects active LAN IPv4 address of server machine."""
    try:
        s = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        s.connect(("8.8.8.8", 80))
        ip = s.getsockname()[0]
        s.close()
        return ip
    except Exception:
        return "127.0.0.1"


def send_wake_on_lan(mac_address: str, target_ip: str = "") -> tuple[bool, str]:
    """
    Sends a Wake-on-LAN (WOL) magic packet to power on a remote PC over the local network.
    Format: 6 bytes 0xFF followed by 16 repetitions of target MAC bytes.
    Broadcasts on UDP ports 9 and 7 to both 255.255.255.255 and the local subnet broadcast.
    """
    import re
    try:
        clean_mac = re.sub(r'[^a-fA-F0-9]', '', mac_address)
        if len(clean_mac) != 12:
            return False, f"Format MAC Address '{mac_address}' tidak valid"

        mac_bytes = bytes.fromhex(clean_mac)
        magic_packet = b'\xff' * 6 + mac_bytes * 16

        destinations = [("255.255.255.255", 9), ("255.255.255.255", 7)]
        if target_ip and "." in target_ip:
            parts = target_ip.split(".")
            if len(parts) == 4 and parts[0] != "127":
                subnet_bcast = f"{parts[0]}.{parts[1]}.{parts[2]}.255"
                destinations.extend([(subnet_bcast, 9), (subnet_bcast, 7), (target_ip, 9)])

        with socket.socket(socket.AF_INET, socket.SOCK_DGRAM) as sock:
            sock.setsockopt(socket.SOL_SOCKET, socket.SO_BROADCAST, 1)
            for dest_ip, port in destinations:
                try:
                    sock.sendto(magic_packet, (dest_ip, port))
                except Exception:
                    pass

        return True, f"Magic packet berhasil dikirim ke MAC {mac_address}"
    except Exception as e:
        return False, f"Gagal mengirim WOL: {str(e)}"


# --- UDP Auto-Discovery Beacon ---
def udp_discovery_beacon():
    """Periodically broadcasts server LAN IP on UDP port 9876 so clients auto-connect even if DHCP changes."""
    sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    sock.setsockopt(socket.SOL_SOCKET, socket.SO_BROADCAST, 1)
    while True:
        try:
            lan_ip = get_lan_ip()
            msg = json.dumps({
                "app": "LabGuardPro",
                "type": "BEACON",
                "ip": lan_ip,
                "port": PORT,
                "ws_url": f"ws://{lan_ip}:{PORT}/ws/agent"
            }).encode("utf-8")
            sock.sendto(msg, ("255.255.255.255", 9876))
        except Exception:
            pass
        time.sleep(8)


# --- Configuration ---
PORT = 8000
HOST = "0.0.0.0"
DB_PATH = os.path.join(os.path.dirname(__file__), "monitoring.db")

def get_db_connection(timeout: float = 20.0) -> sqlite3.Connection:
    """Thread-safe SQLite connection factory with 20s busy timeout preventing lock errors."""
    return sqlite3.connect(DB_PATH, timeout=timeout)
WEB_DIR = os.path.join(os.path.dirname(__file__), "web")
OFFLINE_TIMEOUT_SECONDS = 12.0
DISTRIBUTED_FILES_DIR = os.path.join(os.path.dirname(__file__), "uploads_distributed")
os.makedirs(DISTRIBUTED_FILES_DIR, exist_ok=True)

# --- Agent Release & Version Metadata ---
AGENT_VERSION = "2.3.0"
AGENT_RELEASE_DATE = "06 November 2026"
AGENT_CHANGELOG = [
    "Fitur Senyap (Mute) Audio Massal & Individual untuk ketertiban ruang lab",
    "Deteksi & Peringatan Otomatis Game / Aplikasi Terlarang (Blacklist) real-time",
    "Opsi Auto-Kill: Otomatis menutup paksa game saat dibuka oleh siswa",
    "Pembaruan Software (winget) & Riwayat Windows Update Hotfix KB",
    "Fitur Kirim File / Distribusi Berkas ke Siswa (Desktop/Downloads/Docs)",
    "Remote Control interaktif (Mouse & Keyboard) langsung dari browser",
    "Peta Tata Letak Denah Meja Lab Asli (Visual Seating Plan)"
]

@asynccontextmanager
async def lifespan(app: FastAPI):
    init_db()
    watchdog_task = asyncio.create_task(offline_watchdog())
    beacon_thread = threading.Thread(target=udp_discovery_beacon, daemon=True)
    beacon_thread.start()
    lan_ip = get_lan_ip()
    print("=" * 60)
    print(f"  Lab Monitoring Server Aktif (Port {PORT})")
    print(f"  - Akses IP Jaringan LAN : http://{lan_ip}:{PORT}")
    print(f"  - Akses Komputer Lokal  : http://localhost:{PORT}")
    print(f"  - Beacon Auto-Discovery: UDP 9876 Aktif")
    print("=" * 60)
    yield
    watchdog_task.cancel()

app = FastAPI(title="Lab Monitoring System API", version="2.0", lifespan=lifespan)

app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)


# --- Database Management ---
def init_db():
    conn = get_db_connection()
    cursor = conn.cursor()
    cursor.execute("PRAGMA journal_mode = WAL;")
    cursor.execute("PRAGMA synchronous = NORMAL;")
    cursor.execute("PRAGMA cache_size = -2000;")  # Limit SQLite cache to 2MB RAM
    cursor.execute("PRAGMA temp_store = MEMORY;")
    cursor.execute("""
        CREATE TABLE IF NOT EXISTS clients (
            client_id TEXT PRIMARY KEY,
            hostname TEXT,
            ip_address TEXT,
            mac_address TEXT,
            lab_room TEXT,
            os_info TEXT,
            cpu_pct REAL DEFAULT 0,
            ram_pct REAL DEFAULT 0,
            ram_used_mb REAL DEFAULT 0,
            ram_total_mb REAL DEFAULT 0,
            disk_pct REAL DEFAULT 0,
            active_app TEXT DEFAULT '',
            is_locked INTEGER DEFAULT 0,
            is_online INTEGER DEFAULT 0,
            last_seen REAL DEFAULT 0,
            first_seen REAL DEFAULT 0,
            agent_version TEXT DEFAULT 'v2.1.0'
        )
    """)
    try:
        cursor.execute("ALTER TABLE clients ADD COLUMN agent_version TEXT DEFAULT 'v2.1.0'")
    except Exception:
        pass
    try:
        cursor.execute("ALTER TABLE clients ADD COLUMN is_teacher INTEGER DEFAULT 0")
    except Exception:
        pass
    try:
        cursor.execute("ALTER TABLE clients ADD COLUMN custom_name TEXT DEFAULT ''")
    except Exception:
        pass
    try:
        cursor.execute("ALTER TABLE clients ADD COLUMN machine_guid TEXT DEFAULT ''")
    except Exception:
        pass
    cursor.execute("""
        CREATE TABLE IF NOT EXISTS audit_logs (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            timestamp TEXT,
            action TEXT,
            target TEXT,
            details TEXT,
            status TEXT
        )
    """)
    cursor.execute("""
        CREATE TABLE IF NOT EXISTS telemetry_history (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            client_id TEXT,
            timestamp TEXT,
            cpu_pct REAL,
            ram_pct REAL
        )
    """)
    cursor.execute("""
        CREATE TABLE IF NOT EXISTS seating_plans (
            room TEXT PRIMARY KEY,
            layout_json TEXT,
            updated_at TEXT
        )
    """)
    cursor.execute("""
        CREATE TABLE IF NOT EXISTS blacklist_rules (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            keyword TEXT UNIQUE,
            category TEXT DEFAULT 'game',
            description TEXT DEFAULT '',
            is_active INTEGER DEFAULT 1,
            created_at TEXT
        )
    """)
    cursor.execute("""
        CREATE TABLE IF NOT EXISTS system_settings (
            key TEXT PRIMARY KEY,
            value TEXT
        )
    """)
    cursor.execute("""
        CREATE TABLE IF NOT EXISTS sessions (
            token TEXT PRIMARY KEY,
            username TEXT,
            created_at REAL,
            expires_at REAL,
            ip_address TEXT
        )
    """)
    # Pre-populate default admin credentials if not set
    cursor.execute("SELECT value FROM system_settings WHERE key = 'admin_password'")
    if not cursor.fetchone():
        cursor.execute("INSERT OR REPLACE INTO system_settings (key, value) VALUES ('admin_username', 'admin')")
        cursor.execute("INSERT OR REPLACE INTO system_settings (key, value) VALUES ('admin_password', 'petra123')")
    # Pre-populate default blacklist rules if empty
    cursor.execute("SELECT COUNT(*) FROM blacklist_rules")
    if cursor.fetchone()[0] == 0:
        now_dt = datetime.datetime.now().strftime("%Y-%m-%d %H:%M:%S")
        defaults = [
            ("roblox", "game", "Platform Game Roblox"),
            ("minecraft", "game", "Game Sandbox Minecraft"),
            ("steam", "game", "Platform Game Steam"),
            ("epicgames", "game", "Epic Games Launcher"),
            ("valorant", "game", "Game FPS Valorant"),
            ("fortnite", "game", "Game Battle Royale Fortnite"),
            ("genshin", "game", "Game Genshin Impact"),
            ("pointblank", "game", "Game FPS Point Blank"),
            ("poki", "game", "Web Game Poki"),
            ("crazygames", "game", "Web Game CrazyGames"),
            ("y8", "game", "Web Game Y8"),
            ("kizi", "game", "Web Game Kizi"),
            ("tiktok", "social", "Video Pendek TikTok"),
            ("youtube", "media", "Streaming Video YouTube"),
            ("netflix", "media", "Streaming Film Netflix"),
            ("discord", "social", "Chat Game Discord"),
            ("spotify", "media", "Streaming Musik Spotify"),
            ("slot", "gambling", "Situs Judi Slot Online"),
            ("judi", "gambling", "Situs Judi Online"),
            ("casino", "gambling", "Situs Casino Online"),
            ("poker", "gambling", "Situs Poker Online"),
            ("togel", "gambling", "Situs Togel Online")
        ]
        cursor.executemany(
            "INSERT OR IGNORE INTO blacklist_rules (keyword, category, description, is_active, created_at) VALUES (?, ?, ?, 1, ?)",
            [(k, cat, desc, now_dt) for k, cat, desc in defaults]
        )
    conn.commit()
    conn.close()


def log_audit(action: str, target: str, details: str, status: str = "SUCCESS"):
    try:
        conn = get_db_connection()
        cursor = conn.cursor()
        now_str = datetime.datetime.now().strftime("%Y-%m-%d %H:%M:%S")
        cursor.execute(
            "INSERT INTO audit_logs (timestamp, action, target, details, status) VALUES (?, ?, ?, ?, ?)",
            (now_str, action, target, details, status)
        )
        # Prevent database ballooning: keep only the latest 300 logs
        cursor.execute("DELETE FROM audit_logs WHERE id NOT IN (SELECT id FROM audit_logs ORDER BY id DESC LIMIT 300)")
        conn.commit()
        conn.close()
    except Exception as e:
        print(f"[DB Error] log_audit: {e}")


def get_recent_logs(limit: int = 50) -> List[Dict[str, Any]]:
    try:
        conn = get_db_connection()
        conn.row_factory = sqlite3.Row
        cursor = conn.cursor()
        cursor.execute("SELECT * FROM audit_logs ORDER BY id DESC LIMIT ?", (limit,))
        rows = [dict(r) for r in cursor.fetchall()]
        conn.close()
        return rows
    except Exception:
        return []


# --- Authentication & Session Management ---
ACTIVE_SESSIONS: Dict[str, dict] = {}  # token -> {"username": str, "expires_at": float, "ip": str}

def hash_password(password: str) -> str:
    return hashlib.sha256(password.encode("utf-8")).hexdigest()

def verify_credentials(username: str, password: str) -> bool:
    try:
        conn = get_db_connection()
        c = conn.cursor()
        c.execute("SELECT value FROM system_settings WHERE key = 'admin_username'")
        r_user = c.fetchone()
        stored_user = r_user[0] if r_user else "admin"

        c.execute("SELECT value FROM system_settings WHERE key = 'admin_password'")
        r_pass = c.fetchone()
        stored_pass = r_pass[0] if r_pass else "petra123"
        conn.close()

        if username.strip().lower() != stored_user.strip().lower():
            return False
        
        # Check both plaintext and sha256
        if password == stored_pass or hash_password(password) == stored_pass:
            return True
        return False
    except Exception as e:
        print(f"[Auth Error] verify_credentials: {e}")
        return False

def create_session(username: str, ip_address: str, remember_me: bool = True) -> str:
    token = secrets.token_hex(32)
    now = time.time()
    # 30 days if remember_me, else 24 hours
    expires_at = now + (30 * 86400 if remember_me else 86400)
    
    ACTIVE_SESSIONS[token] = {
        "username": username,
        "expires_at": expires_at,
        "ip": ip_address
    }

    try:
        conn = get_db_connection()
        c = conn.cursor()
        c.execute(
            "INSERT OR REPLACE INTO sessions (token, username, created_at, expires_at, ip_address) VALUES (?, ?, ?, ?, ?)",
            (token, username, now, expires_at, ip_address)
        )
        conn.commit()
        conn.close()
    except Exception as e:
        print(f"[Auth Error] save session: {e}")

    return token

def delete_session(token: str):
    ACTIVE_SESSIONS.pop(token, None)
    try:
        conn = get_db_connection()
        c = conn.cursor()
        c.execute("DELETE FROM sessions WHERE token = ?", (token,))
        conn.commit()
        conn.close()
    except Exception:
        pass

def is_valid_token(token: Optional[str]) -> bool:
    if not token or not isinstance(token, str):
        return False
    
    token = token.strip()
    now = time.time()

    # 1. Fast Memory Cache lookup
    if token in ACTIVE_SESSIONS:
        sess = ACTIVE_SESSIONS[token]
        if sess["expires_at"] > now:
            return True
        else:
            ACTIVE_SESSIONS.pop(token, None)
            return False

    # 2. Database fallback lookup
    try:
        conn = get_db_connection()
        c = conn.cursor()
        c.execute("SELECT username, expires_at, ip_address FROM sessions WHERE token = ?", (token,))
        row = c.fetchone()
        conn.close()
        if row and row[1] > now:
            ACTIVE_SESSIONS[token] = {
                "username": row[0],
                "expires_at": row[1],
                "ip": row[2]
            }
            return True
    except Exception:
        pass

    return False

def is_authenticated(request: Request) -> bool:
    token = request.cookies.get("labguard_token")
    if not token:
        auth_hdr = request.headers.get("Authorization", "")
        if auth_hdr.startswith("Bearer "):
            token = auth_hdr[7:].strip()
    if not token:
        token = request.query_params.get("token")
    
    return is_valid_token(token)


def require_admin_auth(request: Request):
    """Enforces strict teacher / administrator authentication on management endpoints."""
    if not is_authenticated(request):
        raise HTTPException(
            status_code=401,
            detail="Akses ditolak. Sesi Anda belum terautentikasi atau telah berakhir. Silakan login terlebih dahulu."
        )


# --- Real-time Connection Manager ---
class ConnectionManager:
    def __init__(self):
        # Connected WebSockets
        self.active_agents: Dict[str, WebSocket] = {}     # client_id -> WebSocket
        self.admin_websockets: Set[WebSocket] = set()

        # Cached client state
        self.clients_cache: Dict[str, Dict[str, Any]] = {}
        self.client_thumbnails: Dict[str, str] = {}       # client_id -> base64 jpeg
        self.client_processes: Dict[str, List[Dict[str, Any]]] = {}
        self.client_installed_apps: Dict[str, List[Dict[str, Any]]] = {}
        self.client_winget_updates: Dict[str, List[Dict[str, Any]]] = {}   # client_id -> list of pending updates
        self.client_windows_updates: Dict[str, List[Dict[str, Any]]] = {}  # client_id -> Windows hotfix history

        # Simulation wake tracking
        self.woken_clients: Set[str] = set()
        self.last_sim_wake_time: float = 0.0

        # Blacklist & prohibited app tracking
        self.blacklist_keywords: Set[str] = set()
        self.auto_kill_blacklist: bool = False
        self.client_violations: Dict[str, Dict[str, Any]] = {}  # client_id -> violation info

        # Load known clients and blacklist from DB
        self._load_cached_clients()
        self._load_blacklist_rules()

    @staticmethod
    def is_teacher_client(c: dict, client_id: str = "") -> bool:
        """Determines if a workstation is a teacher / instructor computer."""
        if bool(c.get("is_teacher")):
            return True
        try:
            local_host = socket.gethostname().upper()
        except Exception:
            local_host = ""
        cid = str(client_id or c.get("client_id", "")).upper()
        hname = str(c.get("hostname", "")).upper()
        cname = str(c.get("custom_name", "")).lower()
        if cid in ("DESKTOP-2PTRR2S", "DESKTOP-G4DVHV2", local_host):
            return True
        if hname in ("DESKTOP-2PTRR2S", "DESKTOP-G4DVHV2", local_host):
            return True
        if "guru" in cname or "guru" in hname.lower() or "teacher" in cname:
            return True
        return False

    def _load_blacklist_rules(self):
        try:
            conn = get_db_connection()
            conn.row_factory = sqlite3.Row
            cursor = conn.cursor()
            cursor.execute("SELECT keyword FROM blacklist_rules WHERE is_active = 1")
            self.blacklist_keywords = {str(r["keyword"]).strip().lower() for r in cursor.fetchall() if r["keyword"]}
            cursor.execute("SELECT value FROM system_settings WHERE key = 'auto_kill_blacklist'")
            row = cursor.fetchone()
            if row:
                self.auto_kill_blacklist = (row["value"] == "1")
            conn.close()
        except Exception as e:
            print(f"[DB Warning] Load blacklist: {e}")

    def _load_cached_clients(self):
        try:
            conn = get_db_connection()
            conn.row_factory = sqlite3.Row
            cursor = conn.cursor()
            cursor.execute("SELECT * FROM clients")
            for r in cursor.fetchall():
                c = dict(r)
                c["is_online"] = False
                c.setdefault("agent_version", f"v{AGENT_VERSION}")
                c.setdefault("custom_name", "")
                # Prefer custom name, else disambiguated client_id if cloned hostname, else hostname
                if c.get("custom_name"):
                    c["display_name"] = c["custom_name"]
                elif c.get("client_id") and c.get("hostname") and c["client_id"] != c["hostname"]:
                    c["display_name"] = c["client_id"]
                else:
                    c["display_name"] = c.get("hostname") or c.get("client_id")
                c["is_teacher"] = self.is_teacher_client(c, c.get("client_id", ""))
                self.clients_cache[c["client_id"]] = c

            # Automatic startup deduplication: prune ghost duplicate records with identical IP
            ip_map = {}
            for cid, c in list(self.clients_cache.items()):
                ip = c.get("ip_address", "")
                if not ip or ip in ("127.0.0.1", "::1", "localhost"):
                    continue
                if ip not in ip_map:
                    ip_map[ip] = cid
                else:
                    existing_cid = ip_map[ip]
                    existing_c = self.clients_cache[existing_cid]
                    prefer_current = bool(c.get("custom_name") and not existing_c.get("custom_name")) or ('_' not in cid and '_' in existing_cid)
                    canonical_cid = cid if prefer_current else existing_cid
                    stale_cid = existing_cid if prefer_current else cid
                    self.clients_cache.pop(stale_cid, None)
                    ip_map[ip] = canonical_cid
                    try:
                        cursor.execute("DELETE FROM clients WHERE client_id = ?", (stale_cid,))
                        conn.commit()
                    except Exception:
                        pass

            conn.close()
        except Exception as e:
            print(f"[DB Warning] Load clients: {e}")

    async def connect_admin(self, websocket: WebSocket):
        await websocket.accept()
        self.admin_websockets.add(websocket)
        
        # Prepare client list with cached thumbnails included
        clients_data = []
        for c in self.clients_cache.values():
            c_copy = dict(c)
            cid = c.get("client_id")
            if cid in self.client_thumbnails:
                c_copy["thumbnail"] = self.client_thumbnails[cid]
            clients_data.append(c_copy)

        # Send initial full state immediately with instant screenshots
        init_payload = {
            "type": "init",
            "clients": clients_data,
            "thumbnails": self.client_thumbnails,
            "installed_apps": self.client_installed_apps,
            "logs": get_recent_logs(30),
            "stats": self.compute_stats()
        }
        await websocket.send_text(json.dumps(init_payload))

    def disconnect_admin(self, websocket: WebSocket):
        self.admin_websockets.discard(websocket)

    async def broadcast_to_admins(self, payload: dict):
        if not self.admin_websockets:
            return
        msg = json.dumps(payload)
        dead = set()
        for ws in list(self.admin_websockets):
            try:
                await ws.send_text(msg)
            except Exception:
                dead.add(ws)
        if dead:
            self.admin_websockets.difference_update(dead)

    def touch_client(self, client_id: str):
        now = time.time()
        if client_id in self.clients_cache:
            self.clients_cache[client_id]["last_seen"] = now
            self.clients_cache[client_id]["is_online"] = True

    async def register_agent(self, client_id: str, websocket: WebSocket, meta: dict):
        self.active_agents[client_id] = websocket
        now = time.time()
        
        # Determine accurate client IP address:
        # Prefer the actual socket connection IP (websocket.client.host) so that clients
        # with secondary adapters (VPN, VM, Tethering) are displayed with their real LAN IP.
        incoming_ip = websocket.client.host if (websocket.client and getattr(websocket.client, "host", None)) else None
        meta_ip = str(meta.get("ip_address", "")).strip()

        if incoming_ip and incoming_ip not in ("127.0.0.1", "::1", "localhost"):
            client_ip = incoming_ip
        elif meta_ip and meta_ip not in ("127.0.0.1", "::1", "localhost"):
            client_ip = meta_ip
        else:
            client_ip = incoming_ip or meta_ip or "127.0.0.1"

        client_data = self.clients_cache.get(client_id, {})
        custom_name = client_data.get("custom_name", "")
        default_display = meta.get("hostname", client_id)
        if client_id != meta.get("hostname") and not custom_name:
            suffix = client_ip.split('.')[-1] if '.' in client_ip else ""
            default_display = f"{meta.get('hostname')} ({suffix})" if suffix else client_id

        client_data.update({
            "client_id": client_id,
            "hostname": meta.get("hostname", client_id),
            "custom_name": custom_name,
            "display_name": custom_name or default_display,
            "ip_address": client_ip,
            "mac_address": meta.get("mac_address", client_data.get("mac_address", "")),
            "machine_guid": meta.get("machine_guid", client_data.get("machine_guid", "")),
            "lab_room": meta.get("lab_room", client_data.get("lab_room", "Lab Umum")),
            "os_info": meta.get("os_info", client_data.get("os_info", "Windows")),
            "cpu_pct": meta.get("cpu_pct", client_data.get("cpu_pct", 0)),
            "ram_pct": meta.get("ram_pct", client_data.get("ram_pct", 0)),
            "ram_used_mb": meta.get("ram_used_mb", client_data.get("ram_used_mb", 0)),
            "ram_total_mb": meta.get("ram_total_mb", client_data.get("ram_total_mb", 0)),
            "disk_pct": meta.get("disk_pct", client_data.get("disk_pct", 0)),
            "disk_free_gb": meta.get("disk_free_gb", client_data.get("disk_free_gb", 0)),
            "disk_total_gb": meta.get("disk_total_gb", client_data.get("disk_total_gb", 0)),
            "disk_used_gb": meta.get("disk_used_gb", client_data.get("disk_used_gb", 0)),
            "uptime_seconds": meta.get("uptime_seconds", client_data.get("uptime_seconds", 0)),
            "health_score": meta.get("health_score", client_data.get("health_score", 100)),
            "health_status": meta.get("health_status", client_data.get("health_status", "good")),
            "health_issues": meta.get("health_issues", client_data.get("health_issues", [])),
            "active_app": meta.get("active_app", client_data.get("active_app", "Desktop / Standby")),
            "is_locked": meta.get("is_locked", client_data.get("is_locked", 0)),
            "is_teacher": self.is_teacher_client(client_data, client_id),
            "agent_version": meta.get("agent_version", client_data.get("agent_version", f"v{AGENT_VERSION}")),
            "is_online": True,
            "last_seen": now,
            "first_seen": client_data.get("first_seen", now)
        })
        self.clients_cache[client_id] = client_data

        # Auto-prune any stale offline ghost entries that share this client's IP and base hostname
        ghost_ids_to_clean = []
        for other_cid, other_data in list(self.clients_cache.items()):
            if other_cid != client_id and not other_data.get("is_online", False):
                same_ip = (other_data.get("ip_address") == client_ip and client_ip not in ("127.0.0.1", "::1", "localhost"))
                base_other = other_cid.split('_')[0].upper()
                base_current = client_id.split('_')[0].upper()
                if same_ip and base_other == base_current:
                    ghost_ids_to_clean.append(other_cid)
        
        for ghost_id in ghost_ids_to_clean:
            ghost_data = self.clients_cache.get(ghost_id, {})
            if ghost_data.get("custom_name") and not client_data.get("custom_name"):
                client_data["custom_name"] = ghost_data["custom_name"]
                client_data["display_name"] = ghost_data["custom_name"]
            await self.delete_client(ghost_id)

        # Persist to DB
        self._save_client_to_db(client_data)

        # Notify dashboard immediately with cached thumbnail if available
        broadcast_client = dict(client_data)
        if client_id in self.client_thumbnails:
            broadcast_client["thumbnail"] = self.client_thumbnails[client_id]

        await self.broadcast_to_admins({
            "type": "client_connected",
            "client": broadcast_client,
            "stats": self.compute_stats()
        })
        log_audit("CONNECT", client_id, f"Client {client_data['display_name']} ({client_data['ip_address']}) terhubung (Agen {client_data['agent_version']}).")

    def _save_client_to_db(self, c: dict):
        try:
            conn = get_db_connection()
            cursor = conn.cursor()
            cursor.execute("""
                INSERT INTO clients (
                    client_id, hostname, ip_address, mac_address, machine_guid, lab_room,
                    os_info, cpu_pct, ram_pct, ram_used_mb, ram_total_mb,
                    disk_pct, active_app, is_locked, is_online, last_seen, first_seen, agent_version, is_teacher, custom_name
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                ON CONFLICT(client_id) DO UPDATE SET
                    hostname=excluded.hostname,
                    ip_address=excluded.ip_address,
                    mac_address=COALESCE(NULLIF(excluded.mac_address, ''), clients.mac_address),
                    machine_guid=COALESCE(NULLIF(excluded.machine_guid, ''), clients.machine_guid),
                    lab_room=excluded.lab_room,
                    os_info=excluded.os_info,
                    cpu_pct=excluded.cpu_pct,
                    ram_pct=excluded.ram_pct,
                    ram_used_mb=excluded.ram_used_mb,
                    ram_total_mb=excluded.ram_total_mb,
                    disk_pct=excluded.disk_pct,
                    active_app=excluded.active_app,
                    is_locked=excluded.is_locked,
                    is_online=excluded.is_online,
                    last_seen=excluded.last_seen,
                    agent_version=excluded.agent_version,
                    is_teacher=excluded.is_teacher,
                    custom_name=COALESCE(NULLIF(clients.custom_name, ''), excluded.custom_name)
            """, (
                c["client_id"], c.get("hostname", ""), c.get("ip_address", ""),
                c.get("mac_address", ""), c.get("machine_guid", ""), c.get("lab_room", ""), c.get("os_info", ""),
                c.get("cpu_pct", 0), c.get("ram_pct", 0), c.get("ram_used_mb", 0),
                c.get("ram_total_mb", 0), c.get("disk_pct", 0), c.get("active_app", ""),
                1 if c.get("is_locked") else 0, 1 if c.get("is_online") else 0,
                c.get("last_seen", time.time()), c.get("first_seen", time.time()),
                c.get("agent_version", f"v{AGENT_VERSION}"),
                1 if self.is_teacher_client(c, c.get("client_id", "")) else 0,
                c.get("custom_name", "")
            ))
            conn.commit()
            conn.close()
        except Exception as e:
            print(f"[DB Error] save client: {e}")

    async def update_telemetry(self, client_id: str, data: dict):
        if client_id not in self.clients_cache:
            return
        
        c = self.clients_cache[client_id]
        c["cpu_pct"] = data.get("cpu_pct", c.get("cpu_pct", 0))
        c["ram_pct"] = data.get("ram_pct", c.get("ram_pct", 0))
        c["ram_used_mb"] = data.get("ram_used_mb", c.get("ram_used_mb", 0))
        c["ram_total_mb"] = data.get("ram_total_mb", c.get("ram_total_mb", 0))
        c["disk_pct"] = data.get("disk_pct", c.get("disk_pct", 0))
        c["disk_free_gb"] = data.get("disk_free_gb", c.get("disk_free_gb", 0))
        c["disk_total_gb"] = data.get("disk_total_gb", c.get("disk_total_gb", 0))
        c["disk_used_gb"] = data.get("disk_used_gb", c.get("disk_used_gb", 0))
        c["uptime_seconds"] = data.get("uptime_seconds", c.get("uptime_seconds", 0))
        c["health_score"] = data.get("health_score", c.get("health_score", 100))
        c["health_status"] = data.get("health_status", c.get("health_status", "good"))
        c["health_issues"] = data.get("health_issues", c.get("health_issues", []))
        c["active_app"] = data.get("active_app", c.get("active_app", ""))
        c["is_locked"] = data.get("is_locked", c.get("is_locked", 0))
        c["fps_mode"] = data.get("fps_mode", c.get("fps_mode", "1.0s (Aktif)"))
        c["idle_seconds"] = data.get("idle_seconds", c.get("idle_seconds", 0))
        c["is_online"] = True
        c["last_seen"] = time.time()

        if "processes" in data:
            self.client_processes[client_id] = data["processes"]

        # Blacklist & Prohibited App Detection (Exempt teacher PC)
        is_teacher_pc = self.is_teacher_client(c, client_id)
        matched_kw = None
        if not is_teacher_pc and self.blacklist_keywords:
            active_str = (c.get("active_app") or "").lower()
            for kw in self.blacklist_keywords:
                if kw in active_str:
                    matched_kw = kw
                    break
            if not matched_kw and "processes" in data:
                for p in data["processes"]:
                    pname = (p.get("name") or "").lower()
                    for kw in self.blacklist_keywords:
                        if kw in pname:
                            matched_kw = kw
                            break
                    if matched_kw:
                        break

        if matched_kw:
            prev_v = self.client_violations.get(client_id)
            v_info = {
                "detected": True,
                "keyword": matched_kw,
                "app": c.get("active_app", ""),
                "time": time.time()
            }
            self.client_violations[client_id] = v_info
            c["violation"] = v_info

            # Auto-kill if enabled
            if self.auto_kill_blacklist:
                proc_to_kill = c.get("active_app", "").split("(")[0].strip()
                if proc_to_kill.endswith(".exe"):
                    asyncio.create_task(self.send_command_to_clients(client_id, "kill_process", {"name": proc_to_kill}))
                    log_audit("AUTO_KILL", client_id, f"Auto-kill game '{matched_kw}' ({proc_to_kill})", "ACTION")

            # Throttle alerts to admins: once every 12 seconds per client
            if not prev_v or (time.time() - prev_v.get("alert_time", 0) > 12.0):
                v_info["alert_time"] = time.time()
                log_audit("VIOLATION", client_id, f"Aplikasi terlarang terdeteksi: '{matched_kw}' ({c.get('active_app')})", "WARNING")
                await self.broadcast_to_admins({
                    "type": "violation_alert",
                    "client_id": client_id,
                    "hostname": c.get("hostname", client_id),
                    "lab_room": c.get("lab_room", ""),
                    "keyword": matched_kw,
                    "app": c.get("active_app", ""),
                    "auto_killed": self.auto_kill_blacklist
                })
        else:
            if client_id in self.client_violations:
                del self.client_violations[client_id]
                c.pop("violation", None)
                await self.broadcast_to_admins({
                    "type": "violation_cleared",
                    "client_id": client_id
                })

        # Forward telemetry to admins
        await self.broadcast_to_admins({
            "type": "telemetry",
            "client_id": client_id,
            "data": {
                "cpu_pct": c["cpu_pct"],
                "ram_pct": c["ram_pct"],
                "ram_used_mb": c["ram_used_mb"],
                "disk_pct": c["disk_pct"],
                "disk_free_gb": c.get("disk_free_gb", 0),
                "disk_total_gb": c.get("disk_total_gb", 0),
                "disk_used_gb": c.get("disk_used_gb", 0),
                "uptime_seconds": c.get("uptime_seconds", 0),
                "health_score": c.get("health_score", 100),
                "health_status": c.get("health_status", "good"),
                "health_issues": c.get("health_issues", []),
                "active_app": c["active_app"],
                "is_locked": c["is_locked"],
                "fps_mode": c.get("fps_mode", "1.0s (Aktif)"),
                "idle_seconds": c.get("idle_seconds", 0),
                "is_online": True,
                "last_seen": c["last_seen"],
                "violation": c.get("violation")
            },
            "stats": self.compute_stats()
        })

    async def update_thumbnail(self, client_id: str, base64_img: str):
        # Optimization: Do not re-broadcast if screen thumbnail hasn't changed
        if self.client_thumbnails.get(client_id) == base64_img:
            return
        self.client_thumbnails[client_id] = base64_img
        await self.broadcast_to_admins({
            "type": "screen_update",
            "client_id": client_id,
            "thumbnail": base64_img
        })

    async def unregister_agent(self, client_id: str, websocket: Optional[WebSocket] = None):
        if websocket is not None and self.active_agents.get(client_id) != websocket:
            # Belongs to an older or replaced socket; do not drop current active agent
            return
        if client_id in self.active_agents:
            del self.active_agents[client_id]
        
        # Free process cache to reclaim memory
        self.client_processes.pop(client_id, None)

        if client_id in self.clients_cache:
            self.clients_cache[client_id]["is_online"] = False
            self._save_client_to_db(self.clients_cache[client_id])
            
            await self.broadcast_to_admins({
                "type": "client_disconnected",
                "client_id": client_id,
                "stats": self.compute_stats()
            })
            log_audit("DISCONNECT", client_id, f"Client {client_id} terputus.", "WARNING")

    async def send_command_to_clients(self, target: Any, action: str, payload: Optional[dict] = None) -> dict:
        """Target can be 'all', 'online', a lab_room name, an IP address, or a list of client_ids."""
        payload = payload or {}

        # Canonical action normalization
        if action == "lock_screen":
            action = "lock"
        elif action == "unlock_screen":
            action = "unlock"

        # Normalize launch_app command cleanly
        if action == "launch_app":
            raw_app = payload.get("app", "").strip()
            payload["app"] = raw_app

        cmd_msg = json.dumps({
            "type": "command",
            "action": action,
            "payload": payload
        })

        # Special handling for Wake-on-LAN (WOL sends UDP Magic Packets to power on remote PCs)
        if action == "wake":
            self.last_sim_wake_time = time.time()
            if target == "all":
                self.woken_clients.add("all")
            elif isinstance(target, list):
                for t in target:
                    self.woken_clients.add(str(t))
            elif isinstance(target, str):
                self.woken_clients.add(target)

            target_clients = []
            if target in ("all", "offline"):
                target_clients = [c for c in self.clients_cache.values() if not c.get("is_online") or target == "all"]
            elif isinstance(target, list):
                target_clients = [self.clients_cache[cid] for cid in target if cid in self.clients_cache]
            elif isinstance(target, str):
                if target.startswith("room:"):
                    room_name = target.split(":", 1)[1]
                    target_clients = [c for c in self.clients_cache.values() if c.get("lab_room") == room_name]
                elif target in self.clients_cache:
                    target_clients = [self.clients_cache[target]]

            sent_count = 0
            wake_targets = []
            for c in target_clients:
                cid = c.get("client_id", "")
                mac = c.get("mac_address", "")
                ip = c.get("ip_address", "")
                hostname = c.get("hostname", cid)
                if mac:
                    ok, msg = send_wake_on_lan(mac, ip)
                    if ok:
                        sent_count += 1
                        wake_targets.append(hostname)
                        log_audit("WOL", cid, f"Magic Packet (WOL) terkirim ke {hostname} (MAC: {mac})", "SUCCESS")
                else:
                    log_audit("WOL", cid, f"Gagal WOL: MAC Address belum tersimpan untuk {hostname}", "WARNING")

            target_str = target if isinstance(target, str) else f"{len(target_clients)} PCs"
            log_audit("COMMAND", str(target_str), f"Wake-on-LAN terkirim ke {sent_count} PC ({', '.join(wake_targets) if wake_targets else 'tidak ada target'})", "SUCCESS" if sent_count > 0 else "NO_TARGETS")

            await self.broadcast_to_admins({
                "type": "command_broadcasted",
                "action": "wake",
                "target": target,
                "sent_count": sent_count,
                "message": f"Sinyal Wake-on-LAN (WOL) berhasil dikirim ke {sent_count} komputer.",
                "logs": get_recent_logs(10)
            })

            return {
                "status": "ok",
                "action": "wake",
                "sent_count": sent_count,
                "targets": wake_targets,
                "message": f"Sinyal Wake-on-LAN (WOL) berhasil dikirim ke {sent_count} komputer."
            }

        # Clear wake flags if shutdown
        if action == "shutdown":
            if target == "all":
                self.woken_clients.clear()
            elif isinstance(target, list):
                for t in target:
                    self.woken_clients.discard(str(t))
            elif isinstance(target, str):
                self.woken_clients.discard(target)

        target_ids: List[str] = []
        if target == "all":
            target_ids = list(self.active_agents.keys())
        elif target == "online":
            target_ids = [cid for cid, c in self.clients_cache.items() if c.get("is_online") and cid in self.active_agents]
        elif isinstance(target, list):
            for t in target:
                t_str = str(t).strip()
                if t_str in self.active_agents:
                    target_ids.append(t_str)
                else:
                    # Resolve IP address or hostname to active client_id
                    for cid, c in self.clients_cache.items():
                        if cid in self.active_agents and (c.get("ip_address") == t_str or str(c.get("hostname", "")).upper() == t_str.upper()):
                            target_ids.append(cid)
                            break
        elif isinstance(target, str):
            t_str = target.strip()
            if t_str.startswith("room:"):
                room_name = t_str.split(":", 1)[1]
                target_ids = [cid for cid, c in self.clients_cache.items() if c.get("lab_room") == room_name and cid in self.active_agents]
            elif t_str in self.active_agents:
                target_ids = [t_str]
            else:
                # Resolve IP address or hostname to active client_id
                for cid, c in self.clients_cache.items():
                    if cid in self.active_agents and (c.get("ip_address") == t_str or str(c.get("hostname", "")).upper() == t_str.upper()):
                        target_ids = [cid]
                        break

        # Robust Fallback command for older v2.1.0 agents if action is send_file
        fallback_file_cmd = None
        if action == "send_file":
            file_url = payload.get("url", "")
            filename = payload.get("filename", "berkas_guru")
            dest = payload.get("destination", "desktop").lower()
            auto_open = payload.get("auto_open", False)
            show_popup = payload.get("show_popup", True)

            # PowerShell script dynamically resolves real special folders (Desktop, Downloads, Documents)
            # Immune to quote escaping or spaces/parentheses in filenames
            clean_name_escaped = filename.replace("'", "''")
            ps_script = f"""[Net.ServicePointManager]::SecurityProtocol = [Net.SecurityProtocolType]::Tls12
$folder = switch ('{dest}') {{
    'downloads' {{ Join-Path ([Environment]::GetFolderPath([Environment+SpecialFolder]::UserProfile)) 'Downloads' }}
    'documents' {{ [Environment]::GetFolderPath([Environment+SpecialFolder]::MyDocuments) }}
    default     {{ [Environment]::GetFolderPath([Environment+SpecialFolder]::Desktop) }}
}}
if (-not (Test-Path $folder)) {{
    $folder = [Environment]::GetFolderPath([Environment+SpecialFolder]::Desktop)
}}
$destPath = Join-Path $folder '{clean_name_escaped}'
$wc = New-Object System.Net.WebClient
$wc.Headers.Add('User-Agent', 'Mozilla/5.0')
$wc.DownloadFile('{file_url}', $destPath)

if ({1 if show_popup else 0} -eq 1) {{
    try {{
        $ws = New-Object -ComObject Wscript.Shell
        $null = $ws.Popup('Berkas ' + [IO.Path]::GetFileName($destPath) + ' berhasil diterima dari Guru dan disimpan di Desktop/Folder Anda!', 5, 'LabGuard Guru', 64)
    }} catch {{}}
}}

if ({1 if auto_open else 0} -eq 1) {{
    try {{
        Start-Process -FilePath $destPath
    }} catch {{}}
}}
"""
            enc = base64.b64encode(ps_script.encode("utf-16le")).decode("ascii")
            ps_cmd = f"powershell.exe -NoProfile -ExecutionPolicy Bypass -WindowStyle Hidden -EncodedCommand {enc}"
            fallback_file_cmd = json.dumps({
                "type": "command",
                "action": "launch_app",
                "payload": {"app": ps_cmd}
            })

        # Robust Fallback for older agents if action is mute/unmute
        fallback_audio_cmd = None
        if action in ("mute", "unmute", "mute_audio"):
            is_mute = payload.get("mute", True) if action == "mute_audio" else (action == "mute")
            key_code = 173 if is_mute else 175
            ps_mute = f"$ws = New-Object -ComObject WScript.Shell; $ws.SendKeys([char]{key_code})"
            fallback_audio_cmd = json.dumps({
                "type": "command",
                "action": "launch_app",
                "payload": {"app": f"powershell.exe -NoProfile -WindowStyle Hidden -Command \"{ps_mute}\""}
            })

        dead_agents = []
        sent_count = 0
        for cid in target_ids:
            ws = self.active_agents.get(cid)
            if ws:
                try:
                    c_ver = str(self.clients_cache.get(cid, {}).get("agent_version", "")).strip()
                    is_legacy_v2 = not (c_ver.startswith("v2.2") or c_ver.startswith("2.2") or c_ver.startswith("v2.3") or c_ver.startswith("2.3"))
                    is_legacy_v3 = not (c_ver.startswith("v2.3") or c_ver.startswith("2.3"))
                    if action == "send_file" and is_legacy_v2 and fallback_file_cmd:
                        await ws.send_text(fallback_file_cmd)
                    elif action in ("mute", "unmute", "mute_audio") and is_legacy_v3 and fallback_audio_cmd:
                        await ws.send_text(fallback_audio_cmd)
                    else:
                        await ws.send_text(cmd_msg)
                    sent_count += 1
                except Exception as e:
                    print(f"[Command Error] Send to {cid}: {e}")
                    dead_agents.append((cid, ws))

        for cid, ws in dead_agents:
            await self.unregister_agent(cid, ws)

        # If shutdown or restart, execute network RPC power off as secondary safeguard for real LAN PCs
        if action in ("shutdown", "restart"):
            all_target_cids = target_ids if target_ids else []
            if isinstance(target, list):
                all_target_cids = list(set(all_target_cids + target))
            elif isinstance(target, str) and target in self.clients_cache:
                all_target_cids = list(set(all_target_cids + [target]))
            elif target == "all":
                all_target_cids = list(self.clients_cache.keys())

            network_sent = 0
            for cid in all_target_cids:
                c = self.clients_cache.get(cid)
                if c:
                    ip = c.get("ip_address", "")
                    if ip and not ip.startswith("192.168.10.") and ip not in ("127.0.0.1", "localhost", "::1"):
                        try:
                            network_sent += 1
                            threading.Thread(
                                target=NetworkEngine.remote_power_off,
                                args=(ip, 3, "Komputer dimatikan oleh Guru / Administrator Lab", action == "restart", True),
                                daemon=True
                            ).start()
                        except Exception:
                            pass
            if sent_count == 0 and network_sent > 0:
                sent_count = network_sent

        # Update local locked state if action is lock/unlock
        if action in ("lock", "unlock"):
            new_lock_state = 1 if action == "lock" else 0
            for cid in target_ids:
                if cid in self.clients_cache:
                    self.clients_cache[cid]["is_locked"] = new_lock_state
            try:
                conn = get_db_connection()
                cur = conn.cursor()
                for cid in target_ids:
                    cur.execute("UPDATE clients SET is_locked = ? WHERE client_id = ?", (new_lock_state, cid))
                conn.commit()
                conn.close()
            except Exception as e:
                print(f"[DB Error] update is_locked: {e}")
            
            # Broadcast immediate state update to all teacher dashboards
            for cid in target_ids:
                c_data = self.clients_cache.get(cid)
                if c_data:
                    await self.broadcast_to_admins({
                        "type": "client_updated",
                        "client": c_data
                    })

        target_str = target if isinstance(target, str) else f"{len(target_ids)} PCs"
        details = f"Action: {action} ({json.dumps(payload)})"
        log_audit("COMMAND", str(target_str), details, "SUCCESS" if sent_count > 0 else "NO_TARGETS")

        # Notify admins about command result
        await self.broadcast_to_admins({
            "type": "command_broadcasted",
            "action": action,
            "target": target,
            "sent_count": sent_count,
            "logs": get_recent_logs(10)
        })

        return {"status": "ok", "sent_count": sent_count, "targets": target_ids}

    def compute_stats(self) -> dict:
        total = len(self.clients_cache)
        online = sum(1 for c in self.clients_cache.values() if c.get("is_online"))
        offline = total - online
        locked = sum(1 for c in self.clients_cache.values() if c.get("is_online") and c.get("is_locked"))

        online_clients = [c for c in self.clients_cache.values() if c.get("is_online")]
        avg_cpu = round(sum(c.get("cpu_pct", 0) for c in online_clients) / max(len(online_clients), 1), 1)
        avg_ram = round(sum(c.get("ram_pct", 0) for c in online_clients) / max(len(online_clients), 1), 1)

        high_cpu_count = sum(1 for c in online_clients if c.get("cpu_pct", 0) >= 85)

        return {
            "total_clients": total,
            "online_clients": online,
            "offline_clients": offline,
            "locked_clients": locked,
            "avg_cpu": avg_cpu,
            "avg_ram": avg_ram,
            "high_cpu_count": high_cpu_count,
            "violations_count": len(self.client_violations)
        }

    async def delete_client(self, client_id: str) -> bool:
        if client_id in self.active_agents:
            ws = self.active_agents.pop(client_id)
            try:
                await ws.close()
            except Exception:
                pass
        
        existed = False
        if client_id in self.clients_cache:
            del self.clients_cache[client_id]
            existed = True
        self.client_thumbnails.pop(client_id, None)
        self.client_processes.pop(client_id, None)

        try:
            conn = get_db_connection()
            cursor = conn.cursor()
            cursor.execute("DELETE FROM clients WHERE client_id = ?", (client_id,))
            cursor.execute("DELETE FROM telemetry_history WHERE client_id = ?", (client_id,))
            conn.commit()
            conn.close()
            existed = True
        except Exception as e:
            print(f"[DB Error] delete_client: {e}")

        if existed:
            await self.broadcast_to_admins({
                "type": "client_deleted",
                "client_id": client_id,
                "stats": self.compute_stats()
            })

        return existed


init_db()
manager = ConnectionManager()


# --- Background Tasks: Offline Watchdog & Telemetry Logging ---
async def offline_watchdog():
    """Periodically marks clients as offline if heartbeat timed out and trims server memory."""
    last_mem_trim = 0.0
    while True:
        try:
            await asyncio.sleep(3.0)
            now = time.time()
            to_disconnect = []
            for cid, c in list(manager.clients_cache.items()):
                if c.get("is_online"):
                    last_seen = c.get("last_seen", 0)
                    if (now - last_seen) > OFFLINE_TIMEOUT_SECONDS:
                        to_disconnect.append(cid)

            for cid in to_disconnect:
                ws = manager.active_agents.get(cid)
                if ws:
                    try:
                        await ws.close(code=1000, reason="Timeout")
                    except Exception:
                        pass
                await manager.unregister_agent(cid, ws)

            # Periodic memory trimming every 30 seconds
            if (now - last_mem_trim) >= 30.0:
                last_mem_trim = now
                import gc
                gc.collect()
                if sys.platform == "win32":
                    try:
                        import ctypes
                        # Empty working set to return unreferenced physical RAM pages back to Windows OS
                        ctypes.windll.kernel32.SetProcessWorkingSetSize(-1, -1, -1)
                    except Exception:
                        pass

        except Exception as e:
            print(f"[Watchdog Error] {e}")


# --- WebSocket Endpoints ---
@app.websocket("/ws/admin")
async def websocket_admin_endpoint(websocket: WebSocket):
    token = websocket.query_params.get("token") or websocket.cookies.get("labguard_token")
    client_ip = websocket.client.host if websocket.client else "127.0.0.1"
    server_lan_ip = get_lan_ip()
    local_ips = ("127.0.0.1", "::1", "localhost", server_lan_ip)

    # Check authorization strictly via token or cookie
    if not is_valid_token(token):
        await websocket.close(code=4001, reason="Unauthorized")
        return
    await manager.connect_admin(websocket)
    try:
        while True:
            raw_text = await websocket.receive_text()
            try:
                data = json.loads(raw_text)
                msg_type = data.get("type")
                if msg_type == "command":
                    target = data.get("target", "all")
                    action = data.get("action")
                    payload = data.get("payload", {})
                    await manager.send_command_to_clients(target, action, payload)
                elif msg_type in ("start_remote", "stop_remote", "remote_input"):
                    cid = data.get("client_id")
                    if msg_type == "start_remote":
                        # Immediately confirm remote_started and send cached frame for instant 0-ms startup
                        try:
                            await websocket.send_text(json.dumps({
                                "type": "remote_started",
                                "client_id": cid
                            }))
                            if cid in manager.client_thumbnails:
                                await websocket.send_text(json.dumps({
                                    "type": "remote_frame",
                                    "client_id": cid,
                                    "frame": manager.client_thumbnails[cid],
                                    "timestamp": time.time()
                                }))
                        except Exception:
                            pass

                    target_ws = manager.active_agents.get(cid) if cid else None
                    if not target_ws and cid:
                        cid_lower = str(cid).strip().lower()
                        for k, v in manager.active_agents.items():
                            if k.lower() == cid_lower:
                                target_ws = v
                                break

                    if target_ws:
                        try:
                            await target_ws.send_text(raw_text)
                        except Exception as e:
                            print(f"[WS Admin->Agent error ({cid})]: {e}")
                            await manager.unregister_agent(cid, target_ws)
                    elif msg_type == "start_remote":
                        print(f"[WS Admin] Target agent '{cid}' is not currently connected in active_agents!")
                        await websocket.send_text(json.dumps({
                            "type": "remote_stopped",
                            "client_id": cid,
                            "message": f"Komputer {cid} sedang tidak terhubung atau offline."
                        }))
                elif msg_type == "ping":
                    await websocket.send_text(json.dumps({"type": "pong", "time": time.time()}))
            except json.JSONDecodeError:
                pass
    except WebSocketDisconnect:
        manager.disconnect_admin(websocket)
    except Exception as e:
        print(f"[WS Admin Error] {e}")
        manager.disconnect_admin(websocket)


@app.websocket("/ws/agent")
async def websocket_agent_endpoint(websocket: WebSocket):
    await websocket.accept()
    client_id: Optional[str] = None
    try:
        # First message expected is handshake
        raw_first = await websocket.receive_text()
        meta = json.loads(raw_first)
        if meta.get("type") != "handshake":
            await websocket.close(code=1008, reason="Handshake expected")
            return
        
        # Determine accurate client IP & MAC
        incoming_ip = websocket.client.host if (websocket.client and getattr(websocket.client, "host", None)) else None
        meta_ip = str(meta.get("ip_address", "")).strip()
        client_ip = incoming_ip or meta_ip or "127.0.0.1"
        client_mac = str(meta.get("mac_address", "")).strip().upper()
        raw_cid = str(meta.get("client_id") or meta.get("hostname") or f"PC-{int(time.time())}").strip()

        client_guid = str(meta.get("machine_guid", "")).strip().upper()
        matched_cid = None

        # Step 1: Match by persistent Machine GUID (highest accuracy, immune to IP/MAC/adapter changes)
        if client_guid:
            for cid, cdata in manager.clients_cache.items():
                if str(cdata.get("machine_guid", "")).strip().upper() == client_guid:
                    matched_cid = cid
                    break

        # Step 2: Match by physical MAC address
        if not matched_cid and client_mac and client_mac not in ("00:00:00:00:00:00", ""):
            for cid, cdata in manager.clients_cache.items():
                existing_mac = str(cdata.get("mac_address", "")).strip().upper()
                if existing_mac and existing_mac == client_mac:
                    matched_cid = cid
                    break

        # Step 3: Match by identical LAN IP address & matching base hostname
        if not matched_cid and client_ip and client_ip not in ("127.0.0.1", "::1", "localhost"):
            for cid, cdata in manager.clients_cache.items():
                if cdata.get("ip_address") == client_ip:
                    base_existing = cid.split('_')[0].upper()
                    base_incoming = raw_cid.split('_')[0].upper()
                    if base_existing == base_incoming:
                        matched_cid = cid
                        break

        if matched_cid:
            client_id = matched_cid
        else:
            client_id = raw_cid
            existing = manager.clients_cache.get(client_id)
            if existing:
                existing_mac = str(existing.get("mac_address", "")).strip().upper()
                existing_ip = str(existing.get("ip_address", "")).strip()
                # Genuine collision: raw_cid is ALREADY currently ONLINE right now on another socket
                is_currently_online = (client_id in manager.active_agents and manager.active_agents[client_id] != websocket)
                is_collision = False
                if is_currently_online:
                    if client_mac and existing_mac and client_mac != existing_mac:
                        is_collision = True
                    elif client_ip and existing_ip and client_ip != existing_ip:
                        is_collision = True
                
                if is_collision:
                    suffix = client_ip.split('.')[-1] if '.' in client_ip else (client_mac.replace(':', '')[-4:] if client_mac else "2")
                    client_id = f"{raw_cid}_{suffix}"

        await manager.register_agent(client_id, websocket, meta)

        # Send handshake ACK
        await websocket.send_text(json.dumps({
            "type": "handshake_ack",
            "status": "connected",
            "server_time": time.time()
        }))

        # Main message loop
        while True:
            raw_text = await websocket.receive_text()
            data = json.loads(raw_text)
            manager.touch_client(client_id)
            msg_type = data.get("type")

            if msg_type == "telemetry":
                await manager.update_telemetry(client_id, data)
            elif msg_type == "screen":
                thumb = data.get("thumbnail")
                if thumb:
                    await manager.update_thumbnail(client_id, thumb)
            elif msg_type == "remote_frame":
                await manager.broadcast_to_admins({
                    "type": "remote_frame",
                    "client_id": client_id,
                    "frame": data.get("frame"),
                    "timestamp": data.get("timestamp")
                })
            elif msg_type in ("remote_started", "remote_stopped"):
                await manager.broadcast_to_admins({
                    "type": msg_type,
                    "client_id": client_id
                })
            elif msg_type == "installed_apps":
                apps = data.get("apps", [])
                manager.client_installed_apps[client_id] = apps
                await manager.broadcast_to_admins({
                    "type": "installed_apps",
                    "client_id": client_id,
                    "apps": apps
                })
            elif msg_type == "winget_updates":
                # Pending software updates from winget on student PC
                updates = data.get("updates", [])
                manager.client_winget_updates[client_id] = updates
                await manager.broadcast_to_admins({
                    "type": "winget_updates",
                    "client_id": client_id,
                    "updates": updates,
                    "total": len(updates)
                })
            elif msg_type == "windows_updates":
                # Windows hotfix/patch history from student PC
                patches = data.get("patches", [])
                manager.client_windows_updates[client_id] = patches
                await manager.broadcast_to_admins({
                    "type": "windows_updates",
                    "client_id": client_id,
                    "patches": patches,
                    "total": len(patches)
                })
            elif msg_type == "command_result":
                action = data.get("action")
                status = data.get("status", "DONE")
                msg = data.get("message", "")
                log_audit(f"RESULT_{action.upper()}", client_id, msg, status)
                await manager.broadcast_to_admins({
                    "type": "command_result",
                    "client_id": client_id,
                    "action": action,
                    "status": status,
                    "message": msg
                })

    except WebSocketDisconnect:
        if client_id:
            await manager.unregister_agent(client_id, websocket)
    except Exception as e:
        print(f"[WS Agent Error ({client_id})]: {e}")
        if client_id:
            await manager.unregister_agent(client_id, websocket)


# --- REST API Endpoints ---
@app.get("/api/stats")
async def get_stats():
    return manager.compute_stats()


@app.get("/api/clients")
async def get_clients(room: Optional[str] = None):
    clients = list(manager.clients_cache.values())
    if room:
        clients = [c for c in clients if c.get("lab_room") == room]
    return {
        "clients": clients,
        "stats": manager.compute_stats()
    }


@app.get("/api/client/{client_id}")
async def get_client_detail(client_id: str):
    if client_id not in manager.clients_cache:
        raise HTTPException(status_code=404, detail="Client not found")
    client = dict(manager.clients_cache[client_id])
    client["thumbnail"] = manager.client_thumbnails.get(client_id, "")
    client["processes"] = manager.client_processes.get(client_id, [])
    client["installed_apps"] = manager.client_installed_apps.get(client_id, [])
    return client


@app.get("/api/client/{client_id}/apps")
async def get_client_apps(client_id: str):
    if client_id not in manager.clients_cache:
        raise HTTPException(status_code=404, detail="Client not found")
    
    # If apps not yet cached, automatically request from agent
    apps = manager.client_installed_apps.get(client_id, [])
    if not apps and client_id in manager.active_agents:
        await manager.send_command_to_clients(client_id, "get_installed_apps", {})

    return {
        "client_id": client_id,
        "total": len(apps),
        "apps": apps
    }


@app.post("/api/client/{client_id}/apps/refresh")
async def refresh_client_apps(client_id: str, request: Request):
    require_admin_auth(request)
    if client_id not in manager.clients_cache:
        raise HTTPException(status_code=404, detail="Client not found")
    await manager.send_command_to_clients(client_id, "get_installed_apps", {})
    return {"status": "ok", "message": f"Permintaan pemindaian aplikasi dikirim ke {client_id}"}


# --- Software & Windows Update Endpoints ---
@app.get("/api/client/{client_id}/updates/winget")
async def get_client_winget_updates(client_id: str):
    """Returns cached list of pending winget software updates for a client PC."""
    if client_id not in manager.clients_cache:
        raise HTTPException(status_code=404, detail="Client not found")
    updates = manager.client_winget_updates.get(client_id, [])
    return {
        "client_id": client_id,
        "total": len(updates),
        "updates": updates
    }


@app.post("/api/client/{client_id}/updates/winget/check")
async def check_winget_updates(client_id: str, request: Request):
    """Sends winget_check command to student PC to fetch pending software updates via winget."""
    require_admin_auth(request)
    if client_id not in manager.clients_cache:
        raise HTTPException(status_code=404, detail="Client not found")
    if client_id not in manager.active_agents:
        raise HTTPException(status_code=409, detail="Komputer sedang offline, tidak dapat mengirim perintah")
    await manager.send_command_to_clients(client_id, "winget_check", {})
    log_audit("WINGET_CHECK", client_id, f"Permintaan cek pembaruan winget dikirim ke {client_id}")
    return {"status": "ok", "message": f"Permintaan cek pembaruan software (winget) dikirim ke {client_id}"}


@app.post("/api/client/{client_id}/updates/winget/upgrade")
async def winget_upgrade(client_id: str, request: Request):
    """Sends upgrade command via winget to student PC. Upgrades specific package_id or all."""
    require_admin_auth(request)
    if client_id not in manager.clients_cache:
        raise HTTPException(status_code=404, detail="Client not found")
    if client_id not in manager.active_agents:
        raise HTTPException(status_code=409, detail="Komputer sedang offline")
    body = await request.json()
    package_id = body.get("package_id", "")   # Empty = upgrade all
    await manager.send_command_to_clients(client_id, "winget_upgrade", {"package_id": package_id})
    label = f"'{package_id}'" if package_id else "semua paket"
    log_audit("WINGET_UPGRADE", client_id, f"Perintah upgrade winget {label} dikirim ke {client_id}")
    return {"status": "ok", "message": f"Perintah upgrade software {label} dikirim ke {client_id}"}


@app.post("/api/updates/winget/check-all")
async def check_winget_updates_all(request: Request):
    """Broadcast winget update check to ALL online student PCs."""
    require_admin_auth(request)
    res = await manager.send_command_to_clients("all", "winget_check", {})
    log_audit("WINGET_CHECK_ALL", "all", f"Broadcast cek pembaruan winget ke {res.get('sent_count', 0)} PC")
    return {"status": "ok", "sent_count": res.get("sent_count", 0), "message": "Cek pembaruan software dikirim ke semua PC online"}


@app.post("/api/updates/winget/upgrade-all")
async def winget_upgrade_all(request: Request):
    """Broadcast winget upgrade --all to ALL online student PCs."""
    require_admin_auth(request)
    res = await manager.send_command_to_clients("all", "winget_upgrade", {"package_id": ""})
    log_audit("WINGET_UPGRADE_ALL", "all", f"Broadcast upgrade winget --all ke {res.get('sent_count', 0)} PC")
    return {"status": "ok", "sent_count": res.get("sent_count", 0), "message": "Perintah upgrade semua software dikirim ke semua PC online"}


@app.get("/api/client/{client_id}/updates/windows")
async def get_client_windows_updates(client_id: str):
    """Returns cached Windows hotfix/patch list for a client PC."""
    if client_id not in manager.clients_cache:
        raise HTTPException(status_code=404, detail="Client not found")
    patches = manager.client_windows_updates.get(client_id, [])
    return {
        "client_id": client_id,
        "total": len(patches),
        "patches": patches
    }


@app.post("/api/client/{client_id}/updates/windows/check")
async def check_windows_updates(client_id: str, request: Request):
    """Sends windows_update_check command to student PC to fetch installed Windows hotfixes."""
    require_admin_auth(request)
    if client_id not in manager.clients_cache:
        raise HTTPException(status_code=404, detail="Client not found")
    if client_id not in manager.active_agents:
        raise HTTPException(status_code=409, detail="Komputer sedang offline")
    await manager.send_command_to_clients(client_id, "windows_update_check", {})
    log_audit("WINUPDATE_CHECK", client_id, f"Permintaan cek Windows Update dikirim ke {client_id}")
    return {"status": "ok", "message": f"Permintaan cek Windows Update dikirim ke {client_id}"}


@app.post("/api/updates/windows/check-all")
async def check_windows_updates_all(request: Request):
    """Broadcast Windows Update check to ALL online student PCs."""
    require_admin_auth(request)
    """Broadcast Windows Update check to ALL online student PCs."""
    res = await manager.send_command_to_clients("all", "windows_update_check", {})
    log_audit("WINUPDATE_CHECK_ALL", "all", f"Broadcast cek Windows Update ke {res.get('sent_count', 0)} PC")
    return {"status": "ok", "sent_count": res.get("sent_count", 0), "message": "Cek Windows Update dikirim ke semua PC online"}


# --- Audio Mute / Unmute Endpoints ---
@app.post("/api/audio/mute-all")
async def bulk_mute_audio(request: Request):
    """Mutes audio on all online student PCs."""
    require_admin_auth(request)
    res = await manager.send_command_to_clients("all", "mute_audio", {"mute": True})
    log_audit("MUTE_ALL", "all", f"Guru membisukan (mute) audio di {res.get('sent_count', 0)} PC siswa")
    return {"status": "ok", "sent_count": res.get("sent_count", 0), "message": f"Audio di {res.get('sent_count', 0)} PC berhasil disenyapkan (mute)"}


@app.post("/api/audio/unmute-all")
async def bulk_unmute_audio(request: Request):
    """Unmutes audio on all online student PCs."""
    require_admin_auth(request)
    res = await manager.send_command_to_clients("all", "mute_audio", {"mute": False})
    log_audit("UNMUTE_ALL", "all", f"Guru menyalakan kembali audio di {res.get('sent_count', 0)} PC siswa")
    return {"status": "ok", "sent_count": res.get("sent_count", 0), "message": f"Audio di {res.get('sent_count', 0)} PC berhasil diaktifkan kembali"}


@app.post("/api/client/{client_id}/mute")
async def toggle_client_mute(client_id: str, request: Request):
    """Mutes or unmutes a specific client PC."""
    require_admin_auth(request)
    if client_id not in manager.clients_cache:
        raise HTTPException(status_code=404, detail="Client not found")
    mute_state = True
    try:
        body = await request.json()
        mute_state = bool(body.get("mute", True))
    except Exception:
        pass
    await manager.send_command_to_clients(client_id, "mute_audio", {"mute": mute_state})
    action_label = "disenyapkan (mute)" if mute_state else "diaktifkan (unmute)"
    log_audit("MUTE_PC" if mute_state else "UNMUTE_PC", client_id, f"Audio PC {client_id} {action_label}")
    return {"status": "ok", "client_id": client_id, "mute": mute_state, "message": f"Audio {client_id} berhasil {action_label}"}


# --- Blacklist & Game Protection Endpoints ---
@app.get("/api/blacklist")
async def get_blacklist():
    """Returns all blacklist rules, auto-kill state, and currently active violations."""
    try:
        conn = get_db_connection()
        conn.row_factory = sqlite3.Row
        cursor = conn.cursor()
        cursor.execute("SELECT * FROM blacklist_rules ORDER BY category, keyword")
        rules = [dict(r) for r in cursor.fetchall()]
        conn.close()

        # Build active violations list with hostnames and details
        active_violations = []
        for cid, v in manager.client_violations.items():
            c = manager.clients_cache.get(cid, {})
            active_violations.append({
                "client_id": cid,
                "hostname": c.get("hostname", cid),
                "lab_room": c.get("lab_room", ""),
                "keyword": v.get("keyword", ""),
                "app": v.get("app", ""),
                "time": v.get("time", 0)
            })

        return {
            "rules": rules,
            "auto_kill": manager.auto_kill_blacklist,
            "violations": active_violations,
            "total_rules": len(rules),
            "total_violations": len(active_violations)
        }
    except Exception as e:
        raise HTTPException(status_code=500, detail=str(e))


@app.post("/api/blacklist/add")
async def add_blacklist_rule(request: Request):
    """Adds a new prohibited keyword / game rule."""
    require_admin_auth(request)
    body = await request.json()
    keyword = str(body.get("keyword", "")).strip().lower()
    category = str(body.get("category", "custom")).strip()
    description = str(body.get("description", "")).strip() or f"Aplikasi {keyword}"
    if not keyword:
        raise HTTPException(status_code=400, detail="Kata kunci tidak boleh kosong")
    
    try:
        conn = get_db_connection()
        cursor = conn.cursor()
        now_dt = datetime.datetime.now().strftime("%Y-%m-%d %H:%M:%S")
        cursor.execute(
            "INSERT INTO blacklist_rules (keyword, category, description, is_active, created_at) VALUES (?, ?, ?, 1, ?) "
            "ON CONFLICT(keyword) DO UPDATE SET is_active = 1, description = excluded.description",
            (keyword, category, description, now_dt)
        )
        conn.commit()
        conn.close()
        manager._load_blacklist_rules()
        log_audit("BLACKLIST_ADD", keyword, f"Menambahkan kata kunci terlarang '{keyword}' (Kategori: {category})")
        return {"status": "ok", "keyword": keyword, "message": f"Kata kunci '{keyword}' berhasil ditambahkan ke blacklist"}
    except Exception as e:
        raise HTTPException(status_code=500, detail=str(e))


@app.post("/api/blacklist/delete")
async def delete_blacklist_rule(request: Request):
    """Deletes or deactivates a blacklist rule."""
    require_admin_auth(request)
    body = await request.json()
    rule_id = body.get("id")
    keyword = str(body.get("keyword", "")).strip().lower()
    try:
        conn = get_db_connection()
        cursor = conn.cursor()
        if rule_id:
            cursor.execute("DELETE FROM blacklist_rules WHERE id = ?", (rule_id,))
        elif keyword:
            cursor.execute("DELETE FROM blacklist_rules WHERE keyword = ?", (keyword,))
        conn.commit()
        conn.close()
        manager._load_blacklist_rules()
        log_audit("BLACKLIST_DEL", str(keyword or rule_id), "Menghapus aturan blacklist")
        return {"status": "ok", "message": "Aturan blacklist berhasil dihapus"}
    except Exception as e:
        raise HTTPException(status_code=500, detail=str(e))


@app.post("/api/blacklist/toggle-auto-kill")
async def toggle_auto_kill(request: Request):
    """Toggles automatic termination of prohibited apps upon detection."""
    require_admin_auth(request)
    manager.auto_kill_blacklist = not manager.auto_kill_blacklist
    try:
        conn = get_db_connection()
        cursor = conn.cursor()
        cursor.execute(
            "INSERT INTO system_settings (key, value) VALUES ('auto_kill_blacklist', ?) "
            "ON CONFLICT(key) DO UPDATE SET value = excluded.value",
            ("1" if manager.auto_kill_blacklist else "0",)
        )
        conn.commit()
        conn.close()
    except Exception as e:
        print(f"[DB Error] toggle auto-kill: {e}")

    state_str = "AKTIF (Game otomatis ditutup)" if manager.auto_kill_blacklist else "NONAKTIF (Hanya peringatan visual)"
    log_audit("BLACKLIST_AUTOKILL", "all", f"Mode Auto-Kill {state_str}")
    return {"status": "ok", "auto_kill": manager.auto_kill_blacklist, "message": f"Mode Tutup Otomatis (Auto-Kill) sekarang {state_str}"}


@app.post("/api/client/{client_id}/wake")
async def wake_single_client(client_id: str, request: Request):
    """Sends a Wake-on-LAN magic packet to turn on an offline student PC."""
    require_admin_auth(request)
    if client_id not in manager.clients_cache:
        raise HTTPException(status_code=404, detail="Client not found")
    c = manager.clients_cache[client_id]
    mac = c.get("mac_address", "")
    ip = c.get("ip_address", "")
    hostname = c.get("hostname", client_id)
    if not mac:
        raise HTTPException(status_code=400, detail=f"MAC address untuk {hostname} belum tercatat.")

    ok, msg = send_wake_on_lan(mac, ip)
    if not ok:
        raise HTTPException(status_code=500, detail=msg)

    log_audit("WOL", client_id, f"Magic Packet (WOL) terkirim ke {hostname} (MAC: {mac})", "SUCCESS")
    await manager.broadcast_to_admins({
        "type": "command_result",
        "client_id": client_id,
        "action": "wake",
        "status": "SUCCESS",
        "message": f"Sinyal Wake-on-LAN (WOL) terkirim ke {hostname} ({mac})"
    })
    return {"status": "ok", "message": f"Sinyal Wake-on-LAN berhasil dikirim ke {hostname} ({mac})"}


@app.post("/api/client/{client_id}/remote/start")
async def start_remote_control(client_id: str, request: Request, quality: int = 55):
    require_admin_auth(request)
    if client_id not in manager.active_agents:
        raise HTTPException(status_code=404, detail="Client is not connected or offline")
    await manager.active_agents[client_id].send_text(json.dumps({
        "type": "start_remote",
        "client_id": client_id,
        "quality": quality
    }))
    log_audit("REMOTE_START", client_id, f"Guru memulai Remote Control interaktif ke {client_id}")
    return {"status": "ok", "message": f"Remote control started for {client_id}"}


@app.post("/api/client/{client_id}/remote/stop")
async def stop_remote_control(client_id: str, request: Request):
    require_admin_auth(request)
    if client_id in manager.active_agents:
        await manager.active_agents[client_id].send_text(json.dumps({
            "type": "stop_remote",
            "client_id": client_id
        }))
    log_audit("REMOTE_STOP", client_id, f"Guru menghentikan Remote Control interaktif ke {client_id}")
    return {"status": "ok", "message": f"Remote control stopped for {client_id}"}


@app.delete("/api/client/{client_id}")
async def delete_client(client_id: str, request: Request):
    """Permanently deletes a client that was uninstalled or no longer exists."""
    require_admin_auth(request)
    if client_id in manager.active_agents:
        ws = manager.active_agents.get(client_id)
        if ws:
            try:
                await ws.close(code=1000, reason="Client removed by admin")
            except Exception:
                pass
        del manager.active_agents[client_id]

    if client_id in manager.clients_cache:
        del manager.clients_cache[client_id]
    if client_id in manager.client_thumbnails:
        del manager.client_thumbnails[client_id]
    if client_id in manager.client_processes:
        del manager.client_processes[client_id]

    try:
        conn = get_db_connection()
        cursor = conn.cursor()
        cursor.execute("DELETE FROM clients WHERE client_id = ?", (client_id,))
        cursor.execute("DELETE FROM telemetry_history WHERE client_id = ?", (client_id,))
        conn.commit()
        conn.close()
    except Exception as e:
        print(f"[DB Error] delete client: {e}")

    await manager.broadcast_to_admins({
        "type": "client_deleted",
        "client_id": client_id,
        "stats": manager.compute_stats()
    })
    log_audit("CLIENT_DELETE", client_id, f"Komputer {client_id} dihapus dari dashboard.", "SUCCESS")
    return {"status": "ok", "message": f"Komputer {client_id} berhasil dihapus dari daftar."}


@app.post("/api/client/{client_id}/upgrade")
async def upgrade_client_agent(client_id: str, request: Request):
    """Sends a silent auto-update command to client to download latest Client_Siswa_Web.exe and restart."""
    require_admin_auth(request)
    if client_id not in manager.clients_cache:
        raise HTTPException(status_code=404, detail="Client not found")
    
    lan_ip = get_lan_ip()
    host_header = request.headers.get("host", f"{lan_ip}:{PORT}")
    if "localhost" in host_header or "127.0.0.1" in host_header:
        host_header = f"{lan_ip}:{PORT}"
    prefix = request.headers.get("x-forwarded-prefix", "").rstrip("/")
    server_http = f"http://{host_header}{prefix}"
    
    update_cmd = (
        f'cmd.exe /c "timeout /t 1 /nobreak >nul & '
        f'if not exist \"%TEMP%\\LabGuard\" mkdir \"%TEMP%\\LabGuard\" & '
        f'curl -fSL \"{server_http}/download/agent\" -o \"%TEMP%\\LabGuard\\Client_Siswa_Web.exe\" & '
        f'taskkill /f /im Client_Siswa_Web.exe >nul 2>&1 & '
        f'start \"\" \"%TEMP%\\LabGuard\\Client_Siswa_Web.exe\" --silent"'
    )
    
    res = await manager.send_command_to_clients(client_id, "launch_app", {"app": update_cmd})
    log_audit("AGENT_UPGRADE", client_id, f"Perintah perbarui agen versi terbaru dikirim ke {client_id}")
    return {"status": "ok", "message": f"Perintah perbarui agen versi terbaru telah dikirim ke {client_id}"}


@app.post("/api/agents/upgrade-all")
async def upgrade_all_agents(request: Request):
    """Broadcasts silent auto-update command to ALL online student PCs."""
    require_admin_auth(request)
    lan_ip = get_lan_ip()
    host_header = request.headers.get("host", f"{lan_ip}:{PORT}")
    if "localhost" in host_header or "127.0.0.1" in host_header:
        host_header = f"{lan_ip}:{PORT}"
    prefix = request.headers.get("x-forwarded-prefix", "").rstrip("/")
    server_http = f"http://{host_header}{prefix}"
    update_cmd = (
        f'cmd.exe /c "timeout /t 1 /nobreak >nul & '
        f'if not exist \"%TEMP%\\LabGuard\" mkdir \"%TEMP%\\LabGuard\" & '
        f'curl -fSL \"{server_http}/download/agent\" -o \"%TEMP%\\LabGuard\\Client_Siswa_Web.exe\" & '
        f'taskkill /f /im Client_Siswa_Web.exe >nul 2>&1 & '
        f'start \"\" \"%TEMP%\\LabGuard\\Client_Siswa_Web.exe\" --silent"'
    )
    res = await manager.send_command_to_clients("online", "launch_app", {"app": update_cmd})
    log_audit("AGENT_UPGRADE_ALL", "all", f"Broadcast perbarui agen ke {res.get('sent_count', 0)} PC online")
    return {"status": "ok", "sent_count": res.get("sent_count", 0), "message": f"Perintah perbarui agen dikirim ke {res.get('sent_count', 0)} PC online"}


@app.post("/api/client/{client_id}/toggle-teacher")
async def toggle_teacher(client_id: str, request: Request):
    """Toggles teacher / instructor workstation role for a PC."""
    require_admin_auth(request)
    client_data = manager.clients_cache.get(client_id)
    new_status = True
    if client_data:
        new_status = not bool(client_data.get("is_teacher", False))
        client_data["is_teacher"] = new_status
    
    try:
        conn = get_db_connection()
        cursor = conn.cursor()
        cursor.execute("UPDATE clients SET is_teacher = ? WHERE client_id = ?", (1 if new_status else 0, client_id))
        conn.commit()
        conn.close()
    except Exception as e:
        print(f"[DB Error] toggle teacher: {e}")

    await manager.broadcast_to_admins({
        "type": "client_updated",
        "client": client_data or {"client_id": client_id, "is_teacher": new_status}
    })

    return {"status": "ok", "client_id": client_id, "is_teacher": new_status}


@app.post("/api/client/{client_id}/rename")
async def rename_client(client_id: str, request: Request):
    """Renames a client PC (sets custom lab display name, and optionally renames Windows hostname)."""
    require_admin_auth(request)
    if client_id not in manager.clients_cache:
        raise HTTPException(status_code=404, detail="Komputer tidak ditemukan dalam daftar.")

    body = await request.json()
    new_name = str(body.get("custom_name", "")).strip()
    rename_windows = bool(body.get("rename_windows", False))

    c = manager.clients_cache[client_id]
    orig_hostname = c.get("hostname", client_id)

    # Update in memory
    c["custom_name"] = new_name
    default_name = client_id if client_id != orig_hostname else orig_hostname
    c["display_name"] = new_name if new_name else default_name

    # Persist to SQLite DB
    try:
        conn = get_db_connection()
        cursor = conn.cursor()
        cursor.execute("UPDATE clients SET custom_name = ? WHERE client_id = ?", (new_name, client_id))
        conn.commit()
        conn.close()
    except Exception as e:
        print(f"[DB Error] rename_client: {e}")

    # Optional Windows Hostname Rename via PowerShell if requested and client online
    windows_cmd_sent = False
    if rename_windows and new_name and client_id in manager.active_agents:
        import re
        safe_win_name = re.sub(r'[^a-zA-Z0-9-]', '', new_name)[:15]
        if safe_win_name:
            ps_rename = f'powershell -Command "Rename-Computer -NewName \'{safe_win_name}\' -Force"'
            await manager.send_command_to_clients(client_id, "launch_app", {"app": ps_rename})
            windows_cmd_sent = True

    # Broadcast update to all teacher dashboards via WebSocket
    await manager.broadcast_to_admins({
        "type": "client_updated",
        "client": c
    })

    log_msg = f"Nama komputer '{orig_hostname}' diubah menjadi '{c['display_name']}'"
    if windows_cmd_sent:
        log_msg += " (Perintah ganti nama Windows dikirim ke agen)"
    log_audit("RENAME_PC", client_id, log_msg)

    return {
        "status": "ok",
        "client_id": client_id,
        "custom_name": new_name,
        "display_name": c["display_name"],
        "windows_cmd_sent": windows_cmd_sent,
        "message": f"Nama komputer berhasil diubah menjadi '{c['display_name']}'."
    }


@app.post("/api/report")
async def receive_report(request: Request):
    data = await request.body()
    text = data.decode("utf-8", errors="ignore")
    print(f"[CLIENT REPORT]: {text}")
    log_audit("DIAGNOSIS", "report", text, "INFO")
    return {"status": "ok"}


@app.post("/api/command")
async def post_command(request: Request):
    require_admin_auth(request)
    body = await request.json()
    target = body.get("target", "all")
    action = body.get("action")
    payload = body.get("payload", {})
    if not action:
        raise HTTPException(status_code=400, detail="Action is required")
    res = await manager.send_command_to_clients(target, action, payload)
    return res


@app.get("/api/logs")
async def get_logs(request: Request, limit: int = 50):
    require_admin_auth(request)
    return {"logs": get_recent_logs(limit)}


@app.post("/api/logs/clear")
async def clear_logs(request: Request):
    require_admin_auth(request)
    try:
        conn = get_db_connection()
        cursor = conn.cursor()
        cursor.execute("DELETE FROM audit_logs")
        conn.commit()
        conn.close()
        return {"status": "ok", "message": "Audit logs cleared"}
    except Exception as e:
        raise HTTPException(status_code=500, detail=str(e))


# --- Virtual Lab Simulation Controls ---
simulator_process: Optional[subprocess.Popen] = None

@app.post("/api/simulation/toggle")
async def toggle_simulation(request: Request):
    """Starts or stops 12 virtual student PCs for testing and live demonstration."""
    require_admin_auth(request)
    global simulator_process
    if simulator_process and simulator_process.poll() is None:
        simulator_process.terminate()
        simulator_process = None
        log_audit("SIMULATION_STOP", "all", "Simulasi PC lab dihentikan", "SUCCESS")
        return {"running": False, "message": "Simulasi dihentikan"}
    else:
        sim_script = os.path.join(os.path.dirname(__file__), "simulate_lab_clients.py")
        creationflags = 0
        if platform.system() == "Windows":
            creationflags = getattr(subprocess, "CREATE_NO_WINDOW", 0x08000000)
        simulator_process = subprocess.Popen(
            [sys.executable, sim_script, "12", f"ws://127.0.0.1:{PORT}/ws/agent"],
            creationflags=creationflags
        )
        log_audit("SIMULATION_START", "all", "Simulasi 12 PC siswa virtual dijalankan", "SUCCESS")
        return {"running": True, "message": "Simulasi 12 PC siswa virtual aktif"}


@app.get("/api/simulation/status")
async def get_simulation_status():
    """Returns whether the virtual lab client simulator is actively running."""
    global simulator_process
    is_running = simulator_process is not None and simulator_process.poll() is None
    return {"running": is_running}


@app.get("/api/simulation/wake_check/{client_id}")
async def check_sim_wake(client_id: str):
    """Checks whether Wake-on-LAN was triggered for this virtual simulated PC."""
    is_woken = (
        "all" in manager.woken_clients or
        client_id in manager.woken_clients or
        (time.time() - manager.last_sim_wake_time < 30.0)
    )
    if client_id in manager.woken_clients:
        manager.woken_clients.discard(client_id)
    return {"wake": is_woken}


# --- Seating Plan (Denah Ruang Lab) Endpoints ---
@app.get("/api/seating-plan")
async def get_seating_plan(room: str = "all"):
    """Returns saved seating layout coordinates for the specified lab room."""
    try:
        conn = get_db_connection()
        cursor = conn.cursor()
        cursor.execute("SELECT layout_json FROM seating_plans WHERE room = ?", (room,))
        row = cursor.fetchone()
        conn.close()
        if row and row[0]:
            return {"room": room, "layout": json.loads(row[0])}
        return {"room": room, "layout": {}}
    except Exception as e:
        return {"room": room, "layout": {}, "error": str(e)}


@app.post("/api/seating-plan")
async def save_seating_plan(request: Request):
    """Saves seating layout coordinates for a lab room."""
    require_admin_auth(request)
    try:
        body = await request.json()
        room = str(body.get("room", "all")).strip() or "all"
        layout = body.get("layout", {})
        now_str = datetime.datetime.now().strftime("%Y-%m-%d %H:%M:%S")
        conn = get_db_connection()
        cursor = conn.cursor()
        cursor.execute(
            "INSERT INTO seating_plans (room, layout_json, updated_at) VALUES (?, ?, ?) "
            "ON CONFLICT(room) DO UPDATE SET layout_json = excluded.layout_json, updated_at = excluded.updated_at",
            (room, json.dumps(layout), now_str)
        )
        conn.commit()
        conn.close()
        return {"status": "ok", "message": f"Denah tata letak ruang '{room}' berhasil disimpan", "room": room}
    except Exception as e:
        raise HTTPException(status_code=500, detail=str(e))


# --- Client Agent Standalone EXE Download & Installer Endpoints ---
@app.get("/api/server-info")
async def get_server_info(request: Request):
    """Returns local LAN host information, server IP, EXE availability status, and version metadata."""
    lan_ip = get_lan_ip()
    host_header = request.headers.get("host", f"{lan_ip}:{PORT}")
    if "localhost" in host_header or "127.0.0.1" in host_header:
        host_header = f"{lan_ip}:{PORT}"
    
    prefix = request.headers.get("x-forwarded-prefix", "").rstrip("/")
    base_http = f"http://{host_header}{prefix}"
    base_ws = f"ws://{host_header}{prefix}"

    dist_dir = os.path.join(os.path.dirname(__file__), "dist")
    exe_path = os.path.join(dist_dir, "Client_Siswa_Web.exe")
    has_exe = os.path.exists(exe_path)
    exe_size_mb = round(os.path.getsize(exe_path) / (1024 * 1024), 1) if has_exe else 0
    exe_mtime_str = ""
    if has_exe:
        try:
            mtime = os.path.getmtime(exe_path)
            exe_mtime_str = datetime.datetime.fromtimestamp(mtime).strftime("%d %b %Y, %H:%M WIB")
        except Exception:
            pass

    return {
        "lan_ip": lan_ip,
        "port": PORT,
        "server_url": base_http,
        "ws_url": f"{base_ws}/ws/agent",
        "has_exe": has_exe,
        "exe_size_mb": exe_size_mb,
        "exe_last_modified": exe_mtime_str,
        "agent_version": f"v{AGENT_VERSION}",
        "agent_filename": f"Client_Siswa_Web_v{AGENT_VERSION}.exe",
        "agent_release_date": AGENT_RELEASE_DATE,
        "changelog": AGENT_CHANGELOG,
        "download_url": f"{base_http}/download/Client_Siswa_Web_v{AGENT_VERSION}.exe",
        "batch_url": f"{base_http}/download/pasang.bat"
    }


@app.post("/api/files/distribute")
async def distribute_file_to_clients(
    request: Request,
    file: UploadFile = File(...),
    target: str = Form("all"),
    destination: str = Form("desktop"),
    auto_open: bool = Form(False),
    show_popup: bool = Form(True)
):
    """
    Receives an uploaded file from the teacher dashboard and dispatches it
    to online student PCs with auto-download and optional auto-open.
    """
    require_admin_auth(request)
    if not file.filename:
        raise HTTPException(status_code=400, detail="Nama file tidak valid")

    import re
    import uuid
    safe_filename = os.path.basename(file.filename)
    unique_prefix = uuid.uuid4().hex[:8]
    stored_name = f"{unique_prefix}_{safe_filename}"
    target_path = os.path.join(DISTRIBUTED_FILES_DIR, stored_name)

    file_size = 0
    with open(target_path, "wb") as f_out:
        while chunk := await file.read(1024 * 1024):
            f_out.write(chunk)
            file_size += len(chunk)

    lan_ip = get_lan_ip()
    host_header = request.headers.get("host", f"{lan_ip}:{PORT}")
    if "localhost" in host_header or "127.0.0.1" in host_header:
        host_header = f"{lan_ip}:{PORT}"
    download_url = f"http://{host_header}/download/distributed/{urllib.parse.quote(stored_name)}"

    # Target resolution
    target_param: Any = target
    if target not in ("all", "online") and not target.startswith("room:"):
        if "," in target:
            target_param = [t.strip() for t in target.split(",") if t.strip()]
        else:
            target_param = [target.strip()]

    payload = {
        "url": download_url,
        "filename": safe_filename,
        "file_size": file_size,
        "destination": destination,
        "auto_open": auto_open,
        "show_popup": show_popup
    }

    result = await manager.send_command_to_clients(target_param, "send_file", payload)

    target_display = target if isinstance(target, str) else f"{len(target_param)} PCs"
    log_audit(
        "DISTRIBUTE_FILE",
        str(target_display),
        f"Kirim berkas '{safe_filename}' ({round(file_size/1024, 1)} KB) ke {result.get('sent_count', 0)} PC. Lokasi: {destination}, Buka: {auto_open}",
        "SUCCESS" if result.get("sent_count", 0) > 0 else "NO_TARGETS"
    )

    return {
        "status": "ok",
        "filename": safe_filename,
        "file_size": file_size,
        "sent_count": result.get("sent_count", 0),
        "targets": result.get("targets", []),
        "download_url": download_url
    }


@app.get("/download/distributed/{stored_filename}")
async def get_distributed_file(stored_filename: str):
    """Serves the distributed file to student PCs."""
    safe_name = os.path.basename(stored_filename)
    file_path = os.path.join(DISTRIBUTED_FILES_DIR, safe_name)
    if not os.path.exists(file_path):
        raise HTTPException(status_code=404, detail="Berkas tidak ditemukan di server")

    parts = safe_name.split("_", 1)
    original_name = parts[1] if len(parts) > 1 else safe_name
    return FileResponse(
        file_path,
        filename=original_name,
        media_type="application/octet-stream"
    )


@app.get("/download/agent")
@app.head("/download/agent")
@app.get("/download/Client_Siswa_Web.exe")
@app.head("/download/Client_Siswa_Web.exe")
@app.get("/download/Client_Siswa_Web_v{ver}.exe")
@app.head("/download/Client_Siswa_Web_v{ver}.exe")
async def download_client_agent(ver: Optional[str] = None):
    """Serves the compiled standalone Client_Siswa_Web.exe file for direct download with versioned filename."""
    dist_dir = os.path.join(os.path.dirname(__file__), "dist")
    clean_ver = (ver or AGENT_VERSION).strip().lstrip("v")
    versioned_filename = f"Client_Siswa_Web_v{clean_ver}.exe"

    v_path = os.path.join(dist_dir, versioned_filename)
    default_path = os.path.join(dist_dir, "Client_Siswa_Web.exe")

    exe_path = v_path if os.path.exists(v_path) else default_path
    if not os.path.exists(exe_path):
        raise HTTPException(
            status_code=404,
            detail="Berkas Client_Siswa_Web.exe belum dibuat di folder dist. Silakan jalankan BUAT_EXE_SISWA_WEB.bat di server."
        )
    return FileResponse(
        exe_path,
        media_type="application/vnd.microsoft.portable-executable",
        filename=versioned_filename,
        headers={"Content-Disposition": f'attachment; filename="{versioned_filename}"'}
    )


@app.get("/download/pasang.bat")
async def download_quick_batch(request: Request):
    """Generates and serves a 1-click batch installer script configured with current server IP."""
    lan_ip = get_lan_ip()
    host_header = request.headers.get("host", f"{lan_ip}:{PORT}")
    if "localhost" in host_header or "127.0.0.1" in host_header:
        host_header = f"{lan_ip}:{PORT}"
    prefix = request.headers.get("x-forwarded-prefix", "").rstrip("/")
    server_http = f"http://{host_header}{prefix}"

    bat_content = f"""@echo off
title Pemasang Otomatis Client Siswa LabGuard
color 0B
echo ========================================================
echo   PEMASANG OTOMATIS AGEN PC SISWA - LABGUARD PRO
echo ========================================================
echo Server Guru: {server_http}
echo.
set "INSTALL_DIR=%LOCALAPPDATA%\\LabGuardClient"
set "EXE_PATH=%INSTALL_DIR%\\Client_Siswa_Web.exe"
set "CONFIG_PATH=%INSTALL_DIR%\\agent_config.json"
set "STARTUP_DIR=%APPDATA%\\Microsoft\\Windows\\Start Menu\\Programs\\Startup"

if not exist "%INSTALL_DIR%" mkdir "%INSTALL_DIR%"

echo [1/3] Mengunduh Client_Siswa_Web.exe dari server guru...
where curl >nul 2>&1
if %errorlevel% equ 0 (
    curl -fSL "{server_http}/download/agent" -o "%EXE_PATH%"
) else (
    certutil -urlcache -split -f "{server_http}/download/agent" "%EXE_PATH%" >nul 2>&1
)

if not exist "%EXE_PATH%" (
    color 0C
    echo [ERROR] Gagal mengunduh file Client_Siswa_Web.exe dari server {server_http}
    echo Pastikan komputer siswa terhubung ke jaringan yang sama dengan guru.
    pause
    exit /b 1
)

echo [2/3] Mendaftarkan ke Startup Windows...
echo {{^"server_url^": ^"{server_http}^", ^"lab_room^": ^"Lab RPL 1^", ^"auto_startup^": true}} > "%CONFIG_PATH%"
reg add "HKCU\\Software\\Microsoft\\Windows\\CurrentVersion\\Run" /v "LabGuardClient" /t REG_SZ /d "\"%EXE_PATH%\" --silent \"{server_http}\"" /f >nul 2>&1

:: Buat shortcut .lnk menggunakan script VBS bawaan Windows (tanpa PowerShell)
echo Set oWS = WScript.CreateObject("WScript.Shell") > "%TEMP%\\make_shortcut.vbs"
echo sLinkFile = "%STARTUP_DIR%\\LabGuard_Client.lnk" >> "%TEMP%\\make_shortcut.vbs"
echo Set oLink = oWS.CreateShortcut(sLinkFile) >> "%TEMP%\\make_shortcut.vbs"
echo oLink.TargetPath = "%EXE_PATH%" >> "%TEMP%\\make_shortcut.vbs"
echo oLink.Arguments = "--silent " ^& chr(34) ^& "{server_http}" ^& chr(34) >> "%TEMP%\\make_shortcut.vbs"
echo oLink.WorkingDirectory = "%INSTALL_DIR%" >> "%TEMP%\\make_shortcut.vbs"
echo oLink.Description = "LabGuard Pro Client Agent Siswa" >> "%TEMP%\\make_shortcut.vbs"
echo oLink.Save >> "%TEMP%\\make_shortcut.vbs"
cscript //nologo "%TEMP%\\make_shortcut.vbs" >nul 2>&1
del "%TEMP%\\make_shortcut.vbs" >nul 2>&1

echo [3/3] Menjalankan Agen Siswa di latar belakang (background)...
taskkill /f /im Client_Siswa_Web.exe >nul 2>&1
start "" "%EXE_PATH%" --silent "{server_http}"

color 0A
echo.
echo ========================================================
echo [BERHASIL] Agen Siswa telah terpasang dan aktif!
echo Agen kini berjalan di latar belakang (background)
echo dan otomatis aktif setiap kali Windows dinyalakan.
echo ========================================================
timeout /t 5
"""
    return Response(
        content=bat_content,
        media_type="text/plain",
        headers={"Content-Disposition": 'attachment; filename="Pasang_Siswa_Otomatis.bat"'}
    )


# --- Authentication API Endpoints ---
@app.post("/api/auth/login")
async def api_login(request: Request):
    try:
        body = await request.json()
    except Exception:
        raise HTTPException(status_code=400, detail="Format JSON tidak valid")

    username = str(body.get("username", "")).strip()
    password = str(body.get("password", "")).strip()
    remember_me = bool(body.get("remember_me", True))

    if not username or not password:
        raise HTTPException(status_code=400, detail="Username dan kata sandi wajib diisi!")

    client_ip = request.client.host if request.client else "127.0.0.1"

    if not verify_credentials(username, password):
        log_audit("LOGIN_FAIL", username, f"Percobaan login gagal untuk '{username}' dari IP {client_ip}", "WARNING")
        raise HTTPException(status_code=401, detail="Username atau kata sandi tidak sesuai!")

    token = create_session(username, client_ip, remember_me)
    log_audit("LOGIN_SUCCESS", username, f"Pengajar '{username}' berhasil masuk dari IP {client_ip}.", "SUCCESS")

    max_age = 30 * 86400 if remember_me else 86400
    response = JSONResponse(content={
        "ok": True,
        "token": token,
        "username": username,
        "display_name": "Guru / IT Support"
    })
    response.set_cookie(
        key="labguard_token",
        value=token,
        max_age=max_age,
        path="/",
        httponly=False,
        samesite="lax"
    )
    return response


@app.get("/api/auth/status")
async def api_auth_status(request: Request):
    auth = is_authenticated(request)
    client_ip = request.client.host if request.client else "127.0.0.1"
    server_lan_ip = get_lan_ip()
    is_local = client_ip in ("127.0.0.1", "::1", "localhost", server_lan_ip)
    
    token = request.cookies.get("labguard_token") or ""
    return {
        "authenticated": auth,
        "is_localhost": is_local,
        "token": token if auth else ""
    }


@app.post("/api/auth/logout")
async def api_logout(request: Request):
    token = request.cookies.get("labguard_token")
    if not token:
        auth_hdr = request.headers.get("Authorization", "")
        if auth_hdr.startswith("Bearer "):
            token = auth_hdr[7:].strip()
    if token:
        delete_session(token)
    
    # If request is from local server PC, clear all active sessions to guarantee complete logout
    client_ip = request.client.host if request.client else "127.0.0.1"
    server_lan_ip = get_lan_ip()
    if client_ip in ("127.0.0.1", "::1", "localhost", server_lan_ip):
        ACTIVE_SESSIONS.clear()
        try:
            conn = get_db_connection()
            c = conn.cursor()
            c.execute("DELETE FROM sessions")
            conn.commit()
            conn.close()
        except Exception:
            pass

    response = JSONResponse(content={"ok": True})
    response.delete_cookie(key="labguard_token", path="/")
    return response


@app.post("/api/auth/change-password")
async def api_change_password(request: Request):
    if not is_authenticated(request):
        raise HTTPException(status_code=401, detail="Akses ditolak. Silakan login terlebih dahulu.")
    try:
        body = await request.json()
    except Exception:
        raise HTTPException(status_code=400, detail="Format JSON tidak valid")

    old_pwd = str(body.get("old_password", "")).strip()
    new_pwd = str(body.get("new_password", "")).strip()

    if not new_pwd or len(new_pwd) < 6:
        raise HTTPException(status_code=400, detail="Kata sandi baru minimal 6 karakter!")

    conn = get_db_connection()
    c = conn.cursor()
    c.execute("SELECT value FROM system_settings WHERE key = 'admin_password'")
    row = c.fetchone()
    stored_pwd = row[0] if row else "petra123"

    if old_pwd != stored_pwd and hash_password(old_pwd) != stored_pwd:
        conn.close()
        raise HTTPException(status_code=400, detail="Kata sandi lama tidak sesuai!")

    new_hash = hash_password(new_pwd)
    c.execute("INSERT OR REPLACE INTO system_settings (key, value) VALUES ('admin_password', ?)", (new_hash,))
    
    # Invalidate all other sessions for security
    current_token = request.cookies.get("labguard_token") or ""
    if not current_token:
        auth_hdr = request.headers.get("Authorization", "")
        if auth_hdr.startswith("Bearer "):
            current_token = auth_hdr[7:].strip()
    if not current_token:
        current_token = request.query_params.get("token") or ""

    c.execute("DELETE FROM sessions WHERE token != ?", (current_token,))
    conn.commit()
    conn.close()

    for tok in list(ACTIVE_SESSIONS.keys()):
        if tok != current_token:
            ACTIVE_SESSIONS.pop(tok, None)

    log_audit("CHANGE_PASSWORD", "admin", "Kata sandi akun Guru berhasil diperbarui.", "SUCCESS")
    return {"ok": True, "message": "Kata sandi berhasil diubah!"}


# --- Web Dashboard Static Assets & Pages ---
class NoCacheStaticFiles(StaticFiles):
    async def get_response(self, path: str, scope):
        response = await super().get_response(path, scope)
        response.headers["Cache-Control"] = "no-cache, must-revalidate"
        response.headers["Pragma"] = "no-cache"
        response.headers["Expires"] = "0"
        return response

if os.path.exists(WEB_DIR):
    app.mount("/static", NoCacheStaticFiles(directory=WEB_DIR), name="static")


@app.get("/login")
async def serve_login(request: Request):
    if is_authenticated(request):
        prefix = request.headers.get("x-forwarded-prefix", "").rstrip("/")
        index_url = f"{prefix}/" if prefix else "/"
        return RedirectResponse(url=index_url, status_code=302)
    login_path = os.path.join(WEB_DIR, "login.html")
    if os.path.exists(login_path):
        return FileResponse(
            login_path,
            headers={
                "Cache-Control": "no-cache, no-store, must-revalidate",
                "Pragma": "no-cache",
                "Expires": "0"
            }
        )
    return HTMLResponse("<h1>Login Page Not Found</h1>")


@app.get("/")
async def serve_index(request: Request):
    if not is_authenticated(request):
        prefix = request.headers.get("x-forwarded-prefix", "").rstrip("/")
        login_url = f"{prefix}/login" if prefix else "/login"
        return RedirectResponse(url=login_url, status_code=302)
    index_path = os.path.join(WEB_DIR, "index.html")
    if os.path.exists(index_path):
        resp = FileResponse(
            index_path,
            headers={
                "Cache-Control": "no-cache, no-store, must-revalidate",
                "Pragma": "no-cache",
                "Expires": "0"
            }
        )
        # Ensure cookie is synced for current origin (e.g. 10.161.17.165)
        now = time.time()
        for tok, sess in list(ACTIVE_SESSIONS.items()):
            if sess.get("expires_at", 0) > now:
                if not request.cookies.get("labguard_token"):
                    resp.set_cookie(
                        key="labguard_token",
                        value=tok,
                        max_age=30 * 86400,
                        path="/",
                        httponly=False,
                        samesite="lax"
                    )
                break
        return resp
    return HTMLResponse("<h1>Lab Monitoring Web Dashboard</h1><p>Web frontend folder not found.</p>")


if __name__ == "__main__":
    if sys.stdout is None or sys.stderr is None:
        import os
        try:
            devnull = open(os.devnull, "w", encoding="utf-8")
            if sys.stdout is None:
                sys.stdout = devnull
            if sys.stderr is None:
                sys.stderr = devnull
        except Exception:
            pass

    lan_ip = get_lan_ip()
    try:
        print(f"Starting Server: http://{lan_ip}:{PORT} (LAN) / http://localhost:{PORT} (Local)")
    except Exception:
        pass
    uvicorn.run("server_web:app", host=HOST, port=PORT, reload=False, log_level="info")
