import asyncio
"""Sync data real dari OLT ZXAN → DB."""
from fastapi import APIRouter, Depends, HTTPException
from sqlalchemy.orm import Session
from datetime import datetime
import re

from database import get_db
from models import OLT, User, MetricHistory
from auth import get_current_user, audit
from olt_client import OLTClient
from olt_manager import olt_locked
from tenancy import require_olt_access
from olt_client import LAST_READ_SHORT, LAST_READ_LONG
from alerting import check_olt_resource_alerts, check_onu_alerts

router = APIRouter(prefix="/api/v1/olts", tags=["sync"])


def _make_client(olt: OLT) -> OLTClient:
    return OLTClient(
        host=olt.ip_address,
        username=olt.username,
        password=olt.password,
        enable_password=olt.enable_password,
        port=olt.port,
        protocol=olt.protocol,
    )


def _parse_uptime(text: str) -> int:
    """Parse '0 days, 9 hours, 4 minutes' → detik."""
    d = re.search(r"(\d+)\s+day", text)
    h = re.search(r"(\d+)\s+hour", text)
    m = re.search(r"(\d+)\s+minute", text)
    return (int(d.group(1)) if d else 0) * 86400 \
         + (int(h.group(1)) if h else 0) * 3600 \
         + (int(m.group(1)) if m else 0) * 60


def _parse_processor(output: str):
    """Parse 'show processor' → cpu_usage, memory_usage (max antar card)."""
    start = False
    cards = []
    for line in output.splitlines():
        if "---" in line:
            start = True
            continue
        if not start:
            continue
        parts = line.split()
        if len(parts) < 8:
            continue
        try:
            cards.append({
                "rack": int(parts[0]),
                "shelf": int(parts[1]),
                "slot": int(parts[2]),
                "cpu_5s": float(parts[3].rstrip("%")),
                "cpu_1m": float(parts[4].rstrip("%")),
                "cpu_5m": float(parts[5].rstrip("%")),
                "physmem_mb": int(parts[6]),
                "memory": float(parts[7].rstrip("%")),
            })
        except (ValueError, IndexError):
            continue
    if not cards:
        return None
    # Ambil card slot 1 saja (card GPON/utama) — konsisten dengan dashboard
    main = next((c for c in cards if c["slot"] == 1), cards[0])
    return {
        "cpu_usage": main["cpu_5m"],
        "memory_usage": main["memory"],
        "cards": cards,
    }


def _parse_system_group(output: str):
    """Parse 'show system-group' → hostname, uptime, model, firmware, location."""
    r = {"hostname": None, "uptime_seconds": None,
         "model": None, "firmware": None, "location": None}
    for line in output.splitlines():
        line = line.strip()
        if line.startswith("System Description:"):
            m = re.search(r"(\S+)\s+Version\s+(\S+)", line)
            if m:
                r["model"] = m.group(1)
                r["firmware"] = m.group(2)
        elif line.startswith("Started before:"):
            r["uptime_seconds"] = _parse_uptime(line)
        elif line.startswith("System name:"):
            r["hostname"] = line.split(":", 1)[1].strip()
        elif line.startswith("Location:"):
            r["location"] = line.split(":", 1)[1].strip()
    return r


