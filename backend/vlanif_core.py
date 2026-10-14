"""vlanif_core.py -- logika MURNI (tanpa I/O) untuk Add/Edit VLANIF, ZTE C320 ZXAN V2.1.0.
Delete SENGAJA belum ada. Fail-closed: apa pun yang tak dikenali -> preflight gagal, bukan ditebak.

Syntax (dari probe user): `interface vlan 30` (pakai spasi), `ip address <ip> <mask>`, `ip address dhcp`,
`no ip address`, `description TEXT` (maks 100), `no description` [CEK], `shutdown`/`no shutdown`,
`no interface vlan 30` (hanya dipakai internal untuk rollback CREATE).
"""
import hashlib
import ipaddress
import json
import re

from onu_vlan_core import Unsupported, parse_show_vlan_summary          # dipakai ulang
from vlanif import VlanifParseError, parse_ip_interface_brief_all

SHOW_CFG = "show running-config interface vlan {n}"      # [CEK] 'vlan {n}' (spasi) atau 'vlan{n}'? probe dulu
DESC_RE = re.compile(r"^[A-Za-z0-9 _.,:/@#()+=-]{1,100}$")           # tanpa '?' dan '!'
_HDR = re.compile(r"^\s*interface\s+vlan\s*(\d+)\s*$", re.I)           # terima 'vlan 2' maupun 'vlan2'
_PROMPT = re.compile(r"^\S+[#>]$")


def nd(s) -> str:
    return " ".join((s or "").split())


# ---------------------------------------------------------------- state interface
def empty_state():
    return {"exists": False, "mode": "none", "ip": None, "mask": None, "desc": "", "shutdown": False}


def parse_vlanif_config(text: str, vlan: int) -> dict:
    """Parse output `show running-config interface vlan N`. Baris tak dikenal -> Unsupported."""
    lines = (text or "").replace("\r", "").split("\n")
    start = None
    for k, l in enumerate(lines):
        m = _HDR.match(l)
        if m and int(m.group(1)) == vlan:
            start = k
            break
    st = empty_state()
    if start is None:
        return st
    st["exists"] = True
    for x in lines[start + 1:]:
        s = re.sub(r"\s+", " ", x.strip())
        if s == "!" or _PROMPT.match(s):
            break
        if not s:
            continue
        low = s.lower()
        m = re.fullmatch(r"ip address (\d+\.\d+\.\d+\.\d+) (\d+\.\d+\.\d+\.\d+)", low)
        if m:
            st.update(mode="static", ip=m.group(1), mask=m.group(2))
        elif low == "ip address dhcp":
            st.update(mode="dhcp", ip=None, mask=None)
        elif low.startswith("description "):
            st["desc"] = s[len("description "):]
        elif low == "shutdown":
            st["shutdown"] = True
        elif low == "no shutdown":
            st["shutdown"] = False
        else:
            raise Unsupported(f"baris tak dikenal di interface vlan {vlan}: {s[:60]!r}")
    return st


# ---------------------------------------------------------------- validasi input user
def validate_input(vlan, mode, ip=None, mask=None, desc="", allow_dhcp=False) -> dict:
    if isinstance(vlan, bool) or not isinstance(vlan, int) or not 1 <= vlan <= 4094:
        raise ValueError("VLAN ID harus 1-4094")
    if any(ord(ch) < 32 or ord(ch) == 127 for ch in (desc or "")):
        raise ValueError("Description tidak boleh berisi karakter kontrol/baris baru")
    desc = nd(desc)
    if desc and not DESC_RE.match(desc):
        raise ValueError("Description: maks 100 karakter; hanya huruf, angka, spasi, dan _ . , : / @ # ( ) + = -")
    if mode == "dhcp":
        if not allow_dhcp:
            raise ValueError("Mode DHCP dimatikan (VLANIF_ALLOW_DHCP=0): DHCP bisa memasang route/IP yang tak bisa dicek sebelumnya")
        return {"mode": "dhcp", "ip": None, "mask": None, "desc": desc}
    if mode != "static":
        raise ValueError("mode harus static atau dhcp")
    try:
        itf = ipaddress.IPv4Interface(f"{ip}/{mask}")                  # menolak IP salah & mask tak kontigu
    except (ValueError, TypeError):
        raise ValueError("IP/mask tidak valid (mask harus kontigu, mis. 255.255.255.0)")
    net = itf.network
    if not 8 <= net.prefixlen <= 30:
        raise ValueError("Panjang prefix harus 8-30")
    a = itf.ip
    if a in (net.network_address, net.broadcast_address):
        raise ValueError("IP tidak boleh alamat network/broadcast subnet")
    if a.is_loopback or a.is_multicast or a.is_unspecified or a.is_link_local or a.is_reserved:
        raise ValueError("IP loopback/multicast/link-local/reserved tidak diizinkan")
    return {"mode": "static", "ip": str(a), "mask": str(itf.netmask), "desc": desc}


