"""
gui_dashboard.py - wxPython Modern GUI Implementation for Office Network Monitoring & Control.
Implements:
1. Header Panel: Subnet/Room ID input, Start/Stop Server toggle, Live Connection Status Indicator.
2. Action Control Panel: 'Scan Network', 'Ping All', 'Power Off PC', 'Launch App', 'Wake-on-LAN', 'Remote Desktop'.
3. Main Content Area: wx.ListCtrl with real-time updates, custom styling, search filter, and context menu.
"""

import csv
import os
import subprocess
import threading
import time
from typing import Dict, List, Optional

import wx
import wx.lib.newevent

from network_engine import NetworkDevice, NetworkEngine

# Custom Events for thread-safe UI updates
ScanProgressEvent, EVT_SCAN_PROGRESS = wx.lib.newevent.NewEvent()
ScanCompleteEvent, EVT_SCAN_COMPLETE = wx.lib.newevent.NewEvent()
DeviceUpdatedEvent, EVT_DEVICE_UPDATED = wx.lib.newevent.NewEvent()
MonitorTickEvent, EVT_MONITOR_TICK = wx.lib.newevent.NewEvent()


# Modern Color Palette
COLOR_PRIMARY_DARK = wx.Colour(24, 32, 47)      # Deep Slate Blue Header
COLOR_PRIMARY_LIGHT = wx.Colour(255, 255, 255)  # White
COLOR_ACCENT_BLUE = wx.Colour(37, 99, 235)      # Royal Blue
COLOR_ACCENT_GREEN = wx.Colour(16, 185, 129)    # Emerald Green (Online)
COLOR_ACCENT_RED = wx.Colour(239, 68, 68)       # Crimson Red (Offline/Power Off)
COLOR_ACCENT_AMBER = wx.Colour(245, 158, 11)    # Amber Yellow (ARP Only)
COLOR_BG_LIGHT = wx.Colour(248, 250, 252)       # Soft off-white
COLOR_BORDER = wx.Colour(226, 232, 240)         # Border gray
COLOR_TEXT_MAIN = wx.Colour(15, 23, 42)         # Dark slate
COLOR_TEXT_MUTED = wx.Colour(100, 116, 139)     # Cool gray


class RemoteShutdownDialog(wx.Dialog):
    """Modern modal dialog to configure and execute agentless remote shutdown/restart."""
    def __init__(self, parent, target_ip: str, target_host: str):
        super().__init__(parent, title="Agentless Remote Power Control", size=(460, 360),
                         style=wx.DEFAULT_DIALOG_STYLE | wx.RESIZE_BORDER)
        self.target_ip = target_ip
        self.target_host = target_host

        self.SetBackgroundColour(COLOR_BG_LIGHT)
        self._init_ui()
        self.CenterOnParent()

    def _init_ui(self):
        vbox = wx.BoxSizer(wx.VERTICAL)

        # Header Banner
        header = wx.Panel(self)
        header.SetBackgroundColour(COLOR_PRIMARY_DARK)
        h_sizer = wx.BoxSizer(wx.VERTICAL)
        lbl_title = wx.StaticText(header, label="Remote Power Off / Restart (Agentless)")
        lbl_title.SetFont(wx.Font(11, wx.FONTFAMILY_DEFAULT, wx.FONTSTYLE_NORMAL, wx.FONTWEIGHT_BOLD))
        lbl_title.SetForegroundColour(wx.Colour(255, 255, 255))
        lbl_sub = wx.StaticText(header, label=f"Target: {self.target_ip} ({self.target_host})")
        lbl_sub.SetForegroundColour(wx.Colour(203, 213, 225))
        h_sizer.Add(lbl_title, 0, wx.ALL, 6)
        h_sizer.Add(lbl_sub, 0, wx.LEFT | wx.BOTTOM, 6)
        header.SetSizer(h_sizer)
        vbox.Add(header, 0, wx.EXPAND)

        # Form Controls
        panel = wx.Panel(self)
        grid = wx.FlexGridSizer(rows=4, cols=2, vgap=10, hgap=10)
        grid.AddGrowableCol(1, 1)

        # Action Type (Shutdown or Restart)
        grid.Add(wx.StaticText(panel, label="Action:"), 0, wx.ALIGN_CENTER_VERTICAL)
        self.rb_shutdown = wx.RadioButton(panel, label="Power Off (Shutdown)", style=wx.RB_GROUP)
        self.rb_restart = wx.RadioButton(panel, label="Reboot (Restart)")
        r_box = wx.BoxSizer(wx.HORIZONTAL)
        r_box.Add(self.rb_shutdown, 0, wx.RIGHT, 10)
        r_box.Add(self.rb_restart, 0)
        grid.Add(r_box, 1, wx.EXPAND)

        # Timeout Seconds
        grid.Add(wx.StaticText(panel, label="Timeout (Seconds):"), 0, wx.ALIGN_CENTER_VERTICAL)
        self.sp_timeout = wx.SpinCtrl(panel, value="30", min=0, max=600)
        grid.Add(self.sp_timeout, 0, wx.EXPAND)

        # Force Close Apps
        grid.Add(wx.StaticText(panel, label="Force Close Apps:"), 0, wx.ALIGN_CENTER_VERTICAL)
        self.chk_force = wx.CheckBox(panel, label="Force kill hung applications (/f)")
        self.chk_force.SetValue(True)
        grid.Add(self.chk_force, 0, wx.EXPAND)

        # Message Banner
        grid.Add(wx.StaticText(panel, label="Notification Message:"), 0, wx.ALIGN_TOP)
        self.txt_msg = wx.TextCtrl(panel, value="Office Maintenance: This computer will power off shortly.",
                                   style=wx.TE_MULTILINE, size=(-1, 60))
        grid.Add(self.txt_msg, 1, wx.EXPAND)

        p_sizer = wx.BoxSizer(wx.VERTICAL)
        p_sizer.Add(grid, 1, wx.EXPAND | wx.ALL, 15)
        panel.SetSizer(p_sizer)
        vbox.Add(panel, 1, wx.EXPAND)

        # Buttons
        btn_sizer = wx.BoxSizer(wx.HORIZONTAL)
        self.btn_abort = wx.Button(self, label="Abort Pending Shutdown")
        self.btn_abort.SetToolTip("Send 'shutdown /a' to abort any scheduled shutdown on target PC.")
        self.btn_abort.Bind(wx.EVT_BUTTON, self.on_abort)

        self.btn_ok = wx.Button(self, wx.ID_OK, label="Execute Command")
        self.btn_ok.SetBackgroundColour(COLOR_ACCENT_RED)
        self.btn_ok.SetForegroundColour(wx.Colour(255, 255, 255))
        self.btn_ok.Bind(wx.EVT_BUTTON, self.on_execute)

        self.btn_cancel = wx.Button(self, wx.ID_CANCEL, label="Cancel")

        btn_sizer.Add(self.btn_abort, 0, wx.ALL, 10)
        btn_sizer.AddStretchSpacer()
        btn_sizer.Add(self.btn_cancel, 0, wx.ALL, 10)
        btn_sizer.Add(self.btn_ok, 0, wx.ALL, 10)
        vbox.Add(btn_sizer, 0, wx.EXPAND | wx.BOTTOM, 5)

        self.SetSizer(vbox)

    def on_execute(self, event):
        is_reboot = self.rb_restart.GetValue()
        timeout = self.sp_timeout.GetValue()
        force = self.chk_force.GetValue()
        msg = self.txt_msg.GetValue().strip()

        action_name = "RESTART" if is_reboot else "SHUTDOWN"
        confirm = wx.MessageBox(
            f"Are you sure you want to send {action_name} to {self.target_ip} ({self.target_host})?",
            "Confirm Agentless Power Control",
            wx.YES_NO | wx.NO_DEFAULT | wx.ICON_WARNING,
            self
        )
        if confirm != wx.YES:
            return

        success, result_msg = NetworkEngine.remote_power_off(
            ip=self.target_ip,
            timeout_sec=timeout,
            message=msg,
            reboot=is_reboot,
            force=force
        )

        icon = wx.ICON_INFORMATION if success else wx.ICON_ERROR
        title = "Operation Successful" if success else "Operation Failed"
        wx.MessageBox(result_msg, title, wx.OK | icon, self)
        if success:
            self.EndModal(wx.ID_OK)

    def on_abort(self, event):
        success, msg = NetworkEngine.abort_remote_shutdown(self.target_ip)
        icon = wx.ICON_INFORMATION if success else wx.ICON_WARNING
        wx.MessageBox(msg, "Abort Shutdown", wx.OK | icon, self)


