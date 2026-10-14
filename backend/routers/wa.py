"""
routers/wa.py — WA-1: pengaturan WhatsApp Bot + test kirim.

Endpoint (prefix /api/v1/wa), semua butuh privilege 15:
  GET  /providers                 daftar provider valid (POST juga diterima)
  GET  /settings?scope=...        baca setting (token DIMASKING)
  PUT  /settings?scope=...        simpan setting
  GET  /templates?scope=...       daftar template per event
  PUT  /templates/{event}?scope=  simpan template
  GET  /preview?scope=...         pratinjau pesan (angka ONU asli)
  POST /test?scope=...            kirim pesan test (rate limit 1x / 10 detik)

scope_key: "global" (super admin), "user:<id>" (diri sendiri), "olt:<id>" (OLT yang boleh diakses).

WA-1 hanya event "test": "Jumlah ONU sekarang: X". Alert otomatis = WA-2.

Env:
  WA_ALLOW_PRIVATE_HOSTS       allowlist IP:port atau CIDR:port untuk gateway privat,
                               mis. "127.0.0.1:3000,192.168.1.0/24:8080".
                               Kosong = semua private/loopback DITOLAK (default).
  WA_FERNET_KEY                kunci Fernet; kalau kosong dibuat otomatis di file .wa_fernet.key
  WA_TZ                        zona waktu pesan (default Asia/Jakarta)
"""
import ipaddress
import json
import os
import re
import socket
import threading
import time
import urllib.error
import urllib.parse
import urllib.request
from datetime import datetime, timedelta, timezone
from typing import List, Optional
from cryptography.fernet import Fernet, InvalidToken
from fastapi import APIRouter, Depends, HTTPException, Query
from pydantic import BaseModel, ConfigDict, Field, field_validator
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

from database import get_db
from models import OLT, ONU, User, WaSetting, WaTemplate, AuditLog, WaAlertRule, WaAlertLog
from auth import require_privilege

router = APIRouter(prefix="/api/v1/wa", tags=["whatsapp"])

PRIV_WRITE = 15
_ADMIN = require_privilege(PRIV_WRITE)

# ------------------------------------------------------------------ konstanta
PROVIDERS = {
    "baileys": {"label": "Baileys (self-hosted)", "default_url": "",
                "hint": "URL endpoint kirim pesan di gateway Baileys kamu. Format: JSON {number, message}."},
    "fonnte":  {"label": "Fonnte", "default_url": "https://api.fonnte.com/send",
                "hint": "Token = token device Fonnte."},
    "wablas":  {"label": "Wablas", "default_url": "",
                "hint": "URL: https://<domain-wablas>/api/send-message. Token = token Wablas."},
    "n8n":     {"label": "N8N (webhook)", "default_url": "",
                "hint": "URL webhook N8N. Token opsional (dikirim sebagai Bearer)."},
    "custom":  {"label": "Custom REST", "default_url": "",
                "hint": "POST JSON {target, message}. Token opsional (Bearer)."},
}
TOKEN_OPTIONAL = {"n8n", "custom", "baileys"}

ALLOWED_TOKENS = {"test": {"total_onu", "time", "olt_name", "onu_name", "sn", "status", "since", "duration", "count", "list", "severity", "title", "message"}}
DEFAULT_TEMPLATES = {
    "test": "🧪 *Test Connection*\n\nStatus: ✅ Terhubung ke WhatsApp Gateway\nWaktu: {time}",
}
EVENTS = tuple(DEFAULT_TEMPLATES)

SCOPE_RE = re.compile(r"(global|user:[1-9][0-9]{0,8}|olt:[1-9][0-9]{0,8})")
TARGET_RE = re.compile(r"[0-9A-Za-z@._,+-]{1,255}")

# --- Validasi target khusus untuk alert (6b) ---
_TARGET_PHONE = re.compile(r"\+?[0-9]{8,15}")
_TARGET_GROUP = re.compile(r"[0-9]{10,25}(-[0-9]{5,25})?@g\.us")
MAX_ALERT_TARGETS = 5


def normalize_alert_targets(raw):
    """Validasi ketat untuk target alert. Return string ternormalisasi atau raise ValueError.
    Terima: 1-5 nomor HP (+628xx) ATAU 1-5 JID grup WA (xxx@g.us). Koma sebagai pemisah."""
    out = []
    for tok in (raw or "").split(","):
        tok = tok.strip()
        if not tok:
            continue
        if _TARGET_GROUP.fullmatch(tok):
            pass  # JID grup valid apa adanya
        else:
            tok_clean = re.sub(r"[ \-]", "", tok)
            if not _TARGET_PHONE.fullmatch(tok_clean):
                raise ValueError("Format target tidak valid (nomor HP atau JID grup WA)")
            tok = tok_clean
        if tok not in out:
            out.append(tok)
    if not out or len(out) > MAX_ALERT_TARGETS:
        raise ValueError("Isi 1-%d target (koma sebagai pemisah)" % MAX_ALERT_TARGETS)
    return ",".join(out)
TOKEN_RE = re.compile(r"\{([A-Za-z0-9_]*)\}")
MAX_TEMPLATE = 1000
MAX_URL = 500
MAX_TOKEN = 512
TEST_INTERVAL = 10          # detik
HTTP_TIMEOUT = 10           # detik

