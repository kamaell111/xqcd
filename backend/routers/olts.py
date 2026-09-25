from fastapi import APIRouter, Depends, HTTPException
from sqlalchemy.orm import Session
from typing import List
from database import get_db
from models import OLT, User
from schemas import OLTCreate, OLTOut, OLTUpdate, OLTTestRequest
from auth import get_current_user, require_privilege, audit
from olt_manager import olt_manager
from drivers import list_drivers, is_valid as is_driver_valid

router = APIRouter(prefix="/api/v1/olts", tags=["olts"])


@router.get("", response_model=List[OLTOut])
def list_olts(db: Session = Depends(get_db), user: User = Depends(get_current_user)):
    return db.query(OLT).all()


@router.post("", response_model=OLTOut)
def create_olt(req: OLTCreate, db: Session = Depends(get_db),
               user: User = Depends(require_privilege(10))):
    if db.query(OLT).filter(OLT.ip_address == req.ip_address).first():
        raise HTTPException(400, "OLT dengan IP tersebut sudah ada")
    olt = OLT(**req.model_dump())
    db.add(olt)
    db.commit()
    db.refresh(olt)
    audit(db, user.username, "create_olt", req.ip_address)
    return olt


# =================== MULTI-VENDOR (1a-1) ===================
# Route statis harus di atas /{olt_id} biar FastAPI tidak parse "drivers" sebagai int.

@router.get("/drivers")
def get_drivers(user: User = Depends(get_current_user)):
    """Daftar driver + hardware types untuk UI dropdown."""
    return list_drivers()


@router.post("/test-connection")
def test_olt_connection(req: OLTTestRequest,
                        user: User = Depends(require_privilege(15))):
    """Tes koneksi Telnet ke OLT tanpa simpan ke DB."""
    from olt_client import OLTClient
    import re
    try:
        client = OLTClient(
            host=req.ip_address,
            username=req.username,
            password=req.password,
            enable_password=req.enable_password or "",
            port=req.port,
            protocol=req.protocol or "telnet",
        )
        result = client.test_connection()
        if result.get("ok") and result.get("output"):
            out = result["output"]
            m = re.search(r"System name:\s*(\S+)", out) or re.search(r"^\s*hostname\s+(\S+)", out, re.MULTILINE)
            result["hostname"] = m.group(1) if m else ""
            m = re.search(r"(ZXA10\s+)?C\d{3}", out)
            result["model"] = m.group(0).strip() if m else ""
            m = re.search(r"V\d+\.\d+\.\d+", out)
            result["firmware"] = m.group(0) if m else ""
        return result
    except Exception as e:
        return {"ok": False, "error": f"{type(e).__name__}: {e}"}


@router.get("/{olt_id}", response_model=OLTOut)
def get_olt(olt_id: int, db: Session = Depends(get_db), user: User = Depends(get_current_user)):
    olt = db.query(OLT).get(olt_id)
    if not olt:
        raise HTTPException(404, "OLT tidak ditemukan")
    return olt


@router.post("/{olt_id}/config/commit")
def commit_config(olt_id: int, db: Session = Depends(get_db),
                  user: User = Depends(require_privilege(15))):
    """Commit running-config ke flash (write). Dipakai untuk two-phase commit."""
    from olt_client import OLTClient
    from olt_manager import olt_manager

    olt = db.query(OLT).get(olt_id)
    if not olt:
        raise HTTPException(404, "OLT tidak ditemukan")

    lock = olt_manager.get_thread_lock(str(olt_id))
    if not lock.acquire(timeout=30):
        raise HTTPException(409, "OLT sedang sibuk — coba beberapa detik lagi")
    try:
        client = OLTClient(
            host=olt.ip_address, username=olt.username,
            password=olt.password, enable_password=olt.enable_password,
            port=olt.port, protocol=olt.protocol,
        )
        try:
            out = client.commit_config()
        except Exception as e:
            audit(db, user.username, "commit_config", str(olt_id), str(e), "failed")
            raise HTTPException(500, f"Gagal commit: {e}")

        olt_manager.mark_committed(str(olt_id))
        audit(db, user.username, "commit_config", str(olt_id))
        return {
            "ok": True,
            "output": out[:500] if out else "",
        }
    finally:
        lock.release()


