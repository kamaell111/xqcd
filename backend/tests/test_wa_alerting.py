"""
test_wa_alerting.py — WA-2-1. DB = SQLite in-memory, HTTP = fungsi kirim palsu.
Semua waktu dikendalikan lewat argumen `now` (tanpa sleep).

ADAPT: kalau conftest.py-mu sudah punya fixture `db`, hapus fixture `db` di bawah.
       Kalau kolom wajib ONU/OLT/Alert di models.py berbeda, ubah 3 helper make_*.
"""
from datetime import datetime, timedelta

import pytest
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker
from sqlalchemy.pool import StaticPool

from models import (  # ADAPT: nama class WA-1 (WaSetting/WaTemplate) bila beda
    Base, ONU, OLT, Alert, WaSetting, WaAlertRule, WaAlertState, WaAlertLog,
)
import wa_alerting as wa

SCOPE = "global"
T0 = datetime(2026, 10, 6, 10, 0, 0)


# ───────────────────────── fixture & helper ─────────────────────────
@pytest.fixture()
def db():
    engine = create_engine("sqlite://", connect_args={"check_same_thread": False}, poolclass=StaticPool)
    Base.metadata.create_all(engine)
    session = sessionmaker(bind=engine)()
    yield session
    session.close()


@pytest.fixture(autouse=True)
def env(monkeypatch):
    for k, v in {
        "WA_ALERT_STALE_SEC": "300", "WA_ALERT_MAX_RETRY": "3",
        "WA_ALERT_PENDING_TTL_MIN": "30", "WA_ALERT_MAX_PER_MINUTE": "10",
        "WA_ALERT_LOG_RETENTION_DAYS": "30", "WA_ALERT_TZ_OFFSET": "7",
    }.items():
        monkeypatch.setenv(k, v)


def make_olt(db, olt_id=1, name="OLT-1", polled=T0):
    o = OLT(id=olt_id, hostname=name, ip_address="10.0.0.%d" % olt_id, last_polled=polled)
    db.add(o)
    db.commit()
    return o


def make_onu(db, i, status="los", olt_id=1):
    cols = ONU.__table__.columns.keys()
    data = {
        "olt_id": olt_id,
        "pon_port": "1/1",
        "onu_id": i,
        "interface_name": "gpon-onu_1/1/%d:1" % i,
        "serial_number": "ZTEG%08d" % i,
        "status": status,
        "name": "Pelanggan %d" % i,
    }
    onu = ONU(**{k: v for k, v in data.items() if k in cols})
    db.add(onu)
    db.commit()
    return onu


def make_alert(db, severity, title="CPU tinggi", olt_id=1, created=T0):
    a = Alert(olt_id=olt_id, severity=severity, category="cpu", title=title,
              message="detail", source="test", resolved=False, created_at=created)
    db.add(a)
    db.commit()
    return a


def setup_gateway(db, enabled=True):
    db.add(WaSetting(scope_key=SCOPE, enabled=enabled, engine_enabled=enabled,
                     provider="custom", target="6281234567890"))
    db.commit()


def setup_rule(db, event, **kw):
    wa.seed_defaults(db, SCOPE)
    rule = wa.get_rule(db, SCOPE, event)
    rule.enabled = True
    rule.min_duration_sec = kw.pop("min_duration_sec", 0)   # default tes: langsung kirim
    for k, v in kw.items():
        setattr(rule, k, v)
    db.commit()
    return rule


def tick(db, now, **kw):
    """Anggap sync baru saja selesai (last_polled=now), lalu jalankan detect."""
    for o in db.query(OLT).all():
        o.last_polled = now
    db.commit()
    return wa.wa_alert_detect(db, SCOPE, now=now, **kw)


def logs(db, **flt):
    return db.query(WaAlertLog).filter_by(**flt).order_by(WaAlertLog.id).all()


