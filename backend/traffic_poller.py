"""Traffic poller via Telnet (bukan SNMP).

Data per scope:
- PON: 16 perintah `show interface gpon-olt_1/1/X`
- Uplink: N perintah `show interface <name>` (dari tabel Interface)

Simpan rate + raw counter ke table traffic_sample.
"""
from __future__ import annotations
import time as _t
from datetime import datetime
from sqlalchemy.orm import Session

from olt_client import OLTClient, LAST_READ_LONG
from zxan_parser import (parse_pon_traffic_counter, parse_uplink_traffic_counter,
                         parse_onu_traffic_counter)
import asyncio
from olt_snmp import OltSnmpClient
from config import settings


# Round-robin offset per OLT (in-memory)
_ONU_POLL_OFFSET = {}


def _make_client(olt) -> OLTClient:
    return OLTClient(
        host=olt.ip_address,
        username=olt.username,
        password=olt.password,
        enable_password=olt.enable_password,
        port=olt.port,
        protocol=olt.protocol,
    )


def _snmp_port_counters_sync(olt) -> dict | None:
    """Sync wrapper untuk OltSnmpClient.get_port_counters()."""
    client = None
    loop = None
    try:
        client = OltSnmpClient(
            olt.ip_address,
            olt.snmp_community_ro,
            port=olt.snmp_port or 161,
        )
        loop = asyncio.new_event_loop()
        return loop.run_until_complete(client.get_port_counters())
    except Exception as e:
        print(f"[SNMP-TRAFFIC] OLT {olt.id}: error {type(e).__name__}")
        return None
    finally:
        if client is not None and loop is not None:
            try:
                loop.run_until_complete(client.close())
            except Exception:
                pass
        if loop is not None:
            try:
                loop.close()
            except Exception:
                pass


def _snmp_onu_traffic_sync(olt) -> dict | None:
    """Sync wrapper untuk OltSnmpClient.get_onu_traffic_mapped()."""
    client = None
    loop = None
    try:
        client = OltSnmpClient(
            olt.ip_address,
            olt.snmp_community_ro,
            port=olt.snmp_port or 161,
        )
        loop = asyncio.new_event_loop()
        return loop.run_until_complete(client.get_onu_traffic_mapped())
    except Exception as e:
        print(f"[SNMP-TRAFFIC] OLT {olt.id}: ONU traffic error {type(e).__name__}")
        return None
    finally:
        if client is not None and loop is not None:
            try:
                loop.run_until_complete(client.close())
            except Exception:
                pass
        if loop is not None:
            try:
                loop.close()
            except Exception:
                pass


def _save_sample(db, olt_id, scope, entity_key, rx_now, tx_now, now_dt):
    """Hitung rate dari delta counter, simpan ke TrafficSample. Return dict entry."""
    from models import TrafficSample

    prev = (
        db.query(TrafficSample)
        .filter(
            TrafficSample.olt_id == olt_id,
            TrafficSample.scope == scope,
            TrafficSample.entity_key == entity_key,
        )
        .order_by(TrafficSample.ts.desc())
        .first()
    )

    rx_bps = None
    tx_bps = None

    if prev and prev.ts and prev.raw_rx_octets is not None and prev.raw_tx_octets is not None:
        dt_sec = (now_dt - prev.ts).total_seconds()
        if dt_sec > 0.5 and rx_now is not None and tx_now is not None:
            if rx_now >= prev.raw_rx_octets:
                rx_bps = (rx_now - prev.raw_rx_octets) * 8.0 / dt_sec
            if tx_now >= prev.raw_tx_octets:
                tx_bps = (tx_now - prev.raw_tx_octets) * 8.0 / dt_sec

    sample = TrafficSample(
        olt_id=olt_id,
        scope=scope,
        entity_key=entity_key,
        rx_bps=rx_bps,
        tx_bps=tx_bps,
        raw_rx_octets=rx_now,
        raw_tx_octets=tx_now,
        ts=now_dt,
    )
    db.add(sample)

    return {"rx_octets": rx_now, "tx_octets": tx_now, "rx_bps": rx_bps, "tx_bps": tx_bps}


