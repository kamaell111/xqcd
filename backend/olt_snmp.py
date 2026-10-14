"""SNMP client untuk OLT ZTE C320 — read-only polling.

Data yang diambil via SNMP (terverifikasi di lapangan):
- Nama ONU, tipe, status, serial number
- Optical RX/TX per ONU
- Distance per ONU
- Uptime OLT

CPU/Memory OLT TIDAK tersedia di SNMP firmware V2.1.0 — tetap via Telnet.
"""
from __future__ import annotations
import asyncio
from typing import Optional
from dataclasses import dataclass
from datetime import datetime

try:
    from pysnmp.hlapi.v3arch.asyncio import (
        SnmpEngine, CommunityData, UdpTransportTarget, ContextData,
        ObjectType, ObjectIdentity, get_cmd, bulk_cmd, walk_cmd,
    )
    _HAS_PYSNMP = True
except ImportError:
    _HAS_PYSNMP = False
    print("[SNMP] pysnmp tidak terinstall — SNMP disabled")


# =================== OID ZTE C320 (terverifikasi) ===================
# Base prefix ZTE
ZTE_GPON = "1.3.6.1.4.1.3902.1012"

# Standar
OID_IF_NAME = "1.3.6.1.2.1.31.1.1.1.1"          # ifName
OID_IF_HC_IN = "1.3.6.1.2.1.31.1.1.1.6"         # ifHCInOctets (Counter64)
OID_IF_HC_OUT = "1.3.6.1.2.1.31.1.1.1.10"       # ifHCOutOctets (Counter64)
OID_IF_ADMIN_STATUS = "1.3.6.1.2.1.2.2.1.7"     # ifAdminStatus (1=up, 2=down)
OID_IF_OPER_STATUS  = "1.3.6.1.2.1.2.2.1.8"     # ifOperStatus  (1=up, 2=down)
OID_SYS_DESCR = "1.3.6.1.2.1.1.1.0"
OID_SYS_UPTIME = "1.3.6.1.2.1.1.3.0"

# ZTE GPON
OID_ONU_TYPE       = f"{ZTE_GPON}.3.28.1.1.1"    # .<pon_idx>.<onu_id> → "F609"
OID_ONU_NAME       = f"{ZTE_GPON}.3.28.1.1.2"    # → "peo sidorejo"
OID_ONU_DESC       = f"{ZTE_GPON}.3.28.1.1.3"    # → "ONU-1:1"
OID_ONU_SN         = f"{ZTE_GPON}.3.28.1.1.5"    # Hex-STRING → decode
OID_ONU_STATUS     = f"{ZTE_GPON}.3.28.2.1.3"    # 6=online, 0=offline
OID_ONU_DISTANCE   = f"{ZTE_GPON}.3.11.4.1.2"    # meter
OID_ONU_RX         = f"{ZTE_GPON}.3.50.12.1.1.10"  # raw × 0.002 - 30
OID_ONU_TX         = f"{ZTE_GPON}.3.50.12.1.1.14"
OID_PON_NAME       = f"{ZTE_GPON}.3.13.1.1.1"    # "OLT-1".."OLT-16"


# --- OID Traffic ONU (Fase 3) ----------------------------------------------
# .2/.3 VERIFIED via snmpwalk full-subtree: rasio RX:TX 21:1 dan 251:1 pada
# 2 ONU aktif (gpon_1/1/1) -- konsisten pola downstream:upstream.
ZTE_GPON_TRAFFIC = "1.3.6.1.4.1.3902.1015.1010.5.56.1.1"
OID_ONU_TRAFFIC_IN  = f"{ZTE_GPON_TRAFFIC}.2"   # Counter64, RX (OLT->ONU)
OID_ONU_TRAFFIC_OUT = f"{ZTE_GPON_TRAFFIC}.3"   # Counter64, TX (ONU->OLT)

# Base index HANYA terverifikasi untuk PON 1/1/1 (OLT ini: 2 ONU aktif).
# JANGAN ekstrapolasi ke PON lain -- fallback Telnet.
_ONU_TRAFFIC_BASE_IDX = {"gpon_1/1/1": 2416967936}
_ONU_TRAFFIC_STRIDE = 256
# Sanity bound: onu_id > ini kemungkinan collision dengan PON lain.
_ONU_TRAFFIC_MAX_PER_PON = 128

