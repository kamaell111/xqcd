from fastapi import APIRouter, Depends, HTTPException
from sqlalchemy.orm import Session
from typing import List
from datetime import datetime
from database import get_db
from models import User, AuditLog
from schemas import UserCreate, UserOut, UserUpdate, PasswordReset, PasswordSelfChange
from auth import (get_current_user, require_privilege, hash_password,
                  verify_password, bump_token_version, audit)

router = APIRouter(prefix="/api/v1", tags=["users"])


# ⭐ Peta role → privilege. Satu sumber kebenaran.
ROLE_PRIVILEGE = {
    "admin": 15,
    "operator": 10,
    "field_tech": 8,
    "viewer": 5,
}


def _role_to_privilege(role: str) -> int:
    if role not in ROLE_PRIVILEGE:
        raise HTTPException(400, f"Role '{role}' tidak valid. Pilih: {list(ROLE_PRIVILEGE.keys())}")
    return ROLE_PRIVILEGE[role]


def _count_active_admins(db: Session, exclude_id: int = None) -> int:
    """Hitung admin aktif. Exclude_id untuk cek jika user tertentu dinonaktifkan/dihapus."""
    q = db.query(User).filter(
        User.is_active == True,
        User.role == "admin",
    )
    if exclude_id:
        q = q.filter(User.id != exclude_id)
    return q.count()


# =================== LIST ===================
@router.get("/users", response_model=List[UserOut])
def list_users(db: Session = Depends(get_db),
               user: User = Depends(require_privilege(15))):
    return db.query(User).all()


# =================== CREATE ===================
@router.post("/users", response_model=UserOut)
def create_user(req: UserCreate, db: Session = Depends(get_db),
                user: User = Depends(require_privilege(15))):
    if db.query(User).filter(User.username == req.username).first():
        raise HTTPException(400, "Username sudah ada")

    # ⭐ Role menentukan privilege (bukan input manual)
    role = req.role or "viewer"
    priv = _role_to_privilege(role)

    u = User(
        username=req.username,
        password_hash=hash_password(req.password),
        full_name=req.full_name if hasattr(req, "full_name") else None,
        privilege=priv,
        role=role,
    )
    db.add(u)
    db.commit()
    db.refresh(u)
    audit(db, user.username, "create_user", f"{req.username} ({role})")
    return u


# =================== EDIT ===================
@router.put("/users/{user_id}", response_model=UserOut)
def update_user(user_id: int, req: UserUpdate,
                db: Session = Depends(get_db),
                user: User = Depends(require_privilege(15))):
    u = db.query(User).get(user_id)
    if not u:
        raise HTTPException(404, "User tidak ditemukan")

    changed = []
    bump = False

    # Update full_name
    if req.full_name is not None:
        u.full_name = req.full_name
        changed.append(f"full_name={req.full_name}")

    # Update role (mengubah privilege + invalidate token)
    if req.role is not None and req.role != u.role:
        new_priv = _role_to_privilege(req.role)
        old_role = u.role
        # ⭐ Guard: kalau turun dari admin dan tinggal 1 admin → tolak
        if old_role == "admin" and new_priv < 15:
            if _count_active_admins(db, exclude_id=user_id) == 0:
                raise HTTPException(400, "Tidak bisa menurunkan role admin terakhir")
        u.role = req.role
        u.privilege = new_priv
        changed.append(f"role={old_role}→{req.role}")
        bump = True

    # Update is_active (nonaktif/aktifkan)
    if req.is_active is not None and req.is_active != u.is_active:
        if not req.is_active:
            # Guard: tolak kalau admin terakhir
            if u.role == "admin" and _count_active_admins(db, exclude_id=user_id) == 0:
                raise HTTPException(400, "Tidak bisa menonaktifkan admin terakhir")
            # Guard: tolak nonaktifkan diri sendiri
            if u.id == user.id:
                raise HTTPException(400, "Tidak bisa menonaktifkan akun sendiri")
        u.is_active = req.is_active
        changed.append(f"is_active={req.is_active}")
        bump = True

    u.updated_at = datetime.utcnow()

    if bump:
        bump_token_version(db, u)   # commit juga
    else:
        db.commit()

    db.refresh(u)
    if changed:
        audit(db, user.username, "update_user", f"{u.username}: {', '.join(changed)}")
    return u


# =================== GANTI PASSWORD (ADMIN RESET) ===================
@router.put("/users/{user_id}/password")
def admin_reset_password(user_id: int, req: PasswordReset,
                         db: Session = Depends(get_db),
                         user: User = Depends(require_privilege(15))):
    u = db.query(User).get(user_id)
    if not u:
        raise HTTPException(404, "User tidak ditemukan")
    if not req.new_password or len(req.new_password) < 6:
        raise HTTPException(400, "Password minimal 6 karakter")

    u.password_hash = hash_password(req.new_password)
    u.updated_at = datetime.utcnow()
    bump_token_version(db, u)   # invalidate semua token u
    audit(db, user.username, "admin_reset_password", u.username)
    return {"ok": True, "message": f"Password {u.username} berhasil direset"}


# =================== GANTI PASSWORD (SENDIRI) ===================
@router.put("/users/me/password")
def self_change_password(req: PasswordSelfChange,
                         db: Session = Depends(get_db),
                         user: User = Depends(get_current_user)):
    if not verify_password(req.old_password, user.password_hash):
        raise HTTPException(400, "Password lama salah")
    if not req.new_password or len(req.new_password) < 6:
        raise HTTPException(400, "Password baru minimal 6 karakter")
    if req.old_password == req.new_password:
        raise HTTPException(400, "Password baru harus beda dari yang lama")

    user.password_hash = hash_password(req.new_password)
    user.updated_at = datetime.utcnow()
    bump_token_version(db, user)   # invalidate token lama — user harus login ulang
    audit(db, user.username, "self_change_password", user.username)
    return {"ok": True, "message": "Password berhasil diubah. Silakan login ulang."}


# =================== DELETE ===================
@router.delete("/users/{user_id}")
def delete_user(user_id: int, db: Session = Depends(get_db),
                user: User = Depends(require_privilege(15))):
    u = db.query(User).get(user_id)
    if not u:
        raise HTTPException(404, "User tidak ditemukan")

    # ⭐ Guard: admin terakhir
    if u.role == "admin" and _count_active_admins(db, exclude_id=user_id) == 0:
        raise HTTPException(400, "Tidak bisa hapus admin terakhir")

    # ⭐ Guard: tidak bisa hapus diri sendiri
    if u.id == user.id:
        raise HTTPException(400, "Tidak bisa hapus akun sendiri")

    username = u.username
    db.delete(u)
    db.commit()
    audit(db, user.username, "delete_user", username)
    return {"ok": True}


# =================== AUDIT LOGS ===================
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
    if not ids:
        return {"ok": True, "deleted": 0}
    count = db.query(AuditLog).filter(AuditLog.id.in_(ids)).delete(
        synchronize_session=False
    )
    db.commit()
    audit(db, user.username, "bulk_delete_audit_logs", f"count={count}")
    return {"ok": True, "deleted": count}
