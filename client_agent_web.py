"""
client_agent_web.py - Modern Python Client Agent for Computer Lab Monitoring
Features:
- Connects to FastAPI WebSocket Server (/ws/agent)
- Real-time telemetry via psutil (CPU %, RAM %, Disk %, Active Window / App)
- Live screen thumbnail compression & streaming via Pillow (JPEG base64)
- Top process inspection list
- Remote Commands:
    * lock: Fullscreen topmost lock overlay (anti-distraction)
    * unlock: Dismiss lock screen
    * broadcast: Show instructional / urgent notification modal
    * launch_app: Remotely launch application or URL
    * kill_process: Terminate process by PID or name
    * shutdown / restart: Safe OS power controls
- Automatic reconnect with exponential backoff
"""

import asyncio
import base64
import ctypes
import io
import json
import os
import platform
import socket
import subprocess
import sys
import threading
import time
from typing import Any, Dict, List, Optional

try:
    import psutil
except ImportError:
    print("[ERROR] psutil not installed. Run: pip install psutil")
    sys.exit(1)

try:
    from PIL import Image, ImageGrab
except ImportError:
    print("[ERROR] Pillow not installed. Run: pip install Pillow")
    sys.exit(1)

try:
    import websockets
except ImportError:
    print("[ERROR] websockets not installed. Run: pip install websockets")
    sys.exit(1)

import tkinter as tk
from tkinter import ttk

# Enable Per-Monitor DPI Awareness on Windows for pixel-perfect coordinates & screen grabbing
if platform.system() == "Windows":
    try:
        ctypes.windll.shcore.SetProcessDpiAwareness(2)  # PROCESS_PER_MONITOR_DPI_AWARE
    except Exception:
        try:
            ctypes.windll.user32.SetProcessDPIAware()
        except Exception:
            pass


# --- Configuration & Version ---
AGENT_VERSION = "2.4.0"
AGENT_RELEASE_DATE = "11 November 2026"
DEFAULT_SERVER_URL = "ws://10.161.17.165:8000/ws/agent"
DEFAULT_LAB_ROOM = "Lab RPL 1"
TELEMETRY_INTERVAL = 1.5           # Telemetry interval (seconds)
SCREENSHOT_INTERVAL_ACTIVE = 1.0   # Adaptive: Active typing/mouse/games (1.0s)
SCREENSHOT_INTERVAL_IDLE = 4.5     # Adaptive: Idle reading/stationary screen (4.5s)
FORCED_REFRESH_INTERVAL = 6.0      # Maximum interval between frames even if 100% static
USER_IDLE_THRESHOLD_SEC = 2.5      # Seconds without mouse/keyboard before switching to idle
DIFF_THRESHOLD_PCT = 1.2           # Fingerprint difference threshold (%) to detect screen change
THUMBNAIL_SIZE = (380, 220)        # Optimized for bandwidth & crisp grid cards
JPEG_QUALITY = 55                  # Very light (~15-20 KB per frame)


# --- Platform Helper: Active Window Detection ---
def get_active_window_info() -> Dict[str, str]:
    """Returns foreground window title and process name (Windows/Linux fallback)."""
    if platform.system() == "Windows":
        try:
            hwnd = ctypes.windll.user32.GetForegroundWindow()
            if not hwnd:
                return {"window_title": "Desktop", "process_name": "explorer.exe"}
            
            # Get Window Title
            length = ctypes.windll.user32.GetWindowTextLengthW(hwnd)
            buf = ctypes.create_unicode_buffer(length + 1)
            ctypes.windll.user32.GetWindowTextW(hwnd, buf, length + 1)
            title = buf.value.strip() or "Desktop"

            # Get PID and Process Name
            pid = ctypes.c_ulong()
            ctypes.windll.user32.GetWindowThreadProcessId(hwnd, ctypes.byref(pid))
            proc_name = "System"
            if pid.value:
                try:
                    p = psutil.Process(pid.value)
                    proc_name = p.name()
                except Exception:
                    pass

            return {"window_title": title, "process_name": proc_name}
        except Exception:
            return {"window_title": "Desktop", "process_name": "System"}
    else:
        return {"window_title": "Linux Desktop", "process_name": "Desktop"}


def get_top_processes(limit: int = 8) -> List[Dict[str, any]]:
    """Returns top CPU-consuming running processes for remote inspection."""
    procs = []
    try:
        for p in psutil.process_iter(['pid', 'name', 'cpu_percent', 'memory_info']):
            try:
                mem_mb = round(p.info['memory_info'].rss / (1024 * 1024), 1) if p.info.get('memory_info') else 0
                procs.append({
                    "pid": p.info['pid'],
                    "name": p.info['name'] or "Unknown",
                    "cpu": round(p.info.get('cpu_percent') or 0, 1),
                    "memory_mb": mem_mb
                })
            except (psutil.NoSuchProcess, psutil.AccessDenied):
                continue
        # Sort by memory descending
        procs.sort(key=lambda x: x["memory_mb"], reverse=True)
        return procs[:limit]
    except Exception:
        return []


def get_user_idle_seconds() -> float:
    """Returns elapsed seconds since last user keyboard or mouse input on Windows. Returns 0.0 on error."""
    if platform.system() == "Windows":
        try:
            class LASTINPUTINFO(ctypes.Structure):
                _fields_ = [("cbSize", ctypes.c_uint), ("dwTime", ctypes.c_uint)]
            lii = LASTINPUTINFO()
            lii.cbSize = ctypes.sizeof(LASTINPUTINFO)
            if ctypes.windll.user32.GetLastInputInfo(ctypes.byref(lii)):
                millis = ctypes.windll.kernel32.GetTickCount() - lii.dwTime
                return max(0.0, millis / 1000.0)
        except Exception:
            pass
    return 0.0


def compute_screen_fingerprint(image) -> bytes:
    """Downscales image to 16x16 grayscale for ultra-fast, zero-overhead difference detection (256 bytes)."""
    try:
        return image.resize((16, 16), Image.Resampling.BILINEAR).convert("L").tobytes()
    except Exception:
        return b""


def calculate_fingerprint_diff(fp1: bytes, fp2: bytes) -> float:
    """Calculates mean absolute difference percentage between two 16x16 fingerprints."""
    if not fp1 or not fp2 or len(fp1) != 256 or len(fp2) != 256:
        return 100.0
    total_diff = sum(abs(a - b) for a, b in zip(fp1, fp2))
    return (total_diff / (256.0 * 255.0)) * 100.0


def capture_adaptive_thumbnail(last_fingerprint: Optional[bytes] = None, force_encode: bool = False) -> Tuple[Optional[str], bytes, float, bool]:
    """
    Captures desktop screen, computes ultra-light 16x16 perceptual fingerprint (256 bytes).
    Only performs JPEG compression if the screen has actually changed (> DIFF_THRESHOLD_PCT) or force_encode is True.
    Returns: (base64_jpeg_or_None, new_fingerprint, diff_percentage, is_changed)
    """
    try:
        shot = ImageGrab.grab()
        shot.thumbnail(THUMBNAIL_SIZE, Image.Resampling.BILINEAR)

        # 16x16 downscaled grayscale fingerprint (256 bytes)
        new_fp = compute_screen_fingerprint(shot)

        diff_pct = 100.0
        if last_fingerprint and len(last_fingerprint) == 256 and len(new_fp) == 256:
            diff_pct = calculate_fingerprint_diff(new_fp, last_fingerprint)

        is_changed = (diff_pct >= DIFF_THRESHOLD_PCT)

        # If screen hasn't changed materially and force_encode is False, skip JPEG compression to save CPU & bandwidth!
        if not is_changed and not force_encode:
            return None, new_fp, diff_pct, False

        # Convert RGBA to RGB if needed
        if shot.mode != "RGB":
            shot = shot.convert("RGB")
        buf = io.BytesIO()
        shot.save(buf, format="JPEG", quality=JPEG_QUALITY, optimize=False)
        buf.seek(0)
        b64_str = base64.b64encode(buf.read()).decode("ascii")
        return b64_str, new_fp, diff_pct, True
    except Exception:
        return None, (last_fingerprint or b""), 0.0, False


def capture_screen_thumbnail() -> Optional[str]:
    """Captures desktop screen, resizes, compresses as JPEG, returns base64."""
    b64_str, _, _, _ = capture_adaptive_thumbnail(force_encode=True)
    return b64_str


def capture_remote_frame(quality: int = 55, max_width: int = 1280) -> Optional[str]:
    """Captures desktop screen at higher resolution with cursor drawn for interactive remote control."""
    try:
        shot = ImageGrab.grab()
        orig_w, orig_h = shot.size
        
        # Draw hardware cursor onto the frame so teacher sees student's cursor in real-time
        if platform.system() == "Windows":
            try:
                class POINT(ctypes.Structure):
                    _fields_ = [("x", ctypes.c_long), ("y", ctypes.c_long)]
                pt = POINT()
                if ctypes.windll.user32.GetCursorPos(ctypes.byref(pt)):
                    cx, cy = pt.x, pt.y
                    if 0 <= cx < orig_w and 0 <= cy < orig_h:
                        from PIL import ImageDraw
                        draw = ImageDraw.Draw(shot)
                        # Crisp arrow cursor with black shadow/outline
                        arrow_pts = [
                            (cx, cy),
                            (cx, min(orig_h - 1, cy + 18)),
                            (min(orig_w - 1, cx + 5), min(orig_h - 1, cy + 14)),
                            (min(orig_w - 1, cx + 12), min(orig_h - 1, cy + 14))
                        ]
                        draw.polygon(arrow_pts, fill="white", outline="black")
            except Exception:
                pass

        w, h = shot.size
        if w > max_width:
            new_h = int(h * (max_width / w))
            shot = shot.resize((max_width, new_h), Image.Resampling.BILINEAR)
        if shot.mode != "RGB":
            shot = shot.convert("RGB")
        buf = io.BytesIO()
        shot.save(buf, format="JPEG", quality=quality, optimize=False)
        buf.seek(0)
        return base64.b64encode(buf.read()).decode("ascii")
    except Exception:
        return None


# Win32 Mouse Flags
MOUSEEVENTF_MOVE = 0x0001
MOUSEEVENTF_LEFTDOWN = 0x0002
MOUSEEVENTF_LEFTUP = 0x0004
MOUSEEVENTF_RIGHTDOWN = 0x0008
MOUSEEVENTF_RIGHTUP = 0x0010
MOUSEEVENTF_MIDDLEDOWN = 0x0020
MOUSEEVENTF_MIDDLEUP = 0x0040
MOUSEEVENTF_WHEEL = 0x0800
MOUSEEVENTF_ABSOLUTE = 0x8000


