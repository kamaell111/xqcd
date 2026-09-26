"""Traffic poller — extract dari endpoint /traffic/poll.

Reusable untuk:
- Endpoint manual POST /olts/{id}/traffic/poll
- Scheduler job poll_traffic (tiap 5 menit)

Simpan rate + raw counter ke table traffic_sample.
"""
from __future__ import annotations
import time as _t
from datetime import datetime
from typing import Optional
from sqlalchemy.orm import Session


async def poll_traffic_for_olt(db: Session, olt) -> dict:
    """Poll IF-MIB counters via SNMP, hitung rate, simpan ke DB.

    Args:
        db: SQLAlchemy session (WAJIB milik caller, jangan commit di sini)
        olt: model OLT instance

    Returns:
        dict: {ok, saved, elapsed_seconds, pon: {...}, uplink: {...}}
    """
    from olt_snmp import OltSnmpClient
    from models import TrafficSample

    community = olt.snmp_community_ro or "public"
    port = olt.snmp_port or 161

    client = OltSnmpClient(olt.ip_address, community, port=port)
    t0 = _t.time()

    counters = await client.get_port_counters()
    elapsed = round(_t.time() - t0, 2)

    now_dt = datetime.utcnow()
    now_ts = _t.time()
    saved = 0

    pon = {}
    uplink = {}

    for name, data in counters.items():
        # Tentukan scope
        if name.startswith("gpon_"):
            scope = "pon"
        elif name.startswith("gei_") or name.startswith("xgei_"):
            scope = "uplink"
        else:
            continue

        rx_now = data.get("rx_octets")
        tx_now = data.get("tx_octets")

        # Cari sample terakhir
        prev = (
            db.query(TrafficSample)
            .filter(
                TrafficSample.olt_id == olt.id,
                TrafficSample.scope == scope,
                TrafficSample.entity_key == name,
            )
            .order_by(TrafficSample.ts.desc())
            .first()
        )

        rx_bps = None
        tx_bps = None

        if prev and prev.ts and prev.raw_rx_octets is not None and prev.raw_tx_octets is not None:
            dt_sec = (now_dt - prev.ts).total_seconds()
            if dt_sec > 0.5 and rx_now is not None and tx_now is not None:
                # Handle counter reset
                if rx_now >= prev.raw_rx_octets:
                    rx_bps = (rx_now - prev.raw_rx_octets) * 8.0 / dt_sec
                if tx_now >= prev.raw_tx_octets:
                    tx_bps = (tx_now - prev.raw_tx_octets) * 8.0 / dt_sec

        sample = TrafficSample(
            olt_id=olt.id,
            scope=scope,
            entity_key=name,
            rx_bps=rx_bps,
            tx_bps=tx_bps,
            raw_rx_octets=rx_now,
            raw_tx_octets=tx_now,
            ts=now_dt,
        )
        db.add(sample)
        saved += 1

        entry = {
            "ifindex": data.get("ifindex"),
            "rx_octets": rx_now,
            "tx_octets": tx_now,
            "rx_bps": rx_bps,
            "tx_bps": tx_bps,
        }
        if scope == "pon":
            pon[name] = entry
        else:
            uplink[name] = entry

    db.commit()

    return {
        "ok": True,
        "elapsed_seconds": elapsed,
        "pon_count": len(pon),
        "uplink_count": len(uplink),
        "saved": saved,
        "pon": pon,
        "uplink": uplink,
        "ts": now_ts,
    }
