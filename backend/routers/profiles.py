"""
routers/profiles.py — Fase 2: list + sync profile GPON (T-CONT & Traffic), READ-ONLY ke OLT.

Endpoint:
  GET  /api/v1/olts/{olt_id}/profiles/{kind}        kind: tcont | traffic   (semua user login)
  POST /api/v1/olts/{olt_id}/profiles/{kind}/sync   (butuh PRIV_SYNC)

Pola sama dengan sync VLAN, ditambah:
  - parser melempar ParseError  -> cache DB TIDAK disentuh
  - output terpotong paging     -> ParseError (jangan hapus profile gara-gara daftar parsial)
  - status sync per (olt, kind) disimpan di gpon_profile_sync
  - throttle: sync berulang < SYNC_MIN_INTERVAL detik mengembalikan cache
"""
import json
import os
import re
from datetime import datetime, timedelta
from typing import Any, Dict, Optional, Tuple

from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel, StrictInt
from sqlalchemy import func
from sqlalchemy.orm import Session

from database import get_db
from models import OLT, User, ONU, GponProfile, GponProfileSync
from auth import audit, get_current_user, require_privilege
from tenancy import require_olt_access
from olt_client import OLTClient
from olt_locks import olt_lock
from olt_manager import olt_locked
import profile_executor as px


def _make_client(olt: OLT) -> OLTClient:
    return OLTClient(host=olt.ip_address, username=olt.username,
                     password=olt.password, enable_password=olt.enable_password,
                     port=olt.port, protocol=olt.protocol)

import profile_driver_c320 as driver_c320

router = APIRouter(prefix="/api/v1/olts", tags=["profiles"])

PRIV_SYNC = 15                              # level yang boleh sync; turunkan ke level Operator bila perlu
SYNC_MIN_INTERVAL = 10                      # detik
STALE_AFTER = timedelta(hours=6)            # badge "data lama" di UI


def _get_driver(olt: OLT):
    """Multi-OLT: pilih driver berdasarkan model OLT. Sekarang baru C320."""
    # TODO saat ada model lain: if "C600" in (olt.model or ""): return driver_c600
    return driver_c320


def _check_kind(kind: str) -> None:
    if kind not in driver_c320.KINDS:
        raise HTTPException(404, "Jenis profile tidak dikenal (pakai: tcont atau traffic)")


# ------------------------------------------------------------------ DB helpers
def _get_sync_row(db: Session, olt_id: int, kind: str) -> Optional[GponProfileSync]:
    return (
        db.query(GponProfileSync)
        .filter(GponProfileSync.olt_id == olt_id, GponProfileSync.kind == kind)
        .first()
    )


def _set_status(db: Session, olt_id: int, kind: str, status: str,
                error: Optional[str], synced_at: Optional[datetime] = None) -> None:
    """Tidak commit. synced_at hanya diisi saat sukses (= waktu sukses terakhir)."""
    row = _get_sync_row(db, olt_id, kind)
    if row is None:
        row = GponProfileSync(olt_id=olt_id, kind=kind)
        db.add(row)
    row.status = status
    row.error_msg = error
    if synced_at is not None:
        row.synced_at = synced_at


def _apply_sync(db: Session, olt_id: int, kind: str, items: list) -> None:
    """Upsert + hapus yang hilang dari OLT, dalam SATU transaksi."""
    now = datetime.utcnow()
    existing = {
        p.name: p
        for p in db.query(GponProfile)
        .filter(GponProfile.olt_id == olt_id, GponProfile.kind == kind)
        .all()
    }
    seen = set()
    for it in items:
        name = it["name"]
        params = {k: v for k, v in it.items() if k != "name"}
        pj = json.dumps(params, sort_keys=True)
        seen.add(name)
        row = existing.get(name)
        if row is None:
            db.add(GponProfile(olt_id=olt_id, kind=kind, name=name,
                               params_json=pj, synced_at=now))
        else:
            row.params_json = pj
            row.synced_at = now
    for name, row in existing.items():
        if name not in seen:
            print(f"[PROFILE SYNC] olt={olt_id} {kind}: hapus '{name}' (sudah tidak ada di OLT)")
            db.delete(row)
    _set_status(db, olt_id, kind, "ok", None, now)
    db.commit()


