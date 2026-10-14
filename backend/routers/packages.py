"""
routers/packages.py — Fase 4b-1: CRUD Service Package. DB ONLY, tidak menyentuh OLT.

Endpoint (prefix /api/v1/olts):
  GET    /{olt_id}/service-packages                 list             (semua user login)
  GET    /{olt_id}/service-packages/options         pilihan form     (semua user login)
  POST   /{olt_id}/service-packages                 create           (privilege 15)
  PUT    /{olt_id}/service-packages/{pkg_id}        update           (privilege 15)
  DELETE /{olt_id}/service-packages/{pkg_id}        delete           (privilege 15)
  GET    /{olt_id}/service-packages/{pkg_id}/usage  pemakaian        (semua user login)

Referensi T-CONT / Traffic / VLAN divalidasi terhadap CACHE DB (gpon_profiles, vlans),
bukan live ke OLT. Kalau cache basi, sync dulu di Deployment.
"""
import re
from datetime import datetime
from typing import Optional

from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel, StrictBool, StrictInt
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

from database import get_db
from models import OLT, User, VLAN, GponProfile, ServicePackage, AuditLog
from auth import get_current_user, require_privilege
from tenancy import require_olt_access

router = APIRouter(prefix="/api/v1/olts", tags=["service-packages"])

PRIV_WRITE = 15
_ADMIN = require_privilege(PRIV_WRITE)

ID_MAX = 1024   # batas longgar GEMPORT / Service Port / VPort; kencangkan sesuai batas C320 kalau perlu

NAME_RE = re.compile(r"[A-Za-z0-9_.-]{1,64}")
WORD_RE = re.compile(r"[A-Za-z0-9_-]{1,32}")
PATTERN_RE = re.compile(r"[A-Za-z0-9_.@{}-]{1,64}")      # tanpa spasi / karakter kontrol
TOKEN_RE = re.compile(r"\{([A-Za-z0-9_]*)\}")
ALLOWED_TOKENS = {"SN", "RANDOM8"}

FIELDS = (
    "name", "description", "tcont_profile", "traffic_profile",
    "gemport_id", "service_port", "vport", "user_vlan", "vlan",
    "pppoe_user_pattern", "pppoe_password_pattern", "pppoe_nat",
    "firewall_level", "security_mgmt_mode", "wan_service",
)


class PackageIn(BaseModel):
    name: str
    description: Optional[str] = None
    tcont_profile: str
    traffic_profile: str
    gemport_id: StrictInt = 1
    service_port: StrictInt = 1
    vport: StrictInt = 1
    user_vlan: StrictInt
    vlan: StrictInt
    pppoe_user_pattern: Optional[str] = None      # None = belum diisi -> default model {SN}; "" = sengaja kosong
    pppoe_password_pattern: Optional[str] = None  # None = belum diisi -> default model {RANDOM8}; "" = sengaja kosong
    pppoe_nat: StrictBool = True
    firewall_level: str = "low"
    security_mgmt_mode: str = "forward"
    wan_service: str = "internet"


# ------------------------------------------------------------------ helpers
def _clean(body: PackageIn) -> dict:
    d = {}
    for k in FIELDS:
        v = getattr(body, k)
        if isinstance(v, str):
            v = v.strip()
        d[k] = v
    if not d["description"]:
        d["description"] = None
    return d


def _model_kwargs(d: dict) -> dict:
    """Filter None pattern values — biarkan SQLAlchemy column default apply.
    "" eksplisit tetap diteruskan (opt-out non-OLT)."""
    out = {}
    for k, v in d.items():
        if k in ("pppoe_user_pattern", "pppoe_password_pattern") and v is None:
            continue
        out[k] = v
    return out


def _check_pattern(label: str, v: str, errs: list) -> None:
    # Opsional: kosong = tidak dikonfigurasi (mis. paket untuk router non-OLT / Huawei)
    if v is None or v == "":
        return
    if not PATTERN_RE.fullmatch(v):
        errs.append(label + ": 1-64 karakter (huruf, angka, _ . - @ dan token {SN} / {RANDOM8}), tanpa spasi")
        return
    if "{" in TOKEN_RE.sub("", v) or "}" in TOKEN_RE.sub("", v):
        errs.append(label + ": kurung kurawal tidak lengkap")
    for t in TOKEN_RE.findall(v):
        if t not in ALLOWED_TOKENS:
            errs.append(label + ": token {" + t + "} tidak dikenal (pakai: "
                        + ", ".join(sorted(ALLOWED_TOKENS)) + ")")