def execute_remote_input(event: dict):
    """Executes remote mouse and keyboard events on student PC via Windows user32 API."""
    if platform.system() != "Windows":
        return

    try:
        user32 = ctypes.windll.user32
        etype = event.get("type")
        screen_w = max(1, user32.GetSystemMetrics(0))
        screen_h = max(1, user32.GetSystemMetrics(1))

        if etype == "mouse_move":
            rx = float(event.get("rx", 0))
            ry = float(event.get("ry", 0))
            x = int(max(0.0, min(1.0, rx)) * (screen_w - 1))
            y = int(max(0.0, min(1.0, ry)) * (screen_h - 1))
            user32.SetCursorPos(x, y)
            norm_x = int(max(0.0, min(1.0, rx)) * 65535)
            norm_y = int(max(0.0, min(1.0, ry)) * 65535)
            user32.mouse_event(MOUSEEVENTF_MOVE | MOUSEEVENTF_ABSOLUTE, norm_x, norm_y, 0, 0)

        elif etype in ("mouse_down", "mouse_up", "mouse_click", "click", "dblclick"):
            rx = event.get("rx")
            ry = event.get("ry")
            norm_x, norm_y = 0, 0
            if rx is not None and ry is not None:
                rx_f = float(rx)
                ry_f = float(ry)
                x = int(max(0.0, min(1.0, rx_f)) * (screen_w - 1))
                y = int(max(0.0, min(1.0, ry_f)) * (screen_h - 1))
                user32.SetCursorPos(x, y)
                norm_x = int(max(0.0, min(1.0, rx_f)) * 65535)
                norm_y = int(max(0.0, min(1.0, ry_f)) * 65535)

            btn = event.get("button", "left").lower()
            if etype == "mouse_down":
                flag = MOUSEEVENTF_ABSOLUTE | (
                    MOUSEEVENTF_LEFTDOWN if btn == "left" else
                    MOUSEEVENTF_RIGHTDOWN if btn == "right" else
                    MOUSEEVENTF_MIDDLEDOWN
                )
                user32.mouse_event(flag, norm_x, norm_y, 0, 0)

            elif etype == "mouse_up":
                flag = MOUSEEVENTF_ABSOLUTE | (
                    MOUSEEVENTF_LEFTUP if btn == "left" else
                    MOUSEEVENTF_RIGHTUP if btn == "right" else
                    MOUSEEVENTF_MIDDLEUP
                )
                user32.mouse_event(flag, norm_x, norm_y, 0, 0)

            elif etype in ("mouse_click", "click"):
                dflag = MOUSEEVENTF_ABSOLUTE | (
                    MOUSEEVENTF_LEFTDOWN if btn == "left" else
                    MOUSEEVENTF_RIGHTDOWN if btn == "right" else
                    MOUSEEVENTF_MIDDLEDOWN
                )
                uflag = MOUSEEVENTF_ABSOLUTE | (
                    MOUSEEVENTF_LEFTUP if btn == "left" else
                    MOUSEEVENTF_RIGHTUP if btn == "right" else
                    MOUSEEVENTF_MIDDLEUP
                )
                user32.mouse_event(dflag, norm_x, norm_y, 0, 0)
                time.sleep(0.015)
                user32.mouse_event(uflag, norm_x, norm_y, 0, 0)

            elif etype == "dblclick":
                user32.mouse_event(MOUSEEVENTF_ABSOLUTE | MOUSEEVENTF_LEFTDOWN, norm_x, norm_y, 0, 0)
                user32.mouse_event(MOUSEEVENTF_ABSOLUTE | MOUSEEVENTF_LEFTUP, norm_x, norm_y, 0, 0)
                time.sleep(0.04)
                user32.mouse_event(MOUSEEVENTF_ABSOLUTE | MOUSEEVENTF_LEFTDOWN, norm_x, norm_y, 0, 0)
                user32.mouse_event(MOUSEEVENTF_ABSOLUTE | MOUSEEVENTF_LEFTUP, norm_x, norm_y, 0, 0)

        elif etype == "mouse_wheel":
            delta_y = int(event.get("delta_y", 0))
            user32.mouse_event(MOUSEEVENTF_WHEEL, 0, 0, delta_y, 0)

        elif etype in ("key_down", "key_up"):
            key = event.get("key", "")
            is_up = (etype == "key_up")
            flags = 0x0002 if is_up else 0

            VK_MAP = {
                "Enter": 0x0D, "Backspace": 0x08, "Tab": 0x09, "Escape": 0x1B,
                "Space": 0x20, " ": 0x20, "Delete": 0x2E, "Insert": 0x2D,
                "ArrowLeft": 0x25, "ArrowUp": 0x26, "ArrowRight": 0x27, "ArrowDown": 0x28,
                "Home": 0x24, "End": 0x23, "PageUp": 0x21, "PageDown": 0x22,
                "Shift": 0x10, "Control": 0x11, "Alt": 0x12, "Meta": 0x5B, "OS": 0x5B,
                "ContextMenu": 0x5D, "PrintScreen": 0x2C, "ScrollLock": 0x91, "Pause": 0x13,
                "CapsLock": 0x14, "NumLock": 0x90,
                "F1": 0x70, "F2": 0x71, "F3": 0x72, "F4": 0x73, "F5": 0x74, "F6": 0x75,
                "F7": 0x76, "F8": 0x77, "F9": 0x78, "F10": 0x79, "F11": 0x7A, "F12": 0x7B
            }

            if key in VK_MAP:
                vk = VK_MAP[key]
                user32.keybd_event(vk, 0, flags, 0)
            elif len(key) == 1:
                ch_code = ord(key)
                vk_scan = user32.VkKeyScanW(ch_code)
                if vk_scan != -1:
                    vk = vk_scan & 0xFF
                    user32.keybd_event(vk, 0, flags, 0)
                elif not is_up:
                    user32.keybd_event(0, ch_code, 0x0004, 0)
                    user32.keybd_event(0, ch_code, 0x0004 | 0x0002, 0)

        elif etype == "shortcut":
            name = event.get("name")
            if name == "win_r":
                user32.keybd_event(0x5B, 0, 0, 0)
                user32.keybd_event(0x52, 0, 0, 0)
                time.sleep(0.02)
                user32.keybd_event(0x52, 0, 0x0002, 0)
                user32.keybd_event(0x5B, 0, 0x0002, 0)
            elif name == "win_d":
                user32.keybd_event(0x5B, 0, 0, 0)
                user32.keybd_event(0x44, 0, 0, 0)
                time.sleep(0.02)
                user32.keybd_event(0x44, 0, 0x0002, 0)
                user32.keybd_event(0x5B, 0, 0x0002, 0)
            elif name == "win_key":
                user32.keybd_event(0x5B, 0, 0, 0)
                time.sleep(0.02)
                user32.keybd_event(0x5B, 0, 0x0002, 0)
            elif name == "ctrl_shift_esc":
                try:
                    subprocess.Popen(["taskmgr.exe"])
                except Exception:
                    user32.keybd_event(0x11, 0, 0, 0)
                    user32.keybd_event(0x10, 0, 0, 0)
                    user32.keybd_event(0x1B, 0, 0, 0)
                    time.sleep(0.02)
                    user32.keybd_event(0x1B, 0, 0x0002, 0)
                    user32.keybd_event(0x10, 0, 0x0002, 0)
                    user32.keybd_event(0x11, 0, 0x0002, 0)
            elif name == "alt_f4":
                user32.keybd_event(0x12, 0, 0, 0)
                user32.keybd_event(0x73, 0, 0, 0)
                time.sleep(0.02)
                user32.keybd_event(0x73, 0, 0x0002, 0)
                user32.keybd_event(0x12, 0, 0x0002, 0)
            elif name == "alt_tab":
                user32.keybd_event(0x12, 0, 0, 0)
                user32.keybd_event(0x09, 0, 0, 0)
                time.sleep(0.02)
                user32.keybd_event(0x09, 0, 0x0002, 0)
                user32.keybd_event(0x12, 0, 0x0002, 0)

        elif etype == "text":
            text_str = event.get("text", "").strip()
            # If URL is typed, open directly with default browser / launch
            if text_str.startswith(("http://", "https://", "www.")) or (len(text_str) > 4 and "." in text_str and not " " in text_str and text_str.endswith((".com", ".org", ".id", ".net", ".edu", ".html", ".php"))):
                url = text_str if text_str.startswith(("http://", "https://")) else f"https://{text_str}"
                import webbrowser
                try:
                    webbrowser.open(url)
                    return
                except Exception:
                    pass
            for ch in event.get("text", ""):
                char_val = ord(ch)
                user32.keybd_event(0, char_val, 0x0004, 0)
                user32.keybd_event(0, char_val, 0x0004 | 0x0002, 0)
                time.sleep(0.005)

    except Exception as e:
        print(f"[Remote Input Error] {e}")


