"""Logika MURNI edit VLAN per ONU (ZTE C320). Tanpa I/O: bisa dites tanpa OLT/DB.

Model yang didukung (sesuai config ONU uji):
  interface gpon-onu_X:  service-port N vport V user-vlan A vlan A
  pon-onu-mng gpon-onu_X: service NAMA gemport G iphost H vlan A
Selain bentuk itu (mis. user-vlan != vlan, lebih dari 1 service-port) -> DITOLAK.
"""
import re
from dataclasses import dataclass


class VlanError(ValueError):
    pass


IFACE_RE = re.compile(r"gpon-onu_\d+/\d+/\d+:\d+")

# ---------------- Whitelist: satu-satunya bentuk perintah yang boleh dikirim ----------------
_CONFIG_WHITELIST = [re.compile(p) for p in (
    r"configure terminal",
    r"interface gpon-onu_\d+/\d+/\d+:\d+",
    r"pon-onu-mng gpon-onu_\d+/\d+/\d+:\d+",
    r"no service-port \d{1,5}",
    r"service-port \d{1,5} vport \d{1,3} user-vlan \d{1,4} vlan \d{1,4}",
    r"service [A-Za-z0-9_-]{1,32} gemport \d{1,3} iphost \d{1,2} vlan \d{1,4}",
    r"exit",
    r"end",
    r"write",
)]
_SHOW_WHITELIST = [re.compile(p) for p in (
    r"show running-config interface gpon-onu_\d+/\d+/\d+:\d+",
    r"show onu running config gpon-onu_\d+/\d+/\d+:\d+",
    r"show vlan summary",
)]
_VLAN_IN_CMD = re.compile(r"\b(?:user-vlan|vlan) (\d+)")


def _clean(cmd):
    # isprintable() False untuk \n \r \t -> mencegah penyisipan perintah kedua
    return isinstance(cmd, str) and cmd.isprintable() and cmd == cmd.strip()


def is_config_allowed(cmd):
    if not _clean(cmd) or not any(p.fullmatch(cmd) for p in _CONFIG_WHITELIST):
        return False
    return all(1 <= int(v) <= 4094 for v in _VLAN_IN_CMD.findall(cmd))


def is_show_allowed(cmd):
    return _clean(cmd) and any(p.fullmatch(cmd) for p in _SHOW_WHITELIST)


def assert_allowed(cmd):
    if not (is_config_allowed(cmd) or is_show_allowed(cmd)):
        raise VlanError(f"perintah tidak diizinkan: {cmd!r}")


# ---------------- Daftar VLAN ----------------
def parse_vlan_list(text):
    """'1-2,15,101' -> {1,2,15,101}. Format aneh -> VlanError."""
    if text is None:
        raise VlanError("daftar VLAN kosong")
    s = text.strip()
    if not s or s == "-":
        return set()
    out = set()
    for part in s.split(","):
        m = re.fullmatch(r"(\d{1,4})(?:-(\d{1,4}))?", part.strip())
        if not m:
            raise VlanError(f"format VLAN tidak valid: {part!r}")
        a = int(m.group(1))
        b = int(m.group(2) or a)
        if a < 1 or b > 4094 or a > b:
            raise VlanError(f"rentang VLAN tidak valid (1-4094): {part!r}")
        out.update(range(a, b + 1))
    return out


def format_vlan_list(vlans):
    v = sorted(vlans)
    parts, i = [], 0
    while i < len(v):
        j = i
        while j + 1 < len(v) and v[j + 1] == v[j] + 1:
            j += 1
        parts.append(str(v[i]) if i == j else f"{v[i]}-{v[j]}")
        i = j + 1
    return ",".join(parts)


_TOKEN = re.compile(r"\d{1,4}(?:-\d{1,4})?(?:,\d{1,4}(?:-\d{1,4})?)*")


def extract_vlan_list(raw):
    """Ambil daftar VLAN dari output 'show vlan summary' (token terpanjang yang valid).
    Gagal parse -> VlanError (fail-closed: perubahan DIBLOKIR, bukan ditebak)."""
    best = None
    for tok in _TOKEN.findall(raw or ""):
        try:
            s = parse_vlan_list(tok)
        except VlanError:
            continue
        if best is None or len(tok) > len(best[0]):
            best = (tok, s)
    if not best:
        raise VlanError("daftar VLAN tidak ditemukan di output 'show vlan summary'")
    return best[1]