class FakeSender:
    def __init__(self, results=None):
        self.results = list(results or [])
        self.calls = []

    def __call__(self, db, scope_key, text, target):
        self.calls.append((scope_key, text, target))
        return self.results.pop(0) if self.results else (True, None)


# ───────────────────────── detect ─────────────────────────
def test_los_membuat_satu_pending(db):
    make_olt(db); setup_gateway(db); setup_rule(db, "onu_los")
    make_onu(db, 1, "los")
    tick(db, T0)
    rows = logs(db)
    assert len(rows) == 1
    assert rows[0].event == "onu_los" and rows[0].status == "pending"
    assert rows[0].message == ""            # dirender saat dispatch
    tick(db, T0 + timedelta(minutes=1))     # masih LOS -> tidak kirim lagi
    assert len(logs(db)) == 1


def test_min_duration_menahan_sampai_terpenuhi(db):
    make_olt(db); setup_gateway(db); setup_rule(db, "onu_los", min_duration_sec=120)
    make_onu(db, 1, "los")
    tick(db, T0)
    tick(db, T0 + timedelta(seconds=60))
    assert logs(db) == []
    tick(db, T0 + timedelta(seconds=121))
    assert len(logs(db)) == 1


def test_15_onu_los_jadi_satu_mass_event(db):
    make_olt(db); setup_gateway(db); setup_rule(db, "onu_los", mass_threshold=10)
    for i in range(15):
        make_onu(db, i, "los")
    res = tick(db, T0)
    rows = logs(db)
    assert len(rows) == 1
    assert rows[0].event == "mass_event" and rows[0].batch_count == 15
    assert rows[0].device_key == "onu_los"
    assert rows[0].meta["total"] == 15 and len(rows[0].meta["devices"]) == wa.MASS_LIST_MAX
    assert res["onu_los"]["mass"] == 1
    # semua state ditandai terkirim -> siklus berikut tidak mengirim ulang
    tick(db, T0 + timedelta(minutes=1))
    assert len(logs(db)) == 1


def test_susulan_setelah_mass_digabung_ke_ringkasan(db):
    make_olt(db); setup_gateway(db); setup_rule(db, "onu_los", mass_threshold=10)
    for i in range(12):
        make_onu(db, i, "los")
    tick(db, T0)
    make_onu(db, 100, "los")                       # 1 ONU lagi LOS menyusul
    tick(db, T0 + timedelta(minutes=2))
    rows = logs(db)
    assert [r.event for r in rows] == ["mass_event", "mass_event"]
    assert rows[1].batch_count == 1                # bukan WA individual


def test_onu_pulih_menonaktifkan_state(db):
    make_olt(db); setup_gateway(db); setup_rule(db, "onu_los")
    onu = make_onu(db, 1, "los")
    tick(db, T0)
    onu.status = "online"; db.commit()
    res = tick(db, T0 + timedelta(minutes=1))
    assert res["onu_los"]["cleared"] == 1
    assert db.query(WaAlertState).one().active is False


def test_cooldown_flapping_tidak_kirim_dua_kali(db):
    make_olt(db); setup_gateway(db); setup_rule(db, "onu_los", cooldown_min=30)
    onu = make_onu(db, 1, "los")
    tick(db, T0)                                          # LOS -> 1 log
    onu.status = "online"; db.commit()
    tick(db, T0 + timedelta(minutes=1))                   # pulih
    onu.status = "los"; db.commit()
    tick(db, T0 + timedelta(minutes=2))                   # LOS lagi, cepat
    assert len(logs(db)) == 1                             # tertahan cooldown
    tick(db, T0 + timedelta(minutes=20))
    assert len(logs(db)) == 1
    tick(db, T0 + timedelta(minutes=31))                  # masih LOS & cooldown lewat
    assert len(logs(db)) == 2


