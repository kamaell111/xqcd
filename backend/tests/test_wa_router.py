"""
test_wa_router.py — WA-1. Jalankan dari folder backend:  pytest tests/test_wa_router.py -v
HTTP ke gateway di-mock (wa._http_post); tidak ada koneksi keluar. DB = SQLite in-memory.
Pakai IP literal di URL gateway supaya tidak ada DNS lookup.
"""
import os
import sys
from datetime import datetime

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker
from sqlalchemy.pool import StaticPool

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

os.environ.setdefault("WA_FERNET_KEY", "")          # diisi di fixture
from cryptography.fernet import Fernet  # noqa: E402

os.environ["WA_FERNET_KEY"] = Fernet.generate_key().decode()
os.environ["WA_ALLOW_PRIVATE_HOSTS"] = "192.168.1.50"  # semua port utk host ini

from database import Base, get_db  # noqa: E402
from models import OLT, ONU, User, WaSetting, AuditLog, WaAlertLog  # noqa: E402
from routers import wa  # noqa: E402

GOOD = {"provider": "custom", "gateway_url": "http://192.168.1.50:3000/send",
        "target": "6281234567890", "enabled": True, "api_token": "secret-token-ABCD1234"}


class Who:
    def __init__(self, id=1, super_admin=1, owner=None):
        self.id, self.username = id, "u" + str(id)
        self.is_super_admin, self.owner_user_id, self.privilege = super_admin, owner, 15


@pytest.fixture()
def env(monkeypatch):
    eng = create_engine("sqlite://", connect_args={"check_same_thread": False}, poolclass=StaticPool)
    Base.metadata.create_all(eng)
    Session = sessionmaker(bind=eng)
    db = Session()
    db.add_all([User(id=1, username="root", password_hash="x", is_super_admin=1),
                User(id=2, username="adm", password_hash="x"),
                User(id=3, username="adm2", password_hash="x")])
    db.add_all([OLT(id=1, ip_address="10.0.0.1", owner_user_id=2),
                OLT(id=2, ip_address="10.0.0.2", owner_user_id=3)])
    db.flush()
    db.add_all([ONU(olt_id=1, pon_port="1/1/1", onu_id=i) for i in range(1, 4)])    # 3 ONU di OLT 1
    db.add_all([ONU(olt_id=2, pon_port="1/1/1", onu_id=i) for i in range(1, 3)])    # 2 ONU di OLT 2
    db.commit()

    app = FastAPI()
    app.include_router(wa.router)
    app.dependency_overrides[get_db] = lambda: db
    who = {"u": Who()}
    app.dependency_overrides[wa._ADMIN] = lambda: who["u"]

    sent = []

    def fake_post(url, headers, body, timeout=10):
        sent.append({"url": url, "headers": headers, "body": body.decode()})
        return 200, '{"ok": true}'

    monkeypatch.setattr(wa, "_http_post", fake_post)
    wa._rl_last.clear()
    c = TestClient(app)
    c.db, c.who, c.sent = db, who, sent
    yield c
    db.close()


# ------------------------------------------------------------------ provider
def test_providers(env):
    for r in (env.get("/api/v1/wa/providers"), env.post("/api/v1/wa/providers")):
        assert r.status_code == 200
        ids = {p["id"] for p in r.json()["items"]}
        assert ids == {"baileys", "fonnte", "wablas", "n8n", "custom"}


# ------------------------------------------------------------------ setting & masking
def test_settings_empty_then_save_masks_token(env):
    r = env.get("/api/v1/wa/settings?scope=global")
    assert r.json()["configured"] is False

    r = env.put("/api/v1/wa/settings?scope=global", json=GOOD)
    assert r.status_code == 200, r.text
    j = r.json()
    assert j["has_token"] is True and j["token_preview"] == "••••1234"
    assert "api_token" not in j and "secret-token" not in r.text

    row = env.db.query(WaSetting).filter_by(scope_key="global").one()
    assert row.api_token_encrypted and "secret-token" not in row.api_token_encrypted   # terenkripsi
    assert "secret-token" not in env.get("/api/v1/wa/settings?scope=global").text


def test_update_without_token_keeps_old_and_clear_removes(env):
    env.put("/api/v1/wa/settings?scope=global", json=GOOD)
    body = dict(GOOD, api_token=None, target="6289999")
    r = env.put("/api/v1/wa/settings?scope=global", json=body)
    assert r.json()["has_token"] is True and r.json()["target"] == "6289999"
    r = env.put("/api/v1/wa/settings?scope=global", json=dict(body, clear_token=True))
    assert r.json()["has_token"] is False                      # custom: token opsional