_META_HOSTS = {"metadata.google.internal", "metadata", "instance-data"}
# 169.254.169.254 (AWS) TIDAK dimasukkan — sudah ke-cover link-local check.
# fd00:ec2::254 (AWS IPv6) dan 100.100.100.200 (Alibaba) bukan link-local, tetap perlu.
_META_IPS = {ipaddress.ip_address("fd00:ec2::254"),
             ipaddress.ip_address("100.100.100.200")}


# ------------------------------------------------------------------ schema
class SettingIn(BaseModel):
    provider: str
    gateway_url: str
    target: str
    enabled: bool = True
    api_token: Optional[str] = None      # None / "" = biarkan token lama
    clear_token: bool = False            # True = hapus token tersimpan


class TemplateIn(BaseModel):
    message_template: str


# ------------------------------------------------------------------ enkripsi token
_fernet_lock = threading.Lock()
_fernet_obj: Optional[Fernet] = None


def _fernet() -> Fernet:
    global _fernet_obj
    with _fernet_lock:
        if _fernet_obj is not None:
            return _fernet_obj
        key = os.environ.get("WA_FERNET_KEY", "").strip()
        if not key:
            path = os.path.join(os.path.dirname(os.path.abspath(__file__)), ".wa_fernet.key")
            if os.path.exists(path):
                with open(path, "r") as f:
                    key = f.read().strip()
            else:
                key = Fernet.generate_key().decode()
                fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
                with os.fdopen(fd, "w") as f:
                    f.write(key)
        try:
            _fernet_obj = Fernet(key.encode())
        except Exception:
            raise HTTPException(500, "WA_FERNET_KEY tidak valid (harus key Fernet base64 32 byte)")
        return _fernet_obj


def _enc(token: str) -> str:
    return _fernet().encrypt(token.encode()).decode()


def _dec(blob: Optional[str]) -> Optional[str]:
    if not blob:
        return None
    try:
        return _fernet().decrypt(blob.encode()).decode()
    except InvalidToken:
        raise HTTPException(500, "Token tersimpan tidak bisa didekripsi (WA_FERNET_KEY berubah?). Simpan ulang token.")


def _preview_token(blob: Optional[str]) -> Optional[str]:
    t = _dec(blob)
    if not t:
        return None
    return ("••••" + t[-4:]) if len(t) >= 12 else "••••"


# ------------------------------------------------------------------ scope & akses
def _is_super(user) -> bool:
    return bool(getattr(user, "is_super_admin", 0))


def _can_access_olt(db: Session, user, olt_id: int) -> bool:
    """Aturan Phase A: super admin = semua; admin = OLT miliknya; operator/viewer = OLT atasannya.
    Kalau tenancy.py punya helper resmi, ganti isi fungsi ini."""
    if _is_super(user):
        return True
    olt = db.query(OLT.owner_user_id).filter(OLT.id == olt_id).first()
    if not olt or olt[0] is None:
        return False
    owner = user.id if not getattr(user, "owner_user_id", None) else user.owner_user_id
    return olt[0] == owner


def _check_scope(db: Session, user, scope: str) -> str:
    if not SCOPE_RE.fullmatch(scope or ""):
        raise HTTPException(422, 'Scope tidak valid (pakai "global", "user:<id>", atau "olt:<id>")')
    if scope == "global":
        if not _is_super(user):
            raise HTTPException(403, "Scope global hanya untuk Super Admin")
    elif scope.startswith("user:"):
        uid = int(scope.split(":")[1])
        if uid != user.id and not _is_super(user):
            raise HTTPException(403, "Tidak boleh mengakses setting user lain")
    else:
        oid = int(scope.split(":")[1])
        if not _can_access_olt(db, user, oid):
            raise HTTPException(403, "Tidak punya akses ke OLT ini")
    return scope


def _count_onu(db: Session, scope: str) -> int:
    q = db.query(ONU)
    if scope.startswith("olt:"):
        q = q.filter(ONU.olt_id == int(scope.split(":")[1]))
    elif scope.startswith("user:"):
        uid = int(scope.split(":")[1])
        ids = [r[0] for r in db.query(OLT.id).filter(OLT.owner_user_id == uid).all()]
        if not ids:
            return 0
        q = q.filter(ONU.olt_id.in_(ids))
    return q.count()


# ------------------------------------------------------------------ template
def _check_template(event: str, text: str) -> None:
    if event not in EVENTS:
        raise HTTPException(404, 'Event "' + event + '" tidak dikenal (WA-1: ' + ", ".join(EVENTS) + ")")
    text = text or ""
    if not text.strip():
        raise HTTPException(422, "Template tidak boleh kosong")
    if len(text) > MAX_TEMPLATE:
        raise HTTPException(422, "Template maksimal " + str(MAX_TEMPLATE) + " karakter")
    rest = TOKEN_RE.sub("", text)
    if "{" in rest or "}" in rest:
        raise HTTPException(422, "Kurung kurawal tidak lengkap")
    for t in TOKEN_RE.findall(text):
        if t not in ALLOWED_TOKENS[event]:
            raise HTTPException(422, "Placeholder {" + t + "} tidak dikenal (pakai: "
                                + ", ".join("{" + x + "}" for x in sorted(ALLOWED_TOKENS[event])) + ")")


def _now_text() -> str:
    tzname = os.environ.get("WA_TZ", "Asia/Jakarta")
    try:
        from zoneinfo import ZoneInfo
        now = datetime.now(ZoneInfo(tzname))
    except Exception:
        now = datetime.now(timezone(timedelta(hours=7)))
    return now.strftime("%d-%m-%Y %H:%M:%S")


