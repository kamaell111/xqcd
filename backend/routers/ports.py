"""Endpoint enable/disable port OLT (PON & Uplink)."""
import time
import re
from fastapi import APIRouter, Depends, HTTPException
from sqlalchemy.orm import Session

from database import get_db
from models import OLT, User, PONPort, Interface
from schemas import PortToggleRequest
from auth import get_current_user, require_privilege, audit
from olt_client import OLTClient
from olt_manager import olt_locked

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


@router.post("/{olt_id}/ports/toggle")
@olt_locked
def toggle_port(olt_id: int, req: PortToggleRequest,
                db: Session = Depends(get_db),
                user: User = Depends(require_privilege(10))):
    """
    Enable / disable port OLT.
    - port_type: "gpon" (PON port) atau "uplink" (GEI/XGEI)
    - port_name: "1/1/1" untuk GPON, "gei_1/3/1" untuk uplink
    - action: "enable" (no shutdown) atau "disable" (shutdown)
    """
    olt = db.query(OLT).get(olt_id)
    if not olt:
        raise HTTPException(404, "OLT tidak ditemukan")

    # Validasi input
    if req.port_type not in ("gpon", "uplink"):
        raise HTTPException(400, "port_type harus 'gpon' atau 'uplink'")
    if req.action not in ("enable", "disable"):
        raise HTTPException(400, "action harus 'enable' atau 'disable'")

    # Tentukan nama interface di OLT
    if req.port_type == "gpon":
        iface_name = f"gpon-olt_{req.port_name}"
    else:
        iface_name = req.port_name   # "gei_1/3/1" sudah lengkap

    # Command
    if req.action == "enable":
        port_cmd = "no shutdown"
    else:
        port_cmd = "shutdown"

    commands = [
        "configure terminal",
        f"interface {iface_name}",
        port_cmd,
        "exit",
        "end",
        "write",
    ]

    client = _make_client(olt)
    log = []

    # 1. Kirim command
    try:
        out = client.run_config(commands)
        log.append(f"[CMD] {out[:500]}")
    except Exception as e:
        audit(db, user.username, "toggle_port", iface_name, str(e), "failed")
        raise HTTPException(500, f"Gagal kirim command: {e}")

    # 2. Tunggu OLT commit
    time.sleep(3)

    # 3. Verifikasi: cek state interface
    actual_state = None
    try:
        conn = client._connect()
        try:
            rc = conn.send_command_timing("show running-config", read_timeout=60)
            # Cari blok interface
            pattern = rf"interface {re.escape(iface_name)}\\b(.*?)(?=^!|\\Z)"
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
        log.append(f"[VERIFY ERROR] {e}")

    # 4. Update DB juga biar UI langsung sinkron
    if req.port_type == "gpon":
        # cari PONPort
        pon = db.query(PONPort).filter(
            PONPort.olt_id == olt_id, PONPort.port_no == req.port_name
        ).first()
        if pon:
            pon.admin_state = "no shutdown" if req.action == "enable" else "shutdown"
            pon.status = "up" if req.action == "enable" else "down"
    else:
        iface = db.query(Interface).filter(
            Interface.olt_id == olt_id, Interface.name == iface_name
        ).first()
        if iface:
            iface.admin_state = "no shutdown" if req.action == "enable" else "shutdown"
            iface.status = "up" if req.action == "enable" else "down"
    db.commit()

    audit(db, user.username, "toggle_port", iface_name,
          f"action={req.action} · verified={actual_state}")

    return {
        "ok": True,
        "interface": iface_name,
        "action": req.action,
        "actual_state": actual_state,   # "up" / "down" / None
        "log": log,
    }
