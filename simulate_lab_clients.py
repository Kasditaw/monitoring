"""
simulate_lab_clients.py - Simulated Computer Lab Student PCs
Spawns multiple virtual student PC clients (e.g., PC-01 to PC-16) connecting to
the FastAPI WebSocket Server.
Generates realistic fluctuating CPU/RAM telemetry, active application names,
and generates placeholder thumbnail canvases for testing the Web Dashboard!
"""

import asyncio
import base64
import io
import json
import random
import sys
import time
from typing import List

try:
    from PIL import Image, ImageDraw, ImageFont
except ImportError:
    Image = None

import websockets

DEFAULT_SERVER = "ws://127.0.0.1:8000/ws/agent"

SAMPLE_APPS = [
    ("chrome.exe", "Google Chrome - Ujian Online CBT Siswa"),
    ("code.exe", "Visual Studio Code - project_lab.py"),
    ("word.exe", "Microsoft Word - Laporan Praktikum"),
    ("packettracer.exe", "Cisco Packet Tracer - Jaringan Lab"),
    ("roblox.exe", "Roblox Client (⚠️ Game Dilarang)"),
    ("chrome.exe", "YouTube - Belajar Algoritma & Struktur Data"),
    ("idle.exe", "Python IDLE - Latihan 3"),
    ("calc.exe", "Calculator"),
]

LAB_ROOMS = ["Lab RPL 1", "Lab RPL 2", "Lab CBT"]

SIMULATED_INSTALLED_APPS = [
    {"name": "Google Chrome", "version": "128.0.6613.138", "publisher": "Google LLC", "install_location": "C:\\Program Files\\Google\\Chrome\\Application", "exe": "chrome.exe"},
    {"name": "Microsoft Word 2021", "version": "16.0.14332.20542", "publisher": "Microsoft Corporation", "install_location": "C:\\Program Files\\Microsoft Office\\root\\Office16", "exe": "winword.exe"},
    {"name": "Microsoft Excel 2021", "version": "16.0.14332.20542", "publisher": "Microsoft Corporation", "install_location": "C:\\Program Files\\Microsoft Office\\root\\Office16", "exe": "excel.exe"},
    {"name": "Microsoft PowerPoint 2021", "version": "16.0.14332.20542", "publisher": "Microsoft Corporation", "install_location": "C:\\Program Files\\Microsoft Office\\root\\Office16", "exe": "powerpnt.exe"},
    {"name": "Visual Studio Code", "version": "1.93.1", "publisher": "Microsoft Corporation", "install_location": "C:\\Users\\Student\\AppData\\Local\\Programs\\Microsoft VS Code", "exe": "code.exe"},
    {"name": "Python 3.12.6 (64-bit)", "version": "3.12.6150.0", "publisher": "Python Software Foundation", "install_location": "C:\\Program Files\\Python312", "exe": "python.exe"},
    {"name": "Cisco Packet Tracer 8.2", "version": "8.2.1.0118", "publisher": "Cisco Systems, Inc.", "install_location": "C:\\Program Files\\Cisco Packet Tracer 8.2\\bin", "exe": "PacketTracer.exe"},
    {"name": "Scratch 3 Desktop", "version": "3.29.1", "publisher": "MIT Media Lab", "install_location": "C:\\Program Files\\Scratch 3", "exe": "Scratch 3.exe"},
    {"name": "GeoGebra Classic 6", "version": "6.0.785.0", "publisher": "International GeoGebra Institute", "install_location": "C:\\Users\\Student\\AppData\\Local\\GeoGebra", "exe": "GeoGebra.exe"},
    {"name": "VLC Media Player", "version": "3.0.20", "publisher": "VideoLAN", "install_location": "C:\\Program Files\\VideoLAN\\VLC", "exe": "vlc.exe"},
    {"name": "WinRAR 7.01 (64-bit)", "version": "7.01.0", "publisher": "win.rar GmbH", "install_location": "C:\\Program Files\\WinRAR", "exe": "WinRAR.exe"},
    {"name": "Notepad++ (64-bit)", "version": "8.6.9", "publisher": "Notepad++ Team", "install_location": "C:\\Program Files\\Notepad++", "exe": "notepad++.exe"}
]