def _get_template(db: Session, scope: str, event: str) -> str:
    row = (db.query(WaTemplate)
           .filter(WaTemplate.scope_key == scope, WaTemplate.event == event).first())
    return row.message_template if row else DEFAULT_TEMPLATES[event]


def _render(template: str, values: dict) -> str:
    out = template
    for k, v in values.items():      # replace biasa, BUKAN str.format (aman dari injeksi {…})
        out = out.replace("{" + k + "}", str(v))
    return out


def _build_test_message(db: Session, scope: str) -> str:
    """Render template test. Placeholder {total_onu} {time} + semua placeholder alert
    ({olt_name}, {onu_name}, {sn}, {since}, {status}, dll) dari sample_vars."""
    tpl = _get_template(db, scope, "test")
    vals = {"total_onu": _count_onu(db, scope), "time": _now_text()}
    try:
        import wa_alerting as _wal
        # Ambil sample untuk event 'onu_los' (mencakup placeholder umum)
        sample = _wal.sample_vars("onu_los", db=db, scope_key=scope)
        # Merge: prioritaskan total_onu + time (nilai real)
        merged = dict(sample)
        merged.update(vals)
        vals = merged
    except Exception:
        pass
    return _render(tpl, vals)


# ------------------------------------------------------------------ validasi gateway (SSRF)
# Hard deny: tidak bisa di-allowlist. Metadata cloud, multicast, reserved.
_HARD_DENY_NETS = None


def _hard_deny_nets():
    global _HARD_DENY_NETS
    if _HARD_DENY_NETS is None:
        _HARD_DENY_NETS = [
            ipaddress.ip_network(n) for n in (
                "169.254.0.0/16",   # link-local (AWS metadata, dll)
                "0.0.0.0/8",        # unspecified
                "224.0.0.0/4",      # multicast IPv4
                "240.0.0.0/4",      # reserved IPv4
                "fe80::/10",        # link-local IPv6
                "ff00::/8",         # multicast IPv6
            )
        ]
    return _HARD_DENY_NETS


def _allowlist():
    """Parse env WA_ALLOW_PRIVATE_HOSTS: comma-separated IP:port atau CIDR:port.
    Contoh: 127.0.0.1:3000,192.168.1.0/24:3000
    Port '*' atau kosong = semua port.
    """
    rules = []
    for item in os.environ.get("WA_ALLOW_PRIVATE_HOSTS", "").split(","):
        item = item.strip()
        if not item:
            continue
        net_str, _, port_str = item.partition(":")
        try:
            net = ipaddress.ip_network(net_str, strict=False)
        except ValueError:
            continue
        port = None
        if port_str and port_str != "*":
            try:
                port = int(port_str)
            except ValueError:
                continue
        rules.append((net, port))
    return rules


def _ip_blocked(ip, port=None) -> Optional[str]:
    # IPv4-mapped IPv6 (::ffff:1.2.3.4) — normalize dulu
    if getattr(ip, "ipv4_mapped", None):
        ip = ip.ipv4_mapped
    if ip in _META_IPS:
        return "alamat metadata cloud diblokir"
    # Hard deny: apapun scope/env, tetap diblokir
    for net in _hard_deny_nets():
        if ip in net:
            return "alamat tidak diizinkan (link-local/reserved/multicast)"
    # Public IP: boleh
    if ip.is_global:
        return None
    # Private/loopback: cek allowlist (IP:port harus cocok)
    for net, allowed_port in _allowlist():
        if ip in net and (allowed_port is None or allowed_port == port):
            return None
    return ("gateway di jaringan privat tidak diizinkan. "
            "Super admin bisa menambah di env WA_ALLOW_PRIVATE_HOSTS (mis. 127.0.0.1:3000)")


def _check_url(url: str, resolve: bool) -> str:
    """Validasi URL gateway. resolve=True: DNS wajib bisa di-resolve & semua IP lolos (dipakai saat kirim)."""
    url = (url or "").strip()
    if not url or len(url) > MAX_URL:
        raise HTTPException(422, "URL gateway wajib diisi (maks " + str(MAX_URL) + " karakter)")
    p = urllib.parse.urlsplit(url)
    if p.scheme not in ("http", "https") or not p.hostname:
        raise HTTPException(422, "URL gateway harus diawali http:// atau https://")
    if p.username or p.password:
        raise HTTPException(422, "URL gateway tidak boleh memuat user:password")
    host = p.hostname.lower()
    if host in _META_HOSTS:
        raise HTTPException(422, "Host metadata cloud diblokir")
    port = p.port or (443 if p.scheme == "https" else 80)
    try:
        ips = [ipaddress.ip_address(host)]
    except ValueError:
        try:
            infos = socket.getaddrinfo(host, port, proto=socket.IPPROTO_TCP)
            ips = [ipaddress.ip_address(i[4][0].split("%")[0]) for i in infos]
        except (socket.gaierror, ValueError):
            if resolve:
                raise HTTPException(422, "Host gateway tidak bisa di-resolve dari server")
            ips = []
    for ip in ips:
        why = _ip_blocked(ip, port)
        if why:
            raise HTTPException(422, "URL gateway ditolak: " + why)
    return url