def poll_traffic_for_olt(db: Session, olt) -> dict:
    """Poll traffic PON + Uplink via Telnet. Hitung rate, simpan ke DB."""
    from models import Interface

    t0 = _t.time()
    now_dt = datetime.utcnow()
    mode = settings.SNMP_TRAFFIC_MODE

    if mode == "snmp":
        snmp_data = _snmp_port_counters_sync(olt)
        if snmp_data:
            saved = 0
            pon = {}
            uplink = {}
            for i in range(1, 17):
                name = f"gpon_1/1/{i}"
                c = snmp_data.get(name)
                if not c:
                    continue
                entry = _save_sample(db, olt.id, "pon", name,
                                     c["rx_octets"], c["tx_octets"], now_dt)
                pon[name] = entry
                saved += 1
            uplink_names = [iface.name for iface in
                            db.query(Interface).filter(Interface.olt_id == olt.id).all()]
            for name in uplink_names:
                c = snmp_data.get(name)
                if not c:
                    continue
                entry = _save_sample(db, olt.id, "uplink", name,
                                     c["rx_octets"], c["tx_octets"], now_dt)
                uplink[name] = entry
                saved += 1
            if saved > 0:
                db.commit()
                print(f"[SNMP-TRAFFIC] OLT {olt.id}: {saved} sample via SNMP")
                return {"ok": True, "source": "snmp", "saved": saved,
                        "pon_count": len(pon), "uplink_count": len(uplink),
                        "pon": pon, "uplink": uplink, "errors": [],
                        "elapsed_seconds": round(_t.time() - t0, 2),
                        "ts": _t.time()}
        print(f"[SNMP-TRAFFIC] OLT {olt.id}: fallback Telnet")

    client = _make_client(olt)

    pon = {}
    uplink = {}
    saved = 0
    errors = []

    try:
        conn = client._connect()
    except Exception as e:
        return {"ok": False, "error": f"connect gagal: {e}", "elapsed_seconds": round(_t.time() - t0, 2)}

    try:
        # --- PON: 16 port ---
        for i in range(1, 17):
            name = f"gpon_1/1/{i}"
            try:
                out = conn.send_command_timing(
                    f"show interface gpon-olt_1/1/{i}",
                    read_timeout=15, last_read=LAST_READ_LONG,
                )
                parsed = parse_pon_traffic_counter(out)
                if parsed["rx_octets"] is None and parsed["tx_octets"] is None:
                    continue
                entry = _save_sample(db, olt.id, "pon", name,
                                     parsed["rx_octets"], parsed["tx_octets"], now_dt)
                pon[name] = entry
                saved += 1
            except Exception as e:
                errors.append(f"pon {name}: {e}")
                continue

        # --- Uplink: dari tabel Interface ---
        uplink_names = [iface.name for iface in db.query(Interface).filter(Interface.olt_id == olt.id).all()]
        for name in uplink_names:
            try:
                out = conn.send_command_timing(
                    f"show interface {name}",
                    read_timeout=15, last_read=LAST_READ_LONG,
                )
                parsed = parse_uplink_traffic_counter(out)
                if parsed["rx_octets"] is None and parsed["tx_octets"] is None:
                    continue
                entry = _save_sample(db, olt.id, "uplink", name,
                                     parsed["rx_octets"], parsed["tx_octets"], now_dt)
                uplink[name] = entry
                saved += 1
            except Exception as e:
                errors.append(f"uplink {name}: {e}")
                continue
    finally:
        conn.disconnect()

    db.commit()
    elapsed = round(_t.time() - t0, 2)

    return {
        "ok": True,
        "elapsed_seconds": elapsed,
        "pon_count": len(pon),
        "uplink_count": len(uplink),
        "saved": saved,
        "pon": pon,
        "uplink": uplink,
        "errors": errors[:10],
        "ts": _t.time(),
    }



