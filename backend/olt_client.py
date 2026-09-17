"""OLT client untuk ZTE ZXAN — support Telnet & SSH via Netmiko."""
from netmiko import ConnectHandler
from typing import List, Dict
import re
import time as _time
import threading
import atexit

# ⚡ Connection cache — pakai 1 koneksi telnet untuk banyak request
_CONN_CACHE = {}          # {key: (conn, last_used_time)}
_CONN_LOCK = threading.Lock()
_CONN_TTL = 240           # 4 menit


class _CachedConn:
    """Wrapper: disconnect() jadi no-op biar koneksi tetap hidup untuk request berikutnya."""
    def __init__(self, conn, key):
        self._conn = conn
        self._key = key
    def __getattr__(self, name):
        return getattr(self._conn, name)
    def disconnect(self):
        # Update timestamp saja, JANGAN tutup koneksi
        with _CONN_LOCK:
            if self._key in _CONN_CACHE:
                _CONN_CACHE[self._key] = (self._conn, _time.time())


def _cleanup_all():
    with _CONN_LOCK:
        for key, (conn, _) in list(_CONN_CACHE.items()):
            try:
                conn.disconnect()
            except Exception:
                pass
        _CONN_CACHE.clear()

atexit.register(_cleanup_all)


def _get_or_create(host, port, username, password, enable_password, protocol, timeout,
                   device_type):
    key = f"{host}:{port}:{username}"

    # Try cache
    with _CONN_LOCK:
        entry = _CONN_CACHE.get(key)
        if entry:
            conn, last = entry
            if _time.time() - last < _CONN_TTL:
                try:
                    if conn.is_alive():
                        _CONN_CACHE[key] = (conn, _time.time())
                        return _CachedConn(conn, key)
                except Exception:
                    pass
            # Stale
            try:
                conn.disconnect()
            except Exception:
                pass
            _CONN_CACHE.pop(key, None)

    # Create new
    params = {
        "device_type": device_type,
        "host": host,
        "username": username,
        "password": password,
        "port": port,
        "timeout": timeout,
        "conn_timeout": timeout,
        "session_timeout": 120,
        "fast_cli": True,
        "global_delay_factor": 1.0,
    }
    if enable_password:
        params["secret"] = enable_password

    conn = ConnectHandler(**params)

    if enable_password:
        try:
            conn.enable()
        except Exception:
            pass

    try:
        conn.send_command_timing("terminal length 0", read_timeout=10, last_read=0.3)
    except Exception:
        pass

    with _CONN_LOCK:
        old = _CONN_CACHE.get(key)
        if old:
            try:
                old[0].disconnect()
            except Exception:
                pass
        _CONN_CACHE[key] = (conn, _time.time())

    return _CachedConn(conn, key)


# ⚡ Tuning: last_read=0.3 mempercepat 4x (dari 2.2s → 0.5s/command)
# Gunakan LAST_READ_SHORT untuk output pendek, LAST_READ_LONG untuk output panjang
LAST_READ_SHORT = 0.3
LAST_READ_LONG = 1.0


