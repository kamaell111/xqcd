from fastapi import APIRouter, Depends, HTTPException
from sqlalchemy.orm import Session
from typing import List
from database import get_db
from models import OLT, User
from schemas import OLTCreate, OLTOut
from auth import get_current_user, require_privilege, audit

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