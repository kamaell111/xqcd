"""VLANIF create/edit: preview + apply (sinkron, tanpa job/snapshot).

POST /api/v1/olts/{id}/vlanif/preview   body: {action, vlan, mode, ip, mask, description}
POST /api/v1/olts/{id}/vlanif/apply     body: {... , dry_run=true, confirm_text, plan_token}

Tulis nyata butuh SEMUA: dry_run=false + env VLANIF_WRITE_ENABLED=1 + confirm_text == VLAN ID
  + plan_token cocok dengan state OLT saat ini + preflight lolos (build_plan).
Env: VLANIF_PROTECTED_VLANS="2,..."  VLANIF_ALLOW_DHCP=1 (default mati)
     VLANIF_EDIT_REQUIRE_ROUTE_CHECK=1 (default MATI; cek route belum didukung)
"""
import logging
import os
import time
from typing import Literal

from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel, Field
from sqlalchemy.orm import Session

import vlanif_core as vcore
import vlanif_flow as vflow
from auth import audit, require_privilege
from database import get_db
from models import OLT, User
from olt_client import OLTClient
from olt_manager import olt_locked
from tenancy import require_olt_access
from vlanif_delete_core import protected_vlans

router = APIRouter(prefix="/api/v1/olts", tags=["vlanif"])
log = logging.getLogger("vlanif")

BRIEF_CMD = "show ip interface brief"
VSUM_CMD = "show vlan summary"
ROUTE_CMD = "show ip route"


class VlanifReq(BaseModel):
    action: Literal["create", "edit"]
    vlan: int = Field(ge=1, le=4094)
    mode: str = "static"
    ip: str | None = None
    mask: str | None = None
    description: str = ""
    dry_run: bool = True
    confirm_text: str = ""
    plan_token: str = ""


def _flags() -> dict:
    return {
        "write_enabled": os.environ.get("VLANIF_WRITE_ENABLED") == "1",
        "allow_dhcp": os.environ.get("VLANIF_ALLOW_DHCP") == "1",
        "require_route": os.environ.get("VLANIF_EDIT_REQUIRE_ROUTE_CHECK") == "1",
    }


def _reject_control_chars(body: VlanifReq) -> None:
    for name in ("mode", "ip", "mask", "description"):
        v = getattr(body, name)
        if v and any(ord(ch) < 32 or ord(ch) == 127 for ch in v):
            raise HTTPException(422, f"Field '{name}' mengandung karakter kontrol")


def _make_client(olt: OLT) -> OLTClient:
    client = OLTClient(host=olt.ip_address, username=olt.username,
                       password=olt.password, enable_password=olt.enable_password,
                       port=olt.port, protocol=olt.protocol)
    if not (hasattr(client, "read_strict") and hasattr(client, "apply_strict")):
        raise RuntimeError("OLTClient belum memakai StrictConfigMixin")
    return client


def _close(client) -> None:
    if client is None:
        return
    for name in ("disconnect", "close"):
        fn = getattr(client, name, None)
        if callable(fn):
            try:
                fn()
            except Exception:
                pass
            return


def _cfg_cmd(vlan: int) -> str:
    tpl = getattr(vcore, "SHOW_CFG", None)
    if not tpl:
        raise RuntimeError("vlanif_core.SHOW_CFG tidak ditemukan")
    return tpl.format(n=vlan)


def _only(allowed):
    allowed = set(allowed)
    def check(cmds, *_a, **_k):
        items = [cmds] if isinstance(cmds, str) else list(cmds)
        for c in items:
            if str(c).strip() not in allowed:
                raise ValueError(f"perintah baca tidak diizinkan: {c!r}")
    return check


def _read_olt_state(olt: OLT, body: VlanifReq, cfg_cmd: str, want_route: bool) -> dict:
    cmds = [BRIEF_CMD, cfg_cmd, VSUM_CMD] + ([ROUTE_CMD] if want_route else [])
    outs = _make_client(olt).read_strict(cmds, _only(cmds))
    return {"brief": outs[BRIEF_CMD], "cfg": outs[cfg_cmd], "vsum": outs[VSUM_CMD],
            "route": outs[ROUTE_CMD] if want_route else ""}


