from fastapi import APIRouter, Depends, HTTPException
from sqlalchemy.orm import Session
from typing import List
from datetime import datetime
from database import get_db
from models import OLT, User
from schemas import OLTCreate, OLTOut, OLTUpdate, OLTTestRequest
from auth import get_current_user, require_privilege, audit
from tenancy import OwnerContext, get_owner_ctx, scoped_olts, get_olt_or_403, require_olt_access
from olt_manager import olt_manager
from drivers import list_drivers, is_valid as is_driver_valid

router = APIRouter(prefix="/api/v1/olts", tags=["olts"])


@router.get("", response_model=List[OLTOut])
def list_olts(db: Session = Depends(get_db), ctx: OwnerContext = Depends(get_owner_ctx)):
    return scoped_olts(db, ctx).all()


@router.post("", response_model=OLTOut)
def create_olt(req: OLTCreate, db: Session = Depends(get_db),
               user: User = Depends(require_privilege(10))):
    if db.query(OLT).filter(OLT.ip_address == req.ip_address).first():
        raise HTTPException(400, "OLT dengan IP tersebut sudah ada")

    data = req.model_dump()

    # Phase B3: mass-assignment protection
    # - Multivers: boleh set owner_user_id eksplisit (validasi user ada)
    # - Admin biasa: ABAIKAN owner_user_id dari body, auto-set ke self.id
    if user.is_super_admin:
        owner = data.get("owner_user_id")
        if owner:
            target = db.query(User).get(owner)
            if not target:
                raise HTTPException(400, f"Owner user id={owner} tidak ada")
            if target.is_super_admin:
                # OLT milik Multivers = owner_user_id NULL
                data["owner_user_id"] = None
            else:
                data["owner_user_id"] = owner
        # else: NULL (JSN pusat / Multivers)
    else:
        data["owner_user_id"] = user.id

    olt = OLT(**data)
    db.add(olt)
    db.commit()
    db.refresh(olt)
    audit(db, user.username, "create_olt", f"{req.ip_address} owner={data.get('owner_user_id')}")
    return olt


# =================== MULTI-VENDOR (1a-1) ===================
# Route statis harus di atas /{olt_id} biar FastAPI tidak parse "drivers" sebagai int.

@router.get("/drivers")
def get_drivers(user: User = Depends(get_current_user)):
    """Daftar driver + hardware types untuk UI dropdown."""
    return list_drivers()


@router.post("/test-connection")
def test_olt_connection(req: OLTTestRequest,
                        user: User = Depends(require_privilege(15))):
    """Tes koneksi Telnet ke OLT tanpa simpan ke DB."""
    from olt_client import OLTClient
    import re
    try:
        client = OLTClient(
            host=req.ip_address,
            username=req.username,
            password=req.password,
            enable_password=req.enable_password or "",
            port=req.port,
            protocol=req.protocol or "telnet",
            fast_test=True,  # 1a-1: timeout cepat, retry 1x, jangan blok UI 90s
        )
        result = client.test_connection()
        if result.get("ok") and result.get("output"):
            out = result["output"]
            m = re.search(r"System name:\s*(\S+)", out) or re.search(r"^\s*hostname\s+(\S+)", out, re.MULTILINE)
            result["hostname"] = m.group(1) if m else ""
            m = re.search(r"(ZXA10\s+)?C\d{3}", out)
            result["model"] = m.group(0).strip() if m else ""
            m = re.search(r"V\d+\.\d+\.\d+", out)
            result["firmware"] = m.group(0) if m else ""
        return result
    except Exception as e:
        return {"ok": False, "error": f"{type(e).__name__}: {e}"}


# =================== SNMP (Phase 2) ===================

