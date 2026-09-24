"""Helper: catat event ONU saat status berubah."""
from sqlalchemy.orm import Session
from models import ONUEvent


def log_onu_event(db: Session, olt_id: int, onu, event_type: str,
                  old_value=None, new_value=None, detail: str = None):
    """Catat event ONU ke DB. Caller yang commit."""
    try:
        db.add(ONUEvent(
            olt_id=olt_id,
            onu_id=onu.onu_id,
            pon_port=onu.pon_port,
            serial_number=onu.serial_number,
            event_type=event_type,
            old_value=str(old_value)[:32] if old_value is not None else None,
            new_value=str(new_value)[:32] if new_value is not None else None,
            detail=detail,
        ))
    except Exception as e:
        print(f"[EVENT-LOG] Gagal catat event: {e}")