def _validate(db: Session, olt_id: int, d: dict) -> list:
    errs = []
    if not NAME_RE.fullmatch(d["name"]):
        errs.append("Nama: 1-64 karakter, hanya huruf, angka, _ . -")
    if d["description"] and len(d["description"]) > 255:
        errs.append("Deskripsi: maksimal 255 karakter")

    for key, label in (("gemport_id", "GEMPORT"), ("service_port", "Service Port"), ("vport", "VPort")):
        if not 1 <= d[key] <= ID_MAX:
            errs.append(label + ": harus bilangan bulat 1-" + str(ID_MAX))

    for key, kind, label in (("tcont_profile", "tcont", "T-CONT profile"),
                             ("traffic_profile", "traffic", "Traffic profile")):
        hit = (db.query(GponProfile.id)
               .filter(GponProfile.olt_id == olt_id, GponProfile.kind == kind,
                       GponProfile.name == d[key])
               .first())
        if not hit:
            errs.append(label + ' "' + d[key] + '" tidak ada di OLT ini (sync dulu di tab Deployment)')

    for key, label in (("user_vlan", "User VLAN"), ("vlan", "VLAN")):
        hit = (db.query(VLAN.id)
               .filter(VLAN.olt_id == olt_id, VLAN.vlan_id == d[key])
               .first())
        if not hit:
            errs.append(label + " " + str(d[key]) + " tidak ada di tabel VLAN OLT ini")

    _check_pattern("PPPoE user pattern", d["pppoe_user_pattern"], errs)
    _check_pattern("PPPoE password pattern", d["pppoe_password_pattern"], errs)

    for key, label in (("firewall_level", "Firewall level"),
                       ("security_mgmt_mode", "Security mgmt mode"),
                       ("wan_service", "WAN service")):
        if not WORD_RE.fullmatch(d[key]):
            errs.append(label + ": 1-32 karakter (huruf, angka, _ -)")
    return errs


def _ser(p: ServicePackage, issues: Optional[list] = None) -> dict:
    out = {k: getattr(p, k) for k in FIELDS}
    out["id"] = p.id
    out["olt_id"] = p.olt_id
    out["created_at"] = p.created_at.isoformat() if p.created_at else None
    out["updated_at"] = p.updated_at.isoformat() if p.updated_at else None
    out["issues"] = issues or []
    return out


def _issues(p: ServicePackage, tcont: set, traffic: set, vlans: set) -> list:
    out = []
    if p.tcont_profile not in tcont:
        out.append('T-CONT "' + p.tcont_profile + '" tidak ada di cache OLT')
    if p.traffic_profile not in traffic:
        out.append('Traffic "' + p.traffic_profile + '" tidak ada di cache OLT')
    if p.user_vlan not in vlans:
        out.append("User VLAN " + str(p.user_vlan) + " tidak ada di tabel VLAN")
    if p.vlan not in vlans:
        out.append("VLAN " + str(p.vlan) + " tidak ada di tabel VLAN")
    return out


def _names(db: Session, olt_id: int, kind: str) -> list:
    rows = (db.query(GponProfile.name)
            .filter(GponProfile.olt_id == olt_id, GponProfile.kind == kind)
            .order_by(GponProfile.name).all())
    return [r[0] for r in rows]


def _get_pkg(db: Session, olt_id: int, pkg_id: int) -> ServicePackage:
    p = (db.query(ServicePackage)
         .filter(ServicePackage.id == pkg_id, ServicePackage.olt_id == olt_id)
         .first())
    if not p:
        raise HTTPException(404, "Paket tidak ditemukan")
    return p


def _name_taken(db: Session, olt_id: int, name: str, except_id: Optional[int] = None) -> bool:
    q = db.query(ServicePackage.id).filter(ServicePackage.olt_id == olt_id,
                                           ServicePackage.name == name)
    if except_id is not None:
        q = q.filter(ServicePackage.id != except_id)
    return q.first() is not None


def _usage(db: Session, olt_id: int, p: ServicePackage) -> dict:
    # Fase 4b-2: hitung ONU yang memakai paket ini (kolom ONU.package_id).
    return {"count": 0, "samples": []}


def _audit(db: Session, user, action: str, target: str, detail: Optional[str] = None) -> None:
    """Tulis AuditLog di transaksi yang sama dengan perubahan (commit bersama)."""
    db.add(AuditLog(username=getattr(user, "username", None), action=action,
                    target=target, detail=detail, result="success"))


