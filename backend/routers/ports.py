"""Endpoint toggle port + edit port VLAN — async job pattern."""
import time
import re
import os
import datetime
from fastapi import APIRouter, Depends, HTTPException, BackgroundTasks
from sqlalchemy.orm import Session

from database import get_db, SessionLocal
from models import OLT, User, PONPort, Interface
from schemas import PortToggleRequest, PortVLANEditRequest
from auth import get_current_user, require_privilege, audit
from olt_client import OLTClient
from olt_manager import olt_manager
from tenancy import require_olt_access
from config import settings

router = APIRouter(prefix="/api/v1/olts", tags=["ports"])


def _make_client(olt: OLT) -> OLTClient:
    return OLTClient(
        host=olt.ip_address,
        username=olt.username,
        password=olt.password,
        enable_password=olt.enable_password,
        port=olt.port,
        protocol=olt.protocol,
    )


# =================== HELPER ===================
def _parse_vlan_spec(spec: str) -> set:
    ids = set()
    for part in (spec or "").split(","):
        part = part.strip()
        if not part:
            continue
        if "-" in part:
            try:
                a, b = part.split("-")
                ids.update(range(int(a), int(b) + 1))
            except ValueError:
                pass
        elif part.isdigit():
            ids.add(int(part))
    return ids


def _vlan_spec_to_command(vlans: set) -> str:
    if not vlans:
        return ""
    sorted_v = sorted(vlans)
    ranges = []
    start = sorted_v[0]
    prev = sorted_v[0]
    for v in sorted_v[1:]:
        if v == prev + 1:
            prev = v
            continue
        if start == prev:
            ranges.append(str(start))
        else:
            ranges.append(f"{start}-{prev}")
        start = v
        prev = v
    if start == prev:
        ranges.append(str(start))
    else:
        ranges.append(f"{start}-{prev}")
    return ",".join(ranges)


# =================== TOGGLE PORT ===================
def _do_toggle_port(olt_id: int, req_dict: dict, db: Session, username: str,
                     job_id: str = None) -> dict:
    req = PortToggleRequest(**req_dict)

    def _p(msg, prog=None):
        if job_id:
            upd = {"message": msg}
            if prog is not None: upd["progress"] = prog
            olt_manager.update_job(job_id, **upd)
        print(f"[TOGGLE] {msg}")

    _p("Cek OLT...", 10)
    olt = db.query(OLT).get(olt_id)
    if not olt:
        raise RuntimeError("OLT tidak ditemukan")
    if req.port_type not in ("gpon", "uplink"):
        raise RuntimeError("port_type harus gpon/uplink")
    if req.action not in ("enable", "disable"):
        raise RuntimeError("action harus enable/disable")

    iface_name = f"gpon-olt_{req.port_name}" if req.port_type == "gpon" else req.port_name
    port_cmd = "no shutdown" if req.action == "enable" else "shutdown"

    commands = [
        "configure terminal",
        f"interface {iface_name}",
        port_cmd,
        "exit", "end",
    ]

    client = _make_client(olt)
    _p("Kirim ke OLT...", 40)
    # ⭐ Enable = additive → auto-write. Disable = destructive → tunggu commit.
    is_enable = (req.action == "enable")
    try:
        out = client.run_config(commands, save=is_enable)
        if not is_enable:
            olt_manager.mark_pending(str(olt_id), f"disable port {iface_name}")
    except Exception as e:
        raise RuntimeError(f"Gagal kirim command: {e}")

    _p("Verifikasi...", 70)
    time.sleep(3)
    actual_state = None
    try:
        conn = client._connect()
        try:
            rc = conn.send_command_timing("show running-config", read_timeout=60)
            pattern = rf"interface {re.escape(iface_name)}\b(.*?)(?=^!|\Z)"
            m = re.search(pattern, rc, re.DOTALL | re.MULTILINE)
            if m:
                block = m.group(1)
                if "no shutdown" in block:
                    actual_state = "up"
                elif "shutdown" in block:
                    actual_state = "down"
        finally:
            conn.disconnect()
    except Exception as e:
        print(f"[TOGGLE] verify error: {e}")

    if req.port_type == "gpon":
        pon = db.query(PONPort).filter(PONPort.olt_id == olt_id, PONPort.port_no == req.port_name).first()
        if pon:
            pon.admin_state = "no shutdown" if req.action == "enable" else "shutdown"
            pon.status = "up" if req.action == "enable" else "down"
    else:
        iface = db.query(Interface).filter(Interface.olt_id == olt_id, Interface.name == iface_name).first()
        if iface:
            iface.admin_state = "no shutdown" if req.action == "enable" else "shutdown"
            iface.status = "up" if req.action == "enable" else "down"
    db.commit()
    audit(db, username, "toggle_port", iface_name, f"action={req.action} · verified={actual_state}")
    _p("Selesai", 100)
    return {"ok": True, "interface": iface_name, "action": req.action, "actual_state": actual_state}


@router.post("/{olt_id}/ports/toggle")
def toggle_port(req: PortToggleRequest,
                background_tasks: BackgroundTasks,
                db: Session = Depends(get_db),
                olt: OLT = Depends(require_olt_access),
                user: User = Depends(require_privilege(10))):
    resource_key = f"toggle_port:{req.port_type}:{req.port_name}"
    job_id, is_new = olt_manager.create_job_atomic(
        olt_id=str(olt.id), job_type="toggle_port", resource_key=resource_key,
    )
    if not is_new:
        return {"job_id": job_id, "status": "queued", "duplicate": True}

    background_tasks.add_task(
        _run_toggle_job, job_id=job_id, olt_id=olt.id,
        req_dict=req.model_dump() if hasattr(req, "model_dump") else req.dict(),
        username=user.username,
    )
    return {"job_id": job_id, "status": "queued", "message": "Job toggle port dimulai"}