# ------------------------------------------------------------------ pengiriman HTTP
class _NoRedirect(urllib.request.HTTPRedirectHandler):
    def redirect_request(self, *a, **k):      # redirect bisa membelokkan ke metadata → tolak
        return None


def _http_post(url: str, headers: dict, body: bytes, timeout: int = HTTP_TIMEOUT):
    """Return (status_code, body_text). Fungsi ini yang di-mock di test."""
    req = urllib.request.Request(url, data=body, headers=headers, method="POST")
    opener = urllib.request.build_opener(_NoRedirect)
    try:
        with opener.open(req, timeout=timeout) as r:
            return r.status, r.read(65536).decode("utf-8", "replace")
    except urllib.error.HTTPError as e:
        return e.code, e.read(65536).decode("utf-8", "replace")


def _build_request(provider: str, url: str, token: Optional[str], target: str, message: str):
    """Return (headers, body_bytes). Format Fonnte/Wablas = sesuai dokumentasi umum mereka;
    cek ulang ke dokumen resmi kalau respon gateway menolak."""
    if provider == "fonnte":
        return ({"Authorization": token or "", "Content-Type": "application/x-www-form-urlencoded"},
                urllib.parse.urlencode({"target": target, "message": message}).encode())
    if provider == "wablas":
        return ({"Authorization": token or "", "Content-Type": "application/json"},
                json.dumps({"phone": target, "message": message}).encode())
    headers = {"Content-Type": "application/json"}
    if token:
        headers["Authorization"] = "Bearer " + token
    if provider == "baileys":
        payload = {"number": target, "target": target, "message": message}
    elif provider == "n8n":
        payload = {"event": "test", "target": target, "message": message}
    else:
        payload = {"target": target, "message": message}
    return headers, json.dumps(payload).encode()


_QS_TOKEN = re.compile(r'(?i)([?&;](?:token|key|apikey|api_key|secret|password|pass|auth|access_token|sig|signature)=)[^&\s"\']+')
_BEARER_TOKEN = re.compile(r'(?i)(bearer\s+)[A-Za-z0-9._~+/=-]+')
_URL_USERINFO = re.compile(r'(://)[^/@\s]+@')


def _scrub(text, secrets=()):
    """Bersihkan token/URL userinfo dari pesan error sebelum disimpan ke log."""
    msg = str(text)
    for sec in (secrets or ()):
        if sec and len(sec) >= 6:
            msg = msg.replace(sec, "***")
            try:
                import urllib.parse as _up
                msg = msg.replace(_up.quote(sec, safe=""), "***")
            except Exception:
                pass
    msg = _QS_TOKEN.sub(r"\1***", msg)
    msg = _BEARER_TOKEN.sub(r"\1***", msg)
    msg = _URL_USERINFO.sub(r"\1***@", msg)
    return msg[:300]


def _deliver(row: WaSetting, message: str) -> dict:
    url = _check_url(row.gateway_url, resolve=True)
    token = _dec(row.api_token_encrypted)
    headers, body = _build_request(row.provider, url, token, row.target, message)
    try:
        code, text = _http_post(url, headers, body)
    except (urllib.error.URLError, socket.timeout, OSError) as e:
        return {"ok": False, "status_code": None,
                "detail": _scrub("Gagal terhubung ke gateway: " + str(getattr(e, "reason", e)), [token])}
    ok = 200 <= code < 300
    detail = "Terkirim" if ok else "Gateway membalas HTTP " + str(code)
    try:                                         # Fonnte & sejenisnya: HTTP 200 tapi {"status": false}
        j = json.loads(text)
        if isinstance(j, dict) and j.get("status") in (False, "false"):
            ok = False
            detail = "Gateway menolak: " + str(j.get("reason") or j.get("message") or "tanpa alasan")[:200]
    except ValueError:
        pass
    return {"ok": ok, "status_code": code, "detail": detail}


# ------------------------------------------------------------------ rate limit test
_rl_lock = threading.Lock()
_rl_last: dict = {}


def _rate_limit(key: str) -> None:
    now = time.monotonic()
    with _rl_lock:
        last = _rl_last.get(key)
        if last is not None and now - last < TEST_INTERVAL:
            wait = int(TEST_INTERVAL - (now - last)) + 1
            raise HTTPException(429, "Test dibatasi 1x per " + str(TEST_INTERVAL)
                                + " detik. Coba lagi " + str(wait) + " detik lagi.",
                                headers={"Retry-After": str(wait)})
        _rl_last[key] = now
        if len(_rl_last) > 500:                                  # bersihkan entri lama
            for k in [k for k, v in _rl_last.items() if now - v > 3600]:
                _rl_last.pop(k, None)


# ------------------------------------------------------------------ util
def _audit(db: Session, user, action: str, target: str, detail: Optional[str] = None,
           result: str = "success") -> None:
    db.add(AuditLog(username=getattr(user, "username", None), action=action,
                    target=target, detail=detail, result=result))


def _ser(row: Optional[WaSetting], scope: str) -> dict:
    if not row:
        return {"scope": scope, "configured": False, "provider": "", "gateway_url": "",
                "target": "", "enabled": False, "has_token": False, "token_preview": None,
                "engine_enabled": False, "engine_locked": False,
                "updated_at": None}
    return {"scope": scope, "configured": True, "provider": row.provider,
            "gateway_url": row.gateway_url, "target": row.target, "enabled": bool(row.enabled),
            "has_token": bool(row.api_token_encrypted),
            "token_preview": _preview_token(row.api_token_encrypted),
            "engine_enabled": bool(row.engine_enabled),
            "engine_locked": bool(row.engine_locked),
            "updated_at": row.updated_at.isoformat() if row.updated_at else None}