# ------------------------------------------------------------------ endpoints
@router.get("/{olt_id}/service-packages/options")
def package_options(db: Session = Depends(get_db),
                    olt: OLT = Depends(require_olt_access),
                    user: User = Depends(get_current_user)):
    vlans = (db.query(VLAN).filter(VLAN.olt_id == olt.id).order_by(VLAN.vlan_id).all())
    return {
        "tcont": _names(db, olt.id, "tcont"),
        "traffic": _names(db, olt.id, "traffic"),
        "vlans": [{"vlan_id": v.vlan_id, "name": v.name} for v in vlans],
        "id_max": ID_MAX,
    }


@router.get("/{olt_id}/service-packages")
def list_packages(db: Session = Depends(get_db),
                  olt: OLT = Depends(require_olt_access),
                  user: User = Depends(get_current_user)):
    tcont = set(_names(db, olt.id, "tcont"))
    traffic = set(_names(db, olt.id, "traffic"))
    vlans = {v[0] for v in db.query(VLAN.vlan_id).filter(VLAN.olt_id == olt.id).all()}
    rows = (db.query(ServicePackage).filter(ServicePackage.olt_id == olt.id)
            .order_by(ServicePackage.name).all())
    return {"kind": "package",
            "items": [_ser(p, _issues(p, tcont, traffic, vlans)) for p in rows]}


@router.post("/{olt_id}/service-packages")
def create_package(body: PackageIn,
                   db: Session = Depends(get_db),
                   olt: OLT = Depends(require_olt_access),
                   user: User = Depends(_ADMIN)):
    d = _clean(body)
    errs = _validate(db, olt.id, d)
    if errs:
        raise HTTPException(422, " · ".join(errs))
    if _name_taken(db, olt.id, d["name"]):
        raise HTTPException(409, 'Paket "' + d["name"] + '" sudah ada di OLT ini')
    now = datetime.utcnow()
    p = ServicePackage(olt_id=olt.id, created_at=now, updated_at=now, **_model_kwargs(d))
    db.add(p)
    _audit(db, user, "package.create", "olt=" + str(olt.id) + " paket=" + d["name"])
    try:
        db.commit()
    except IntegrityError:
        db.rollback()
        raise HTTPException(409, 'Paket "' + d["name"] + '" sudah ada di OLT ini')
    db.refresh(p)
    return _ser(p)


@router.put("/{olt_id}/service-packages/{pkg_id}")
def update_package(pkg_id: int, body: PackageIn,
                   db: Session = Depends(get_db),
                   olt: OLT = Depends(require_olt_access),
                   user: User = Depends(_ADMIN)):
    p = _get_pkg(db, olt.id, pkg_id)
    d = _clean(body)
    errs = _validate(db, olt.id, d)
    if errs:
        raise HTTPException(422, " · ".join(errs))
    if _name_taken(db, olt.id, d["name"], except_id=p.id):
        raise HTTPException(409, 'Paket "' + d["name"] + '" sudah ada di OLT ini')
    for k, v in _model_kwargs(d).items():
        setattr(p, k, v)
    p.updated_at = datetime.utcnow()
    _audit(db, user, "package.update", "olt=" + str(olt.id) + " paket=" + d["name"])
    try:
        db.commit()
    except IntegrityError:
        db.rollback()
        raise HTTPException(409, 'Paket "' + d["name"] + '" sudah ada di OLT ini')
    db.refresh(p)
    return _ser(p)


@router.delete("/{olt_id}/service-packages/{pkg_id}")
def delete_package(pkg_id: int,
                   db: Session = Depends(get_db),
                   olt: OLT = Depends(require_olt_access),
                   user: User = Depends(_ADMIN)):
    p = _get_pkg(db, olt.id, pkg_id)
    use = _usage(db, olt.id, p)
    if use["count"] > 0:
        raise HTTPException(409, "Paket masih dipakai " + str(use["count"]) + " ONU")
    name = p.name
    db.delete(p)
    _audit(db, user, "package.delete", "olt=" + str(olt.id) + " paket=" + name)
    db.commit()
    return {"ok": True, "message": 'Paket "' + name + '" dihapus.'}


@router.get("/{olt_id}/service-packages/{pkg_id}/usage")
def package_usage(pkg_id: int,
                  db: Session = Depends(get_db),
                  olt: OLT = Depends(require_olt_access),
                  user: User = Depends(get_current_user)):
    p = _get_pkg(db, olt.id, pkg_id)
    return _usage(db, olt.id, p)