class OLTClient:
    """
    Client untuk OLT ZTE ZXAN (C320/C300/C620, dll).

    Contoh Telnet:
        OLTClient("10.0.0.1", "admin", "admin",
                  enable_password="enablepass", port=23, protocol="telnet")
    Contoh SSH:
        OLTClient("10.0.0.2", "admin", "admin",
                  enable_password="...", port=22, protocol="ssh")
    """

    def __init__(self, host, username, password,
                 enable_password=None, port=23, protocol="telnet",
                 timeout=30):
        self.host = host
        self.username = username
        self.password = password
        self.enable_password = enable_password
        self.port = port
        self.protocol = (protocol or "telnet").lower()
        self.timeout = timeout

    # ---------- internal ----------
    def _device_type(self) -> str:
        return "zte_zxros" if self.protocol == "ssh" else "zte_zxros_telnet"

    def _connect(self):
        """Ambil koneksi dari cache atau buat baru."""
        return _get_or_create(
            self.host, self.port, self.username, self.password,
            self.enable_password, self.protocol, self.timeout,
            self._device_type(),
        )

    # ---------- public API ----------
    def test_connection(self) -> Dict:
        """Tes koneksi + ambil 'show version'. Return dict."""
        try:
            conn = self._connect()
            try:
                out = conn.send_command_timing("show system-group", read_timeout=20, last_read=LAST_READ_LONG)
                return {"ok": True, "output": out}
            finally:
                conn.disconnect()
        except Exception as e:
            return {"ok": False, "error": str(e)}

    def run_commands(self, commands: List[str]) -> str:
        """Kirim command mode non-config (show/display)."""
        conn = self._connect()
        try:
            return conn.send_command_timing(
                "\n".join(commands), read_timeout=60, last_read=LAST_READ_LONG
            )
        finally:
            conn.disconnect()

    def run_config(self, commands: List[str], save: bool = True) -> str:
        """Kirim command config satu per satu pakai send_command_timing.
        Lebih reliable untuk ZTE ZXAN via Telnet (prompt detection sering timeout)."""
        conn = self._connect()
        try:
            output_lines = []
            for raw_cmd in commands:
                cmd = raw_cmd.strip()   # <-- strip leading/trailing spaces
                if not cmd:
                    continue
                try:
                    out = conn.send_command_timing(
                        cmd,
                        read_timeout=30,
                        last_read=LAST_READ_SHORT,
                        strip_prompt=False,
                        strip_command=False,
                    )
                    output_lines.append(f">>> {cmd}")
                    output_lines.append(out)
                    # Kalau ada error, log
                    if any(k in out for k in ("Error", "Invalid", "Incomplete", "unknown ME", "Ambiguous")):
                        output_lines.append(f"    [WARN] command mungkin gagal")
                except Exception as e:
                    output_lines.append(f">>> {cmd}")
                    output_lines.append(f"    [ERROR] {e}")

            if save:
                try:
                    out = conn.send_command_timing(
                        "write", read_timeout=60, last_read=LAST_READ_LONG,
                        strip_prompt=False, strip_command=False,
                    )
                    output_lines.append(">>> write")
                    output_lines.append(out)
                except Exception as e:
                    output_lines.append(f"[SAVE ERROR] {e}")

            return "\n".join(output_lines)
        finally:
            conn.disconnect()

    def get_running_config(self) -> str:
        conn = self._connect()
        try:
            return conn.send_command(
                "show running-config", read_timeout=120
            )
        finally:
            conn.disconnect()

    # ---------- validasi ----------
    _BLACKLIST = re.compile(
        r"^\s*(reload|reboot|erase|format|delete|"
        r"factory-reset|restore|clear\s+startup)",
        re.IGNORECASE,
    )

    _ALLOWED = re.compile(
        r"^\s*("
        r"configure|config|interface|exit|end|no\s|undo\s|"
        r"onu|gpon|pon|epon|gpon-olt|gpon-onu|pon-onu-mng|"
        r"tcont|gemport|service-port|vport|sn-bind|name|"
        r"profile|onu-type|add-card|add-rack|add-shelf|add-subcard|"
        r"uncfg-onu-display-info|onu-pnp|"
        r"pppoe|wan|iphost|firewall|security-mgmt|"
        r"ip\s|iphost|dhcp|"
        r"switchport|hybrid-attribute|negotiation|speed|duplex|"
        r"flowcontrol|linktrap|shutdown|port-protect|uplink-isolate|"
        r"description|mtu|tag-mode|phy-attribute|"
        r"vlan|vlan-reserve|"
        r"hostname|username|snmp-server|logging|clock|"
        r"service\s|line\s|banner|message-of-day|"
        r"config-filename|set-pnp|operator-mode|"
        r"ip\s+route|static-route|"
        r"write|save"
        r")\b",
        re.IGNORECASE,
    )

    def validate_commands(self, commands: List[str]) -> Dict:
        errors = []
        for raw in commands:
            c = raw.strip()
            if not c or c.startswith("!") or c.startswith("#"):
                continue
            if self._BLACKLIST.match(c):
                errors.append(f"Command dilarang (berbahaya): {c}")
                continue
            if not self._ALLOWED.match(c):
                errors.append(f"Command tidak dikenali: {c}")
        return {"valid": len(errors) == 0, "errors": errors}


# Backward-compat
ZXANClient = OLTClient