def _get_row(db: Session, scope: str) -> Optional[WaSetting]:
    return db.query(WaSetting).filter(WaSetting.scope_key == scope).first()


# ------------------------------------------------------------------ endpoints
@router.api_route("/providers", methods=["GET", "POST"])
def list_providers(user: User = Depends(_ADMIN)):
    return {"items": [dict(id=k, **v, token_required=k not in TOKEN_OPTIONAL) for k, v in PROVIDERS.items()]}


@router.get("/settings")
def get_settings(scope: str = "global", db: Session = Depends(get_db), user: User = Depends(_ADMIN)):
    _check_scope(db, user, scope)
    return _ser(_get_row(db, scope), scope)


@router.put("/settings")
def put_settings(body: SettingIn, scope: str = "global",
                 db: Session = Depends(get_db), user: User = Depends(_ADMIN)):
    _check_scope(db, user, scope)
    provider = (body.provider or "").strip().lower()
    if provider not in PROVIDERS:
        raise HTTPException(422, "Provider tidak valid (pakai: " + ", ".join(PROVIDERS) + ")")
    url = _check_url(body.gateway_url, resolve=False)
    target = (body.target or "").strip()
    if not TARGET_RE.fullmatch(target):
        raise HTTPException(422, "Target: 1-255 karakter (angka, huruf, + @ . _ , -), tanpa spasi")
    new_token = (body.api_token or "").strip()
    if len(new_token) > MAX_TOKEN:
        raise HTTPException(422, "Token terlalu panjang (maks " + str(MAX_TOKEN) + ")")

    row = _get_row(db, scope)
    now = datetime.utcnow()
    if not row:
        row = WaSetting(scope_key=scope, created_at=now)
        db.add(row)
    if body.clear_token:
        row.api_token_encrypted = None
    elif new_token:
        row.api_token_encrypted = _enc(new_token)
    if provider not in TOKEN_OPTIONAL and not row.api_token_encrypted:
        raise HTTPException(422, "Token wajib diisi untuk provider " + PROVIDERS[provider]["label"])
    row.provider, row.gateway_url, row.target = provider, url, target
    row.enabled = bool(body.enabled)
    row.updated_at = now
    _audit(db, user, "wa.settings.update",
           "scope=" + scope, "provider=" + provider + " enabled=" + str(row.enabled))   # token TIDAK dicatat
    try:
        db.commit()
    except IntegrityError:
        db.rollback()
        raise HTTPException(409, "Setting scope ini baru saja dibuat user lain. Muat ulang.")
    db.refresh(row)
    return _ser(row, scope)


@router.get("/templates")
def get_templates(scope: str = "global", db: Session = Depends(get_db), user: User = Depends(_ADMIN)):
    _check_scope(db, user, scope)
    rows = {r.event: r for r in db.query(WaTemplate).filter(WaTemplate.scope_key == scope).all()}
    items = []
    for ev in EVENTS:
        r = rows.get(ev)
        items.append({"event": ev,
                      "message_template": r.message_template if r else DEFAULT_TEMPLATES[ev],
                      "default_template": DEFAULT_TEMPLATES[ev],
                      "is_default": r is None,
                      "placeholders": sorted(ALLOWED_TOKENS[ev]),
                      "updated_at": r.updated_at.isoformat() if r and r.updated_at else None})
    return {"scope": scope, "items": items}


@router.put("/templates/{event}")
def put_template(event: str, body: TemplateIn, scope: str = "global",
                 db: Session = Depends(get_db), user: User = Depends(_ADMIN)):
    _check_scope(db, user, scope)
    text = body.message_template.replace("\r\n", "\n").strip("\n")
    _check_template(event, text)
    row = (db.query(WaTemplate)
           .filter(WaTemplate.scope_key == scope, WaTemplate.event == event).first())
    now = datetime.utcnow()
    if row:
        row.message_template, row.updated_at = text, now
    else:
        db.add(WaTemplate(scope_key=scope, event=event, message_template=text, updated_at=now))
    _audit(db, user, "wa.template.update", "scope=" + scope + " event=" + event)
    try:
        db.commit()
    except IntegrityError:
        db.rollback()
        raise HTTPException(409, "Template baru saja diubah user lain. Muat ulang.")
    return {"ok": True, "event": event, "message_template": text}


@router.get("/preview")
def preview(scope: str = "global", db: Session = Depends(get_db), user: User = Depends(_ADMIN)):
    _check_scope(db, user, scope)
    return {"scope": scope, "total_onu": _count_onu(db, scope),
            "message": _build_test_message(db, scope)}


def send_wa_message(db, scope_key, text, target_override=None):
    """Wrapper untuk engine WA-2. Return (ok: bool, error: str|None). Tidak raise."""
    row = _get_row(db, scope_key)
    if not row:
        return False, "Setting gateway belum dibuat"
    original = row.target
    if target_override:
        try:
            target_override = normalize_alert_targets(target_override)
        except ValueError as e:
            return False, "Target override invalid: %s" % e
    if target_override and target_override != original:
        row.target = target_override
    try:
        res = _deliver(row, text)
        return res["ok"], res["detail"]
    except HTTPException as exc:
        return False, str(exc.detail)
    except Exception as exc:
        return False, "%s: %s" % (type(exc).__name__, exc)
    finally:
        row.target = original


