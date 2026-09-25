import time
from fastapi import APIRouter, Depends, HTTPException, BackgroundTasks
from sqlalchemy.orm import Session
from datetime import datetime
from database import get_db
from models import OLT, User, ConfigVersion, VLAN, ONU, Interface
from schemas import ConfigPreviewRequest, ConfigApplyRequest, VLANCreate
from auth import get_current_user, require_privilege, audit
from config import settings
from olt_client import OLTClient
from olt_manager import olt_locked, olt_manager
from tenancy import require_olt_access
from zxan_parser import parse_running_config

router = APIRouter(prefix="/api/v1/olts", tags=["config"])


def _parse_vlan_summary(output: str) -> list:
    """Parse output 'show vlan summary' → list VLAN ID.
    Contoh: '1-2,15,101' → [1, 2, 15, 101]
    """
    import re as _re
    vlans = set()
    for line in output.splitlines():
        s = line.strip()
        # Skip baris kosong
        if not s:
            continue
        # Skip header (mengandung huruf)
        if _re.search(r"[A-Za-z]", s):
            continue
        # Baris kandidat: hanya digit, koma, dash, spasi
        if not _re.match(r"^[\d,\-\s]+$", s):
            continue
        # Parse: pisah per koma
        for part in s.split(","):
            part = part.strip()
            if not part:
                continue
            if "-" in part:
                try:
                    a, b = part.split("-")
                    for n in range(int(a), int(b) + 1):
                        vlans.add(n)
                except ValueError:
                    pass
            elif part.isdigit():
                vlans.add(int(part))
    return sorted(vlans)


def _make_client(olt: OLT) -> OLTClient:
    return OLTClient(
        host=olt.ip_address,
        username=olt.username,
        password=olt.password,
        enable_password=olt.enable_password,
        port=olt.port,
        protocol=olt.protocol,
    )


@router.get("/{olt_id}/config")
def get_running_config(db: Session = Depends(get_db),
                       olt: OLT = Depends(require_olt_access),
                       user: User = Depends(get_current_user)):
    client = _make_client(olt)
    try:
        raw = client.get_running_config()
    except Exception as e:
        raise HTTPException(500, f"Gagal ambil config: {e}")
    parsed = parse_running_config(raw)
    return {"raw": raw, "parsed": parsed}


@router.post("/{olt_id}/config/preview")
def preview_config(req: ConfigPreviewRequest,
                   db: Session = Depends(get_db),
                   olt: OLT = Depends(require_olt_access),
                   user: User = Depends(get_current_user)):
    client = _make_client(olt)
    result = client.validate_commands(req.commands)
    return {"commands": req.commands, "validation": result}


@router.post("/{olt_id}/config/apply")
@olt_locked
def apply_config(req: ConfigApplyRequest,
                 db: Session = Depends(get_db),
                 olt: OLT = Depends(require_olt_access),
                 user: User = Depends(require_privilege(15))):
    client = _make_client(olt)
    val = client.validate_commands(req.commands)
    if not val["valid"]:
        raise HTTPException(400, {"message": "Validasi gagal", "errors": val["errors"]})
    try:
        old_cfg = client.get_running_config()
        db.add(ConfigVersion(olt_id=olt_id,
                             version=f"pre-{datetime.utcnow().isoformat()}",
                             content=old_cfg, author=user.username,
                             comment="auto-backup sebelum apply"))
        db.commit()
    except Exception:
        pass
    try:
        output = client.run_config(req.commands, save=True)
        db.add(ConfigVersion(olt_id=olt_id,
                             version=f"applied-{datetime.utcnow().isoformat()}",
                             content="\n".join(req.commands),
                             author=user.username, comment=req.comment))
        db.commit()
        audit(db, user.username, "apply_config", str(olt_id), req.comment or "")
        return {"ok": True, "output": output}
    except Exception as e:
        audit(db, user.username, "apply_config", str(olt_id), str(e), "failed")
        raise HTTPException(500, f"Gagal apply: {e}")


@router.get("/{olt_id}/config/versions")
def list_versions(db: Session = Depends(get_db),
                  olt: OLT = Depends(require_olt_access),
                  user: User = Depends(get_current_user)):
    versions = db.query(ConfigVersion).filter(
        ConfigVersion.olt_id == olt.id
    ).order_by(ConfigVersion.created_at.desc()).all()
    return [{"id": v.id, "version": v.version, "author": v.author,
             "comment": v.comment, "created_at": v.created_at} for v in versions]