# ------------------------------------------------------------------ OLT access
def _fetch_unlocked(olt: OLT, kind: str) -> list:
    """Baca + parse TANPA lock. Hanya untuk kode yang sudah memegang lock (@olt_locked):
    lock tidak reentrant, jadi memanggil _fetch_from_olt dari sana = deadlock."""
    driver = _get_driver(olt)
    cmd = driver.SHOW_COMMANDS[kind]
    client = _make_client(olt)
    conn = client._connect()
    try:
        out = conn.send_command_timing(cmd, read_timeout=15, last_read=1.0)
    finally:
        conn.disconnect()
    return driver.parse(kind, out)


def _fetch_from_olt(olt: OLT, kind: str) -> list:
    with olt_lock(olt.id):                   # serialisasi sesi telnet per OLT
        return _fetch_unlocked(olt, kind)


def _run_sync(db: Session, olt: OLT, kind: str) -> Tuple[bool, Optional[str]]:
    """Ambil dari OLT lalu tulis cache. Gagal = cache lama utuh, status 'error'."""
    try:
        items = _fetch_from_olt(olt, kind)
        _apply_sync(db, olt.id, kind, items)
        return True, None
    except Exception as e:                   # noqa: BLE001 - sengaja luas: jangan sampai merusak cache
        db.rollback()
        msg = f"{type(e).__name__}: {e}"[:300]
        print(f"[PROFILE SYNC] olt={olt.id} kind={kind} GAGAL: {msg}")
        try:
            _set_status(db, olt.id, kind, "error", msg)
            db.commit()
        except Exception:                    # noqa: BLE001
            db.rollback()
        return False, msg


# --------------------------------------------------------------------- payload
def _payload(db: Session, olt_id: int, kind: str) -> Dict[str, Any]:
    rows = (
        db.query(GponProfile)
        .filter(GponProfile.olt_id == olt_id, GponProfile.kind == kind)
        .all()
    )
    items = []
    for r in rows:
        try:
            params = json.loads(r.params_json or "{}")
        except ValueError:
            params = {}
        items.append({**params, "id": r.id, "name": r.name})   # id/name tidak bisa tertimpa
    items.sort(key=lambda x: x["name"].lower())

    st = _get_sync_row(db, olt_id, kind)
    synced_at = st.synced_at if st else None
    stale = bool(synced_at) and (datetime.utcnow() - synced_at) > STALE_AFTER
    return {
        "kind": kind,
        "items": items,
        "synced_at": (synced_at.isoformat() + "Z") if synced_at else None,
        "status": st.status if st else "never",     # never | ok | error
        "error": st.error_msg if st else None,
        "stale": stale,
    }


# ------------------------------------------------------------------- endpoints
@router.get("/{olt_id}/profiles/{kind}")
def list_profiles(kind: str,
                  db: Session = Depends(get_db),
                  olt: OLT = Depends(require_olt_access),
                  user: User = Depends(get_current_user)):
    """Baca dari cache. Hanya kalau BELUM PERNAH sync, ambil dari OLT satu kali."""
    _check_kind(kind)
    if _get_sync_row(db, olt.id, kind) is None:
        _run_sync(db, olt, kind)             # gagal pun tidak fatal; status 'error' ikut di payload
    return _payload(db, olt.id, kind)


@router.post("/{olt_id}/profiles/{kind}/sync")
def sync_profiles(kind: str,
                  db: Session = Depends(get_db),
                  olt: OLT = Depends(require_olt_access),
                  user: User = Depends(require_privilege(PRIV_SYNC))):
    _check_kind(kind)
    st = _get_sync_row(db, olt.id, kind)
    if (st and st.status == "ok" and st.synced_at
            and (datetime.utcnow() - st.synced_at).total_seconds() < SYNC_MIN_INTERVAL):
        data = _payload(db, olt.id, kind)
        data["throttled"] = True
        return data

    ok, err = _run_sync(db, olt, kind)
    if not ok:
        raise HTTPException(502, f"Sync dari OLT gagal: {err}")
    data = _payload(db, olt.id, kind)
    data["throttled"] = False
    return data


# =============================================================================
# FASE 3a: create / delete profile (MENULIS ke OLT)
# Pagar: PROFILE_WRITE_ENABLED=1 -> dry_run=false -> evaluasi ulang di tiap request
#        -> konfirmasi nama (delete) -> executor memverifikasi kondisi LIVE.
# =============================================================================
PRIV_WRITE = 15
_ADMIN = require_privilege(PRIV_WRITE)

_N = r"[A-Za-z0-9_.-]{1,64}"
_WRITE_OK = [re.compile(p) for p in (
    r"configure terminal", r"gpon", r"exit", r"end",
    r"show gpon profile (tcont|traffic)",
    rf"profile tcont {_N} type [124] (fixed|assured|maximum) [0-9]+( priority [0-9]+)?",
    rf"profile traffic {_N} sir [0-9]+( pir [0-9]+)?( cbs [0-9]+)?( pbs [0-9]+)?",
    rf"no profile (tcont|traffic) {_N}",
)]


