"""
wa_alerting.py — WA-2: Alert Engine (detect -> outbox -> dispatch)

Alur:
  wa_alert_detect / wa_alert_uncfg  -> bandingkan kondisi saat ini dgn wa_alert_state
                                       -> tulis wa_alert_log (status=pending)
  wa_alert_dispatch                 -> klaim atomik pending -> render template
                                       -> send_wa_message() -> sent / retry / failed
  wa_alert_cleanup                  -> hapus log & state lama

Semua waktu = UTC naive (sama seperti datetime.utcnow di models.py).
Fungsi inti menerima `now` supaya bisa dites tanpa sleep.
"""
from __future__ import annotations

import logging
import os
import re
from datetime import datetime, timedelta, timezone
from typing import Any, Callable, Dict, List, Optional, Set, Tuple

from sqlalchemy import func, or_, text, bindparam

# ───────────────────────── ADAPTER ─────────────────────────
# Satu-satunya tempat yang perlu disesuaikan dgn nama class di models.py kamu.
# (WaSetting / WaTemplate = class dari WA-1; sisanya dari WA-2-1.)
from models import (  # noqa: E402
    ONU,
    OLT,
    Alert,
    User,
    WaSetting,
    WaTemplate,
    WaAlertRule,
    WaAlertState,
    WaAlertLog,
    WaFlag,
)
# ───────────────────────────────────────────────────────────

log = logging.getLogger("wa_alerting")

RULE_EVENTS = ("onu_los", "onu_offline", "onu_uncfg", "system_alert")
TEMPLATE_EVENTS = RULE_EVENTS + ("mass_event",)

STATUS_LABEL = {
    "onu_los": "LOS",
    "onu_offline": "Offline",
    "onu_uncfg": "Belum terdaftar",
    "system_alert": "Alert",
}
SEVERITY_RANK = {"info": 0, "warning": 1, "critical": 2}

BACKOFF_MIN = (1, 5, 15)        # jeda retry ke-1, ke-2, ke-3
MASS_LIST_MAX = 10              # jumlah device yg dicantumkan di pesan ringkasan
ALERT_LOOKBACK_H = 24           # system_alert: hanya alert belum-resolved <= 24 jam
SENDING_LEASE_SEC = 120         # 'sending' lebih lama dari ini dianggap macet

DEFAULT_RULES: Dict[str, Dict[str, Any]] = {
    "onu_los":      dict(min_duration_sec=120, cooldown_min=30, mass_threshold=10),
    "onu_offline":  dict(min_duration_sec=300, cooldown_min=30, mass_threshold=10),
    "onu_uncfg":    dict(min_duration_sec=0,   cooldown_min=60, mass_threshold=10),
    "system_alert": dict(min_duration_sec=0,   cooldown_min=30, mass_threshold=10,
                         severity_min="warning"),
}

DEFAULT_TEMPLATES: Dict[str, str] = {
    "onu_los": (
        "🔴 *ONU LOS*\n"
        "OLT: {olt_name}\n"
        "ONU: {onu_name}\n"
        "SN: {sn}\n"
        "Status: {status}\n"
        "Sejak: {since} ({duration})\n"
        "Waktu: {time}"
    ),
    "onu_offline": (
        "🟠 *ONU Offline*\n"
        "OLT: {olt_name}\n"
        "ONU: {onu_name}\n"
        "SN: {sn}\n"
        "Sejak: {since} ({duration})\n"
        "Waktu: {time}"
    ),
    "onu_uncfg": (
        "🆕 *ONU Baru Terdeteksi*\n"
        "OLT: {olt_name}\n"
        "SN: {sn}\n"
        "Lokasi: {onu_name}\n"
        "Waktu: {time}"
    ),
    "system_alert": (
        "⚠️ *Alert Sistem* ({severity})\n"
        "{title}\n"
        "{message}\n"
        "OLT: {olt_name}\n"
        "Waktu: {time}"
    ),
    "mass_event": (
        "🚨 *Gangguan Massal: {status}*\n"
        "OLT: {olt_name}\n"
        "Jumlah: {count}\n"
        "{list}\n"
        "Waktu: {time}"
    ),
}

VAR_KEYS = (
    "olt_name", "onu_name", "sn", "status", "since", "duration", "time",
    "count", "list", "severity", "title", "message",
)

# Status engine (in-memory, direset saat restart; cukup utk single-worker)
ENGINE_STATUS: Dict[str, Dict[str, Any]] = {}

SendFn = Callable[[Any, str, str, Optional[str]], Tuple[bool, Optional[str]]]
Conds = Dict[Tuple[int, str], Dict[str, Any]]


# ───────────────────────── helper umum ─────────────────────────
def _env_int(name: str, default: int, alt: Optional[str] = None) -> int:
    for key in (name, alt):
        if key and os.getenv(key) not in (None, ""):
            try:
                return int(os.getenv(key))  # type: ignore[arg-type]
            except ValueError:
                pass
    return default


TOKEN_REQUIRED = {"fonnte", "wablas"}   # inverse dari TOKEN_OPTIONAL di wa.py


def env_allow() -> bool:
    """Kill-switch darurat dari env. Default: tidak memblokir (DB yang menentukan)."""
    return os.getenv("WA_ALERT_ENABLED", "1").strip().lower() not in ("0", "false", "no")


def alerts_enabled() -> bool:
    """Backward compat: alias ke env_allow (dipakai kode lama)."""
    return env_allow()


