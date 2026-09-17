"""Alert management + hapus + delete."""
from fastapi import APIRouter, Depends, HTTPException, Query
from sqlalchemy.orm import Session
from typing import List, Optional
from datetime import datetime, timezone

from database import get_db
from models import Alert, User
from auth import get_current_user, require_privilege, audit

router = APIRouter(prefix="/api/v1/alerts", tags=["alerts"])


def _serialize_alert(a: Alert) -> dict:
    """Serialize alert + tandai created_at sebagai UTC (suffix Z)
    supaya browser auto-convert ke WIB."""
    return {
        "id": a.id,
        "olt_id": a.olt_id,
        "severity": a.severity,
        "category": a.category,
        "title": a.title,
        "message": a.message,
        "source": a.source,
        "acknowledged": a.acknowledged,
        "resolved": a.resolved,
        "created_at": a.created_at.isoformat() + "Z" if a.created_at else None,
    }


@router.get("")
def list_alerts(severity: Optional[str] = None, resolved: Optional[bool] = None,
                olt_id: Optional[int] = None, limit: int = 100,
                db: Session = Depends(get_db),
                user: User = Depends(get_current_user)):
    q = db.query(Alert)
    if severity:
        q = q.filter(Alert.severity == severity)
    if resolved is not None:
        q = q.filter(Alert.resolved == resolved)
    if olt_id:
        q = q.filter(Alert.olt_id == olt_id)
    alerts = q.order_by(Alert.created_at.desc()).limit(limit).all()
    return [_serialize_alert(a) for a in alerts]


@router.get("/recent-recoveries")
def get_recent_recoveries(since: float = 0,
                          user: User = Depends(get_current_user)):
    """Recovery terbaru (max 60 detik terakhir).
    Frontend polling ini untuk notifikasi 'ONU sudah online lagi'."""
    from recovery_tracker import get_recoveries
    return get_recoveries(since)


@router.post("/{alert_id}/ack")
def ack_alert(alert_id: int, db: Session = Depends(get_db),
              user: User = Depends(get_current_user)):
    a = db.query(Alert).get(alert_id)
    if not a:
        raise HTTPException(404, "Alert tidak ditemukan")
    a.acknowledged = True
    a.ack_by = user.username
    a.ack_at = datetime.now(timezone.utc)
    db.commit()
    audit(db, user.username, "ack_alert", str(alert_id))
    return {"ok": True}


@router.post("/{alert_id}/resolve")
def resolve_alert(alert_id: int, db: Session = Depends(get_db),
                  user: User = Depends(get_current_user)):
    a = db.query(Alert).get(alert_id)
    if not a:
        raise HTTPException(404, "Alert tidak ditemukan")
    a.resolved = True
    a.resolved_at = datetime.now(timezone.utc)
    db.commit()
    audit(db, user.username, "resolve_alert", str(alert_id))
    return {"ok": True}


@router.delete("/{alert_id}")
def delete_alert(alert_id: int, db: Session = Depends(get_db),
                 user: User = Depends(require_privilege(10))):
    """Hapus 1 alert."""
    a = db.query(Alert).get(alert_id)
    if not a:
        raise HTTPException(404, "Alert tidak ditemukan")
    db.delete(a)
    db.commit()
    audit(db, user.username, "delete_alert", str(alert_id))
    return {"ok": True}


@router.post("/bulk-delete")
def bulk_delete_alerts(ids: List[int], db: Session = Depends(get_db),
                       user: User = Depends(require_privilege(10))):
    """Hapus beberapa alert sekaligus berdasarkan list ID."""
    if not ids:
        return {"ok": True, "deleted": 0}
    count = db.query(Alert).filter(Alert.id.in_(ids)).delete(
        synchronize_session=False
    )
    db.commit()
    audit(db, user.username, "bulk_delete_alerts", f"count={count}")
    return {"ok": True, "deleted": count}


@router.delete("")
def delete_all_alerts(only_resolved: bool = Query(False),
                      db: Session = Depends(get_db),
                      user: User = Depends(require_privilege(15))):
    """Hapus semua alert.
    - only_resolved=false (default): hapus semua
    - only_resolved=true: hapus cuma yang sudah resolved"""
    q = db.query(Alert)
    if only_resolved:
        q = q.filter(Alert.resolved == True)
    count = q.count()
    q.delete(synchronize_session=False)
    db.commit()
    audit(db, user.username, "delete_all_alerts",
          f"count={count} only_resolved={only_resolved}")
    return {"ok": True, "deleted": count}