def test_remind_after_min(db):
    make_olt(db); setup_gateway(db); setup_rule(db, "onu_los", remind_after_min=60)
    make_onu(db, 1, "los")
    tick(db, T0)
    tick(db, T0 + timedelta(minutes=59))
    assert len(logs(db)) == 1
    tick(db, T0 + timedelta(minutes=61))
    assert len(logs(db)) == 2


def test_baseline_saat_enable_tanpa_kirim(db):
    make_olt(db); setup_gateway(db)
    for i in range(3):
        make_onu(db, i, "los")
    wa.seed_defaults(db, SCOPE)
    rule = wa.get_rule(db, SCOPE, "onu_los")
    rule.enabled = True; rule.min_duration_sec = 0; db.commit()
    for o in db.query(OLT).all():
        o.last_polled = T0
    db.commit()
    wa.baseline_state(db, SCOPE, "onu_los", now=T0)
    assert db.query(WaAlertState).filter_by(active=True).count() == 3
    assert logs(db) == []
    tick(db, T0 + timedelta(minutes=1))                   # siklus normal pun tidak mengirim
    assert logs(db) == []
    make_onu(db, 99, "los")                               # yang BARU tetap terkirim
    tick(db, T0 + timedelta(minutes=2))
    assert len(logs(db)) == 1


def test_olt_stale_dilewati_dan_state_tidak_dihapus(db):
    make_olt(db); setup_gateway(db); setup_rule(db, "onu_los")
    onu = make_onu(db, 1, "los")
    tick(db, T0)
    onu.status = "online"; db.commit()
    stale_now = T0 + timedelta(minutes=10)
    db.query(OLT).first().last_polled = T0                # sync berhenti 10 menit
    db.commit()
    wa.wa_alert_detect(db, SCOPE, now=stale_now)          # tanpa tick -> data basi
    assert db.query(WaAlertState).one().active is True    # tidak disimpulkan "pulih"
    onu2 = make_onu(db, 2, "los")
    wa.wa_alert_detect(db, SCOPE, now=stale_now)
    assert len(logs(db)) == 1                             # ONU baru di OLT basi tidak diproses


def test_gateway_off_atau_rule_off_tidak_mencatat(db):
    make_olt(db); setup_gateway(db, enabled=False); setup_rule(db, "onu_los")
    make_onu(db, 1, "los")
    assert tick(db, T0) == {"skipped": "gateway_off"}
    assert logs(db) == []
    db.query(WaSetting).update({"enabled": True}); db.commit()
    wa.get_rule(db, SCOPE, "onu_los").enabled = False; db.commit()
    assert tick(db, T0) == {"skipped": "no_rules"}


def test_system_alert_filter_severity(db):
    make_olt(db); setup_gateway(db); setup_rule(db, "system_alert", severity_min="warning")
    make_alert(db, "info", "Info saja")
    make_alert(db, "critical", "CPU kritis")
    tick(db, T0)
    rows = logs(db)
    assert len(rows) == 1 and "CPU kritis" in rows[0].device_label


# ───────────────────────── uncfg ─────────────────────────
def uncfg_provider(sns, ts=T0, olt_id=1):
    def _p():
        return {olt_id: ([{"sn": sn, "interface": "gpon-olt_1/1/1"} for sn in sns], ts)}
    return _p


def test_uncfg_dedupe_per_olt_sn(db):
    make_olt(db); setup_gateway(db); setup_rule(db, "onu_uncfg")
    r1 = wa.wa_alert_uncfg(db, SCOPE, now=T0, provider=uncfg_provider(["ZTEGC0000001"]))
    r2 = wa.wa_alert_uncfg(db, SCOPE, now=T0 + timedelta(minutes=5),
                           provider=uncfg_provider(["ZTEGC0000001"], ts=T0 + timedelta(minutes=5)))
    assert r1["queued"] == 1 and r2["queued"] == 0
    assert len(logs(db, event="onu_uncfg")) == 1