@router.post("/test")
def send_test(scope: str = "global", db: Session = Depends(get_db), user: User = Depends(_ADMIN)):
    _check_scope(db, user, scope)
    row = _get_row(db, scope)
    if not row:
        raise HTTPException(422, "Setting WhatsApp untuk scope ini belum disimpan")
    _rate_limit(scope + "|" + str(user.id))
    message = _build_test_message(db, scope)
    res = _deliver(row, message)
    _audit(db, user, "wa.test", "scope=" + scope, res["detail"],
           result="success" if res["ok"] else "failed")
    db.commit()
    if not res["ok"]:
        raise HTTPException(502, res["detail"])
    return {"ok": True, "message": message, "detail": res["detail"]}

# ============= WA-2: Rate limit test rule =============
_test_rl_lock = threading.Lock()
_test_rl: dict = {}
_TEST_COOLDOWN_SEC = 30


def _test_rate_limit(scope: str, event: str) -> None:
    """Cooldown server-side untuk tombol Test di tab Alert Rules."""
    key = (scope, event)
    now = time.monotonic()
    with _test_rl_lock:
        last = _test_rl.get(key)
        if last is not None and now - last < _TEST_COOLDOWN_SEC:
            wait = int(_TEST_COOLDOWN_SEC - (now - last)) + 1
            raise HTTPException(429, "Test dibatasi 1x per " + str(_TEST_COOLDOWN_SEC) + " detik. Coba lagi " + str(wait) + " detik.",
                                headers={"Retry-After": str(wait)})
        _test_rl[key] = now
        if len(_test_rl) > 500:
            for k in [k for k, v in _test_rl.items() if now - v > 3600]:
                _test_rl.pop(k, None)


# ============= WA-2: Engine master toggle =============

_BLOCK_MSG = {
    "env_kill": "Dimatikan darurat oleh admin server (env).",
    "global_kill": "Dimatikan darurat oleh admin.",
    "no_gateway": "Gateway belum diatur. Isi dulu di tab Setting.",
    "gateway_off": "Bot belum diaktifkan di tab Setting.",
    "no_token": "Token gateway belum diisi di tab Setting.",
    "no_target": "Nomor tujuan belum diisi di tab Setting.",
    "scope_orphan": "Akun atau OLT untuk scope ini sudah tidak aktif.",
    "locked_by_admin": "Dikunci oleh admin.",
    "master_off": "Alert WA otomatis sedang mati (master toggle OFF).",
}


class EngineIn(BaseModel):
    enabled: Optional[bool] = None
    locked: Optional[bool] = None


class KillIn(BaseModel):
    enabled: bool


@router.put("/engine")
def put_engine(body: EngineIn, scope: str = Query(...),
               db: Session = Depends(get_db), user: User = Depends(_ADMIN)):
    _check_scope(db, user, scope)
    import wa_alerting as wal

    if body.enabled is None and body.locked is None:
        raise HTTPException(422, "Isi enabled dan/atau locked")

    row = _get_row(db, scope)
    if row is None:
        raise HTTPException(409, _BLOCK_MSG["no_gateway"])

    # --- Validasi dulu semua, baru apply ---
    if body.locked is not None and not _is_super(user):
        raise HTTPException(403, "Hanya super admin yang boleh mengunci")
    if body.locked and body.enabled:
        raise HTTPException(422, "Tidak bisa mengunci sambil menyalakan")

    if body.enabled:
        if row.engine_locked and not _is_super(user):
            raise HTTPException(403, _BLOCK_MSG["locked_by_admin"])
        if not wal.env_allow():
            raise HTTPException(409, _BLOCK_MSG["env_kill"])
        if wal.global_kill_on(db):
            raise HTTPException(409, _BLOCK_MSG["global_kill"])
        why = wal.gateway_blocker(row)
        if why:
            raise HTTPException(409, _BLOCK_MSG.get(why, "Gateway belum siap"))

    # --- Apply ---
    if body.locked is not None:
        wal.set_engine_locked(db, scope, body.locked)
    if body.enabled is not None:
        wal.set_engine_enabled(db, scope, bool(body.enabled))

    _audit(db, user, "wa.engine.update", "scope=" + scope,
           "enabled=%s locked=%s" % (body.enabled, body.locked))
    db.commit()

    row = _get_row(db, scope)
    ok, why = wal.engine_state(db, scope)
    return {"scope": scope,
            "engine_enabled": bool(row.engine_enabled),
            "engine_locked": bool(row.engine_locked),
            "effective": ok,
            "blocked_by": why}


@router.put("/global-kill")
def put_global_kill(body: KillIn, db: Session = Depends(get_db),
                    user: User = Depends(_ADMIN)):
    if not _is_super(user):
        raise HTTPException(403, "Hanya super admin")
    from sqlalchemy import text as _text
    db.execute(_text("""INSERT INTO wa_flag(name, value, updated_at, updated_by)
                        VALUES ('global_kill', :v, :now, :u)
                        ON CONFLICT(name) DO UPDATE
                          SET value=:v, updated_at=:now, updated_by=:u"""),
               {"v": "1" if body.enabled else "0",
                "now": datetime.utcnow().strftime("%Y-%m-%d %H:%M:%S.%f"),
                "u": getattr(user, "username", "")})
    _audit(db, user, "wa.global_kill", "global", "enabled=%s" % body.enabled)
    db.commit()
    return {"global_kill": body.enabled}