def _run_toggle_job(job_id: str, olt_id: int, req_dict: dict, username: str):
    olt_manager.update_job(job_id, status="running", progress=5, message="Mulai...")
    db = SessionLocal()
    try:
        result = _do_toggle_port(olt_id, req_dict, db, username, job_id=job_id)
        olt_manager.finish_job(job_id, "success", result=result)
    except Exception as e:
        import traceback; traceback.print_exc()
        olt_manager.finish_job(job_id, "failed", error=str(e)[:300])
    finally:
        db.close()


# =================== EDIT PORT VLAN ===================
def _do_edit_port_vlan(olt_id: int, req_dict: dict, db: Session, username: str,
                        job_id: str = None) -> dict:
    req = PortVLANEditRequest(**req_dict)

    def _p(msg, prog=None):
        if job_id:
            upd = {"message": msg}
            if prog is not None: upd["progress"] = prog
            olt_manager.update_job(job_id, **upd)
        print(f"[EDIT-PORT] {msg}")

    _p("Validasi...", 10)
    olt = db.query(OLT).get(olt_id)
    if not olt:
        raise RuntimeError("OLT tidak ditemukan")
    if req.mode not in ("access", "trunk", "hybrid"):
        raise RuntimeError("Mode harus access/trunk/hybrid")
    if not (1 <= req.native_vlan <= 4094):
        raise RuntimeError("Native VLAN 1-4094")

    tag_set = _parse_vlan_spec(req.tag_vlans)
    if not tag_set:
        raise RuntimeError("Tag VLAN wajib diisi")

    mgmt = int(getattr(settings, "MANAGEMENT_VLAN", 0) or 0)
    if mgmt > 0 and mgmt not in tag_set:
        raise RuntimeError(f"VLAN {mgmt} (manajemen) tidak ada di tag list")

    client = _make_client(olt)
    _p("Backup running-config...", 20)
    backup_dir = "./data/config_backups"
    os.makedirs(backup_dir, exist_ok=True)
    stamp = datetime.datetime.utcnow().strftime("%Y%m%d_%H%M%S")
    backup_file = f"{backup_dir}/{req.port_name.replace('/', '_')}_{stamp}.cfg"
    try:
        rc = client.get_running_config()
        with open(backup_file, "w") as f:
            f.write(rc)
    except Exception as e:
        print(f"[EDIT-PORT] backup warning: {e}")

    tag_cmd = _vlan_spec_to_command(tag_set)
    commands = ["configure terminal", f"interface {req.port_name}", f"switchport mode {req.mode}"]
    if req.mode in ("access", "hybrid"):
        commands.append(f"switchport default vlan {req.native_vlan}")
    if req.mode in ("trunk", "hybrid") and tag_cmd:
        commands.append(f"switchport vlan {tag_cmd} tag")
    commands += ["exit", "end"]

    _p("Kirim ke OLT...", 40)
    try:
        client.run_config(commands, save=False)
        olt_manager.mark_pending(str(olt_id), f"edit port {req.port_name}")
    except Exception as e:
        raise RuntimeError(f"Gagal kirim command: {e}")

    _p("Verifikasi OLT...", 70)
    time.sleep(3)
    still_alive = False
    try:
        conn = client._connect()
        try:
            probe = conn.send_command_timing("show clock", read_timeout=10, last_read=0.3)
            still_alive = bool(probe and "Error" not in probe)
        finally:
            conn.disconnect()
    except Exception as e:
        print(f"[EDIT-PORT] verify error: {e}")

    if not still_alive:
        raise RuntimeError(f"OLT lost setelah edit. Backup: {backup_file}")

    iface = db.query(Interface).filter(Interface.olt_id == olt_id, Interface.name == req.port_name).first()
    if iface:
        iface.switchport_mode = req.mode
        iface.vlans = tag_cmd
        db.commit()

    audit(db, username, "edit_port_vlan", req.port_name,
          f"mode={req.mode} native={req.native_vlan} tag={tag_cmd}")
    _p("Selesai", 100)
    return {"ok": True, "port": req.port_name, "mode": req.mode,
            "tag_vlans": tag_cmd, "backup_file": backup_file}


@router.post("/{olt_id}/ports/vlan")
def edit_port_vlan(req: PortVLANEditRequest,
                   background_tasks: BackgroundTasks,
                   db: Session = Depends(get_db),
                   olt: OLT = Depends(require_olt_access),
                   user: User = Depends(require_privilege(15))):
    resource_key = f"edit_port:{req.port_name}"
    job_id, is_new = olt_manager.create_job_atomic(
        olt_id=str(olt.id), job_type="edit_port_vlan", resource_key=resource_key,
    )
    if not is_new:
        return {"job_id": job_id, "status": "queued", "duplicate": True}

    background_tasks.add_task(
        _run_edit_port_job, job_id=job_id, olt_id=olt.id,
        req_dict=req.model_dump() if hasattr(req, "model_dump") else req.dict(),
        username=user.username,
    )
    return {"job_id": job_id, "status": "queued", "message": "Job edit port dimulai"}


def _run_edit_port_job(job_id: str, olt_id: int, req_dict: dict, username: str):
    olt_manager.update_job(job_id, status="running", progress=5, message="Mulai...")
    db = SessionLocal()
    try:
        result = _do_edit_port_vlan(olt_id, req_dict, db, username, job_id=job_id)
        olt_manager.finish_job(job_id, "success", result=result)
    except Exception as e:
        import traceback; traceback.print_exc()
        olt_manager.finish_job(job_id, "failed", error=str(e)[:300])
    finally:
        db.close()