def generate_fake_thumbnail(pc_name: str, app_name: str, cpu: float, ram: float, is_locked: bool) -> str:
    """Generates an in-memory test thumbnail image."""
    width, height = 360, 220
    if not Image:
        return ""
    
    bg_color = (20, 25, 38) if not is_locked else (15, 23, 42)
    img = Image.new("RGB", (width, height), color=bg_color)
    draw = ImageDraw.Draw(img)

    # Top title bar
    bar_color = (30, 41, 59)
    draw.rectangle([(0, 0), (width, 28)], fill=bar_color)
    draw.text((10, 6), f"🖥️  {pc_name} Desktop", fill=(226, 232, 240))

    if is_locked:
        # Draw lock graphics
        draw.rectangle([(30, 50), (width - 30, height - 30)], outline=(245, 158, 11), width=2, fill=(24, 30, 48))
        draw.text((width // 2 - 50, 80), "🔒 LAYAR DIKUNCI", fill=(245, 158, 11))
        draw.text((width // 2 - 80, 115), "Harap perhatikan Guru", fill=(148, 163, 184))
    else:
        # App window mockup
        draw.rectangle([(25, 45), (width - 25, height - 25)], fill=(30, 38, 56), outline=(71, 85, 105), width=1)
        draw.rectangle([(25, 45), (width - 25, 70)], fill=(45, 55, 75))
        draw.text((35, 50), f"Active: {app_name[:30]}", fill=(190, 215, 240))

        # Metrics bar inside thumbnail
        cpu_color = (239, 68, 68) if cpu > 80 else (16, 185, 129)
        draw.text((35, 85), f"CPU: {cpu}%", fill=cpu_color)
        draw.text((35, 105), f"RAM: {ram}%", fill=(56, 189, 248))
        draw.text((35, 140), "Sistem Berjalan Normal", fill=(100, 116, 139))

    buf = io.BytesIO()
    img.save(buf, format="JPEG", quality=55)
    buf.seek(0)
    return base64.b64encode(buf.read()).decode("ascii")


def check_server_wake(server_url: str, client_id: str) -> bool:
    try:
        import urllib.request
        base_http = server_url.replace("/ws/agent", "").replace("ws://", "http://").replace("wss://", "https://")
        req_url = f"{base_http}/api/simulation/wake_check/{client_id}"
        req = urllib.request.Request(req_url, headers={"User-Agent": "SimClient/1.0"})
        with urllib.request.urlopen(req, timeout=2.0) as resp:
            data = json.loads(resp.read().decode())
            return bool(data.get("wake", False))
    except Exception:
        return False


async def simulate_student_pc(pc_index: int, total_pcs: int, server_url: str):
    pc_name = f"LAB1-PC{pc_index:02d}"
    ip_addr = f"192.168.10.{pc_index + 10}"
    mac_addr = f"52:54:00:{pc_index:02x}:12:34"
    room = LAB_ROOMS[pc_index % len(LAB_ROOMS)]
    
    app_choice = SAMPLE_APPS[pc_index % len(SAMPLE_APPS)]
    active_app = f"{app_choice[0]} ({app_choice[1]})"
    
    cpu = random.uniform(15.0, 45.0)
    ram = random.uniform(40.0, 75.0)
    is_locked = 0
    is_remote_active = False
    is_powered_on = True

    while True:
        if not is_powered_on:
            # PC virtual dalam kondisi mati/standby. Menunggu sinyal Wake-on-LAN (WOL)
            await asyncio.sleep(2.0)
            should_wake = await asyncio.to_thread(check_server_wake, server_url, pc_name)
            if should_wake:
                print(f"[{pc_name}] Sinyal Wake-on-LAN diterima! Menyalakan PC virtual...")
                is_powered_on = True
            continue

        try:
            async with websockets.connect(server_url) as ws:
                # 1. Handshake
                handshake = {
                    "type": "handshake",
                    "client_id": pc_name,
                    "hostname": pc_name,
                    "ip_address": ip_addr,
                    "mac_address": mac_addr,
                    "lab_room": room,
                    "os_info": "Windows 11 Pro 64-bit",
                    "agent_version": "v2.1.0",
                    "ram_total_mb": 16384,
                    "is_locked": is_locked
                }
                await ws.send(json.dumps(handshake))
                await ws.recv()  # ack

                # Send initial installed applications list
                await ws.send(json.dumps({
                    "type": "installed_apps",
                    "apps": SIMULATED_INSTALLED_APPS
                }))

                while True:
                    # Fluctuate metrics realistically
                    cpu = max(5.0, min(98.0, cpu + random.uniform(-8.0, 8.0)))
                    ram = max(25.0, min(95.0, ram + random.uniform(-2.0, 2.0)))
                    
                    # Occasionally switch running app
                    if random.random() < 0.15:
                        app_choice = random.choice(SAMPLE_APPS)
                        active_app = f"{app_choice[0]} ({app_choice[1]})"

                    # Diagnostic health simulation
                    disk_free = 42.5 if "PC-01" not in pc_name else 7.8
                    score = 98 if disk_free > 10 else 78
                    h_status = "good" if score >= 90 else "warning"
                    h_issues = [] if h_status == "good" else [f"Disk C: sisa {disk_free} GB"]

                    telem = {
                        "type": "telemetry",
                        "cpu_pct": round(cpu, 1),
                        "ram_pct": round(ram, 1),
                        "ram_used_mb": round((ram / 100.0) * 16384, 1),
                        "ram_total_mb": 16384,
                        "disk_pct": 52.4 if disk_free > 10 else 92.0,
                        "disk_free_gb": disk_free,
                        "disk_total_gb": 120.0,
                        "uptime_seconds": 14200,
                        "health_score": score,
                        "health_status": h_status,
                        "health_issues": h_issues,
                        "active_app": active_app,
                        "is_locked": is_locked,
                        "processes": [
                            {"pid": 1001, "name": app_choice[0], "cpu": round(cpu * 0.6, 1), "memory_mb": 420.5},
                            {"pid": 1002, "name": "explorer.exe", "cpu": 1.2, "memory_mb": 180.2},
                            {"pid": 1003, "name": "svchost.exe", "cpu": 0.8, "memory_mb": 95.0},
                            {"pid": 1004, "name": "system", "cpu": 0.5, "memory_mb": 45.1}
                        ]
                    }
                    await ws.send(json.dumps(telem))

                    # Send thumbnail
                    thumb = generate_fake_thumbnail(pc_name, active_app, round(cpu, 1), round(ram, 1), bool(is_locked))
                    if thumb:
                        await ws.send(json.dumps({"type": "screen", "thumbnail": thumb}))

                    # Check for server commands (non-blocking with timeout)
                    timeout_val = 0.15 if is_remote_active else 2.0
                    try:
                        cmd_raw = await asyncio.wait_for(ws.recv(), timeout=timeout_val)
                        cmd = json.loads(cmd_raw)
                        mtype = cmd.get("type")
                        if mtype == "command":
                            action = cmd.get("action")
                            print(f"[{pc_name}] Menerima perintah: {action}")
                            if action == "lock":
                                is_locked = 1
                            elif action == "unlock":
                                is_locked = 0
                            elif action == "clean_temp":
                                try:
                                    await ws.send(json.dumps({
                                        "type": "command_result",
                                        "action": action,
                                        "status": "DONE",
                                        "message": f"Pembersihan {pc_name} sukses! Berhasil membebaskan 1,420 MB ruang harddisk C:"
                                    }))
                                except Exception:
                                    pass
                            elif action == "shutdown":
                                print(f"[{pc_name}] Komputer virtual dimatikan oleh Guru (shutdown)")
                                try:
                                    await ws.send(json.dumps({
                                        "type": "command_result",
                                        "action": action,
                                        "status": "DONE",
                                        "message": f"{pc_name} berhasil dimatikan (shutdown)"
                                    }))
                                except Exception:
                                    pass
                                is_powered_on = False
                                await ws.close()
                                break
                            elif action == "restart":
                                print(f"[{pc_name}] Komputer virtual me-restart...")
                                try:
                                    await ws.send(json.dumps({
                                        "type": "command_result",
                                        "action": action,
                                        "status": "DONE",
                                        "message": f"{pc_name} sedang me-restart..."
                                    }))
                                except Exception:
                                    pass
                                await ws.close()
                                await asyncio.sleep(4.0)
                                break
                            
                            elif action == "get_installed_apps":
                                await ws.send(json.dumps({
                                    "type": "installed_apps",
                                    "apps": SIMULATED_INSTALLED_APPS
                                }))
                                await ws.send(json.dumps({
                                    "type": "command_result",
                                    "action": action,
                                    "status": "DONE",
                                    "message": f"Ditemukan {len(SIMULATED_INSTALLED_APPS)} aplikasi terpasang di {pc_name}"
                                }))
                                continue

                            # Send acknowledgment for other commands
                            await ws.send(json.dumps({
                                "type": "command_result",
                                "action": action,
                                "status": "DONE",
                                "message": f"{pc_name} berhasil memproses {action}"
                            }))
                        elif mtype == "start_remote":
                            is_remote_active = True
                            print(f"[{pc_name}] Remote control aktif")
                            await ws.send(json.dumps({"type": "remote_started", "client_id": pc_name}))
                            await ws.send(json.dumps({
                                "type": "remote_frame",
                                "frame": thumb,
                                "timestamp": time.time()
                            }))
                        elif mtype == "stop_remote":
                            is_remote_active = False
                            print(f"[{pc_name}] Remote control nonaktif")
                            await ws.send(json.dumps({"type": "remote_stopped", "client_id": pc_name}))
                        elif mtype == "remote_input":
                            await ws.send(json.dumps({
                                "type": "remote_frame",
                                "frame": thumb,
                                "timestamp": time.time()
                            }))
                    except asyncio.TimeoutError:
                        if is_remote_active:
                            await ws.send(json.dumps({
                                "type": "remote_frame",
                                "frame": thumb,
                                "timestamp": time.time()
                            }))

        except Exception as e:
            # Reconnect delay
            await asyncio.sleep(3.0)


async def main():
    total_pcs = int(sys.argv[1]) if len(sys.argv) > 1 else 12
    server_url = sys.argv[2] if len(sys.argv) > 2 else DEFAULT_SERVER
    print(f"=====================================================")
    print(f"  SIMULATOR LABORATORIUM KOMPUTER                    ")
    print(f"  Menjalankan {total_pcs} PC Siswa Virtual             ")
    print(f"  Target Server: {server_url}                        ")
    print(f"=====================================================")
    
    tasks = [simulate_student_pc(i + 1, total_pcs, server_url) for i in range(total_pcs)]
    await asyncio.gather(*tasks)


if __name__ == "__main__":
    try:
        asyncio.run(main())
    except KeyboardInterrupt:
        print("\n[SIMULATOR] Dihentikan.")