# ---------------------------------------------------------------- builder
def _ip_lines(old_mode, new):
    out = []
    if old_mode != "none":
        out.append("no ip address")
    if new["mode"] == "static":
        out.append(f"ip address {new['ip']} {new['mask']}")
    elif new["mode"] == "dhcp":
        out.append("ip address dhcp")
    return out


def build_create_cmds(vlan: int, new: dict) -> list:
    cmds = ["configure terminal", f"interface vlan {int(vlan)}"] + _ip_lines("none", new)
    if new["desc"]:
        cmds.append(f"description {new['desc']}")
    return cmds + ["no shutdown", "exit", "exit"]


def build_edit_cmds(vlan: int, old: dict, new: dict) -> list:
    """Hanya mengirim yang BERUBAH (IP tidak disentuh bila tak berubah). new.mode boleh 'none' (dipakai rollback)."""
    ip_changed = (old["mode"], old["ip"], old["mask"]) != (new["mode"], new["ip"], new["mask"])
    desc_changed = nd(old["desc"]) != nd(new["desc"])
    if not ip_changed and not desc_changed:
        raise ValueError("Tidak ada perubahan")
    cmds = ["configure terminal", f"interface vlan {int(vlan)}"]
    if ip_changed:
        cmds += _ip_lines(old["mode"], new)
    if desc_changed:
        cmds.append(f"description {nd(new['desc'])}" if nd(new["desc"]) else "no description")
    return cmds + ["exit", "exit"]


def build_rollback_create_cmds(vlan: int) -> list:
    """HANYA untuk membatalkan interface yang dibuat job ini sendiri (state awal: tidak ada)."""
    return ["configure terminal", f"no interface vlan {int(vlan)}", "exit"]


def expected_state(action: str, old: dict, new: dict) -> dict:
    return {"exists": True, "mode": new["mode"], "ip": new["ip"], "mask": new["mask"], "desc": nd(new["desc"]),
            "shutdown": False if action == "create" else old["shutdown"]}


def verify_state(expected: dict, now: dict) -> list:
    probs = []
    if not now["exists"]:
        return ["interface tidak ditemukan setelah apply"]
    for k in ("mode", "ip", "mask", "shutdown"):
        if expected[k] != now[k]:
            probs.append(f"{k}: diharapkan {expected[k]!r}, terbaca {now[k]!r}")
    if nd(expected["desc"]) != nd(now["desc"]):
        probs.append("description tidak sesuai")
    return probs


# ---------------------------------------------------------------- preflight (murni)
def check_overlap(vlan: int, ip: str, mask: str, rows: list) -> list:
    net = ipaddress.IPv4Interface(f"{ip}/{mask}").network
    bad = []
    for r in rows:
        if r.get("vlan_id") == vlan or not r.get("ip_address") or not r.get("mask"):
            continue
        try:
            other = ipaddress.IPv4Interface(f"{r['ip_address']}/{r['mask']}").network
        except ValueError:
            continue
        if net.overlaps(other):
            bad.append(f"{r['interface']} {r['ip_address']}/{other.prefixlen}")
    return bad


def lockout_reasons(connect_host: str, vlan: int, action: str, old_ip: dict, new: dict, protected: set) -> list:
    """old_ip/new: {'ip','mask'} (boleh kosong). Alasan = BLOKIR."""
    reasons = []
    if vlan in protected:
        reasons.append(f"VLAN {vlan} masuk daftar terlindungi (VLANIF_PROTECTED_VLANS)")
    try:
        host = ipaddress.IPv4Address(connect_host)
    except (ValueError, TypeError):
        host = None
    if host is None:
        if action == "edit":
            reasons.append("Alamat koneksi app ke OLT bukan IPv4 literal; tidak bisa memastikan ini bukan interface manajemen")
        return reasons
    for label, st in (("sekarang", old_ip), ("baru", new)):
        if st and st.get("ip") and st.get("mask"):
            itf = ipaddress.IPv4Interface(f"{st['ip']}/{st['mask']}")
            if itf.ip == host:
                reasons.append(f"IP {label} {itf.ip} = IP yang dipakai app untuk terhubung ke OLT (interface manajemen)")
            elif host in itf.network:
                reasons.append(f"IP koneksi app ({host}) berada di subnet {label} {itf.network}")
    return reasons