def global_kill_on(db) -> bool:
    """Flag kill-switch global di DB, di-set super admin. Tanpa restart."""
    try:
        v = db.execute(text("SELECT value FROM wa_flag WHERE name = 'global_kill'")).scalar()
        return v == "1"
    except Exception:
        return False


def gateway_blocker(s) -> Optional[str]:
    """Kesiapan KONFIGURASI gateway (terpisah dari niat user / master toggle)."""
    if s is None:
        return "no_gateway"
    if not s.enabled:
        return "gateway_off"
    if (s.provider or "").lower() in TOKEN_REQUIRED and not s.api_token_encrypted:
        return "no_token"
    if not (s.target or "").strip():
        return "no_target"
    return None


def _scope_alive(db, scope_key: str) -> bool:
    """Scope yatim (user/OLT sudah dihapus) dilewati engine."""
    if scope_key == "global":
        return True
    kind, _, raw = scope_key.partition(":")
    if not raw.isdigit():
        return False
    if kind == "user":
        return db.query(User.id).filter(User.id == int(raw), User.is_active == True).first() is not None  # noqa: E712
    if kind == "olt":
        return db.query(OLT.id).filter(OLT.id == int(raw)).first() is not None
    return False


def set_engine_enabled(db, scope_key: str, enabled: bool) -> bool:
    """Update engine_enabled. Kalau transisi OFF->ON, catat baseline untuk
    tiap rule aktif supaya kondisi existing tidak memicu burst WA."""
    row = _get_settings(db, scope_key)
    if not row:
        return False
    was_on = bool(row.engine_enabled)
    row.engine_enabled = bool(enabled)
    db.commit()

    if not was_on and enabled:
        try:
            events = [r[0] for r in db.query(WaAlertRule.event)
                        .filter(WaAlertRule.scope_key == scope_key,
                                WaAlertRule.enabled == True).all()]  # noqa: E712
        except Exception:
            events = []
        for ev in events:
            try:
                baseline_state(db, scope_key, ev)
            except Exception as exc:
                log.warning("baseline %s/%s gagal: %s", scope_key, ev, exc)
    return True


def set_engine_locked(db, scope_key: str, locked: bool) -> bool:
    """Lock/unlock. Lock memaksa engine_enabled=0 juga."""
    row = _get_settings(db, scope_key)
    if not row:
        return False
    row.engine_locked = bool(locked)
    if locked:
        row.engine_enabled = False
    db.commit()
    return True


def engine_state(db, scope_key: str):
    """Satu-satunya sumber kebenaran. Return (efektif: bool, alasan_blokir: str | None).
    Baca DB setiap panggilan, jangan di-cache (supaya OFF instan di multi-worker)."""
    if not env_allow():
        return False, "env_kill"
    if global_kill_on(db):
        return False, "global_kill"
    s = _get_settings(db, scope_key)
    why = gateway_blocker(s)
    if why:
        return False, why
    if not _scope_alive(db, scope_key):
        return False, "scope_orphan"
    if s.engine_locked:
        return False, "locked_by_admin"
    if not s.engine_enabled:
        return False, "master_off"
    return True, None


def wa_alert_intervals() -> Dict[str, int]:
    """Dipakai app.py saat mendaftarkan job. Env typo lama WA_ALERT_UNCGF_INTERVAL tetap dibaca."""
    return {
        "detect": max(5, _env_int("WA_ALERT_INTERVAL", 60)),
        "uncfg": max(30, _env_int("WA_ALERT_UNCFG_INTERVAL", 300, alt="WA_ALERT_UNCGF_INTERVAL")),
        "dispatch": max(5, _env_int("WA_ALERT_DISPATCH_INTERVAL", 15)),
    }


def _utcnow() -> datetime:
    return datetime.utcnow()


def _iso(dt: Optional[datetime]) -> Optional[str]:
    return dt.isoformat() + "Z" if dt else None


def _tz_label() -> str:
    off = _env_int("WA_ALERT_TZ_OFFSET", 7)
    return {7: "WIB", 8: "WITA", 9: "WIT"}.get(off, "UTC%+d" % off)


def fmt_time(dt: datetime) -> str:
    local = dt + timedelta(hours=_env_int("WA_ALERT_TZ_OFFSET", 7))
    return local.strftime("%d/%m/%Y %H:%M") + " " + _tz_label()


def fmt_duration(seconds: float) -> str:
    s = max(0, int(seconds))
    if s < 60:
        return "%d detik" % s
    m = s // 60
    if m < 60:
        return "%d menit" % m
    h, m = divmod(m, 60)
    return "%d jam %d menit" % (h, m) if m else "%d jam" % h


def _first(obj: Any, names: Tuple[str, ...], default: str = "") -> str:
    """Ambil atribut pertama yang terisi (toleran terhadap beda nama kolom)."""
    for n in names:
        v = getattr(obj, n, None)
        if v not in (None, ""):
            return str(v)
    return default


_NAME_ATTRS = ("name", "description", "customer_name", "username")
_SN_ATTRS = ("sn", "serial_number", "serial")


def _vars(**kw: Any) -> Dict[str, str]:
    d = {k: "-" for k in VAR_KEYS}
    d.update({k: str(v) for k, v in kw.items() if v not in (None, "")})
    return d


_VAR_RE = re.compile(r"\{(\w+)\}")


def render_template(tpl: str, values: Dict[str, Any]) -> str:
    """Ganti {var}. Var tak dikenal dibiarkan apa adanya (typo di template jadi kelihatan)."""
    return _VAR_RE.sub(lambda m: str(values.get(m.group(1), m.group(0))), tpl)