def _write_enabled() -> bool:
    return os.environ.get("PROFILE_WRITE_ENABLED") == "1"


def _profile_validator(cmds, *_a, **_k):
    items = [cmds] if isinstance(cmds, str) else list(cmds)
    for c in items:
        s = str(c)
        if "\n" in s or "\r" in s or not any(p.fullmatch(s.strip()) for p in _WRITE_OK):
            raise ValueError(f"perintah profile tidak diizinkan: {s!r}")


def _sequence(cmds: list) -> list:
    return ["configure terminal", "gpon", *cmds, "exit", "exit"]


def _make_sender(olt: OLT, kind: str):
    show = _get_driver(olt).SHOW_COMMANDS[kind]

    def send(cmds):
        client = _make_client(olt)
        if not hasattr(client, "apply_strict"):
            raise RuntimeError("OLTClient belum memakai StrictConfigMixin")
        return client.apply_strict(
            _sequence(cmds), rollback=[], verify_cmds=[show],
            verify_fn=lambda outs: (True, "ok"),
            validator=_profile_validator)
    return send


def _try_fetch(olt: OLT, kind: str):
    try:
        return _fetch_unlocked(olt, kind), None
    except Exception as e:
        return None, f"{type(e).__name__}: {e}"[:300]


def _sync_cache(db: Session, olt_id: int, kind: str, live) -> None:
    if not live:
        return
    try:
        _apply_sync(db, olt_id, kind, live)
    except Exception as e:
        db.rollback()
        print(f"[PROFILE] sync cache olt={olt_id} kind={kind} gagal: {e}")


def _usage(db: Session, olt_id: int, kind: str, name: str) -> Dict[str, Any]:
    if kind != "tcont":
        return {"kind": kind, "checked": False, "blocking_count": 0, "samples": [],
                "message": "Pemakaian Traffic profile tidak bisa dicek dari DB aplikasi; cek manual di OLT."}
    q = db.query(ONU).filter(ONU.olt_id == olt_id, func.lower(ONU.tcont) == name.lower())
    n = q.count()
    samples = [{"name": o.name, "interface": o.interface_name} for o in q.limit(5).all()]
    return {"kind": kind, "checked": True, "blocking_count": n, "samples": samples,
            "message": (f"{n} ONU di DB aplikasi memakai T-CONT ini." if n
                        else "Tidak ada ONU di DB aplikasi yang memakai T-CONT ini.")}


def _plan_public(plan: Dict[str, Any], *, dry_run: bool) -> Dict[str, Any]:
    return {"name": plan["name"], "dry_run": dry_run, "allowed": plan["allowed"],
            "ready": plan["ready"], "needs": plan["needs"], "checks": plan["checks"],
            "commands": plan["commands"],
            "sequence": _sequence(plan["commands"]) if plan["commands"] else [],
            "write_enabled": _write_enabled()}


def _why(plan: Dict[str, Any]) -> str:
    return "; ".join(c["label"] + (f" ({c['detail']})" if c.get("detail") else "")
                     for c in plan["checks"] if not c["ok"])


def _exec_http(e) -> HTTPException:
    return HTTPException(e.status, e.message + (f" [{e.detail}]" if e.detail else ""))


def _check_name_or_422(driver, name: str) -> None:
    if not driver.NAME_RE.fullmatch(name):
        raise HTTPException(422, "Nama profile tidak valid (1-64 karakter: huruf, angka, _ . -)")


_PARAM_KEYS = ("name", "type", "fixed", "assured", "maximum", "priority", "sir", "pir", "cbs", "pbs")


class ProfileCreateReq(BaseModel):
    name: str
    type: Optional[StrictInt] = None
    fixed: Optional[StrictInt] = None
    assured: Optional[StrictInt] = None
    maximum: Optional[StrictInt] = None
    priority: Optional[StrictInt] = None
    sir: Optional[StrictInt] = None
    pir: Optional[StrictInt] = None
    cbs: Optional[StrictInt] = None
    pbs: Optional[StrictInt] = None
    dry_run: bool = True


class ProfileDeleteReq(BaseModel):
    confirm_name: Optional[str] = None
    manual_ack: bool = False
    dry_run: bool = True