@router.post("/{olt_id}/test-snmp")
async def test_snmp(olt: OLT = Depends(require_olt_access),
                    user: User = Depends(get_current_user)):
    """Test koneksi SNMP ke OLT + ambil sysDescr + uptime.

    Pakai community dari olt.snmp_community_ro atau fallback 'public'.
    """
    from olt_snmp import OltSnmpClient
    community = olt.snmp_community_ro or "public"
    port = olt.snmp_port or 161

    client = OltSnmpClient(olt.ip_address, community, port=port)
    try:
        result = await client.test_connection()
        if result.get("ok"):
            up = await client.get_sys_uptime()
            result["uptime_seconds"] = up
            result["community_used"] = community
        return result
    except Exception as e:
        return {"ok": False, "error": f"{type(e).__name__}: {e}"}


@router.post("/{olt_id}/sync-snmp")
async def sync_snmp(olt: OLT = Depends(require_olt_access),
                    user: User = Depends(get_current_user)):
    """Walk ONU via SNMP — return JSON untuk verifikasi.

    TIDAK menyentuh DB dulu. Cuma lihat apa yang SNMP kirim.
    Kalau sudah OK, baru integrate ke _do_sync_pons.
    """
    from olt_snmp import OltSnmpClient
    import time as _t

    community = olt.snmp_community_ro or "public"
    port = olt.snmp_port or 161

    client = OltSnmpClient(olt.ip_address, community, port=port)
    t0 = _t.time()
    try:
        onus = await client.list_onus()
        elapsed = round(_t.time() - t0, 2)
        return {
            "ok": True,
            "total": len(onus),
            "elapsed_seconds": elapsed,
            "onus": [
                {
                    "pon_idx": o.pon_idx,
                    "onu_id": o.onu_id,
                    "name": o.name,
                    "desc": o.desc,
                    "onu_type": o.onu_type,
                    "serial_number": o.serial_number,
                    "status": o.status,
                    "rx_dbm": o.rx_dbm,
                    "tx_dbm": o.tx_dbm,
                    "distance_m": o.distance_m,
                }
                for o in onus
            ],
        }
    except Exception as e:
        return {"ok": False, "error": f"{type(e).__name__}: {e}"}


@router.get("/{olt_id}", response_model=OLTOut)
def get_olt(olt_id: int, db: Session = Depends(get_db), ctx: OwnerContext = Depends(get_owner_ctx)):
    return get_olt_or_403(db, olt_id, ctx)


@router.post("/{olt_id}/config/commit")
def commit_config(db: Session = Depends(get_db),
                  olt: OLT = Depends(require_olt_access),
                  user: User = Depends(require_privilege(15))):
    """Commit running-config ke flash (write). Dipakai untuk two-phase commit."""
    from olt_client import OLTClient
    from olt_manager import olt_manager

    lock = olt_manager.get_thread_lock(str(olt.id))
    if not lock.acquire(timeout=30):
        raise HTTPException(409, "OLT sedang sibuk — coba beberapa detik lagi")
    try:
        client = OLTClient(
            host=olt.ip_address, username=olt.username,
            password=olt.password, enable_password=olt.enable_password,
            port=olt.port, protocol=olt.protocol,
        )
        try:
            out = client.commit_config()
        except Exception as e:
            audit(db, user.username, "commit_config", str(olt.id), str(e), "failed")
            raise HTTPException(500, f"Gagal commit: {e}")

        olt_manager.mark_committed(str(olt.id))
        # ⚡ Cache invalidate — config berubah, cache running-config harus expired
        try:
            from routers.sync import invalidate_olt_cache
            invalidate_olt_cache(olt.id)
        except Exception as e:
            print(f"[CACHE] gagal invalidate: {e}")
        audit(db, user.username, "commit_config", str(olt.id))
        return {
            "ok": True,
            "output": out[:500] if out else "",
        }
    finally:
        lock.release()


@router.get("/{olt_id}/config/pending")
def get_config_pending(db: Session = Depends(get_db),
                       olt: OLT = Depends(require_olt_access),
                       user: User = Depends(get_current_user)):
    """Status pending config (belum di-commit)."""
    from olt_manager import olt_manager
    return olt_manager.get_pending_state(str(olt.id))