def _to_dt(ts: Any) -> Optional[datetime]:
    if ts is None:
        return None
    if isinstance(ts, datetime):
        return ts
    try:
        return datetime.fromtimestamp(float(ts), tz=timezone.utc).replace(tzinfo=None)
    except (TypeError, ValueError, OverflowError, OSError):
        return None


# ───────────────────────── settings / rule / template ─────────────────────────
def _get_settings(db, scope_key: str):
    return db.query(WaSetting).filter(WaSetting.scope_key == scope_key).first()


def _gateway_on(db, scope_key: str) -> bool:
    s = _get_settings(db, scope_key)
    return bool(s and s.enabled)


def seed_defaults(db, scope_key: str) -> None:
    """Buat rule (enabled=0) dan template default yang belum ada. Idempotent + race-safe."""
    from sqlalchemy.dialects.sqlite import insert as _sqlite_insert
    for ev, d in DEFAULT_RULES.items():
        stmt = _sqlite_insert(WaAlertRule).values(
            scope_key=scope_key, event=ev, enabled=False, **d
        ).on_conflict_do_nothing(index_elements=["scope_key", "event"])
        db.execute(stmt)
    for ev, tpl in DEFAULT_TEMPLATES.items():
        stmt = _sqlite_insert(WaTemplate).values(
            scope_key=scope_key, event=ev, message_template=tpl
        ).on_conflict_do_nothing(index_elements=["scope_key", "event"])
        db.execute(stmt)
    db.commit()


def get_template(db, scope_key: str, event: str) -> str:
    t = (
        db.query(WaTemplate)
        .filter(WaTemplate.scope_key == scope_key, WaTemplate.event == event)
        .first()
    )
    if t and (t.message_template or "").strip():
        return t.message_template
    return DEFAULT_TEMPLATES.get(event, "{message}")


def get_rule(db, scope_key: str, event: str):
    return (
        db.query(WaAlertRule)
        .filter(WaAlertRule.scope_key == scope_key, WaAlertRule.event == event)
        .first()
    )


# ───────────────────────── logika inti: kapan kirim ─────────────────────────
MIN_COOLDOWN_MIN = 5
MIN_REMIND_MIN = 30


def _cooldown_sec(rule) -> int:
    return max(rule.cooldown_min or 0, MIN_COOLDOWN_MIN) * 60


def _remind_sec(rule) -> int:
    r = rule.remind_after_min or 0
    return 0 if r <= 0 else max(r, MIN_REMIND_MIN) * 60


def should_fire(state, rule, now: datetime) -> bool:
    """
    Sekali per episode. Episode = rentang kondisi aktif sejak state_since.
      - belum mencapai min_duration           -> tidak
      - belum pernah dikirim                  -> ya
      - episode ini sudah dikirim             -> ya hanya jika remind_after_min > 0 dan sudah lewat
      - episode baru (flapping), pernah kirim -> ya hanya jika cooldown_min sudah lewat
    """
    if not state.active or state.state_since is None:
        return False
    if (now - state.state_since).total_seconds() < (rule.min_duration_sec or 0):
        return False
    last = state.last_sent_at
    if last is None:
        return True
    since_last = (now - last).total_seconds()
    if last >= state.state_since:
        remind = _remind_sec(rule)
        return remind > 0 and since_last >= remind
    return since_last >= _cooldown_sec(rule)


# ───────────────────────── konteks OLT + stale guard ─────────────────────────
def _olt_context(db, now: datetime, scope_key: str = "global"):
    """Ambil OLT yang relevan utk scope. PENTING: filter per tenant, cegah bocor."""
    stale = _env_int("WA_ALERT_STALE_SEC", 300)
    q = db.query(OLT)
    if scope_key.startswith("olt:"):
        try:
            oid = int(scope_key.split(":", 1)[1])
        except (ValueError, IndexError):
            olts = []
        else:
            olts = q.filter(OLT.id == oid).all()
    elif scope_key.startswith("user:"):
        try:
            uid = int(scope_key.split(":", 1)[1])
        except (ValueError, IndexError):
            olts = []
        else:
            olts = q.filter(OLT.owner_user_id == uid).all()
    else:  # "global" atau fallback
        olts = q.all()
    names = {o.id: (getattr(o, "name", None) or "OLT-%s" % o.id) for o in olts}
    fresh = {
        o.id
        for o in olts
        if o.last_polled and (now - o.last_polled).total_seconds() <= stale
    }
    return olts, names, fresh


def _covered(base_ids: Set[int], rule) -> Set[int]:
    allowed = {int(x) for x in (rule.olt_ids or [])}
    return set(base_ids) & allowed if allowed else set(base_ids)


# ───────────────────────── kolektor kondisi ─────────────────────────
def _collect_onu(db, event: str, covered: Set[int], names: Dict[int, str]) -> Conds:
    if not covered:
        return {}
    status_val = "los" if event == "onu_los" else "offline"
    rows = (
        db.query(ONU)
        .filter(func.lower(ONU.status) == status_val, ONU.olt_id.in_(list(covered)))
        .all()
    )
    conds: Conds = {}
    for o in rows:
        name = _first(o, _NAME_ATTRS)
        sn = _first(o, _SN_ATTRS)
        shown = name or sn or "ONU#%s" % o.id
        olt_name = names.get(o.olt_id, "OLT-%s" % o.olt_id)
        conds[(o.olt_id, str(o.id))] = {
            "label": "%s / %s" % (olt_name, shown),
            "short": "%s (%s)" % (name, sn) if name and sn else shown,
            "vars": _vars(olt_name=olt_name, onu_name=shown, sn=sn, status=STATUS_LABEL[event]),
        }
    return conds