def launch_system_application(app_cmd: str) -> tuple[bool, str]:
    """
    Intelligent Windows application and URL launcher.
    Supports:
    - Web URLs (opens Chrome/Edge or default browser, auto-prefixes https://)
    - Browser commands (chrome, msedge) with or without URLs
    - Microsoft Office (Word, Excel, PowerPoint, Access)
    - Coding & education tools (VS Code, Scratch 3, PacketTracer, Python IDLE, Paint)
    - System utilities (Notepad, Calculator, CMD, PowerShell, Explorer)
    - Quoted paths with spaces and custom command line arguments
    - Process termination commands (taskkill)
    - Strips redundant 'start ' prefixes seamlessly
    """
    import shlex
    import shutil

    app_cmd = app_cmd.strip()
    if not app_cmd:
        return False, "Perintah aplikasi kosong"

    # 1. Normalize: Strip redundant 'start ' prefix if present
    if app_cmd.lower().startswith("start "):
        app_cmd = app_cmd[6:].strip()

    # 2. Direct Process Killing (e.g., taskkill /f /im ...)
    if app_cmd.lower().startswith("taskkill"):
        try:
            subprocess.Popen(app_cmd, shell=True)
            return True, f"Perintah penghentian '{app_cmd}' berhasil dikirim"
        except Exception as e:
            return False, f"Gagal menjalankan taskkill: {e}"

    # 3. Direct Web URLs (e.g., https://classroom.google.com or www.google.com)
    is_direct_url = False
    clean_url = app_cmd
    if clean_url.startswith(("http://", "https://")):
        is_direct_url = True
    elif not " " in clean_url and any(clean_url.lower().startswith(x) for x in ("www.", "classroom.google.", "quizizz.", "kahoot.", "youtube.", "scratch.mit.", "canva.", "wordwall.", "padlet.")):
        is_direct_url = True
        clean_url = "https://" + clean_url

    if is_direct_url:
        # Prefer Google Chrome for school lab environments, then Edge, then default browser
        chrome_paths = [
            r"C:\Program Files\Google\Chrome\Application\chrome.exe",
            r"C:\Program Files (x86)\Google\Chrome\Application\chrome.exe",
            os.path.expandvars(r"%LOCALAPPDATA%\Google\Chrome\Application\chrome.exe"),
            os.path.expandvars(r"%PROGRAMFILES%\Google\Chrome\Application\chrome.exe"),
        ]
        edge_paths = [
            r"C:\Program Files (x86)\Microsoft\Edge\Application\msedge.exe",
            r"C:\Program Files\Microsoft\Edge\Application\msedge.exe",
            os.path.expandvars(r"%LOCALAPPDATA%\Microsoft\Edge\Application\msedge.exe"),
        ]

        for cp in chrome_paths:
            if os.path.exists(cp):
                try:
                    subprocess.Popen(f'"{cp}" "{clean_url}"', shell=True)
                    return True, f"Membuka URL di Google Chrome: {clean_url}"
                except Exception:
                    pass

        for ep in edge_paths:
            if os.path.exists(ep):
                try:
                    subprocess.Popen(f'"{ep}" "{clean_url}"', shell=True)
                    return True, f"Membuka URL di Microsoft Edge: {clean_url}"
                except Exception:
                    pass

        import webbrowser
        try:
            webbrowser.open(clean_url)
            return True, f"Membuka URL di browser: {clean_url}"
        except Exception as e:
            return False, f"Gagal membuka URL '{clean_url}': {e}"

    # 4. Windows Application Launcher
    if platform.system() == "Windows":
        # Safely parse executable and arguments respecting quotes
        try:
            tokens = shlex.split(app_cmd, posix=False)
        except Exception:
            tokens = app_cmd.split(maxsplit=1)

        raw_exe = tokens[0].strip('"\'' ) if tokens else ""
        args = " ".join(tokens[1:]).strip() if len(tokens) > 1 else ""

        if not raw_exe:
            return False, "Nama executable tidak valid"

        # Special UWP App Protocols for Windows 10/11
        clean_lower = raw_exe.lower()
        if clean_lower in ("calc", "calc.exe", "calculator", "calculator.exe"):
            try:
                subprocess.Popen("start calc:", shell=True)
                return True, "Kalkulator berhasil dibuka"
            except Exception:
                pass
        elif clean_lower in ("mspaint", "mspaint.exe", "paint", "paint.exe"):
            try:
                subprocess.Popen("start ms-paint:", shell=True)
                return True, "Paint / Gambar berhasil dibuka"
            except Exception:
                pass

        # Special check: If raw_exe is a directory / folder path (e.g. C:\Users\... or D:\Folder)
        if os.path.isdir(raw_exe):
            subprocess.Popen(f'explorer.exe "{raw_exe}"', shell=True)
            return True, f"Folder '{os.path.basename(raw_exe) or raw_exe}' berhasil dibuka di File Explorer siswa"

        # Special check: If raw_exe is explorer.exe opening a folder
        if raw_exe.lower() in ("explorer", "explorer.exe"):
            clean_arg = args.strip('"\'' )
            if clean_arg and os.path.exists(clean_arg):
                subprocess.Popen(f'explorer.exe "{clean_arg}"', shell=True)
                return True, f"Folder '{clean_arg}' berhasil dibuka di File Explorer siswa"
            else:
                subprocess.Popen(f'explorer.exe {args}'.strip(), shell=True)
                return True, "File Explorer berhasil dibuka di layar siswa"

        # If raw_exe is already an absolute path that exists
        if os.path.isabs(raw_exe) and os.path.exists(raw_exe):
            work_dir = os.path.dirname(raw_exe) if os.path.isfile(raw_exe) else None
            cmd = f'"{raw_exe}" {args}'.strip()
            subprocess.Popen(cmd, cwd=work_dir, shell=True)
            return True, f"Aplikasi '{os.path.basename(raw_exe)}' berhasil dijalankan"

        clean_first = raw_exe if raw_exe.lower().endswith(".exe") else f"{raw_exe}.exe"
        resolved_path = None

        # Comprehensive known locations map for common educational & lab software
        candidates = {
            "chrome.exe": [
                r"C:\Program Files\Google\Chrome\Application\chrome.exe",
                r"C:\Program Files (x86)\Google\Chrome\Application\chrome.exe",
                os.path.expandvars(r"%LOCALAPPDATA%\Google\Chrome\Application\chrome.exe"),
                os.path.expandvars(r"%PROGRAMFILES%\Google\Chrome\Application\chrome.exe"),
            ],
            "msedge.exe": [
                r"C:\Program Files (x86)\Microsoft\Edge\Application\msedge.exe",
                r"C:\Program Files\Microsoft\Edge\Application\msedge.exe",
                os.path.expandvars(r"%LOCALAPPDATA%\Microsoft\Edge\Application\msedge.exe"),
            ],
            "winword.exe": [
                r"C:\Program Files\Microsoft Office\root\Office16\WINWORD.EXE",
                r"C:\Program Files (x86)\Microsoft Office\root\Office16\WINWORD.EXE",
                r"C:\Program Files\Microsoft Office\Office16\WINWORD.EXE",
                r"C:\Program Files (x86)\Microsoft Office\Office16\WINWORD.EXE",
                r"C:\Program Files\Microsoft Office\Office15\WINWORD.EXE",
            ],
            "excel.exe": [
                r"C:\Program Files\Microsoft Office\root\Office16\EXCEL.EXE",
                r"C:\Program Files (x86)\Microsoft Office\root\Office16\EXCEL.EXE",
                r"C:\Program Files\Microsoft Office\Office16\EXCEL.EXE",
                r"C:\Program Files (x86)\Microsoft Office\Office16\EXCEL.EXE",
                r"C:\Program Files\Microsoft Office\Office15\EXCEL.EXE",
            ],
            "powerpnt.exe": [
                r"C:\Program Files\Microsoft Office\root\Office16\POWERPNT.EXE",
                r"C:\Program Files (x86)\Microsoft Office\root\Office16\POWERPNT.EXE",
                r"C:\Program Files\Microsoft Office\Office16\POWERPNT.EXE",
                r"C:\Program Files (x86)\Microsoft Office\Office16\POWERPNT.EXE",
                r"C:\Program Files\Microsoft Office\Office15\POWERPNT.EXE",
            ],
            "code.exe": [
                os.path.expandvars(r"%LOCALAPPDATA%\Programs\Microsoft VS Code\Code.exe"),
                r"C:\Program Files\Microsoft VS Code\Code.exe",
                r"C:\Program Files (x86)\Microsoft VS Code\Code.exe",
            ],
            "scratch.exe": [
                os.path.expandvars(r"%PROGRAMFILES%\Scratch 3\Scratch 3.exe"),
                os.path.expandvars(r"%LOCALAPPDATA%\Programs\Scratch 3\Scratch 3.exe"),
                os.path.expandvars(r"%PROGRAMFILES(X86)%\Scratch 3\Scratch 3.exe"),
                os.path.expandvars(r"%LOCALAPPDATA%\Programs\scratch-desktop\Scratch Desktop.exe"),
            ],
            "scratch 3.exe": [
                os.path.expandvars(r"%PROGRAMFILES%\Scratch 3\Scratch 3.exe"),
                os.path.expandvars(r"%LOCALAPPDATA%\Programs\Scratch 3\Scratch 3.exe"),
                os.path.expandvars(r"%PROGRAMFILES(X86)%\Scratch 3\Scratch 3.exe"),
            ],
            "packettracer.exe": [
                r"C:\Program Files\Cisco Packet Tracer 8.2.2\bin\PacketTracer.exe",
                r"C:\Program Files\Cisco Packet Tracer 8.2.1\bin\PacketTracer.exe",
                r"C:\Program Files\Cisco Packet Tracer 8.2.0\bin\PacketTracer.exe",
                r"C:\Program Files\Cisco Packet Tracer 8.1.1\bin\PacketTracer.exe",
                r"C:\Program Files\Cisco Packet Tracer 8.0\bin\PacketTracer.exe",
                r"C:\Program Files\Cisco Packet Tracer 7.3.1\bin\PacketTracer.exe",
            ],
            "typingmaster.exe": [
                r"C:\Program Files (x86)\TypingMaster11\tmaster.exe",
                r"C:\Program Files (x86)\TypingMaster\tmaster.exe",
                r"C:\Program Files\TypingMaster11\tmaster.exe",
                r"C:\Program Files\TypingMaster\tmaster.exe",
            ],
            "exambrowser.exe": [
                r"C:\Exambrowser\Exambro.exe",
                r"C:\Exambrowser\Exambrowser.exe",
                r"C:\Exambro\Exambro.exe",
                r"C:\ExambroClient\ExambroClient.exe",
                r"C:\ANBK\Exambro.exe",
                r"C:\ANBK\Exambrowser.exe",
                r"C:\CBT\Exambro.exe",
                r"D:\Exambrowser\Exambro.exe",
                r"D:\Exambro\Exambro.exe",
                r"D:\ANBK\Exambro.exe",
                os.path.expandvars(r"%USERPROFILE%\Desktop\Exambro.exe"),
                os.path.expandvars(r"%USERPROFILE%\Desktop\Exambrowser.exe"),
                os.path.expandvars(r"%USERPROFILE%\Desktop\Exambrowser\Exambro.exe"),
                os.path.expandvars(r"%PUBLIC%\Desktop\Exambro.exe"),
            ],
            "exambro.exe": [
                r"C:\Exambrowser\Exambro.exe",
                r"C:\Exambrowser\Exambrowser.exe",
                r"C:\Exambro\Exambro.exe",
                r"C:\ExambroClient\ExambroClient.exe",
                r"C:\ANBK\Exambro.exe",
                r"C:\ANBK\Exambrowser.exe",
                r"C:\CBT\Exambro.exe",
                r"D:\Exambrowser\Exambro.exe",
                r"D:\Exambro\Exambro.exe",
                r"D:\ANBK\Exambro.exe",
                os.path.expandvars(r"%USERPROFILE%\Desktop\Exambro.exe"),
                os.path.expandvars(r"%USERPROFILE%\Desktop\Exambrowser.exe"),
                os.path.expandvars(r"%USERPROFILE%\Desktop\Exambrowser\Exambro.exe"),
                os.path.expandvars(r"%PUBLIC%\Desktop\Exambro.exe"),
            ]
        }

        # Check candidate dictionary first
        c_key = clean_first.lower()
        if c_key in candidates:
            for cand in candidates[c_key]:
                if os.path.exists(cand):
                    resolved_path = cand
                    break

        is_open_exambro_folder = any(clean_first.lower().startswith(x) for x in (
            "open_exambro_folder", "folder_exambro", "folder_exambrowser",
            "buka_folder_exambro", "buka_folder_exambrowser"
        )) or any(x in app_cmd.lower() for x in (
            "buka folder exambro", "buka folder exambrowser", "open folder exambro"
        ))

        # Smart scan for ExamBrowser in typical folders & nested client directories (e.g. 64BitExambrowserClient_26.0222)
        if not resolved_path and (is_open_exambro_folder or any(x in clean_first.lower() for x in ("exam", "anbk", "cbt"))):
            search_dirs = [
                os.path.expandvars(r"%USERPROFILE%\Documents"),
                os.path.expandvars(r"%USERPROFILE%\Desktop"),
                os.path.expandvars(r"%PUBLIC%\Desktop"),
                os.path.expandvars(r"%USERPROFILE%\Downloads"),
                r"C:\Exambrowser", r"C:\Exambro", r"C:\ANBK", r"C:\CBT",
                r"D:\Exambrowser", r"D:\Exambro", r"D:\ANBK", r"D:\CBT",
                "C:\\", "D:\\"
            ]
            for sdir in search_dirs:
                if not os.path.isdir(sdir):
                    continue
                try:
                    for fname in os.listdir(sdir):
                        full_cand = os.path.join(sdir, fname)
                        if os.path.isfile(full_cand) and fname.lower().endswith(".exe") and ("exam" in fname.lower() or "anbk" in fname.lower()):
                            resolved_path = full_cand
                            break
                        # Search nested directories matching exam / anbk / cbt / client / 64bit
                        if os.path.isdir(full_cand) and any(kw in fname.lower() for kw in ("exam", "anbk", "cbt", "client", "64bit", "32bit")):
                            try:
                                for root, _, files in os.walk(full_cand):
                                    for wf in files:
                                        if wf.lower().endswith(".exe") and ("exam" in wf.lower() or "anbk" in wf.lower()):
                                            resolved_path = os.path.join(root, wf)
                                            break
                                    if resolved_path:
                                        break
                            except Exception:
                                pass
                        if resolved_path:
                            break
                except Exception:
                    pass
                if resolved_path:
                    break

        # If user specifically asked to open the ExamBrowser folder
        if is_open_exambro_folder:
            if resolved_path:
                target_f = os.path.dirname(resolved_path)
                subprocess.Popen(f'explorer.exe "{target_f}"', shell=True)
                return True, f"Folder ExamBrowser berhasil dibuka di: {target_f}"
            else:
                docs = os.path.expandvars(r"%USERPROFILE%\Documents")
                subprocess.Popen(f'explorer.exe "{docs}"', shell=True)
                return True, "Folder Documents siswa berhasil dibuka di File Explorer"

        # Check Windows App Paths Registry (where registered Win32 applications live)
        if not resolved_path:
            try:
                import winreg
                reg_roots = (
                    (winreg.HKEY_LOCAL_MACHINE, r"SOFTWARE\Microsoft\Windows\CurrentVersion\App Paths"),
                    (winreg.HKEY_CURRENT_USER, r"SOFTWARE\Microsoft\Windows\CurrentVersion\App Paths"),
                    (winreg.HKEY_LOCAL_MACHINE, r"SOFTWARE\WOW6432Node\Microsoft\Windows\CurrentVersion\App Paths"),
                )
                for root_key, sub_path in reg_roots:
                    try:
                        with winreg.OpenKey(root_key, rf"{sub_path}\{clean_first}") as k:
                            val = winreg.QueryValue(k, "")
                            if val:
                                val_clean = val.strip('"\'' )
                                if os.path.exists(val_clean):
                                    resolved_path = val_clean
                                    break
                    except Exception:
                        pass
                    if resolved_path:
                        break
            except Exception:
                pass

        # Check system PATH via shutil.which
        if not resolved_path:
            which_path = shutil.which(raw_exe) or shutil.which(clean_first)
            if which_path and os.path.exists(which_path):
                resolved_path = which_path

        # If resolved to an absolute path, launch directly with its working directory
        if resolved_path:
            # If application is ExamBrowser / ANBK, run with RunAsAdmin and launch Auto-RUN watcher
            if "exam" in os.path.basename(resolved_path).lower() or "anbk" in os.path.basename(resolved_path).lower():
                try:
                    import winreg
                    with winreg.CreateKey(winreg.HKEY_CURRENT_USER, r"Software\Microsoft\Windows NT\CurrentVersion\AppCompatFlags\Layers") as key:
                        winreg.SetValueEx(key, resolved_path, 0, winreg.REG_SZ, "~ RUNASADMIN")
                except Exception:
                    pass
                work_dir = os.path.dirname(resolved_path) if os.path.isfile(resolved_path) else None
                watcher_ps = """
Add-Type @'
using System;
using System.Runtime.InteropServices;
public class ExamAutoRunner {
    [DllImport("user32.dll")] public static extern IntPtr FindWindow(string c, string n);
    [DllImport("user32.dll")] public static extern bool GetWindowRect(IntPtr h, out RECT r);
    [DllImport("user32.dll")] public static extern bool SetForegroundWindow(IntPtr h);
    [DllImport("user32.dll")] public static extern bool SetCursorPos(int x, int y);
    [DllImport("user32.dll")] public static extern void mouse_event(uint f, uint x, uint y, uint b, uint e);
    public const uint M_LDOWN = 0x02;
    public const uint M_LUP = 0x04;
    [StructLayout(LayoutKind.Sequential)] public struct RECT { public int L, T, R, B; }
    public static bool ClickWindowRun(string title) {
        IntPtr hwnd = FindWindow(null, title);
        if (hwnd == IntPtr.Zero) return false;
        SetForegroundWindow(hwnd);
        RECT r;
        if (GetWindowRect(hwnd, out r)) {
            int width = r.R - r.L;
            int height = r.B - r.T;
            if (width > 200 && height > 200) {
                int cx = r.L + (width / 2);
                int cy = r.T + (int)(height * 0.84);
                SetCursorPos(cx, cy);
                mouse_event(M_LDOWN, 0, 0, 0, 0);
                System.Threading.Thread.Sleep(80);
                mouse_event(M_LUP, 0, 0, 0, 0);
                return true;
            }
        }
        return false;
    }
}
'@
$found = $false;
for ($i = 0; $i -lt 30; $i++) {
    Start-Sleep -Milliseconds 500;
    $hwnd = [ExamAutoRunner]::FindWindow($null, "Exambrowser Client");
    if ($hwnd -ne [IntPtr]::Zero) { $found = $true; break; }
}
if ($found) {
    Start-Sleep -Milliseconds 5500;
    [ExamAutoRunner]::ClickWindowRun("Exambrowser Client");
    Start-Sleep -Milliseconds 400;
    [ExamAutoRunner]::ClickWindowRun("Exambrowser Client");
}
"""
                wb64 = base64.b64encode(watcher_ps.encode("utf-16le")).decode("ascii")
                ps_exec = (
                    f'Start-Process -FilePath "{resolved_path}" -WorkingDirectory "{work_dir}" -Verb RunAs; '
                    f'Start-Process powershell.exe -WindowStyle Hidden -ArgumentList "-NoProfile -ExecutionPolicy Bypass -EncodedCommand {wb64}"'
                )
                lb64 = base64.b64encode(ps_exec.encode("utf-16le")).decode("ascii")
                subprocess.Popen(f'powershell.exe -NoProfile -ExecutionPolicy Bypass -WindowStyle Hidden -EncodedCommand {lb64}', shell=True)
                return True, f"ExamBrowser berhasil dijalankan (Mode Administrator & Auto-RUN Ujian)"

            work_dir = os.path.dirname(resolved_path) if os.path.isfile(resolved_path) else None
            cmd = f'"{resolved_path}" {args}'.strip()
            subprocess.Popen(cmd, cwd=work_dir, shell=True)
            return True, f"Aplikasi '{os.path.basename(resolved_path)}' berhasil dijalankan"

        # Otherwise invoke Windows 'start' shell command (handles PATH, shortcuts, URLs)
        try:
            cmd = f'start "" "{raw_exe}" {args}'.strip() if args else f'start "" "{raw_exe}"'
            subprocess.Popen(cmd, shell=True)
            return True, f"Aplikasi '{raw_exe}' berhasil dijalankan"
        except Exception:
            try:
                subprocess.Popen(app_cmd, shell=True)
                return True, f"Aplikasi '{app_cmd}' dijalankan"
            except Exception as e:
                return False, f"Gagal menjalankan '{app_cmd}': {e}"

    # Non-Windows fallback
    try:
        subprocess.Popen(app_cmd, shell=True)
        return True, f"Aplikasi '{app_cmd}' dijalankan"
    except Exception as e:
        return False, f"Gagal menjalankan '{app_cmd}': {e}"


# --- Lock Screen Overlay (Tkinter) ---
class LockScreenManager:
    def __init__(self):
        self.root: Optional[tk.Tk] = None
        self.is_locked = False
        self._thread: Optional[threading.Thread] = None

    def _set_taskmgr_disabled(self, disabled: bool):
        if platform.system() == "Windows":
            try:
                import winreg
                key = winreg.CreateKey(winreg.HKEY_CURRENT_USER, r"Software\Microsoft\Windows\CurrentVersion\Policies\System")
                if disabled:
                    winreg.SetValueEx(key, "DisableTaskMgr", 0, winreg.REG_DWORD, 1)
                else:
                    try:
                        winreg.DeleteValue(key, "DisableTaskMgr")
                    except FileNotFoundError:
                        pass
                winreg.CloseKey(key)
            except Exception:
                pass

    def lock(self, message: str = "Layar Komputer Ini Dikunci oleh Guru / Administrator"):
        if self.is_locked:
            return
        self.is_locked = True
        self._set_taskmgr_disabled(True)

        def _run_gui():
            try:
                root = tk.Tk()
                self.root = root
                root.title("Classroom Monitor - Screen Locked")
                root.attributes("-fullscreen", True)
                root.attributes("-topmost", True)
                root.configure(background="#0b0f19")

                # Block Alt+F4 / window close
                root.protocol("WM_DELETE_WINDOW", lambda: None)
                root.bind("<<Unlock>>", lambda e: root.destroy())

                # Periodic heartbeat to maintain topmost & focus, and automatically destroy window on unlock
                def _heartbeat():
                    if not self.is_locked:
                        try:
                            root.destroy()
                        except Exception:
                            pass
                        return
                    try:
                        root.lift()
                        root.attributes("-topmost", True)
                        root.focus_force()
                        root.after(200, _heartbeat)
                    except Exception:
                        pass
                root.after(200, _heartbeat)

                # Center Card Container
                card = tk.Frame(root, bg="#111827", highlightbackground="#1f2937", highlightthickness=2, padx=40, pady=30)
                card.place(relx=0.5, rely=0.5, anchor=tk.CENTER)

                icon = tk.Label(card, text="🔒", font=("Segoe UI Emoji", 56), bg="#111827", fg="#f59e0b")
                icon.pack(pady=(0, 10))

                title = tk.Label(card, text="LAYAR DIKUNCI OLEH GURU", font=("Segoe UI", 24, "bold"), bg="#111827", fg="#f8fafc")
                title.pack(pady=5)

                msg_label = tk.Label(card, text=message, font=("Segoe UI", 13), bg="#111827", fg="#94a3b8", wraplength=500, justify=tk.CENTER)
                msg_label.pack(pady=15)

                hint = tk.Label(card, text="Harap perhatikan penjelasan materi di layar depan atau dengarkan instruksi guru.", font=("Segoe UI", 11, "italic"), bg="#111827", fg="#64748b")
                hint.pack(pady=(10, 0))

                root.mainloop()
            finally:
                self.root = None
                self.is_locked = False

        self._thread = threading.Thread(target=_run_gui, daemon=True)
        self._thread.start()

    def unlock(self):
        self.is_locked = False
        self._set_taskmgr_disabled(False)
        if self.root:
            try:
                self.root.event_generate("<<Unlock>>", when="tail")
            except Exception:
                pass