def test_token_required_for_fonnte(env):
    body = dict(GOOD, provider="fonnte", gateway_url="https://api.fonnte.com/send", api_token=None)
    r = env.put("/api/v1/wa/settings?scope=global", json=body)
    assert r.status_code == 422 and "Token wajib" in r.json()["detail"]


@pytest.mark.parametrize("patch,frag", [
    ({"provider": "telegram"}, "Provider tidak valid"),
    ({"gateway_url": "ftp://192.168.1.5/x"}, "http://"),
    ({"gateway_url": "http://user:pw@192.168.1.5/x"}, "user:password"),
    ({"gateway_url": "http://169.254.169.254/latest"}, "link-local"),
    ({"gateway_url": "http://169.254.10.10/x"}, "link-local"),
    ({"gateway_url": "http://metadata.google.internal/x"}, "metadata"),
    ({"target": "628 123"}, "Target"),
    ({"target": ""}, "Target"),
])
def test_validation_rejects(env, patch, frag):
    r = env.put("/api/v1/wa/settings?scope=global", json=dict(GOOD, **patch))
    assert r.status_code == 422 and frag in r.json()["detail"], r.text


def test_private_gateway_blocked_without_allowlist(env, monkeypatch):
    """Default: private IP ditolak tanpa allowlist (aman multi-tenant)."""
    monkeypatch.delenv("WA_ALLOW_PRIVATE_HOSTS", raising=False)
    r = env.put("/api/v1/wa/settings?scope=global", json=GOOD)
    assert r.status_code == 422 and "privat" in r.json()["detail"]


def test_private_gateway_allowed_with_allowlist(env, monkeypatch):
    """Allowlist untuk host:port yang tepat -> private IP diterima."""
    monkeypatch.setenv("WA_ALLOW_PRIVATE_HOSTS", "192.168.1.50:3000")
    r = env.put("/api/v1/wa/settings?scope=global", json=GOOD)
    assert r.status_code == 200, r.text


def test_private_gateway_allowlist_port_mismatch(env, monkeypatch):
    """Allowlist host tapi port beda -> tetap ditolak."""
    monkeypatch.setenv("WA_ALLOW_PRIVATE_HOSTS", "192.168.1.50:9999")
    r = env.put("/api/v1/wa/settings?scope=global", json=GOOD)
    assert r.status_code == 422 and "privat" in r.json()["detail"]


def test_metadata_endpoint_blocked_even_with_allowlist(env, monkeypatch):
    """Metadata cloud = hard-deny. Allowlist tidak bisa meloloskan."""
    monkeypatch.setenv("WA_ALLOW_PRIVATE_HOSTS", "169.254.0.0/16")
    bad = dict(GOOD, gateway_url="http://169.254.169.254/latest/meta-data/")
    r = env.put("/api/v1/wa/settings?scope=global", json=bad)
    assert r.status_code == 422 and "alamat tidak diizinkan" in r.json()["detail"]


# ------------------------------------------------------------------ scope / akses
def test_scope_rules(env):
    env.who["u"] = Who(id=2, super_admin=0)                       # admin biasa, pemilik OLT 1
    assert env.get("/api/v1/wa/settings?scope=global").status_code == 403
    assert env.get("/api/v1/wa/settings?scope=user:3").status_code == 403
    assert env.get("/api/v1/wa/settings?scope=user:2").status_code == 200
    assert env.get("/api/v1/wa/settings?scope=olt:1").status_code == 200
    assert env.get("/api/v1/wa/settings?scope=olt:2").status_code == 403
    assert env.get("/api/v1/wa/settings?scope=bogus").status_code == 422
    assert env.get("/api/v1/wa/settings?scope=olt:1;drop").status_code == 422


# ------------------------------------------------------------------ template
def test_templates_default_and_custom(env):
    j = env.get("/api/v1/wa/templates?scope=global").json()
    t = j["items"][0]
    assert t["event"] == "test" and t["is_default"] and "Test Connection" in t["message_template"]

    r = env.put("/api/v1/wa/templates/test?scope=global", json={"message_template": "ONU: {total_onu} @ {time}"})
    assert r.status_code == 200
    assert env.get("/api/v1/wa/templates?scope=global").json()["items"][0]["is_default"] is False


@pytest.mark.parametrize("tpl", ["halo {online}", "halo {total_onu", "   ", "x" * 1001])
def test_template_rejects(env, tpl):
    r = env.put("/api/v1/wa/templates/test?scope=global", json={"message_template": tpl})
    assert r.status_code == 422


def test_template_unknown_event(env):
    r = env.put("/api/v1/wa/templates/dying_gasp?scope=global", json={"message_template": "x"})
    assert r.status_code == 404


