"""Light sync — dipicu oleh SNMP Trap.

Cek status ONU spesifik (1-2 command), update DB, trigger alert.
BUKAN sync-pons lengkap (yang 15 command). Ini cepat (~1 detik).
"""
import re
import asyncio
from datetime import datetime

from database import SessionLocal
from models import OLT, ONU
from olt_client import OLTClient, LAST_READ_SHORT
from olt_manager import olt_manager
from alerting import check_onu_alerts


def trigger_light_sync_from_trap(olt_ip: str):
    """Dipanggil dari trap receiver (event loop).

    Panggil light sync di thread executor biar tidak block event loop.
    """
    loop = asyncio.get_event_loop()
    loop.run_in_executor(None, _do_light_sync_by_ip, olt_ip)


def _do_light_sync_by_ip(olt_ip: str):
    """Cari OLT by IP, ambil lock, sync ringan."""
    db = SessionLocal()
    try:
        olt = db.query(OLT).filter(OLT.ip_address == olt_ip).first()
        if not olt:
            print(f"[TRAP-SYNC] OLT {olt_ip} tidak ada di DB — skip")
            return

        # Cek circuit breaker
        reason = olt_manager.is_olt_circuit_open(str(olt.id))
        if reason:
            print(f"[TRAP-SYNC] OLT {olt.ip_address} — {reason}, skip")
            return

        # Reuse lock yang sama dengan endpoint & scheduler
        lock = olt_manager.get_thread_lock(str(olt.id))
        if not lock.acquire(timeout=15):
            print(f"[TRAP-SYNC] OLT {olt.id} sibuk — skip trap sync")
            return

        try:
            _do_light_sync(db, olt)
            olt_manager.record_olt_result(str(olt.id), True)
        except Exception as e:
            olt_manager.record_olt_result(str(olt.id), False, str(e)[:200])
            raise
        finally:
            lock.release()
    except Exception as e:
        print(f"[TRAP-SYNC] Error: {e}")
    finally:
        db.close()


def _do_light_sync(db, olt: OLT):
    """Sync ringan: cek ONU state, update status, trigger alert."""
    print(f"[TRAP-SYNC] Light sync untuk OLT {olt.ip_address}")

    client = OLTClient(
        host=olt.ip_address,
        username=olt.username,
        password=olt.password,
        enable_password=olt.enable_password,
        port=olt.port,
        protocol=olt.protocol,
    )

    try:
        conn = client._connect()
        try:
            state_out = conn.send_command_timing(
                "show gpon onu state",
                read_timeout=15,
                last_read=LAST_READ_SHORT,
            )
        finally:
            conn.disconnect()
    except Exception as e:
        print(f"[TRAP-SYNC] Gagal cek OLT: {e}")
        return

    # Parse ONU state: "1/1/1:1  enable  enable  working  1(GPON)"
    active_onus = {}   # {(pon, onu_id): phase}
    for line in state_out.splitlines():
        s = line.strip()
        if not s or s.startswith("OnuIndex") or s.startswith("---") or s.startswith("ONU Number"):
            continue
        parts = s.split()
        if len(parts) < 4:
            continue
        idx = parts[0]   # "1/1/1:1"
        phase = parts[3].lower()
        try:
            pon, onu_id = idx.split(":")
            active_onus[(pon, int(onu_id))] = phase
        except ValueError:
            continue

    # Update ONU di DB
    onus = db.query(ONU).filter(ONU.olt_id == olt.id).all()
    changed = 0
    for onu in onus:
        key = (onu.pon_port, onu.onu_id)
        if key in active_onus:
            phase = active_onus[key]
            if phase == "working":
                new_status = "online"
            elif phase == "los":
                new_status = "los"           # ⭐ fiber putus, modem masih nyala
            elif phase == "dying-gasp":
                new_status = "dying_gasp"
            elif phase == "offline":
                new_status = "offline"       # ⭐ modem mati total
            elif phase in ("configuring", "initial"):
                new_status = "configuring"
            else:
                new_status = "unknown"
        else:
            # ONU hilang dari OLT → offline
            new_status = "offline"

        if onu.status != new_status:
            # ⭐ Catat event sebelum ubah status
            from event_log import log_onu_event
            log_onu_event(db, olt.id, onu, "status_change",
                          old_value=onu.status, new_value=new_status,
                          detail="via trap")
            onu.status = new_status
            onu.updated_at = datetime.utcnow()
            changed += 1
            if new_status == "dying_gasp":
                onu.last_dying_gasp = datetime.utcnow()

            # Kalau ONU offline ATAU LOS → PPPoE = disconnected
            # (LOS = fiber putus, OLT tidak bisa monitor ONU, internet pasti mati)
            if new_status in ("offline", "los"):
                onu.pppoe_status = "disconnected"
                onu.pppoe_online_duration = 0

            # Kalau ONU online → set "connecting" (polling PPPoE nanti)
            elif new_status == "online":
                onu.pppoe_status = "connecting"

            print(f"[TRAP-SYNC] {key} → {new_status}")
            try:
                check_onu_alerts(db, olt.id, onu)
            except Exception as e:
                print(f"[TRAP-SYNC] Alert error: {e}")

    # ⭐ COMMIT DULU — biar UI update cepat (status online/offline)
    db.commit()
    print(f"[TRAP-SYNC] Done — {changed} ONU diupdate (phase 1)")

    # ⭐ PHASE 2: poll PPPoE untuk ONU yang baru online (blocking, tapi setelah UI update)
    for onu in onus:
        key = (onu.pon_port, onu.onu_id)
        if key in active_onus and onu.status == "online" and onu.pppoe_status == "connecting":
            print(f"[TRAP-SYNC] {key} → polling PPPoE (max 30s)...")
            _poll_pppoe_until_connected(olt, onu, client)
            print(f"[TRAP-SYNC] {key} → PPPoE: {onu.pppoe_status}")
            db.commit()
            break   # hanya poll 1 ONU (yang baru online)


def _poll_pppoe_until_connected(olt, onu, client):
    """Poll PPPoE status tiap 5 detik, max 6x (30s). Update onu.pppoe_status langsung."""
    import time as _time
    for attempt in range(6):
        _time.sleep(5)
        try:
            conn = client._connect()
            try:
                pppoe_out = conn.send_command_timing(
                    f"show gpon remote-onu pppoe gpon-onu_{onu.pon_port}:{onu.onu_id}",
                    read_timeout=15,
                    last_read=0.3,
                )
            finally:
                conn.disconnect()
            m = re.search(r"Status:\s+(\S+)", pppoe_out)
            if m:
                onu.pppoe_status = m.group(1).lower()
            m = re.search(r"Online duration:\s+(\d+)", pppoe_out)
            if m:
                onu.pppoe_online_duration = int(m.group(1))
            print(f"[TRAP-SYNC]   attempt {attempt+1}: {onu.pppoe_status}")
            if onu.pppoe_status == "connected":
                return
        except Exception as e:
            print(f"[TRAP-SYNC]   attempt {attempt+1} error: {e}")

    # Optional: kalau perlu, update PON port count juga
    # (skip untuk sekarang — cukup ONU)