# --- Broadcast Popup Notification ---
def show_broadcast_popup(message: str, title: str = "Pengumuman Guru", urgent: bool = False):
    """Displays a non-blocking modern popup dialog with teacher's broadcast."""
    def _popup():
        win = tk.Tk()
        win.title(f"📢 {title}")
        win.attributes("-topmost", True)
        win.geometry("480x280")
        win.resizable(False, False)
        
        bg_color = "#18181b" if not urgent else "#2e1015"
        win.configure(bg=bg_color)

        # Center on screen
        win.update_idletasks()
        x = (win.winfo_screenwidth() - win.winfo_reqwidth()) // 2
        y = (win.winfo_screenheight() - win.winfo_reqheight()) // 2
        win.geometry(f"+{x}+{y}")

        pad = tk.Frame(win, bg=bg_color, padx=25, pady=20)
        pad.pack(fill=tk.BOTH, expand=True)

        header_color = "#ef4444" if urgent else "#38bdf8"
        icon_text = "🚨 PERINGATAN PENTING GURU" if urgent else "📢 PENGUMUMAN DARI GURU"
        header = tk.Label(pad, text=icon_text, font=("Segoe UI", 14, "bold"), bg=bg_color, fg=header_color)
        header.pack(anchor="w", pady=(0, 10))

        body = tk.Label(pad, text=message, font=("Segoe UI", 12), bg=bg_color, fg="#f1f5f9", wraplength=430, justify=tk.LEFT)
        body.pack(anchor="w", fill=tk.BOTH, expand=True, pady=5)

        btn_bg = "#dc2626" if urgent else "#2563eb"
        btn = tk.Button(
            pad, text="✓ Saya Mengerti", font=("Segoe UI", 11, "bold"),
            bg=btn_bg, fg="white", activebackground="#1d4ed8", activeforeground="white",
            relief=tk.FLAT, padx=20, pady=8, cursor="hand2",
            command=win.destroy
        )
        btn.pack(anchor="e", pady=(10, 0))

        # Windows system beep on urgent
        if urgent:
            try:
                import winsound
                winsound.MessageBeep(winsound.MB_ICONEXCLAMATION)
            except Exception:
                pass

        win.mainloop()

    threading.Thread(target=_popup, daemon=True).start()


def get_installed_applications() -> List[Dict[str, Any]]:
    """
    Scans Windows Registry (32-bit, 64-bit, Current User, and App Paths)
    to compile a clean, comprehensive list of installed software on this PC.
    """
    apps = []
    if platform.system() != "Windows":
        return apps

    import winreg
    scan_targets = [
        (winreg.HKEY_LOCAL_MACHINE, r"SOFTWARE\Microsoft\Windows\CurrentVersion\Uninstall"),
        (winreg.HKEY_LOCAL_MACHINE, r"SOFTWARE\WOW6432Node\Microsoft\Windows\CurrentVersion\Uninstall"),
        (winreg.HKEY_CURRENT_USER, r"SOFTWARE\Microsoft\Windows\CurrentVersion\Uninstall")
    ]

    for root, subkey in scan_targets:
        try:
            with winreg.OpenKey(root, subkey, 0, winreg.KEY_READ | winreg.KEY_WOW64_64KEY) as k:
                num_subkeys = winreg.QueryInfoKey(k)[0]
                for i in range(num_subkeys):
                    try:
                        sk_name = winreg.EnumKey(k, i)
                        with winreg.OpenKey(k, sk_name) as sk:
                            try:
                                name, _ = winreg.QueryValueEx(sk, "DisplayName")
                                if not name or not name.strip():
                                    continue
                                name = name.strip()
                                # Filter system internal noise, GUIDs, and hotfixes
                                if (name.startswith(("KB", "{")) and len(name) > 28) or any(
                                    x in name.lower() for x in ["security update", "hotfix", "redistributable", "update for windows"]
                                ):
                                    continue

                                ver = ""
                                try:
                                    ver, _ = winreg.QueryValueEx(sk, "DisplayVersion")
                                except Exception:
                                    pass

                                pub = ""
                                try:
                                    pub, _ = winreg.QueryValueEx(sk, "Publisher")
                                except Exception:
                                    pass

                                install_loc = ""
                                try:
                                    install_loc, _ = winreg.QueryValueEx(sk, "InstallLocation")
                                except Exception:
                                    pass

                                exe_hint = ""
                                try:
                                    disp_icon, _ = winreg.QueryValueEx(sk, "DisplayIcon")
                                    if disp_icon and ".exe" in disp_icon.lower():
                                        exe_hint = disp_icon.split(".exe")[0] + ".exe"
                                        exe_hint = exe_hint.strip('"')
                                except Exception:
                                    pass

                                if not any(a["name"].lower() == name.lower() for a in apps):
                                    apps.append({
                                        "name": name,
                                        "version": str(ver) if ver else "",
                                        "publisher": str(pub) if pub else "",
                                        "install_location": str(install_loc) if install_loc else "",
                                        "exe": str(exe_hint) if exe_hint else ""
                                    })
                            except Exception:
                                pass
                    except Exception:
                        pass
        except Exception:
            pass

    # Also scan App Paths registry key for direct runnable exe names
    try:
        with winreg.OpenKey(winreg.HKEY_LOCAL_MACHINE, r"SOFTWARE\Microsoft\Windows\CurrentVersion\App Paths") as k:
            num_subkeys = winreg.QueryInfoKey(k)[0]
            for i in range(num_subkeys):
                try:
                    exe_name = winreg.EnumKey(k, i)
                    with winreg.OpenKey(k, exe_name) as sk:
                        path_val, _ = winreg.QueryValueEx(sk, "")
                        clean_name = exe_name.replace(".exe", "").capitalize()
                        if path_val and os.path.exists(path_val) and not any(
                            a["name"].lower() == clean_name.lower() or clean_name.lower() in a["name"].lower() for a in apps
                        ):
                            apps.append({
                                "name": clean_name,
                                "version": "",
                                "publisher": "Windows Application",
                                "install_location": os.path.dirname(path_val),
                                "exe": path_val
                            })
                except Exception:
                    pass
    except Exception:
        pass

    apps.sort(key=lambda x: x["name"].lower())
    return apps


def get_winget_pending_updates() -> List[Dict[str, Any]]:
    """
    Runs 'winget list --upgrade' to fetch all pending software updates available on this PC.
    Returns a list of dicts with keys: id, name, version, available_version, source.
    Falls back to empty list if winget is not installed or unavailable.
    """
    updates = []
    if platform.system() != "Windows":
        return updates
    try:
        result = subprocess.run(
            ["winget", "list", "--upgrade", "--accept-source-agreements", "--disable-interactivity"],
            capture_output=True,
            text=True,
            timeout=60,
            creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0x08000000)
        )
        lines = result.stdout.splitlines()
        # Parse winget table output — skip header rows, parse columns by position
        # Winget output looks like:
        # Name                        Id                       Version    Available  Source
        # ------------------------------------------------------------------------------------
        # Google Chrome               Google.Chrome            120.0.1234 121.0.0    winget
        header_idx = -1
        separator_idx = -1
        for i, line in enumerate(lines):
            if line.strip().startswith("---"):
                separator_idx = i
                header_idx = i - 1
                break

        if header_idx < 0 or separator_idx < 0:
            return updates

        # Determine column boundaries from header line using field names
        hdr = lines[header_idx]
        col_name    = hdr.lower().find("name")
        col_id      = hdr.lower().find("id")
        col_ver     = hdr.lower().find("version")
        col_avail   = hdr.lower().find("available")
        col_source  = hdr.lower().find("source")

        for line in lines[separator_idx + 1:]:
            if not line.strip() or "upgrades available" in line.lower():
                continue
            try:
                name    = line[col_name:col_id].strip()         if col_name >= 0 and col_id > col_name else ""
                pkg_id  = line[col_id:col_ver].strip()          if col_id >= 0 and col_ver > col_id else ""
                ver     = line[col_ver:col_avail].strip()       if col_ver >= 0 and col_avail > col_ver else ""
                avail   = line[col_avail:col_source].strip()    if col_avail >= 0 and col_source > col_avail else ""
                source  = line[col_source:].strip()             if col_source >= 0 else ""
                if name and pkg_id:
                    updates.append({
                        "name": name,
                        "id": pkg_id,
                        "version": ver,
                        "available_version": avail,
                        "source": source
                    })
            except Exception:
                pass
    except FileNotFoundError:
        # winget is not installed on this system
        pass
    except subprocess.TimeoutExpired:
        pass
    except Exception:
        pass
    return updates


def run_winget_upgrade(package_id: str = "") -> str:
    """
    Runs 'winget upgrade <package_id>' or 'winget upgrade --all' if package_id is empty.
    Returns a result message string. Runs silently without a visible window.
    """
    if platform.system() != "Windows":
        return "winget hanya tersedia di Windows"
    try:
        if package_id:
            cmd = ["winget", "upgrade", package_id, "--accept-source-agreements",
                   "--accept-package-agreements", "--disable-interactivity", "--silent"]
            label = f"'{package_id}'"
        else:
            cmd = ["winget", "upgrade", "--all", "--accept-source-agreements",
                   "--accept-package-agreements", "--disable-interactivity", "--silent"]
            label = "semua paket"

        result = subprocess.run(
            cmd,
            capture_output=True,
            text=True,
            timeout=300,   # 5 minutes max per upgrade
            creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0x08000000)
        )
        if result.returncode == 0:
            return f"Upgrade winget {label} berhasil"
        else:
            stderr = (result.stderr or "").strip()[:300]
            return f"Upgrade winget {label} selesai (kode: {result.returncode}). {stderr}"
    except FileNotFoundError:
        return "winget tidak tersedia di komputer ini"
    except subprocess.TimeoutExpired:
        return "Upgrade timeout (lebih dari 5 menit)"
    except Exception as e:
        return f"Gagal upgrade winget: {e}"


def get_windows_hotfixes() -> List[Dict[str, Any]]:
    """
    Runs PowerShell Get-HotFix to retrieve installed Windows Update KB patches on this PC.
    Returns list of dicts with keys: hotfix_id, description, installed_by, installed_on.
    """
    patches = []
    if platform.system() != "Windows":
        return patches
    try:
        ps_script = (
            "Get-HotFix | Sort-Object InstalledOn -Descending -ErrorAction SilentlyContinue | "
            "Select-Object -First 50 HotFixID, Description, InstalledBy, "
            "@{N='InstalledOn';E={if($_.InstalledOn){$_.InstalledOn.ToString('yyyy-MM-dd')}else{''}}} | "
            "ConvertTo-Json -Compress"
        )
        result = subprocess.run(
            ["powershell", "-NoProfile", "-NonInteractive", "-Command", ps_script],
            capture_output=True,
            text=True,
            timeout=30,
            creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0x08000000)
        )
        if result.stdout.strip():
            raw = json.loads(result.stdout.strip())
            # PowerShell returns a dict (not list) when only 1 result
            if isinstance(raw, dict):
                raw = [raw]
            for item in raw:
                patches.append({
                    "hotfix_id":    str(item.get("HotFixID", "") or ""),
                    "description":  str(item.get("Description", "") or ""),
                    "installed_by": str(item.get("InstalledBy", "") or ""),
                    "installed_on": str(item.get("InstalledOn", "") or "")
                })
    except subprocess.TimeoutExpired:
        pass
    except Exception:
        pass
    return patches


def control_system_audio(mute: bool = True) -> tuple[bool, str]:
    """
    Controls Windows system master audio:
    - mute=True: Silences master volume via WM_APPCOMMAND and VK_VOLUME_MUTE key event.
    - mute=False: Unmutes audio by sending volume up key events (Windows automatically unmutes).
    """
    if platform.system() != "Windows":
        return False, "Kontrol audio hanya didukung di Windows"
    try:
        user32 = ctypes.windll.user32
        VK_VOLUME_MUTE = 0xAD
        VK_VOLUME_DOWN = 0xAE
        VK_VOLUME_UP = 0xAF
        KEYEVENTF_KEYUP = 0x0002

        def press(vk):
            user32.keybd_event(vk, 0, 0, 0)
            user32.keybd_event(vk, 0, KEYEVENTF_KEYUP, 0)

        if mute:
            # Send WM_APPCOMMAND mute to desktop window
            WM_APPCOMMAND = 0x0319
            APPCOMMAND_VOLUME_MUTE = 0x80000
            hwnd = user32.GetDesktopWindow()
            user32.SendMessageW(hwnd, WM_APPCOMMAND, hwnd, APPCOMMAND_VOLUME_MUTE)
            # Extra safeguard: ensure mute key pressed
            press(VK_VOLUME_MUTE)
            return True, "Speaker komputer berhasil di-MUTE (suara disenyapkan)"
        else:
            # In Windows, sending Volume UP immediately un-mutes and restores audio level
            press(VK_VOLUME_UP)
            press(VK_VOLUME_UP)
            press(VK_VOLUME_DOWN)
            return True, "Speaker komputer berhasil di-UNMUTE (suara aktif kembali)"
    except Exception as e:
        return False, f"Gagal mengubah status audio: {e}"