@router.get("/{olt_id}/circuit")
def get_circuit_state(db: Session = Depends(get_db),
                      olt: OLT = Depends(require_olt_access),
                      user: User = Depends(get_current_user)):
    """Status circuit breaker OLT — untuk indikator di UI."""
    state = olt_manager.get_olt_circuit_state(str(olt.id))
    state["olt_id"] = olt.id
    state["ip_address"] = olt.ip_address
    return state


@router.patch("/{olt_id}", response_model=OLTOut)
def update_olt(req: OLTUpdate,
               db: Session = Depends(get_db),
               olt: OLT = Depends(require_olt_access),
               user: User = Depends(require_privilege(15))):
    """Update OLT. Field None = tidak diubah. Password kosong = tidak ubah."""
    data = req.model_dump(exclude_unset=True)

    # Phase B3: mass-assignment protection owner_user_id
    if "owner_user_id" in data:
        if not user.is_super_admin:
            # Admin biasa tidak boleh transfer kepemilikan
            raise HTTPException(
                403,
                "Hanya Multivers yang bisa ubah kepemilikan OLT"
            )
        owner = data["owner_user_id"]
        if owner is not None:
            target = db.query(User).get(owner)
            if not target:
                raise HTTPException(400, f"Owner user id={owner} tidak ada")
            if target.is_super_admin:
                data["owner_user_id"] = None  # Multivers = NULL

    new_driver = data.get("driver", olt.driver)
    new_hw = data.get("hardware_type", olt.hardware_type)
    if not is_driver_valid(new_driver, new_hw):
        raise HTTPException(400, f"Driver/hardware tidak valid: {new_driver}/{new_hw}")

    new_ip = data.get("ip_address", olt.ip_address)
    new_port = data.get("port", olt.port)
    dup = db.query(OLT).filter(
        OLT.ip_address == new_ip, OLT.port == new_port, OLT.id != olt.id
    ).first()
    if dup:
        raise HTTPException(400, f"OLT dengan {new_ip}:{new_port} sudah ada")

    if "password" in data and data["password"] == "":
        data.pop("password")
    if "enable_password" in data and data["enable_password"] == "":
        data.pop("enable_password")

    cred_keys = {"ip_address", "port", "username", "password", "enable_password", "protocol"}
    should_evict = bool(cred_keys & set(data.keys()))

    for k, v in data.items():
        setattr(olt, k, v)

    db.commit()
    db.refresh(olt)

    if should_evict:
        from olt_client import evict_connection
        evicted = evict_connection(olt.ip_address, olt.port)
        print(f"[OLT-UPDATE] id={olt.id} evicted {evicted} connection(s)")

    safe_changes = {k: v for k, v in data.items()
                    if k not in ("password", "enable_password")}
    audit(db, user.username, "update_olt", f"{olt.id} {safe_changes}")
    return olt


@router.post("/{olt_id}/traffic/poll")
async def poll_traffic(olt: OLT = Depends(require_olt_access),
                       db: Session = Depends(get_db),
                       user: User = Depends(get_current_user)):
    """Poll IF-MIB counters → hitung rate → simpan ke DB.

    Thin wrapper di atas traffic_poller.poll_traffic_for_olt().
    """
    from traffic_poller import poll_traffic_for_olt
    try:
        return await poll_traffic_for_olt(db, olt)
    except Exception as e:
        db.rollback()
        return {"ok": False, "error": f"{type(e).__name__}: {e}"}



@router.delete("/{olt_id}")
def delete_olt(db: Session = Depends(get_db),
               olt: OLT = Depends(require_olt_access),
               user: User = Depends(require_privilege(15))):
    # Bersihkan cache koneksi Netmiko + state in-memory sebelum hapus
    from olt_client import evict_connection
    evicted = evict_connection(olt.ip_address, olt.port)

    # Fix #5: bersihkan lock/circuit/pending/jobs terminal dari olt_manager
    cleared = olt_manager.clear_olt_state(olt.id)

    oid = olt.id
    db.delete(olt)
    db.commit()
    audit(db, user.username, "delete_olt", str(oid))
    return {"ok": True, "evicted_connections": evicted, "cleared_state": cleared}