@router.post("/{olt_id}/sync")
@olt_locked
def sync_olt(db: Session = Depends(get_db),
             olt: OLT = Depends(require_olt_access),
             user: User = Depends(get_current_user)):
    client = _make_client(olt)
    try:
        conn = client._connect()
        try:
            proc_out = conn.send_command_timing("show processor", read_timeout=30, last_read=LAST_READ_LONG)
            sys_out = conn.send_command_timing("show system-group", read_timeout=30, last_read=LAST_READ_LONG)
        finally:
            conn.disconnect()
    except Exception as e:
        olt.status = "offline"
        db.commit()
        audit(db, user.username, "sync_olt", str(olt.id), str(e), "failed")
        raise HTTPException(500, f"Gagal sync: {e}")

    proc = _parse_processor(proc_out)
    sysinfo = _parse_system_group(sys_out)

    if proc:
        olt.cpu_usage = proc["cpu_usage"]
        olt.memory_usage = proc["memory_usage"]
        # Simpan ke history untuk chart
        now = datetime.utcnow()
        db.add(MetricHistory(olt_id=olt_id, metric="cpu",
                             value=proc["cpu_usage"], ts=now))
        db.add(MetricHistory(olt_id=olt_id, metric="memory",
                             value=proc["memory_usage"], ts=now))
    if sysinfo:
        if sysinfo["hostname"]:
            olt.hostname = sysinfo["hostname"]
        if sysinfo["uptime_seconds"] is not None:
            olt.uptime_seconds = sysinfo["uptime_seconds"]
        if sysinfo["model"]:
            olt.model = sysinfo["model"]
        if sysinfo["firmware"]:
            olt.firmware = sysinfo["firmware"]
        if sysinfo["location"]:
            olt.location = sysinfo["location"]

    olt.status = "online"
    olt.last_polled = datetime.utcnow()
    db.commit()
    db.refresh(olt)

    audit(db, user.username, "sync_olt", str(olt_id))

    return {
        "ok": True,
        "olt": {
            "hostname": olt.hostname,
            "model": olt.model,
            "firmware": olt.firmware,
            "location": olt.location,
            "cpu_usage": olt.cpu_usage,
            "memory_usage": olt.memory_usage,
            "uptime_seconds": olt.uptime_seconds,
            "status": olt.status,
            "last_polled": olt.last_polled,
        },
        "raw": {
            "processor": proc_out,
            "system_group": sys_out,
        },
    }


# ============================================================
# SYNC PON & ONU
# ============================================================

def _parse_olt_interface(output: str) -> dict:
    """Parse 'show interface gpon-olt_1/1/X' → status, onu count."""
    r = {"status": "down", "onu_registered": 0, "onu_max": 0, "description": None}
    for line in output.splitlines():
        line = line.strip()
        if "is activate" in line and "line protocol is up" in line:
            r["status"] = "up"
        elif "is activate" in line and "line protocol is down" in line:
            r["status"] = "down"
        elif "Description is" in line:
            r["description"] = line.split("is", 1)[1].strip().rstrip(".")
        elif "number of registered onus is" in line:
            import re as _re
            m = _re.search(r"number of registered onus is (\d+)", line)
            if m:
                r["onu_registered"] = int(m.group(1))
            m2 = _re.search(r"has (\d+) onus", line)
            if m2:
                r["onu_max"] = int(m2.group(1))
    return r


def _parse_onu_state(output: str) -> list:
    """Parse 'show gpon onu state' → list semua ONU aktif."""
    onus = []
    started = False
    for line in output.splitlines():
        if "---" in line:
            started = True
            continue
        if not started or not line.strip():
            continue
        if line.startswith("ONU Number:"):
            break
        parts = line.split()
        if len(parts) < 5:
            continue
        # Format: 1/1/1:1   enable   enable   working   1(GPON)
        idx = parts[0]           # 1/1/1:1
        try:
            pon_and_id = idx.split(":")
            pon = pon_and_id[0]      # 1/1/1
            onu_id = int(pon_and_id[1])
        except (ValueError, IndexError):
            continue
        onus.append({
            "onu_index": idx,
            "pon_port": pon,
            "onu_id": onu_id,
            "admin_state": parts[1],
            "omcc_state": parts[2],
            "phase_state": parts[3],
            "channel": parts[4] if len(parts) > 4 else "",
        })
    return onus


def _parse_onu_uncfg(output: str) -> list:
    """Parse 'show gpon onu uncfg' → list ONU belum terdaftar."""
    uncfg = []
    started = False
    for line in output.splitlines():
        if "---" in line:
            started = True
            continue
        if not started or not line.strip():
            continue
        parts = line.split()
        if len(parts) < 3:
            continue
        idx = parts[0]
        sn = parts[1]
        state = parts[2]
        # Vendor dari prefix SN
        vendor = "Unknown"
        if sn.startswith("HWTC"):
            vendor = "Huawei"
        elif sn.startswith("ZTEG") or sn.startswith("YYKC"):
            vendor = "ZTE"
        elif sn.startswith("FHTT"):
            vendor = "Fiberhome"
        uncfg.append({
            "onu_index": idx,
            "serial_number": sn,
            "state": state,
            "vendor": vendor,
        })
    return uncfg


def _parse_gpon_ports_from_config(running_config: str) -> dict:
    """Parse admin state gpon-olt_1/1/X dari running-config."""
    ports = {}
    current = None
    for line in running_config.splitlines():
        stripped = line.strip()
        m = __import__("re").match(r"^interface gpon-olt_(1/1/\d+)$", stripped)
        if m:
            current = m.group(1)
            ports[current] = {"admin_state": "shutdown"}
            continue
        if current and stripped == "no shutdown":
            ports[current]["admin_state"] = "no shutdown"
        if current and stripped == "!":
            current = None
    return ports