def discover_server_beacon(timeout: float = 2.0) -> Optional[str]:
    """Listens on UDP 9876 for server beacon broadcast to auto-discover teacher's LAN IP."""
    sock = None
    try:
        sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        sock.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
        if hasattr(socket, "SO_BROADCAST"):
            sock.setsockopt(socket.SOL_SOCKET, socket.SO_BROADCAST, 1)
        sock.bind(("", 9876))
        sock.settimeout(timeout)
        data, _ = sock.recvfrom(2048)
        beacon = json.loads(data.decode("utf-8"))
        if beacon.get("app") == "LabGuardPro" and beacon.get("ws_url"):
            return beacon["ws_url"]
    except Exception:
        pass
    finally:
        if sock:
            try:
                sock.close()
            except Exception:
                pass
    return None


# --- Main Client Agent Engine ---
class LabClientAgent:
    def __init__(self, server_url: str = DEFAULT_SERVER_URL, lab_room: str = DEFAULT_LAB_ROOM):
        self.server_url = server_url
        self.lab_room = lab_room
        self.hostname = socket.gethostname()
        self.client_id = self.hostname
        self.ip_address = self._detect_local_ip()
        self.mac_address = self._detect_mac_address()
        self.machine_guid = self._detect_machine_guid()
        self.lock_mgr = LockScreenManager()
        self.running = True
        self.remote_active = False
        self.remote_quality = 55

    def _detect_local_ip(self) -> str:
        # 1. Try to route towards the teacher / server IP
        try:
            from urllib.parse import urlparse
            parsed = urlparse(self.server_url)
            host = parsed.hostname or ""
            if host and host not in ("127.0.0.1", "localhost", "::1"):
                s = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
                s.connect((host, 80))
                ip = s.getsockname()[0]
                s.close()
                if ip and not ip.startswith("127."):
                    return ip
        except Exception:
            pass

        # 2. Fallback: Route towards public DNS
        try:
            s = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
            s.connect(("8.8.8.8", 80))
            ip = s.getsockname()[0]
            s.close()
            if ip and not ip.startswith("127."):
                return ip
        except Exception:
            pass

        # 3. Fallback: Hostname resolution
        try:
            ip = socket.gethostbyname(socket.gethostname())
            if ip and not ip.startswith("127."):
                return ip
        except Exception:
            pass

        return "127.0.0.1"

    def _detect_machine_guid(self) -> str:
        """Returns persistent hardware GUID unique to this computer."""
        if platform.system() == "Windows":
            try:
                import winreg
                with winreg.OpenKey(winreg.HKEY_LOCAL_MACHINE, r"SOFTWARE\Microsoft\Cryptography") as key:
                    val, _ = winreg.QueryValueEx(key, "MachineGuid")
                    if val and len(str(val).strip()) > 10:
                        return str(val).strip().upper()
            except Exception:
                pass
        
        # Persistent file in Local AppData
        try:
            app_dir = os.path.join(os.environ.get("LOCALAPPDATA", os.path.expanduser("~")), "LabGuardClient")
            os.makedirs(app_dir, exist_ok=True)
            guid_file = os.path.join(app_dir, "machine_guid.txt")
            if os.path.exists(guid_file):
                with open(guid_file, "r", encoding="utf-8") as f:
                    content = f.read().strip()
                    if len(content) >= 16:
                        return content.upper()
            import uuid
            new_guid = str(uuid.uuid4()).upper()
            with open(guid_file, "w", encoding="utf-8") as f:
                f.write(new_guid)
            return new_guid
        except Exception:
            return ""

    def _detect_mac_address(self) -> str:
        # 1. Attempt to match the specific MAC address of the adapter bound to local IP
        try:
            for iface, addrs in psutil.net_if_addrs().items():
                has_ip = any(addr.family == socket.AF_INET and addr.address == self.ip_address for addr in addrs)
                if has_ip:
                    for addr in addrs:
                        # AF_LINK (MAC)
                        if getattr(addr, 'family', None) and getattr(psutil, 'AF_LINK', None) and addr.family == psutil.AF_LINK:
                            if addr.address and len(addr.address) >= 12:
                                return addr.address.replace('-', ':').upper()
        except Exception:
            pass

        # 2. Fallback to uuid.getnode()
        try:
            import uuid
            mac = uuid.getnode()
            return ':'.join(('%012X' % mac)[i:i+2] for i in range(0, 12, 2))
        except Exception:
            return "00:00:00:00:00:00"

    def get_system_health(self) -> dict:
        """Evaluates PC health metrics across Storage, Uptime, CPU, and RAM."""
        try:
            drive_path = 'C:\\' if platform.system() == 'Windows' else '/'
            disk = psutil.disk_usage(drive_path)
            disk_free_gb = round(disk.free / (1024**3), 1)
            disk_total_gb = round(disk.total / (1024**3), 1)
            disk_used_gb = round(disk.used / (1024**3), 1)
            disk_pct = round(disk.percent, 1)
        except Exception:
            disk_free_gb = 25.0
            disk_total_gb = 120.0
            disk_used_gb = 95.0
            disk_pct = 79.0

        try:
            uptime_seconds = int(time.time() - psutil.boot_time())
        except Exception:
            uptime_seconds = 3600

        try:
            cpu_pct = round(psutil.cpu_percent(interval=None), 1)
        except Exception:
            cpu_pct = 0.0

        try:
            vmem = psutil.virtual_memory()
            ram_pct = round(vmem.percent, 1)
            ram_used_mb = round(vmem.used / (1024 * 1024), 1)
            ram_total_mb = round(vmem.total / (1024 * 1024), 1)
        except Exception:
            ram_pct = 50.0
            ram_used_mb = 4096.0
            ram_total_mb = 8192.0

        score = 100
        issues = []
        status = "good"  # "good" | "warning" | "critical"

        # Storage Drive C rules
        if disk_free_gb < 5.0 or disk_pct >= 95:
            score -= 40
            issues.append(f"Disk C: sisa {disk_free_gb} GB")
            status = "critical"
        elif disk_free_gb < 15.0 or disk_pct >= 85:
            score -= 20
            issues.append(f"Disk C: sisa {disk_free_gb} GB")
            if status != "critical":
                status = "warning"

        # RAM rules
        if ram_pct >= 92:
            score -= 25
            issues.append(f"RAM penuh {ram_pct}%")
            status = "critical"
        elif ram_pct >= 82:
            score -= 15
            issues.append(f"RAM tinggi {ram_pct}%")
            if status != "critical":
                status = "warning"

        # CPU rules
        if cpu_pct >= 95:
            score -= 20
            issues.append(f"CPU overload {int(cpu_pct)}%")
            if status != "critical":
                status = "warning"

        # Uptime rules (>3 days without restart)
        uptime_days = uptime_seconds / 86400.0
        if uptime_days > 4.0:
            score -= 15
            issues.append(f"Uptime {int(uptime_days)} hari (Perlu reboot)")
            if status != "critical":
                status = "warning"

        score = max(10, min(100, score))

        return {
            "health_score": score,
            "health_status": status,
            "health_issues": issues,
            "disk_free_gb": disk_free_gb,
            "disk_total_gb": disk_total_gb,
            "disk_used_gb": disk_used_gb,
            "disk_pct": disk_pct,
            "uptime_seconds": uptime_seconds,
            "cpu_pct": cpu_pct,
            "ram_pct": ram_pct,
            "ram_used_mb": ram_used_mb,
            "ram_total_mb": ram_total_mb
        }

    def clean_system_temp(self) -> float:
        """Cleans temporary files from %TEMP% and user cache to free up disk space."""
        reclaimed_bytes = 0
        dirs_to_clean = []
        temp_dir = os.environ.get("TEMP") or os.environ.get("TMP")
        if temp_dir and os.path.exists(temp_dir):
            dirs_to_clean.append(temp_dir)
        local_app_data = os.environ.get("LOCALAPPDATA")
        if local_app_data:
            app_temp = os.path.join(local_app_data, "Temp")
            if os.path.exists(app_temp) and app_temp not in dirs_to_clean:
                dirs_to_clean.append(app_temp)

        for d in dirs_to_clean:
            try:
                for root, dirs, files in os.walk(d, topdown=False):
                    for f in files:
                        try:
                            fp = os.path.join(root, f)
                            sz = os.path.getsize(fp)
                            os.remove(fp)
                            reclaimed_bytes += sz
                        except Exception:
                            pass
                    for sub in dirs:
                        try:
                            os.rmdir(os.path.join(root, sub))
                        except Exception:
                            pass
            except Exception:
                pass

        return round(reclaimed_bytes / (1024 * 1024), 1)

    def get_handshake_payload(self) -> dict:
        health = self.get_system_health()
        active = get_active_window_info()
        active_display = active["process_name"]
        if active["window_title"] and active["window_title"] != "Desktop":
            short_title = active["window_title"][:35] + ("..." if len(active["window_title"]) > 35 else "")
            active_display = f"{active['process_name']} ({short_title})"

        return {
            "type": "handshake",
            "client_id": self.client_id,
            "hostname": self.hostname,
            "ip_address": self.ip_address,
            "mac_address": self.mac_address,
            "machine_guid": self.machine_guid,
            "lab_room": self.lab_room,
            "os_info": f"{platform.system()} {platform.release()}",
            "agent_version": f"v{AGENT_VERSION}",
            "cpu_pct": health["cpu_pct"],
            "ram_pct": health["ram_pct"],
            "ram_used_mb": health["ram_used_mb"],
            "ram_total_mb": health["ram_total_mb"],
            "disk_pct": health["disk_pct"],
            "disk_free_gb": health["disk_free_gb"],
            "disk_total_gb": health["disk_total_gb"],
            "disk_used_gb": health["disk_used_gb"],
            "uptime_seconds": health["uptime_seconds"],
            "health_score": health["health_score"],
            "health_status": health["health_status"],
            "health_issues": health["health_issues"],
            "active_app": active_display,
            "is_locked": 1 if self.lock_mgr.is_locked else 0
        }

    def collect_telemetry(self) -> dict:
        health = self.get_system_health()
        active = get_active_window_info()
        idle_sec = get_user_idle_seconds()
        fps_mode = "1.0s (Aktif)" if idle_sec < USER_IDLE_THRESHOLD_SEC else "4.5s (Hemat Bandwidth)"

        active_display = active["process_name"]
        if active["window_title"] and active["window_title"] != "Desktop":
            short_title = active["window_title"][:35] + ("..." if len(active["window_title"]) > 35 else "")
            active_display = f"{active['process_name']} ({short_title})"

        return {
            "type": "telemetry",
            "agent_version": f"v{AGENT_VERSION}",
            "cpu_pct": health["cpu_pct"],
            "ram_pct": health["ram_pct"],
            "ram_used_mb": health["ram_used_mb"],
            "ram_total_mb": health["ram_total_mb"],
            "disk_pct": health["disk_pct"],
            "disk_free_gb": health["disk_free_gb"],
            "disk_total_gb": health["disk_total_gb"],
            "disk_used_gb": health["disk_used_gb"],
            "uptime_seconds": health["uptime_seconds"],
            "health_score": health["health_score"],
            "health_status": health["health_status"],
            "health_issues": health["health_issues"],
            "active_app": active_display,
            "is_locked": 1 if self.lock_mgr.is_locked else 0,
            "idle_seconds": round(idle_sec, 1),
            "fps_mode": fps_mode,
            "processes": get_top_processes(6)
        }

    async def handle_command(self, ws, cmd: dict):
        action = cmd.get("action")
        payload = cmd.get("payload", {})
        print(f"[AGENT] Received command: {action} with {payload}")

        status = "DONE"
        message = ""

        try:
            if action == "lock":
                msg = payload.get("message", "Layar Komputer Ini Dikunci oleh Guru / Administrator")
                self.lock_mgr.lock(msg)
                message = "Layar berhasil dikunci"

            elif action == "unlock":
                self.lock_mgr.unlock()
                message = "Layar berhasil dibuka"

            elif action == "broadcast":
                msg = payload.get("message", "Perhatian siswa!")
                urgent = payload.get("urgent", False)
                show_broadcast_popup(msg, urgent=urgent)
                message = "Pesan broadcast ditampilkan di layar siswa"

            elif action == "launch_app":
                app_cmd = payload.get("app", "").strip()
                if app_cmd:
                    try:
                        ok, msg = launch_system_application(app_cmd)
                        status = "DONE" if ok else "ERROR"
                        message = msg
                    except Exception as e:
                        status = "ERROR"
                        message = f"Gagal menjalankan '{app_cmd}': {e}"
                else:
                    status = "ERROR"
                    message = "Nama aplikasi kosong"

            elif action == "send_file":
                file_url = payload.get("url", "")
                filename = payload.get("filename", "berkas_dari_guru")
                dest_type = payload.get("destination", "desktop").lower()
                auto_open = payload.get("auto_open", False)
                show_popup = payload.get("show_popup", True)

                if not file_url:
                    status = "ERROR"
                    message = "URL berkas pengiriman kosong"
                else:
                    try:
                        dest_label = "Folder Downloads" if dest_type == "downloads" else ("Folder Documents" if dest_type == "documents" else "Desktop")
                        target_dir = None
                        if platform.system() == "Windows":
                            # 1. First priority: Exact registry User Shell Folders (handles OneDrive / redirected disks)
                            try:
                                import winreg
                                reg_map = {
                                    "desktop": "Desktop",
                                    "downloads": "{374DE290-123F-4565-9164-39C4925E467B}",
                                    "documents": "Personal"
                                }
                                reg_key = winreg.OpenKey(
                                    winreg.HKEY_CURRENT_USER,
                                    r"Software\Microsoft\Windows\CurrentVersion\Explorer\User Shell Folders"
                                )
                                val_name = reg_map.get(dest_type, "Desktop")
                                raw_val, _ = winreg.QueryValueEx(reg_key, val_name)
                                winreg.CloseKey(reg_key)
                                exp = os.path.expandvars(raw_val)
                                if os.path.exists(exp):
                                    target_dir = exp
                            except Exception:
                                pass

                            # 2. Second priority: Shell32 SHGetFolderPathW
                            if not target_dir:
                                try:
                                    import ctypes.wintypes
                                    buf = ctypes.create_unicode_buffer(ctypes.wintypes.MAX_PATH)
                                    csidl = 0x0000 if dest_type == "desktop" else (0x0005 if dest_type == "documents" else 0x0028)
                                    ctypes.windll.shell32.SHGetFolderPathW(None, csidl, None, 0, buf)
                                    res = buf.value
                                    if dest_type == "downloads":
                                        res = os.path.join(res, "Downloads")
                                    if os.path.exists(res):
                                        target_dir = res
                                except Exception:
                                    pass

                        # 3. Third priority: User home directory fallback
                        if not target_dir:
                            user_home = os.environ.get("USERPROFILE", os.path.expanduser("~"))
                            target_dir = os.path.join(user_home, "Downloads" if dest_type == "downloads" else ("Documents" if dest_type == "documents" else "Desktop"))

                        os.makedirs(target_dir, exist_ok=True)
                        dest_path = os.path.join(target_dir, filename)

                        # Check if file exists & is locked by another process, auto-increment if needed
                        base_n, ext_n = os.path.splitext(filename)
                        counter = 1
                        while os.path.exists(dest_path):
                            try:
                                with open(dest_path, "ab"):
                                    pass
                                break
                            except (PermissionError, OSError):
                                dest_path = os.path.join(target_dir, f"{base_n} ({counter}){ext_n}")
                                counter += 1

                        # Download file safely with stream & timeout
                        import urllib.request
                        req = urllib.request.Request(file_url, headers={"User-Agent": "LabGuard-Agent/2.3"})
                        with urllib.request.urlopen(req, timeout=60) as resp, open(dest_path, "wb") as f_out:
                            while chunk := resp.read(64 * 1024):
                                f_out.write(chunk)

                        message = f"File '{filename}' berhasil diterima dan disimpan di {dest_label}"
                        status = "DONE"

                        # Show visual popup if requested
                        if show_popup:
                            try:
                                show_broadcast_popup(
                                    f"📁 Berkas Diterima dari Guru!\n\nNama File: {filename}\nLokasi: {dest_label} Anda\n\nSilakan periksa berkas tersebut.",
                                    urgent=False
                                )
                            except Exception:
                                pass

                        # Auto open if requested
                        if auto_open and os.path.exists(dest_path):
                            try:
                                if platform.system() == "Windows":
                                    os.startfile(dest_path)
                                else:
                                    subprocess.Popen(["xdg-open", dest_path])
                            except Exception as e:
                                message += f" (Gagal membuka otomatis: {e})"

                    except Exception as e:
                        status = "ERROR"
                        message = f"Gagal mengunduh file: {e}"

            elif action == "kill_process":
                pid = payload.get("pid")
                proc_name = payload.get("name")
                killed = False
                if pid:
                    try:
                        p = psutil.Process(int(pid))
                        p.terminate()
                        killed = True
                        message = f"Proses PID {pid} dihentikan"
                    except Exception as e:
                        message = f"Gagal stop PID {pid}: {e}"
                elif proc_name:
                    count = 0
                    for p in psutil.process_iter(['name']):
                        if p.info['name'] and p.info['name'].lower() == proc_name.lower():
                            try:
                                p.terminate()
                                count += 1
                            except Exception:
                                pass
                    killed = count > 0
                    message = f"{count} proses '{proc_name}' dihentikan"

                if not killed:
                    status = "ERROR"

            elif action == "shutdown":
                message = "Komputer sedang dimatikan paksa (shutdown)..."
                status = "DONE"
                # Send confirmation back to server immediately before OS network shutdown
                try:
                    await ws.send(json.dumps({
                        "type": "command_result",
                        "action": action,
                        "status": status,
                        "message": message
                    }))
                except Exception:
                    pass

                def execute_shutdown():
                    time.sleep(1.0)
                    if platform.system() == "Windows":
                        # 1. Primary: Windows force shutdown (/s /f /t 2)
                        try:
                            subprocess.run(
                                ["shutdown", "/s", "/f", "/t", "2", "/c", "Komputer dimatikan oleh Guru / Administrator Lab"],
                                check=False,
                                creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0x08000000)
                            )
                        except Exception:
                            pass
                        # 2. Secondary fallback: PowerShell Stop-Computer -Force
                        try:
                            subprocess.run(
                                ["powershell", "-NoProfile", "-Command", "Stop-Computer -Force"],
                                check=False,
                                creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0x08000000)
                            )
                        except Exception:
                            pass
                    else:
                        os.system("shutdown -h now")

                threading.Thread(target=execute_shutdown, daemon=True).start()
                return

            elif action == "restart":
                message = "Komputer sedang di-restart paksa..."
                status = "DONE"
                try:
                    await ws.send(json.dumps({
                        "type": "command_result",
                        "action": action,
                        "status": status,
                        "message": message
                    }))
                except Exception:
                    pass

                def execute_restart():
                    time.sleep(1.0)
                    if platform.system() == "Windows":
                        # 1. Primary: Windows force restart (/r /f /t 2)
                        try:
                            subprocess.run(
                                ["shutdown", "/r", "/f", "/t", "2", "/c", "Komputer di-restart oleh Guru / Administrator Lab"],
                                check=False,
                                creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0x08000000)
                            )
                        except Exception:
                            pass
                        # 2. Secondary fallback: PowerShell Restart-Computer -Force
                        try:
                            subprocess.run(
                                ["powershell", "-NoProfile", "-Command", "Restart-Computer -Force"],
                                check=False,
                                creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0x08000000)
                            )
                        except Exception:
                            pass
                    else:
                        os.system("shutdown -r now")

                threading.Thread(target=execute_restart, daemon=True).start()
                return

            elif action == "clean_temp":
                try:
                    reclaimed_mb = await asyncio.to_thread(self.clean_system_temp)
                    message = f"Pembersihan sukses! Berhasil membebaskan {reclaimed_mb} MB ruang harddisk C:"
                    status = "DONE"
                except Exception as e:
                    status = "ERROR"
                    message = f"Gagal membersihkan temporary files: {e}"

                try:
                    await ws.send(json.dumps({
                        "type": "command_result",
                        "action": action,
                        "status": status,
                        "message": message
                    }))
                    # Immediately broadcast updated telemetry with newly freed space
                    await ws.send(json.dumps(self.collect_telemetry()))
                except Exception:
                    pass

            elif action == "get_installed_apps":
                apps = await asyncio.to_thread(get_installed_applications)
                await ws.send(json.dumps({
                    "type": "installed_apps",
                    "client_id": self.client_id,
                    "apps": apps
                }))
                message = f"Ditemukan {len(apps)} aplikasi terpasang"
                status = "DONE"

            elif action == "winget_check":
                # Fetch list of pending software updates via winget
                updates = await asyncio.to_thread(get_winget_pending_updates)
                await ws.send(json.dumps({
                    "type": "winget_updates",
                    "client_id": self.client_id,
                    "updates": updates
                }))
                message = f"Ditemukan {len(updates)} pembaruan tersedia (winget)"
                status = "DONE"

            elif action == "winget_upgrade":
                # Upgrade one package or all packages using winget
                package_id = payload.get("package_id", "").strip()
                result_msg = await asyncio.to_thread(run_winget_upgrade, package_id)
                message = result_msg
                status = "DONE"

            elif action == "windows_update_check":
                # Get installed Windows hotfixes / KB patches via PowerShell Get-HotFix
                patches = await asyncio.to_thread(get_windows_hotfixes)
                await ws.send(json.dumps({
                    "type": "windows_updates",
                    "client_id": self.client_id,
                    "patches": patches
                }))
                message = f"Ditemukan {len(patches)} Windows Update patch terpasang"
                status = "DONE"

            elif action in ("mute", "unmute", "mute_audio"):
                # Remote audio mute/unmute control
                mute_flag = payload.get("mute", True) if action == "mute_audio" else (action == "mute")
                ok, msg = await asyncio.to_thread(control_system_audio, mute_flag)
                message = msg
                status = "DONE" if ok else "ERROR"

        except Exception as e:
            status = "ERROR"
            message = str(e)

        # Send response back to server
        resp = {
            "type": "command_result",
            "action": action,
            "status": status,
            "message": message
        }
        await ws.send(json.dumps(resp))

    async def run(self):
        print(f"==================================================")
        print(f"  Classroom Monitor Client Agent (Web WebSocket)  ")
        print(f"  PC: {self.hostname} ({self.ip_address})         ")
        print(f"  Ruang: {self.lab_room}                          ")
        print(f"  Server Target: {self.server_url}                ")
        print(f"==================================================")

        fail_count = 0
        backoff = 2
        while self.running:
            try:
                print(f"[AGENT] Menghubungkan ke server {self.server_url}...")
                async with websockets.connect(self.server_url, ping_interval=20, ping_timeout=10) as ws:
                    print(f"[AGENT] Terhubung ke Server! Mengirim handshake...")
                    await ws.send(json.dumps(self.get_handshake_payload()))

                    # Wait for handshake ack
                    ack_raw = await ws.recv()
                    print(f"[AGENT] Handshake diterima: {ack_raw}")
                    backoff = 2  # Reset backoff on successful connection
                    fail_count = 0

                    # Send installed apps list asynchronously on initial connection
                    async def send_initial_apps():
                        try:
                            apps = await asyncio.to_thread(get_installed_applications)
                            if apps and self.running:
                                await ws.send(json.dumps({
                                    "type": "installed_apps",
                                    "client_id": self.client_id,
                                    "apps": apps
                                }))
                        except Exception:
                            pass

                    asyncio.create_task(send_initial_apps())

                    last_screen_time = 0.0
                    last_telem_time = 0.0
                    last_thumb_sent = ""
                    last_fingerprint = b""
                    last_active_app = ""
                    last_mem_trim = 0.0
                    current_fps_mode = "1.0s (Aktif)"

                    # Concurrent tasks: Telemetry/Screen sender, Remote Screen Streamer, and Command receiver
                    async def sender_loop():
                        nonlocal last_screen_time, last_telem_time, last_thumb_sent, last_fingerprint, last_active_app, last_mem_trim, current_fps_mode
                        while self.running:
                            now = time.time()
                            idle_sec = get_user_idle_seconds()
                            user_active = (idle_sec < USER_IDLE_THRESHOLD_SEC)

                            # Determine adaptive interval: 1.0s if typing/mouse active, 4.5s if idle/reading
                            target_interval = SCREENSHOT_INTERVAL_ACTIVE if user_active else SCREENSHOT_INTERVAL_IDLE
                            current_fps_mode = "1.0s (Aktif)" if user_active else "4.5s (Hemat Bandwidth)"

                            # Detect immediate app/window switch (e.g. Chrome -> Word -> Game)
                            active_info = get_active_window_info()
                            curr_app = active_info.get("process_name", "")
                            app_switched = bool(curr_app and last_active_app and curr_app != last_active_app)
                            if app_switched or (curr_app and not last_active_app):
                                last_active_app = curr_app

                            # 1. Send Telemetry every TELEMETRY_INTERVAL (1.5s)
                            if (now - last_telem_time) >= TELEMETRY_INTERVAL:
                                telem = self.collect_telemetry()
                                telem["fps_mode"] = current_fps_mode
                                telem["idle_seconds"] = round(idle_sec, 1)
                                await ws.send(json.dumps(telem))
                                last_telem_time = now

                            # 2. Adaptive Screen Capture & Transmission (skip if remote is streaming)
                            time_since_screen = (now - last_screen_time)
                            needs_screen_check = not self.remote_active and (
                                app_switched or 
                                (time_since_screen >= target_interval)
                            )

                            if needs_screen_check:
                                force_encode = (time_since_screen >= FORCED_REFRESH_INTERVAL) or app_switched or (not last_thumb_sent)
                                thumb, new_fp, diff_pct, changed = await asyncio.to_thread(
                                    capture_adaptive_thumbnail, last_fingerprint, force_encode
                                )
                                last_fingerprint = new_fp

                                # Send frame if screen actually changed or forced refresh was due
                                if (changed or force_encode) and thumb and (thumb != last_thumb_sent):
                                    last_thumb_sent = thumb
                                    await ws.send(json.dumps({
                                        "type": "screen",
                                        "thumbnail": thumb
                                    }))
                                last_screen_time = now

                            # 3. Periodic memory reclamation on client agent
                            if (now - last_mem_trim) >= 60.0:
                                last_mem_trim = now
                                import gc
                                gc.collect()
                                if platform.system() == "Windows":
                                    try:
                                        ctypes.windll.kernel32.SetProcessWorkingSetSize(-1, -1, -1)
                                    except Exception:
                                        pass

                            # Sleep in 0.25s slices for sub-second reaction to app switches
                            await asyncio.sleep(0.25)

                    async def remote_stream_loop():
                        while self.running:
                            if self.remote_active:
                                t0 = time.time()
                                try:
                                    frame = await asyncio.to_thread(capture_remote_frame, self.remote_quality, 1280)
                                    if frame and self.remote_active:
                                        await ws.send(json.dumps({
                                            "type": "remote_frame",
                                            "frame": frame,
                                            "timestamp": time.time()
                                        }))
                                except Exception as e:
                                    print(f"[Remote Stream Error] {e}")
                                elapsed = time.time() - t0
                                sleep_time = max(0.01, 0.05 - elapsed)
                                await asyncio.sleep(sleep_time)
                            else:
                                await asyncio.sleep(0.1)

                    async def receiver_loop():
                        while self.running:
                            msg_raw = await ws.recv()
                            msg = json.loads(msg_raw)
                            mtype = msg.get("type")
                            if mtype == "command":
                                await self.handle_command(ws, msg)
                            elif mtype == "start_remote":
                                self.remote_active = True
                                self.remote_quality = int(msg.get("quality", 55))
                                print(f"[AGENT] Remote Control diaktifkan oleh Guru (quality={self.remote_quality})")
                                await ws.send(json.dumps({"type": "remote_started", "client_id": self.client_id}))
                                # Send first frame immediately for zero-delay startup
                                frame = await asyncio.to_thread(capture_remote_frame, self.remote_quality, 1280)
                                if frame and self.remote_active:
                                    await ws.send(json.dumps({
                                        "type": "remote_frame",
                                        "frame": frame,
                                        "timestamp": time.time()
                                    }))
                            elif mtype == "stop_remote":
                                self.remote_active = False
                                print("[AGENT] Remote Control dinonaktifkan")
                                await ws.send(json.dumps({"type": "remote_stopped", "client_id": self.client_id}))
                            elif mtype == "remote_input":
                                event = msg.get("event")
                                if event:
                                    asyncio.create_task(asyncio.to_thread(execute_remote_input, event))

                    done, pending = await asyncio.wait(
                        [
                            asyncio.create_task(sender_loop()),
                            asyncio.create_task(remote_stream_loop()),
                            asyncio.create_task(receiver_loop())
                        ],
                        return_when=asyncio.FIRST_COMPLETED
                    )
                    for t in pending:
                        t.cancel()

            except (websockets.exceptions.ConnectionClosed, ConnectionRefusedError, OSError) as e:
                fail_count += 1
                if fail_count >= 3 and fail_count % 5 == 0:
                    print(f"[AGENT] Menghubungi server gagal {fail_count}x. Mencari server guru via UDP Beacon (Port 9876)...")
                    discovered_ws = await asyncio.to_thread(discover_server_beacon, 2.0)
                    if discovered_ws and discovered_ws != self.server_url:
                        print(f"[AGENT] Server guru ditemukan otomatis di {discovered_ws}! Menyimpan & beralih target...")
                        self.server_url = discovered_ws
                        save_agent_config(self.server_url, self.lab_room)
                        fail_count = 0
                        backoff = 1
                        continue

                print(f"[AGENT] Koneksi ke server terputus/gagal ({e}). Mencoba lagi dalam {backoff} detik...")
                await asyncio.sleep(backoff)
                backoff = min(backoff * 1.5, 15)
            except Exception as e:
                print(f"[AGENT] Terjadi kesalahan: {e}. Mencoba lagi dalam {backoff} detik...")
                await asyncio.sleep(backoff)


