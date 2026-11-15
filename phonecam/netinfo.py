# SPDX-License-Identifier: GPL-3.0-or-later
"""Find this computer's Wi-Fi / LAN addresses on macOS, Windows and Linux (no internet needed).

Real Wi-Fi / Ethernet adapters come first. VPN tunnels, virtual machines, Docker and similar adapters go last,
because a phone on your Wi-Fi can't reach those (a VPN often owns the default route, so "the default route's
address" alone is the wrong answer).
"""
import ipaddress
import platform
import re
import socket
import subprocess

_VIRTUAL = ("utun", "tun", "tap", "ppp", "ipsec", "bridge", "vmnet", "vboxnet", "docker", "veth", "virbr", "br-", "tailscale",
            "zt", "wg", "awdl", "llw", "gif", "stf", "anpi", "lo", "vethernet", "virtualbox", "vmware", "hyper-v", "wsl",
            "loopback", "bluetooth", "zerotier", "vpn", "nordlynx", "proton", "mullvad")
_REAL = ("en", "eth", "wl", "wi-fi", "wifi", "wireless", "ethernet")


def _private(ip):
    try:
        a = ipaddress.ip_address(ip)
    except ValueError:
        return False
    return a.version == 4 and a.is_private and not a.is_loopback and not a.is_link_local


def _cmd(args):
    try:
        kw = {}
        if platform.system() == "Windows":
            kw["creationflags"] = 0x08000000  # CREATE_NO_WINDOW
        return subprocess.run(args, capture_output=True, text=True, timeout=4, **kw).stdout
    except Exception:
        return ""


def _interfaces():
    """[(ip, adapter name)] as reported by the system."""
    out = []
    if platform.system() == "Windows":
        name = ""
        for line in _cmd(["ipconfig"]).splitlines():
            if line and not line[0].isspace() and line.rstrip().endswith(":"):
                name = line.strip().rstrip(":")
            elif "IPv4" in line and ":" in line:
                out.append((re.sub(r"\(.*\)", "", line.split(":")[-1]).strip(), name))
        return out
    text = _cmd(["/sbin/ifconfig"]) or _cmd(["ifconfig"])
    if text:
        name = ""
        for line in text.splitlines():
            if line and not line[0].isspace():
                name = line.split(":")[0].split()[0]
            else:
                t = line.strip().split()
                if t and t[0] == "inet" and len(t) > 1:
                    out.append((t[1].replace("addr:", ""), name))
        return out
    for line in _cmd(["ip", "-4", "-o", "addr"]).splitlines():  # Linux without ifconfig
        t = line.split()
        if len(t) > 3 and t[2] == "inet":
            out.append((t[3].split("/")[0], t[1]))
    return out


def _route_ip():
    try:
        s = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        s.connect(("8.8.8.8", 1))  # sends nothing; only asks the OS which address it would use
        ip = s.getsockname()[0]
        s.close()
        return ip
    except OSError:
        return ""


def candidates():
    """[(ip, label)] best first."""
    route = _route_ip()
    seen, rows = set(), []
    for ip, name in _interfaces():
        if _private(ip) and ip not in seen:
            seen.add(ip)
            low = name.lower()
            virtual = low.startswith(_VIRTUAL) or any(v in low for v in _VIRTUAL if len(v) > 4)
            real = low.startswith(_REAL) or any(r in low for r in _REAL if len(r) > 3)
            score = 2 if virtual else (0 if real else 1)
            if ip == route and not virtual:
                score -= 0.5
            rows.append((score, len(rows), ip, name or "network"))
    if route and _private(route) and route not in seen:
        rows.append((1.5, len(rows), route, "default route"))
    try:
        for info in socket.getaddrinfo(socket.gethostname(), None, socket.AF_INET):
            ip = info[4][0]
            if _private(ip) and ip not in seen and ip != route:
                seen.add(ip)
                rows.append((1.8, len(rows), ip, "host name"))
    except OSError:
        pass
    rows.sort()
    return [(ip, name) for _s, _i, ip, name in rows]


def local_ips():
    return [ip for ip, _ in candidates()]


def main_ip():
    ips = local_ips()
    return ips[0] if ips else "127.0.0.1"


def host_names():
    names = ["localhost"]
    try:
        h = socket.gethostname().split(".")[0]
        if h:
            names.append(h + ".local")
    except OSError:
        pass
    if platform.system() == "Darwin":
        local = _cmd(["/usr/sbin/scutil", "--get", "LocalHostName"]).strip()
        if local and local + ".local" not in names:
            names.append(local + ".local")
    return names