# ------------------------------------------------------------------ preview & test kirim
def test_preview_counts_per_scope(env):
    _prev = env.get("/api/v1/wa/preview?scope=global").json()
    assert "Test Connection" in _prev["message"]
    assert _prev["total_onu"] == 5
    assert env.get("/api/v1/wa/preview?scope=olt:1").json()["total_onu"] == 3
    assert env.get("/api/v1/wa/preview?scope=olt:2").json()["total_onu"] == 2
    assert env.get("/api/v1/wa/preview?scope=user:2").json()["total_onu"] == 3


def test_test_send_ok(env):
    env.put("/api/v1/wa/settings?scope=olt:1", json=GOOD)
    r = env.post("/api/v1/wa/test?scope=olt:1")
    assert r.status_code == 200, r.text
    assert "Test Connection" in r.json()["message"]
    assert len(env.sent) == 1
    s = env.sent[0]
    assert s["url"] == GOOD["gateway_url"]
    assert s["headers"]["Authorization"] == "Bearer " + GOOD["api_token"]
    assert "Test Connection" in s["body"] and "6281234567890" in s["body"]
    logs = env.db.query(AuditLog).filter(AuditLog.action == "wa.test").all()
    assert logs and all(GOOD["api_token"] not in (l.detail or "") for l in logs)


def test_test_send_without_setting(env):
    r = env.post("/api/v1/wa/test?scope=global")
    assert r.status_code == 422 and "belum disimpan" in r.json()["detail"]
    assert env.sent == []


def test_test_rate_limited(env):
    env.put("/api/v1/wa/settings?scope=global", json=GOOD)
    assert env.post("/api/v1/wa/test?scope=global").status_code == 200
    r = env.post("/api/v1/wa/test?scope=global")
    assert r.status_code == 429 and "Retry-After" in r.headers
    assert len(env.sent) == 1


def test_gateway_http_error_returns_502(env, monkeypatch):
    env.put("/api/v1/wa/settings?scope=global", json=GOOD)
    monkeypatch.setattr(wa, "_http_post", lambda *a, **k: (500, "boom"))
    r = env.post("/api/v1/wa/test?scope=global")
    assert r.status_code == 502 and "500" in r.json()["detail"]


def test_gateway_status_false_in_200_is_failure(env, monkeypatch):
    env.put("/api/v1/wa/settings?scope=global",
            json=dict(GOOD, provider="fonnte", gateway_url="http://192.168.1.50/send"))
    monkeypatch.setattr(wa, "_http_post", lambda *a, **k: (200, '{"status": false, "reason": "invalid token"}'))
    r = env.post("/api/v1/wa/test?scope=global")
    assert r.status_code == 502 and "invalid token" in r.json()["detail"]


def test_fonnte_request_shape(env):
    env.put("/api/v1/wa/settings?scope=global",
            json=dict(GOOD, provider="fonnte", gateway_url="http://192.168.1.50/send", api_token="tok12345678"))
    assert env.post("/api/v1/wa/test?scope=global").status_code == 200
    s = env.sent[0]
    assert s["headers"]["Authorization"] == "tok12345678"
    assert s["headers"]["Content-Type"] == "application/x-www-form-urlencoded"
    assert "target=6281234567890" in s["body"] and "message=" in s["body"]


def test_gateway_unreachable(env, monkeypatch):
    import urllib.error
    env.put("/api/v1/wa/settings?scope=global", json=GOOD)

    def boom(*a, **k):
        raise urllib.error.URLError("connection refused")

    monkeypatch.setattr(wa, "_http_post", boom)
    r = env.post("/api/v1/wa/test?scope=global")
    assert r.status_code == 502 and "Gagal terhubung" in r.json()["detail"]

# ------------------------------------------------------------------ WA-2 cross-scope
# Fixture `env` sudah punya: user 1 (super), user 2 (owner OLT 1), user 3 (owner OLT 2).
# Semua test di bawah pakai env.who["u"] untuk ganti identitas.

def test_alert_rules_own_user_scope_ok(env):
    env.who["u"] = Who(id=2, super_admin=0)
    r = env.get("/api/v1/wa/alert-rules?scope=user:2")
    assert r.status_code == 200, r.text
    assert isinstance(r.json(), list)
    assert len(r.json()) >= 4  # seed_defaults bikin 4 rule


def test_alert_rules_cross_user_denied(env):
    env.who["u"] = Who(id=2, super_admin=0)
    r = env.get("/api/v1/wa/alert-rules?scope=user:3")
    assert r.status_code == 403


def test_alert_rules_global_non_super_denied(env):
    env.who["u"] = Who(id=2, super_admin=0)
    r = env.get("/api/v1/wa/alert-rules?scope=global")
    assert r.status_code == 403