@router.get("/{olt_id}/vlans")
def list_vlans(db: Session = Depends(get_db),
               olt: OLT = Depends(require_olt_access),
               user: User = Depends(get_current_user)):
    # Sync VLAN dari OLT dulu
    client = _make_client(olt)

    try:
        conn = client._connect()
        try:
            out = conn.send_command_timing("show vlan summary", read_timeout=15)
        finally:
            conn.disconnect()
        olt_vlan_ids = _parse_vlan_summary(out)

        # ⭐ SAFETY: kalau OLT balikin kosong ATAU parsing gagal, JANGAN hapus DB.
        # Cegah mass-deletion kalau Telnet timeout/parser error.
        if not olt_vlan_ids:
            print(f"[VLAN SYNC] OLT balikin kosong — skip sync (DB tidak diubah). Raw: {out[:200]!r}")
            raise RuntimeError("OLT returned empty VLAN list — skip sync to prevent DB wipe")

        # Bandingkan dengan DB
        db_vlans = {v.vlan_id: v for v in db.query(VLAN).filter(VLAN.olt_id == olt.id).all()}

        # Tambah yang ada di OLT tapi belum di DB
        for vid in olt_vlan_ids:
            if vid not in db_vlans:
                print(f"[VLAN SYNC] Tambah VLAN {vid} dari OLT")
                db.add(VLAN(olt_id=olt.id, vlan_id=vid, name=f"VLAN{vid}"))

        # Hapus dari DB yang sudah tidak ada di OLT
        for vid, v in db_vlans.items():
            if vid not in olt_vlan_ids:
                print(f"[VLAN SYNC] Hapus VLAN {vid} (tidak ada di OLT)")
                db.delete(v)

        db.commit()
    except Exception as e:
        print(f"[VLAN SYNC] {e}")

    vlans = db.query(VLAN).filter(VLAN.olt_id == olt.id).order_by(VLAN.vlan_id).all()
    return [{"id": v.id, "vlan_id": v.vlan_id, "name": v.name,
             "description": v.description} for v in vlans]


def _do_create_vlan(olt_id: int, req_dict: dict, db: Session, username: str,
                     job_id: str = None) -> dict:
    from schemas import VLANCreate
    req = VLANCreate(**req_dict)

    def _p(msg, prog=None):
        if job_id:
            upd = {"message": msg}
            if prog is not None: upd["progress"] = prog
            olt_manager.update_job(job_id, **upd)
        print(f"[CREATE-VLAN] {msg}")

    _p("Cek OLT...", 10)
    olt = db.query(OLT).get(olt_id)
    if not olt:
        raise RuntimeError("OLT tidak ditemukan")
    if db.query(VLAN).filter(VLAN.olt_id == olt.id, VLAN.vlan_id == req.vlan_id).first():
        raise RuntimeError("VLAN sudah ada di database")

    client = _make_client(olt)
    vlan_name = req.name or f"VLAN{req.vlan_id}"
    commands = [
        "configure terminal", "vlan database",
        f"vlan {req.vlan_id} name {vlan_name}",
        "exit", "end", "write",
    ]

    _p("Kirim ke OLT...", 40)
    try:
        out = client.run_config(commands, save=True)
    except Exception as e:
        raise RuntimeError(f"Gagal create VLAN: {e}")

    _p("Verifikasi...", 70)
    import time as _t
    _t.sleep(2)
    olt_has_vlan = False
    try:
        conn = client._connect()
        try:
            summary = conn.send_command_timing("show vlan summary", read_timeout=15)
            olt_vlans = _parse_vlan_summary(summary)
            olt_has_vlan = req.vlan_id in olt_vlans
        finally:
            conn.disconnect()
    except Exception as e:
        print(f"[CREATE-VLAN] verify error: {e}")

    if not olt_has_vlan:
        raise RuntimeError(f"VLAN {req.vlan_id} tidak muncul di OLT setelah create")

    # ⭐ Cek dulu — mungkin sudah di-insert oleh auto-sync (race condition)
    existing = db.query(VLAN).filter(
        VLAN.olt_id == olt_id, VLAN.vlan_id == req.vlan_id
    ).first()
    if existing:
        # Sudah ada dari sync paralel — anggap sukses
        print(f"[CREATE-VLAN] VLAN {req.vlan_id} sudah ada (dari sync paralel) — sukses")
        _p("Selesai", 100)
        return {
            "id": existing.id,
            "vlan_id": existing.vlan_id,
            "name": existing.name,
            "description": existing.description,
            "note": "sudah ada dari sync paralel",
        }

    v = VLAN(olt_id=olt_id, vlan_id=req.vlan_id, name=vlan_name, description=req.description)
    try:
        db.add(v); db.commit(); db.refresh(v)
    except Exception as e:
        db.rollback()
        # Cek lagi setelah error (mungkin baru di-insert paralel)
        existing = db.query(VLAN).filter(
            VLAN.olt_id == olt_id, VLAN.vlan_id == req.vlan_id
        ).first()
        if existing:
            print(f"[CREATE-VLAN] VLAN {req.vlan_id} sudah ada setelah error — sukses")
            return {"id": existing.id, "vlan_id": existing.vlan_id, "name": existing.name}
        if "UNIQUE" in str(e).upper() or "integrity" in str(e).lower():
            raise RuntimeError(f"VLAN {req.vlan_id} sudah ada di database")
        raise RuntimeError(f"Gagal simpan VLAN: {e}")
    audit(db, username, "create_vlan", f"{olt_id}/vlan{req.vlan_id}")
    _p("Selesai", 100)
    return {"id": v.id, "vlan_id": v.vlan_id, "name": v.name, "description": v.description}