@router.post("/{olt_id}/profiles/{kind}")
@olt_locked
def create_profile(olt_id: int, kind: str, body: ProfileCreateReq,
                   db: Session = Depends(get_db),
                   olt: OLT = Depends(require_olt_access),
                   user: User = Depends(_ADMIN)):
    _check_kind(kind)
    if not body.dry_run and not _write_enabled():
        raise HTTPException(403, "Fitur tulis profile dimatikan (set PROFILE_WRITE_ENABLED=1 di backend)")
    driver = _get_driver(olt)
    params = {k: getattr(body, k) for k in _PARAM_KEYS if getattr(body, k) is not None}
    live, live_err = _try_fetch(olt, kind)
    plan = px.evaluate_create(driver, kind, params, live=live, live_error=live_err,
                              write_enabled=_write_enabled())
    tag = f"{kind} {body.name}"[:120]
    if body.dry_run:
        audit(db, user.username, "profile_create_dryrun", tag)
        return _plan_public(plan, dry_run=True)
    if not plan["ready"]:
        raise HTTPException(409, "Tidak bisa dibuat: " + _why(plan))

    audit(db, user.username, "profile_create_start", tag)
    try:
        res = px.run_create(driver, kind, plan, params,
                            fetch_live=lambda: _fetch_unlocked(olt, kind),
                            send=_make_sender(olt, kind))
    except px.ExecError as e:
        _sync_cache(db, olt.id, kind, e.live)
        audit(db, user.username, "profile_create_failed", f"{tag} | {e.message}"[:300])
        raise _exec_http(e)
    except Exception as e:
        audit(db, user.username, "profile_create_error", f"{tag} | {type(e).__name__}"[:300])
        raise HTTPException(500, f"Gagal tak terduga: {type(e).__name__}: {e}. "
                                 f"Cek profile '{body.name}' langsung di OLT.")
    _sync_cache(db, olt.id, kind, res["live"])
    audit(db, user.username, "profile_create_ok", tag)
    return {"ok": True, "status": "created", "name": body.name, "warning": res["warning"],
            "message": f"Profile '{body.name}' dibuat."}


@router.post("/{olt_id}/profiles/{kind}/{name}/delete")
@olt_locked
def delete_profile(olt_id: int, kind: str, name: str, body: ProfileDeleteReq,
                   db: Session = Depends(get_db),
                   olt: OLT = Depends(require_olt_access),
                   user: User = Depends(_ADMIN)):
    _check_kind(kind)
    driver = _get_driver(olt)
    _check_name_or_422(driver, name)
    if not body.dry_run and not _write_enabled():
        raise HTTPException(403, "Fitur tulis profile dimatikan (set PROFILE_WRITE_ENABLED=1 di backend)")
    live, live_err = _try_fetch(olt, kind)
    plan = px.evaluate_delete(driver, kind, name, live=live, live_error=live_err,
                              usage=_usage(db, olt.id, kind, name),
                              write_enabled=_write_enabled(),
                              confirm_name=body.confirm_name, manual_ack=body.manual_ack)
    tag = f"{kind} {name}"
    if body.dry_run:
        audit(db, user.username, "profile_delete_dryrun", tag)
        return _plan_public(plan, dry_run=True)
    if not plan["ready"]:
        raise HTTPException(409, "Tidak bisa dihapus: " + _why(plan))

    audit(db, user.username, "profile_delete_start", tag)
    try:
        res = px.run_delete(driver, kind, plan,
                            fetch_live=lambda: _fetch_unlocked(olt, kind),
                            send=_make_sender(olt, kind))
    except px.ExecError as e:
        _sync_cache(db, olt.id, kind, e.live)
        audit(db, user.username, "profile_delete_failed", f"{tag} | {e.message}"[:300])
        raise _exec_http(e)
    except Exception as e:
        audit(db, user.username, "profile_delete_error", f"{tag} | {type(e).__name__}"[:300])
        raise HTTPException(500, f"Gagal tak terduga: {type(e).__name__}: {e}. "
                                 f"Cek profile '{name}' langsung di OLT.")
    _sync_cache(db, olt.id, kind, res["live"])
    audit(db, user.username, "profile_delete_ok", tag)
    return {"ok": True, "status": "deleted", "name": name, "warning": res["warning"],
            "message": f"Profile '{name}' dihapus."}


@router.get("/{olt_id}/profiles/{kind}/{name}/usage")
def profile_usage(kind: str, name: str,
                  db: Session = Depends(get_db),
                  olt: OLT = Depends(require_olt_access),
                  user: User = Depends(get_current_user)):
    _check_kind(kind)
    _check_name_or_422(_get_driver(olt), name)
    return _usage(db, olt.id, kind, name)