# --- OID Status Detail ONU (Fase 1b) ----------------------------------------
# Subtree verified live via snmpwalk + cross-check CLI (timestamp match 4x).
# HANYA value status .4=Online yang confirmed via observasi nyata.
ZTE_GPON_STATUS_DETAIL  = "1.3.6.1.4.1.3902.1082.500.10.2.3.8.1"
OID_ONU_STATUS_GRANULAR = f"{ZTE_GPON_STATUS_DETAIL}.4"
OID_ONU_LAST_ONLINE     = f"{ZTE_GPON_STATUS_DETAIL}.5"
OID_ONU_LAST_OFFLINE    = f"{ZTE_GPON_STATUS_DETAIL}.6"
OID_ONU_OFFLINE_REASON  = f"{ZTE_GPON_STATUS_DETAIL}.7"
OID_ONU_IP_ADDRESS      = "1.3.6.1.4.1.3902.1012.3.50.16.1.1.10"

# Base index: slot=1,pon=1 -> 285278465 (BEDA number space dari traffic)
_ONU_STATUS_BASE_IDX = {"gpon_1/1/1": 285278465}

# HANYA .4=Online VERIFIED. Sisanya HIPOTESIS.
_ONU_STATUS_MAP = {
    4: "online",                  # VERIFIED (baseline)
    2: "los",                     # VERIFIED (2x PON shutdown)
    3: "losi",                    # dari historis natural event
    5: "dying_gasp_hypothesis",   # belum trigger-tested
    6: "auth_failed_hypothesis",  # belum trigger-tested
    7: "offline_hypothesis",      # belum trigger-tested
    1: "unknown_1",               # 1 sample (~3s), belum solid
}

# Whitelist status SNMP yang boleh menimpa onu.status (Telnet-derived).
# Value di luar ini (1, 5, 6, 7, 0, None) -> fallback Telnet.
# Catatan: 3 (losi) dinormalisasi ke "los" karena alerting.py strict-match "los".
SNMP_TRUSTED_STATUS = {
    2: "los",
    3: "los",
    4: "online",
}
_ONU_OFFLINE_REASON_MAP = {
    2: "LOS",
    3: "LOSi",
}

# --- Board status (Fase 2a) --------------------------------------------------
_BOARD_BASE = "1.3.6.1.4.1.3902.1082.10.1.2.4.1"
_ENTITY_DESCR_BASE = "1.3.6.1.2.1.47.1.1.1.1.2"   # entPhysicalDescr

_BOARD_COLS = {
    4:  ("board_type", "str"),
    7:  ("cpu_5s", "pct"),
    9:  ("cpu_avg", "pct"),
    10: ("cpu_alarm_threshold", "pct"),
    11: ("mem_pct", "pct"),
    12: ("mem_alarm_threshold", "pct"),
    14: ("serial", "str"),
    19: ("phymem_mb", "int"),
    25: ("uptime_hex", "hex"),
    27: ("firmware", "str"),
}
CONTROL_BOARD_PREFIXES = ("SMX",)
CONTROL_SLOT_DEFAULT = 3


def _coerce_board(kind, raw):
    try:
        if kind == "str":
            return str(raw).strip() or None
        if kind == "hex":
            return str(raw)
        v = int(raw)
        if kind == "pct":
            return float(v) if 0 <= v <= 100 else None
        return v
    except (TypeError, ValueError):
        return None


def _is_empty_slot(board: dict) -> bool:
    return board.get("phymem_mb") == 0 and board.get("cpu_avg") == 0


def _parse_entity_index(oid_str: str, base: str):
    suffix = oid_str[len(base):].lstrip(".")
    if not suffix:
        return None
    try:
        return int(suffix.split(".")[-1])
    except ValueError:
        return None


def pick_control_slot(boards: dict, entity_rows: dict = None):
    entity_rows = entity_rows or {}
    for slot in sorted(boards):
        descr = (entity_rows.get(slot * 10) or "").lower()
        if "management" in descr:
            return slot
    for slot in sorted(boards):
        if (boards[slot].get("board_type") or "").upper().startswith(CONTROL_BOARD_PREFIXES):
            return slot
    return CONTROL_SLOT_DEFAULT if CONTROL_SLOT_DEFAULT in boards else None