def _collect_alerts(db, rule, names: Dict[int, str], covered: Set[int], now: datetime) -> Conds:
    min_rank = SEVERITY_RANK.get((rule.severity_min or "warning").lower(), 1)
    cutoff = now - timedelta(hours=ALERT_LOOKBACK_H)
    rows = (
        db.query(Alert)
        .filter(Alert.resolved == False, Alert.created_at >= cutoff)  # noqa: E712
        .all()
    )
    conds: Conds = {}
    for a in rows:
        olt_id = a.olt_id or 0
        if olt_id not in covered:
            continue
        sev = (a.severity or "warning").lower()
        if SEVERITY_RANK.get(sev, 1) < min_rank:
            continue
        olt_name = names.get(olt_id, "Sistem") if olt_id else "Sistem"
        title = a.title or "Alert"
        conds[(olt_id, str(a.id))] = {
            "label": "%s / %s" % (olt_name, title),
            "short": title,
            "vars": _vars(
                olt_name=olt_name, severity=sev, title=title,
                message=(a.message or "")[:300], status=STATUS_LABEL["system_alert"],
            ),
        }
    return conds


def _default_uncfg_provider() -> Dict[int, Tuple[List[dict], Optional[datetime]]]:
    """Baca cache uncfg dari routers/sync.py. Kalau cache kosong → fallback live telnet."""
    from routers import sync as sync_mod

    out: Dict[int, Tuple[List[dict], Optional[datetime]]] = {}
    for olt_id, entry in list(sync_mod._UNCFG_CACHE.items()):
        raw, ts = entry[0], entry[1]
        try:
            items = list(raw) if isinstance(raw, (list, tuple)) else sync_mod._parse_onu_uncfg(raw)
        except Exception:
            log.warning("parse uncfg OLT %s gagal", olt_id, exc_info=True)
            continue
        out[olt_id] = (items, _to_dt(ts))

    # Fallback: cache kosong → query live ke OLT (khusus user telnet/SSH)
    if not out:
        log.info("uncfg cache kosong → fallback live query")
        out = _live_uncfg_provider()
    return out


def _live_uncfg_provider() -> Dict[int, Tuple[List[dict], Optional[datetime]]]:
    """Query live ke OLT via telnet/SSH `show gpon onu uncfg`.
    Dipakai kalau cache kosong (mis. user pakai telnet, bukan SNMP)."""
    result: Dict[int, Tuple[List[dict], Optional[datetime]]] = {}
    try:
        from routers.onu import _make_client
        from routers import sync as sync_mod
    except Exception as exc:
        log.warning("live uncfg: import gagal: %s", exc)
        return result

    db = _open_session()
    try:
        olts = db.query(OLT).all()
        for olt in olts:
            try:
                client = _make_client(olt)
                conn = client._connect()
                try:
                    raw = conn.send_command_timing(
                        "show gpon onu uncfg", read_timeout=20, last_read=0.5
                    )
                finally:
                    try:
                        conn.disconnect()
                    except Exception:
                        pass
                items = sync_mod._parse_onu_uncfg(raw)
                result[olt.id] = (items, _utcnow())
                log.info("live uncfg OLT %s: %d ONU belum terdaftar", olt.id, len(items))
            except Exception as exc:
                log.warning("live uncfg OLT %s gagal: %s", getattr(olt, "id", "?"), exc)
    finally:
        db.close()
    return result


def _collect_uncfg(db, rule, fresh: Set[int], names: Dict[int, str], now: datetime, provider):
    stale = _env_int("WA_ALERT_STALE_SEC", 300)
    data = (provider or _default_uncfg_provider)()
    allowed = _covered(fresh, rule)
    conds: Conds = {}
    covered: Set[int] = set()
    for olt_id, (items, ts) in data.items():
        if olt_id not in allowed:
            continue                      # OLT stale / di luar filter rule
        if ts is None or (now - ts).total_seconds() > stale:
            continue                      # cache terlalu tua -> jangan simpulkan apa-apa
        covered.add(olt_id)
        olt_name = names.get(olt_id, "OLT-%s" % olt_id)
        for it in items:
            sn = ""
            for k in ("sn", "serial", "serial_number", "SN"):
                if it.get(k):
                    sn = str(it[k]).strip().upper()
                    break
            if not sn:
                continue
            port = ""
            for k in ("interface", "onu_index", "pon", "port", "onu"):
                if it.get(k):
                    port = str(it[k])
                    break
            conds[(olt_id, sn)] = {
                "label": "%s / %s" % (olt_name, sn),
                "short": sn,
                "vars": _vars(olt_name=olt_name, onu_name=port, sn=sn, status=STATUS_LABEL["onu_uncfg"]),
            }
    return conds, covered, names


def _gather(db, event: str, rule, now: datetime, provider=None, scope_key: str = "global"):
    olts, names, fresh = _olt_context(db, now, scope_key)
    if event in ("onu_los", "onu_offline"):
        covered = _covered(fresh, rule)
        return _collect_onu(db, event, covered, names), covered, names
    if event == "system_alert":
        covered = _covered({o.id for o in olts} | {0}, rule)
        return _collect_alerts(db, rule, names, covered, now), covered, names
    if event == "onu_uncfg":
        return _collect_uncfg(db, rule, fresh, names, now, provider)
    raise ValueError("event tidak dikenal: %s" % event)


