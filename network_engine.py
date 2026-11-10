"""
network_engine.py - Agentless Network Engine for Office Network Monitoring & Control.
Handles multi-threaded ICMP pinging, ARP table inspection, hostname resolution,
Wake-on-LAN magic packet sending, and agentless remote control (WMI / RPC / WinRM).
"""

import os
import re
import socket
import struct
import subprocess
import threading
from concurrent.futures import ThreadPoolExecutor, as_completed
from datetime import datetime
from typing import Callable, Dict, List, Optional, Tuple


class NetworkDevice:
    """Represents a monitored network host/device."""
    def __init__(self, ip: str, mac: str = "Unknown", hostname: str = "Unknown",
                 status: str = "Offline", latency: str = "-"):
        self.ip = ip
        self.mac = mac
        self.hostname = hostname
        self.status = status  # "Online", "Offline", "Online (ARP)"
        self.latency = latency
        self.last_seen = datetime.now().strftime("%H:%M:%S")

    def to_dict(self) -> Dict[str, str]:
        return {
            "ip": self.ip,
            "mac": self.mac,
            "hostname": self.hostname,
            "status": self.status,
            "latency": self.latency,
            "last_seen": self.last_seen
        }


class NetworkEngine:
    """High-performance agentless network scanner and remote administrator."""

    def __init__(self):
        self._arp_cache: Dict[str, str] = {}
        self._cancel_scan = False

    @staticmethod
    def get_local_ip() -> str:
        """Auto-detects the machine's primary local IP address."""
        try:
            s = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
            # Connecting to a public address (doesn't send actual data) to determine interface
            s.connect(("8.8.8.8", 80))
            ip = s.getsockname()[0]
            s.close()
            return ip
        except Exception:
            try:
                return socket.gethostbyname(socket.gethostname())
            except Exception:
                return "192.168.1.1"

    @classmethod
    def get_default_subnet(cls) -> str:
        """Returns the first 3 octets of the local IP, e.g., '192.168.1'."""
        local_ip = cls.get_local_ip()
        parts = local_ip.split(".")
        if len(parts) == 4:
            return f"{parts[0]}.{parts[1]}.{parts[2]}"
        return "192.168.1"

    def refresh_arp_cache(self) -> Dict[str, str]:
        """
        Parses Windows ARP table ('arp -a') to map IP -> MAC Address.
        Does not require administrative privileges.
        """
        cache = {}
        try:
            # Hide console window in Windows
            startupinfo = None
            if os.name == 'nt':
                startupinfo = subprocess.STARTUPINFO()
                startupinfo.dwFlags |= subprocess.STARTF_USESHOWWINDOW

            proc = subprocess.run(
                ["arp", "-a"],
                capture_output=True,
                text=True,
                timeout=5,
                startupinfo=startupinfo,
                check=False
            )
            # Regex for IP and MAC address pairs
            # e.g., '10.161.17.1   2c-3f-0b-b7-b6-40   dynamic'
            pattern = re.compile(
                r'(\d{1,3}(?:\.\d{1,3}){3})\s+([0-9a-fA-F]{2}(?:-[0-9a-fA-F]{2}){5})'
            )
            for line in proc.stdout.splitlines():
                match = pattern.search(line)
                if match:
                    ip, mac = match.groups()
                    cache[ip] = mac.upper()
        except Exception:
            pass

        self._arp_cache = cache
        return cache

    def resolve_hostname(self, ip: str) -> str:
        """
        Resolves hostname agentlessly using DNS reverse lookup and NetBIOS as fallback.
        """
        # 1. Reverse DNS
        try:
            name, _, _ = socket.gethostbyaddr(ip)
            if name and name != ip:
                # Return short hostname if FQDN
                return name.split(".")[0]
        except (socket.herror, socket.gaierror, socket.timeout):
            pass

        # 2. NetBIOS lookup via nbtstat on Windows
        if os.name == 'nt':
            try:
                startupinfo = subprocess.STARTUPINFO()
                startupinfo.dwFlags |= subprocess.STARTF_USESHOWWINDOW
                proc = subprocess.run(
                    ["nbtstat", "-A", ip],
                    capture_output=True,
                    text=True,
                    timeout=2,
                    startupinfo=startupinfo,
                    check=False
                )
                # Look for '<00>  UNIQUE'
                match = re.search(r'^\s*([A-Za-z0-9_\-\.]+)\s+<00>\s+UNIQUE', proc.stdout, re.MULTILINE)
                if match:
                    return match.group(1).strip()
            except Exception:
                pass

        return "Unknown"

    def ping_single(self, ip: str, timeout_ms: int = 500) -> Tuple[bool, Optional[str]]:
        """
        Sends 1 ICMP Echo request to test reachability and measures round-trip latency.
        Returns: (is_online, latency_string)
        """
        try:
            startupinfo = None
            if os.name == 'nt':
                startupinfo = subprocess.STARTUPINFO()
                startupinfo.dwFlags |= subprocess.STARTF_USESHOWWINDOW

            cmd = ["ping", "-n", "1", "-w", str(timeout_ms), ip]
            proc = subprocess.run(
                cmd,
                capture_output=True,
                text=True,
                timeout=(timeout_ms / 1000.0) + 1.0,
                startupinfo=startupinfo,
                check=False
            )

            if proc.returncode == 0:
                # Parse latency: 'time=12ms' or 'time<1ms' or Indonesian 'waktu=12ms'
                match = re.search(r'(?:time|waktu)[=<](\d+)\s*ms', proc.stdout, re.IGNORECASE)
                if match:
                    return True, f"{match.group(1)} ms"
                elif "<1ms" in proc.stdout.lower() or "< 1ms" in proc.stdout.lower():
                    return True, "<1 ms"
                return True, "1 ms"
        except Exception:
            pass

        return False, None

    def probe_device(self, ip: str, resolve_names: bool = True) -> NetworkDevice:
        """
        Comprehensive agentless probe for a single IP:
        Checks ping, ARP table, latency, and hostname.
        """
        is_online, latency = self.ping_single(ip, timeout_ms=450)
        mac = self._arp_cache.get(ip, "Unknown")

        # If ping timed out, check if an ARP entry was dynamically populated
        if not is_online and mac != "Unknown" and mac != "FF-FF-FF-FF-FF-FF":
            # PC is online on layer 2 (ARP), but ICMP ping is blocked by Windows Firewall
            is_online = True
            status = "Online (ARP)"
            latency = "N/A"
        elif is_online:
            status = "Online"
            latency = latency or "1 ms"
        else:
            status = "Offline"
            latency = "-"

        hostname = "Unknown"
        if is_online and resolve_names:
            hostname = self.resolve_hostname(ip)

        device = NetworkDevice(
            ip=ip,
            mac=mac,
            hostname=hostname,
            status=status,
            latency=latency
        )
        return device

    def scan_subnet(self, subnet: str, start_ip: int = 1, end_ip: int = 254,
                    progress_callback: Optional[Callable[[int, int, NetworkDevice], None]] = None,
                    max_workers: int = 40) -> List[NetworkDevice]:
        """
        Scans a subnet (e.g. '192.168.1') from start_ip to end_ip concurrently.
        Emits progress_callback(current_count, total_count, discovered_device) if provided.
        """
        self._cancel_scan = False
        self.refresh_arp_cache()

        ips_to_scan = [f"{subnet}.{i}" for i in range(start_ip, end_ip + 1)]
        total = len(ips_to_scan)
        completed = 0
        devices: List[NetworkDevice] = []

        with ThreadPoolExecutor(max_workers=max_workers) as executor:
            future_to_ip = {executor.submit(self.probe_device, ip): ip for ip in ips_to_scan}

            for future in as_completed(future_to_ip):
                if self._cancel_scan:
                    break

                completed += 1
                try:
                    dev = future.result()
                    # Only report devices that are either Online or have known MAC
                    if dev.status != "Offline" or dev.mac != "Unknown":
                        devices.append(dev)
                    if progress_callback:
                        progress_callback(completed, total, dev)
                except Exception:
                    pass

        # Sort results by IP address numerically
        def ip_sort_key(d: NetworkDevice):
            try:
                return [int(x) for x in d.ip.split(".")]
            except Exception:
                return [0, 0, 0, 0]

        devices.sort(key=ip_sort_key)
        return devices

    def cancel_scan(self):
        """Signals active scan to abort."""
        self._cancel_scan = True

    # -------------------------------------------------------------
    # AGENTLESS REMOTE CONTROL COMMANDS
    # -------------------------------------------------------------

    @staticmethod
    def remote_power_off(ip: str, timeout_sec: int = 30,
                         message: str = "System shutdown initiated by Office Network Administrator.",
                         reboot: bool = False,
                         force: bool = True) -> Tuple[bool, str]:
        """
        Executes agentless remote shutdown/restart on Windows targets using standard RPC / shutdown.exe.
        Requires target to allow File & Printer Sharing (port 445) and user to have Admin privileges.
        """
        if os.name != 'nt':
            return False, "Perintah shutdown jarak jauh Windows hanya didukung pada host Windows."

        action_flag = "/r" if reboot else "/s"
        cmd = ["shutdown", "/m", f"\\\\{ip}", action_flag, "/t", str(timeout_sec)]
        if force:
            cmd.append("/f")
        if message:
            cmd.extend(["/c", message])

        try:
            startupinfo = subprocess.STARTUPINFO()
            startupinfo.dwFlags |= subprocess.STARTF_USESHOWWINDOW
            proc = subprocess.run(
                cmd,
                capture_output=True,
                text=True,
                timeout=10,
                startupinfo=startupinfo,
                check=False
            )
            if proc.returncode == 0:
                action_name = "Restart" if reboot else "Shutdown"
                return True, f"{action_name} berhasil dikirim ke {ip} (Timeout: {timeout_sec}s)."
            else:
                err_msg = proc.stderr.strip() or proc.stdout.strip()
                # Try fallback via PowerShell Stop-Computer / Restart-Computer
                ps_cmd = "Restart-Computer" if reboot else "Stop-Computer"
                ps_exec = ["powershell", "-NoProfile", "-Command", f"{ps_cmd} -ComputerName '{ip}' -Force"]
                ps_proc = subprocess.run(
                    ps_exec,
                    capture_output=True,
                    text=True,
                    timeout=10,
                    startupinfo=startupinfo,
                    check=False
                )
                if ps_proc.returncode == 0:
                    return True, f"Berhasil melalui PowerShell {ps_cmd} ke {ip}."

                return False, f"Gagal shutdown {ip}: {err_msg or ps_proc.stderr.strip()}"
        except Exception as e:
            return False, f"Exception saat shutdown {ip}: {str(e)}"

    @staticmethod
    def abort_remote_shutdown(ip: str) -> Tuple[bool, str]:
        """Aborts a pending shutdown on the target PC."""
        if os.name != 'nt':
            return False, "Hanya didukung di Windows."
        try:
            startupinfo = subprocess.STARTUPINFO()
            startupinfo.dwFlags |= subprocess.STARTF_USESHOWWINDOW
            proc = subprocess.run(
                ["shutdown", "/m", f"\\\\{ip}", "/a"],
                capture_output=True,
                text=True,
                timeout=6,
                startupinfo=startupinfo,
                check=False
            )
            if proc.returncode == 0:
                return True, f"Shutdown pada {ip} berhasil dibatalkan."
            return False, proc.stderr.strip() or "Gagal membatalkan shutdown."
        except Exception as e:
            return False, str(e)

    @staticmethod
    def remote_launch_app(ip: str, command: str) -> Tuple[bool, str]:
        """
        Executes an application or command on the remote machine agentlessly via WMI Win32_Process.
        Command example: 'notepad.exe', 'calc.exe', or a bat script.
        """
        if os.name != 'nt':
            return False, "Eksekusi jarak jauh WMI hanya didukung pada platform Windows."

        # Use PowerShell WMI execution to start process on remote machine
        # Invoke-WmiMethod -Class Win32_Process -Name Create -ArgumentList "<command>" -ComputerName "<ip>"
        ps_script = (
            f"$res = Invoke-WmiMethod -Class Win32_Process -Name Create "
            f"-ArgumentList '{command}' -ComputerName '{ip}' -ErrorAction Stop; "
            f"if ($res.ReturnValue -eq 0) {{ 'OK:' + $res.ProcessId }} else {{ 'ERR:' + $res.ReturnValue }}"
        )
        try:
            startupinfo = subprocess.STARTUPINFO()
            startupinfo.dwFlags |= subprocess.STARTF_USESHOWWINDOW
            proc = subprocess.run(
                ["powershell", "-NoProfile", "-Command", ps_script],
                capture_output=True,
                text=True,
                timeout=12,
                startupinfo=startupinfo,
                check=False
            )
            out = proc.stdout.strip()
            if "OK:" in out:
                pid = out.split("OK:")[1].strip()
                return True, f"Aplikasi berhasil dijalankan di {ip} (Process ID: {pid})."
            elif "ERR:" in out:
                code = out.split("ERR:")[1].strip()
                return False, f"WMI mengembalikan status error code {code}."
            else:
                err = proc.stderr.strip() or out
                return False, f"Gagal mengeksekusi di {ip}: {err}"
        except Exception as e:
            return False, f"Exception saat menjalankan aplikasi di {ip}: {str(e)}"

    @staticmethod
    def send_wake_on_lan(mac_address: str, broadcast_ip: str = "255.255.255.255", port: int = 9) -> Tuple[bool, str]:
        """
        Sends a standard Wake-on-LAN (WOL) magic packet to boot up an offline computer.
        Format: 6 bytes 0xFF followed by 16 repetitions of the 6-byte target MAC address.
        """
        try:
            # Clean MAC address
            clean_mac = re.sub(r'[^a-fA-F0-9]', '', mac_address)
            if len(clean_mac) != 12:
                return False, f"Format MAC Address tidak valid: '{mac_address}'"

            mac_bytes = bytes.fromhex(clean_mac)
            magic_packet = b'\xff' * 6 + mac_bytes * 16

            # Send broadcast UDP packet
            with socket.socket(socket.AF_INET, socket.SOCK_DGRAM) as sock:
                sock.setsockopt(socket.SOL_SOCKET, socket.SO_BROADCAST, 1)
                sock.sendto(magic_packet, (broadcast_ip, port))

            return True, f"Magic Packet (WOL) berhasil dikirim ke {mac_address} via {broadcast_ip}:{port}."
        except Exception as e:
            return False, f"Gagal mengirim WOL: {str(e)}"
