"""routers/system.py — endpoint info sistem untuk tab About."""
import shutil
import time
import os
from datetime import datetime

from fastapi import APIRouter, Depends
from sqlalchemy.orm import Session
from sqlalchemy import func

from database import get_db
from models import OLT, ONU, Alert
from auth import get_current_user
from models import User

APP_VERSION = "1.0.0"
_STARTED_AT = time.time()

router = APIRouter(prefix="/api/v1/system", tags=["system"])


def _gb(nbytes):
    return round(nbytes / (1024 ** 3), 2)


@router.get("/info")
def system_info(db: Session = Depends(get_db), user: User = Depends(get_current_user)):
    try:
        disk = shutil.disk_usage("/")
        disk_used = _gb(disk.used)
        disk_total = _gb(disk.total)
        disk_free = _gb(disk.free)
    except Exception:
        disk_used = disk_total = disk_free = 0

    # Hitung stats
    try:
        olts_count = db.query(func.count(OLT.id)).scalar() or 0
        onus_count = db.query(func.count(ONU.id)).scalar() or 0
        alerts_active = db.query(func.count(Alert.id)).filter(Alert.resolved == False).scalar() or 0
    except Exception:
        olts_count = onus_count = alerts_active = 0

    return {
        "app_name": "OLT Manager",
        "backend_version": APP_VERSION,
        "python_version": os.sys.version.split()[0],
        "uptime_seconds": int(time.time() - _STARTED_AT),
        "started_at": datetime.utcfromtimestamp(_STARTED_AT).isoformat() + "Z",
        "disk_used_gb": disk_used,
        "disk_total_gb": disk_total,
        "disk_free_gb": disk_free,
        "counts": {
            "olts": olts_count,
            "onus": onus_count,
            "alerts_active": alerts_active,
        },
        "server_time": datetime.utcnow().isoformat() + "Z",
    }
