"""Endpoint ONU lintas-OLT untuk Super Admin (view global)."""
from fastapi import APIRouter, Depends, HTTPException
from sqlalchemy.orm import Session
from typing import List, Optional
from database import get_db
from models import ONU, OLT
from schemas import ONUWithOLTOut
from tenancy import OwnerContext, get_owner_ctx, scoped_olts

router = APIRouter(prefix="/api/v1", tags=["onus-global"])


@router.get("/onus", response_model=List[ONUWithOLTOut])
def list_onus_global(
    olt_id: Optional[int] = None,
    status: Optional[str] = None,
    q: Optional[str] = None,
    limit: int = 1000,
    db: Session = Depends(get_db),
    ctx: OwnerContext = Depends(get_owner_ctx),
):
    """ONU lintas OLT.

    - Super Admin: tanpa olt_id -> semua OLT dalam scope
    - Admin biasa: cuma OLT yang user punya akses
    - Kalau olt_id dikirim & tidak dalam scope -> 403
    """
    visible_ids = [o.id for o in scoped_olts(db, ctx).all()]
    if olt_id is not None:
        if olt_id not in visible_ids:
            raise HTTPException(403, "Anda tidak punya akses ke OLT ini")
        target_ids = [olt_id]
    else:
        target_ids = visible_ids

    if not target_ids:
        return []

    query = (
        db.query(ONU, OLT.hostname)
        .join(OLT, ONU.olt_id == OLT.id)
        .filter(ONU.olt_id.in_(target_ids))
    )
    if status:
        query = query.filter(ONU.status == status)
    if q:
        like = f"%{q}%"
        query = query.filter(
            (ONU.serial_number.ilike(like)) | (ONU.name.ilike(like))
        )
    rows = query.limit(limit).all()

    result = []
    for onu, olt_hostname in rows:
        d = {k: v for k, v in onu.__dict__.items() if not k.startswith("_")}
        d["olt_id"] = onu.olt_id
        d["olt_hostname"] = olt_hostname
        result.append(d)
    return result