# ───────────────────────── enqueue ke log ─────────────────────────
def enqueue_alert(
    db, *, scope_key: str, event: str, olt_id: Optional[int], device_key: str,
    device_label: str, vars_: Dict[str, str], dedupe_key: str, now: datetime,
    batch_count: int = 1, meta_extra: Optional[dict] = None,
):
    """Tulis 1 baris pending. Return None jika dedupe_key sudah ada (tidak dobel)."""
    if db.query(WaAlertLog.id).filter(WaAlertLog.dedupe_key == dedupe_key).first():
        return None
    meta = {"vars": vars_}
    meta.update(meta_extra or {})
    row = WaAlertLog(
        scope_key=scope_key, event=event, olt_id=olt_id or None,
        device_key=device_key, device_label=(device_label or "")[:255],
        message="", meta=meta, status="pending", attempts=0,
        batch_count=batch_count, dedupe_key=dedupe_key[:255], created_at=now,
    )
    db.add(row)
    db.flush()
    return row


def _ts(dt: datetime) -> str:
    return dt.strftime("%Y%m%d%H%M%S")


def _recent_mass(db, scope_key: str, event: str, olt_id: int, now: datetime, window_min: int) -> bool:
    if window_min <= 0:
        return False
    since = now - timedelta(minutes=window_min)
    return (
        db.query(WaAlertLog.id)
        .filter(
            WaAlertLog.scope_key == scope_key, WaAlertLog.event == "mass_event",
            WaAlertLog.device_key == event, WaAlertLog.olt_id == (olt_id or None),
            WaAlertLog.created_at >= since,
        )
        .first()
        is not None
    )


def _queue_single(db, scope_key, event, key, st, info, now) -> bool:
    olt_id, dev = key
    vars_ = dict(info["vars"])
    vars_.update(
        since=fmt_time(st.state_since),
        duration=fmt_duration((now - st.state_since).total_seconds()),
        time=fmt_time(now),
    )
    dedupe = "%s:%s:%s:%s:%s:%s" % (scope_key, event, olt_id, dev, _ts(st.state_since), st.send_count or 0)
    row = enqueue_alert(
        db, scope_key=scope_key, event=event, olt_id=olt_id, device_key=dev,
        device_label=info["label"], vars_=vars_, dedupe_key=dedupe, now=now,
    )
    return row is not None


def _queue_mass(db, scope_key, event, olt_id, items, conds, names, now) -> bool:
    n = len(items)
    shown = items[:MASS_LIST_MAX]
    lines = ["%d. %s" % (i, conds[k]["short"]) for i, (k, _) in enumerate(shown, 1)]
    if n > MASS_LIST_MAX:
        lines.append("… dan %d lainnya" % (n - MASS_LIST_MAX))
    olt_name = names.get(olt_id, "Sistem") if olt_id else "Sistem"
    vars_ = _vars(
        olt_name=olt_name, count="%d %s" % (n, "alert" if event == "system_alert" else "ONU"),
        list="\n".join(lines), status=STATUS_LABEL[event], time=fmt_time(now),
    )
    devices = [{"key": k[1], "label": conds[k]["short"]} for k, _ in shown]
    row = enqueue_alert(
        db, scope_key=scope_key, event="mass_event", olt_id=olt_id, device_key=event,
        device_label=olt_name, vars_=vars_, now=now, batch_count=n,
        dedupe_key="%s:mass:%s:%s:%s" % (scope_key, event, olt_id, _ts(now)),
        meta_extra={"source_event": event, "total": n, "devices": devices},
    )
    return row is not None


def _evaluate(db, scope_key, rule, event, conds: Conds, covered: Set[int], names, now,
              *, baseline: bool = False) -> Dict[str, int]:
    stats = {"new": 0, "cleared": 0, "queued": 0, "mass": 0}
    states = {
        (s.olt_id, s.device_key): s
        for s in db.query(WaAlertState)
        .filter(WaAlertState.scope_key == scope_key, WaAlertState.event == event)
        .all()
    }

    # Fix IDOR: filter conds ke OLT milik scope saja (defense in depth)
    _olts, _, _ = _olt_context(db, now, scope_key)
    _allowed = {o.id for o in _olts}
    if rule.olt_ids:
        _allowed &= set(rule.olt_ids)
    conds = {k: v for k, v in conds.items() if k[0] in _allowed}
    covered = set(covered) & _allowed

    # 1) kondisi yang ada sekarang -> buat / aktifkan state
    for key, _info in conds.items():
        st = states.get(key)
        fresh_episode = False
        if st is None:
            st = WaAlertState(
                scope_key=scope_key, event=event, olt_id=key[0], device_key=key[1],
                state_since=now, active=True, send_count=0,
            )
            db.add(st)
            states[key] = st
            stats["new"] += 1
            fresh_episode = True
        elif not st.active:
            st.active = True
            st.state_since = now
            st.cleared_at = None
            fresh_episode = True
        if baseline:
            if fresh_episode:
                st.state_since = now
            st.last_sent_at = now         # tandai "sudah dikirim" TANPA kirim

    # 2) kondisi yang hilang (hanya utk OLT yang datanya segar) -> nonaktif
    for key, st in states.items():
        if st.active and key[0] in covered and key not in conds:
            st.active = False
            st.cleared_at = now
            stats["cleared"] += 1

    if baseline:
        return stats

    # 3) kandidat kirim
    candidates = [(k, st) for k, st in states.items() if k in conds and should_fire(st, rule, now)]
    candidates.sort(key=lambda c: (c[1].state_since, c[0][1]))
    by_olt: Dict[int, List[Tuple[Tuple[int, str], Any]]] = {}
    for k, st in candidates:
        by_olt.setdefault(k[0], []).append((k, st))

    max_individual = _env_int("WA_MAX_INDIVIDUAL", 10)
    for olt_id, items in by_olt.items():
        thr = int(rule.mass_threshold or 0)
        if thr > 0 and _recent_mass(db, scope_key, event, olt_id, now, rule.cooldown_min or 0):
            thr = 1       # sudah ada ringkasan baru-baru ini: susulan digabung ke ringkasan
        # Anti-flood: kalau kandidat >max_individual, paksa mass (apapun setting user)
        if len(items) > max_individual:
            thr = 1
        if thr > 0 and len(items) >= thr:
            if _queue_mass(db, scope_key, event, olt_id, items, conds, names, now):
                stats["mass"] += 1
            for _k, st in items:
                st.last_sent_at = now
                st.send_count = (st.send_count or 0) + 1
        else:
            for k, st in items:
                if _queue_single(db, scope_key, event, k, st, conds[k], now):
                    stats["queued"] += 1
                st.last_sent_at = now
                st.send_count = (st.send_count or 0) + 1
    return stats


