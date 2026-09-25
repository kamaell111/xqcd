from fastapi import APIRouter, Depends, HTTPException
from sqlalchemy.orm import Session
from typing import List, Optional
from datetime import datetime, timedelta
from database import get_db
from models import OLT, PONPort, ONU, Interface, User, Alert, ONUEvent
from schemas import PONPortOut, ONUOut
from auth import get_current_user
from tenancy import require_olt_access
from olt_manager import olt_manager

router = APIRouter(prefix="/api/v1/olts", tags=["monitoring"])


@router.get("/{olt_id}/status")
async def olt_status(olt: OLT = Depends(require_olt_access),
                     db: Session = Depends(get_db)):
    # SNMP dimatikan sementara — OLT di NAT publik, port UDP 161 tidak reachable.
    # Data CPU/Mem/Uptime diambil via endpoint /sync (Telnet).
    return {
        "hostname": olt.hostname,
        "ip_address": olt.ip_address,
        "status": olt.status,
        "cpu_usage": olt.cpu_usage,
        "memory_usage": olt.memory_usage,
        "uptime_seconds": olt.uptime_seconds,
        "temperature": olt.temperature,
        "firmware": olt.firmware,
        "model": olt.model,
        "last_polled": olt.last_polled,
        "active_alerts": db.query(Alert).filter(
            Alert.olt_id == olt.id, Alert.resolved == False
        ).count(),
        "circuit": olt_manager.get_olt_circuit_state(str(olt.id)),
    }


@router.get("/{olt_id}/pons", response_model=List[PONPortOut])
def list_pons(olt: OLT = Depends(require_olt_access),
              db: Session = Depends(get_db)):
    return db.query(PONPort).filter(PONPort.olt_id == olt.id).order_by(PONPort.port_no).all()


@router.get("/{olt_id}/pons/{port}/onus", response_model=List[ONUOut])
def list_onus_on_pon(port: str,
                     olt: OLT = Depends(require_olt_access),
                     db: Session = Depends(get_db)):
    return db.query(ONU).filter(ONU.olt_id == olt.id, ONU.pon_port == port).all()


@router.get("/{olt_id}/onus", response_model=List[ONUOut])
def list_all_onus(status: Optional[str] = None,
                  olt: OLT = Depends(require_olt_access),
                  db: Session = Depends(get_db)):
    q = db.query(ONU).filter(ONU.olt_id == olt.id)
    if status:
        q = q.filter(ONU.status == status)
    return q.all()


@router.get("/{olt_id}/onus/{onu_id}", response_model=ONUOut)
def get_onu(onu_id: int,
            olt: OLT = Depends(require_olt_access),
            db: Session = Depends(get_db)):
    onu = db.query(ONU).filter(ONU.olt_id == olt.id, ONU.onu_id == onu_id).first()
    if not onu:
        raise HTTPException(404, "ONU tidak ditemukan")
    return onu


@router.get("/{olt_id}/onus/{onu_id}/events")
def get_onu_events(onu_id: int, limit: int = 50,
                   olt: OLT = Depends(require_olt_access),
                   db: Session = Depends(get_db)):
    """Timeline event ONU — status change + pppoe change."""
    onu = db.query(ONU).filter(ONU.olt_id == olt.id, ONU.onu_id == onu_id).first()
    if not onu:
        raise HTTPException(404, "ONU tidak ditemukan")
    events = (db.query(ONUEvent)
                .filter(ONUEvent.olt_id == olt.id, ONUEvent.onu_id == onu_id)
                .order_by(ONUEvent.created_at.desc())
                .limit(limit)
                .all())
    return [
        {
            "id": e.id,
            "event_type": e.event_type,
            "old_value": e.old_value,
            "new_value": e.new_value,
            "detail": e.detail,
            "created_at": e.created_at.isoformat() + "Z" if e.created_at else None,
        }
        for e in events
    ]


@router.get("/{olt_id}/interfaces")
def list_interfaces(olt: OLT = Depends(require_olt_access),
                    db: Session = Depends(get_db)):
    ifaces = db.query(Interface).filter(Interface.olt_id == olt.id).all()
    return [{
        "id": i.id, "name": i.name, "type": i.type, "status": i.status,
        "admin_state": i.admin_state, "speed": i.speed, "duplex": i.duplex,
        "switchport_mode": i.switchport_mode, "vlans": i.vlans,
        "rx_bytes": i.rx_bytes, "tx_bytes": i.tx_bytes,
        "rx_errors": i.rx_errors, "tx_errors": i.tx_errors,
        "hybrid_attribute": i.hybrid_attribute,
    } for i in ifaces]


@router.get("/{olt_id}/metrics")
def get_metrics(metric: str = "cpu", hours: int = 24,
                olt: OLT = Depends(require_olt_access),
                db: Session = Depends(get_db)):
    from models import MetricHistory
    since = datetime.utcnow() - timedelta(hours=hours)
    rows = (db.query(MetricHistory)
              .filter(MetricHistory.olt_id == olt.id,
                      MetricHistory.metric == metric,
                      MetricHistory.ts >= since)
              .order_by(MetricHistory.ts.asc())
              .all())
    # Tandai sebagai UTC (Z) supaya browser convert ke WIB otomatis
    points = [{"ts": r.ts.isoformat() + "Z", "value": r.value} for r in rows]
    return {"metric": metric, "points": points}