def detect_system_hardware_profile() -> Dict[str, str]:
    """Extracts exact hardware and OS specs matching enterprise DEX profiler."""
    hostname = socket.gethostname()
    
    # 1. CPU Name
    cpu_name = platform.processor() or "Generic x64 Processor"
    if platform.system() == "Windows":
        try:
            import winreg
            k = winreg.OpenKey(winreg.HKEY_LOCAL_MACHINE, r"HARDWARE\DESCRIPTION\System\CentralProcessor\0")
            cpu_name = winreg.QueryValueEx(k, "ProcessorNameString")[0].strip()
            winreg.CloseKey(k)
        except Exception:
            pass

    # 2. Motherboard / Model
    model_name = "Desktop PC System"
    if platform.system() == "Windows":
        try:
            import winreg
            b = winreg.OpenKey(winreg.HKEY_LOCAL_MACHINE, r"HARDWARE\DESCRIPTION\System\BIOS")
            mfg = winreg.QueryValueEx(b, "SystemManufacturer")[0].strip()
            prod = winreg.QueryValueEx(b, "SystemProductName")[0].strip()
            model_name = f"{mfg} {prod}".strip()
            winreg.CloseKey(b)
        except Exception:
            pass

    # 3. OS Version
    os_ver = f"{platform.system()} {platform.release()}"
    if platform.system() == "Windows":
        try:
            import winreg
            c = winreg.OpenKey(winreg.HKEY_LOCAL_MACHINE, r"SOFTWARE\Microsoft\Windows NT\CurrentVersion")
            prod_name = winreg.QueryValueEx(c, "ProductName")[0].strip()
            build_num = 0
            try:
                build_str = winreg.QueryValueEx(c, "CurrentBuildNumber")[0].strip()
                build_num = int(build_str)
            except Exception:
                pass
            if build_num >= 22000 and "Windows 10" in prod_name:
                prod_name = prod_name.replace("Windows 10", "Windows 11")
            os_ver = prod_name
            winreg.CloseKey(c)
        except Exception:
            pass

    # 4. Total Physical RAM
    try:
        total_ram_gb = round(psutil.virtual_memory().total / (1024 ** 3), 1)
        ram_str = f"{total_ram_gb} GB Total Physical RAM"
    except Exception:
        ram_str = "15.8 GB Total Physical RAM"

    # 5. Platform
    arch = platform.machine().lower() or "amd64"
    plat_str = f"{arch} · User-space TLS 1.3 Direct"

    # 6. Device ID
    import uuid
    mac_int = uuid.getnode()
    dev_id = f"PCD-{mac_int:012x}"

    return {
        "hostname": hostname,
        "cpu": cpu_name,
        "model": model_name,
        "os": os_ver,
        "ram": ram_str,
        "platform": plat_str,
        "device_id": dev_id
    }