@router.post("/{olt_id}/vlans")
def create_vlan(req: VLANCreate,
                background_tasks: BackgroundTasks,
                db: Session = Depends(get_db),
                olt: OLT = Depends(require_olt_access),
                user: User = Depends(require_privilege(10))):
    resource_key = f"create_vlan:{req.vlan_id}"
    job_id, is_new = olt_manager.create_job_atomic(
        olt_id=str(olt.id), job_type="create_vlan", resource_key=resource_key,
    )
    if not is_new:
        return {"job_id": job_id, "status": "queued", "duplicate": True}
    background_tasks.add_task(
        _run_create_vlan_job, job_id=job_id, olt_id=olt.id,
        req_dict=req.model_dump() if hasattr(req, "model_dump") else req.dict(),
        username=user.username,
    )
    return {"job_id": job_id, "status": "queued", "message": "Job create VLAN dimulai"}


def _run_create_vlan_job(job_id: str, olt_id: int, req_dict: dict, username: str):
    from database import SessionLocal
    olt_manager.update_job(job_id, status="running", progress=5, message="Mulai...")
    db = SessionLocal()
    try:
        result = _do_create_vlan(olt_id, req_dict, db, username, job_id=job_id)
        olt_manager.finish_job(job_id, "success", result=result)
    except Exception as e:
        import traceback; traceback.print_exc()
        olt_manager.finish_job(job_id, "failed", error=str(e)[:300])
    finally:
        db.close()


@router.post("/{olt_id}/vlans_legacy_disabled")
@olt_locked
def _legacy_create_vlan_disabled(req: VLANCreate, db: Session = Depends(get_db),
                olt: OLT = Depends(require_olt_access),
                user: User = Depends(require_privilege(10))):
    if db.query(VLAN).filter(VLAN.olt_id == olt_id, VLAN.vlan_id == req.vlan_id).first():
        raise HTTPException(400, "VLAN sudah ada")

    client = _make_client(olt)
    vlan_name = req.name or f"VLAN{req.vlan_id}"

    # 1. Kirim ke OLT dulu
    commands = [
        "configure terminal",
        "vlan database",
        f"vlan {req.vlan_id} name {vlan_name}",
        "exit",
        "end",
        "write",
    ]
    try:
        out = client.run_config(commands, save=True)   # additive
        print(f"[VLAN CREATE] {out[:300]}")
    except Exception as e:
        audit(db, user.username, "create_vlan", f"{olt_id}/vlan{req.vlan_id}", str(e), "failed")
        raise HTTPException(500, f"Gagal create VLAN di OLT: {e}")

    # 2. Verifikasi dari OLT
    time.sleep(2)
    olt_has_vlan = False
    try:
        conn = client._connect()
        try:
            summary_out = conn.send_command_timing("show vlan summary", read_timeout=15)
            olt_vlans = _parse_vlan_summary(summary_out)
            olt_has_vlan = req.vlan_id in olt_vlans
        finally:
            conn.disconnect()
    except Exception as e:
        print(f"[VLAN VERIFY] {e}")

    if not olt_has_vlan:
        raise HTTPException(500, f"VLAN {req.vlan_id} tidak muncul di OLT setelah create")

    # 3. Baru simpan DB
    v = VLAN(olt_id=olt_id, vlan_id=req.vlan_id, name=vlan_name,
             description=req.description)
    db.add(v)
    db.commit()
    db.refresh(v)
    audit(db, user.username, "create_vlan", f"{olt_id}/vlan{req.vlan_id}")
    return {"id": v.id, "vlan_id": v.vlan_id, "name": v.name,
            "description": v.description}


