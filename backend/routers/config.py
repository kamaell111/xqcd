import time
from fastapi import APIRouter, Depends, HTTPException
from sqlalchemy.orm import Session
from datetime import datetime
from database import get_db
from models import OLT, User, ConfigVersion, VLAN
from schemas import ConfigPreviewRequest, ConfigApplyRequest, VLANCreate
from auth import get_current_user, require_privilege, audit
from olt_client import OLTClient
from olt_manager import olt_locked
from zxan_parser import parse_running_config

router = APIRouter(prefix="/api/v1/olts", tags=["config"])


def _parse_vlan_summary(output: str) -> list:
    """Parse output 'show vlan summary' → list VLAN ID.
    Contoh: '1-2,15,101' → [1, 2, 15, 101]
    """
    import re as _re
    vlans = set()
    # Cari baris berisi detail, biasanya setelah "Details are following:"
    for line in output.splitlines():
        s = line.strip()
        # Cari baris dengan pola angka, koma, dash
        if _re.match(r"^[\d,\-\s]+$", s) and "," in s:
            for part in s.split(","):
                part = part.strip()
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
def get_running_config(olt_id: int, db: Session = Depends(get_db),
                       user: User = Depends(get_current_user)):
    olt = db.query(OLT).get(olt_id)
    if not olt:
        raise HTTPException(404, "OLT tidak ditemukan")
    client = _make_client(olt)
    try:
        raw = client.get_running_config()
    except Exception as e:
        raise HTTPException(500, f"Gagal ambil config: {e}")
    parsed = parse_running_config(raw)
    return {"raw": raw, "parsed": parsed}


@router.post("/{olt_id}/config/preview")
def preview_config(olt_id: int, req: ConfigPreviewRequest,
                   db: Session = Depends(get_db),
                   user: User = Depends(get_current_user)):
    olt = db.query(OLT).get(olt_id)
    if not olt:
        raise HTTPException(404, "OLT tidak ditemukan")
    client = _make_client(olt)
    result = client.validate_commands(req.commands)
    return {"commands": req.commands, "validation": result}


@router.post("/{olt_id}/config/apply")
@olt_locked
def apply_config(olt_id: int, req: ConfigApplyRequest,
                 db: Session = Depends(get_db),
                 user: User = Depends(require_privilege(15))):
    olt = db.query(OLT).get(olt_id)
    if not olt:
        raise HTTPException(404, "OLT tidak ditemukan")
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
        output = client.run_config(req.commands)
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
def list_versions(olt_id: int, db: Session = Depends(get_db),
                  user: User = Depends(get_current_user)):
    versions = db.query(ConfigVersion).filter(
        ConfigVersion.olt_id == olt_id
    ).order_by(ConfigVersion.created_at.desc()).all()
    return [{"id": v.id, "version": v.version, "author": v.author,
             "comment": v.comment, "created_at": v.created_at} for v in versions]


@router.get("/{olt_id}/vlans")
def list_vlans(olt_id: int, db: Session = Depends(get_db),
               user: User = Depends(get_current_user)):
    olt = db.query(OLT).get(olt_id)
    if not olt:
        raise HTTPException(404, "OLT tidak ditemukan")

    # Sync VLAN dari OLT dulu
    client = _make_client(olt)

    try:
        conn = client._connect()
        try:
            out = conn.send_command_timing("show vlan summary", read_timeout=15)
        finally:
            conn.disconnect()
        olt_vlan_ids = _parse_vlan_summary(out)

        # Bandingkan dengan DB
        db_vlans = {v.vlan_id: v for v in db.query(VLAN).filter(VLAN.olt_id == olt_id).all()}

        # Tambah yang ada di OLT tapi belum di DB
        for vid in olt_vlan_ids:
            if vid not in db_vlans:
                db.add(VLAN(olt_id=olt_id, vlan_id=vid, name=f"VLAN{vid}"))

        # Hapus dari DB yang sudah tidak ada di OLT
        for vid, v in db_vlans.items():
            if vid not in olt_vlan_ids:
                db.delete(v)

        db.commit()
    except Exception as e:
        print(f"[VLAN SYNC] {e}")

    vlans = db.query(VLAN).filter(VLAN.olt_id == olt_id).order_by(VLAN.vlan_id).all()
    return [{"id": v.id, "vlan_id": v.vlan_id, "name": v.name,
             "description": v.description} for v in vlans]


@router.post("/{olt_id}/vlans")
@olt_locked
def create_vlan(olt_id: int, req: VLANCreate, db: Session = Depends(get_db),
                user: User = Depends(require_privilege(10))):
    olt = db.query(OLT).get(olt_id)
    if not olt:
        raise HTTPException(404, "OLT tidak ditemukan")
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
        out = client.run_config(commands)
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


@router.delete("/{olt_id}/vlans/{vlan_id}")
@olt_locked
def delete_vlan(olt_id: int, vlan_id: int, db: Session = Depends(get_db),
                user: User = Depends(require_privilege(10))):
    olt = db.query(OLT).get(olt_id)
    if not olt:
        raise HTTPException(404, "OLT tidak ditemukan")
    v = db.query(VLAN).filter(VLAN.olt_id == olt_id, VLAN.vlan_id == vlan_id).first()
    if not v:
        raise HTTPException(404, "VLAN tidak ditemukan")

    client = _make_client(olt)

    # 1. Kirim ke OLT
    commands = [
        "configure terminal",
        "vlan database",
        f"no vlan {vlan_id}",
        "exit",
        "end",
        "write",
    ]
    try:
        out = client.run_config(commands)
        print(f"[VLAN DELETE] {out[:300]}")
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