def _build_plan(olt: OLT, body: VlanifReq) -> dict:
    f = _flags()
    try:
        protected = protected_vlans(os.environ.get("VLANIF_PROTECTED_VLANS", ""))
        cfg_cmd = _cfg_cmd(body.vlan)
    except (ValueError, RuntimeError) as e:
        raise HTTPException(500, str(e))
    try:
        st = _read_olt_state(olt, body, cfg_cmd, want_route=(body.action == "edit" and f["require_route"]))
    except Exception as e:
        log.exception("vlanif baca OLT olt=%s", olt.id)
        raise HTTPException(502, f"Gagal membaca OLT; tidak ada yang diubah ({e})")
    try:
        return vcore.build_plan(
            olt_id=olt.id, action=body.action, vlan=body.vlan,
            new_raw={"mode": body.mode, "ip": body.ip, "mask": body.mask, "description": body.description},
            connect_host=olt.ip_address,
            brief_text=st["brief"], cfg_text=st["cfg"], vsum_text=st["vsum"], route_text=st["route"],
            protected=protected, allow_dhcp=f["allow_dhcp"], require_route_check=f["require_route"])
    except ValueError as e:
        raise HTTPException(422, str(e))
    except Exception as e:
        log.exception("vlanif build_plan olt=%s", olt.id)
        raise HTTPException(500, f"Gagal menyusun rencana: {e}")


def _public(plan: dict, body: VlanifReq) -> dict:
    return {"ok": plan["ok"], "checks": plan["checks"], "commands": plan["commands"],
            "old": plan.get("old"), "plan_token": plan["plan_token"],
            "write_enabled": _flags()["write_enabled"], "confirm_hint": str(body.vlan),
            "dry_run": True, "note": "Dry-run: tidak ada yang ditulis ke OLT."}


@router.post("/{olt_id}/vlanif/preview")
@olt_locked
def vlanif_preview(olt_id: int, body: VlanifReq,
                   olt: OLT = Depends(require_olt_access),
                   user: User = Depends(require_privilege(10))):
    _reject_control_chars(body)
    return _public(_build_plan(olt, body), body)


@router.post("/{olt_id}/vlanif/apply")
@olt_locked
def vlanif_apply(olt_id: int, body: VlanifReq,
                 db: Session = Depends(get_db),
                 olt: OLT = Depends(require_olt_access),
                 user: User = Depends(require_privilege(15))):
    _reject_control_chars(body)
    if not body.dry_run:
        if not _flags()["write_enabled"]:
            raise HTTPException(403, "Tulis VLANIF dimatikan (VLANIF_WRITE_ENABLED != 1). Hanya dry-run.")
        if body.confirm_text.strip() != str(body.vlan):
            raise HTTPException(400, f"Konfirmasi salah: ketik VLAN ID persis ('{body.vlan}').")
        if not body.plan_token:
            raise HTTPException(400, "plan_token wajib (jalankan preview dulu).")

    plan = _build_plan(olt, body)
    tag = f"{body.action} vlan{body.vlan}"
    if body.dry_run:
        audit(db, user.username, "vlanif_dryrun", tag)
        return _public(plan, body)

    if not plan["ok"]:
        failed = "; ".join(str(c.get("name")) for c in plan["checks"] if not c.get("ok"))
        raise HTTPException(409, "Preflight gagal: " + failed)
    if plan["plan_token"] != body.plan_token:
        raise HTTPException(409, "State OLT berubah sejak preview; ulangi preview.")

    audit(db, user.username, "vlanif_start", tag)
    client_a = None
    try:
        client_a = _make_client(olt)
        res = vflow.apply_change(
            action=body.action, vlan=body.vlan, plan=plan, client_a=client_a,
            make_client=lambda: _make_client(olt), sleep=time.sleep,
            log=lambda m: log.warning("vlanif olt=%s: %s", olt.id, m))
    except Exception as e:
        log.exception("vlanif apply olt=%s", olt.id)
        audit(db, user.username, "vlanif_failed", f"{tag} | {e}")
        raise HTTPException(500, f"Gagal tak terduga: {e}. Periksa vlan{body.vlan} langsung di OLT.")
    finally:
        _close(client_a)

    status = res.get("status")
    audit(db, user.username, f"vlanif_{status}", tag)
    return {"ok": status == "applied", "status": status,
            "detail": res.get("detail", ""), "manual_cmds": res.get("manual_cmds") or []}
