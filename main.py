"""
main.py - Titik Masuk Utama Aplikasi Monitoring Lab Komputer Sekolah (wxPython)
Mengonfigurasi kesadaran DPI Windows, menginisialisasi aplikasi wx, dan membuka Teacher Dashboard.
"""

import ctypes
import os
import sys

import wx

from server_dashboard import TeacherDashboardFrame


def set_high_dpi_awareness():
    """Enables crisp, high-DPI scaling on Windows 10/11."""
    if os.name == 'nt':
        try:
            # SetProcessDpiAwareness(2) -> Process_Per_Monitor_DPI_Aware
            ctypes.windll.shcore.SetProcessDpiAwareness(2)
        except Exception:
            try:
                ctypes.windll.user32.SetProcessDPIAware()
            except Exception:
                pass


def main():
    set_high_dpi_awareness()

    app = wx.App(False)
    frame = TeacherDashboardFrame()
    frame.Show()
    app.MainLoop()


if __name__ == "__main__":
    main()