# ============= WA-2: Alert Rules & Log =============

class AlertRuleIn(BaseModel):
    model_config = ConfigDict(extra="forbid")
    enabled: Optional[bool] = None
    min_duration_sec: Optional[int] = Field(None, ge=0, le=86400)
    cooldown_min: Optional[int] = Field(None, ge=5, le=10080)
    remind_after_min: Optional[int] = Field(None, ge=0, le=10080)
    mass_threshold: Optional[int] = Field(None, ge=0, le=1000)
    severity_min: Optional[str] = None
    olt_ids: Optional[List[int]] = None
    target_override: Optional[str] = Field(None, max_length=255)

    @field_validator("remind_after_min")
    @classmethod
    def _validate_remind(cls, v):
        """Minimum 30 menit kalau aktif. Cegah spam: remind terlalu pendek
        bisa bikin nomor WA kena ban (mis. remind=1 = 60 WA/jam)."""
        if v is None or v == 0:
            return v
        if v < 30:
            raise ValueError("Reminder minimal 30 menit (0 = nonaktif). "
                             "Reminder terlalu pendek berisiko membuat nomor WA kena ban.")
        return v

    @field_validator("target_override")
    @classmethod
    def _validate_target(cls, v):
        if v is None:
            return v
        v = (v or "").strip()
        if not v:
            return None
        try:
            return normalize_alert_targets(v)
        except ValueError as e:
            raise ValueError(str(e))


_NULLABLE = {"severity_min", "olt_ids", "target_override"}


def _dump(model):
    return model.model_dump(exclude_unset=True) if hasattr(model, "model_dump") else model.dict(exclude_unset=True)


@router.get("/alert-rules")
def list_alert_rules(scope: str = "global", db: Session = Depends(get_db), user: User = Depends(_ADMIN)):
    _check_scope(db, user, scope)
    import wa_alerting as wal
    wal.seed_defaults(db, scope)
    rules = db.query(WaAlertRule).filter(WaAlertRule.scope_key == scope).all()
    order = {e: i for i, e in enumerate(wal.RULE_EVENTS)}
    rules.sort(key=lambda r: order.get(r.event, 99))
    return [wal.rule_to_dict(r) for r in rules]


def validate_rule_olt_ids(db, scope_key, ids):
    """Cegah IDOR: olt_ids harus subset dari OLT milik scope. Return None kalau kosong."""
    if not ids:
        return None
    import wa_alerting as wal
    olts, _, _ = wal._olt_context(db, __import__("datetime").datetime.utcnow(), scope_key)
    allowed = {o.id for o in olts}
    bad = sorted(set(ids) - allowed)
    if bad:
        raise HTTPException(403, "OLT bukan milik scope ini: " + ", ".join(map(str, bad)))
    return sorted(set(ids))

@router.put("/alert-rules/{event}")
def update_alert_rule(event: str, body: AlertRuleIn, scope: str = "global",
                     db: Session = Depends(get_db), user: User = Depends(_ADMIN)):
    _check_scope(db, user, scope)
    import wa_alerting as wal
    if event not in wal.RULE_EVENTS:
        raise HTTPException(404, "Event tidak dikenal")
    wal.seed_defaults(db, scope)
    rule = wal.get_rule(db, scope, event)

    data = {k: v for k, v in _dump(body).items() if v is not None or k in _NULLABLE}
    if data.get("severity_min") is not None and data["severity_min"] not in wal.SEVERITY_RANK:
        raise HTTPException(422, "severity_min harus info | warning | critical")
    if data.get("olt_ids") is not None:
        data["olt_ids"] = sorted(set(data["olt_ids"])) or None
    if "target_override" in data:
        data["target_override"] = (data["target_override"] or "").strip() or None

    was_on = bool(rule.enabled)
    for k, v in data.items():
        setattr(rule, k, v)
    db.commit()

    out = {"rule": wal.rule_to_dict(rule)}
    if rule.enabled and not was_on:
        try:
            out["baseline"] = wal.baseline_state(db, scope, event)
        except Exception as exc:
            db.rollback()
            out["baseline_error"] = str(exc)[:200]
    elif was_on and not rule.enabled:
        out["pending_cancelled"] = wal.cancel_pending(db, scope, event)
    return out


@router.post("/alert-rules/{event}/reset")
def reset_alert_rule(event: str, scope: str = "global",
                     db: Session = Depends(get_db), user: User = Depends(_ADMIN)):
    """Kembalikan rule ke default pabrik (enabled=False, cooldown/remind default)."""
    _check_scope(db, user, scope)
    import wa_alerting as wal
    if event not in wal.RULE_EVENTS:
        raise HTTPException(404, "Event tidak dikenal")
    wal.seed_defaults(db, scope)
    rule = wal.get_rule(db, scope, event)
    defaults = wal.DEFAULT_RULES.get(event) or {}
    rule.enabled = False
    rule.min_duration_sec = int(defaults.get("min_duration_sec", 0))
    rule.cooldown_min = int(defaults.get("cooldown_min", 30))
    rule.remind_after_min = int(defaults.get("remind_after_min", 0))
    rule.mass_threshold = int(defaults.get("mass_threshold", 10))
    rule.severity_min = defaults.get("severity_min")
    rule.olt_ids = None
    rule.target_override = None
    db.commit()
    _audit(db, user, "wa.rule.reset", "scope=" + scope, "event=" + event)
    db.commit()
    return {"rule": wal.rule_to_dict(rule), "message": "Rule dikembalikan ke default"}