class RemoteLaunchAppDialog(wx.Dialog):
    """Modern modal dialog to remotely execute applications agentlessly via WMI / WinRM."""
    def __init__(self, parent, target_ip: str, target_host: str):
        super().__init__(parent, title="Agentless Remote Application Launcher", size=(480, 290),
                         style=wx.DEFAULT_DIALOG_STYLE | wx.RESIZE_BORDER)
        self.target_ip = target_ip
        self.target_host = target_host
        self.SetBackgroundColour(COLOR_BG_LIGHT)
        self._init_ui()
        self.CenterOnParent()

    def _init_ui(self):
        vbox = wx.BoxSizer(wx.VERTICAL)

        # Header
        header = wx.Panel(self)
        header.SetBackgroundColour(COLOR_PRIMARY_DARK)
        h_sizer = wx.BoxSizer(wx.VERTICAL)
        lbl_title = wx.StaticText(header, label="Launch Remote Application (Agentless WMI)")
        lbl_title.SetFont(wx.Font(11, wx.FONTFAMILY_DEFAULT, wx.FONTSTYLE_NORMAL, wx.FONTWEIGHT_BOLD))
        lbl_title.SetForegroundColour(wx.Colour(255, 255, 255))
        lbl_sub = wx.StaticText(header, label=f"Target: {self.target_ip} ({self.target_host})")
        lbl_sub.SetForegroundColour(wx.Colour(203, 213, 225))
        h_sizer.Add(lbl_title, 0, wx.ALL, 6)
        h_sizer.Add(lbl_sub, 0, wx.LEFT | wx.BOTTOM, 6)
        header.SetSizer(h_sizer)
        vbox.Add(header, 0, wx.EXPAND)

        # Form
        panel = wx.Panel(self)
        grid = wx.FlexGridSizer(rows=2, cols=2, vgap=12, hgap=10)
        grid.AddGrowableCol(1, 1)

        grid.Add(wx.StaticText(panel, label="Preset Commands:"), 0, wx.ALIGN_CENTER_VERTICAL)
        self.cmb_preset = wx.ComboBox(panel, choices=[
            "Custom Command...",
            "notepad.exe",
            "calc.exe",
            "taskmgr.exe",
            "cmd.exe /c gpupdate /force",
            "powershell -Command \"Restart-Service Spooler\""
        ], style=wx.CB_READONLY)
        self.cmb_preset.SetSelection(1)
        self.cmb_preset.Bind(wx.EVT_COMBOBOX, self.on_preset_change)
        grid.Add(self.cmb_preset, 1, wx.EXPAND)

        grid.Add(wx.StaticText(panel, label="Command / Executable:"), 0, wx.ALIGN_CENTER_VERTICAL)
        self.txt_cmd = wx.TextCtrl(panel, value="notepad.exe")
        grid.Add(self.txt_cmd, 1, wx.EXPAND)

        p_sizer = wx.BoxSizer(wx.VERTICAL)
        p_sizer.Add(grid, 0, wx.EXPAND | wx.ALL, 15)

        note_lbl = wx.StaticText(panel, label="Note: Uses WMI Win32_Process.Create on target machine.\nTarget PC must have WMI and RPC enabled in Windows Firewall.")
        note_lbl.SetForegroundColour(COLOR_TEXT_MUTED)
        p_sizer.Add(note_lbl, 0, wx.LEFT | wx.RIGHT | wx.BOTTOM, 15)

        panel.SetSizer(p_sizer)
        vbox.Add(panel, 1, wx.EXPAND)

        # Buttons
        btn_sizer = wx.BoxSizer(wx.HORIZONTAL)
        self.btn_cancel = wx.Button(self, wx.ID_CANCEL, label="Cancel")
        self.btn_launch = wx.Button(self, wx.ID_OK, label="Launch on Target")
        self.btn_launch.SetBackgroundColour(COLOR_ACCENT_BLUE)
        self.btn_launch.SetForegroundColour(wx.Colour(255, 255, 255))
        self.btn_launch.Bind(wx.EVT_BUTTON, self.on_launch)

        btn_sizer.AddStretchSpacer()
        btn_sizer.Add(self.btn_cancel, 0, wx.ALL, 10)
        btn_sizer.Add(self.btn_launch, 0, wx.ALL, 10)
        vbox.Add(btn_sizer, 0, wx.EXPAND | wx.BOTTOM, 5)

        self.SetSizer(vbox)

    def on_preset_change(self, event):
        val = self.cmb_preset.GetStringSelection()
        if val != "Custom Command...":
            self.txt_cmd.SetValue(val)

    def on_launch(self, event):
        cmd = self.txt_cmd.GetValue().strip()
        if not cmd:
            wx.MessageBox("Please enter a command to execute.", "Validation Error", wx.OK | wx.ICON_WARNING, self)
            return

        success, msg = NetworkEngine.remote_launch_app(self.target_ip, cmd)
        icon = wx.ICON_INFORMATION if success else wx.ICON_ERROR
        title = "Execution Succeeded" if success else "Execution Error"
        wx.MessageBox(msg, title, wx.OK | icon, self)
        if success:
            self.EndModal(wx.ID_OK)