@router.get("/{olt_id}/config/pending")
def get_config_pending(olt_id: int, db: Session = Depends(get_db),
                       user: User = Depends(get_current_user)):
    """Status pending config (belum di-commit)."""
    from olt_manager import olt_manager
    olt = db.query(OLT).get(olt_id)
    if not olt:
        raise HTTPException(404, "OLT tidak ditemukan")
    return olt_manager.get_pending_state(str(olt_id))


@router.get("/{olt_id}/circuit")
def get_circuit_state(olt_id: int, db: Session = Depends(get_db),
                      user: User = Depends(get_current_user)):
    """Status circuit breaker OLT — untuk indikator di UI."""
    olt = db.query(OLT).get(olt_id)
    if not olt:
        raise HTTPException(404, "OLT tidak ditemukan")
    state = olt_manager.get_olt_circuit_state(str(olt_id))
    state["olt_id"] = olt_id
    state["ip_address"] = olt.ip_address
    return state


@router.patch("/{olt_id}", response_model=OLTOut)
def update_olt(olt_id: int, req: OLTUpdate,
               db: Session = Depends(get_db),
               user: User = Depends(require_privilege(15))):
    """Update OLT. Field None = tidak diubah. Password kosong = tidak ubah."""
    olt = db.query(OLT).get(olt_id)
    if not olt:
        raise HTTPException(404, "OLT tidak ditemukan")

    data = req.model_dump(exclude_unset=True)

    new_driver = data.get("driver", olt.driver)
    new_hw = data.get("hardware_type", olt.hardware_type)
    if not is_driver_valid(new_driver, new_hw):
        raise HTTPException(400, f"Driver/hardware tidak valid: {new_driver}/{new_hw}")

    new_ip = data.get("ip_address", olt.ip_address)
    new_port = data.get("port", olt.port)
    dup = db.query(OLT).filter(
        OLT.ip_address == new_ip, OLT.port == new_port, OLT.id != olt_id
    ).first()
    if dup:
        raise HTTPException(400, f"OLT dengan {new_ip}:{new_port} sudah ada")

    if "password" in data and data["password"] == "":
        data.pop("password")
    if "enable_password" in data and data["enable_password"] == "":
        data.pop("enable_password")

    cred_keys = {"ip_address", "port", "username", "password", "enable_password", "protocol"}
    should_evict = bool(cred_keys & set(data.keys()))

    for k, v in data.items():
        setattr(olt, k, v)

    db.commit()
    db.refresh(olt)

    if should_evict:
        from olt_client import evict_connection
        evicted = evict_connection(olt.ip_address, olt.port)
        print(f"[OLT-UPDATE] id={olt_id} evicted {evicted} connection(s)")

    safe_changes = {k: v for k, v in data.items()
                    if k not in ("password", "enable_password")}
    audit(db, user.username, "update_olt", f"{olt_id} {safe_changes}")
    return olt


@router.delete("/{olt_id}")
def delete_olt(olt_id: int, db: Session = Depends(get_db),
               user: User = Depends(require_privilege(15))):
    olt = db.query(OLT).get(olt_id)
    if not olt:
        raise HTTPException(404, "OLT tidak ditemukan")

    # Bersihkan cache koneksi Netmiko + state in-memory sebelum hapus
    from olt_client import evict_connection
    evicted = evict_connection(olt.ip_address, olt.port)

    # Fix #5: bersihkan lock/circuit/pending/jobs terminal dari olt_manager
    cleared = olt_manager.clear_olt_state(olt_id)

    db.delete(olt)
    db.commit()
    audit(db, user.username, "delete_olt", str(olt_id))
    return {"ok": True, "evicted_connections": evicted, "cleared_state": cleared}