@router.post("/alert-rules/{event}/test")
def test_alert_rule(event: str, scope: str = "global",
                    db: Session = Depends(get_db), user: User = Depends(_ADMIN)):
    _check_scope(db, user, scope)
    _test_rate_limit(scope, event)
    import wa_alerting as wal
    if event not in wal.TEMPLATE_EVENTS:
        raise HTTPException(404, "Event tidak dikenal")
    wal.seed_defaults(db, scope)
    text = "\U0001F9EA *[CONTOH]*\n" + wal.render_template(wal.get_template(db, scope, event), wal.sample_vars(event, db=db, scope_key=scope))
    rule = wal.get_rule(db, scope, event)
    ok, err = send_wa_message(db, scope, text, rule.target_override if rule else None)
    if not ok:
        raise HTTPException(502, err or "Gagal kirim")
    return {"ok": True, "message": text}


@router.get("/alerts-log")
def list_alerts_log(status_: Optional[str] = Query(None, alias="status"),
                    event: Optional[str] = None, olt_id: Optional[int] = None,
                    limit: int = Query(50, ge=1, le=200), offset: int = Query(0, ge=0),
                    scope: str = "global", db: Session = Depends(get_db),
                    user: User = Depends(_ADMIN)):
    _check_scope(db, user, scope)
    q = db.query(WaAlertLog).filter(WaAlertLog.scope_key == scope)
    if status_:
        q = q.filter(WaAlertLog.status == status_)
    if event:
        q = q.filter(WaAlertLog.event == event)
    if olt_id is not None:
        q = q.filter(WaAlertLog.olt_id == olt_id)
    total = q.count()
    rows = q.order_by(WaAlertLog.created_at.desc(), WaAlertLog.id.desc()).offset(offset).limit(limit).all()
    import wa_alerting as wal
    return {"total": total, "items": [wal.log_to_dict(r, with_meta=False) for r in rows]}


def _get_log_or_404(db, scope, log_id):
    row = db.query(WaAlertLog).filter(WaAlertLog.id == log_id, WaAlertLog.scope_key == scope).first()
    if not row:
        raise HTTPException(404, "Log tidak ditemukan")
    return row


@router.delete("/alerts-log/{log_id}")
def delete_alert_log(log_id: int, scope: str = "global",
                     db: Session = Depends(get_db), user: User = Depends(_ADMIN)):
    """Hapus 1 log alert WA."""
    _check_scope(db, user, scope)
    row = db.query(WaAlertLog).filter(
        WaAlertLog.id == log_id, WaAlertLog.scope_key == scope
    ).first()
    if not row:
        raise HTTPException(404, "Log tidak ditemukan")
    db.delete(row)
    db.commit()
    _audit(db, user, "wa.log.delete", "scope=" + scope, "log_id=" + str(log_id))
    db.commit()
    return {"ok": True, "deleted": 1}


class BulkDeleteIn(BaseModel):
    ids: List[int]


@router.post("/alerts-log/bulk-delete")
def bulk_delete_alert_logs(body: BulkDeleteIn, scope: str = "global",
                           db: Session = Depends(get_db), user: User = Depends(_ADMIN)):
    """Hapus banyak log alert WA sekaligus."""
    _check_scope(db, user, scope)
    if not body.ids:
        return {"ok": True, "deleted": 0}
    n = db.query(WaAlertLog).filter(
        WaAlertLog.id.in_(body.ids), WaAlertLog.scope_key == scope
    ).delete(synchronize_session=False)
    db.commit()
    _audit(db, user, "wa.log.bulk_delete", "scope=" + scope,
           "count=" + str(n) + " ids=" + str(body.ids[:10]))
    db.commit()
    return {"ok": True, "deleted": n}


@router.get("/alerts-log/{log_id}")
def get_alert_log(log_id: int, scope: str = "global",
                  db: Session = Depends(get_db), user: User = Depends(_ADMIN)):
    _check_scope(db, user, scope)
    import wa_alerting as wal
    return wal.log_to_dict(_get_log_or_404(db, scope, log_id))


@router.post("/alerts-log/{log_id}/retry")
def retry_alert_log(log_id: int, scope: str = "global",
                    db: Session = Depends(get_db), user: User = Depends(_ADMIN)):
    _check_scope(db, user, scope)
    # Cek resource dulu (404), baru cek engine state (409)
    row = _get_log_or_404(db, scope, log_id)
    import wa_alerting as wal
    ok, why = wal.engine_state(db, scope)
    if not ok:
        raise HTTPException(409, _BLOCK_MSG.get(why, "Engine tidak aktif"))
    if row.status not in ("failed", "expired"):
        raise HTTPException(409, "Hanya log failed/expired yang bisa di-retry")
    row.status = "pending"
    row.attempts = 0
    row.error = None
    row.next_retry_at = datetime.utcnow()
    db.commit()
    import wa_alerting as wal
    return wal.log_to_dict(row)


@router.get("/alerts-status")
def alerts_status(scope: str = "global", db: Session = Depends(get_db), user: User = Depends(_ADMIN)):
    _check_scope(db, user, scope)
    import wa_alerting as wal
    return wal.status_summary(db, scope, is_super=_is_super(user))

