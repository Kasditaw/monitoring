"""
client_agent.py - Client Agent for Classroom Monitor / Lab Dashboard.
Runs on student / client PCs and connects to Teacher Dashboard (Server).

Features implemented based on Dashboard controls:
1. Authentication: Handshake with Room ID & Access Key.
2. Live Screen Streaming: Captures and sends screen thumbnails for Dashboard zoom grid.
3. Lock Screen / Unlock Screen: Fullscreen modal lock overlay blocking student distraction.
4. Launch App: Remotely launches applications (e.g. notepad.exe, browser).
5. Send File / Send Folder: Receives files/folders from teacher and saves to Desktop.
6. Power Off: Safely shuts down client PC when instructed by teacher.
7. Auto-Reconnect: Robust background connection loop.
"""

import io
import json
import os
import socket
import struct
import subprocess
import sys
import threading
import time
import zipfile
from typing import Optional

try:
    from PIL import Image, ImageGrab
except ImportError:
    print("[ERROR] Pillow library not found. Install via: pip install Pillow")
    sys.exit(1)

import tkinter as tk
from tkinter import messagebox


# Default Configuration
DEFAULT_SERVER_IP = "127.0.0.1"  # Ganti dengan IP komputer Server / Guru
DEFAULT_SERVER_PORT = 8888
DEFAULT_ROOM_ID = "labs"
DEFAULT_ACCESS_KEY = "siswa123"
THUMBNAIL_SIZE = (400, 250)      # Ukuran snapshot hemat bandwidth
FPS_INTERVAL = 1.0               # Kirim layar tiap 1 detik


class LockScreenOverlay:
    """Jendela layar penuh (fullscreen topmost) untuk mengunci komputer siswa."""
    def __init__(self):
        self.root: Optional[tk.Tk] = None
        self.is_locked = False
        self._thread: Optional[threading.Thread] = None

    def show(self, message: str = "Layar Komputer Ini Dikunci oleh Guru / Administrator"):
        if self.is_locked:
            return
        self.is_locked = True

        def _run_gui():
            self.root = tk.Tk()
            self.root.title("Classroom Monitor - Screen Locked")
            self.root.attributes("-fullscreen", True)
            self.root.attributes("-topmost", True)
            self.root.configure(background="#0F172A")  # Dark modern slate

            # Mencegah penutupan via Alt+F4
            self.root.protocol("WM_DELETE_WINDOW", lambda: None)

            # Container tengah
            frame = tk.Frame(self.root, bg="#0F172A")
            frame.place(relx=0.5, rely=0.5, anchor=tk.CENTER)

            icon_lbl = tk.Label(frame, text="🔒", font=("Segoe UI Emoji", 64), bg="#0F172A", fg="#F59E0B")
            icon_lbl.pack(pady=10)

            title_lbl = tk.Label(
                frame,
                text="LAYAR DIKUNCI",
                font=("Helvetica", 28, "bold"),
                bg="#0F172A",
                fg="#F8FAFC"
            )
            title_lbl.pack(pady=5)

            msg_lbl = tk.Label(
                frame,
                text=message,
                font=("Helvetica", 14),
                bg="#0F172A",
                fg="#94A3B8"
            )
            msg_lbl.pack(pady=10)

            hint_lbl = tk.Label(
                frame,
                text="Harap perhatikan penjelasan guru di depan kelas.",
                font=("Helvetica", 11, "italic"),
                bg="#0F172A",
                fg="#64748B"
            )
            hint_lbl.pack(pady=15)

            # Bind tombol darurat untuk administrator (Ctrl + Shift + Alt + U)
            self.root.bind("<Control-Alt-Shift-U>", lambda e: self.hide())

            self.root.mainloop()

        self._thread = threading.Thread(target=_run_gui, daemon=True)
        self._thread.start()

    def hide(self):
        if not self.is_locked:
            return
        self.is_locked = False
        if self.root:
            try:
                self.root.after(0, self.root.destroy)
            except Exception:
                pass


