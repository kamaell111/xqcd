"""
Test router Service Package — SQLite in-memory, tanpa OLT.
Jalankan dari folder backend:   pytest test_service_packages.py -v
Butuh httpx (untuk TestClient):  pip install httpx
"""
import os
import sys
from types import SimpleNamespace

import pytest

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

from fastapi import FastAPI  # noqa: E402
from fastapi.testclient import TestClient  # noqa: E402
from sqlalchemy import create_engine  # noqa: E402
from sqlalchemy.orm import sessionmaker  # noqa: E402
from sqlalchemy.pool import StaticPool  # noqa: E402

from database import Base, get_db  # noqa: E402
from models import OLT, VLAN, GponProfile, ServicePackage, AuditLog  # noqa: E402
from auth import get_current_user  # noqa: E402
from tenancy import require_olt_access  # noqa: E402
from routers import packages  # noqa: E402


@pytest.fixture()
def env():
    engine = create_engine("sqlite://", connect_args={"check_same_thread": False},
                           poolclass=StaticPool)
    Base.metadata.create_all(engine)
    Session = sessionmaker(bind=engine, autoflush=False)

    db = Session()
    for oid, ip in ((1, "10.0.0.1"), (2, "10.0.0.2")):
        db.add(OLT(id=oid, ip_address=ip))
        db.add(GponProfile(olt_id=oid, kind="tcont", name="1G", params_json="{}"))
        db.add(GponProfile(olt_id=oid, kind="traffic", name="100M", params_json="{}"))
        db.add(VLAN(olt_id=oid, vlan_id=100, name="INET"))
    db.commit()
    db.close()

    app = FastAPI()
    app.include_router(packages.router)

    def _db():
        s = Session()
        try:
            yield s
        finally:
            s.close()

    def _olt(olt_id: int):
        return SimpleNamespace(id=olt_id)

    admin = SimpleNamespace(username="admin", privilege=15)
    app.dependency_overrides[get_db] = _db
    app.dependency_overrides[require_olt_access] = _olt
    app.dependency_overrides[get_current_user] = lambda: admin
    app.dependency_overrides[packages._ADMIN] = lambda: admin

    return SimpleNamespace(client=TestClient(app), Session=Session)


def url(olt=1, tail=""):
    return "/api/v1/olts/" + str(olt) + "/service-packages" + tail


def payload(**kw):
    p = {"name": "PAKET-100M", "tcont_profile": "1G", "traffic_profile": "100M",
         "gemport_id": 1, "service_port": 1, "vport": 1, "user_vlan": 100, "vlan": 100}
    p.update(kw)
    return p


def test_options(env):
    r = env.client.get(url(1, "/options"))
    assert r.status_code == 200
    d = r.json()
    assert d["tcont"] == ["1G"]
    assert d["traffic"] == ["100M"]
    assert d["vlans"] == [{"vlan_id": 100, "name": "INET"}]
    assert d["id_max"] == packages.ID_MAX


def test_create_ok_dan_default(env):
    r = env.client.post(url(), json=payload())
    assert r.status_code == 200, r.text
    d = r.json()
    assert d["name"] == "PAKET-100M"
    assert d["pppoe_user_pattern"] == "{SN}"
    assert d["pppoe_password_pattern"] == "{RANDOM8}"
    assert d["pppoe_nat"] is True
    assert d["firewall_level"] == "low"
    assert d["security_mgmt_mode"] == "forward"
    assert d["wan_service"] == "internet"
    assert env.client.get(url()).json()["items"][0]["name"] == "PAKET-100M"


def test_create_menulis_audit(env):
    env.client.post(url(), json=payload())
    s = env.Session()
    rows = s.query(AuditLog).all()
    s.close()
    assert len(rows) == 1 and rows[0].action == "package.create" and rows[0].username == "admin"


def test_nama_duplikat_409(env):
    assert env.client.post(url(), json=payload()).status_code == 200
    assert env.client.post(url(), json=payload()).status_code == 409


@pytest.mark.parametrize("name", ["", "pakai spasi", "a/b", "x" * 65, "kutip'"])
def test_nama_tidak_valid_422(env, name):
    assert env.client.post(url(), json=payload(name=name)).status_code == 422


def test_tcont_tidak_ada_422(env):
    r = env.client.post(url(), json=payload(tcont_profile="NGAWUR"))
    assert r.status_code == 422 and "T-CONT" in r.json()["detail"]


