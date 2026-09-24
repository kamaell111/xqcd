"""Alert helper — auto-generate, anti-duplikat, auto-resolve."""
from datetime import datetime
from sqlalchemy.orm import Session
from models import Alert, OLT
from config import settings
from recovery_tracker import add_recovery


def process_alert(db: Session, olt_id: int, category: str,
                  source: str, severity: str,
                  title: str, message: str,
                  condition_active: bool) -> dict:
    """
    Kelola alert untuk 1 kondisi.

    - Kalau condition_active=True:
        → Kalau alert existing (belum resolved) ada: update timestamp saja (anti-spam)
        → Kalau belum ada: buat alert baru
    - Kalau condition_active=False:
        → Kalau alert existing ada: resolve otomatis
    """
    existing = db.query(Alert).filter(
        Alert.olt_id == olt_id,
        Alert.category == category,
        Alert.source == source,
        Alert.resolved == False,
    ).first()

    if condition_active:
        if existing:
            # Update pesan kalau berubah (biar info terbaru)
            existing.message = message
            existing.severity = severity
            existing.title = title
            existing.created_at = datetime.utcnow()  # geser biar di atas
            return {"action": "updated", "alert_id": existing.id}
        else:
            alert = Alert(
                olt_id=olt_id, severity=severity, category=category,
                title=title, message=message, source=source,
                resolved=False, acknowledged=False,
            )
            db.add(alert)
            db.flush()
            return {"action": "created", "alert_id": alert.id}
    else:
        if existing and settings.ALERT_AUTO_RESOLVE:
            existing.resolved = True
            existing.resolved_at = datetime.utcnow()
            # ⭐ Catat recovery untuk notifikasi ke user (toast)
            try:
                add_recovery(olt_id, source, f"{existing.title} — sudah pulih")
            except Exception:
                pass

            # ⭐ Buat alert INFO sebagai history (muncul di menu Alerts)
            try:
                info_alert = Alert(
                    olt_id=olt_id,
                    severity="info",
                    category=f"{category}_recovered",
                    title=f"✅ {existing.title} — sudah pulih",
                    message=f"Recovery: {existing.title} kembali normal.",
                    source=source,
                    resolved=False,
                    acknowledged=False,
                )
                db.add(info_alert)
                db.flush()
            except Exception as e:
                print(f"[ALERT] Gagal buat info alert: {e}")

            return {"action": "resolved", "alert_id": existing.id}
        return {"action": "none"}


def check_olt_resource_alerts(db: Session, olt: OLT) -> list:
    """Cek CPU/memory OLT. Dipanggil dari scheduler & sync."""
    results = []

    # CPU
    if olt.cpu_usage is not None:
        active = olt.cpu_usage > settings.ALERT_CPU_WARNING
        r = process_alert(
            db, olt_id=olt.id, category="cpu",
            source=f"olt:{olt.ip_address}",
            severity="warning",
            title=f"CPU OLT {olt.hostname} tinggi ({olt.cpu_usage:.1f}%)",
            message=f"CPU {olt.cpu_usage:.1f}% > {settings.ALERT_CPU_WARNING}%",
            condition_active=active,
        )
        results.append(("cpu", r))

    # Memory
    if olt.memory_usage is not None:
        active = olt.memory_usage > settings.ALERT_MEM_WARNING
        r = process_alert(
            db, olt_id=olt.id, category="memory",
            source=f"olt:{olt.ip_address}",
            severity="warning",
            title=f"Memory OLT {olt.hostname} tinggi ({olt.memory_usage:.1f}%)",
            message=f"Memory {olt.memory_usage:.1f}% > {settings.ALERT_MEM_WARNING}%",
            condition_active=active,
        )
        results.append(("memory", r))

    return results