def _run_event(db, scope_key, rule, event, now, provider=None, baseline=False):
    conds, covered, names = _gather(db, event, rule, now, provider, scope_key)
    stats = _evaluate(db, scope_key, rule, event, conds, covered, names, now, baseline=baseline)
    db.commit()
    return stats


# ───────────────────────── API engine ─────────────────────────
def wa_alert_detect(db, scope_key: str, now: Optional[datetime] = None) -> Dict[str, Any]:
    """onu_los, onu_offline, system_alert. (uncfg punya job sendiri.)"""
    now = now or _utcnow()
    if not _gateway_on(db, scope_key):
        return {"skipped": "gateway_off"}
    rules = {
        r.event: r
        for r in db.query(WaAlertRule)
        .filter(WaAlertRule.scope_key == scope_key, WaAlertRule.enabled == True)  # noqa: E712
        .all()
    }
    result: Dict[str, Any] = {}
    for ev in ("onu_los", "onu_offline", "system_alert"):
        if ev in rules:
            result[ev] = _run_event(db, scope_key, rules[ev], ev, now)
    return result or {"skipped": "no_rules"}


def wa_alert_uncfg(db, scope_key: str, now: Optional[datetime] = None,
                   provider: Optional[Callable] = None) -> Dict[str, Any]:
    now = now or _utcnow()
    if not _gateway_on(db, scope_key):
        return {"skipped": "gateway_off"}
    rule = get_rule(db, scope_key, "onu_uncfg")
    if not rule or not rule.enabled:
        return {"skipped": "rule_off"}
    return _run_event(db, scope_key, rule, "onu_uncfg", now, provider)


def baseline_state(db, scope_key: str, event: str, now: Optional[datetime] = None,
                   provider: Optional[Callable] = None) -> Dict[str, int]:
    """Dipanggil saat rule diaktifkan: kondisi yang SUDAH ada dicatat aktif tanpa kirim WA."""
    now = now or _utcnow()
    rule = get_rule(db, scope_key, event)
    if not rule:
        return {}
    return _run_event(db, scope_key, rule, event, now, provider, baseline=True)


def cancel_pending(db, scope_key: str, event: str, reason: str = "rule dinonaktifkan") -> int:
    rows = (
        db.query(WaAlertLog)
        .filter(WaAlertLog.scope_key == scope_key, WaAlertLog.status == "pending")
        .all()
    )
    n = 0
    for r in rows:
        if r.event == event or (r.event == "mass_event" and r.device_key == event):
            r.status = "expired"
            r.error = reason
            n += 1
    db.commit()
    return n


def _default_send_fn() -> SendFn:
    from routers.wa import send_wa_message  # lazy: hindari circular import
    return send_wa_message