def _parse_remote_pppoe(output: str) -> dict:
    """Parse 'show gpon remote-onu pppoe gpon-onu_X' → status + duration.
    Return: {"status": "connected|disconnected|connecting|unknown",
             "online_duration": int (detik),
             "username": str, "nat": str}"""
    import re as _re
    r = {"status": "unknown", "online_duration": 0, "username": None, "nat": None}
    for line in output.splitlines():
        s = line.strip()
        m = _re.match(r"Status:\s+(\S+)", s)
        if m:
            r["status"] = m.group(1).lower()
            continue
        m = _re.match(r"Online duration:\s+(\d+)", s)
        if m:
            r["online_duration"] = int(m.group(1))
            continue
        m = _re.match(r"Username:\s+(\S+)", s)
        if m:
            r["username"] = m.group(1)
            continue
        m = _re.match(r"NAT:\s+(\S+)", s)
        if m:
            r["nat"] = m.group(1)
    return r


def _parse_onu_config_from_running(running: str) -> dict:
    """Parse blok config ONU dari running-config → dict per onu_index.

    Return: {onu_index: {name, sn, type, tcont, gemport, service_port,
                          user_vlan, vlan, pppoe_user, pppoe_nat}}
    """
    import re as _re
    result = {}

    # 1. Parse "onu X type F609 sn YYY" dari interface gpon-olt_*  → SN & type
    sn_map = {}   # {(pon, onu_id): {sn, type}}
    current_olt = None
    for line in running.splitlines():
        s = line.strip()
        m = _re.match(r"^interface gpon-olt_(1/1/\d+)$", s)
        if m:
            current_olt = m.group(1)
            continue
        if s == "!":
            current_olt = None
            continue
        if current_olt:
            m2 = _re.match(r"^onu (\d+) type (\S+) sn (\S+)", s)
            if m2:
                sn_map[(current_olt, int(m2.group(1)))] = {
                    "type": m2.group(2),
                    "sn": m2.group(3),
                }

    # 2. Parse "interface gpon-onu_X:Y" + "pon-onu-mng gpon-onu_X:Y"
    current = None
    current_type = None
    for line in running.splitlines():
        s = line.strip()
        m = _re.match(r"^interface gpon-onu_(1/1/\d+):(\d+)$", s)
        if m:
            current = f"{m.group(1)}:{m.group(2)}"
            current_type = "interface"
            result.setdefault(current, {"pon": m.group(1), "onu_id": int(m.group(2))})
            continue
        m = _re.match(r"^pon-onu-mng gpon-onu_(1/1/\d+):(\d+)$", s)
        if m:
            current = f"{m.group(1)}:{m.group(2)}"
            current_type = "pon-onu-mng"
            result.setdefault(current, {"pon": m.group(1), "onu_id": int(m.group(2))})
            continue
        if s == "!":
            current = None
            current_type = None
            continue
        if not current:
            continue

        d = result[current]
        if current_type == "interface":
            m2 = _re.match(r"^name (.+)$", s)
            if m2: d["name"] = m2.group(1).strip()
            m2 = _re.match(r"^tcont (\d+) name (\S+) profile (\S+)", s)
            if m2: d["tcont"] = m2.group(3)
            m2 = _re.match(r"^gemport (\d+) tcont", s)
            if m2: d["gemport"] = int(m2.group(1))
            m2 = _re.match(r"^service-port (\d+) vport (\d+) user-vlan (\d+) vlan (\d+)", s)
            if m2:
                d["service_port"] = int(m2.group(1))
                d["vport"] = int(m2.group(2))
                d["user_vlan"] = int(m2.group(3))
                d["vlan"] = int(m2.group(4))
        elif current_type == "pon-onu-mng":
            m2 = _re.match(r"^pppoe (\d+) nat (\S+) user (\S+) password", s)
            if m2:
                d["pppoe_user"] = m2.group(3)
                d["pppoe_nat"] = (m2.group(2) == "enable")

    # 3. Merge SN & type dari langkah 1
    for idx, d in result.items():
        key = (d.get("pon"), d.get("onu_id"))
        if key in sn_map:
            d["sn"] = sn_map[key]["sn"]
            d["type"] = sn_map[key]["type"]

    return result


