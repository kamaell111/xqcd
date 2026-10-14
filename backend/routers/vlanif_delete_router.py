"""DELETE VLANIF. Timpa file: backend/routers/vlanif_delete_router.py

POST /api/v1/olts/{id}/vlanif/delete   body: {vlan, confirm_ip, dry_run=true}
Tulis nyata butuh: dry_run=false + env VLANIF_WRITE_ENABLED=1 (flag yang sama dengan add/edit).
Env VLANIF_PROTECTED_VLANS="2,..." = VLANIF yang tidak boleh dihapus.
Memakai StrictConfigMixin (read_strict / apply_strict) milik OLTClient.
"""
import importlib
import os
from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel
from sqlalchemy.orm import Session

from database import get_db
from models import OLT, User
from olt_client import OLTClient
from olt_manager import olt_locked
from tenancy import require_olt_access
from auth import require_privilege, audit
import vlanif_delete_core as core

router = APIRouter(prefix="/api/v1/olts", tags=["vlanif-delete"])

BRIEF_CMD = "show ip interface brief"
ROUTE_CMD = "show ip route"


def _make_client(olt: OLT) -> OLTClient:
    client = OLTClient(host=olt.ip_address, username=olt.username,
                       password=olt.password, enable_password=olt.enable_password,
                       port=olt.port, protocol=olt.protocol)
    if not (hasattr(client, "read_strict") and hasattr(client, "apply_strict")):
        raise RuntimeError("OLTClient belum memakai StrictConfigMixin (class OLTClient(StrictConfigMixin))")
    return client


def _brief_parser():
    """parse_ip_interface_brief_all: cari di modul yang mungkin menyimpannya."""
    for mod in ("vlanif_core", "vlanif", "routers.vlanif"):
        try:
            return importlib.import_module(mod).parse_ip_interface_brief_all
        except (ImportError, AttributeError):
            continue
    raise RuntimeError("parse_ip_interface_brief_all tidak ditemukan (cek: grep -rn 'def parse_ip_interface_brief_all' backend)")


def _read_state(olt: OLT, vlan: int):
    """1 sesi baca ke OLT -> (baris VLANIF atau None, set VLAN pemegang default route).
    Error apa pun di sini = penghapusan DITOLAK (fail-closed)."""
    outs = _make_client(olt).read_strict([BRIEF_CMD, ROUTE_CMD], core.assert_read_allowed)
    row = core.pick_vlanif(_brief_parser()(outs[BRIEF_CMD]), vlan)
    routes = core.parse_default_route_vlans(outs[ROUTE_CMD])
    return row, routes


def _verify_gone(vlan: int):
    """Verifikasi longgar: gagal HANYA kalau vlanN jelas masih ada. Kalau output tak terbaca, dilewati."""
    def fn(outs):
        try:
            rows = _brief_parser()(outs[BRIEF_CMD])
        except Exception:
            return True, "verifikasi dilewati (output tidak terbaca)"
        if any(str(r.get("vlan_id")) == str(vlan) for r in rows or []):
            return False, f"vlan{vlan} masih ada di OLT"
        return True, "ok"
    return fn


def _apply(olt: OLT, vlan: int, commands: list):
    return _make_client(olt).apply_strict(
        commands, rollback=[], verify_cmds=[BRIEF_CMD],
        verify_fn=_verify_gone(vlan), validator=core.assert_any_allowed,
    )


class DeleteReq(BaseModel):
    vlan: int
    confirm_ip: str
    dry_run: bool = True


@router.post("/{olt_id}/vlanif/delete")
@olt_locked
def delete_vlanif(olt_id: int, body: DeleteReq,
                  db: Session = Depends(get_db),
                  olt: OLT = Depends(require_olt_access),
                  user: User = Depends(require_privilege(15))):
    try:
        protected = core.protected_vlans(os.environ.get("VLANIF_PROTECTED_VLANS", ""))
    except ValueError as e:
        raise HTTPException(500, str(e))
    try:
        row, routes = _read_state(olt, body.vlan)
    except Exception as e:
        raise HTTPException(502, f"Gagal membaca VLANIF/route dari OLT; penghapusan ditolak ({e})")

    why = core.preflight(body.vlan, row, olt.ip_address, routes, protected)
    if why:
        raise HTTPException(409, "Tidak boleh dihapus: " + "; ".join(why))
    if not core.confirm_ok(body.confirm_ip, body.vlan, row):
        raise HTTPException(400, f"Konfirmasi salah: ketik persis '{core.expected_confirm(body.vlan, row)}'")

    commands = core.build_delete(body.vlan)
    tag = f"vlan{body.vlan} {core.expected_confirm(body.vlan, row)}"
    if body.dry_run:
        audit(db, user.username, "delete_vlanif_dryrun", tag)
        return {"ok": True, "dry_run": True, "status": "preview", "commands": commands}
    if os.environ.get("VLANIF_WRITE_ENABLED") != "1":
        raise HTTPException(403, "Fitur tulis VLANIF dimatikan (set VLANIF_WRITE_ENABLED=1 di backend)")

    audit(db, user.username, "delete_vlanif_start", tag)
    try:
        log = _apply(olt, body.vlan, commands)
    except Exception as e:
        print("[VLANIF-DELETE] GAGAL:", e, "\n" + "\n".join(getattr(e, "log", [])))
        audit(db, user.username, "delete_vlanif_failed", f"{tag} | {e}")
        raise HTTPException(500, f"OLT menolak/gagal: {e}. Cek vlan{body.vlan} langsung di OLT.")
    print("[VLANIF-DELETE] OK:", tag, "\n" + "\n".join(log))
    audit(db, user.username, "delete_vlanif_ok", tag)
    return {"ok": True, "status": "deleted", "message": f"VLANIF vlan{body.vlan} dihapus"}