def wa_alert_dispatch(db, now: Optional[datetime] = None, send_fn: Optional[SendFn] = None) -> Dict[str, int]:
    now = now or _utcnow()
    send_fn = send_fn or _default_send_fn()
    res = {"sent": 0, "retry": 0, "failed": 0, "expired": 0, "reclaimed": 0, "skipped": 0}
    ttl = _env_int("WA_ALERT_PENDING_TTL_MIN", 30)
    max_retry = _env_int("WA_ALERT_MAX_RETRY", 3)
    max_pm = _env_int("WA_ALERT_MAX_PER_MINUTE", 10)

    # 1) pending kedaluwarsa -> expired (alert basi tidak berguna)
    cutoff = now - timedelta(minutes=ttl)
    for r in (
        db.query(WaAlertLog)
        .filter(WaAlertLog.status == "pending",
                func.coalesce(WaAlertLog.next_retry_at, WaAlertLog.created_at) < cutoff)
        .all()
    ):
        r.status = "expired"
        r.error = r.error or "kedaluwarsa (melewati TTL)"
        res["expired"] += 1

    # 2) 'sending' yang macet (proses mati di tengah kirim) -> pending lagi.
    #    next_retry_at dipakai sebagai penanda waktu klaim saat status='sending'.
    lease = now - timedelta(seconds=SENDING_LEASE_SEC)
    for r in db.query(WaAlertLog).filter(WaAlertLog.status == "sending", WaAlertLog.next_retry_at < lease).all():
        r.status = "pending"
        r.next_retry_at = None
        res["reclaimed"] += 1
    db.commit()

    # 3) rate limit
    limit = 200
    if max_pm > 0:
        sent_recent = (
            db.query(func.count(WaAlertLog.id))
            .filter(WaAlertLog.status == "sent", WaAlertLog.sent_at >= now - timedelta(seconds=60))
            .scalar()
            or 0
        )
        limit = max(0, max_pm - sent_recent)
    if limit == 0:
        return res

    # Hanya proses scope yang aktif (engine_state ok). Kalau scope OFF,
    # baris pending-nya dibiarkan expire lewat TTL, bukan dikirim.
    active_scopes = _active_scopes(db)
    if not active_scopes:
        return res
    due = (
        db.query(WaAlertLog.id, WaAlertLog.scope_key)
        .filter(WaAlertLog.status == "pending",
                WaAlertLog.scope_key.in_(active_scopes),
                or_(WaAlertLog.next_retry_at.is_(None), WaAlertLog.next_retry_at <= now))
        .order_by(WaAlertLog.created_at.asc(), WaAlertLog.id.asc())
        .limit(limit)
        .all()
    )

    gateway_cache: Dict[str, bool] = {}
    for rid, scope in due:
        # gateway dimatikan -> jangan sentuh (biarkan pending, nanti expired oleh TTL)
        if scope not in gateway_cache:
            gateway_cache[scope] = _gateway_on(db, scope)
        if not gateway_cache[scope]:
            res["skipped"] += 1
            continue
        # klaim atomik: hanya satu pemanggil yang menang
        claimed = (
            db.query(WaAlertLog)
            .filter(WaAlertLog.id == rid, WaAlertLog.status == "pending")
            .update({"status": "sending", "next_retry_at": now}, synchronize_session=False)
        )
        db.commit()
        if not claimed:
            continue
        row = db.query(WaAlertLog).filter(WaAlertLog.id == rid).one()
        db.refresh(row)

        if not row.message:
            values = _vars()
            values.update((row.meta or {}).get("vars", {}))
            row.message = render_template(get_template(db, row.scope_key, row.event), values)

        src_event = row.device_key if row.event == "mass_event" else row.event
        rule = get_rule(db, row.scope_key, src_event)
        target = rule.target_override if rule and rule.target_override else None

        try:
            ok, err = send_fn(db, row.scope_key, row.message, target)
        except Exception as exc:  # send_fn tidak boleh menjatuhkan job
            ok, err = False, "%s: %s" % (type(exc).__name__, exc)

        row.attempts = (row.attempts or 0) + 1
        if ok:
            row.status = "sent"
            row.sent_at = now
            row.error = None
            row.next_retry_at = None
            res["sent"] += 1
        else:
            row.error = (err or "gagal kirim")[:500]
            if row.attempts > max_retry:
                row.status = "failed"
                row.next_retry_at = None
                res["failed"] += 1
            else:
                row.status = "pending"
                row.next_retry_at = now + timedelta(minutes=BACKOFF_MIN[min(row.attempts, len(BACKOFF_MIN)) - 1])
                res["retry"] += 1
        db.commit()
    return res


def wa_alert_cleanup(db, now: Optional[datetime] = None) -> Dict[str, int]:
    now = now or _utcnow()
    cutoff = now - timedelta(days=_env_int("WA_ALERT_LOG_RETENTION_DAYS", 30))
    n_logs = db.query(WaAlertLog).filter(WaAlertLog.created_at < cutoff).delete(synchronize_session=False)
    n_states = (
        db.query(WaAlertState)
        .filter(WaAlertState.active == False, WaAlertState.cleared_at < cutoff)  # noqa: E712
        .delete(synchronize_session=False)
    )
    db.commit()
    return {"logs": n_logs, "states": n_states}


# ───────────────────────── serialisasi + status (dipakai router) ─────────────────────────
def rule_to_dict(r) -> Dict[str, Any]:
    return {
        "event": r.event, "enabled": bool(r.enabled),
        "min_duration_sec": r.min_duration_sec, "cooldown_min": r.cooldown_min,
        "remind_after_min": r.remind_after_min, "mass_threshold": r.mass_threshold,
        "severity_min": r.severity_min, "olt_ids": r.olt_ids or None,
        "target_override": r.target_override, "updated_at": _iso(r.updated_at),
    }


def log_to_dict(r, with_meta: bool = True) -> Dict[str, Any]:
    d = {
        "id": r.id, "event": r.event, "olt_id": r.olt_id, "device_key": r.device_key,
        "device_label": r.device_label, "message": r.message, "status": r.status,
        "error": r.error, "attempts": r.attempts, "batch_count": r.batch_count,
        "next_retry_at": _iso(r.next_retry_at), "created_at": _iso(r.created_at),
        "sent_at": _iso(r.sent_at),
    }
    if with_meta:
        d["meta"] = r.meta
    return d


