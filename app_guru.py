"""
app_guru.py - Native Windows Desktop Application Launcher for LabGuard PRO Teacher
Launches the dashboard in dedicated, clean standalone App Mode (Chrome / Edge engine).
No address bar, no tabs, no 'Not secure' badge, isolated window and taskbar icon.
"""

import os
import sys
import time
import socket
import urllib.request
import subprocess
import platform

BASE_DIR = os.path.dirname(os.path.abspath(__file__))
os.chdir(BASE_DIR)

SERVER_PORT = 8000
SERVER_URL = f"http://localhost:{SERVER_PORT}/"
CHECK_URL = f"http://127.0.0.1:{SERVER_PORT}/api/server-info"


def is_server_running(port=8000, host="127.0.0.1"):
    """Fast socket check if server port is actively listening."""
    s = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    s.settimeout(0.5)
    try:
        s.connect((host, port))
        s.close()
        return True
    except Exception:
        return False


def wait_for_server(timeout=10.0):
    """Polls server-info until FastAPI server responds with 200 OK."""
    start = time.time()
    while time.time() - start < timeout:
        try:
            req = urllib.request.Request(CHECK_URL, headers={"User-Agent": "LabGuardApp/2.2"})
            with urllib.request.urlopen(req, timeout=1.0) as resp:
                if resp.status == 200:
                    return True
        except Exception:
            pass
        time.sleep(0.3)
    return False


def ensure_backend_running():
    """Starts server_web.py in the background if not already running."""
    if is_server_running(SERVER_PORT):
        return True

    server_script = os.path.join(BASE_DIR, "server_web.py")
    if not os.path.exists(server_script):
        return False

    py_dir = os.path.dirname(sys.executable)
    py_exec = sys.executable
    pythonw_path = os.path.join(py_dir, "pythonw.exe")

    log_path = os.path.join(BASE_DIR, "server_web.log")

    # Attempt 1: Start using pythonw.exe or python.exe with CREATE_NO_WINDOW
    chosen_py = pythonw_path if os.path.exists(pythonw_path) else py_exec
    flags = 0x08000000 if platform.system() == "Windows" else 0  # CREATE_NO_WINDOW

    try:
        log_f = open(log_path, "a", encoding="utf-8")
        subprocess.Popen(
            [chosen_py, server_script],
            cwd=BASE_DIR,
            creationflags=flags,
            stdin=subprocess.DEVNULL,
            stdout=log_f,
            stderr=log_f,
            close_fds=False
        )
        if wait_for_server(7.0):
            return True
    except Exception as e:
        print(f"[Warning] Silent spawn failed: {e}")

    # Attempt 2: Fallback using standard python.exe
    if chosen_py != py_exec:
        try:
            log_f = open(log_path, "a", encoding="utf-8")
            subprocess.Popen(
                [py_exec, server_script],
                cwd=BASE_DIR,
                creationflags=flags,
                stdin=subprocess.DEVNULL,
                stdout=log_f,
                stderr=log_f,
                close_fds=False
            )
            if wait_for_server(7.0):
                return True
        except Exception as e:
            print(f"[Warning] Fallback python spawn failed: {e}")

    return False


def find_browser_app_executable():
    """Locates Chrome or Microsoft Edge executable for native standalone app mode."""
    candidates = [
        r"C:\Program Files\Google\Chrome\Application\chrome.exe",
        r"C:\Program Files (x86)\Google\Chrome\Application\chrome.exe",
        os.path.expandvars(r"%LOCALAPPDATA%\Google\Chrome\Application\chrome.exe"),
        r"C:\Program Files (x86)\Microsoft\Edge\Application\msedge.exe",
        r"C:\Program Files\Microsoft\Edge\Application\msedge.exe",
    ]
    for exe in candidates:
        if os.path.exists(exe):
            return exe
    return None


def launch_app_window():
    """Launches the clean, standalone desktop application window."""
    ensure_backend_running()

    browser_exe = find_browser_app_executable()
    app_data_dir = os.path.join(os.environ.get("LOCALAPPDATA", BASE_DIR), "LabGuardTeacherApp")
    os.makedirs(app_data_dir, exist_ok=True)

    if browser_exe:
        # Launch dedicated standalone App window:
        # --app: frameless, no url bar, no tabs, no bookmark bar
        # --user-data-dir: isolated from normal browser tabs and extensions
        cmd = [
            browser_exe,
            f"--app={SERVER_URL}",
            f"--user-data-dir={app_data_dir}",
            "--window-size=1366,820",
            "--start-maximized"
        ]
        try:
            subprocess.Popen(cmd)
            return True
        except Exception as e:
            print(f"[Warning] Gagal membuka browser app: {e}")

    # Fallback to default browser
    import webbrowser
    webbrowser.open(SERVER_URL)
    return True


if __name__ == "__main__":
    launch_app_window()