class MainDashboardFrame(wx.Frame):
    """Primary application frame for Office Network Monitoring & Control Dashboard."""

    def __init__(self):
        super().__init__(None, title="Office Network Monitoring & Control Dashboard (Agentless)",
                         size=(1100, 720))

        self.engine = NetworkEngine()
        self.devices: Dict[str, NetworkDevice] = {}  # ip -> NetworkDevice
        self.filtered_ips: List[str] = []

        # Server / Continuous Monitor State
        self.server_running = False
        self.monitor_thread: Optional[threading.Thread] = None
        self.monitor_stop_event = threading.Event()
        self.scan_in_progress = False

        self._init_ui()
        self._bind_events()

        self.SetMinSize((850, 550))
        self.Center()

        # Initial Subnet Auto-detect
        self.detect_network()

    def _init_ui(self):
        self.SetBackgroundColour(COLOR_BG_LIGHT)
        main_sizer = wx.BoxSizer(wx.VERTICAL)

        # 1. HEADER PANEL
        self.header_panel = self._create_header_panel()
        main_sizer.Add(self.header_panel, 0, wx.EXPAND)

        # 2. ACTION CONTROL PANEL (BARIS TOMBOL)
        self.action_panel = self._create_action_panel()
        main_sizer.Add(self.action_panel, 0, wx.EXPAND | wx.LEFT | wx.RIGHT | wx.TOP, 10)

        # Filter & Search Bar
        filter_bar = self._create_filter_bar()
        main_sizer.Add(filter_bar, 0, wx.EXPAND | wx.LEFT | wx.RIGHT | wx.TOP, 10)

        # 3. MAIN CONTENT AREA (ListCtrl)
        self.content_panel = self._create_content_panel()
        main_sizer.Add(self.content_panel, 1, wx.EXPAND | wx.ALL, 10)

        # Status Bar
        self.status_bar = self.CreateStatusBar(4)
        self.status_bar.SetStatusWidths([-3, -2, -2, -3])
        self.status_bar.SetStatusText("Ready. Select subnet and click 'Scan Network'.", 0)
        self.status_bar.SetStatusText("Devices: 0 Online / 0 Total", 1)
        self.status_bar.SetStatusText("Server: IDLE", 2)
        self.status_bar.SetStatusText(f"Host IP: {self.engine.get_local_ip()}", 3)

        self.SetSizer(main_sizer)

    def _create_header_panel(self) -> wx.Panel:
        panel = wx.Panel(self)
        panel.SetBackgroundColour(COLOR_PRIMARY_DARK)

        h_sizer = wx.BoxSizer(wx.HORIZONTAL)

        # App Brand & Icon
        brand_sizer = wx.BoxSizer(wx.VERTICAL)
        lbl_app_name = wx.StaticText(panel, label="NETWATCH DASHBOARD")
        lbl_app_name.SetFont(wx.Font(13, wx.FONTFAMILY_DEFAULT, wx.FONTSTYLE_NORMAL, wx.FONTWEIGHT_BOLD))
        lbl_app_name.SetForegroundColour(wx.Colour(255, 255, 255))

        lbl_desc = wx.StaticText(panel, label="Agentless Office Network Monitoring & Control")
        lbl_desc.SetFont(wx.Font(8, wx.FONTFAMILY_DEFAULT, wx.FONTSTYLE_NORMAL, wx.FONTWEIGHT_NORMAL))
        lbl_desc.SetForegroundColour(wx.Colour(148, 163, 184))

        brand_sizer.Add(lbl_app_name, 0, wx.TOP | wx.LEFT, 8)
        brand_sizer.Add(lbl_desc, 0, wx.BOTTOM | wx.LEFT, 8)
        h_sizer.Add(brand_sizer, 0, wx.ALIGN_CENTER_VERTICAL | wx.RIGHT, 20)

        h_sizer.AddStretchSpacer()

        # Network Subnet / Room ID Input Area
        sub_sizer = wx.BoxSizer(wx.HORIZONTAL)

        lbl_subnet = wx.StaticText(panel, label="Subnet / Room:")
        lbl_subnet.SetForegroundColour(wx.Colour(226, 232, 240))
        lbl_subnet.SetFont(wx.Font(9, wx.FONTFAMILY_DEFAULT, wx.FONTSTYLE_NORMAL, wx.FONTWEIGHT_BOLD))

        self.txt_subnet = wx.TextCtrl(panel, value="192.168.1", size=(120, 28))
        self.txt_subnet.SetToolTip("Enter first 3 octets of subnet, e.g., 192.168.1")

        self.btn_detect = wx.Button(panel, label="Auto Detect", size=(85, 28))
        self.btn_detect.SetToolTip("Auto-detect local network subnet")
        self.btn_detect.Bind(wx.EVT_BUTTON, lambda evt: self.detect_network())

        sub_sizer.Add(lbl_subnet, 0, wx.ALIGN_CENTER_VERTICAL | wx.RIGHT, 6)
        sub_sizer.Add(self.txt_subnet, 0, wx.ALIGN_CENTER_VERTICAL | wx.RIGHT, 6)
        sub_sizer.Add(self.btn_detect, 0, wx.ALIGN_CENTER_VERTICAL | wx.RIGHT, 15)

        h_sizer.Add(sub_sizer, 0, wx.ALIGN_CENTER_VERTICAL)

        # Server Toggle Button & Status Indicator
        server_box = wx.BoxSizer(wx.HORIZONTAL)

        self.btn_server_toggle = wx.Button(panel, label="Start Server", size=(110, 32))
        self.btn_server_toggle.SetBackgroundColour(COLOR_ACCENT_GREEN)
        self.btn_server_toggle.SetForegroundColour(wx.Colour(255, 255, 255))
        self.btn_server_toggle.SetFont(wx.Font(9, wx.FONTFAMILY_DEFAULT, wx.FONTSTYLE_NORMAL, wx.FONTWEIGHT_BOLD))
        self.btn_server_toggle.Bind(wx.EVT_BUTTON, self.on_toggle_server)

        # Connection Status LED Badge
        self.pnl_status_badge = wx.Panel(panel, size=(120, 32))
        self.pnl_status_badge.SetBackgroundColour(wx.Colour(51, 65, 85))
        badge_sizer = wx.BoxSizer(wx.HORIZONTAL)
        self.lbl_status_led = wx.StaticText(self.pnl_status_badge, label="● IDLE")
        self.lbl_status_led.SetFont(wx.Font(9, wx.FONTFAMILY_DEFAULT, wx.FONTSTYLE_NORMAL, wx.FONTWEIGHT_BOLD))
        self.lbl_status_led.SetForegroundColour(wx.Colour(148, 163, 184))
        badge_sizer.Add(self.lbl_status_led, 0, wx.ALL | wx.ALIGN_CENTER, 7)
        self.pnl_status_badge.SetSizer(badge_sizer)

        server_box.Add(self.btn_server_toggle, 0, wx.ALIGN_CENTER_VERTICAL | wx.RIGHT, 8)
        server_box.Add(self.pnl_status_badge, 0, wx.ALIGN_CENTER_VERTICAL | wx.RIGHT, 10)

        h_sizer.Add(server_box, 0, wx.ALIGN_CENTER_VERTICAL)

        panel.SetSizer(h_sizer)
        return panel

    def _create_action_panel(self) -> wx.Panel:
        """Creates the primary Action Control Panel with button row."""
        panel = wx.Panel(self)
        panel.SetBackgroundColour(wx.Colour(255, 255, 255))
        p_sizer = wx.BoxSizer(wx.HORIZONTAL)

        # Button: Scan Network
        self.btn_scan = wx.Button(panel, label="▶ Scan Network", size=(125, 34))
        self.btn_scan.SetBackgroundColour(COLOR_ACCENT_BLUE)
        self.btn_scan.SetForegroundColour(wx.Colour(255, 255, 255))
        self.btn_scan.SetFont(wx.Font(9, wx.FONTFAMILY_DEFAULT, wx.FONTSTYLE_NORMAL, wx.FONTWEIGHT_BOLD))
        self.btn_scan.Bind(wx.EVT_BUTTON, self.on_scan_network)
        self.btn_scan.SetToolTip("Initiates multi-threaded scan across all 254 IP addresses in subnet.")
        p_sizer.Add(self.btn_scan, 0, wx.ALL, 6)

        # Button: Ping All
        self.btn_ping_all = wx.Button(panel, label="⚡ Ping All", size=(100, 34))
        self.btn_ping_all.SetBackgroundColour(wx.Colour(14, 165, 233))
        self.btn_ping_all.SetForegroundColour(wx.Colour(255, 255, 255))
        self.btn_ping_all.Bind(wx.EVT_BUTTON, self.on_ping_all)
        self.btn_ping_all.SetToolTip("Quickly tests latency for all known devices.")
        p_sizer.Add(self.btn_ping_all, 0, wx.ALL, 6)

        p_sizer.Add(wx.StaticLine(panel, style=wx.LI_VERTICAL), 0, wx.EXPAND | wx.ALL, 6)

        # Button: Power Off PC (Agentless WMI / RPC)
        self.btn_power_off = wx.Button(panel, label="⏻ Power Off PC", size=(125, 34))
        self.btn_power_off.SetBackgroundColour(COLOR_ACCENT_RED)
        self.btn_power_off.SetForegroundColour(wx.Colour(255, 255, 255))
        self.btn_power_off.Bind(wx.EVT_BUTTON, self.on_power_off_click)
        self.btn_power_off.SetToolTip("Execute remote shutdown or restart on selected PC without agent.")
        p_sizer.Add(self.btn_power_off, 0, wx.ALL, 6)

        # Button: Launch App (Agentless WMI)
        self.btn_launch_app = wx.Button(panel, label="🚀 Launch App", size=(115, 34))
        self.btn_launch_app.SetBackgroundColour(wx.Colour(124, 58, 237))
        self.btn_launch_app.SetForegroundColour(wx.Colour(255, 255, 255))
        self.btn_launch_app.Bind(wx.EVT_BUTTON, self.on_launch_app_click)
        self.btn_launch_app.SetToolTip("Execute remote process / script on selected PC via WMI.")
        p_sizer.Add(self.btn_launch_app, 0, wx.ALL, 6)

        p_sizer.Add(wx.StaticLine(panel, style=wx.LI_VERTICAL), 0, wx.EXPAND | wx.ALL, 6)

        # Button: Wake-on-LAN
        self.btn_wol = wx.Button(panel, label="🔔 Wake-on-LAN", size=(115, 34))
        self.btn_wol.SetBackgroundColour(COLOR_ACCENT_AMBER)
        self.btn_wol.SetForegroundColour(wx.Colour(255, 255, 255))
        self.btn_wol.Bind(wx.EVT_BUTTON, self.on_wake_on_lan)
        self.btn_wol.SetToolTip("Send Magic Packet UDP to boot selected PC.")
        p_sizer.Add(self.btn_wol, 0, wx.ALL, 6)

        # Button: Remote Desktop (mstsc)
        self.btn_rdp = wx.Button(panel, label="💻 Remote Desktop", size=(125, 34))
        self.btn_rdp.Bind(wx.EVT_BUTTON, self.on_remote_desktop)
        self.btn_rdp.SetToolTip("Open native Windows Remote Desktop (mstsc) to selected IP.")
        p_sizer.Add(self.btn_rdp, 0, wx.ALL, 6)

        p_sizer.AddStretchSpacer()

        # Gauge / Progress bar for scan
        self.gauge = wx.Gauge(panel, range=254, size=(160, 20), style=wx.GA_HORIZONTAL | wx.GA_SMOOTH)
        self.gauge.SetValue(0)
        self.gauge.Hide()  # Hidden until scan runs
        p_sizer.Add(self.gauge, 0, wx.ALIGN_CENTER_VERTICAL | wx.RIGHT, 10)

        panel.SetSizer(p_sizer)
        return panel

    def _create_filter_bar(self) -> wx.Panel:
        """Search and filter input box."""
        panel = wx.Panel(self)
        sizer = wx.BoxSizer(wx.HORIZONTAL)

        lbl = wx.StaticText(panel, label="Search / Filter:")
        lbl.SetForegroundColour(COLOR_TEXT_MUTED)
        self.txt_filter = wx.TextCtrl(panel, size=(240, 26))
        self.txt_filter.SetHint("Filter by IP, Hostname, or MAC...")
        self.txt_filter.Bind(wx.EVT_TEXT, self.on_filter_changed)

        btn_clear = wx.Button(panel, label="Clear", size=(60, 26))
        btn_clear.Bind(wx.EVT_BUTTON, lambda evt: self.txt_filter.SetValue(""))

        self.btn_export = wx.Button(panel, label="Export CSV", size=(85, 26))
        self.btn_export.Bind(wx.EVT_BUTTON, self.on_export_csv)

        sizer.Add(lbl, 0, wx.ALIGN_CENTER_VERTICAL | wx.RIGHT, 6)
        sizer.Add(self.txt_filter, 0, wx.ALIGN_CENTER_VERTICAL | wx.RIGHT, 6)
        sizer.Add(btn_clear, 0, wx.ALIGN_CENTER_VERTICAL | wx.RIGHT, 15)
        sizer.AddStretchSpacer()
        sizer.Add(self.btn_export, 0, wx.ALIGN_CENTER_VERTICAL)

        panel.SetSizer(sizer)
        return panel

    def _create_content_panel(self) -> wx.Panel:
        """Main Content Area: wx.ListCtrl displaying devices in real-time."""
        panel = wx.Panel(self)
        p_sizer = wx.BoxSizer(wx.VERTICAL)

        self.list_ctrl = wx.ListCtrl(
            panel,
            style=wx.LC_REPORT | wx.LC_VRULES | wx.LC_HRULES | wx.LC_SINGLE_SEL
        )

        # Setup Columns
        self.list_ctrl.InsertColumn(0, "IP Address", width=140)
        self.list_ctrl.InsertColumn(1, "MAC Address", width=160)
        self.list_ctrl.InsertColumn(2, "Hostname", width=220)
        self.list_ctrl.InsertColumn(3, "Status", width=120)
        self.list_ctrl.InsertColumn(4, "Latency", width=100)
        self.list_ctrl.InsertColumn(5, "Last Seen", width=120)

        # Context menu
        self.list_ctrl.Bind(wx.EVT_LIST_ITEM_RIGHT_CLICK, self.on_right_click)
        self.list_ctrl.Bind(wx.EVT_LIST_ITEM_ACTIVATED, self.on_item_double_click)

        p_sizer.Add(self.list_ctrl, 1, wx.EXPAND)
        panel.SetSizer(p_sizer)
        return panel

    def _bind_events(self):
        self.Bind(EVT_SCAN_PROGRESS, self.on_scan_progress_event)
        self.Bind(EVT_SCAN_COMPLETE, self.on_scan_complete_event)
        self.Bind(EVT_DEVICE_UPDATED, self.on_device_updated_event)
        self.Bind(EVT_MONITOR_TICK, self.on_monitor_tick_event)
        self.Bind(wx.EVT_CLOSE, self.on_close_window)

    # -------------------------------------------------------------
    # NETWORK LOGIC & THREADING
    # -------------------------------------------------------------

    def detect_network(self):
        """Auto detects the primary network subnet."""
        subnet = self.engine.get_default_subnet()
        self.txt_subnet.SetValue(subnet)
        self.status_bar.SetStatusText(f"Detected subnet: {subnet}.0/24", 0)

    def on_scan_network(self, event):
        """Starts multi-threaded background network scan."""
        if self.scan_in_progress:
            # Cancel current scan
            self.engine.cancel_scan()
            self.btn_scan.SetLabel("▶ Scan Network")
            self.scan_in_progress = False
            self.gauge.Hide()
            self.action_panel.Layout()
            return

        subnet = self.txt_subnet.GetValue().strip()
        parts = subnet.split(".")
        if len(parts) != 3 or not all(p.isdigit() and 0 <= int(p) <= 255 for p in parts):
            wx.MessageBox("Invalid subnet format. Expected format like '192.168.1' (3 octets).",
                            "Validation Error", wx.OK | wx.ICON_WARNING, self)
            return

        self.scan_in_progress = True
        self.btn_scan.SetLabel("⏹ Cancel Scan")
        self.gauge.SetValue(0)
        self.gauge.SetRange(254)
        self.gauge.Show()
        self.action_panel.Layout()

        self.status_bar.SetStatusText(f"Scanning subnet {subnet}.1 - {subnet}.254...", 0)

        # Run scanner in separate worker thread
        def scan_worker():
            def progress(current, total, dev: NetworkDevice):
                wx.PostEvent(self, ScanProgressEvent(current=current, total=total, device=dev))

            start_t = time.time()
            devs = self.engine.scan_subnet(subnet, 1, 254, progress_callback=progress, max_workers=50)
            elapsed = time.time() - start_t
            wx.PostEvent(self, ScanCompleteEvent(devices=devs, elapsed=elapsed))

        threading.Thread(target=scan_worker, daemon=True).name = "ScanWorkerThread"
        threading.Thread(target=scan_worker, daemon=True).start()

    def on_scan_progress_event(self, event):
        """Triggered from worker thread as each IP finishes."""
        self.gauge.SetValue(event.current)
        dev: NetworkDevice = event.device
        if dev.status != "Offline" or dev.mac != "Unknown":
            self.devices[dev.ip] = dev
            self.refresh_table()

    def on_scan_complete_event(self, event):
        """Triggered when scan completes."""
        self.scan_in_progress = False
        self.btn_scan.SetLabel("▶ Scan Network")
        self.gauge.Hide()
        self.action_panel.Layout()

        # Update devices dict with all found
        for d in event.devices:
            self.devices[d.ip] = d

        self.refresh_table()
        online_count = sum(1 for d in self.devices.values() if "Online" in d.status)
        total_count = len(self.devices)
        self.status_bar.SetStatusText(
            f"Scan completed in {event.elapsed:.1f}s. Found {total_count} devices ({online_count} online).", 0
        )
        self.status_bar.SetStatusText(f"Devices: {online_count} Online / {total_count} Total", 1)

    def on_ping_all(self, event):
        """Quickly re-pings only the discovered devices in parallel."""
        if not self.devices:
            wx.MessageBox("No devices to ping. Run a network scan first.", "Information", wx.OK | wx.ICON_INFORMATION, self)
            return

        self.status_bar.SetStatusText("Pinging all known devices...", 0)

        def ping_worker():
            for ip in list(self.devices.keys()):
                online, lat = self.engine.ping_single(ip, timeout_ms=400)
                dev = self.devices[ip]
                dev.status = "Online" if online else "Offline"
                dev.latency = lat or "-"
                dev.last_seen = time.strftime("%H:%M:%S")
                wx.PostEvent(self, DeviceUpdatedEvent(device=dev))

        threading.Thread(target=ping_worker, daemon=True).start()

    def on_device_updated_event(self, event):
        """Updates a single device's status in the list."""
        dev = event.device
        self.devices[dev.ip] = dev
        self.refresh_table()

    # -------------------------------------------------------------
    # SERVER / CONTINUOUS MONITORING
    # -------------------------------------------------------------

    def on_toggle_server(self, event):
        """Starts or stops the continuous monitoring server thread."""
        if not self.server_running:
            self.start_monitoring_server()
        else:
            self.stop_monitoring_server()

    def start_monitoring_server(self):
        """Activates continuous background network monitoring."""
        self.server_running = True
        self.monitor_stop_event.clear()

        self.btn_server_toggle.SetLabel("Stop Server")
        self.btn_server_toggle.SetBackgroundColour(COLOR_ACCENT_RED)
        self.btn_server_toggle.SetForegroundColour(wx.Colour(255, 255, 255))

        self.pnl_status_badge.SetBackgroundColour(COLOR_ACCENT_GREEN)
        self.lbl_status_led.SetLabel("● ACTIVE")
        self.lbl_status_led.SetForegroundColour(wx.Colour(255, 255, 255))
        self.header_panel.Layout()

        self.status_bar.SetStatusText("Continuous Monitoring: RUNNING (Every 20s)", 2)

        def server_loop():
            while not self.monitor_stop_event.is_set():
                if self.devices:
                    for ip in list(self.devices.keys()):
                        if self.monitor_stop_event.is_set():
                            break
                        online, lat = self.engine.ping_single(ip, timeout_ms=300)
                        dev = self.devices[ip]
                        dev.status = "Online" if online else "Offline"
                        dev.latency = lat or "-"
                        dev.last_seen = time.strftime("%H:%M:%S")
                        wx.PostEvent(self, DeviceUpdatedEvent(device=dev))
                wx.PostEvent(self, MonitorTickEvent())
                # Wait 20 seconds or until stopped
                self.monitor_stop_event.wait(timeout=20.0)

        self.monitor_thread = threading.Thread(target=server_loop, daemon=True)
        self.monitor_thread.start()

    def stop_monitoring_server(self):
        """Stops continuous background monitoring."""
        self.server_running = False
        self.monitor_stop_event.set()

        self.btn_server_toggle.SetLabel("Start Server")
        self.btn_server_toggle.SetBackgroundColour(COLOR_ACCENT_GREEN)
        self.btn_server_toggle.SetForegroundColour(wx.Colour(255, 255, 255))

        self.pnl_status_badge.SetBackgroundColour(wx.Colour(51, 65, 85))
        self.lbl_status_led.SetLabel("● IDLE")
        self.lbl_status_led.SetForegroundColour(wx.Colour(148, 163, 184))
        self.header_panel.Layout()

        self.status_bar.SetStatusText("Server: IDLE", 2)

    def on_monitor_tick_event(self, event):
        online_count = sum(1 for d in self.devices.values() if "Online" in d.status)
        total_count = len(self.devices)
        self.status_bar.SetStatusText(f"Devices: {online_count} Online / {total_count} Total", 1)

    # -------------------------------------------------------------
    # LIST CONTROL & FILTERING
    # -------------------------------------------------------------

    def on_filter_changed(self, event):
        self.refresh_table()

    def refresh_table(self):
        """Refreshes the ListCtrl view based on current device dict and filter term."""
        filter_text = self.txt_filter.GetValue().strip().lower()

        # Build list of matching IPs
        sorted_ips = sorted(
            self.devices.keys(),
            key=lambda ip: [int(x) for x in ip.split(".") if x.isdigit()]
        )

        self.filtered_ips = []
        for ip in sorted_ips:
            dev = self.devices[ip]
            if filter_text:
                matched = (filter_text in dev.ip.lower() or
                           filter_text in dev.mac.lower() or
                           filter_text in dev.hostname.lower() or
                           filter_text in dev.status.lower())
                if not matched:
                    continue
            self.filtered_ips.append(ip)

        self.list_ctrl.DeleteAllItems()

        for idx, ip in enumerate(self.filtered_ips):
            dev = self.devices[ip]
            self.list_ctrl.InsertItem(idx, dev.ip)
            self.list_ctrl.SetItem(idx, 1, dev.mac)
            self.list_ctrl.SetItem(idx, 2, dev.hostname)
            self.list_ctrl.SetItem(idx, 3, dev.status)
            self.list_ctrl.SetItem(idx, 4, dev.latency)
            self.list_ctrl.SetItem(idx, 5, dev.last_seen)

            # Styling row based on status
            if dev.status == "Online":
                self.list_ctrl.SetItemTextColour(idx, wx.Colour(16, 140, 95))
            elif "ARP" in dev.status:
                self.list_ctrl.SetItemTextColour(idx, wx.Colour(180, 110, 0))
            else:
                self.list_ctrl.SetItemTextColour(idx, wx.Colour(180, 50, 50))

            # Alternate row background
            if idx % 2 == 1:
                self.list_ctrl.SetItemBackgroundColour(idx, wx.Colour(241, 245, 249))
            else:
                self.list_ctrl.SetItemBackgroundColour(idx, wx.Colour(255, 255, 255))

    def get_selected_device(self) -> Optional[NetworkDevice]:
        """Returns the currently selected NetworkDevice or None."""
        idx = self.list_ctrl.GetFirstSelected()
        if idx != -1 and idx < len(self.filtered_ips):
            ip = self.filtered_ips[idx]
            return self.devices.get(ip)
        return None

    # -------------------------------------------------------------
    # ACTION CONTROL HANDLERS
    # -------------------------------------------------------------

    def on_power_off_click(self, event):
        dev = self.get_selected_device()
        if not dev:
            wx.MessageBox("Please select a target PC from the list first.", "Target Required",
                            wx.OK | wx.ICON_WARNING, self)
            return

        dlg = RemoteShutdownDialog(self, dev.ip, dev.hostname)
        dlg.ShowModal()
        dlg.Destroy()

    def on_launch_app_click(self, event):
        dev = self.get_selected_device()
        if not dev:
            wx.MessageBox("Please select a target PC from the list first.", "Target Required",
                            wx.OK | wx.ICON_WARNING, self)
            return

        dlg = RemoteLaunchAppDialog(self, dev.ip, dev.hostname)
        dlg.ShowModal()
        dlg.Destroy()

    def on_wake_on_lan(self, event):
        dev = self.get_selected_device()
        if not dev:
            wx.MessageBox("Please select a PC from the list first.", "Target Required",
                            wx.OK | wx.ICON_WARNING, self)
            return

        if not dev.mac or dev.mac == "Unknown":
            wx.MessageBox(f"MAC Address for {dev.ip} is not known. Cannot send Wake-on-LAN.",
                            "Unknown MAC", wx.OK | wx.ICON_ERROR, self)
            return

        success, msg = NetworkEngine.send_wake_on_lan(dev.mac)
        icon = wx.ICON_INFORMATION if success else wx.ICON_ERROR
        wx.MessageBox(msg, "Wake-on-LAN Result", wx.OK | icon, self)

    def on_remote_desktop(self, event):
        dev = self.get_selected_device()
        if not dev:
            wx.MessageBox("Please select a PC from the list first.", "Target Required",
                            wx.OK | wx.ICON_WARNING, self)
            return

        try:
            subprocess.Popen(["mstsc", f"/v:{dev.ip}"])
            self.status_bar.SetStatusText(f"Launched Remote Desktop for {dev.ip}", 0)
        except Exception as e:
            wx.MessageBox(f"Failed to launch mstsc: {str(e)}", "Error", wx.OK | wx.ICON_ERROR, self)

    def on_item_double_click(self, event):
        """Double clicking a device triggers ping test."""
        dev = self.get_selected_device()
        if not dev:
            return

        online, lat = self.engine.ping_single(dev.ip, timeout_ms=600)
        dev.status = "Online" if online else "Offline"
        dev.latency = lat or "-"
        dev.last_seen = time.strftime("%H:%M:%S")
        self.refresh_table()

    def on_right_click(self, event):
        """Displays contextual right-click menu for selected PC."""
        dev = self.get_selected_device()
        if not dev:
            return

        menu = wx.Menu()
        m_ping = menu.Append(wx.ID_ANY, f"Ping {dev.ip}")
        m_shutdown = menu.Append(wx.ID_ANY, "Remote Power Off / Restart...")
        m_launch = menu.Append(wx.ID_ANY, "Launch Remote Application...")
        m_wol = menu.Append(wx.ID_ANY, "Send Wake-on-LAN (WOL)")
        menu.AppendSeparator()
        m_rdp = menu.Append(wx.ID_ANY, "Connect with Remote Desktop (mstsc)")
        m_share = menu.Append(wx.ID_ANY, f"Open Network Share (\\\\{dev.ip}\\c$)")
        menu.AppendSeparator()
        m_copy_ip = menu.Append(wx.ID_ANY, "Copy IP Address")
        m_copy_mac = menu.Append(wx.ID_ANY, "Copy MAC Address")

        self.Bind(wx.EVT_MENU, lambda evt: self.on_item_double_click(evt), m_ping)
        self.Bind(wx.EVT_MENU, lambda evt: self.on_power_off_click(evt), m_shutdown)
        self.Bind(wx.EVT_MENU, lambda evt: self.on_launch_app_click(evt), m_launch)
        self.Bind(wx.EVT_MENU, lambda evt: self.on_wake_on_lan(evt), m_wol)
        self.Bind(wx.EVT_MENU, lambda evt: self.on_remote_desktop(evt), m_rdp)
        self.Bind(wx.EVT_MENU, lambda evt: self.open_share(dev.ip), m_share)
        self.Bind(wx.EVT_MENU, lambda evt: self.copy_to_clipboard(dev.ip), m_copy_ip)
        self.Bind(wx.EVT_MENU, lambda evt: self.copy_to_clipboard(dev.mac), m_copy_mac)

        self.PopupMenu(menu)
        menu.Destroy()

    def open_share(self, ip: str):
        try:
            os.startfile(f"\\\\{ip}\\c$")
        except Exception as e:
            wx.MessageBox(f"Failed to open share: {str(e)}", "Network Share Error", wx.OK | wx.ICON_ERROR, self)

    def copy_to_clipboard(self, text: str):
        if wx.TheClipboard.Open():
            wx.TheClipboard.SetData(wx.TextDataObject(text))
            wx.TheClipboard.Close()
            self.status_bar.SetStatusText(f"Copied '{text}' to clipboard.", 0)

    def on_export_csv(self, event):
        """Exports current device list to CSV."""
        if not self.devices:
            wx.MessageBox("No devices to export.", "Information", wx.OK | wx.ICON_INFORMATION, self)
            return

        with wx.FileDialog(self, "Export Devices to CSV", wildcard="CSV files (*.csv)|*.csv",
                           style=wx.FD_SAVE | wx.FD_OVERWRITE_PROMPT) as fileDialog:
            if fileDialog.ShowModal() == wx.ID_CANCEL:
                return

            path = fileDialog.GetPath()
            try:
                with open(path, "w", newline="", encoding="utf-8") as f:
                    writer = csv.writer(f)
                    writer.writerow(["IP Address", "MAC Address", "Hostname", "Status", "Latency", "Last Seen"])
                    for ip in self.filtered_ips:
                        d = self.devices[ip]
                        writer.writerow([d.ip, d.mac, d.hostname, d.status, d.latency, d.last_seen])

                wx.MessageBox(f"Exported successfully to:\n{path}", "Export Complete", wx.OK | wx.ICON_INFORMATION, self)
            except Exception as e:
                wx.MessageBox(f"Export failed: {str(e)}", "Export Error", wx.OK | wx.ICON_ERROR, self)

    def on_close_window(self, event):
        """Cleanup before exit."""
        if self.server_running:
            self.stop_monitoring_server()
        if self.scan_in_progress:
            self.engine.cancel_scan()
        event.Skip()