class BroadcastMessageOverlay:
    """Jendela pop-up modal di layar siswa untuk menampilkan pesan siaran/pengumuman dari guru."""
    def __init__(self):
        pass

    def show(self, title: str = "Pemberitahuan dari Guru", message: str = "", urgent: bool = False):
        def _run():
            try:
                if sys.platform == "win32":
                    try:
                        import winsound
                        winsound.MessageBeep(winsound.MB_ICONEXCLAMATION)
                    except Exception:
                        pass

                win = tk.Tk()
                win.title(title)
                win.attributes("-topmost", True)
                win_bg = "#1E293B" if urgent else "#FFFFFF"
                win.configure(bg=win_bg)

                # Ukuran dan posisi di tengah layar
                w, h = 520, 340
                sw = win.winfo_screenwidth()
                sh = win.winfo_screenheight()
                x = max(0, (sw - w) // 2)
                y = max(0, (sh - h) // 2)
                win.geometry(f"{w}x{h}+{x}+{y}")
                win.resizable(False, False)

                # Header Banner
                hdr_bg = "#DC2626" if urgent else "#2563EB"
                hdr = tk.Frame(win, bg=hdr_bg, height=54)
                hdr.pack(fill=tk.X)
                hdr.pack_propagate(False)

                hdr_icon = "🚨 PENGUMUMAN PENTING GURU" if urgent else "📢 PESAN DARI GURU / PENGAWAS LAB"
                lbl_hdr = tk.Label(hdr, text=hdr_icon, font=("Helvetica", 12, "bold"), fg="#FFFFFF", bg=hdr_bg)
                lbl_hdr.pack(pady=15)

                # Body Pesan
                body_bg = "#0F172A" if urgent else "#F8FAFC"
                body_frame = tk.Frame(win, bg=body_bg, padx=24, pady=18)
                body_frame.pack(fill=tk.BOTH, expand=True)

                lbl_msg = tk.Label(
                    body_frame,
                    text=message,
                    font=("Helvetica", 12),
                    bg=body_bg,
                    fg="#F8FAFC" if urgent else "#1E293B",
                    wraplength=460,
                    justify=tk.LEFT
                )
                lbl_msg.pack(fill=tk.BOTH, expand=True)

                # Footer Tombol Tutup
                foot_frame = tk.Frame(win, bg=body_bg, pady=12)
                foot_frame.pack(fill=tk.X)

                btn_dismiss = tk.Button(
                    foot_frame,
                    text="✓ Saya Mengerti (Tutup Pesan)",
                    font=("Helvetica", 10, "bold"),
                    bg="#10B981",
                    fg="#FFFFFF",
                    activebackground="#059669",
                    activeforeground="#FFFFFF",
                    relief=tk.FLAT,
                    padx=18,
                    pady=6,
                    cursor="hand2",
                    command=win.destroy
                )
                btn_dismiss.pack()

                win.focus_force()
                win.mainloop()
            except Exception as e:
                print(f"[!] Gagal memunculkan dialog broadcast: {e}")

        threading.Thread(target=_run, daemon=True).start()


class ClientAgent:
    """Agent penghubung antara PC Siswa/Klien dan Dashboard Guru."""

    def __init__(self, server_ip: str, port: int, room_id: str, access_key: str):
        self.server_ip = server_ip
        self.port = port
        self.room_id = room_id
        self.access_key = access_key
        self.hostname = socket.gethostname()

        self.sock: Optional[socket.socket] = None
        self.running = True
        self.is_authenticated = False
        self.lock_overlay = LockScreenOverlay()
        self.broadcast_overlay = BroadcastMessageOverlay()

        # Direktori penyimpanan file yang dikirim guru
        self.desktop_dir = os.path.join(os.path.expanduser("~"), "Desktop", "Materi_Guru")
        os.makedirs(self.desktop_dir, exist_ok=True)

    def start(self):
        """Memulai loop koneksi dengan mekanisme reconnect otomatis."""
        print(f"[*] Client Agent Dimulai...")
        print(f"[*] Menghubungkan ke Server: {self.server_ip}:{self.port}")
        print(f"[*] Room ID: '{self.room_id}' | Hostname: '{self.hostname}'")

        while self.running:
            try:
                self.sock = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
                self.sock.settimeout(10.0)
                self.sock.connect((self.server_ip, self.port))
                self.sock.settimeout(None)

                print(f"[+] Berhasil terhubung ke Server {self.server_ip}:{self.port}")

                # 1. Kirim Handshake Otentikasi
                if self._handshake():
                    self.is_authenticated = True
                    # Thread 1: Kirim Streaming Snapshot Layar
                    stream_thread = threading.Thread(target=self._screen_stream_loop, daemon=True)
                    stream_thread.start()

                    # Thread 2: Dengarkan Perintah dari Server
                    self._command_listen_loop()
            except (ConnectionRefusedError, socket.timeout, OSError) as e:
                print(f"[-] Tidak dapat terhubung ke server ({e}). Mencoba lagi dalam 4 detik...")
                time.sleep(4)
            except Exception as e:
                print(f"[!] Error koneksi: {e}. Reconnecting...")
                time.sleep(4)
            finally:
                self.is_authenticated = False
                if self.sock:
                    try:
                        self.sock.close()
                    except Exception:
                        pass

    def _send_packet(self, packet_type: str, payload: dict, extra_data: bytes = b""):
        """
        Protokol Paket Biner:
        [4 byte Length Header][Header JSON][Binary Data (Image / File)]
        """
        payload["type"] = packet_type
        header_bytes = json.dumps(payload).encode("utf-8")
        header_len = len(header_bytes)
        extra_len = len(extra_data)

        # Format: 4 byte header_length, 4 byte extra_data_length
        prefix = struct.pack(">II", header_len, extra_len)
        self.sock.sendall(prefix + header_bytes + extra_data)

    def _read_packet(self) -> tuple[dict, bytes]:
        """Membaca satu paket utuh dari socket."""
        prefix = self._recv_exact(8)
        header_len, extra_len = struct.unpack(">II", prefix)

        header_bytes = self._recv_exact(header_len)
        header = json.loads(header_bytes.decode("utf-8"))

        extra_data = b""
        if extra_len > 0:
            extra_data = self._recv_exact(extra_len)

        return header, extra_data

    def _recv_exact(self, num_bytes: int) -> bytes:
        """Helper membaca persis N byte dari socket buffer."""
        data = bytearray()
        while len(data) < num_bytes:
            packet = self.sock.recv(num_bytes - len(data))
            if not packet:
                raise ConnectionResetError("Server connection lost")
            data.extend(packet)
        return bytes(data)

    def _handshake(self) -> bool:
        """Kirim autentikasi Room ID dan Access Key ke Teacher Dashboard."""
        auth_data = {
            "room_id": self.room_id,
            "access_key": self.access_key,
            "hostname": self.hostname,
            "os": sys.platform
        }
        self._send_packet("AUTH_REQUEST", auth_data)

        # Terima respon
        resp, _ = self._read_packet()
        if resp.get("status") == "OK":
            print(f"[+] Autentikasi Berhasil! Terdaftar di Room '{self.room_id}'")
            return True
        else:
            print(f"[-] Autentikasi Gagal: {resp.get('message', 'Akses ditolak')}")
            return False

    def _screen_stream_loop(self):
        """Mengambil screenshot, kompres ke JPEG kecil, dan kirim ke Guru secara berkala."""
        while self.running and self.is_authenticated:
            try:
                # Ambil tangkapan layar
                screenshot = ImageGrab.grab()
                # Resize menjadi ukuran thumbnail hemat bandwidth
                thumbnail = screenshot.resize(THUMBNAIL_SIZE, Image.Resampling.BILINEAR)

                # Simpan ke byte buffer format JPEG
                buf = io.BytesIO()
                thumbnail.save(buf, format="JPEG", quality=50, optimize=True)
                jpg_data = buf.getvalue()

                # Kirim paket SCREEN_UPDATE
                meta = {
                    "width": THUMBNAIL_SIZE[0],
                    "height": THUMBNAIL_SIZE[1],
                    "timestamp": time.time()
                }
                self._send_packet("SCREEN_UPDATE", meta, extra_data=jpg_data)

                time.sleep(FPS_INTERVAL)
            except Exception as e:
                # Jika socket putus, biarkan loop perintah menangani reconnect
                break

    def _command_listen_loop(self):
        """Mendengarkan instruksi tombol aksi dari Teacher Dashboard."""
        while self.running:
            header, extra_data = self._read_packet()
            action = header.get("action")
            print(f"[*] Menerima Perintah dari Guru: {action}")

            # 1. LOCK SCREEN
            if action == "LOCK_SCREEN":
                msg = header.get("message", "Layar Komputer Ini Dikunci oleh Guru / Administrator")
                self.lock_overlay.show(message=msg)

            # 2. UNLOCK SCREEN
            elif action == "UNLOCK_SCREEN":
                self.lock_overlay.hide()

            # 3. LAUNCH APP
            elif action == "LAUNCH_APP":
                app_cmd = header.get("command", "notepad.exe")
                self._execute_app(app_cmd)

            # 4. POWER OFF
            elif action == "POWER_OFF":
                self._power_off()

            # 5. SEND FILE
            elif action == "SEND_FILE":
                filename = header.get("filename", "file_dari_guru.bin")
                self._save_file(filename, extra_data)

            # 6. SEND FOLDER (ZIP)
            elif action == "SEND_FOLDER":
                folder_name = header.get("folder_name", "Folder_Materi")
                self._extract_folder(folder_name, extra_data)

            # 7. BROADCAST MESSAGE
            elif action == "BROADCAST_MESSAGE":
                title = header.get("title", "Pesan dari Guru / Pengawas Lab")
                msg = header.get("message", "")
                urgent = header.get("urgent", False)
                self.broadcast_overlay.show(title=title, message=msg, urgent=urgent)

    def _execute_app(self, cmd: str):
        """Menjalankan program di PC Klien."""
        try:
            print(f"[+] Menjalankan aplikasi: {cmd}")
            subprocess.Popen(cmd, shell=True)
        except Exception as e:
            print(f"[-] Gagal menjalankan aplikasi {cmd}: {e}")

    def _power_off(self):
        """Mematikan komputer siswa."""
        print("[!] Perintah Power Off diterima!")
        if sys.platform == "win32":
            subprocess.run(["shutdown", "/s", "/f", "/t", "5", "/c", "Komputer dimatikan oleh Guru."], check=False)
        else:
            subprocess.run(["shutdown", "-h", "now"], check=False)

    def _save_file(self, filename: str, data: bytes):
        """Menyimpan file yang dikirim guru ke Desktop/Materi_Guru."""
        try:
            target_path = os.path.join(self.desktop_dir, filename)
            with open(target_path, "wb") as f:
                f.write(data)
            print(f"[+] File berhasil disimpan: {target_path}")
        except Exception as e:
            print(f"[-] Gagal menyimpan file: {e}")

    def _extract_folder(self, folder_name: str, zip_bytes: bytes):
        """Mengekstrak berkas folder zip yang dikirim guru."""
        try:
            target_dir = os.path.join(self.desktop_dir, folder_name)
            os.makedirs(target_dir, exist_ok=True)
            with zipfile.ZipFile(io.BytesIO(zip_bytes)) as z:
                z.extractall(target_dir)
            print(f"[+] Folder berhasil diekstrak ke: {target_dir}")
        except Exception as e:
            print(f"[-] Gagal ekstrak folder: {e}")


def auto_detect_teacher(timeout: float = 2.0) -> Optional[dict]:
    """Mendengarkan beacon UDP dari Dashboard Guru di jaringan lokal."""
    try:
        sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        sock.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
        sock.bind(("", 8889))
        sock.settimeout(timeout)
        data, addr = sock.recvfrom(1024)
        info = json.loads(data.decode("utf-8"))
        if info.get("service") == "CLASSROOM_MONITOR_TEACHER":
            info["teacher_ip"] = addr[0]
            sock.close()
            return info
        sock.close()
    except Exception:
        pass
    return None


def launch_agent_gui():
    """Jendela konfigurasi awal sebelum agent terkoneksi, dengan fitur Auto-Detect Guru."""
    config_file = os.path.join(os.path.dirname(__file__), "agent_config.json")
    cfg = {
        "server_ip": DEFAULT_SERVER_IP,
        "port": DEFAULT_SERVER_PORT,
        "room_id": DEFAULT_ROOM_ID,
        "access_key": DEFAULT_ACCESS_KEY
    }
    if os.path.exists(config_file):
        try:
            with open(config_file, "r") as f:
                cfg.update(json.load(f))
        except Exception:
            pass

    # Mode Otomatis (Headless / Startup): misal dipanggil dengan argumen "python client_agent.py --auto"
    if "--auto" in sys.argv or "--silent" in sys.argv:
        print("[*] Mode Otomatis aktif. Mencari Komputer Guru di jaringan lab...")
        while True:
            found = auto_detect_teacher(timeout=3.0)
            if found:
                print(f"[+] Komputer Guru ditemukan di {found['teacher_ip']}. Menghubungkan...")
                agent = ClientAgent(
                    server_ip=found["teacher_ip"],
                    port=found.get("tcp_port", cfg["port"]),
                    room_id=found.get("room_id", cfg["room_id"]),
                    access_key=found.get("access_key", cfg["access_key"])
                )
                agent.start()
                break
            time.sleep(2)
        return

    # Jendela konfigurasi tkinter yang sangat mudah untuk siswa
    app = tk.Tk()
    app.title("Classroom Monitor - Komputer Siswa")
    app.geometry("420x370")
    app.resizable(False, False)
    app.configure(bg="#F8FAFC")

    tk.Label(app, text="🖥️ Komputer Siswa (Client Lab)", font=("Helvetica", 14, "bold"),
             bg="#F8FAFC", fg="#1E293B").pack(pady=8)

    lbl_status = tk.Label(app, text="Klik 'Cari Otomatis' untuk mendeteksi IP Guru di lab.",
                          font=("Helvetica", 9), bg="#F8FAFC", fg="#64748B")
    lbl_status.pack(pady=2)

    frm = tk.Frame(app, bg="#F8FAFC")
    frm.pack(padx=24, pady=8, fill=tk.BOTH)

    tk.Label(frm, text="IP Komputer Guru:", bg="#F8FAFC", font=("Helvetica", 9, "bold")).grid(row=0, column=0, sticky=tk.W, pady=4)
    ent_ip = tk.Entry(frm, width=22, font=("Helvetica", 10))
    ent_ip.insert(0, cfg["server_ip"])
    ent_ip.grid(row=0, column=1, pady=4)

    tk.Label(frm, text="Port Server:", bg="#F8FAFC").grid(row=1, column=0, sticky=tk.W, pady=4)
    ent_port = tk.Entry(frm, width=22, font=("Helvetica", 10))
    ent_port.insert(0, str(cfg["port"]))
    ent_port.grid(row=1, column=1, pady=4)

    tk.Label(frm, text="Ruang Lab (Room ID):", bg="#F8FAFC").grid(row=2, column=0, sticky=tk.W, pady=4)
    ent_room = tk.Entry(frm, width=22, font=("Helvetica", 10))
    ent_room.insert(0, cfg["room_id"])
    ent_room.grid(row=2, column=1, pady=4)

    tk.Label(frm, text="Kunci Akses (Key):", bg="#F8FAFC").grid(row=3, column=0, sticky=tk.W, pady=4)
    ent_key = tk.Entry(frm, width=22, font=("Helvetica", 10))
    ent_key.insert(0, cfg["access_key"])
    ent_key.grid(row=3, column=1, pady=4)

    def on_detect():
        lbl_status.config(text="🔍 Memindai sinyal Komputer Guru di lab...", fg="#2563EB")
        app.update()
        found = auto_detect_teacher(timeout=2.5)
        if found:
            ent_ip.delete(0, tk.END)
            ent_ip.insert(0, found["teacher_ip"])
            if "room_id" in found:
                ent_room.delete(0, tk.END)
                ent_room.insert(0, found["room_id"])
            if "access_key" in found:
                ent_key.delete(0, tk.END)
                ent_key.insert(0, found["access_key"])
            lbl_status.config(
                text=f"✓ Ditemukan: {found['teacher_ip']} ({found.get('hostname', 'Guru')})",
                fg="#16A34A"
            )
        else:
            lbl_status.config(
                text="❌ Belum terdeteksi. Pastikan Guru sudah klik 'Start Server'.",
                fg="#DC2626"
            )

    btn_detect = tk.Button(app, text="🔍 Cari Komputer Guru Otomatis", bg="#F1F5F9", fg="#1E293B",
                           font=("Helvetica", 9, "bold"), relief=tk.GROOVE, command=on_detect, cursor="hand2")
    btn_detect.pack(pady=4, padx=24, fill=tk.X)

    def on_connect():
        ip = ent_ip.get().strip()
        try:
            port = int(ent_port.get().strip())
        except ValueError:
            messagebox.showerror("Error", "Port harus berupa angka.")
            return

        room = ent_room.get().strip()
        key = ent_key.get().strip()

        # Simpan konfigurasi
        with open(config_file, "w") as f:
            json.dump({"server_ip": ip, "port": port, "room_id": room, "access_key": key}, f)

        app.destroy()

        agent = ClientAgent(server_ip=ip, port=port, room_id=room, access_key=key)
        agent.start()

    btn_connect = tk.Button(app, text="🚀 Hubungkan ke Guru Sekarang", bg="#2563EB", fg="white",
                            font=("Helvetica", 10, "bold"), command=on_connect, height=2, cursor="hand2")
    btn_connect.pack(pady=10, padx=24, fill=tk.X)

    # Coba deteksi otomatis di awal jika masih default 127.0.0.1
    def initial_scan():
        found = auto_detect_teacher(timeout=1.0)
        if found:
            ent_ip.delete(0, tk.END)
            ent_ip.insert(0, found["teacher_ip"])
            lbl_status.config(text=f"✓ Komputer Guru Ditemukan: {found['teacher_ip']}", fg="#16A34A")

    app.after(500, initial_scan)
    app.mainloop()


if __name__ == "__main__":
    launch_agent_gui()
