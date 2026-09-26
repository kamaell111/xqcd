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
    try:
        return round(raw * 0.002 - 30, 2)
    except Exception:
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
    host = sys.argv[1] if len(sys.argv) > 1 else "136.1.1.200"
    community = sys.argv[2] if len(sys.argv) > 2 else "bagoes_ro"

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