def get_app_data_dir() -> str:
    r"""Returns directory for permanent agent installation (%LOCALAPPDATA%\LabGuardClient)."""
    app_data = os.environ.get("LOCALAPPDATA") or os.environ.get("APPDATA") or os.path.expanduser("~")
    target = os.path.join(app_data, "LabGuardClient")
    os.makedirs(target, exist_ok=True)
    return target


def get_config_file_path() -> str:
    """Returns absolute path to agent_config.json in LocalAppData or beside executable."""
    app_data_cfg = os.path.join(get_app_data_dir(), "agent_config.json")
    if os.path.exists(app_data_cfg):
        return app_data_cfg
    base_dir = os.path.dirname(sys.executable if getattr(sys, 'frozen', False) else __file__)
    local_cfg = os.path.join(base_dir, "agent_config.json")
    if os.path.exists(local_cfg):
        return local_cfg
    return app_data_cfg


def load_agent_config() -> dict:
    default_cfg = {
        "server_url": DEFAULT_SERVER_URL,
        "lab_room": DEFAULT_LAB_ROOM,
        "auto_startup": True,
        "device_id": ""
    }
    cfg_file = get_config_file_path()
    if os.path.exists(cfg_file):
        try:
            with open(cfg_file, "r", encoding="utf-8") as f:
                saved = json.load(f)
                default_cfg.update(saved)
        except Exception:
            pass
    return default_cfg


def save_agent_config(server_url: str, lab_room: str = "Lab RPL 1", target_dir: Optional[str] = None):
    if target_dir:
        cfg_file = os.path.join(target_dir, "agent_config.json")
    else:
        cfg_file = get_config_file_path()
    try:
        profile = detect_system_hardware_profile()
        data = {
            "server_url": server_url,
            "lab_room": lab_room,
            "auto_startup": True,
            "device_id": profile["device_id"],
            "hostname": profile["hostname"]
        }
        with open(cfg_file, "w", encoding="utf-8") as f:
            json.dump(data, f, indent=2)
    except Exception as e:
        print(f"[Warning] Gagal menyimpan config: {e}")


def install_agent_to_system(server_url: str, lab_room: str = "Lab RPL 1", auto_startup: bool = True) -> tuple:
    r"""
    Installs executable into %LOCALAPPDATA%\LabGuardClient, writes config,
    registers to Windows Startup, and launches in background.
    """
    try:
        install_dir = get_app_data_dir()
        dest_exe = os.path.join(install_dir, "Client_Siswa_Web.exe")
        current_exe = sys.executable if getattr(sys, 'frozen', False) else os.path.abspath(__file__)

        # 1. Copy executable if compiled and running from outside target dir
        if getattr(sys, 'frozen', False):
            if os.path.abspath(current_exe).lower() != os.path.abspath(dest_exe).lower():
                import shutil
                shutil.copy2(current_exe, dest_exe)
        else:
            dest_exe = current_exe

        # 2. Save config in installation directory
        save_agent_config(server_url, lab_room, target_dir=install_dir)

        # 3. Add to Windows Startup
        if auto_startup and platform.system() == "Windows":
            # Registry Run Entry
            try:
                import winreg
                key = winreg.OpenKey(winreg.HKEY_CURRENT_USER, r"Software\Microsoft\Windows\CurrentVersion\Run", 0, winreg.KEY_SET_VALUE)
                if getattr(sys, 'frozen', False):
                    winreg.SetValueEx(key, "LabGuardClientAgent", 0, winreg.REG_SZ, f'"{dest_exe}" --silent')
                else:
                    winreg.SetValueEx(key, "LabGuardClientAgent", 0, winreg.REG_SZ, f'pythonw "{dest_exe}" --silent')
                winreg.CloseKey(key)
            except Exception as e:
                print(f"[Warning] Gagal menulis ke registry: {e}")

            # Also create Startup folder shortcut via PowerShell
            try:
                startup_folder = os.path.join(os.environ.get("APPDATA", ""), r"Microsoft\Windows\Start Menu\Programs\Startup")
                shortcut_path = os.path.join(startup_folder, "LabGuard_Client.lnk")
                target_cmd = dest_exe if getattr(sys, 'frozen', False) else "pythonw.exe"
                target_args = "--silent" if getattr(sys, 'frozen', False) else f'"{dest_exe}" --silent'

                ps_script = (
                    f'$ws = New-Object -ComObject WScript.Shell; '
                    f'$s = $ws.CreateShortcut("{shortcut_path}"); '
                    f'$s.TargetPath = "{target_cmd}"; '
                    f'$s.Arguments = "{target_args}"; '
                    f'$s.WorkingDirectory = "{install_dir}"; '
                    f'$s.Description = "LabGuard Pro Client Agent Siswa"; '
                    f'$s.Save()'
                )
                subprocess.run(
                    ["powershell", "-NoProfile", "-NonInteractive", "-Command", ps_script],
                    creationflags=subprocess.CREATE_NO_WINDOW if platform.system() == "Windows" else 0,
                    capture_output=True
                )
            except Exception as e:
                print(f"[Warning] Gagal membuat shortcut startup: {e}")

        # 4. Launch background agent process
        start_agent_in_background(dest_exe, install_dir)
        return True, "Agen siswa berhasil dipasang dan berjalan di latar belakang!"
    except Exception as e:
        return False, str(e)


def uninstall_agent_from_system() -> tuple:
    """Removes startup shortcuts, registry entries, stops running agent processes."""
    try:
        # 1. Kill background agent processes (excluding self)
        my_pid = os.getpid()
        for p in psutil.process_iter(['pid', 'name']):
            try:
                if p.pid != my_pid and "Client_Siswa_Web" in p.name():
                    p.terminate()
            except Exception:
                pass

        # 2. Remove Registry Run
        if platform.system() == "Windows":
            try:
                import winreg
                key = winreg.OpenKey(winreg.HKEY_CURRENT_USER, r"Software\Microsoft\Windows\CurrentVersion\Run", 0, winreg.KEY_SET_VALUE)
                winreg.DeleteValue(key, "LabGuardClientAgent")
                winreg.CloseKey(key)
            except Exception:
                pass

            # 3. Remove Startup folder shortcut
            startup_folder = os.path.join(os.environ.get("APPDATA", ""), r"Microsoft\Windows\Start Menu\Programs\Startup")
            shortcut_path = os.path.join(startup_folder, "LabGuard_Client.lnk")
            if os.path.exists(shortcut_path):
                try:
                    os.remove(shortcut_path)
                except Exception:
                    pass

        # 4. Remove config file
        cfg_path = os.path.join(get_app_data_dir(), "agent_config.json")
        if os.path.exists(cfg_path):
            try:
                os.remove(cfg_path)
            except Exception:
                pass

        return True, "Agen LabGuard Pro berhasil dicopot dari sistem ini."
    except Exception as e:
        return False, str(e)


def start_agent_in_background(exe_path: str, work_dir: str):
    """Launches detached agent process with no console window."""
    if getattr(sys, 'frozen', False):
        cmd = [exe_path, "--silent"]
    else:
        pythonw = os.path.join(os.path.dirname(sys.executable), "pythonw.exe")
        if not os.path.exists(pythonw):
            pythonw = sys.executable
        cmd = [pythonw, exe_path, "--silent"]

    flags = 0
    if platform.system() == "Windows":
        flags = subprocess.CREATE_NO_WINDOW | getattr(subprocess, 'DETACHED_PROCESS', 0x00000008)

    subprocess.Popen(
        cmd,
        cwd=work_dir,
        creationflags=flags,
        close_fds=True
    )


def parse_to_websocket_url(raw_url: str) -> str:
    """Safely converts HTTP/HTTPS/WS endpoint or plain IP:PORT into valid WebSocket URL."""
    url = raw_url.strip()
    if not url:
        return DEFAULT_SERVER_URL

    # Normalize missing protocol
    if "://" not in url:
        if "/" in url:
            parts = url.split("/", 1)
            host_part = parts[0]
            path_part = "/" + parts[1]
            if ":" not in host_part:
                url = f"ws://{host_part}{path_part}"
            else:
                url = f"ws://{url}"
        else:
            if ":" not in url:
                url = f"ws://{url}:8000/ws/agent"
            else:
                url = f"ws://{url}"

    # Convert http/https to ws/wss
    if url.startswith("http://"):
        url = "ws://" + url[7:]
    elif url.startswith("https://"):
        url = "wss://" + url[8:]
    elif not (url.startswith("ws://") or url.startswith("wss://")):
        url = "ws://" + url

    # Ensure path ends with WebSocket agent endpoint
    from urllib.parse import urlparse
    parsed = urlparse(url)
    if not parsed.path or parsed.path == "/":
        url = url.rstrip("/") + "/ws/agent"
    elif not ("/ws/" in parsed.path or parsed.path.endswith("/ws/agent")):
        url = url.rstrip("/") + "/ws/agent"

    return url