# ---------------- Config sekarang ----------------
_SP_RE = re.compile(r"^\s*service-port\s+(\d+)\s+vport\s+(\d+)\s+user-vlan\s+(\d+)\s+vlan\s+(\d+)\s*$", re.M)
_SVC_RE = re.compile(r"^\s*service\s+(\S+)\s+gemport\s+(\d+)\s+iphost\s+(\d+)\s+vlan\s+(\d+)\s*$", re.M)


@dataclass
class Current:
    sp_id: int
    vport: int
    vlan: int
    svc_name: str
    gemport: int
    iphost: int


def parse_current(iface_cfg, mng_cfg):
    sps = _SP_RE.findall(iface_cfg or "")
    svcs = _SVC_RE.findall(mng_cfg or "")
    if len(sps) != 1:
        raise VlanError(f"ditemukan {len(sps)} baris service-port (harus tepat 1). Edit manual di OLT.")
    if len(svcs) != 1:
        raise VlanError(f"ditemukan {len(svcs)} baris 'service ... iphost ... vlan' (harus tepat 1). Edit manual di OLT.")
    sp_id, vport, uvlan, vlan = map(int, sps[0])
    name, gem, iph, svlan = svcs[0][0], int(svcs[0][1]), int(svcs[0][2]), int(svcs[0][3])
    if not (uvlan == vlan == svlan):
        raise VlanError(f"VLAN tidak seragam (user-vlan {uvlan}, vlan {vlan}, service {svlan}). Edit manual di OLT.")
    return Current(sp_id, vport, vlan, name, gem, iph)


# ---------------- Validasi target + rencana ----------------
def validate_target(new_vlan, cur, olt_vlans, uplink_sets):
    errs = []
    if isinstance(new_vlan, bool) or not isinstance(new_vlan, int) or not (2 <= new_vlan <= 4094):
        return ["VLAN baru harus angka 2-4094"]
    if new_vlan == cur.vlan:
        errs.append("VLAN baru sama dengan VLAN sekarang")
    if new_vlan not in olt_vlans:
        errs.append(f"VLAN {new_vlan} belum dibuat di OLT (yang ada: {format_vlan_list(olt_vlans)})")
    if not any(new_vlan in s for s in uplink_sets):
        errs.append(f"VLAN {new_vlan} tidak ada di allowed list uplink trunk mana pun: pelanggan akan offline")
    return errs


@dataclass
class Plan:
    iface: str
    old_vlan: int
    new_vlan: int
    apply: list
    rollback: list
    verify: list


def _block(iface, c, vlan):
    return [
        "configure terminal",
        f"interface {iface}",
        f"no service-port {c.sp_id}",
        f"service-port {c.sp_id} vport {c.vport} user-vlan {vlan} vlan {vlan}",
        "exit",
        f"pon-onu-mng {iface}",
        f"service {c.svc_name} gemport {c.gemport} iphost {c.iphost} vlan {vlan}",
        "exit",
        "end",
    ]


def build_plan(iface, cur, new_vlan):
    if not IFACE_RE.fullmatch(iface or ""):
        raise VlanError(f"nama interface ONU tidak valid: {iface!r}")
    plan = Plan(
        iface=iface, old_vlan=cur.vlan, new_vlan=new_vlan,
        apply=_block(iface, cur, new_vlan),
        rollback=_block(iface, cur, cur.vlan),
        verify=[f"show running-config interface {iface}", f"show onu running config {iface}"],
    )
    for c in plan.apply + plan.rollback + plan.verify:
        assert_allowed(c)          # sabuk pengaman: bug builder tidak boleh lolos ke OLT
    return plan


def check_after(iface_cfg, mng_cfg, new_vlan):
    """Verifikasi setelah apply: kedua baris harus sudah VLAN baru."""
    try:
        cur = parse_current(iface_cfg, mng_cfg)
    except VlanError as e:
        return False, str(e)
    if cur.vlan != new_vlan:
        return False, f"VLAN di OLT masih {cur.vlan}, bukan {new_vlan}"
    return True, "ok"


# ==================== Compatibility Shim (untuk vlanif_core.py) ====================
# Alias: nama-nama yang dipakai vlanif_core.py, mapping ke fungsi existing.
# JANGAN hapus. Dipakai oleh vlanif_core.py untuk reuse parser.

Unsupported = VlanError  # alias exception


def parse_show_vlan_summary(text):
    """Alias dari extract_vlan_list. Return: set[int] VLAN IDs.
    Raise Unsupported (VlanError) kalau gagal parse."""
    return extract_vlan_list(text)