def test_alert_rules_olt_owner_ok(env):
    env.who["u"] = Who(id=2, super_admin=0)
    r = env.get("/api/v1/wa/alert-rules?scope=olt:1")
    assert r.status_code == 200, r.text


def test_alert_rules_olt_non_owner_denied(env):
    env.who["u"] = Who(id=2, super_admin=0)
    r = env.get("/api/v1/wa/alert-rules?scope=olt:2")
    assert r.status_code == 403


@pytest.mark.parametrize("bad", [
    "user:0", "user:01", "user:001", "user:-1", "user:+1",
    "user:0000000000", "user:9999999999", "user:99999999999999",
    "USER:1", "User:1", "user:1 ", " user:1", "user:1\n",
    "user:", "olt:0", "olt:01", "olt:9999999999",
    "global ", " global", "GLOBAL", "Global",
    "", " ",
])
def test_alert_rules_malformed_scope_422(env, bad):
    env.who["u"] = Who(id=1, super_admin=1)
    r = env.get("/api/v1/wa/alert-rules", params={"scope": bad})
    assert r.status_code == 422, f"scope={bad!r} -> {r.status_code}"


def test_alerts_log_isolated_per_scope(env):
    db = env.db
    db.add(WaAlertLog(scope_key="user:2", event="onu_los", olt_id=1,
                      device_key="k1", dedupe_key="dd1", status="sent",
                      created_at=datetime.utcnow(), sent_at=datetime.utcnow()))
    db.add(WaAlertLog(scope_key="user:3", event="onu_los", olt_id=2,
                      device_key="k2", dedupe_key="dd2", status="sent",
                      created_at=datetime.utcnow(), sent_at=datetime.utcnow()))
    db.commit()
    env.who["u"] = Who(id=1, super_admin=1)
    r2 = env.get("/api/v1/wa/alerts-log?scope=user:2")
    r3 = env.get("/api/v1/wa/alerts-log?scope=user:3")
    assert r2.status_code == 200 and r2.json()["total"] == 1
    assert r3.status_code == 200 and r3.json()["total"] == 1


def test_alerts_log_cross_scope_denied(env):
    env.who["u"] = Who(id=2, super_admin=0)
    r = env.get("/api/v1/wa/alerts-log?scope=user:3")
    assert r.status_code == 403


def test_alerts_log_retry_404_for_missing(env):
    env.who["u"] = Who(id=1, super_admin=1)
    r = env.post("/api/v1/wa/alerts-log/99999/retry?scope=global")
    assert r.status_code == 404


def test_alerts_status_own_scope_ok(env):
    env.who["u"] = Who(id=2, super_admin=0)
    r = env.get("/api/v1/wa/alerts-status?scope=user:2")
    assert r.status_code == 200
    assert "env_enabled" in r.json()


# ============= Validasi remind_after_min =============

def test_remind_after_min_rejects_too_small(env):
    """Reminder < 30 menit harus ditolak."""
    env.who["u"] = Who(id=1, super_admin=1)
    for bad in [1, 5, 15, 29]:
        r = env.put("/api/v1/wa/alert-rules/onu_los?scope=global",
                    json={"remind_after_min": bad})
        assert r.status_code == 422, f"remind={bad} harus 422, dapat {r.status_code}"


def test_remind_after_min_accepts_zero_and_valid(env):
    """0 = nonaktif, 30+ = OK."""
    env.who["u"] = Who(id=1, super_admin=1)
    for good in [0, 30, 60, 120]:
        r = env.put("/api/v1/wa/alert-rules/onu_los?scope=global",
                    json={"remind_after_min": good})
        assert r.status_code == 200, f"remind={good} harus 200, dapat {r.status_code}: {r.text}"


# ============= Validasi remind_after_min =============

def test_remind_after_min_rejects_too_small(env):
    """Reminder < 30 menit harus ditolak."""
    env.who["u"] = Who(id=1, super_admin=1)
    for bad in [1, 5, 15, 29]:
        r = env.put("/api/v1/wa/alert-rules/onu_los?scope=global",
                    json={"remind_after_min": bad})
        assert r.status_code == 422, f"remind={bad} harus 422, dapat {r.status_code}"


def test_remind_after_min_accepts_zero_and_valid(env):
    """0 = nonaktif, 30+ = OK."""
    env.who["u"] = Who(id=1, super_admin=1)
    for good in [0, 30, 60, 120]:
        r = env.put("/api/v1/wa/alert-rules/onu_los?scope=global",
                    json={"remind_after_min": good})
        assert r.status_code == 200, f"remind={good} harus 200, dapat {r.status_code}: {r.text}"
