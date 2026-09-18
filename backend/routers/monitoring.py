from fastapi import APIRouter, Depends, HTTPException
from sqlalchemy.orm import Session
from typing import List, Optional
from datetime import datetime, timedelta
from database import get_db
from models import OLT, PONPort, ONU, Interface, User, Alert
from schemas import PONPortOut, ONUOut
from auth import get_current_user

router = APIRouter(prefix="/api/v1/olts", tags=["monitoring"])


@router.get("/{olt_id}/status")
async def olt_status(olt_id: int, db: Session = Depends(get_db),
                     user: User = Depends(get_current_user)):
    olt = db.query(OLT).get(olt_id)
    if not olt:
        raise HTTPException(404, "OLT tidak ditemukan")
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
            Alert.olt_id == olt_id, Alert.resolved == False
        ).count(),
    }


@router.get("/{olt_id}/pons", response_model=List[PONPortOut])
def list_pons(olt_id: int, db: Session = Depends(get_db),
              user: User = Depends(get_current_user)):
    return db.query(PONPort).filter(PONPort.olt_id == olt_id).order_by(PONPort.port_no).all()


@router.get("/{olt_id}/pons/{port}/onus", response_model=List[ONUOut])
def list_onus_on_pon(olt_id: int, port: str, db: Session = Depends(get_db),
                     user: User = Depends(get_current_user)):
    return db.query(ONU).filter(ONU.olt_id == olt_id, ONU.pon_port == port).all()


@router.get("/{olt_id}/onus", response_model=List[ONUOut])
def list_all_onus(olt_id: int, status: Optional[str] = None,
                  db: Session = Depends(get_db),
                  user: User = Depends(get_current_user)):
    q = db.query(ONU).filter(ONU.olt_id == olt_id)
    if status:
        q = q.filter(ONU.status == status)
    return q.all()


@router.get("/{olt_id}/onus/{onu_id}", response_model=ONUOut)
def get_onu(olt_id: int, onu_id: int, db: Session = Depends(get_db),
            user: User = Depends(get_current_user)):
    onu = db.query(ONU).filter(ONU.olt_id == olt_id, ONU.onu_id == onu_id).first()
    if not onu:
        raise HTTPException(404, "ONU tidak ditemukan")
    return onu


@router.get("/{olt_id}/interfaces")
def list_interfaces(olt_id: int, db: Session = Depends(get_db),
                    user: User = Depends(get_current_user)):
    ifaces = db.query(Interface).filter(Interface.olt_id == olt_id).all()
    return [{
        "id": i.id, "name": i.name, "type": i.type, "status": i.status,
        "admin_state": i.admin_state, "speed": i.speed, "duplex": i.duplex,
        "switchport_mode": i.switchport_mode, "vlans": i.vlans,
        "rx_bytes": i.rx_bytes, "tx_bytes": i.tx_bytes,
        "rx_errors": i.rx_errors, "tx_errors": i.tx_errors,
        "hybrid_attribute": i.hybrid_attribute,
    } for i in ifaces]


@router.get("/{olt_id}/metrics")
def get_metrics(olt_id: int, metric: str = "cpu", hours: int = 24,
                db: Session = Depends(get_db),
                user: User = Depends(get_current_user)):
    from models import MetricHistory
    since = datetime.utcnow() - timedelta(hours=hours)
    rows = (db.query(MetricHistory)
              .filter(MetricHistory.olt_id == olt_id,
                      MetricHistory.metric == metric,
                      MetricHistory.ts >= since)
              .order_by(MetricHistory.ts.asc())
              .all())
    # Tandai sebagai UTC (Z) supaya browser convert ke WIB otomatis
    points = [{"ts": r.ts.isoformat() + "Z", "value": r.value} for r in rows]
    return {"metric": metric, "points": points}