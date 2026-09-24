from fastapi import APIRouter, Depends, HTTPException
from sqlalchemy.orm import Session
from typing import List
from database import get_db
from models import OLT, User
from schemas import OLTCreate, OLTOut
from auth import get_current_user, require_privilege, audit
from olt_manager import olt_manager

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


@router.delete("/{olt_id}")
def delete_olt(olt_id: int, db: Session = Depends(get_db),
               user: User = Depends(require_privilege(15))):
    olt = db.query(OLT).get(olt_id)
    if not olt:
        raise HTTPException(404, "OLT tidak ditemukan")
    db.delete(olt)
    db.commit()
    audit(db, user.username, "delete_olt", str(olt_id))
    return {"ok": True}