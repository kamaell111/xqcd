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

    t0 = _t.time()
    now_dt = datetime.utcnow()

    all_onus = (
        db.query(ONU)
        .filter(ONU.olt_id == olt.id)
        .order_by(ONU.id)
        .all()
    )
    if not all_onus:
        return {"ok": True, "saved": 0, "total": 0, "batch": 0,
                "elapsed_seconds": round(_t.time() - t0, 2)}

    n = len(all_onus)
    offset = _ONU_POLL_OFFSET.get(olt.id, 0)
    size = min(batch_size, n)
    batch = [all_onus[(offset + i) % n] for i in range(size)]
    _ONU_POLL_OFFSET[olt.id] = (offset + size) % n

    client = _make_client(olt)
    try:
        conn = client._connect()
    except Exception as e:
        return {"ok": False, "error": f"connect gagal: {e}",
                "elapsed_seconds": round(_t.time() - t0, 2)}

    saved = 0
    errors = []
    onu_entries = {}

    try:
        for onu in batch:
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
            except Exception as e:
                errors.append(f"{key}: {e}")
                continue
    finally:
        conn.disconnect()

    db.commit()

    return {
        "ok": True,
        "elapsed_seconds": round(_t.time() - t0, 2),
        "total": n,
        "batch": len(batch),
        "saved": saved,
        "offset_next": _ONU_POLL_OFFSET.get(olt.id, 0),
        "onu": onu_entries,
        "errors": errors[:10],
    }
