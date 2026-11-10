# LabGuard PRO - Classroom & Computer Lab Monitoring System

Sistem Pemantauan dan Pengendalian Laboratorium Komputer Terintegrasi (Sekolah, Kampus, & Balai Latihan Kerja). Dilengkapi dengan dua pilihan antarmuka:
1. **🌐 Web Dashboard Modern (FastAPI + WebSocket + Vanilla JS/CSS)**: Dapat diakses via web browser dari laptop, PC admin, maupun tablet/HP.
2. **🖥️ Desktop Dashboard (wxPython)**: Antarmuka aplikasi desktop native untuk komputer guru.

---

## 🚀 Fitur Utama Sistem Monitoring Web (v2.0)

### 1. Dashboard Admin & Guru Berbasis Web ([`web/`](web/))
- **Live PC Grid & Real-time Snapshots**: Menampilkan seluruh PC siswa dengan thumbnail layar langsung, status online/offline, dan indikator aktivitas.
- **Visualisasi Telemetri Sistem Real-time**:
  - Penggunaan CPU (%) dan RAM (GB & %) dengan progress bar dinamis bergradasi warna (Hijau, Kuning, Merah).
  - Deteksi otomatis judul aplikasi/jendela yang sedang aktif di PC siswa (misal: CBT Browser, VS Code, atau game yang tidak diizinkan ⚠️).
- **📢 Pengumuman & Broadcast Layar Instan**:
  - Kirim pesan pop-up ke semua komputer atau PC terpilih.
  - Template siap pakai: *Ujian Dimulai*, *Waktu Sisa 10 Menit*, *Harap Perhatikan Guru*, *Kumpulkan Tugas*.
  - Mode darurat (*urgent*) disertai bunyi alarm audio sintetis Web Audio API dan pop-up merah tebal.
- **🔒 Penguncian Layar Anti-Distraksi (Lock / Unlock)**:
  - Mengunci layar PC siswa (*fullscreen topmost overlay*) agar siswa fokus memperhatikan instruksi guru di depan kelas.
- **🚀 Peluncuran Aplikasi Massal (Launch App)**:
  - Menjalankan aplikasi pembelajaran atau membuka tautan browser ujian CBT secara serentak di seluruh PC siswa.
- **🔍 Pemeriksaan Rinci & Process Manager**:
  - Membuka live view layar ukuran besar.
  - Menampilkan daftar proses/aplikasi yang sedang berjalan di PC siswa beserta penggunaan CPU & RAM.
  - Tombol **Hentikan (Kill Process)** untuk menutup paksa aplikasi atau game yang melanggar tata tertib lab.
- **⏻ Kontrol Daya Jarak Jauh**: Mematikan (*Shutdown*) atau me-restart (*Reboot*) komputer lab dengan jeda waktu aman.
- **📜 Catatan Aktivitas & Audit Logs**: Riwayat perintah yang dikirim guru dan log koneksi PC siswa tersimpan di database SQLite.
- **📱 Desain Responsif & Dark Theme Glassmorphism**: Tampilan ultra-modern dark mode dengan glassmorphism dan suara notifikasi Web Audio.

---

## 📁 Struktur Berkas

```
Monitoring/
├── server_web.py          # Backend Server Web & WebSocket (FastAPI + SQLite)
├── client_agent_web.py    # Client Agent Siswa Web (psutil, Pillow, WebSocket)
├── simulate_lab_clients.py# Simulator 12+ PC Siswa Virtual untuk Uji Coba Tanpa PC Fisik
├── web/                   # Frontend Dashboard Web
│   ├── index.html         # Struktur UI Web Dashboard
│   ├── style.css          # Desain Modern Dark Mode & Glassmorphism
│   └── app.js             # Logika WebSocket Client & Web Audio Synthesizer
│
├── JALANKAN_WEB_SERVER.bat# 1-Klik Jalankan Web Server & Buka Browser
├── JALANKAN_AGENT_WEB.bat # 1-Klik Jalankan Agent Siswa Web
├── SIMULASI_LAB_SISWA.bat # 1-Klik Menjalankan Simulasi 12 PC Siswa
│
├── index.php              # Bridge Launcher untuk Laragon & XAMPP (Auto-detect & Redirect)
├── SETUP_LARAGON.bat      # 1-Klik Setup & Integrasi Virtual Host Laragon
├── server_dashboard.py    # Desktop Dashboard Guru (wxPython)
├── client_agent.py        # Desktop Client Agent Siswa
├── gui_dashboard.py       # Pemindai Jaringan Agentless
├── network_engine.py      # Mesin ARP, Ping, WMI, WOL
├── requirements.txt       # Dependensi Python
└── README.md              # Dokumentasi teknis
```