def check_onu_alerts(db: Session, olt_id: int, onu) -> list:
    """Cek alert per-ONU: offline, pppoe down, optical low."""
    results = []
    onu_label = onu.serial_number or onu.interface_name or f"onu-{onu.onu_id}"
    source = f"onu:{onu_label}"

    status_lower = (onu.status or "").lower()

    # 0. DyingGasp — kemungkinan pelanggan cabut adaptor. Suppress by default.
    if status_lower == "dying_gasp":
        if getattr(settings, "ALERT_DYING_GASP", False):
            r = process_alert(
                db, olt_id=olt_id, category="onu_dying_gasp",
                source=source, severity="info",
                title=f"ONU {onu_label} mati daya",
                message="Sinyal dying-gasp — kemungkinan pelanggan cabut adaptor / listrik padam.",
                condition_active=True,
            )
            results.append(("onu_dying_gasp", r))
        return results

    # 1. ONU offline / LOS / CONFIGURING — bedakan pesan
    is_down = onu.status not in ("online", "up", "working")

    if status_lower == "los":
        alert_title = f"ONU {onu_label} LOS"
        alert_msg = "Fiber optik terputus / tidak ada sinyal. Cek kabel fiber & konektor."
        alert_sev = "critical"
    elif status_lower == "offline":
        # Cek apakah baru saja dying_gasp (dalam 10 menit) → kemungkinan cabut adaptor
        recent_dying = False
        try:
            if onu.last_dying_gasp:
                recent_dying = (datetime.utcnow() - onu.last_dying_gasp).total_seconds() < 600
        except Exception:
            pass
        if recent_dying:
            alert_title = f"ONU {onu_label} offline — kemungkinan cabut adaptor"
            alert_msg = "ONU baru saja mengirim dying-gasp. Kemungkinan pelanggan cabut adaptor / listrik padam."
            alert_sev = "info"
        else:
            alert_title = f"ONU {onu_label} offline"
            alert_msg = "Modem mati / tidak terhubung ke OLT. Cek power & kabel LAN modem."
            alert_sev = "critical"
    elif status_lower == "configuring":
        alert_title = f"ONU {onu_label} configuring"
        alert_msg = "ONU sedang proses registrasi ke OLT."
        alert_sev = "warning"
    elif is_down:
        alert_title = f"ONU {onu_label} {onu.status}"
        alert_msg = f"Status ONU: {onu.status}. Cek koneksi fisik / optik."
        alert_sev = "critical"
    else:
        # online — alert akan auto-resolve
        alert_title = f"ONU {onu_label} online"
        alert_msg = "ONU kembali online."
        alert_sev = "info"

    r = process_alert(
        db, olt_id=olt_id, category="onu_offline",
        source=source, severity=alert_sev,
        title=alert_title,
        message=alert_msg,
        condition_active=is_down,
    )
    results.append(("onu_offline", r))

    # 2. PPPoE down (hanya untuk ONU yang punya config PPPoE)
    if onu.pppoe_user:
        pppoe_down = (onu.pppoe_status == "disconnected")
        r = process_alert(
            db, olt_id=olt_id, category="pppoe_down",
            source=source, severity="warning",
            title=f"PPPoE {onu_label} terputus",
            message=f"User {onu.pppoe_user} · status: {onu.pppoe_status}",
            condition_active=pppoe_down,
        )
        results.append(("pppoe_down", r))

    # 3. Optical RX low
    if onu.optical_rx is not None:
        if onu.optical_rx < settings.ALERT_OPTICAL_RX_CRITICAL:
            sev = "critical"
            active = True
        elif onu.optical_rx < settings.ALERT_OPTICAL_RX_WARNING:
            sev = "warning"
            active = True
        else:
            sev = "info"
            active = False
        r = process_alert(
            db, olt_id=olt_id, category="optical_low",
            source=source, severity=sev,
            title=f"Optical RX {onu_label} lemah ({onu.optical_rx:.2f} dBm)",
            message=f"RX {onu.optical_rx:.2f} dBm < {settings.ALERT_OPTICAL_RX_WARNING} dBm",
            condition_active=active,
        )
        results.append(("optical_low", r))

    return results