def test_uncfg_cache_basi_diabaikan(db):
    make_olt(db); setup_gateway(db); setup_rule(db, "onu_uncfg")
    old = T0 - timedelta(minutes=30)
    wa.wa_alert_uncfg(db, SCOPE, now=T0, provider=uncfg_provider(["ZTEGC0000001"], ts=old))
    assert logs(db) == []


# ───────────────────────── enqueue / dedupe ─────────────────────────
def test_enqueue_dedupe_key_sama_satu_log(db):
    kw = dict(scope_key=SCOPE, event="onu_los", olt_id=1, device_key="7",
              device_label="OLT-1 / x", vars_={}, dedupe_key="k-1", now=T0)
    assert wa.enqueue_alert(db, **kw) is not None
    assert wa.enqueue_alert(db, **kw) is None
    db.commit()
    assert len(logs(db)) == 1


# ───────────────────────── dispatch ─────────────────────────
def pending_los(db, n=1):
    make_olt(db); setup_gateway(db); setup_rule(db, "onu_los", mass_threshold=0)
    for i in range(n):
        make_onu(db, i, "los")
    tick(db, T0)


def test_dispatch_sukses_status_sent_dan_pesan_dirender(db):
    pending_los(db)
    send = FakeSender()
    res = wa.wa_alert_dispatch(db, now=T0 + timedelta(seconds=15), send_fn=send)
    row = logs(db)[0]
    assert res["sent"] == 1 and row.status == "sent" and row.attempts == 1
    assert row.sent_at == T0 + timedelta(seconds=15)
    assert "ONU LOS" in row.message and "Pelanggan 0" in row.message and "OLT-1" in row.message
    assert "{" not in row.message                         # tidak ada variabel tersisa
    assert send.calls[0][0] == SCOPE


def test_dispatch_gagal_retry_backoff_lalu_failed(db):
    pending_los(db)
    send = FakeSender([(False, "HTTP 500")] * 10)
    t = T0
    expected_gap = [1, 5, 15]
    for gap in expected_gap:
        wa.wa_alert_dispatch(db, now=t, send_fn=send)
        row = logs(db)[0]
        assert row.status == "pending" and row.error == "HTTP 500"
        assert row.next_retry_at == t + timedelta(minutes=gap)
        wa.wa_alert_dispatch(db, now=t + timedelta(seconds=30), send_fn=send)   # belum jatuh tempo
        assert logs(db)[0].attempts == expected_gap.index(gap) + 1
        t = row.next_retry_at
    res = wa.wa_alert_dispatch(db, now=t, send_fn=send)                         # percobaan ke-4
    row = logs(db)[0]
    assert res["failed"] == 1 and row.status == "failed" and row.attempts == 4


def test_dispatch_exception_dari_send_fn_dianggap_gagal(db):
    pending_los(db)
    def boom(*a, **k):
        raise RuntimeError("koneksi putus")
    wa.wa_alert_dispatch(db, now=T0, send_fn=boom)
    row = logs(db)[0]
    assert row.status == "pending" and "koneksi putus" in row.error


def test_dispatch_rate_limit_per_menit(db):
    make_olt(db); setup_gateway(db)
    for i in range(12):
        wa.enqueue_alert(db, scope_key=SCOPE, event="onu_los", olt_id=1, device_key=str(i),
                         device_label="x", vars_={}, dedupe_key="k%d" % i, now=T0)
    db.commit()
    send = FakeSender()
    assert wa.wa_alert_dispatch(db, now=T0, send_fn=send)["sent"] == 10
    assert wa.wa_alert_dispatch(db, now=T0 + timedelta(seconds=20), send_fn=send)["sent"] == 0
    assert wa.wa_alert_dispatch(db, now=T0 + timedelta(seconds=61), send_fn=send)["sent"] == 2