def _find_vlan_dependencies(db, olt_id: int, vlan_id: int) -> dict:
    """Cari siapa saja yang pakai VLAN ini — ONU + uplink interface."""
    onus = db.query(ONU).filter(
        ONU.olt_id == olt_id,
        (ONU.user_vlan == vlan_id) | (ONU.vlan == vlan_id),
    ).all()
    onu_list = [
        {
            "onu_id": o.onu_id,
            "pon_port": o.pon_port,
            "serial_number": o.serial_number,
            "name": o.name,
            "status": o.status,
        }
        for o in onus
    ]

    ifaces = db.query(Interface).filter(Interface.olt_id == olt_id).all()
    iface_list = []
    for i in ifaces:
        if not i.vlans:
            continue
        # parse "1-2,15,101" jadi list int
        ids = set()
        for part in str(i.vlans).split(","):
            part = part.strip()
            if "-" in part:
                try:
                    a, b = part.split("-")
                    ids.update(range(int(a), int(b) + 1))
                except ValueError:
                    pass
            elif part.isdigit():
                ids.add(int(part))
        if vlan_id in ids:
            iface_list.append({"name": i.name, "type": i.type, "status": i.status})

    return {
        "vlan_id": vlan_id,
        "onu_count": len(onu_list),
        "interface_count": len(iface_list),
        "onus": onu_list[:20],   # cuma tampilkan 20 pertama
        "interfaces": iface_list,
        "is_management": (int(vlan_id) == int(getattr(settings, "MANAGEMENT_VLAN", 0) or 0)),
    }


@router.get("/{olt_id}/vlans/{vlan_id}/dependencies")
def get_vlan_dependencies(vlan_id: int, db: Session = Depends(get_db),
                          olt: OLT = Depends(require_olt_access),
                          user: User = Depends(get_current_user)):
    """Cek siapa saja yang pakai VLAN ini sebelum dihapus."""
    return _find_vlan_dependencies(db, olt.id, vlan_id)


def _do_delete_vlan(olt_id: int, vlan_id: int, force: bool, db: Session,
                     username: str, job_id: str = None) -> dict:
    def _p(msg, prog=None):
        if job_id:
            upd = {"message": msg}
            if prog is not None: upd["progress"] = prog
            olt_manager.update_job(job_id, **upd)
        print(f"[DELETE-VLAN] {msg}")

    _p("Cek OLT...", 10)
    olt = db.query(OLT).get(olt_id)
    if not olt:
        raise RuntimeError("OLT tidak ditemukan")
    v = db.query(VLAN).filter(VLAN.olt_id == olt_id, VLAN.vlan_id == vlan_id).first()
    if not v:
        raise RuntimeError("VLAN tidak ditemukan")

    _p("Cek dependency...", 20)
    deps = _find_vlan_dependencies(db, olt_id, vlan_id)
    if deps["is_management"]:
        raise RuntimeError(f"VLAN {vlan_id} adalah VLAN manajemen — tidak boleh dihapus")
    if (deps["onu_count"] > 0 or deps["interface_count"] > 0) and not force:
        raise RuntimeError(f"VLAN {vlan_id} masih dipakai {deps['onu_count']} ONU + {deps['interface_count']} interface. Pakai force=true untuk paksa.")

    client = _make_client(olt)
    commands = [
        "configure terminal", "vlan database",
        f"no vlan {vlan_id}",
        "exit", "end",
    ]
    _p("Kirim ke OLT...", 40)
    try:
        client.run_config(commands, save=False)
        olt_manager.mark_pending(str(olt_id), f"delete VLAN {vlan_id}")
    except Exception as e:
        raise RuntimeError(f"Gagal hapus VLAN: {e}")

    _p("Verifikasi...", 70)
    import time as _t
    _t.sleep(3)
    still_has = True
    try:
        conn = client._connect()
        try:
            summary = conn.send_command_timing("show vlan summary", read_timeout=15)
            still_has = vlan_id in _parse_vlan_summary(summary)
        finally:
            conn.disconnect()
    except Exception as e:
        print(f"[DELETE-VLAN] verify error: {e}")

    if still_has:
        raise RuntimeError(f"VLAN {vlan_id} masih ada di OLT setelah delete")

    db.delete(v); db.commit()
    audit(db, username, "delete_vlan", f"{olt_id}/vlan{vlan_id}")
    _p("Selesai", 100)
    return {"ok": True, "vlan_id": vlan_id, "dependencies": deps}


