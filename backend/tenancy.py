"""Ownership context untuk multi-admin OLT management.

Phase B1: fondasi enforcement.

Aturan scope (3 baris):
- Multivers (is_super_admin=1) -> None   (lihat semua OLT)
- Admin (privilege >= 15)      -> user.id (OLT miliknya sendiri)
- Operator/Viewer/FieldTech    -> user.owner_user_id (admin atasannya)

Helper WAJIB dipakai di semua endpoint yang menyentuh OLT:
- scoped_olts(db, ctx)          -> Query untuk list endpoint
- get_olt_or_403(db, id, ctx)   -> untuk detail + nested endpoint
"""
from dataclasses import dataclass
from typing import Optional
from fastapi import Depends, HTTPException, status
from sqlalchemy.orm import Session

from database import get_db
from models import User, OLT
from auth import get_current_user


@dataclass
class OwnerContext:
    user_id: int
    username: str
    role: str
    privilege: int
    is_super_admin: bool
    scope_owner_id: Optional[int]   # None = Multivers (lihat semua)

    @property
    def is_multivers(self) -> bool:
        return self.is_super_admin


def get_owner_ctx(user: User = Depends(get_current_user)) -> OwnerContext:
    """Resolve OwnerContext dari user yang login.

    Kuncinya: cek is_super_admin HANYA di sini. Endpoint di bawahnya
    cukup cek ctx.scope_owner_id is None / bukan — tidak pernah
    sentuh is_super_admin lagi.
    """
    if user.is_super_admin:
        scope = None
    elif user.privilege >= 15:
        scope = user.id
    else:
        scope = user.owner_user_id

    return OwnerContext(
        user_id=user.id,
        username=user.username,
        role=user.role,
        privilege=user.privilege,
        is_super_admin=bool(user.is_super_admin),
        scope_owner_id=scope,
    )


def scoped_olts(db: Session, ctx: OwnerContext):
    """Query OLT dengan scope ownership. Return Query (bisa di-chain)."""
    q = db.query(OLT)
    if ctx.scope_owner_id is not None:
        q = q.filter(OLT.owner_user_id == ctx.scope_owner_id)
    return q


def get_olt_or_403(db: Session, olt_id: int, ctx: OwnerContext) -> OLT:
    """Ambil OLT by id dengan cek ownership.

    - 404 jika OLT tidak ada sama sekali
    - 403 jika OLT ada tapi bukan milik user (scope mismatch)
    """
    olt = db.query(OLT).filter(OLT.id == olt_id).first()
    if olt is None:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "OLT tidak ditemukan")
    if ctx.scope_owner_id is not None and olt.owner_user_id != ctx.scope_owner_id:
        raise HTTPException(
            status.HTTP_403_FORBIDDEN,
            "Anda tidak punya akses ke OLT ini"
        )
    return olt


def require_olt_access(
    olt_id: int,
    db: Session = Depends(get_db),
    ctx: OwnerContext = Depends(get_owner_ctx),
) -> OLT:
    """FastAPI dependency: validasi akses ke OLT dari path param {olt_id}.

    Return OLT kalau user punya akses (Multivers atau owner match).
    Raise 404 kalau OLT tidak ada, 403 kalau bukan milik user.

    Pemakaian:
        @router.get("/{olt_id}/status")
        def olt_status(olt: OLT = Depends(require_olt_access), ...):
            # olt sudah divalidasi
    """
    return get_olt_or_403(db, olt_id, ctx)


def scoped_alerts(db: Session, ctx: OwnerContext):
    """Query Alert dengan scope ownership (join ke OLT)."""
    from models import Alert
    q = db.query(Alert)
    if ctx.scope_owner_id is not None:
        # Subquery: OLT ids milik scope
        olt_subq = db.query(OLT.id).filter(OLT.owner_user_id == ctx.scope_owner_id).subquery()
        q = q.filter(Alert.olt_id.in_(olt_subq))
    return q


def require_alert_access(
    alert_id: int,
    db: Session = Depends(get_db),
    ctx: OwnerContext = Depends(get_owner_ctx),
):
    """FastAPI dependency: validasi akses ke Alert by alert_id.

    Validasi via alert.olt_id -> OLT.owner_user_id.
    Return Alert atau raise 404/403.
    """
    from models import Alert
    alert = db.query(Alert).get(alert_id)
    if not alert:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "Alert tidak ditemukan")
    if ctx.scope_owner_id is not None:
        olt = db.query(OLT).get(alert.olt_id)
        if not olt or olt.owner_user_id != ctx.scope_owner_id:
            raise HTTPException(
                status.HTTP_403_FORBIDDEN,
                "Anda tidak punya akses ke alert ini"
            )
    return alert
