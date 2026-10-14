"""Edit VLAN per ONU (ZTE C320). Tempel sebagai file baru: backend/routers/onu_vlan_router.py

Aman-secara-default:
  - preview  : BACA-SAJA (show), tidak menulis apa pun ke OLT
  - apply    : dry_run=True secara default; tulis nyata butuh dry_run=False
               DAN env ONU_VLAN_WRITE_ENABLED=1 di proses backend
               DAN serial ONU diketik ulang (confirm_serial)
"""
import os
from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel
from sqlalchemy.orm import Session

from database import get_db
from models import OLT, ONU, Interface, User
from olt_client import OLTClient
from olt_manager import olt_locked
from tenancy import require_olt_access
from auth import require_privilege, audit
from olt_strict import StrictConfigError
import onu_vlan_core as core
from olt_locks import olt_lock

router = APIRouter(prefix="/api/v1/onu", tags=["onu-vlan"])


def _make_client(olt: OLT) -> OLTClient:
    return OLTClient(host=olt.ip_address, username=olt.username,
                     password=olt.password, enable_password=olt.enable_password,
                     port=olt.port, protocol=olt.protocol)


class PreviewReq(BaseModel):
    new_vlan: int


class ApplyReq(BaseModel):
    new_vlan: int
    confirm_serial: str
    dry_run: bool = True
    save: bool = False


def _load_onu(db: Session, olt: OLT, onu_pk: int) -> ONU:
    # Frontend kirim o.onu_id (nomor ONU di PON), konsisten dengan endpoint existing
    onu = db.query(ONU).filter(ONU.olt_id == olt.id, ONU.onu_id == onu_pk).first()
    if not onu:
        raise HTTPException(404, "ONU tidak ditemukan")
    if not onu.interface_name or not core.IFACE_RE.fullmatch(onu.interface_name):
        raise HTTPException(422, f"interface_name ONU tidak valid: {onu.interface_name!r}")
    return onu


def _make_plan(db: Session, olt: OLT, onu: ONU, new_vlan: int):
    iface = onu.interface_name
    shows = [f"show running-config interface {iface}",
             f"show onu running config {iface}",
             "show vlan summary"]
    try:
        outs = _make_client(olt).read_strict(shows, core.assert_allowed)
    except core.VlanError as e:
        raise HTTPException(500, str(e))
    except Exception as e:
        raise HTTPException(502, f"Gagal membaca OLT: {e}")
    try:
        cur = core.parse_current(outs[shows[0]], outs[shows[1]])
        olt_vlans = core.extract_vlan_list(outs[shows[2]])
    except core.VlanError as e:
        return None, [str(e)], {}
    uplinks = []
    for i in db.query(Interface).filter(Interface.olt_id == olt.id).all():
        if (i.switchport_mode or "").lower() == "trunk" and i.vlans:
            try:
                uplinks.append(core.parse_vlan_list(i.vlans))
            except core.VlanError:
                pass
    errors = core.validate_target(new_vlan, cur, olt_vlans, uplinks)
    plan = None if errors else core.build_plan(iface, cur, new_vlan)
    info = {"current_vlan": cur.vlan, "service_port": cur.sp_id,
            "olt_vlans": core.format_vlan_list(olt_vlans)}
    return plan, errors, info


@router.post("/{olt_id}/{onu_id}/vlan/preview")
@olt_locked
def preview_onu_vlan(olt_id: int, onu_id: int, body: PreviewReq,
                     db: Session = Depends(get_db),
                     olt: OLT = Depends(require_olt_access),
                     user: User = Depends(require_privilege(10))):
    onu = _load_onu(db, olt, onu_id)
    plan, errors, info = _make_plan(db, olt, onu, body.new_vlan)
    return {
        "onu": {"id": onu.id, "name": onu.name, "serial_number": onu.serial_number,
                "interface_name": onu.interface_name},
        "new_vlan": body.new_vlan, "errors": errors, **info,
        "commands": plan.apply if plan else [],
        "rollback": plan.rollback if plan else [],
        "write_enabled": os.environ.get("ONU_VLAN_WRITE_ENABLED") == "1",
    }


@router.post("/{olt_id}/{onu_id}/vlan/apply")
@olt_locked
def apply_onu_vlan(olt_id: int, onu_id: int, body: ApplyReq,
                   db: Session = Depends(get_db),
                   olt: OLT = Depends(require_olt_access),
                   user: User = Depends(require_privilege(15))):
    onu = _load_onu(db, olt, onu_id)
    if (body.confirm_serial or "").strip().upper() != (onu.serial_number or "").upper():
        raise HTTPException(400, "Serial konfirmasi tidak cocok dengan ONU ini")
    plan, errors, _info = _make_plan(db, olt, onu, body.new_vlan)
    if errors:
        raise HTTPException(422, "; ".join(errors))

    tag = f"{onu.serial_number} {plan.old_vlan}->{plan.new_vlan}"
    if body.dry_run:
        audit(db, user.username, "edit_onu_vlan_dryrun", tag)
        return {"ok": True, "dry_run": True, "commands": plan.apply, "rollback": plan.rollback}
    if os.environ.get("ONU_VLAN_WRITE_ENABLED") != "1":
        raise HTTPException(403, "Fitur tulis VLAN ONU dimatikan (set ONU_VLAN_WRITE_ENABLED=1 di backend)")

    audit(db, user.username, "edit_onu_vlan_start", tag)
    try:
        log = _make_client(olt).apply_strict(
            plan.apply, plan.rollback, plan.verify,
            lambda outs: core.check_after(outs[plan.verify[0]], outs[plan.verify[1]], plan.new_vlan),
            core.assert_allowed,
            save_cmd="write" if body.save else None,
        )
    except StrictConfigError as e:
        print("[ONU-VLAN] GAGAL:", e, "\n" + "\n".join(e.log))
        audit(db, user.username, "edit_onu_vlan_failed", f"{tag} | {e}")
        raise HTTPException(500, f"{e} (rollback terkirim: {'ya' if e.rollback_sent else 'TIDAK'}). "
                                 "Cek ONU ini langsung di OLT.")
    except core.VlanError as e:
        raise HTTPException(500, str(e))

    print("[ONU-VLAN] OK:", tag, "\n" + "\n".join(log))
    onu.user_vlan = plan.new_vlan
    onu.vlan = plan.new_vlan
    db.commit()
    audit(db, user.username, "edit_onu_vlan_ok", tag + (" (saved)" if body.save else " (belum write)"))
    return {"ok": True, "message": f"VLAN ONU diubah {plan.old_vlan} -> {plan.new_vlan}",
            "saved": body.save}
