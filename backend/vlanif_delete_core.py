"""Logika murni DELETE VLANIF (ZTE C320). Tanpa I/O: bisa dites tanpa OLT/DB.

Preflight: blokir HANYA 3 kondisi
  1. IP VLANIF == olt.ip_address
  2. VLANIF memegang default route (atau default route TIDAK bisa dipastikan -> ditolak)
  3. VLAN ada di env VLANIF_PROTECTED_VLANS (mis. "2,10")
"""
import ipaddress
import re

# Tambahkan juga ke _ALLOWED milik olt_client/vlanif_flow:  no interface vlan \d{1,4}
_ALLOWED = [re.compile(p) for p in (
    r"configure terminal",
    r"no interface vlan \d{1,4}",
    r"exit",
)]


_READ_ALLOWED = ("show ip interface brief", "show ip route")


class DeleteBlocked(ValueError):
    pass


def _norm_ip(s):
    return (s or "").strip().split("/")[0].strip()


def _same_ip(a, b):
    a, b = _norm_ip(a), _norm_ip(b)
    if not a or not b:
        return False
    try:
        return ipaddress.ip_address(a) == ipaddress.ip_address(b)
    except ValueError:
        return a == b


def protected_vlans(env_value):
    """'2,10' -> {2,10}. Isi env rusak -> ValueError (fail-closed, jangan diabaikan diam-diam)."""
    out = set()
    for tok in (env_value or "").split(","):
        tok = tok.strip()
        if not tok:
            continue
        if not tok.isdigit() or not (1 <= int(tok) <= 4094):
            raise ValueError(f"VLANIF_PROTECTED_VLANS tidak valid: {tok!r}")
        out.add(int(tok))
    return out


def preflight(vlan, vlanif, olt_ip, default_route_vlans, protected):
    """Return daftar alasan BLOK (kosong = boleh dihapus).
    vlanif: {"vlan": int, "ip": str|None} atau None kalau tidak ada.
    default_route_vlans: set VLAN yang memegang default route; None = gagal baca."""
    if isinstance(vlan, bool) or not isinstance(vlan, int) or not (1 <= vlan <= 4094):
        return ["VLAN harus angka 1-4094"]
    if vlanif is None:
        return [f"VLANIF vlan{vlan} tidak ditemukan"]
    why = []
    if _same_ip(vlanif.get("ip"), olt_ip):
        why.append(f"IP VLANIF ({_norm_ip(vlanif.get('ip'))}) sama dengan IP yang dipakai aplikasi: jalur koneksi")
    if default_route_vlans is None:
        why.append("default route tidak bisa dipastikan (gagal baca), ditolak demi aman")
    elif vlan in default_route_vlans:
        why.append(f"vlan{vlan} memegang default route 0.0.0.0: bisa memutus routing")
    if vlan in protected:
        why.append(f"vlan{vlan} ada di VLANIF_PROTECTED_VLANS")
    return why


def expected_confirm(vlan, vlanif):
    """IP lengkap. VLANIF tanpa IP (mis. DHCP belum dapat lease) -> 'vlan<N>'."""
    ip = _norm_ip((vlanif or {}).get("ip"))
    return ip if ip and ip != "-" else f"vlan{vlan}"


def confirm_ok(typed, vlan, vlanif):
    want = expected_confirm(vlan, vlanif)
    got = (typed or "").strip()
    if want.startswith("vlan"):
        return got.lower() == want
    return _same_ip(got, want) and "/" not in got


def build_delete(vlan):
    cmds = ["configure terminal", f"no interface vlan {vlan}", "exit"]
    for c in cmds:
        assert_allowed(c)
    return cmds


def assert_allowed(cmd):
    if not (isinstance(cmd, str) and cmd.isprintable() and cmd == cmd.strip()
            and any(p.fullmatch(cmd) for p in _ALLOWED)):
        raise DeleteBlocked(f"perintah tidak diizinkan: {cmd!r}")
    m = re.fullmatch(r"no interface vlan (\d+)", cmd)
    if m and not (1 <= int(m.group(1)) <= 4094):
        raise DeleteBlocked(f"VLAN di luar 1-4094: {cmd!r}")


# ---------------- Validator baca & gabungan ----------------
def assert_read_allowed(cmd):
    """Hanya 2 perintah baca yang boleh (dipakai read_strict)."""
    if cmd not in _READ_ALLOWED:
        raise DeleteBlocked(f"perintah baca tidak diizinkan: {cmd!r}")


def assert_any_allowed(cmd):
    """Perintah hapus ATAU baca (dipakai apply_strict: commands + verify_cmds)."""
    if cmd in _READ_ALLOWED:
        return
    assert_allowed(cmd)


# ---------------- Parser 'show ip route' ----------------
_IPV4 = re.compile(r"\d{1,3}(?:\.\d{1,3}){3}")
_HEADER = re.compile(r"\bDest\b.*\bInterface\b", re.I)


def parse_default_route_vlans(text):
    """Set nomor VLAN yang memegang default route 0.0.0.0/0 (Interface 'vlan<N>').
    Default route lewat 'mng1' dst. tidak dihitung (bukan VLANIF).
    FAIL-CLOSED: format tidak dikenali / jumlah baris tidak cocok dengan 'Total number of routes' -> ValueError."""
    if not text or not _HEADER.search(text):
        raise ValueError("format 'show ip route' tidak dikenali")
    total = re.search(r"Total number of routes:\s*(\d+)", text, re.I)
    vlans, count, in_table = set(), 0, False
    for line in text.splitlines():
        if _HEADER.search(line):
            in_table = True
            continue
        if not in_table:
            continue
        parts = line.split()
        if not parts or not _IPV4.fullmatch(parts[0]):
            continue
        if len(parts) < 4 or not _IPV4.fullmatch(parts[1]):
            raise ValueError(f"baris route tidak dikenali: {line!r}")
        count += 1
        if parts[0] == "0.0.0.0" and parts[1] == "0.0.0.0":
            m = re.fullmatch(r"vlan(\d+)", parts[3], re.I)
            if m:
                vlans.add(int(m.group(1)))
    if total and int(total.group(1)) != count:
        raise ValueError(f"jumlah route tidak cocok (header {total.group(1)}, terbaca {count}): output terpotong?")
    return vlans


# ---------------- Pilih baris VLANIF dari hasil parse 'show ip interface brief' ----------------
_NO_IP = {"", "-", "unassigned", "0.0.0.0", "none", "n/a"}


def pick_vlanif(rows, vlan):
    """rows: list[dict] dari parse_ip_interface_brief_all. Return {"vlan": N, "ip": str|None} atau None."""
    for r in rows or []:
        if str(r.get("vlan_id")) == str(vlan):
            ip = _norm_ip(str(r.get("ip_address") or ""))
            return {"vlan": vlan, "ip": None if ip.lower() in _NO_IP else ip}
    return None