def route_check(route_text: str, vlan: int, old_net) -> list:
    """Cek apakah ada route (mis. default route) yang bergantung pada interface ini.
    BELUM diimplementasi: butuh sampel `show ip route`. Sampai ada -> Unsupported (edit diblokir kecuali dimatikan)."""
    raise Unsupported("pemeriksaan route belum diimplementasi: butuh sampel output 'show ip route'")


def protection_for(connect_host: str, row: dict, protected: set):
    """Untuk daftar UI: (terlindungi?, alasan)."""
    r = lockout_reasons(connect_host, row["vlan_id"], "edit", {"ip": row.get("ip_address"), "mask": row.get("mask")}, None, protected)
    return (True, r[0]) if r else (False, "")


def build_plan(*, olt_id, action, vlan, new_raw, connect_host, brief_text, cfg_text, vsum_text, route_text,
               protected, allow_dhcp=False, require_route_check=True) -> dict:
    """Satu fungsi murni: semua data hasil BACA masuk, rencana + preflight keluar. Tidak ada I/O, tidak ada tulis."""
    checks = []

    def add(name, ok, detail=""):
        checks.append({"name": name, "ok": bool(ok), "detail": detail})

    out = {"ok": False, "checks": checks, "commands": [], "old": None, "expected": None, "plan_token": None}
    if action not in ("create", "edit"):
        add("Aksi didukung (create/edit)", False, f"aksi {action!r}; delete belum tersedia")
        return out
    try:
        new = validate_input(vlan, new_raw.get("mode"), new_raw.get("ip"), new_raw.get("mask"),
                             new_raw.get("description", ""), allow_dhcp)
        add("Input valid", True)
    except ValueError as e:
        add("Input valid", False, str(e))
        return out
    try:
        rows = parse_ip_interface_brief_all(brief_text)
        st = parse_vlanif_config(cfg_text, vlan)
    except (VlanifParseError, Unsupported) as e:
        add("OLT terbaca & format dikenali", False, str(e))
        return out
    add("OLT terbaca & format dikenali", True)
    row = next((r for r in rows if r["vlan_id"] == vlan), None)
    exists = st["exists"] or row is not None
    if exists and not st["exists"]:
        add("Detail config interface terbaca", False, "ada di 'show ip interface brief' tapi config tak terbaca; edit diblokir")
        return out

    if action == "create":
        add("VLANIF belum ada", not exists, f"vlan {vlan} sudah ada" if exists else "")
        try:
            vlans = parse_show_vlan_summary(vsum_text)
            add("VLAN sudah dibuat di OLT", vlan in vlans,
                f"VLAN ada: {','.join(map(str, sorted(vlans)))}. Buat dulu di tab Daftar VLAN" if vlan not in vlans else "")
        except Unsupported as e:
            add("VLAN sudah dibuat di OLT", False, str(e))
        old, old_ip = empty_state(), None
    else:
        add("VLANIF ada", exists)
        old = st
        old_ip = {"ip": (row or {}).get("ip_address") or st["ip"], "mask": (row or {}).get("mask") or st["mask"]}
        try:
            cmds_probe = build_edit_cmds(vlan, old, new)
            add("Ada perubahan", True)
        except ValueError:
            add("Ada perubahan", False, "input sama dengan config sekarang")
            return out
    if new["mode"] == "static":
        bad = check_overlap(vlan, new["ip"], new["mask"], rows)
        add("IP/subnet tidak tumpang tindih", not bad, "bentrok dengan: " + ", ".join(bad) if bad else "")
    lock = lockout_reasons(connect_host, vlan, action, old_ip, new if new["mode"] == "static" else None, protected)
    add("Bukan jalur manajemen OLT (anti-lockout)", not lock, "; ".join(lock))
    if action == "edit" and old["mode"] != "none" and require_route_check:
        try:
            deps = route_check(route_text, vlan, None)
            add("Tidak ada route yang bergantung pada interface ini", not deps, "; ".join(deps))
        except Unsupported as e:
            add("Tidak ada route yang bergantung pada interface ini", False, str(e))

    out["ok"] = all(c["ok"] for c in checks)
    if out["ok"]:
        out["commands"] = build_create_cmds(vlan, new) if action == "create" else cmds_probe
    out["old"] = old
    out["new"] = new
    out["expected"] = expected_state(action, old, new)
    out["plan_token"] = hashlib.sha256(json.dumps(
        {"o": olt_id, "a": action, "v": vlan, "old": old, "new": new}, sort_keys=True).encode()).hexdigest()[:32]
    return out
