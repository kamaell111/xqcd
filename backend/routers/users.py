from fastapi import APIRouter, Depends, HTTPException
from sqlalchemy.orm import Session
from typing import List
from database import get_db
from models import User, AuditLog
from schemas import UserCreate, UserOut
from auth import get_current_user, require_privilege, hash_password, audit

router = APIRouter(prefix="/api/v1", tags=["users"])


@router.get("/users", response_model=List[UserOut])
def list_users(db: Session = Depends(get_db),
               user: User = Depends(require_privilege(15))):
    return db.query(User).all()


@router.post("/users", response_model=UserOut)
def create_user(req: UserCreate, db: Session = Depends(get_db),
                user: User = Depends(require_privilege(15))):
    if db.query(User).filter(User.username == req.username).first():
        raise HTTPException(400, "Username sudah ada")
    u = User(username=req.username,
             password_hash=hash_password(req.password),
             privilege=req.privilege, role=req.role)
    db.add(u)
    db.commit()
    db.refresh(u)
    audit(db, user.username, "create_user", req.username)
    return u


@router.delete("/users/{user_id}")
def delete_user(user_id: int, db: Session = Depends(get_db),
                user: User = Depends(require_privilege(15))):
    u = db.query(User).get(user_id)
    if not u:
        raise HTTPException(404, "User tidak ditemukan")
    db.delete(u)
    db.commit()
    audit(db, user.username, "delete_user", str(user_id))
    return {"ok": True}


@router.get("/audit-logs")
def list_audit(limit: int = 200, db: Session = Depends(get_db),
               user: User = Depends(require_privilege(10))):
    logs = db.query(AuditLog).order_by(AuditLog.created_at.desc()).limit(limit).all()
    return [{"id": l.id, "username": l.username, "action": l.action,
             "target": l.target, "detail": l.detail, "result": l.result,
             "ip_address": l.ip_address,
             "created_at": l.created_at.isoformat() + "Z" if l.created_at else None}
            for l in logs]


@router.post("/audit-logs/bulk-delete")
def bulk_delete_audit_logs(ids: List[int], db: Session = Depends(get_db),
                            user: User = Depends(require_privilege(15))):
    """Hapus beberapa audit log sekaligus berdasarkan list ID."""
    if not ids:
        return {"ok": True, "deleted": 0}
    count = db.query(AuditLog).filter(AuditLog.id.in_(ids)).delete(
        synchronize_session=False
    )
    db.commit()
    # Audit log tentang penghapusan audit log (meta)
    audit(db, user.username, "bulk_delete_audit_logs", f"count={count}")
    return {"ok": True, "deleted": count}