def show_installer_gui(cfg: dict):
    """
    Renders pixel-perfect, modern LabGuard PRO Setup & One-Click Background Installer.
    """
    from tkinter import messagebox
    profile = detect_system_hardware_profile()

    win = tk.Tk()
    win.title("LabGuard PRO - Pemasang Agen Siswa (Background Service)")
    win.geometry("680x580")
    win.resizable(False, False)
    win.configure(bg="#f8fafc")

    # Center on screen
    win.update_idletasks()
    sw = win.winfo_screenwidth()
    sh = win.winfo_screenheight()
    x = max(0, (sw - 680) // 2)
    y = max(0, (sh - 580) // 2)
    win.geometry(f"680x580+{x}+{y}")

    # ================= TOP HEADER (NAVY BLUE) =================
    header_frame = tk.Frame(win, bg="#0f172a", padx=24, pady=16)
    header_frame.pack(fill=tk.X)

    header_left = tk.Frame(header_frame, bg="#0f172a")
    header_left.pack(side=tk.LEFT, fill=tk.Y)

    title_row = tk.Frame(header_left, bg="#0f172a")
    title_row.pack(anchor="w")

    lbl_logo = tk.Label(title_row, text="🛡️", font=("Segoe UI Emoji", 16), bg="#0f172a")
    lbl_logo.pack(side=tk.LEFT, padx=(0, 8))

    lbl_brand = tk.Label(title_row, text="LabGuard", font=("Segoe UI", 16, "bold"), bg="#0f172a", fg="#38bdf8")
    lbl_brand.pack(side=tk.LEFT)

    lbl_pro = tk.Label(title_row, text="PRO", font=("Segoe UI", 16, "bold"), bg="#0f172a", fg="#f59e0b")
    lbl_pro.pack(side=tk.LEFT, padx=(4, 0))

    lbl_title_sub = tk.Label(title_row, text=" — Pemasang Agen Siswa", font=("Segoe UI", 12), bg="#0f172a", fg="#94a3b8")
    lbl_title_sub.pack(side=tk.LEFT, padx=(4, 0))

    lbl_sub = tk.Label(
        header_left,
        text="Aplikasi Pemantauan & Kontrol Laboratorium Komputer Real-time",
        font=("Segoe UI", 9),
        bg="#0f172a",
        fg="#cbd5e1"
    )
    lbl_sub.pack(anchor="w", pady=(4, 0))

    badge_frame = tk.Frame(header_frame, bg="#1e293b", highlightthickness=1, highlightbackground="#334155", padx=12, pady=4)
    badge_frame.pack(side=tk.RIGHT, anchor="center")

    lbl_badge = tk.Label(badge_frame, text=f"v{AGENT_VERSION} · Standalone EXE", font=("Segoe UI", 8, "bold"), bg="#1e293b", fg="#38bdf8")
    lbl_badge.pack()

    # ================= BODY CONTENT =================
    body_frame = tk.Frame(win, bg="#f8fafc", padx=24, pady=16)
    body_frame.pack(fill=tk.BOTH, expand=True)

    # ================= HARDWARE PROFILE CARD =================
    prof_card = tk.LabelFrame(
        body_frame,
        text="  PROFIL KOMPUTER INI  ",
        font=("Segoe UI", 8, "bold"),
        fg="#0284c7",
        bg="#ffffff",
        bd=1,
        relief=tk.SOLID,
        padx=12,
        pady=8
    )
    prof_card.pack(fill=tk.X, pady=(0, 12))

    tk.Label(prof_card, text="Nama PC / Siswa:", font=("Segoe UI", 8), bg="#ffffff", fg="#64748b").grid(row=0, column=0, sticky="w", pady=2)
    tk.Label(prof_card, text=profile["hostname"], font=("Segoe UI", 8, "bold"), bg="#ffffff", fg="#0f172a").grid(row=0, column=1, sticky="w", padx=(6, 24), pady=2)

    tk.Label(prof_card, text="Sistem Operasi:", font=("Segoe UI", 8), bg="#ffffff", fg="#64748b").grid(row=0, column=2, sticky="w", pady=2)
    tk.Label(prof_card, text=profile["os"][:30], font=("Segoe UI", 8, "bold"), bg="#ffffff", fg="#0f172a").grid(row=0, column=3, sticky="w", padx=(6, 0), pady=2)

    tk.Label(prof_card, text="Prosesor (CPU):", font=("Segoe UI", 8), bg="#ffffff", fg="#64748b").grid(row=1, column=0, sticky="w", pady=2)
    tk.Label(prof_card, text=profile["cpu"][:30], font=("Segoe UI", 8), bg="#ffffff", fg="#334155").grid(row=1, column=1, sticky="w", padx=(6, 24), pady=2)

    tk.Label(prof_card, text="Kapasitas RAM:", font=("Segoe UI", 8), bg="#ffffff", fg="#64748b").grid(row=1, column=2, sticky="w", pady=2)
    tk.Label(prof_card, text=profile["ram"], font=("Segoe UI", 8), bg="#ffffff", fg="#334155").grid(row=1, column=3, sticky="w", padx=(6, 0), pady=2)

    # ================= FORM CARD =================
    form_card = tk.LabelFrame(
        body_frame,
        text="  PENGATURAN KONEKSI SERVER GURU  ",
        font=("Segoe UI", 8, "bold"),
        fg="#0284c7",
        bg="#ffffff",
        bd=1,
        relief=tk.SOLID,
        padx=14,
        pady=10
    )
    form_card.pack(fill=tk.X, pady=(0, 12))

    # Field 1: Server URL
    tk.Label(form_card, text="Alamat URL / IP Komputer Guru:", font=("Segoe UI", 9, "bold"), bg="#ffffff", fg="#0f172a").pack(anchor="w")
    tk.Label(form_card, text="Masukkan alamat IP komputer guru (misal: 10.161.17.165:8000 atau ws://10.161.17.165:8000/ws/agent)", font=("Segoe UI", 8), bg="#ffffff", fg="#64748b").pack(anchor="w", pady=(1, 4))

    ent_url = tk.Entry(form_card, font=("Segoe UI", 10), relief=tk.SOLID, bd=1, bg="#ffffff", fg="#0f172a")
    ent_url.insert(0, cfg.get("server_url", DEFAULT_SERVER_URL))
    ent_url.pack(fill=tk.X, ipady=4, pady=(0, 10))

    # Field 2: Lab Room
    tk.Label(form_card, text="Nama Ruang Laboratorium:", font=("Segoe UI", 9, "bold"), bg="#ffffff", fg="#0f172a").pack(anchor="w")
    cb_room = ttk.Combobox(
        form_card,
        values=["Lab RPL 1", "Lab RPL 2", "Lab CBT", "Lab Multimedia", "Lab Umum"],
        font=("Segoe UI", 9)
    )
    cb_room.set(cfg.get("lab_room", DEFAULT_LAB_ROOM))
    cb_room.pack(fill=tk.X, ipady=3, pady=(2, 6))

    # Checkbox Startup
    var_startup = tk.BooleanVar(value=True)
    chk_startup = tk.Checkbutton(
        form_card,
        text="Jalankan otomatis di latar belakang saat Windows dinyalakan (Windows Startup)",
        variable=var_startup,
        font=("Segoe UI", 9, "bold"),
        bg="#ffffff",
        fg="#0284c7",
        activebackground="#ffffff"
    )
    chk_startup.pack(anchor="w", pady=(4, 0))

    # ================= STATUS LIST =================
    feat_box = tk.Frame(body_frame, bg="#ffffff", relief=tk.SOLID, bd=1, padx=14, pady=8)
    feat_box.pack(fill=tk.X, pady=(0, 14))

    features = [
        "Berjalan di latar belakang (Background Stealth) tanpa jendela hitam CMD yang mengganggu",
        "Otomatis terhubung ke Web Dashboard Guru secara real-time via WebSocket",
        "Mendukung Kunci Layar (Lock Screen), Broadcast Pengumuman, & Telemetri CPU/RAM"
    ]
    for feat in features:
        row = tk.Frame(feat_box, bg="#ffffff")
        row.pack(anchor="w", pady=1)
        tk.Label(row, text="✓", font=("Segoe UI", 8, "bold"), bg="#ffffff", fg="#16a34a").pack(side=tk.LEFT, padx=(0, 6))
        tk.Label(row, text=feat, font=("Segoe UI", 8), bg="#ffffff", fg="#334155").pack(side=tk.LEFT)

    # ================= ACTIONS =================
    btn_bar = tk.Frame(body_frame, bg="#f8fafc")
    btn_bar.pack(fill=tk.X)

    def on_install_click():
        url_input = ent_url.get().strip()
        room_input = cb_room.get().strip() or "Lab RPL 1"
        final_url = parse_to_websocket_url(url_input)
        auto_start = var_startup.get()

        ok, msg = install_agent_to_system(final_url, room_input, auto_startup=auto_start)
        if ok:
            messagebox.showinfo(
                "Pemasangan Berhasil!",
                f"✅ Agen Siswa LabGuard PRO Berhasil Dipasang!\n\n"
                f"• Server: {final_url}\n"
                f"• Ruang: {room_input}\n"
                f"• Auto-Startup: {'Aktif' if auto_start else 'Nonaktif'}\n\n"
                f"Agen kini berjalan di latar belakang (background) dan siap dipantau oleh guru.\n"
                f"Jendela pemasang ini akan ditutup."
            )
            win.destroy()
            sys.exit(0)
        else:
            messagebox.showerror("Gagal Memasang", f"Terjadi kesalahan saat memasang:\n{msg}")

    def on_portable_run():
        url_input = ent_url.get().strip()
        room_input = cb_room.get().strip() or "Lab RPL 1"
        final_url = parse_to_websocket_url(url_input)
        save_agent_config(final_url, room_input)
        
        # Start in background without startup
        install_dir = get_app_data_dir()
        current_exe = sys.executable if getattr(sys, 'frozen', False) else os.path.abspath(__file__)
        start_agent_in_background(current_exe, install_dir)
        messagebox.showinfo("Berjalan di Latar Belakang", "Agen sedang berjalan di latar belakang untuk sesi ini.")
        win.destroy()
        sys.exit(0)

    def on_uninstall_click():
        if messagebox.askyesno("Copot Pemasangan", "Apakah Anda yakin ingin mencopot Agen Siswa LabGuard dari komputer ini?"):
            ok, msg = uninstall_agent_from_system()
            if ok:
                messagebox.showinfo("Selesai", "Agen berhasil dicopot dari sistem dan startup dinonaktifkan.")
                win.destroy()
                sys.exit(0)
            else:
                messagebox.showerror("Gagal", f"Gagal mencopot agen: {msg}")

    # Primary Action Button
    btn_install = tk.Button(
        btn_bar,
        text="🚀 Pasang & Jalankan di Latar Belakang",
        font=("Segoe UI", 10, "bold"),
        bg="#0284c7",
        fg="#ffffff",
        activebackground="#0369a1",
        activeforeground="#ffffff",
        relief=tk.FLAT,
        padx=18,
        pady=8,
        cursor="hand2",
        command=on_install_click
    )
    btn_install.pack(side=tk.LEFT, padx=(0, 10))

    btn_portable = tk.Button(
        btn_bar,
        text="▶️ Jalankan Saja",
        font=("Segoe UI", 9),
        bg="#e2e8f0",
        fg="#1e293b",
        relief=tk.FLAT,
        padx=12,
        pady=8,
        cursor="hand2",
        command=on_portable_run
    )
    btn_portable.pack(side=tk.LEFT, padx=(0, 10))

    btn_uninstall = tk.Button(
        btn_bar,
        text="🗑️ Copot",
        font=("Segoe UI", 9),
        bg="#fee2e2",
        fg="#b91c1c",
        relief=tk.FLAT,
        padx=10,
        pady=8,
        cursor="hand2",
        command=on_uninstall_click
    )
    btn_uninstall.pack(side=tk.LEFT, padx=(0, 10))

    btn_close = tk.Button(
        btn_bar,
        text="Tutup",
        font=("Segoe UI", 9),
        bg="#ffffff",
        fg="#64748b",
        relief=tk.SOLID,
        bd=1,
        padx=12,
        pady=7,
        cursor="hand2",
        command=win.destroy
    )
    btn_close.pack(side=tk.RIGHT)

    win.mainloop()


if __name__ == "__main__":
    # Check CLI options
    is_silent = any(arg in sys.argv for arg in ["--silent", "--background", "--auto", "--hidden", "-s"])
    is_uninstall = "--uninstall" in sys.argv
    is_install_cli = "--install" in sys.argv

    if is_uninstall:
        ok, msg = uninstall_agent_from_system()
        print(f"[UNINSTALL] {msg}")
        sys.exit(0)

    cfg = load_agent_config()

    if is_install_cli:
        # CLI install: Client_Siswa_Web.exe --install [--server ws://...] [--room "Lab RPL 1"]
        server_arg = cfg.get("server_url", DEFAULT_SERVER_URL)
        room_arg = cfg.get("lab_room", DEFAULT_LAB_ROOM)
        for i, a in enumerate(sys.argv):
            if a == "--server" and i + 1 < len(sys.argv):
                server_arg = sys.argv[i + 1]
            elif a == "--room" and i + 1 < len(sys.argv):
                room_arg = sys.argv[i + 1]
        final_ws_url = parse_to_websocket_url(server_arg)
        ok, msg = install_agent_to_system(final_ws_url, room_arg, auto_startup=True)
        print(f"[INSTALL CLI] {msg}")
        sys.exit(0)

    if is_silent:
        # BACKGROUND MODE: Silently run without any UI window
        server_url_raw = cfg.get("server_url", DEFAULT_SERVER_URL)
        lab_room = cfg.get("lab_room", DEFAULT_LAB_ROOM)
        for arg in sys.argv[1:]:
            if not arg.startswith("-"):
                server_url_raw = arg
                break
        final_ws_url = parse_to_websocket_url(server_url_raw)
        agent = LabClientAgent(server_url=final_ws_url, lab_room=lab_room)
        try:
            asyncio.run(agent.run())
        except KeyboardInterrupt:
            pass
        sys.exit(0)

    # Interactive GUI Setup / Installer
    show_installer_gui(cfg)
