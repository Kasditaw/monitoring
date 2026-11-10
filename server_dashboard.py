"""
server_dashboard.py - Classroom / Lab Monitor Teacher Dashboard (wxPython)
Antarmuka Khusus Guru / Administrator Lab Komputer Sekolah:
- Header: Room ID & Lab Presets, Access Key, Start Server, Quick Selection, Zoom Slider, Counter PC
- Menu Bar: Lab & Kelas, Aksi Massal Siswa, Alat Bantu (Scanner & Test Client), Bantuan
- Row 1: ALL STUDENTS [📢 Kirim Pesan, Kunci Layar, Buka Kunci, Kirim File, Kirim Folder, Power Off]
- Row 2: SELECTED ONLY [📢 Kirim Pesan, Kunci Layar, Buka Kunci, Kirim File, Kirim Folder, Power Off]
- Row 3: LAUNCH APP [notepad.exe, Launch All, Launch Selected]
- Main Area: Grid Thumbnail Layar Komputer Siswa Real-time dengan Menu Klik Kanan (Context Menu)
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
from typing import Dict, List, Optional

import wx
from PIL import Image


class AgentConnection:
    """Mewakili satu sesi koneksi dari komputer siswa."""
    def __init__(self, sock: socket.socket, addr: tuple, hostname: str, room_id: str):
        self.sock = sock
        self.addr = addr
        self.ip = addr[0]
        self.hostname = hostname
        self.room_id = room_id
        self.last_frame: Optional[wx.Bitmap] = None
        self.last_seen = time.time()
        self.selected = False

    def send_command(self, action: str, extra_payload: Optional[dict] = None, data: bytes = b""):
        try:
            payload = {"action": action}
            if extra_payload:
                payload.update(extra_payload)
            header_bytes = json.dumps(payload).encode("utf-8")
            prefix = struct.pack(">II", len(header_bytes), len(data))
            self.sock.sendall(prefix + header_bytes + data)
        except Exception as e:
            print(f"[!] Error sending command to {self.ip}: {e}")


class BroadcastMessageDialog(wx.Dialog):
    """Dialog modal guru untuk mengirim pengumuman/pesan siaran ke layar siswa."""
    def __init__(self, parent, target_desc: str = "Semua Siswa"):
        super().__init__(parent, title="📢 Kirim Pesan / Pengumuman ke Layar Siswa", size=(560, 480),
                         style=wx.DEFAULT_DIALOG_STYLE | wx.RESIZE_BORDER)
        self.target_desc = target_desc
        self.message_text = ""
        self.is_urgent = False

        self._init_ui()
        self.CenterOnParent()

    def _init_ui(self):
        panel = wx.Panel(self)
        panel.SetBackgroundColour(wx.Colour(248, 250, 252))
        main_sizer = wx.BoxSizer(wx.VERTICAL)

        # Header Info Banner
        hdr_pnl = wx.Panel(panel)
        hdr_pnl.SetBackgroundColour(wx.Colour(37, 99, 235))
        hdr_sz = wx.BoxSizer(wx.HORIZONTAL)

        lbl_title = wx.StaticText(hdr_pnl, label="📢 Broadcast Pesan Guru ke Komputer Siswa")
        lbl_title.SetFont(wx.Font(11, wx.FONTFAMILY_DEFAULT, wx.FONTSTYLE_NORMAL, wx.FONTWEIGHT_BOLD))
        lbl_title.SetForegroundColour(wx.Colour(255, 255, 255))
        hdr_sz.Add(lbl_title, 0, wx.ALL, 12)
        hdr_pnl.SetSizer(hdr_sz)
        main_sizer.Add(hdr_pnl, 0, wx.EXPAND | wx.BOTTOM, 12)

        content_sizer = wx.BoxSizer(wx.VERTICAL)

        # Penerima Info
        lbl_target = wx.StaticText(panel, label=f"🎯 Target Penerima: {self.target_desc}")
        lbl_target.SetFont(wx.Font(10, wx.FONTFAMILY_DEFAULT, wx.FONTSTYLE_NORMAL, wx.FONTWEIGHT_BOLD))
        lbl_target.SetForegroundColour(wx.Colour(30, 41, 59))
        content_sizer.Add(lbl_target, 0, wx.BOTTOM, 10)

        # Template Dropdown
        lbl_tmpl = wx.StaticText(panel, label="Gunakan Contoh Pesan Cepat (Template):")
        lbl_tmpl.SetFont(wx.Font(9, wx.FONTFAMILY_DEFAULT, wx.FONTSTYLE_NORMAL, wx.FONTWEIGHT_NORMAL))
        content_sizer.Add(lbl_tmpl, 0, wx.BOTTOM, 4)

        templates = [
            "-- Pilih Contoh Pesan Cepat --",
            "Perhatian! Praktikum / Ujian akan segera dimulai. Tutup semua tab/aplikasi yang tidak diperlukan.",
            "Waktu praktikum / pengerjaan tugas tersisa 10 menit. Harap segera simpan semua file Anda.",
            "Harap semua siswa memperhatikan penjelasan dan demonstrasi materi di layar proyektor depan.",
            "Peringatan: Dilarang membuka game, YouTube, atau media sosial selama jam pelajaran berlangsung.",
            "Waktu praktikum telah selesai! Simpan pekerjaan Anda, logout, dan rapikan kembali keyboard & mouse.",
            "Silakan kumpulkan berkas laporan/tugas Anda ke folder yang telah dibagikan guru."
        ]
        self.cmb_template = wx.ComboBox(panel, choices=templates, style=wx.CB_READONLY)
        self.cmb_template.SetSelection(0)
        self.cmb_template.Bind(wx.EVT_COMBOBOX, self.on_select_template)
        content_sizer.Add(self.cmb_template, 0, wx.EXPAND | wx.BOTTOM, 12)

        # Message Text Input
        lbl_input = wx.StaticText(panel, label="Isi Pesan Pengumuman:")
        lbl_input.SetFont(wx.Font(9, wx.FONTFAMILY_DEFAULT, wx.FONTSTYLE_NORMAL, wx.FONTWEIGHT_BOLD))
        content_sizer.Add(lbl_input, 0, wx.BOTTOM, 4)

        self.txt_message = wx.TextCtrl(panel, style=wx.TE_MULTILINE | wx.TE_WORDWRAP, size=(-1, 130))
        self.txt_message.SetFont(wx.Font(10, wx.FONTFAMILY_DEFAULT, wx.FONTSTYLE_NORMAL, wx.FONTWEIGHT_NORMAL))
        content_sizer.Add(self.txt_message, 1, wx.EXPAND | wx.BOTTOM, 12)

        # Urgent Checkbox
        self.chk_urgent = wx.CheckBox(panel, label="🚨 Tandai sebagai Pesan Darurat / Peringatan Penting (Highlight Merah & Suara)")
        self.chk_urgent.SetFont(wx.Font(9, wx.FONTFAMILY_DEFAULT, wx.FONTSTYLE_NORMAL, wx.FONTWEIGHT_BOLD))
        self.chk_urgent.SetForegroundColour(wx.Colour(185, 28, 28))
        content_sizer.Add(self.chk_urgent, 0, wx.BOTTOM, 15)

        # Buttons
        btn_sizer = wx.BoxSizer(wx.HORIZONTAL)
        btn_cancel = wx.Button(panel, wx.ID_CANCEL, label="Batal", size=(90, 32))
        self.btn_send = wx.Button(panel, wx.ID_OK, label="📢 Kirim Pesan Sekarang", size=(190, 32))
        self.btn_send.SetBackgroundColour(wx.Colour(14, 165, 233))
        self.btn_send.SetForegroundColour(wx.Colour(255, 255, 255))
        self.btn_send.SetFont(wx.Font(9, wx.FONTFAMILY_DEFAULT, wx.FONTSTYLE_NORMAL, wx.FONTWEIGHT_BOLD))
        self.btn_send.Bind(wx.EVT_BUTTON, self.on_send)

        btn_sizer.AddStretchSpacer()
        btn_sizer.Add(btn_cancel, 0, wx.RIGHT, 8)
        btn_sizer.Add(self.btn_send, 0)
        content_sizer.Add(btn_sizer, 0, wx.EXPAND)

        main_sizer.Add(content_sizer, 1, wx.EXPAND | wx.LEFT | wx.RIGHT | wx.BOTTOM, 15)
        panel.SetSizer(main_sizer)

    def on_select_template(self, event):
        idx = self.cmb_template.GetSelection()
        if idx > 0:
            val = self.cmb_template.GetString(idx)
            self.txt_message.SetValue(val)

    def on_send(self, event):
        msg = self.txt_message.GetValue().strip()
        if not msg:
            wx.MessageBox("Harap masukkan isi pesan pengumuman terlebih dahulu.", "Peringatan",
                          wx.OK | wx.ICON_WARNING, self)
            return
        self.message_text = msg
        self.is_urgent = self.chk_urgent.GetValue()
        self.EndModal(wx.ID_OK)


class ClientCardPanel(wx.Panel):
    """Visual card representing a connected student PC with live screen thumbnail."""
    def __init__(self, parent, agent: AgentConnection, base_size: int = 240):
        super().__init__(parent, style=wx.BORDER_SIMPLE)
        self.agent = agent
        self.base_size = base_size
        self.is_selected = False

        self.SetBackgroundColour(wx.Colour(255, 255, 255))
        self._init_ui()

        # Click event to toggle selection
        self.Bind(wx.EVT_LEFT_DOWN, self.on_click)
        self.bmp_view.Bind(wx.EVT_LEFT_DOWN, self.on_click)
        self.lbl_name.Bind(wx.EVT_LEFT_DOWN, self.on_click)

        # Right-Click Context Menu
        self.Bind(wx.EVT_RIGHT_DOWN, self.on_right_click)
        self.bmp_view.Bind(wx.EVT_RIGHT_DOWN, self.on_right_click)
        self.lbl_name.Bind(wx.EVT_RIGHT_DOWN, self.on_right_click)

    def _init_ui(self):
        vbox = wx.BoxSizer(wx.VERTICAL)

        # Header Info: Checkbox + Hostname + IP
        info_sizer = wx.BoxSizer(wx.HORIZONTAL)
        self.chk_select = wx.CheckBox(self)
        self.chk_select.Bind(wx.EVT_CHECKBOX, self.on_chk_toggle)
        self.lbl_name = wx.StaticText(self, label=f"🖥️ {self.agent.hostname} ({self.agent.ip})")
        self.lbl_name.SetFont(wx.Font(9, wx.FONTFAMILY_DEFAULT, wx.FONTSTYLE_NORMAL, wx.FONTWEIGHT_BOLD))
        self.lbl_name.SetForegroundColour(wx.Colour(30, 41, 59))

        info_sizer.Add(self.chk_select, 0, wx.ALL | wx.ALIGN_CENTER_VERTICAL, 4)
        info_sizer.Add(self.lbl_name, 1, wx.ALL | wx.ALIGN_CENTER_VERTICAL, 4)
        vbox.Add(info_sizer, 0, wx.EXPAND | wx.ALL, 2)

        # Live Screen Thumbnail
        w = self.base_size
        h = int(self.base_size * 0.625)
        empty_bmp = wx.Bitmap(w, h)
        self.bmp_view = wx.StaticBitmap(self, bitmap=empty_bmp, size=(w, h))
        vbox.Add(self.bmp_view, 1, wx.EXPAND | wx.ALL, 4)

        self.SetSizer(vbox)

    def set_zoom(self, size: int):
        self.base_size = size
        w = size
        h = int(size * 0.625)
        self.bmp_view.SetMinSize((w, h))
        self.bmp_view.SetSize((w, h))
        self.SetMinSize((w + 10, h + 38))
        self.update_frame(self.agent.last_frame)

    def update_frame(self, bitmap: Optional[wx.Bitmap]):
        if bitmap and bitmap.IsOk():
            img = bitmap.ConvertToImage()
            w = self.base_size
            h = int(self.base_size * 0.625)
            scaled = img.Scale(w, h, wx.IMAGE_QUALITY_HIGH)
            self.bmp_view.SetBitmap(wx.Bitmap(scaled))
            self.Refresh()

    def on_click(self, event):
        self.toggle_selection(not self.is_selected)

    def on_chk_toggle(self, event):
        self.toggle_selection(self.chk_select.GetValue())

    def toggle_selection(self, selected: bool):
        self.is_selected = selected
        self.agent.selected = selected
        self.chk_select.SetValue(selected)
        if selected:
            self.SetBackgroundColour(wx.Colour(219, 234, 254))  # Soft blue highlight
        else:
            self.SetBackgroundColour(wx.Colour(255, 255, 255))
        self.Refresh()

    def on_right_click(self, event):
        menu = wx.Menu()
        m_msg = menu.Append(wx.ID_ANY, f"📢 Kirim Pesan ke {self.agent.hostname}...")
        menu.AppendSeparator()
        m_lock = menu.Append(wx.ID_ANY, "🔒 Kunci Layar PC Ini")
        m_unlock = menu.Append(wx.ID_ANY, "🔓 Buka Kunci Layar PC Ini")
        menu.AppendSeparator()
        m_app = menu.Append(wx.ID_ANY, "🚀 Jalankan Aplikasi...")
        m_file = menu.Append(wx.ID_ANY, "📁 Kirim File ke PC Ini...")
        menu.AppendSeparator()
        m_power = menu.Append(wx.ID_ANY, "⏻ Matikan PC Ini (Power Off)")

        self.Bind(wx.EVT_MENU, self._single_broadcast, m_msg)
        self.Bind(wx.EVT_MENU, lambda e: self.agent.send_command("LOCK_SCREEN"), m_lock)
        self.Bind(wx.EVT_MENU, lambda e: self.agent.send_command("UNLOCK_SCREEN"), m_unlock)
        self.Bind(wx.EVT_MENU, self._single_launch_app, m_app)
        self.Bind(wx.EVT_MENU, self._single_send_file, m_file)
        self.Bind(wx.EVT_MENU, self._single_power_off, m_power)

        self.PopupMenu(menu)
        menu.Destroy()

    def _single_broadcast(self, event):
        top = self.GetTopLevelParent()
        if hasattr(top, "broadcast_to_single"):
            top.broadcast_to_single(self.agent)

    def _single_launch_app(self, event):
        dlg = wx.TextEntryDialog(self, f"Perintah/Aplikasi untuk {self.agent.hostname}:",
                                 "Jalankan Aplikasi", "notepad.exe")
        if dlg.ShowModal() == wx.ID_OK:
            cmd = dlg.GetValue().strip()
            if cmd:
                self.agent.send_command("LAUNCH_APP", {"command": cmd})
        dlg.Destroy()

    def _single_send_file(self, event):
        with wx.FileDialog(self, f"Pilih File untuk {self.agent.hostname}",
                           style=wx.FD_OPEN | wx.FD_FILE_MUST_EXIST) as dlg:
            if dlg.ShowModal() == wx.ID_OK:
                path = dlg.GetPath()
                fname = os.path.basename(path)
                with open(path, "rb") as f:
                    content = f.read()
                self.agent.send_command("SEND_FILE", {"filename": fname}, data=content)
                wx.MessageBox(f"File '{fname}' berhasil dikirim ke {self.agent.hostname}.",
                              "Sukses", wx.OK | wx.ICON_INFORMATION, self)

    def _single_power_off(self, event):
        res = wx.MessageBox(f"Apakah Anda yakin ingin mematikan komputer {self.agent.hostname} ({self.agent.ip})?",
                            "Konfirmasi Matikan PC", wx.YES_NO | wx.ICON_QUESTION, self)
        if res == wx.YES:
            self.agent.send_command("POWER_OFF")


class TeacherDashboardFrame(wx.Frame):
    """The School Classroom & Computer Lab Teacher Dashboard (wxPython)."""

    def __init__(self):
        super().__init__(None, title="Classroom Monitor - Dashboard Guru Lab Komputer Sekolah", size=(1320, 800))

        self.agents: Dict[str, AgentConnection] = {}  # ip -> AgentConnection
        self.card_widgets: Dict[str, ClientCardPanel] = {}

        self.server_sock: Optional[socket.socket] = None
        self.server_running = False
        self.server_port = 8888
        self.zoom_size = 240

        self.SetBackgroundColour(wx.Colour(255, 255, 255))
        self._init_menu_bar()
        self._init_ui()
        self.Center()

    def _init_menu_bar(self):
        menubar = wx.MenuBar()

        # 1. Menu Lab & Kelas
        m_lab = wx.Menu()
        item_server = m_lab.Append(wx.ID_ANY, "Mulai / Hentikan Server", "Nyalakan atau matikan penerimaan koneksi lab")
        m_lab.AppendSeparator()
        item_sel_all = m_lab.Append(wx.ID_ANY, "Pilih Semua PC Siswa\tCtrl+A")
        item_desel_all = m_lab.Append(wx.ID_ANY, "Batalkan Pilihan Semua PC\tCtrl+D")
        m_lab.AppendSeparator()
        item_exit = m_lab.Append(wx.ID_EXIT, "Keluar\tAlt+F4")
        menubar.Append(m_lab, "&Lab & Kelas")

        # 2. Menu Aksi Siswa
        m_aksi = wx.Menu()
        item_msg_all = m_aksi.Append(wx.ID_ANY, "📢 Kirim Pesan ke Semua Siswa\tCtrl+M")
        item_lock_all = m_aksi.Append(wx.ID_ANY, "🔒 Kunci Semua Layar Siswa\tCtrl+L")
        item_unlock_all = m_aksi.Append(wx.ID_ANY, "🔓 Buka Kunci Semua Layar Siswa\tCtrl+U")
        m_aksi.AppendSeparator()
        item_power_all = m_aksi.Append(wx.ID_ANY, "⏻ Matikan Semua PC Lab")
        menubar.Append(m_aksi, "&Aksi Siswa")

        # 3. Menu Alat Bantu
        m_tools = wx.Menu()
        item_scanner = m_tools.Append(wx.ID_ANY, "🌐 Buka Network Scanner Jaringan (Agentless)", "Pindai IP & ARP seluruh PC di subnet")
        item_test_client = m_tools.Append(wx.ID_ANY, "🖥️ Jalankan Client Agent Test Lokal", "Jalankan 1 PC Siswa simulator di komputer ini")
        menubar.Append(m_tools, "A&lat Bantu")

        # 4. Menu Bantuan
        m_help = wx.Menu()
        item_guide = m_help.Append(wx.ID_ANY, "📖 Panduan Penggunaan Lab Sekolah")
        item_about = m_help.Append(wx.ID_ABOUT, "ℹ️ Tentang Aplikasi")
        menubar.Append(m_help, "&Bantuan")

        self.SetMenuBar(menubar)

        # Bind events
        self.Bind(wx.EVT_MENU, self.on_toggle_server, item_server)
        self.Bind(wx.EVT_MENU, lambda e: self.select_all_cards(True), item_sel_all)
        self.Bind(wx.EVT_MENU, lambda e: self.select_all_cards(False), item_desel_all)
        self.Bind(wx.EVT_MENU, lambda e: self.Close(), item_exit)

        self.Bind(wx.EVT_MENU, lambda e: self.broadcast_message_action(target_all=True), item_msg_all)
        self.Bind(wx.EVT_MENU, lambda e: self.cmd_all("LOCK_SCREEN"), item_lock_all)
        self.Bind(wx.EVT_MENU, lambda e: self.cmd_all("UNLOCK_SCREEN"), item_unlock_all)
        self.Bind(wx.EVT_MENU, lambda e: self.on_power_all_confirm(), item_power_all)

        self.Bind(wx.EVT_MENU, self.on_open_network_scanner, item_scanner)
        self.Bind(wx.EVT_MENU, self.on_launch_test_client, item_test_client)
        self.Bind(wx.EVT_MENU, self.on_show_guide, item_guide)
        self.Bind(wx.EVT_MENU, self.on_show_about, item_about)

    def _init_ui(self):
        main_sizer = wx.BoxSizer(wx.VERTICAL)

        # -------------------------------------------------------------
        # 1. TOP HEADER BAR (School & Lab Identity)
        # -------------------------------------------------------------
        top_bar = wx.Panel(self)
        top_bar.SetBackgroundColour(wx.Colour(248, 250, 252))
        top_sizer = wx.BoxSizer(wx.HORIZONTAL)

        # Lab Room Selector
        lbl_room = wx.StaticText(top_bar, label="🏫 Ruang Lab:")
        lbl_room.SetFont(wx.Font(9, wx.FONTFAMILY_DEFAULT, wx.FONTSTYLE_NORMAL, wx.FONTWEIGHT_BOLD))
        
        lab_presets = ["labs", "lab-1", "lab-2", "lab-cbt", "lab-multimedia"]
        self.cmb_room = wx.ComboBox(top_bar, value="labs", choices=lab_presets, size=(125, 26))

        lbl_key = wx.StaticText(top_bar, label="Kunci Akses:")
        lbl_key.SetFont(wx.Font(9, wx.FONTFAMILY_DEFAULT, wx.FONTSTYLE_NORMAL, wx.FONTWEIGHT_NORMAL))
        self.txt_key = wx.TextCtrl(top_bar, value="siswa123", size=(100, 26))

        self.btn_start = wx.Button(top_bar, label="Start Server", size=(100, 28))
        self.btn_start.SetBackgroundColour(wx.Colour(16, 185, 129))  # Emerald Green
        self.btn_start.SetForegroundColour(wx.Colour(255, 255, 255))
        self.btn_start.SetFont(wx.Font(8, wx.FONTFAMILY_DEFAULT, wx.FONTSTYLE_NORMAL, wx.FONTWEIGHT_BOLD))
        self.btn_start.Bind(wx.EVT_BUTTON, self.on_toggle_server)

        # Selection Quick Buttons
        self.btn_sel_all = wx.Button(top_bar, label="☑ Pilih Semua", size=(105, 28))
        self.btn_sel_all.Bind(wx.EVT_BUTTON, lambda e: self.select_all_cards(True))

        self.btn_desel_all = wx.Button(top_bar, label="☐ Lepas Pilihan", size=(105, 28))
        self.btn_desel_all.Bind(wx.EVT_BUTTON, lambda e: self.select_all_cards(False))

        # Counter badge
        self.pnl_counter = wx.Panel(top_bar, size=(175, 28))
        self.pnl_counter.SetBackgroundColour(wx.Colour(224, 231, 255))
        cnt_sz = wx.BoxSizer(wx.HORIZONTAL)
        self.lbl_counter = wx.StaticText(self.pnl_counter, label="● 0 PC Siswa Terhubung")
        self.lbl_counter.SetFont(wx.Font(9, wx.FONTFAMILY_DEFAULT, wx.FONTSTYLE_NORMAL, wx.FONTWEIGHT_BOLD))
        self.lbl_counter.SetForegroundColour(wx.Colour(30, 58, 138))
        cnt_sz.Add(self.lbl_counter, 0, wx.ALL | wx.ALIGN_CENTER, 5)
        self.pnl_counter.SetSizer(cnt_sz)

        top_sizer.Add(lbl_room, 0, wx.ALL | wx.ALIGN_CENTER_VERTICAL, 6)
        top_sizer.Add(self.cmb_room, 0, wx.ALL | wx.ALIGN_CENTER_VERTICAL, 4)
        top_sizer.Add(lbl_key, 0, wx.ALL | wx.ALIGN_CENTER_VERTICAL, 6)
        top_sizer.Add(self.txt_key, 0, wx.ALL | wx.ALIGN_CENTER_VERTICAL, 4)
        top_sizer.Add(self.btn_start, 0, wx.LEFT | wx.ALIGN_CENTER_VERTICAL, 8)
        top_sizer.Add(self.btn_sel_all, 0, wx.LEFT | wx.ALIGN_CENTER_VERTICAL, 6)
        top_sizer.Add(self.btn_desel_all, 0, wx.LEFT | wx.ALIGN_CENTER_VERTICAL, 4)
        top_sizer.Add(self.pnl_counter, 0, wx.LEFT | wx.ALIGN_CENTER_VERTICAL, 10)

        top_sizer.AddStretchSpacer()

        lbl_zoom = wx.StaticText(top_bar, label="Zoom Layar:")
        self.slider_zoom = wx.Slider(top_bar, value=240, minValue=160, maxValue=480, size=(130, -1))
        self.slider_zoom.Bind(wx.EVT_SLIDER, self.on_zoom_change)

        top_sizer.Add(lbl_zoom, 0, wx.ALL | wx.ALIGN_CENTER_VERTICAL, 6)
        top_sizer.Add(self.slider_zoom, 0, wx.RIGHT | wx.ALIGN_CENTER_VERTICAL, 15)

        top_bar.SetSizer(top_sizer)
        main_sizer.Add(top_bar, 0, wx.EXPAND | wx.BOTTOM, 4)

        # -------------------------------------------------------------
        # 2. ACTION CONTROLS (ROW 1, ROW 2, ROW 3)
        # -------------------------------------------------------------
        btn_panel = wx.Panel(self)
        btn_panel.SetBackgroundColour(wx.Colour(255, 255, 255))
        grid_btns = wx.BoxSizer(wx.VERTICAL)

        # ROW 1: ALL STUDENTS
        r1 = wx.BoxSizer(wx.HORIZONTAL)
        lbl_all = self._create_badge(btn_panel, "ALL STUDENTS", wx.Colour(241, 245, 249), wx.Colour(30, 41, 59))
        r1.Add(lbl_all, 0, wx.RIGHT | wx.ALIGN_CENTER_VERTICAL, 8)

        btn_msg_all = self._create_btn(btn_panel, "📢 Kirim Pesan", wx.Colour(14, 165, 233), lambda e: self.broadcast_message_action(target_all=True))
        btn_lock_all = self._create_btn(btn_panel, "Lock Screen", wx.Colour(234, 179, 8), lambda e: self.cmd_all("LOCK_SCREEN"))
        btn_unlock_all = self._create_btn(btn_panel, "Unlock Screen", wx.Colour(37, 99, 235), lambda e: self.cmd_all("UNLOCK_SCREEN"))
        btn_file_all = self._create_btn(btn_panel, "Send File", wx.Colour(37, 99, 235), lambda e: self.send_file_action(target_all=True))
        btn_folder_all = self._create_btn(btn_panel, "Send Folder", wx.Colour(37, 99, 235), lambda e: self.send_folder_action(target_all=True))
        btn_power_all = self._create_btn(btn_panel, "Power Off", wx.Colour(239, 68, 68), lambda e: self.on_power_all_confirm())

        for b in [btn_msg_all, btn_lock_all, btn_unlock_all, btn_file_all, btn_folder_all, btn_power_all]:
            r1.Add(b, 0, wx.RIGHT | wx.ALIGN_CENTER_VERTICAL, 6)
        grid_btns.Add(r1, 0, wx.EXPAND | wx.ALL, 4)

        # ROW 2: SELECTED ONLY
        r2 = wx.BoxSizer(wx.HORIZONTAL)
        lbl_sel = self._create_badge(btn_panel, "SELECTED ONLY", wx.Colour(241, 245, 249), wx.Colour(30, 41, 59))
        r2.Add(lbl_sel, 0, wx.RIGHT | wx.ALIGN_CENTER_VERTICAL, 8)

        btn_msg_sel = self._create_btn(btn_panel, "📢 Kirim Pesan", wx.Colour(14, 165, 233), lambda e: self.broadcast_message_action(target_all=False))
        btn_lock_sel = self._create_btn(btn_panel, "Lock Screen", wx.Colour(234, 179, 8), lambda e: self.cmd_selected("LOCK_SCREEN"))
        btn_unlock_sel = self._create_btn(btn_panel, "Unlock Screen", wx.Colour(37, 99, 235), lambda e: self.cmd_selected("UNLOCK_SCREEN"))
        btn_file_sel = self._create_btn(btn_panel, "Send File", wx.Colour(37, 99, 235), lambda e: self.send_file_action(target_all=False))
        btn_folder_sel = self._create_btn(btn_panel, "Send Folder", wx.Colour(37, 99, 235), lambda e: self.send_folder_action(target_all=False))
        btn_power_sel = self._create_btn(btn_panel, "Power Off", wx.Colour(239, 68, 68), lambda e: self.cmd_selected("POWER_OFF"))

        for b in [btn_msg_sel, btn_lock_sel, btn_unlock_sel, btn_file_sel, btn_folder_sel, btn_power_sel]:
            r2.Add(b, 0, wx.RIGHT | wx.ALIGN_CENTER_VERTICAL, 6)
        grid_btns.Add(r2, 0, wx.EXPAND | wx.ALL, 4)

        # ROW 3: LAUNCH APP
        r3 = wx.BoxSizer(wx.HORIZONTAL)
        lbl_app = self._create_badge(btn_panel, "LAUNCH APP", wx.Colour(241, 245, 249), wx.Colour(30, 41, 59))
        r3.Add(lbl_app, 0, wx.RIGHT | wx.ALIGN_CENTER_VERTICAL, 8)

        self.txt_app = wx.TextCtrl(btn_panel, value="notepad.exe", size=(160, 28))
        r3.Add(self.txt_app, 0, wx.RIGHT | wx.ALIGN_CENTER_VERTICAL, 10)

        btn_launch_all = self._create_btn(btn_panel, "Launch All", wx.Colour(37, 99, 235), self.on_launch_all)
        btn_launch_sel = self._create_btn(btn_panel, "Launch Selected", wx.Colour(37, 99, 235), self.on_launch_selected)

        r3.Add(btn_launch_all, 0, wx.RIGHT | wx.ALIGN_CENTER_VERTICAL, 6)
        r3.Add(btn_launch_sel, 0, wx.RIGHT | wx.ALIGN_CENTER_VERTICAL, 6)
        grid_btns.Add(r3, 0, wx.EXPAND | wx.ALL, 4)

        btn_panel.SetSizer(grid_btns)
        main_sizer.Add(btn_panel, 0, wx.EXPAND | wx.LEFT | wx.RIGHT, 10)
        main_sizer.Add(wx.StaticLine(self), 0, wx.EXPAND | wx.ALL, 6)

        # -------------------------------------------------------------
        # 3. MAIN CONTENT: SCROLLABLE THUMBNAIL GRID
        # -------------------------------------------------------------
        self.scroll_win = wx.ScrolledWindow(self, style=wx.VSCROLL)
        self.scroll_win.SetScrollRate(10, 10)
        self.scroll_win.SetBackgroundColour(wx.Colour(255, 255, 255))

        self.grid_sizer = wx.WrapSizer(wx.HORIZONTAL)
        self.scroll_win.SetSizer(self.grid_sizer)
        main_sizer.Add(self.scroll_win, 1, wx.EXPAND | wx.ALL, 8)

        self.SetSizer(main_sizer)

    def _create_badge(self, parent, text: str, bg: wx.Colour, fg: wx.Colour) -> wx.Panel:
        pnl = wx.Panel(parent, size=(110, 28))
        pnl.SetBackgroundColour(bg)
        sz = wx.BoxSizer(wx.HORIZONTAL)
        lbl = wx.StaticText(pnl, label=text)
        lbl.SetFont(wx.Font(8, wx.FONTFAMILY_DEFAULT, wx.FONTSTYLE_NORMAL, wx.FONTWEIGHT_BOLD))
        lbl.SetForegroundColour(fg)
        sz.Add(lbl, 0, wx.ALL | wx.ALIGN_CENTER, 6)
        pnl.SetSizer(sz)
        return pnl

    def _create_btn(self, parent, label: str, bg: wx.Colour, handler) -> wx.Button:
        btn = wx.Button(parent, label=label, size=(110, 28))
        btn.SetBackgroundColour(bg)
        btn.SetForegroundColour(wx.Colour(255, 255, 255))
        btn.SetFont(wx.Font(8, wx.FONTFAMILY_DEFAULT, wx.FONTSTYLE_NORMAL, wx.FONTWEIGHT_BOLD))
        btn.Bind(wx.EVT_BUTTON, handler)
        return btn

    def select_all_cards(self, select: bool):
        for card in self.card_widgets.values():
            card.toggle_selection(select)

    def on_zoom_change(self, event):
        val = self.slider_zoom.GetValue()
        self.zoom_size = val
        for card in self.card_widgets.values():
            card.set_zoom(val)
        self.grid_sizer.Layout()
        self.scroll_win.FitInside()

    # -------------------------------------------------------------
    # SERVER NETWORKING & THREADS
    # -------------------------------------------------------------

    def on_toggle_server(self, event):
        if not self.server_running:
            self.start_server()
        else:
            self.stop_server()

    def start_server(self):
        self.server_running = True
        self.btn_start.SetLabel("Stop Server")
        self.btn_start.SetBackgroundColour(wx.Colour(239, 68, 68))

        def listen_loop():
            try:
                self.server_sock = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
                self.server_sock.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
                self.server_sock.bind(("0.0.0.0", self.server_port))
                self.server_sock.listen(100)
                print(f"[*] Dashboard Guru mendengarkan di port {self.server_port}...")

                while self.server_running:
                    client_sock, addr = self.server_sock.accept()
                    threading.Thread(target=self._handle_client, args=(client_sock, addr), daemon=True).start()
            except Exception as e:
                print(f"[!] Server socket error: {e}")

        def udp_beacon_loop():
            """Mengirim sinyal broadcast UDP agar PC Siswa dapat menemukan IP Guru otomatis."""
            b_sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
            b_sock.setsockopt(socket.SOL_SOCKET, socket.SO_BROADCAST, 1)
            while self.server_running:
                try:
                    room = self.cmb_room.GetValue().strip()
                    key = self.txt_key.GetValue().strip()
                    msg = json.dumps({
                        "service": "CLASSROOM_MONITOR_TEACHER",
                        "room_id": room,
                        "access_key": key,
                        "tcp_port": self.server_port,
                        "hostname": socket.gethostname()
                    }).encode("utf-8")
                    b_sock.sendto(msg, ("255.255.255.255", 8889))
                except Exception:
                    pass
                time.sleep(2)
            try:
                b_sock.close()
            except Exception:
                pass

        threading.Thread(target=listen_loop, daemon=True).start()
        threading.Thread(target=udp_beacon_loop, daemon=True).start()

    def stop_server(self):
        self.server_running = False
        self.btn_start.SetLabel("Start Server")
        self.btn_start.SetBackgroundColour(wx.Colour(16, 185, 129))
        if self.server_sock:
            try:
                self.server_sock.close()
            except Exception:
                pass

    def _handle_client(self, client_sock: socket.socket, addr: tuple):
        """Menangani handshake otentikasi dan aliran snapshot dari client agent."""
        expected_room = self.cmb_room.GetValue().strip()
        expected_key = self.txt_key.GetValue().strip()

        try:
            prefix = client_sock.recv(8)
            if not prefix:
                return
            h_len, extra_len = struct.unpack(">II", prefix)
            h_bytes = client_sock.recv(h_len)
            header = json.loads(h_bytes.decode("utf-8"))

            client_room = header.get("room_id")
            client_key = header.get("access_key")
            hostname = header.get("hostname", addr[0])

            # Verifikasi Room ID & Access Key
            if client_room != expected_room or client_key != expected_key:
                resp = json.dumps({"status": "ERR", "message": "Otentikasi Room/Key salah"}).encode("utf-8")
                client_sock.sendall(struct.pack(">II", len(resp), 0) + resp)
                client_sock.close()
                return

            resp = json.dumps({"status": "OK"}).encode("utf-8")
            client_sock.sendall(struct.pack(">II", len(resp), 0) + resp)

            agent = AgentConnection(client_sock, addr, hostname, client_room)
            wx.CallAfter(self._add_client_card, agent)

            while self.server_running:
                p = client_sock.recv(8)
                if not p:
                    break
                h_len, ex_len = struct.unpack(">II", p)
                meta_bytes = self._recv_exact(client_sock, h_len)
                jpeg_bytes = self._recv_exact(client_sock, ex_len)

                image = wx.Image(io.BytesIO(jpeg_bytes))
                if image.IsOk():
                    bmp = wx.Bitmap(image)
                    agent.last_frame = bmp
                    wx.CallAfter(self._update_client_frame, agent.ip, bmp)
        except Exception:
            pass
        finally:
            wx.CallAfter(self._remove_client_card, addr[0])

    def _recv_exact(self, sock: socket.socket, num_bytes: int) -> bytes:
        data = bytearray()
        while len(data) < num_bytes:
            packet = sock.recv(num_bytes - len(data))
            if not packet:
                raise ConnectionResetError()
            data.extend(packet)
        return bytes(data)

    def _add_client_card(self, agent: AgentConnection):
        self.agents[agent.ip] = agent
        card = ClientCardPanel(self.scroll_win, agent, base_size=self.zoom_size)
        self.card_widgets[agent.ip] = card
        self.grid_sizer.Add(card, 0, wx.ALL, 8)
        self.grid_sizer.Layout()
        self.scroll_win.FitInside()
        self._update_counter()

    def _remove_client_card(self, ip: str):
        if ip in self.agents:
            del self.agents[ip]
        if ip in self.card_widgets:
            card = self.card_widgets.pop(ip)
            card.Destroy()
            self.grid_sizer.Layout()
            self.scroll_win.FitInside()
        self._update_counter()

    def _update_counter(self):
        count = len(self.agents)
        self.lbl_counter.SetLabel(f"● {count} PC Siswa Terhubung")

    def _update_client_frame(self, ip: str, bmp: wx.Bitmap):
        if ip in self.card_widgets:
            self.card_widgets[ip].update_frame(bmp)

    # -------------------------------------------------------------
    # ACTION DISPATCHERS
    # -------------------------------------------------------------

    def cmd_all(self, action: str):
        for ag in self.agents.values():
            ag.send_command(action)

    def cmd_selected(self, action: str):
        selected = [ag for ag in self.agents.values() if ag.selected]
        if not selected:
            wx.MessageBox("Pilih setidaknya 1 PC siswa terlebih dahulu.", "Peringatan", wx.OK | wx.ICON_WARNING, self)
            return
        for ag in selected:
            ag.send_command(action)

    def on_power_all_confirm(self):
        res = wx.MessageBox("Apakah Anda yakin ingin mematikan SEMUA komputer siswa di laboratorium ini?",
                            "Konfirmasi Matikan Semua PC", wx.YES_NO | wx.ICON_WARNING, self)
        if res == wx.YES:
            self.cmd_all("POWER_OFF")

    def broadcast_message_action(self, target_all: bool):
        targets = list(self.agents.values()) if target_all else [ag for ag in self.agents.values() if ag.selected]
        if not targets:
            wx.MessageBox("Tidak ada PC siswa yang terhubung atau dipilih.", "Peringatan",
                          wx.OK | wx.ICON_WARNING, self)
            return

        desc = f"Semua PC Siswa ({len(targets)} PC)" if target_all else f"{len(targets)} PC Siswa Terpilih"
        dlg = BroadcastMessageDialog(self, target_desc=desc)
        if dlg.ShowModal() == wx.ID_OK:
            payload = {
                "title": "🚨 PENGUMUMAN PENTING GURU" if dlg.is_urgent else "📢 Pesan dari Guru / Pengawas Lab",
                "message": dlg.message_text,
                "urgent": dlg.is_urgent
            }
            for ag in targets:
                ag.send_command("BROADCAST_MESSAGE", payload)
            wx.MessageBox(f"Pesan berhasil disiarkan ke {len(targets)} PC Siswa.",
                          "Pesan Terkirim", wx.OK | wx.ICON_INFORMATION, self)
        dlg.Destroy()

    def broadcast_to_single(self, agent: AgentConnection):
        desc = f"PC {agent.hostname} ({agent.ip})"
        dlg = BroadcastMessageDialog(self, target_desc=desc)
        if dlg.ShowModal() == wx.ID_OK:
            payload = {
                "title": "🚨 PENGUMUMAN PENTING GURU" if dlg.is_urgent else "📢 Pesan Pribadi dari Guru",
                "message": dlg.message_text,
                "urgent": dlg.is_urgent
            }
            agent.send_command("BROADCAST_MESSAGE", payload)
            wx.MessageBox(f"Pesan berhasil dikirim ke {agent.hostname}.", "Pesan Terkirim",
                          wx.OK | wx.ICON_INFORMATION, self)
        dlg.Destroy()

    def on_launch_all(self, event):
        cmd = self.txt_app.GetValue().strip()
        if not cmd:
            return
        for ag in self.agents.values():
            ag.send_command("LAUNCH_APP", {"command": cmd})

    def on_launch_selected(self, event):
        cmd = self.txt_app.GetValue().strip()
        selected = [ag for ag in self.agents.values() if ag.selected]
        if not selected:
            wx.MessageBox("Pilih setidaknya 1 PC siswa terlebih dahulu.", "Peringatan", wx.OK | wx.ICON_WARNING, self)
            return
        for ag in selected:
            ag.send_command("LAUNCH_APP", {"command": cmd})

    def send_file_action(self, target_all: bool):
        targets = list(self.agents.values()) if target_all else [ag for ag in self.agents.values() if ag.selected]
        if not targets:
            wx.MessageBox("Tidak ada PC target yang dipilih.", "Peringatan", wx.OK | wx.ICON_WARNING, self)
            return

        with wx.FileDialog(self, "Pilih File untuk Dikirim", style=wx.FD_OPEN | wx.FD_FILE_MUST_EXIST) as dlg:
            if dlg.ShowModal() == wx.ID_CANCEL:
                return
            path = dlg.GetPath()
            fname = os.path.basename(path)
            with open(path, "rb") as f:
                content = f.read()

            for ag in targets:
                ag.send_command("SEND_FILE", {"filename": fname}, data=content)
            wx.MessageBox(f"File '{fname}' berhasil dikirim ke {len(targets)} PC.", "Sukses", wx.OK | wx.ICON_INFORMATION, self)

    def send_folder_action(self, target_all: bool):
        targets = list(self.agents.values()) if target_all else [ag for ag in self.agents.values() if ag.selected]
        if not targets:
            wx.MessageBox("Tidak ada PC target yang dipilih.", "Peringatan", wx.OK | wx.ICON_WARNING, self)
            return

        with wx.DirDialog(self, "Pilih Folder untuk Dikirim") as dlg:
            if dlg.ShowModal() == wx.ID_CANCEL:
                return
            folder_path = dlg.GetPath()
            folder_name = os.path.basename(folder_path)

            buf = io.BytesIO()
            with zipfile.ZipFile(buf, "w", zipfile.ZIP_DEFLATED) as z:
                for root, _, files in os.walk(folder_path):
                    for file in files:
                        full_p = os.path.join(root, file)
                        rel_p = os.path.relpath(full_p, folder_path)
                        z.write(full_p, rel_p)
            zip_bytes = buf.getvalue()

            for ag in targets:
                ag.send_command("SEND_FOLDER", {"folder_name": folder_name}, data=zip_bytes)
            wx.MessageBox(f"Folder '{folder_name}' berhasil dikompresi & dikirim ke {len(targets)} PC.", "Sukses", wx.OK | wx.ICON_INFORMATION, self)

    def on_open_network_scanner(self, event):
        try:
            from gui_dashboard import MainDashboardFrame
            scanner_frame = MainDashboardFrame()
            scanner_frame.Show()
        except Exception as e:
            wx.MessageBox(f"Gagal membuka Network Scanner: {e}", "Error", wx.OK | wx.ICON_ERROR, self)

    def on_launch_test_client(self, event):
        client_script = os.path.join(os.path.dirname(__file__), "client_agent.py")
        if os.path.exists(client_script):
            subprocess.Popen([sys.executable, client_script])
            wx.MessageBox("Simulator PC Siswa (client_agent.py) berhasil dijalankan!",
                          "Client Test", wx.OK | wx.ICON_INFORMATION, self)
        else:
            wx.MessageBox("Berkas client_agent.py tidak ditemukan.", "Error", wx.OK | wx.ICON_ERROR, self)

    def on_show_guide(self, event):
        guide_text = (
            "PANDUAN PENGGUNAAN MONITORING LAB KOMPUTER SEKOLAH\n\n"
            "1. Menghubungkan PC Siswa ke Dashboard Guru:\n"
            "   - Di komputer Guru, pastikan tombol 'Start Server' sudah ditekan (warna merah: Stop Server).\n"
            "   - Di komputer Siswa, jalankan client_agent.py (atau client_agent.exe).\n"
            "   - Masukkan IP komputer Guru, Room ID (misal: 'labs'), dan Access Key ('siswa123').\n"
            "   - Thumbnail layar siswa akan langsung muncul di grid dashboard.\n\n"
            "2. Mengirim Pesan / Pengumuman:\n"
            "   - Klik tombol '📢 Kirim Pesan' pada baris ALL STUDENTS untuk siaran massal.\n"
            "   - Atau pilih beberapa kartu PC siswa lalu klik '📢 Kirim Pesan' pada SELECTED ONLY.\n"
            "   - Tersedia template siap pakai untuk ujian, instruksi tugas, dan peringatan tertib lab.\n\n"
            "3. Kunci Layar (Lock Screen):\n"
            "   - Gunakan 'Lock Screen' saat guru menjelaskan materi atau saat jeda instruksi ujian.\n\n"
            "4. Distribusi Bahan Ajar & Aplikasi:\n"
            "   - Kirim file/folder materi yang langsung otomatis tersimpan di Desktop siswa.\n"
            "   - Jalankan program seperti notepad.exe atau browser CBT secara serentak."
        )
        wx.MessageBox(guide_text, "Panduan Monitoring Lab Sekolah", wx.OK | wx.ICON_INFORMATION, self)

    def on_show_about(self, event):
        about_text = (
            "Classroom & Computer Lab Monitoring System\n"
            "Didesain khusus untuk Lingkungan Sekolah & Laboratorium Komputer.\n\n"
            "Antarmuka: wxPython Modern Desktop GUI\n"
            "Fungsi: Live Screen Monitoring, Broadcast Message, Remote Lock & App Launcher"
        )
        wx.MessageBox(about_text, "Tentang Aplikasi", wx.OK | wx.ICON_INFORMATION, self)


def main():
    app = wx.App(False)
    frame = TeacherDashboardFrame()
    frame.Show()
    app.MainLoop()


if __name__ == "__main__":
    main()