def test_traffic_tidak_ada_422(env):
    r = env.client.post(url(), json=payload(traffic_profile="NGAWUR"))
    assert r.status_code == 422 and "Traffic" in r.json()["detail"]


def test_vlan_tidak_ada_422(env):
    assert env.client.post(url(), json=payload(user_vlan=999)).status_code == 422
    assert env.client.post(url(), json=payload(vlan=999)).status_code == 422


@pytest.mark.parametrize("field", ["gemport_id", "service_port", "vport"])
@pytest.mark.parametrize("val", [0, -1, packages.ID_MAX + 1])
def test_id_di_luar_rentang_422(env, field, val):
    assert env.client.post(url(), json=payload(**{field: val})).status_code == 422


def test_id_bukan_integer_422(env):
    assert env.client.post(url(), json=payload(gemport_id="abc")).status_code == 422
    assert env.client.post(url(), json=payload(gemport_id=1.5)).status_code == 422


@pytest.mark.parametrize("pat", ["{BOGUS}", "ada spasi", "{SN", "user\nbaris2", "a;b"])
def test_pattern_tidak_valid_422(env, pat):
    assert env.client.post(url(), json=payload(pppoe_user_pattern=pat)).status_code == 422


def test_pattern_valid(env):
    r = env.client.post(url(), json=payload(pppoe_user_pattern="isp-{SN}", pppoe_password_pattern="{RANDOM8}"))
    assert r.status_code == 200


def test_update_ok(env):
    pid = env.client.post(url(), json=payload()).json()["id"]
    r = env.client.put(url(1, "/" + str(pid)), json=payload(gemport_id=2, description="paket baru"))
    assert r.status_code == 200
    d = r.json()
    assert d["gemport_id"] == 2 and d["description"] == "paket baru"
    assert d["updated_at"] >= d["created_at"]


def test_update_nama_bentrok_409(env):
    env.client.post(url(), json=payload(name="A"))
    pid_b = env.client.post(url(), json=payload(name="B")).json()["id"]
    assert env.client.put(url(1, "/" + str(pid_b)), json=payload(name="A")).status_code == 409


def test_update_nama_sendiri_boleh(env):
    pid = env.client.post(url(), json=payload()).json()["id"]
    assert env.client.put(url(1, "/" + str(pid)), json=payload()).status_code == 200


def test_update_tidak_ada_404(env):
    assert env.client.put(url(1, "/9999"), json=payload()).status_code == 404


def test_delete_ok(env):
    pid = env.client.post(url(), json=payload()).json()["id"]
    assert env.client.delete(url(1, "/" + str(pid))).status_code == 200
    assert env.client.get(url()).json()["items"] == []
    assert env.client.delete(url(1, "/" + str(pid))).status_code == 404


def test_usage_nol(env):
    pid = env.client.post(url(), json=payload()).json()["id"]
    r = env.client.get(url(1, "/" + str(pid) + "/usage"))
    assert r.status_code == 200 and r.json()["count"] == 0


def test_multi_olt_terisolasi(env):
    pid = env.client.post(url(1), json=payload()).json()["id"]
    assert env.client.get(url(2)).json()["items"] == []
    # nama sama boleh di OLT lain
    assert env.client.post(url(2), json=payload()).status_code == 200
    # paket OLT 1 tidak bisa disentuh lewat OLT 2
    pid_olt1_only = env.client.post(url(1), json=payload(name="HANYA-OLT1")).json()["id"]
    assert env.client.put(url(2, "/" + str(pid_olt1_only)), json=payload(name="X")).status_code == 404
    assert env.client.delete(url(2, "/" + str(pid_olt1_only))).status_code == 404
    assert pid  # dipakai agar tidak dianggap variabel menganggur


def test_list_menandai_referensi_putus(env):
    env.client.post(url(), json=payload())
    s = env.Session()
    s.query(GponProfile).filter(GponProfile.olt_id == 1, GponProfile.kind == "tcont").delete()
    s.query(VLAN).filter(VLAN.olt_id == 1).delete()
    s.commit()
    s.close()
    item = env.client.get(url()).json()["items"][0]
    assert any("T-CONT" in i for i in item["issues"])
    assert any("VLAN" in i for i in item["issues"])