def sample_vars(event: str, db=None, scope_key: str = "global") -> Dict[str, str]:
    """Sample placeholder. Kalau db+scope_key ada, ambil OLT/ONU asli dari DB."""
    now = _utcnow()
    olt_name = "OLT-Contoh"
    onu_name = "Pelanggan Contoh"
    sn = "ZTEGC0A1B2C3"
    if db is not None:
        try:
            olts, _names, _fresh = _olt_context(db, now, scope_key)
            if olts:
                _o = olts[0]
                olt_name = getattr(_o, "hostname", None) or getattr(_o, "name", None) or ("OLT-%s" % _o.id)
                _onu = db.query(ONU).filter(ONU.olt_id == _o.id).first()
                if _onu:
                    onu_name = _first(_onu, _NAME_ATTRS) or onu_name
                    sn = _first(_onu, _SN_ATTRS) or sn
        except Exception:
            pass
    return _vars(
        olt_name=olt_name, onu_name=onu_name, sn=sn,
        status=STATUS_LABEL.get(event, "LOS") if event != "mass_event" else "LOS",
        since=fmt_time(now - timedelta(minutes=5)), duration="5 menit", time=fmt_time(now),
        count="12 ONU",
        list="1. " + onu_name + " (" + sn + "), 2. Pelanggan B, ... dan 10 lainnya",
        severity="warning", title="CPU OLT tinggi", message="CPU 92% selama 5 menit",
    )


def status_summary(db, scope_key: str, is_super: bool = False) -> Dict[str, Any]:
    now = _utcnow()
    day = now - timedelta(hours=24)

    def _count(*conds):
        return db.query(func.count(WaAlertLog.id)).filter(WaAlertLog.scope_key == scope_key, *conds).scalar() or 0

    last_err = (
        db.query(WaAlertLog)
        .filter(WaAlertLog.scope_key == scope_key, WaAlertLog.status.in_(("failed", "pending")),
                WaAlertLog.error.isnot(None))
        .order_by(WaAlertLog.id.desc()).first()
    )
    rules_on = [
        r[0] for r in db.query(WaAlertRule.event)
        .filter(WaAlertRule.scope_key == scope_key, WaAlertRule.enabled == True).all()  # noqa: E712
    ]
    s_row = _get_settings(db, scope_key)
    ok, why = engine_state(db, scope_key)
    return {
        # backward compat (UI lama baca ini — TETAP ADA)
        "env_enabled": ok,   # deprecated: pakai "effective"
        # baru
        "engine_enabled": bool(s_row and s_row.engine_enabled),
        "engine_locked":  bool(s_row and s_row.engine_locked),
        "effective":      ok,
        "blocked_by":     why,
        "env_override":   not env_allow(),
        "global_kill":    global_kill_on(db),
        # existing
        "gateway_enabled": _gateway_on(db, scope_key),
        "gateway_ready":   gateway_blocker(s_row) is None,
        "rules_enabled": rules_on,
        "intervals": wa_alert_intervals(),
        "engine": {k: dict(v) for k, v in ENGINE_STATUS.items()} if is_super else {},
        "pending": _count(WaAlertLog.status == "pending"),
        "sent_24h": _count(WaAlertLog.status == "sent", WaAlertLog.created_at >= day),
        "failed_24h": _count(WaAlertLog.status == "failed", WaAlertLog.created_at >= day),
        "last_error": {"at": _iso(last_err.created_at), "error": last_err.error} if last_err else None,
    }


# ───────────────────────── job scheduler (dipanggil APScheduler) ─────────────────────────
def _open_session():
    from database import SessionLocal  # ADAPT: sesuaikan modul session factory-mu
    return SessionLocal()


def _active_scopes(db) -> List[str]:
    """Scope yang lolos engine_state (env ok, global kill off, gateway siap, master ON)."""
    if not env_allow() or global_kill_on(db):
        return []
    rows = (db.query(WaSetting.scope_key)
              .filter(WaSetting.enabled == True,            # noqa: E712
                      WaSetting.engine_enabled == True,     # noqa: E712
                      WaSetting.engine_locked == False)     # noqa: E712
              .all())
    return [r[0] for r in rows if engine_state(db, r[0])[0]]


def _scopes(db) -> List[str]:
    """Backward compat: sekarang = scope yang aktif saja."""
    return _active_scopes(db)


def _per_scope(db, fn: Callable[[Any, str], Any]) -> Dict[str, Any]:
    out: Dict[str, Any] = {}
    errors: List[str] = []
    for sk in _scopes(db):
        try:
            out[sk] = fn(db, sk)
        except Exception as exc:
            db.rollback()
            log.exception("wa_alert scope %s gagal", sk)
            errors.append("%s: %s" % (sk, exc))
    if errors:
        raise RuntimeError("; ".join(errors)[:300])
    return out


def _run_job(name: str, fn: Callable[[Any], Any], gated: bool = True):
    # gated=True: job deteksi (detect/uncfg) — skip kalau env OFF (hemat)
    # gated=False: job dispatch/cleanup — WAJIB jalan supaya pending expire & log purge
    if gated and not env_allow():
        return None
    st = ENGINE_STATUS.setdefault(name, {})
    db = _open_session()
    try:
        out = fn(db)
        st.update(at=_iso(_utcnow()), ok=True, error=None, result=out)
        return out
    except Exception as exc:
        db.rollback()
        log.exception("wa_alert job %s gagal", name)
        st.update(at=_iso(_utcnow()), ok=False, error=str(exc)[:300])
        return None
    finally:
        db.close()


def job_wa_detect():
    return _run_job("detect", lambda db: _per_scope(db, wa_alert_detect))


def job_wa_uncfg():
    return _run_job("uncfg", lambda db: _per_scope(db, wa_alert_uncfg))


def job_wa_dispatch():
    return _run_job("dispatch", wa_alert_dispatch, gated=False)


def job_wa_cleanup():
    return _run_job("cleanup", wa_alert_cleanup, gated=False)
