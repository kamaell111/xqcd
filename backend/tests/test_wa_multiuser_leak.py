"""
test_wa_multiuser_leak.py — Test kebocoran tenant di engine detect.
Verifikasi: rule user:X hanya lihat ONU milik X, tidak bocor tenant lain.

Fix yang diverifikasi: _olt_context filter per scope (Batch 3B).
"""
import os
import sys
from datetime import datetime, timedelta

import pytest
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker
from sqlalchemy.pool import StaticPool

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from cryptography.fernet import Fernet
os.environ.setdefault("WA_FERNET_KEY", Fernet.generate_key().decode())

from models import Base, OLT, ONU, User, WaSetting, WaAlertRule, WaAlertLog  # noqa: E402
import wa_alerting as wal  # noqa: E402


T0 = datetime(2026, 10, 6, 10, 0, 0)
SCOPE_A = "user:1"
SCOPE_B = "user:2"
SCOPE_G = "global"


@pytest.fixture()
def db():
    engine = create_engine("sqlite://", connect_args={"check_same_thread": False},
                           poolclass=StaticPool)
    Base.metadata.create_all(engine)
    session = sessionmaker(bind=engine)()
    # Users
    session.add_all([
        User(id=1, username="a", password_hash="x"),
        User(id=2, username="b", password_hash="x"),
    ])
    # OLT A (owner 1), OLT B (owner 2)
    session.add(OLT(id=1, ip_address="10.0.0.1", owner_user_id=1, last_polled=T0))
    session.add(OLT(id=2, ip_address="10.0.0.2", owner_user_id=2, last_polled=T0))
    session.flush()
    # ONU LOS di masing-masing OLT
    session.add(ONU(olt_id=1, pon_port="1/1", onu_id=1, status="los",
                    name="ONU-A-LOS", serial_number="SN-A1"))
    session.add(ONU(olt_id=2, pon_port="1/1", onu_id=1, status="los",
                    name="ONU-B-LOS", serial_number="SN-B1"))
    session.commit()
    yield session
    session.close()


def _setup_rule(db, scope, olt_ids=None):
    """Seed rule + gateway utk scope tertentu (default: SEMUA OLT)."""
    db.add(WaSetting(scope_key=scope, enabled=True, engine_enabled=True,
                     provider="custom", target="6281234567890"))
    db.add(WaAlertRule(scope_key=scope, event="onu_los", enabled=True,
                       min_duration_sec=0, cooldown_min=0, remind_after_min=0,
                       mass_threshold=100, olt_ids=olt_ids))
    db.commit()


def test_user_a_hanya_lihat_onu_tenant_a(db):
    """Rule user:1 tidak boleh mendeteksi ONU di OLT tenant lain."""
    _setup_rule(db, SCOPE_A)
    stats = wal.wa_alert_detect(db, SCOPE_A, now=T0 + timedelta(seconds=10))
    logs = db.query(WaAlertLog).filter(WaAlertLog.scope_key == SCOPE_A).all()
    assert len(logs) == 1, f"BOCOR: user A dapat {len(logs)} log (harus 1)"
    assert logs[0].olt_id == 1, f"BOCOR: user A dapat OLT {logs[0].olt_id} (harus 1)"


def test_user_b_hanya_lihat_onu_tenant_b(db):
    """Rule user:2 tidak boleh mendeteksi ONU di OLT tenant lain."""
    _setup_rule(db, SCOPE_B)
    stats = wal.wa_alert_detect(db, SCOPE_B, now=T0 + timedelta(seconds=10))
    logs = db.query(WaAlertLog).filter(WaAlertLog.scope_key == SCOPE_B).all()
    assert len(logs) == 1, f"BOCOR: user B dapat {len(logs)} log (harus 1)"
    assert logs[0].olt_id == 2, f"BOCOR: user B dapat OLT {logs[0].olt_id} (harus 2)"


def test_global_lihat_semua_tenant(db):
    """Scope global (super admin) boleh lihat semua OLT."""
    _setup_rule(db, SCOPE_G)
    stats = wal.wa_alert_detect(db, SCOPE_G, now=T0 + timedelta(seconds=10))
    logs = db.query(WaAlertLog).filter(WaAlertLog.scope_key == SCOPE_G).all()
    assert len(logs) == 2, f"global harus lihat 2 log, dapat {len(logs)}"
    olt_ids = {l.olt_id for l in logs}
    assert olt_ids == {1, 2}, f"global harus lihat OLT 1+2, bukan {olt_ids}"


def test_scope_olt_lihat_olt_itu_saja(db):
    """Scope olt:1 hanya lihat OLT 1, meski ada ONU LOS di OLT 2."""
    _setup_rule(db, "olt:1")
    stats = wal.wa_alert_detect(db, "olt:1", now=T0 + timedelta(seconds=10))
    logs = db.query(WaAlertLog).filter(WaAlertLog.scope_key == "olt:1").all()
    assert len(logs) == 1, f"olt:1 harus 1 log, dapat {len(logs)}"
    assert logs[0].olt_id == 1, f"olt:1 harus OLT 1, dapat {logs[0].olt_id}"


def test_olt_ids_filter_membatasi_lebih_lagi(db):
    """Rule user:1 dengan olt_ids=[2] (bukan miliknya) → tidak deteksi apa pun."""
    _setup_rule(db, SCOPE_A, olt_ids=[2])
    stats = wal.wa_alert_detect(db, SCOPE_A, now=T0 + timedelta(seconds=10))
    logs = db.query(WaAlertLog).filter(WaAlertLog.scope_key == SCOPE_A).all()
    assert len(logs) == 0, f"User A set olt_ids=[2] harus 0 log, dapat {len(logs)}"