@router.delete("/{olt_id}/vlans/{vlan_id}")
def delete_vlan(olt_id: int, vlan_id: int,
                force: bool = False,
                background_tasks: BackgroundTasks = None,
                db: Session = Depends(get_db),
                olt: OLT = Depends(require_olt_access),
                user: User = Depends(require_privilege(10))):
    resource_key = f"delete_vlan:{vlan_id}"
    job_id, is_new = olt_manager.create_job_atomic(
        olt_id=str(olt.id), job_type="delete_vlan", resource_key=resource_key,
    )
    if not is_new:
        return {"job_id": job_id, "status": "queued", "duplicate": True}
    background_tasks.add_task(
        _run_delete_vlan_job, job_id=job_id, olt_id=olt.id,
        vlan_id=vlan_id, force=force, username=user.username,
    )
    return {"job_id": job_id, "status": "queued", "message": "Job delete VLAN dimulai"}


def _run_delete_vlan_job(job_id: str, olt_id: int, vlan_id: int, force: bool, username: str):
    from database import SessionLocal
    olt_manager.update_job(job_id, status="running", progress=5, message="Mulai...")
    db = SessionLocal()
    try:
        result = _do_delete_vlan(olt_id, vlan_id, force, db, username, job_id=job_id)
        olt_manager.finish_job(job_id, "success", result=result)
    except Exception as e:
        import traceback; traceback.print_exc()
        olt_manager.finish_job(job_id, "failed", error=str(e)[:300])
    finally:
        db.close()


@router.delete("/{olt_id}/vlans_legacy_disabled/{vlan_id}")
@olt_locked
def _legacy_delete_vlan_disabled(olt_id: int, vlan_id: int,
                force: bool = False,
                db: Session = Depends(get_db),
                olt: OLT = Depends(require_olt_access),
                user: User = Depends(require_privilege(10))):
    v = db.query(VLAN).filter(VLAN.olt_id == olt.id, VLAN.vlan_id == vlan_id).first()
    if not v:
        raise HTTPException(404, "VLAN tidak ditemukan")

    # ⭐ GUARD: cek dependency sebelum hapus
    deps = _find_vlan_dependencies(db, olt.id, vlan_id)

    if deps["is_management"]:
        raise HTTPException(400, {
            "message": f"VLAN {vlan_id} adalah VLAN manajemen OLT — tidak boleh dihapus.",
            "dependencies": deps,
        })

    if (deps["onu_count"] > 0 or deps["interface_count"] > 0) and not force:
        raise HTTPException(409, {
            "message": f"VLAN {vlan_id} masih dipakai {deps['onu_count']} ONU dan {deps['interface_count']} interface. Kirim ulang dengan ?force=true untuk hapus paksa.",
            "dependencies": deps,
        })

    client = _make_client(olt)

    # 1. Kirim ke OLT
    commands = [
        "configure terminal",
        "vlan database",
        f"no vlan {vlan_id}",
        "exit",
        "end",
        # tidak ada 'write' — two-phase commit
    ]
    try:
        out = client.run_config(commands, save=False)   # destructive → two-phase
        print(f"[VLAN DELETE] {out[:300]}")
        from olt_manager import olt_manager as _om
        _om.mark_pending(str(olt_id), f"delete VLAN {vlan_id}")
    except Exception as e:
        audit(db, user.username, "delete_vlan", f"{olt_id}/vlan{vlan_id}", str(e), "failed")
        raise HTTPException(500, f"Gagal hapus VLAN di OLT: {e}")

    # 2. Verifikasi
    time.sleep(3)
    olt_still_has = True
    try:
        conn = client._connect()
        try:
            summary_out = conn.send_command_timing("show vlan summary", read_timeout=15)
            olt_vlans = _parse_vlan_summary(summary_out)
            olt_still_has = vlan_id in olt_vlans
        finally:
            conn.disconnect()
    except Exception as e:
        print(f"[VLAN VERIFY] {e}")

    if olt_still_has:
        raise HTTPException(500, f"VLAN {vlan_id} masih ada di OLT setelah delete")

    # 3. Baru hapus DB
    db.delete(v)
    db.commit()
    audit(db, user.username, "delete_vlan", f"{olt_id}/vlan{vlan_id}")
    return {"ok": True}