def test_dispatch_pending_kedaluwarsa_jadi_expired(db):
    pending_los(db)
    send = FakeSender()
    res = wa.wa_alert_dispatch(db, now=T0 + timedelta(minutes=31), send_fn=send)
    assert res["expired"] == 1 and send.calls == []
    assert logs(db)[0].status == "expired"


def test_dispatch_gateway_off_tidak_mengirim(db):
    pending_los(db)
    db.query(WaSetting).update({"enabled": False}); db.commit()
    send = FakeSender()
    res = wa.wa_alert_dispatch(db, now=T0 + timedelta(seconds=15), send_fn=send)
    # Scope dengan gateway OFF tidak masuk iterasi (filter _active_scopes),
    # jadi skipped=0. Yang penting: TIDAK dikirim & status tetap pending.
    assert send.calls == [] and logs(db)[0].status == "pending"


def test_dispatch_pakai_target_override_dan_klaim_atomik(db):
    pending_los(db)
    wa.get_rule(db, SCOPE, "onu_los").target_override = "6281234567890@g.us"; db.commit()
    send = FakeSender()
    wa.wa_alert_dispatch(db, now=T0, send_fn=send)
    assert send.calls[0][2] == "6281234567890@g.us"
    wa.wa_alert_dispatch(db, now=T0 + timedelta(seconds=5), send_fn=send)       # sudah sent -> tak diulang
    assert len(send.calls) == 1


def test_cancel_pending_saat_rule_dimatikan(db):
    pending_los(db)
    assert wa.cancel_pending(db, SCOPE, "onu_los") == 1
    assert logs(db)[0].status == "expired"


# ───────────────────────── cleanup & helper ─────────────────────────
def test_cleanup_hapus_log_dan_state_lama(db):
    make_olt(db)
    old = T0 - timedelta(days=40)
    wa.enqueue_alert(db, scope_key=SCOPE, event="onu_los", olt_id=1, device_key="1",
                     device_label="x", vars_={}, dedupe_key="old", now=old)
    wa.enqueue_alert(db, scope_key=SCOPE, event="onu_los", olt_id=1, device_key="2",
                     device_label="x", vars_={}, dedupe_key="new", now=T0)
    db.add(WaAlertState(scope_key=SCOPE, event="onu_los", olt_id=1, device_key="1",
                        state_since=old, active=False, cleared_at=old))
    db.commit()
    assert wa.wa_alert_cleanup(db, now=T0) == {"logs": 1, "states": 1}
    assert [r.dedupe_key for r in logs(db)] == ["new"]


def test_render_template_dan_format_helper():
    assert wa.render_template("Hai {a} {zzz}", {"a": "ok"}) == "Hai ok {zzz}"
    assert wa.fmt_duration(45) == "45 detik" and wa.fmt_duration(300) == "5 menit"
    assert wa.fmt_duration(3900) == "1 jam 5 menit"
    assert wa.fmt_time(datetime(2026, 10, 6, 3, 0)) == "06/10/2026 10:00 WIB"


def test_should_fire_cabang_utama():
    class S: pass
    class R: min_duration_sec = 0; cooldown_min = 30; remind_after_min = 0
    s = S(); s.active = True; s.state_since = T0; s.last_sent_at = None
    assert wa.should_fire(s, R, T0) is True
    s.last_sent_at = T0                                   # episode ini sudah dikirim
    assert wa.should_fire(s, R, T0 + timedelta(hours=5)) is False
    s.state_since = T0 + timedelta(minutes=2)             # episode baru, cooldown belum lewat
    assert wa.should_fire(s, R, T0 + timedelta(minutes=10)) is False
    assert wa.should_fire(s, R, T0 + timedelta(minutes=30)) is True
    s.active = False
    assert wa.should_fire(s, R, T0 + timedelta(days=1)) is False


def test_seed_defaults_semua_nonaktif_dan_idempotent(db):
    wa.seed_defaults(db, SCOPE); wa.seed_defaults(db, SCOPE)
    rules = db.query(WaAlertRule).all()
    assert {r.event for r in rules} == set(wa.RULE_EVENTS)
    assert all(r.enabled is False for r in rules)

