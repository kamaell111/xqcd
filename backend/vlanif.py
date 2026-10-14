"""vlanif.py -- parser MURNI untuk `show ip interface brief` (ZTE C320 ZXAN V2.1.0). Read-only, tanpa I/O."""
import ipaddress
import re

_HEADER = re.compile(r"^\s*Interface\s+IP-Address\b", re.I)
_ROW = re.compile(r"^(vlan\d{1,4})\s+(\S+)\s+(\S+)\s+(\S+)\s+(\S+)\s+(\S+)(?:\s+(.*?))?\s*$", re.I)
_ERR = re.compile(r"%\s*Error|Invalid|Incomplete|Unrecognized|Ambiguous", re.I)
_MORE = re.compile(r"[ \t]*--\s*More\s*--[ \t\x08]*", re.I)


class VlanifParseError(ValueError):
    pass


def _clean(text: str) -> str:
    t = (text or "").replace("\r", "")
    t = _MORE.sub("\n", t)
    return t.replace("\x08", "")


def _ipv4(s: str):
    try:
        return str(ipaddress.IPv4Address(s))
    except ValueError:
        return None


def _prefix(mask: str):
    try:
        return ipaddress.IPv4Network(f"0.0.0.0/{mask}").prefixlen     # menolak mask tak kontigu
    except ValueError:
        return None


_ROW_ANY = re.compile(r"^(\S+)\s+(\S+)\s+(\S+)\s+(\S+)\s+(\S+)\s+(\S+)(?:\s+(.*?))?\s*$")
_VLAN_NAME = re.compile(r"^vlan(\d{1,4})$", re.I)


def _rows(output: str) -> list:
    """Semua baris interface dari 'show ip interface brief'. Fail-closed bila header tak ada / OLT menolak."""
    t = _clean(output)
    lines = t.split("\n")
    if not any(_HEADER.match(l) for l in lines):
        if _ERR.search(t):
            raise VlanifParseError("OLT menolak command 'show ip interface brief'")
        raise VlanifParseError("header 'Interface IP-Address ...' tidak ditemukan")
    rows = []
    for line in lines:
        m = _ROW_ANY.match(line.strip())
        if not m or m.group(1).lower() == "interface":
            continue
        name, ip, mask, admin, phy, prot, desc = m.groups()
        vm = _VLAN_NAME.match(name)
        if not vm and not (_ipv4(ip) or ip.lower() in ("unassigned", "--", "none")):
            continue                                            # bukan baris data (baris vlan selalu dipertahankan)
        vid = int(vm.group(1)) if vm else None
        if vm and not 1 <= vid <= 4094:
            continue
        admin, phy, prot = admin.lower(), phy.lower(), prot.lower()
        status = "admin_down" if admin != "up" else ("up" if phy == "up" and prot == "up" else "down")
        desc = (desc or "").strip()
        rows.append({
            "interface": name.lower(), "vlan_id": vid,
            "ip_address": _ipv4(ip), "mask": _ipv4(mask), "prefix": _prefix(mask) if _ipv4(mask) else None,
            "admin_status": admin, "phy_status": phy, "protocol_status": prot, "status": status,
            "description": "" if desc.lower() == "none" else desc,       # 'none' = kolom kosong di OLT ini
        })
    return rows


def parse_ip_interface_brief(output: str) -> list:
    """Hanya interface bernama vlan<ID>, unik, urut VLAN ID.
    Contoh baris:  vlan2         10.1.1.200      255.255.255.0   up    up   up   none"""
    seen, items = set(), []
    for r in _rows(output):
        if r["vlan_id"] is None or r["vlan_id"] in seen:
            continue
        seen.add(r["vlan_id"])
        items.append(r)
    return sorted(items, key=lambda x: x["vlan_id"])


def parse_ip_interface_brief_all(output: str) -> list:
    """SEMUA interface (vlan, mng, gei, ...) -- dipakai preflight untuk cek IP/subnet tumpang tindih."""
    return _rows(output)