def _parse_onu_detail(output: str) -> dict:
    """Parse 'show gpon onu detail-info gpon-onu_X' → distance, online_duration."""
    import re as _re
    r = {"distance": None, "online_duration": 0}
    for line in output.splitlines():
        s = line.strip()
        m = _re.match(r"ONU Distance:\s+(\d+)m", s)
        if m:
            r["distance"] = int(m.group(1))
            continue
        # "Online Duration: 11h 02m 44s"
        m = _re.match(r"Online Duration:\s+(.+)", s)
        if m:
            dur_str = m.group(1).strip()
            h = _re.search(r"(\d+)h", dur_str)
            mm = _re.search(r"(\d+)m", dur_str)
            sec = _re.search(r"(\d+)s", dur_str)
            total = 0
            if h: total += int(h.group(1)) * 3600
            if mm: total += int(mm.group(1)) * 60
            if sec: total += int(sec.group(1))
            r["online_duration"] = total
    return r


def _parse_attenuation(output: str) -> dict:
    """Parse 'show pon power attenuation gpon-onu_X' → optical + attenuation."""
    r = {"olt_rx": None, "olt_tx": None, "onu_rx": None, "onu_tx": None,
         "atten_up": None, "atten_down": None}
    import re as _re
    for line in output.splitlines():
        # Baris up: up  Rx :-19.875(dbm)  Tx:2.158(dbm)  22.033(dB)
        if line.strip().startswith("up "):
            m = _re.search(r"Rx\s*:(-?[\d.]+)\(dbm\)", line)
            if m: r["olt_rx"] = float(m.group(1))
            m = _re.search(r"Tx\s*:(-?[\d.]+)\(dbm\)", line)
            if m: r["onu_tx"] = float(m.group(1))
            m = _re.search(r"(\d+\.\d+)\(dB\)", line)
            if m: r["atten_up"] = float(m.group(1))
        # Baris down: down  Tx :6.574(dbm)  Rx:-15.544(dbm)  22.118(dB)
        elif line.strip().startswith("down "):
            m = _re.search(r"Tx\s*:(-?[\d.]+)\(dbm\)", line)
            if m: r["olt_tx"] = float(m.group(1))
            m = _re.search(r"Rx\s*:(-?[\d.]+)\(dbm\)", line)
            if m: r["onu_rx"] = float(m.group(1))
            m = _re.search(r"(\d+\.\d+)\(dB\)", line)
            if m: r["atten_down"] = float(m.group(1))
    return r


