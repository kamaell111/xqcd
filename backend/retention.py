"""Retention job — hapus data lama biar DB tidak bengkak.

Strategi:
- onu_events: simpan 90 hari
- audit_logs: simpan 180 hari
- alerts: simpan 90 hari (kecuali yang belum resolved)
- metric_history: simpan 30 hari
"""
from datetime import datetime, timedelta
from sqlalchemy.orm import Session
from sqlalchemy import text


RETENTION = {
    "onu_events": 90,
    "audit_logs": 180,
    "alerts": 90,
    "metric_history": 30,
}


def run_retention(db: Session) -> dict:
    """Hapus data lama + VACUUM. Return statistik."""
    stats = {}
    now = datetime.utcnow()

    try:
        # 1. ONU events
        cutoff = now - timedelta(days=RETENTION["onu_events"])
        n = db.query(__import__("models").ONUEvent).filter(
            __import__("models").ONUEvent.created_at < cutoff
        ).delete(synchronize_session=False)
        stats["onu_events"] = n

        # 2. Audit logs
        cutoff = now - timedelta(days=RETENTION["audit_logs"])
        n = db.query(__import__("models").AuditLog).filter(
            __import__("models").AuditLog.created_at < cutoff
        ).delete(synchronize_session=False)
        stats["audit_logs"] = n

        # 3. Alerts (resolved only)
        cutoff = now - timedelta(days=RETENTION["alerts"])
        n = db.query(__import__("models").Alert).filter(
            __import__("models").Alert.resolved == True,
            __import__("models").Alert.created_at < cutoff
        ).delete(synchronize_session=False)
        stats["alerts_resolved"] = n

        # 4. Metric history
        cutoff = now - timedelta(days=RETENTION["metric_history"])
        n = db.query(__import__("models").MetricHistory).filter(
            __import__("models").MetricHistory.ts < cutoff
        ).delete(synchronize_session=False)
        stats["metric_history"] = n

        db.commit()

        # 5. VACUUM (di luar transaction)
        try:
            db.execute(text("VACUUM"))
            db.commit()
            stats["vacuum"] = "ok"
        except Exception as e:
            stats["vacuum"] = f"error: {e}"

    except Exception as e:
        db.rollback()
        stats["error"] = str(e)

    return stats


def log_retention(stats: dict):
    """Print ringkas hasil retention."""
    total = sum(v for k, v in stats.items() if isinstance(v, int))
    if total == 0 and stats.get("vacuum") == "ok":
        print(f"[RETENTION] Tidak ada data lama. Vacuum OK.")
    else:
        print(f"[RETENTION] Dihapus: {stats}")