def poll_traffic_onu_batch(db: Session, olt, batch_size: int = 30) -> dict:
    """Poll traffic ONU batch (round-robin). Simpan ke traffic_sample scope='onu'.

    Entity key: interface_name ONU (misal 'gpon-onu_1/1/1:1').
    """
    from models import ONU

    mode = settings.SNMP_TRAFFIC_MODE
    t0 = _t.time()
    now_dt = datetime.utcnow()

    all_onus = (
        db.query(ONU)
        .filter(ONU.olt_id == olt.id)
        .order_by(ONU.id)
        .all()
    )
    if not all_onus:
        return {"ok": True, "source": "none", "saved": 0, "total": 0, "batch": 0,
                "snmp_saved": 0, "telnet_saved": 0,
                "elapsed_seconds": round(_t.time() - t0, 2)}

    n = len(all_onus)
    offset = _ONU_POLL_OFFSET.get(olt.id, 0)
    size = min(batch_size, n)
    batch = [all_onus[(offset + i) % n] for i in range(size)]
    _ONU_POLL_OFFSET[olt.id] = (offset + size) % n

    saved = 0
    snmp_saved = 0
    telnet_saved = 0
    errors = []
    onu_entries = {}
    telnet_batch = list(batch)

    if mode == "snmp":
        snmp_map = _snmp_onu_traffic_sync(olt)
        if snmp_map:
            telnet_batch = []
            for onu in batch:
                c = snmp_map.get(onu.onu_id) if onu.pon_port == "1/1/1" else None
                if c is not None and c.get("rx_octets") is not None and c.get("tx_octets") is not None:
                    key = onu.interface_name or f"gpon-onu_{onu.pon_port}:{onu.onu_id}"
                    try:
                        entry = _save_sample(db, olt.id, "onu", key,
                                             c["rx_octets"], c["tx_octets"], now_dt)
                        onu_entries[key] = entry
                        saved += 1
                        snmp_saved += 1
                    except Exception as e:
                        errors.append(f"{key}: {type(e).__name__}")
                        telnet_batch.append(onu)
                else:
                    if onu.pon_port != "1/1/1":
                        print(f"[SNMP-TRAFFIC] ONU {onu.id} PON {onu.pon_port}: "
                              f"belum didukung SNMP, fallback Telnet")
                    telnet_batch.append(onu)

    if telnet_batch:
        client = _make_client(olt)
        try:
            conn = client._connect()
        except Exception as e:
            if saved == 0:
                return {"ok": False, "error": f"connect gagal: {type(e).__name__}",
                        "elapsed_seconds": round(_t.time() - t0, 2)}
            errors.append(f"telnet connect gagal: {type(e).__name__}")
            conn = None
        if conn:
            try:
                for onu in telnet_batch:
                    key = onu.interface_name or f"gpon-onu_{onu.pon_port}:{onu.onu_id}"
                    try:
                        out = conn.send_command_timing(
                            f"show interface {key}",
                            read_timeout=15, last_read=LAST_READ_LONG,
                        )
                        parsed = parse_onu_traffic_counter(out)
                        if parsed["rx_octets"] is None and parsed["tx_octets"] is None:
                            continue
                        entry = _save_sample(db, olt.id, "onu", key,
                                             parsed["rx_octets"], parsed["tx_octets"], now_dt)
                        onu_entries[key] = entry
                        saved += 1
                        telnet_saved += 1
                    except Exception as e:
                        errors.append(f"{key}: {type(e).__name__}")
                        continue
            finally:
                conn.disconnect()

    db.commit()

    if snmp_saved > 0 and telnet_saved > 0:
        source = "mixed"
    elif snmp_saved > 0:
        source = "snmp"
    elif telnet_saved > 0:
        source = "telnet"
    else:
        source = "none"

    return {
        "ok": True,
        "source": source,
        "elapsed_seconds": round(_t.time() - t0, 2),
        "total": n,
        "batch": len(batch),
        "saved": saved,
        "snmp_saved": snmp_saved,
        "telnet_saved": telnet_saved,
        "offset_next": _ONU_POLL_OFFSET.get(olt.id, 0),
        "onu": onu_entries,
        "errors": errors[:10],
    }