def _do_sync_pons(db: Session, olt: OLT) -> dict:
    """Core sync PON + ONU — dipakai endpoint DAN scheduler.
    Raise Exception kalau gagal. Caller yang handle commit + audit."""
    from models import PONPort, ONU
    from olt_manager import olt_manager
    olt_id = olt.id
    client = _make_client(olt)
    optical_map = {}    # {onu_index: {"rx": ..., "tx": ...}}
    pppoe_map = {}      # {onu_index: {"status": ..., "online_duration": ..., "username": ...}}
    detail_map = {}     # {onu_index: {"distance": ..., "online_duration": ...}}
    try:
        conn = client._connect()
        try:
            # 3 command dasar
            running_cfg = conn.send_command_timing(
                "show running-config", read_timeout=60, last_read=LAST_READ_LONG)
            onu_state_out = conn.send_command_timing(
                "show gpon onu state", read_timeout=30, last_read=LAST_READ_SHORT)
            uncfg_out = conn.send_command_timing(
                "show gpon onu uncfg", read_timeout=30, last_read=LAST_READ_SHORT)

            # Ambil optical + pppoe status untuk setiap ONU
            import re as _re2

            # ⚡ SNMP walk dulu (1-2 detik untuk 100+ ONU) — optical + distance
            snmp_data = {}
            if olt.snmp_community_ro:
                try:
                    from olt_snmp import OltSnmpClient
                    async def _do_snmp():
                        c = OltSnmpClient(olt.ip_address, olt.snmp_community_ro,
                                          port=olt.snmp_port or 161)
                        return await c.list_onus()
                    loop = asyncio.new_event_loop()
                    try:
                        snmp_onus = loop.run_until_complete(_do_snmp())
                    finally:
                        loop.close()
                    for so in snmp_onus:
                        pon_num = 1 + (so.pon_idx - 268501248) // 256
                        snmp_data[f"1/1/{pon_num}:{so.onu_id}"] = so
                    print(f"[SNMP] {len(snmp_data)} ONU via SNMP")
                except Exception as e:
                    print(f"[SNMP] gagal: {e} — fallback Telnet untuk optical")

            # ⚡ Pre-parse state untuk tahu mana ONU online (skip PPPoE kalau offline)
            _onu_state_pre = _parse_onu_state(onu_state_out)
            online_idxs = set()
            for _o in _onu_state_pre:
                if (_o.get("phase_state") or "").lower().strip() == "working":
                    online_idxs.add(_o["onu_index"])
            print(f"[SYNC] {len(online_idxs)} ONU online dari {len(_onu_state_pre)} total")

            for m in _re2.finditer(r"^\s*(\d+/\d+/\d+:\d+)\s+enable", onu_state_out, _re2.MULTILINE):
                idx = m.group(1)
                so = snmp_data.get(idx)

                # Optical: SNMP dulu, fallback Telnet
                if so is not None and so.rx_dbm is not None:
                    optical_map[idx] = {"onu_rx": so.rx_dbm, "onu_tx": so.tx_dbm}
                else:
                    try:
                        att_cmd = f"show pon power attenuation gpon-onu_{idx}"
                        att_out = conn.send_command_timing(att_cmd, read_timeout=15, last_read=LAST_READ_SHORT)
                        optical_map[idx] = _parse_attenuation(att_out)
                    except Exception as e:
                        print(f"[OPTICAL] {idx} error: {e}")
                # PPPoE status — skip kalau ONU tidak online
                if idx not in online_idxs:
                    pppoe_map[idx] = {
                        "status": "disconnected",
                        "online_duration": 0,
                        "username": None,
                        "nat": None,
                    }
                else:
                    try:
                        pppoe_cmd = f"show gpon remote-onu pppoe gpon-onu_{idx}"
                        pppoe_out = conn.send_command_timing(pppoe_cmd, read_timeout=15, last_read=LAST_READ_SHORT)
                        if "Error" not in pppoe_out and "Invalid" not in pppoe_out:
                            pppoe_map[idx] = _parse_remote_pppoe(pppoe_out)
                        else:
                            print(f"[PPPOE] {idx} tidak support remote-onu pppoe")
                    except Exception as e:
                        print(f"[PPPOE] {idx} error: {e}")
                # Detail: distance — SNMP dulu, fallback Telnet
                if so is not None and so.distance_m is not None:
                    detail_map[idx] = {"distance": so.distance_m, "online_duration": None}
                else:
                    try:
                        det_cmd = f"show gpon onu detail-info gpon-onu_{idx}"
                        det_out = conn.send_command_timing(det_cmd, read_timeout=30, last_read=LAST_READ_LONG)
                        detail_map[idx] = _parse_onu_detail(det_out)
                    except Exception as e:
                        print(f"[DETAIL] {idx} error: {e}")
        finally:
            conn.disconnect()
    except Exception as e:
        raise RuntimeError(f"Gagal sync PON: {e}") from e

    # Parse admin state dari running-config
    cfg_ports = _parse_gpon_ports_from_config(running_cfg)
    port_infos = {}
    for i in range(1, 17):
        port_no = f"1/1/{i}"
        admin = cfg_ports.get(port_no, {}).get("admin_state", "shutdown")
        port_infos[port_no] = {
            "status": "up" if admin == "no shutdown" else "down",
            "admin_state": admin,
        }

    # Parse hasil
    onus = _parse_onu_state(onu_state_out)
    uncfg = _parse_onu_uncfg(uncfg_out)
    cfg_onus = _parse_onu_config_from_running(running_cfg)   # ⭐ data lengkap dari running-config

    # Hitung ONU per port
    onu_count_per_port = {}
    for o in onus:
        onu_count_per_port[o["pon_port"]] = onu_count_per_port.get(o["pon_port"], 0) + 1

    # Update / buat PONPort rows
    for i in range(1, 17):
        port_no = f"1/1/{i}"
        info = port_infos.get(port_no, {})
        pon = db.query(PONPort).filter(
            PONPort.olt_id == olt_id, PONPort.port_no == port_no
        ).first()
        if not pon:
            pon = PONPort(olt_id=olt_id, port_no=port_no)
            db.add(pon)
        pon.status = info.get("status", "down")
        pon.admin_state = "no shutdown" if info.get("status") == "up" else "shutdown"
        pon.onu_count = onu_count_per_port.get(port_no, 0)
        pon.last_updated = datetime.utcnow()

    # Update / buat ONU rows + ambil optical untuk tiap ONU
    onu_by_key = {}
    for o in onus:
        key = (o["pon_port"], o["onu_id"])
        onu = db.query(ONU).filter(
            ONU.olt_id == olt_id,
            ONU.pon_port == o["pon_port"],
            ONU.onu_id == o["onu_id"],
        ).first()
        # Tentukan status berdasarkan phase_state dari OLT
        phase = (o.get("phase_state") or "").lower().strip()
        if phase == "working":
            new_status = "online"
        elif phase == "los":
            new_status = "los"
        elif phase == "dying-gasp":
            new_status = "dying_gasp"
        elif phase == "offline":
            new_status = "offline"
        elif phase in ("configuring", "initial"):
            new_status = "configuring"
        else:
            new_status = "unknown"

        if not onu:
            # ⭐ Sync boleh INSERT ONU baru (kalau tidak ada job provisioning aktif)
            # Cek dulu ada job aktif untuk slot ini?
            job_key = f"onu:{o['pon_port']}:{o['onu_id']}"
            has_active_job = False
            try:
                for jid, job in list(olt_manager.jobs.items()):
                    if job["status"] in ("queued", "running") and job.get("olt_id") == str(olt_id):
                        # Cek apakah job ini untuk slot yang sama
                        has_active_job = True
                        break
            except Exception:
                pass

            if has_active_job:
                print(f"[SYNC] Skip insert ONU {o['pon_port']}:{o['onu_id']} — ada job aktif")
                continue

            print(f"[SYNC] Insert ONU baru: {o['pon_port']}:{o['onu_id']} (dari OLT)")
            onu = ONU(
                olt_id=olt_id,
                pon_port=o["pon_port"],
                onu_id=o["onu_id"],
                interface_name=f"gpon-onu_{o['onu_index']}",
                status=new_status,
                updated_at=datetime.utcnow(),
            )
            db.add(onu)
            db.flush()

        # ONU sudah ada di DB — update status & data
        if onu.status != new_status:
            # ⭐ Catat event sebelum ubah status
            from event_log import log_onu_event
            log_onu_event(db, olt_id, onu, "status_change",
                          old_value=onu.status, new_value=new_status)
            onu.status = new_status
            # ⭐ Kalau offline/LOS/dying_gasp → reset PPPoE juga
            if new_status in ("offline", "los", "dying_gasp"):
                onu.pppoe_status = "disconnected"
                onu.pppoe_online_duration = 0
            # Catat waktu dying_gasp untuk suppression alert offline
            if new_status == "dying_gasp":
                onu.last_dying_gasp = datetime.utcnow()
        onu.updated_at = datetime.utcnow()

        # Pakai optical yang sudah diambil di koneksi utama
        optical = optical_map.get(o["onu_index"])
        if optical:
            if optical["onu_rx"] is not None:
                onu.optical_rx = optical["onu_rx"]
            if optical["onu_tx"] is not None:
                onu.optical_tx = optical["onu_tx"]

        # Enrich dari running-config (SN, name, VLAN, service-port, dll)
        cfg = cfg_onus.get(o["onu_index"])
        if cfg:
            if cfg.get("sn"):
                onu.serial_number = cfg["sn"]
            if cfg.get("type"):
                onu.type = cfg["type"]
            if cfg.get("name"):
                onu.name = cfg["name"]
            if cfg.get("tcont"):
                onu.tcont = cfg["tcont"]
            if cfg.get("gemport") is not None:
                onu.gemport = cfg["gemport"]
            if cfg.get("service_port") is not None:
                onu.service_port = cfg["service_port"]
            if cfg.get("user_vlan") is not None:
                onu.user_vlan = cfg["user_vlan"]
            if cfg.get("vlan") is not None:
                onu.vlan = cfg["vlan"]
            if cfg.get("pppoe_user"):
                onu.pppoe_user = cfg["pppoe_user"]
            if cfg.get("pppoe_nat") is not None:
                onu.pppoe_nat = cfg["pppoe_nat"]

        # Simpan distance dari detail-info
        det = detail_map.get(o["onu_index"])
        if det and det["distance"] is not None:
            onu.distance = det["distance"]

        # Simpan status PPPoE / internet (prioritas: remote-onu pppoe — realtime)
        pppoe = pppoe_map.get(o["onu_index"])
        if pppoe:
            onu.pppoe_status = pppoe["status"]
            onu.pppoe_online_duration = pppoe["online_duration"]
            if pppoe["username"]:
                onu.pppoe_user = pppoe["username"]
            if pppoe["nat"] == "enable":
                onu.pppoe_nat = True
        else:
            onu.pppoe_status = "unknown"
        onu.internet_checked_at = datetime.utcnow()

        # 🔔 AUTO-ALERT per-ONU (offline, pppoe down, optical low)
        try:
            check_onu_alerts(db, olt_id, onu)
        except Exception as e:
            print(f"[ALERT ONU] {onu.serial_number or onu.onu_id}: {e}")

        onu_by_key[key] = onu

    # === Ghost cleanup: ONU di DB yang TIDAK ada di OLT → HAPUS ===
    active_keys = set()
    for o in onus:
        active_keys.add((o["pon_port"], o["onu_id"]))

    all_db_onus = db.query(ONU).filter(ONU.olt_id == olt_id).all()
    for db_onu in all_db_onus:
        db_key = (db_onu.pon_port, db_onu.onu_id)
        if db_key not in active_keys:
            # Cek apakah ada job provisioning aktif untuk slot ini
            has_job = False
            try:
                for jid, job in list(olt_manager.jobs.items()):
                    if job["status"] in ("queued", "running"):
                        has_job = True
                        break
            except Exception:
                pass

            if has_job:
                # Sedang provisioning — jangan hapus, tunggu selesai
                db_onu.status = "configuring"
                continue

            # ONU ghost → hapus dari DB
            print(f"[SYNC] Hapus ghost ONU: {db_onu.pon_port}:{db_onu.onu_id} (tidak ada di OLT)")
            db.delete(db_onu)

    # === Fix 2: ONU di DB yang TIDAK muncul di OLT saat ini → mark offline ===
    # (ONU hilang dari show gpon onu state = tidak terdaftar / mati total)
    active_keys = set(onu_by_key.keys())   # key (pon_port, onu_id) yang aktif di OLT
    all_db_onus = db.query(ONU).filter(ONU.olt_id == olt_id).all()
    for db_onu in all_db_onus:
        db_key = (db_onu.pon_port, db_onu.onu_id)
        if db_key not in active_keys:
            # ONU ini tidak muncul di OLT — mark offline
            if db_onu.status != "offline":
                print(f"[SYNC] ONU {db_onu.pon_port}:{db_onu.onu_id} tidak muncul di OLT → mark offline")
                db_onu.status = "offline"
                db_onu.updated_at = datetime.utcnow()
                # Trigger alert
                try:
                    check_onu_alerts(db, olt_id, db_onu)
                except Exception as e:
                    print(f"[ALERT ONU] {db_onu.serial_number or db_onu.onu_id}: {e}")

    olt.last_polled = datetime.utcnow()
    olt.status = "online"

    # 🔔 AUTO-ALERT resource OLT (CPU, memory)
    try:
        check_olt_resource_alerts(db, olt)
    except Exception as e:
        print(f"[ALERT OLT] {e}")

    db.commit()

    return {
        "ok": True,
        "pons": [
            {
                "port_no": f"1/1/{i}",
                "status": port_infos.get(f"1/1/{i}", {}).get("status", "down"),
                "onu_count": onu_count_per_port.get(f"1/1/{i}", 0),
            }
            for i in range(1, 17)
        ],
        "onus": onus,
        "unconfigured": uncfg,
        "summary": {
            "total_onu_aktif": len(onus),
            "total_onu_uncfg": len(uncfg),
            "pons_up": sum(1 for i in range(1, 17)
                           if port_infos.get(f"1/1/{i}", {}).get("status") == "up"),
        },
    }

@router.post("/{olt_id}/sync-pons")
@olt_locked
def sync_pons(db: Session = Depends(get_db),
              olt: OLT = Depends(require_olt_access),
              user: User = Depends(get_current_user)):
    """Sync status 16 PON port + list ONU dari OLT (endpoint manual)."""
    try:
        result = _do_sync_pons(db, olt)
        audit(db, user.username, "sync_pons", str(olt.id))
        return result
    except HTTPException:
        raise
    except Exception as e:
        audit(db, user.username, "sync_pons", str(olt.id), str(e), "failed")
        raise HTTPException(500, f"Gagal sync PON: {e}")