# ── OID Traffic Migration (Fase 2) ───────────────────────────────────────────
_OID_IF_NAME     = "1.3.6.1.2.1.31.1.1.1.1"
_OID_IF_HC_IN    = "1.3.6.1.2.1.31.1.1.1.6"    # Counter64
_OID_IF_HC_OUT   = "1.3.6.1.2.1.31.1.1.1.10"   # Counter64


# Mapping status SNMP → status app
ONU_STATUS_MAP = {
    6: "online",
    0: "offline",
    # Nilai lain (LOS, dying_gasp) belum diverifikasi di lapangan.
    # Fallback: kalau > 0 tapi bukan 6 → "offline"
}


def _decode_optical(raw: int) -> Optional[float]:
    """Konversi raw SNMP ke dBm.

    Formula terverifikasi:
    - raw 6450  → -17.1 dBm
    - raw 3926  → -22.1 dBm
    - raw 16326 → +2.65 dBm
    """
    if raw is None:
        return None
    if raw == 65535:       # sentinel ZTE: tidak ada sinyal
        return None
    try:
        return round(raw * 0.002 - 30, 2)
    except Exception:
        return None



def _decode_onu_timestamp(raw) -> Optional[datetime]:
    """Decode 8-byte ZTE ONU timestamp: [yy_hi][yy_lo][mm][dd][hh][mm][ss][00].

    Verified 4x cross-check vs CLI `show gpon onu detail-info`
    (contoh: 07EA0A070F350100 -> 2026-10-07 15:53:01).
    """
    if raw is None:
        return None
    try:
        b = bytes(raw)
    except Exception:
        return None
    if len(b) != 8:
        return None
    year = (b[0] << 8) | b[1]
    month, day, hour, minute, second = b[2], b[3], b[4], b[5], b[6]
    if year == 0 and month == 0 and day == 0:
        return None
    try:
        return datetime(year, month, day, hour, minute, second)
    except ValueError:
        return None


def _decode_serial(val) -> Optional[str]:
    """Decode SN dari Hex-STRING / OctetString ZTE.

    Contoh: b'YYKC\x37_\x05\x11' → 'YYKC375F0511'
    4 byte pertama = prefix vendor (ASCII)
    4 byte sisanya = hex uppercase
    """
    if val is None:
        return None
    try:
        # pysnmp OctetString — ambil bytes langsung
        if hasattr(val, "asOctets"):
            raw = val.asOctets()
        elif isinstance(val, bytes):
            raw = val
        else:
            s = str(val).strip()
            if not s:
                return None
            clean = s.replace(" ", "").replace(":", "")
            raw = bytes.fromhex(clean)
        if len(raw) < 8:
            return None
        prefix = raw[:4].decode("ascii", errors="ignore")
        suffix = raw[4:].hex().upper()
        return prefix + suffix
    except Exception:
        return None


def _parse_index(oid: str) -> list:
    """Ambil tail index dari OID (angka setelah base)."""
    parts = oid.split(".")
    return [int(p) for p in parts if p.isdigit()]