def test_mass_override_saat_individual_kebanyakan(db):
    """Anti-flood: mass_threshold=0 (selalu individual) tapi 15 LOS dalam 1 siklus
    harus dipaksa jadi mass (bukan 15 individual)."""
    make_olt(db); setup_gateway(db); setup_rule(db, "onu_los", mass_threshold=0)
    for i in range(15):
        make_onu(db, i, "los")
    res = tick(db, T0)
    rows = logs(db)
    # Harus cuma 1 (mass), bukan 15 (individual)
    assert len(rows) == 1, "Dapat %d log, harus 1 (mass override)" % len(rows)
    assert rows[0].event == "mass_event", "Harus mass_event, bukan onu_los"
    assert rows[0].batch_count == 15
    assert res["onu_los"]["mass"] == 1
    assert res["onu_los"]["queued"] == 0


def test_mass_tidak_override_kalau_dibawah_limit(db, monkeypatch):
    """mass_threshold=0 + 5 LOS < MAX_INDIVIDUAL(10) -> tetap 5 individual."""
    monkeypatch.setenv("WA_MAX_INDIVIDUAL", "10")
    make_olt(db); setup_gateway(db); setup_rule(db, "onu_los", mass_threshold=0)
    for i in range(5):
        make_onu(db, i, "los")
    res = tick(db, T0)
    rows = logs(db)
    assert len(rows) == 5, "Harus 5 individual (dibawah limit), dapat %d" % len(rows)
    for r in rows:
        assert r.event == "onu_los"
    assert res["onu_los"]["queued"] == 5
    assert res["onu_los"]["mass"] == 0


def test_mass_override_env_bisa_diset(db, monkeypatch):
    """WA_MAX_INDIVIDUAL=3 -> 4 LOS sudah dipaksa mass."""
    monkeypatch.setenv("WA_MAX_INDIVIDUAL", "3")
    make_olt(db); setup_gateway(db); setup_rule(db, "onu_los", mass_threshold=0)
    for i in range(4):
        make_onu(db, i, "los")
    res = tick(db, T0)
    rows = logs(db)
    assert len(rows) == 1
    assert rows[0].event == "mass_event"
    assert rows[0].batch_count == 4

# ============= Fix 4: clamp cooldown & remind =============

def test_cooldown_dikunci_min_5_menit():
    from types import SimpleNamespace as NS
    from datetime import datetime, timedelta as TD
    now = datetime(2026, 10, 7, 12, 0, 0)
    # last_sent = 2 menit lalu, state_since = 1 menit lalu -> flapping (episode baru)
    st = NS(active=True, state_since=now - TD(seconds=60),
            last_sent_at=now - TD(seconds=120))
    rule = NS(min_duration_sec=0, cooldown_min=0, remind_after_min=0)
    # Sekarang: since_last = 120s < 300s -> False
    assert wa.should_fire(st, rule, now) is False
    # +4m: since_last = 360s >= 300s -> True
    assert wa.should_fire(st, rule, now + TD(minutes=4)) is True


def test_remind_dikunci_min_30_menit():
    from types import SimpleNamespace as NS
    from datetime import datetime, timedelta as TD
    now = datetime(2026, 10, 7, 12, 0, 0)
    st = NS(active=True, state_since=now - TD(hours=2),
            last_sent_at=now - TD(minutes=10))
    rule = NS(min_duration_sec=0, cooldown_min=5, remind_after_min=1)
    # Remind=1 di DB, tapi engine clamp ke 30 menit
    # Saat now: since_last = 10m < 30m -> False
    assert wa.should_fire(st, rule, now) is False
    # Saat +20m: since_last = 30m >= 30m -> True
    assert wa.should_fire(st, rule, now + TD(minutes=20)) is True