---

## 🐘 Penggunaan Bersama Laragon / XAMPP

Sistem ini **didukung penuh** di lingkungan **Laragon** maupun **XAMPP**:
1. **Junction Otomatis**: Proyek ini dapat dihubungkan langsung ke `C:\laragon\www\Monitoring` dengan menjalankan **`SETUP_LARAGON.bat`**.
2. **Virtual Host Laragon**: Anda dapat mengakses dashboard melalui **`http://monitoring.test`** atau **`http://localhost:8000`**.
3. **Smart Bridge Launcher (`index.php`)**:
   - Jika Anda membuka `http://monitoring.test` atau `http://localhost/Monitoring` saat server Python aktif, Anda akan langsung dialihkan ke dashboard.
   - Jika server Python belum aktif, halaman antarmuka modern akan muncul dengan tombol **1-Klik "Jalankan Server Sekarang"** tanpa perlu membuka terminal secara manual.
4. **Auto-Detect Python Laragon**: Seluruh script `.bat` otomatis mendeteksi Python di lingkungan Laragon (`C:\laragon\bin\python\...\python.exe`).

---

## ⚙️ Panduan Menjalankan Sistem Web Modern (Rekomendasi)

### 1. Menjalankan Server & Dashboard Guru
Cukup klik dua kali:
👉 **`JALANKAN_WEB_SERVER.bat`**
- Server FastAPI akan berjalan pada port `8000`.
- Browser akan otomatis terbuka ke: **`http://localhost:8000`** (atau buka dari perangkat lain di jaringan yang sama melalui `http://[IP-KOMPUTER-GURU]:8000`).

### 2. Menguji Coba Dashboard dengan Simulasi Lab (Tanpa PC Fisik)
Jika Anda ingin melihat tampilan dashboard dengan 12 PC siswa virtual secara instan:
Cukup klik dua kali:
👉 **`SIMULASI_LAB_SISWA.bat`**
- 12 PC siswa virtual (`LAB1-PC01` s/d `LAB1-PC12`) akan langsung muncul di web dashboard dengan fluktuasi CPU, RAM, nama aplikasi, dan thumbnail layar!

### 3. Memasang di Komputer Siswa yang Sebenarnya
Di setiap komputer siswa dalam laboratorium:
Cukup klik dua kali:
👉 **`JALANKAN_AGENT_WEB.bat`**
- Masukkan IP komputer server/guru (contoh: `192.168.1.100`).
- Masukkan nama ruang lab (contoh: `Lab RPL 1`).
- Komputer siswa akan otomatis terhubung dan mulai memancarkan telemetri serta thumbnail layar ke dashboard guru.

---

## 🔌 Rute API Backend (REST & WebSocket)

| Endpoint | Protokol | Keterangan |
| --- | --- | --- |
| `/` | HTTP GET | Tampilan Web Dashboard Admin |
| `/ws/admin` | WebSocket | Saluran komunikasi real-time untuk browser admin |
| `/ws/agent` | WebSocket | Saluran komunikasi client agent siswa |
| `/api/stats` | HTTP GET | Ringkasan statistik lab (Total, Online, CPU/RAM rata-rata) |
| `/api/clients` | HTTP GET | Daftar seluruh PC siswa beserta statusnya |
| `/api/client/{id}` | HTTP GET | Rincian lengkap PC siswa beserta daftar proses aktif |
| `/api/command` | HTTP POST | Pengiriman perintah ke PC siswa (lock, unlock, broadcast, dll.) |
| `/api/logs` | HTTP GET | Riwayat log audit perintah guru |