def _global_idx_to_onu(global_idx: int, pon_name: str = "gpon_1/1/1") -> int | None:
    """Konversi global_idx SNMP -> onu_id.

    HANYA valid untuk PON di _ONU_TRAFFIC_BASE_IDX (sekarang: gpon_1/1/1).
    onu_id di luar _ONU_TRAFFIC_MAX_PER_PON ditolak -- kemungkinan besar
    gidx itu milik PON lain, bukan PON1.
    """
    base = _ONU_TRAFFIC_BASE_IDX.get(pon_name)
    if base is None:
        return None
    offset = global_idx - base
    if offset < 0 or offset % _ONU_TRAFFIC_STRIDE != 0:
        return None
    onu_id = (offset // _ONU_TRAFFIC_STRIDE) + 1
    if onu_id > _ONU_TRAFFIC_MAX_PER_PON:
        return None
    return onu_id


@dataclass
class SnmpOnuRow:
    pon_idx: int          # 268501248 dll (composite index ZTE)
    onu_id: int
    name: Optional[str] = None
    desc: Optional[str] = None
    onu_type: Optional[str] = None
    serial_number: Optional[str] = None
    status: Optional[str] = None
    rx_dbm: Optional[float] = None
    tx_dbm: Optional[float] = None
    distance_m: Optional[int] = None


class OltSnmpClient:
    """SNMP client untuk 1 OLT. Reusable — reuse engine + transport."""

    def __init__(self, host: str, community: str = "public",
                 port: int = 161, timeout: int = 3, retries: int = 2):
        if not _HAS_PYSNMP:
            raise RuntimeError("pysnmp tidak terinstall")
        self.host = host
        self.community = community
        self.port = port
        self.timeout = timeout
        self.retries = retries
        self._engine = None
        self._transport = None

    async def _get_transport(self):
        if self._transport is None:
            self._transport = await UdpTransportTarget.create(
                (self.host, self.port),
                timeout=self.timeout,
                retries=self.retries,
            )
        if self._engine is None:
            self._engine = SnmpEngine()
        return self._engine, self._transport

    async def get(self, oid: str):
        """SNMP GET satu OID."""
        engine, transport = await self._get_transport()
        err_ind, err_status, err_idx, var_binds = await get_cmd(
            engine,
            CommunityData(self.community, mpModel=1),  # v2c
            transport,
            ContextData(),
            ObjectType(ObjectIdentity(oid)),
        )
        if err_ind:
            raise RuntimeError(f"SNMP error: {err_ind}")
        if err_status:
            raise RuntimeError(f"SNMP status: {err_status.prettyPrint()}")
        return var_binds[0]

    async def walk(self, base_oid: str) -> list:
        """SNMP WALK — return list of (oid, value) pasangan.

        pysnmp 7.x: bulk_cmd adalah coroutine yang return 1 batch.
        Loop manual sampai batch kosong atau keluar subtree.
        """
        engine, transport = await self._get_transport()
        results = []
        current_oid = base_oid
        try:
            while True:
                err_ind, err_status, err_idx, var_binds = await bulk_cmd(
                    engine,
                    CommunityData(self.community, mpModel=1),
                    transport,
                    ContextData(),
                    0, 20,  # non-repeaters, max-repetitions
                    ObjectType(ObjectIdentity(current_oid)),
                    lexicographicMode=False,
                )
                if err_ind or err_status:
                    break
                if not var_binds:
                    break
                stop = False
                for vb in var_binds:
                    oid_str = str(vb[0])
                    if not oid_str.startswith(base_oid):
                        stop = True
                        break
                    results.append((oid_str, vb[1]))
                if stop:
                    break
                # Lanjut dari OID terakhir
                last_oid = str(var_binds[-1][0])
                if last_oid == current_oid:
                    break  # avoid infinite loop
                current_oid = last_oid
        except Exception as e:
            print(f"[SNMP-WALK] {base_oid} error: {e}")
        return results

    async def get_port_counters(self) -> dict:
        """Walk IF-MIB: ifName + ifHCInOctets + ifHCOutOctets.

        Return: {ifname: {"ifindex": int, "rx_octets": int, "tx_octets": int}}
        """
        names = await self.walk(OID_IF_NAME)
        if not names:
            return {}

        # Map ifindex -> ifname
        idx_to_name = {}
        for oid_str, val in names:
            tail = _parse_index(oid_str[len(OID_IF_NAME):])
            if tail:
                idx_to_name[tail[0]] = str(val)

        result = {}
        for name in idx_to_name.values():
            result[name] = {"ifindex": None, "rx_octets": None, "tx_octets": None}

        # Walk RX
        rx_rows = await self.walk(OID_IF_HC_IN)
        for oid_str, val in rx_rows:
            tail = _parse_index(oid_str[len(OID_IF_HC_IN):])
            if tail and tail[0] in idx_to_name:
                name = idx_to_name[tail[0]]
                result[name]["ifindex"] = tail[0]
                result[name]["rx_octets"] = int(val)

        # Walk TX
        tx_rows = await self.walk(OID_IF_HC_OUT)
        for oid_str, val in tx_rows:
            tail = _parse_index(oid_str[len(OID_IF_HC_OUT):])
            if tail and tail[0] in idx_to_name:
                name = idx_to_name[tail[0]]
                if result[name]["ifindex"] is None:
                    result[name]["ifindex"] = tail[0]
                result[name]["tx_octets"] = int(val)

        return result

    async def walk_onu_traffic(self) -> dict:
        """Walk OID_ONU_TRAFFIC_IN + OID_ONU_TRAFFIC_OUT -- SEQUENTIAL.

        Return: {global_idx: {"rx_octets": int, "tx_octets": int}} -- index
        MENTAH dari seluruh tabel (bisa lintas PON), belum di-map ke onu_id.
        """
        result: dict = {}
        rx_rows = await self.walk(OID_ONU_TRAFFIC_IN)
        for oid_str, val in rx_rows:
            tail = _parse_index(oid_str[len(OID_ONU_TRAFFIC_IN):])
            if tail:
                gidx = tail[0]
                result.setdefault(gidx, {"rx_octets": None, "tx_octets": None})
                result[gidx]["rx_octets"] = int(val)
        tx_rows = await self.walk(OID_ONU_TRAFFIC_OUT)
        for oid_str, val in tx_rows:
            tail = _parse_index(oid_str[len(OID_ONU_TRAFFIC_OUT):])
            if tail:
                gidx = tail[0]
                result.setdefault(gidx, {"rx_octets": None, "tx_octets": None})
                result[gidx]["tx_octets"] = int(val)
        return result

    async def get_onu_traffic_mapped(self) -> dict:
        """Walk via walk_onu_traffic(), map global_idx -> onu_id HANYA PON 1/1/1.

        Return: {onu_id: {"rx_octets": int, "tx_octets": int}}, bisa {}.
        """
        raw = await self.walk_onu_traffic()
        if not raw:
            return {}
        mapped = {}
        for gidx, counters in raw.items():
            onu_id = _global_idx_to_onu(gidx, "gpon_1/1/1")
            if onu_id is None:
                continue
            if onu_id in mapped and mapped[onu_id] != counters:
                print(f"[SNMP-TRAFFIC] WARNING: onu_id {onu_id} collision "
                      f"terdeteksi -- mapping PON1 mungkin tidak akurat")
            mapped[onu_id] = counters
        return mapped

    async def get_port_status(self) -> dict:
        """Walk ifName + ifAdminStatus + ifOperStatus -- SEQUENTIAL.

        Return: {ifname: {"ifindex": int, "admin": int, "oper": int}}
        """
        names = await self.walk(OID_IF_NAME)
        if not names:
            return {}
        idx_to_name = {}
        for oid_str, val in names:
            tail = _parse_index(oid_str[len(OID_IF_NAME):])
            if tail:
                idx_to_name[tail[0]] = str(val)
        result = {name: {"ifindex": None, "admin": None, "oper": None}
                  for name in idx_to_name.values()}
        admin_rows = await self.walk(OID_IF_ADMIN_STATUS)
        for oid_str, val in admin_rows:
            tail = _parse_index(oid_str[len(OID_IF_ADMIN_STATUS):])
            if tail and tail[0] in idx_to_name:
                name = idx_to_name[tail[0]]
                result[name]["ifindex"] = tail[0]
                result[name]["admin"] = int(val)
        oper_rows = await self.walk(OID_IF_OPER_STATUS)
        for oid_str, val in oper_rows:
            tail = _parse_index(oid_str[len(OID_IF_OPER_STATUS):])
            if tail and tail[0] in idx_to_name:
                name = idx_to_name[tail[0]]
                if result[name]["ifindex"] is None:
                    result[name]["ifindex"] = tail[0]
                result[name]["oper"] = int(val)
        return result

    async def get_onu_status_detail(self, pon_name: str = "gpon_1/1/1") -> dict:
        """Walk status granular + timestamp + offline-reason untuk 1 PON.

        HANYA PON di _ONU_STATUS_BASE_IDX (sekarang: gpon_1/1/1).
        Return: {onu_id: {status_raw, status_label, last_online,
                          last_offline, offline_reason_raw, offline_reason_label}}
        """
        base = _ONU_STATUS_BASE_IDX.get(pon_name)
        if base is None:
            return {}
        result: dict = {}

        async def _walk_field(oid_root, field):
            rows = await self.walk(oid_root)
            for oid_str, val in rows:
                tail = _parse_index(oid_str[len(oid_root):])
                if len(tail) != 2 or tail[0] != base:
                    continue
                onu_id = tail[1]
                result.setdefault(onu_id, {})
                result[onu_id][field] = val

        await _walk_field(OID_ONU_STATUS_GRANULAR, "status_raw")
        await _walk_field(OID_ONU_LAST_ONLINE, "last_online_raw")
        await _walk_field(OID_ONU_LAST_OFFLINE, "last_offline_raw")
        await _walk_field(OID_ONU_OFFLINE_REASON, "offline_reason_raw")

        for onu_id, f in result.items():
            sr = f.get("status_raw")
            f["status_label"] = (_ONU_STATUS_MAP.get(sr, f"unknown_{sr}")
                                  if sr is not None else None)
            f["last_online"] = _decode_onu_timestamp(f.pop("last_online_raw", None))
            f["last_offline"] = _decode_onu_timestamp(f.pop("last_offline_raw", None))
            rr = f.get("offline_reason_raw")
            f["offline_reason_label"] = (_ONU_OFFLINE_REASON_MAP.get(rr, f"unknown_{rr}")
                                          if rr is not None else None)
        return result

    async def get_board_status(self) -> dict:
        """Walk board metrics. Slot kosong (phymem=0 & cpu_avg=0) di-exclude.

        Return: {slot: {board_type, serial, cpu_5s, cpu_avg, cpu_alarm_threshold,
                        mem_pct, mem_alarm_threshold, phymem_mb, firmware,
                        uptime_hex, is_control}}
        """
        rows = await self.walk(_BOARD_BASE)
        boards: dict = {}
        for oid_str, raw in rows:
            tail = oid_str[len(_BOARD_BASE):].lstrip(".")
            parts = tail.split(".")
            if len(parts) != 4:
                continue
            try:
                col, slot = int(parts[0]), int(parts[3])
            except ValueError:
                continue
            spec = _BOARD_COLS.get(col)
            if spec:
                field, kind = spec
                boards.setdefault(slot, {})[field] = _coerce_board(kind, raw)

        boards = {s: b for s, b in boards.items() if not _is_empty_slot(b)}

        entity_rows: dict = {}
        try:
            entity_raw = await self.walk(_ENTITY_DESCR_BASE)
            for oid_str, val in entity_raw:
                idx = _parse_entity_index(oid_str, _ENTITY_DESCR_BASE)
                if idx is not None:
                    entity_rows[idx] = str(val)
        except Exception:
            pass

        ctrl_slot = pick_control_slot(boards, entity_rows)
        for slot, b in boards.items():
            b["is_control"] = (slot == ctrl_slot)
        return boards

    async def close(self) -> None:
        """Tutup pysnmp transport explicit untuk cegah FD/socket leak."""
        if self._transport:
            try:
                self._transport.close()
            except Exception:
                pass
            self._transport = None
        self._engine = None

    async def test_connection(self) -> dict:
        """Test koneksi SNMP — ambil sysDescr."""
        try:
            vb = await self.get(OID_SYS_DESCR)
            return {"ok": True, "sys_descr": str(vb[1])}
        except Exception as e:
            return {"ok": False, "error": str(e)}

    async def get_sys_uptime(self) -> Optional[int]:
        """Uptime OLT dalam detik."""
        try:
            vb = await self.get(OID_SYS_UPTIME)
            # Timeticks value: int sudah dalam centiseconds
            return int(vb[1]) // 100
        except Exception:
            return None

    async def list_onus(self) -> list[SnmpOnuRow]:
        """Ambil semua ONU via SNMP.

        Strategi: walk OID_ONU_NAME (base), lalu untuk tiap index
        walk OID lain di posisi sama. Karena ZTE pakai composite index,
        index dari walk NAME konsisten dengan walk lain.
        """
        # Walk name dulu — ini anchor index
        name_rows = await self.walk(OID_ONU_NAME)
        if not name_rows:
            return []

        # Build map: index_key -> row
        rows: dict[tuple, SnmpOnuRow] = {}
        for oid_str, val in name_rows:
            tail = _parse_index(oid_str[len(OID_ONU_NAME):])
            if len(tail) < 2:
                continue
            key = (tail[-2], tail[-1])
            rows[key] = SnmpOnuRow(
                pon_idx=tail[-2], onu_id=tail[-1], name=str(val)
            )

        # Walk OID lain paralel
        walks = await asyncio.gather(
            self.walk(OID_ONU_DESC),
            self.walk(OID_ONU_TYPE),
            self.walk(OID_ONU_SN),
            self.walk(OID_ONU_STATUS),
            self.walk(OID_ONU_RX),
            self.walk(OID_ONU_TX),
            self.walk(OID_ONU_DISTANCE),
            return_exceptions=True,
        )
        desc_rows, type_rows, sn_rows, status_rows, rx_rows, tx_rows, dist_rows = walks

        for oid_str, val in (desc_rows if isinstance(desc_rows, list) else []):
            tail = _parse_index(oid_str[len(OID_ONU_DESC):])
            if len(tail) >= 2:
                key = (tail[-2], tail[-1])
                if key in rows:
                    rows[key].desc = str(val)

        for oid_str, val in (type_rows if isinstance(type_rows, list) else []):
            tail = _parse_index(oid_str[len(OID_ONU_TYPE):])
            if len(tail) >= 2:
                key = (tail[-2], tail[-1])
                if key in rows:
                    rows[key].onu_type = str(val)

        for oid_str, val in (sn_rows if isinstance(sn_rows, list) else []):
            tail = _parse_index(oid_str[len(OID_ONU_SN):])
            if len(tail) >= 2:
                key = (tail[-2], tail[-1])
                if key in rows:
                    rows[key].serial_number = _decode_serial(val)

        for oid_str, val in (status_rows if isinstance(status_rows, list) else []):
            tail = _parse_index(oid_str[len(OID_ONU_STATUS):])
            if len(tail) >= 2:
                key = (tail[-2], tail[-1])
                if key in rows:
                    try:
                        rows[key].status = ONU_STATUS_MAP.get(int(val), "unknown")
                    except Exception:
                        pass

        for oid_str, val in (rx_rows if isinstance(rx_rows, list) else []):
            # OID RX punya index extra (…pon.onu.1) — ambil 2 angka terakhir
            tail = _parse_index(oid_str[len(OID_ONU_RX):])
            if len(tail) >= 2:
                key = (tail[-3], tail[-2]) if len(tail) >= 3 else (tail[-2], tail[-1])
                if key in rows:
                    try:
                        rows[key].rx_dbm = _decode_optical(int(val))
                    except Exception:
                        pass

        for oid_str, val in (tx_rows if isinstance(tx_rows, list) else []):
            tail = _parse_index(oid_str[len(OID_ONU_TX):])
            if len(tail) >= 2:
                key = (tail[-3], tail[-2]) if len(tail) >= 3 else (tail[-2], tail[-1])
                if key in rows:
                    try:
                        rows[key].tx_dbm = _decode_optical(int(val))
                    except Exception:
                        pass

        for oid_str, val in (dist_rows if isinstance(dist_rows, list) else []):
            tail = _parse_index(oid_str[len(OID_ONU_DISTANCE):])
            if len(tail) >= 2:
                key = (tail[-2], tail[-1])
                if key in rows:
                    try:
                        rows[key].distance_m = int(val)
                    except Exception:
                        pass

        return list(rows.values())


# =================== TEST STANDALONE ===================
if __name__ == "__main__":
    import sys
    import json
    import os
    host = sys.argv[1] if len(sys.argv) > 1 else os.environ.get("OLT_HOST")
    community = sys.argv[2] if len(sys.argv) > 2 else os.environ.get("SNMP_COMMUNITY")
    if not host or not community:
        sys.exit("Usage: OLT_HOST=x.x.x.x SNMP_COMMUNITY=xxx python olt_snmp.py [host] [community]")

    async def main():
        client = OltSnmpClient(host, community)
        print(f"[TEST] Connect ke {host} (community={community})")
        r = await client.test_connection()
        print(f"  test_connection: {r}")

        up = await client.get_sys_uptime()
        print(f"  uptime: {up} detik ({up//86400 if up else '?'} hari)")

        print(f"[TEST] Walk ONU list...")
        onus = await client.list_onus()
        print(f"  Total ONU: {len(onus)}")
        for o in onus:
            print(f"    {o.pon_idx}:{o.onu_id} SN={o.serial_number} name={o.name!r} "
                  f"status={o.status} rx={o.rx_dbm} tx={o.tx_dbm} dist={o.distance_m}m")

    asyncio.run(main())
