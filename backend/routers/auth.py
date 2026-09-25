from fastapi import APIRouter, Depends, HTTPException, Request
from sqlalchemy.orm import Session
from datetime import datetime
from database import get_db
from models import User
from schemas import LoginRequest, Token
from auth import verify_password, create_access_token, get_current_user, audit

router = APIRouter(prefix="/api/v1/auth", tags=["auth"])


@router.post("/login", response_model=Token)
def login(req: LoginRequest, request: Request, db: Session = Depends(get_db)):
    user = db.query(User).filter(User.username == req.username).first()
    if not user or not verify_password(req.password, user.password_hash):
        audit(db, req.username, "login", "auth", "failed", "failed", request.client.host)
        raise HTTPException(401, "Username atau password salah")
    if not user.is_active:
        raise HTTPException(403, "User tidak aktif")
    user.last_login = datetime.utcnow()
    db.commit()
    token = create_access_token(
        {"sub": user.username, "priv": user.privilege, "role": user.role},
        token_version=user.token_version or 0,
    )
    audit(db, user.username, "login", "auth", "success", "success", request.client.host)
    return Token(access_token=token, user={
        "id": user.id, "username": user.username,
        "privilege": user.privilege, "role": user.role,
    })


@router.get("/me")
def me(user: User = Depends(get_current_user)):
    return {
        "id": user.id,
        "username": user.username,
        "privilege": user.privilege,
        "role": user.role,
        "full_name": user.full_name,
        "is_super_admin": user.is_super_admin or 0,
        "owner_user_id": user.owner_user_id,
    }