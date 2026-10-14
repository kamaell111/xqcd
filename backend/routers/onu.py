import time
import re
from datetime import datetime
from fastapi import APIRouter, Depends, HTTPException, BackgroundTasks
from sqlalchemy.orm import Session
from database import get_db, SessionLocal
from models import OLT, ONU, User, VLAN
from schemas import ONUProvisionRequest
from auth import get_current_user, require_privilege, audit
from zxan_parser import generate_onu_config
from olt_client import OLTClient
from olt_manager import olt_locked, olt_manager
from tenancy import OwnerContext, get_owner_ctx, get_olt_or_403, require_olt_access

router = APIRouter(prefix="/api/v1/onu", tags=["onu"])


def _make_client(olt: OLT) -> OLTClient:
    return OLTClient(
        host=olt.ip_address,
        username=olt.username,
        password=olt.password,
        enable_password=olt.enable_password,
        port=olt.port,
        protocol=olt.protocol,
    )


@router.post("/provision")
def provision_onu_start(req: ONUProvisionRequest,
                        background_tasks: BackgroundTasks,
                        db: Session = Depends(get_db),
                        ctx: OwnerContext = Depends(get_owner_ctx),
                        user: User = Depends(require_privilege(10))):
    """Bikin job provisioning ONU — return instan (<1 detik).
    Proses aktual jalan di background. Poll GET /api/v1/jobs/{job_id}."""
    olt = get_olt_or_403(db, req.olt_id, ctx)  # 403 kalau bukan milik user

    # Cek SN duplikat
    if db.query(ONU).filter(ONU.olt_id == req.olt_id,
                            ONU.serial_number == req.serial_number).first():
        raise HTTPException(400, "ONU dengan SN tersebut sudah terdaftar")

    # Cek (pon_port, onu_id) duplikat — tidak boleh ada 2 ONU di slot yang sama
    if db.query(ONU).filter(ONU.olt_id == req.olt_id,
                            ONU.pon_port == req.pon_port,
                            ONU.onu_id == req.onu_id).first():
        raise HTTPException(400, f"Slot {req.pon_port}:{req.onu_id} sudah terpakai ONU lain")

    # Idempotency ATOMIK: pakai composite key (SN + slot) untuk cegah race
    resource_key = f"onu:{req.serial_number}:{req.pon_port}:{req.onu_id}"
    job_id, is_new = olt_manager.create_job_atomic(
        olt_id=str(req.olt_id),
        job_type="provision_onu",
        resource_key=resource_key,
    )

    if not is_new:
        return {
            "job_id": job_id,
            "status": "queued",
            "message": "Job sudah berjalan untuk ONU ini",
            "duplicate": True,
        }

    # Kick background task
    req_dict = req.model_dump() if hasattr(req, "model_dump") else req.dict()
    background_tasks.add_task(
        _run_provision_job,
        job_id=job_id,
        req_dict=req_dict,
        username=user.username,
    )

    return {
        "job_id": job_id,
        "status": "queued",
        "message": "Job provisioning dimulai. Poll GET /api/v1/jobs/{job_id}.",
    }


# =================== INVENTORY ===================
@router.get("/inventory")
def get_inventory(olt_id: int,
                  db: Session = Depends(get_db),
                  ctx: OwnerContext = Depends(get_owner_ctx),
                  user: User = Depends(get_current_user)):
    """Daftar untuk dropdown dinamis di wizard."""
    olt = get_olt_or_403(db, olt_id, ctx)

    vlans = db.query(VLAN).filter(VLAN.olt_id == olt_id).order_by(VLAN.vlan_id).all()
    vlan_list = [{"id": v.vlan_id, "name": v.name or f"VLAN{v.vlan_id}"} for v in vlans]

    onus = db.query(ONU).filter(ONU.olt_id == olt_id).all()
    tcont_set, onu_type_set = set(), set()
    for o in onus:
        if o.tcont: tcont_set.add(o.tcont)
        if o.type: onu_type_set.add(o.type)

    return {
        "vlans": vlan_list,
        "tcont_profiles": sorted(tcont_set) or ["1G", "100M", "50M", "20M"],
        "traffic_profiles": sorted(tcont_set) or ["1G", "100M", "50M", "20M"],
        "onu_types": sorted(onu_type_set) or ["F609", "F601", "F660", "F670L"],
        "used_service_ports": sorted([o.service_port for o in onus if o.service_port]),
        "used_gemports_global": sorted(list({o.gemport for o in onus if o.gemport})),
    }


@router.get("/uncfg")
def get_uncfg(olt_id: int,
              db: Session = Depends(get_db),
              ctx: OwnerContext = Depends(get_owner_ctx),
              user: User = Depends(get_current_user)):
    """Daftar ONU belum terdaftar dari OLT (untuk SN picker).

    CATATAN: sebelum Phase B2b, endpoint ini pakai db.query(OLT).first()
    yang salah untuk multi-OLT. Sekarang wajib ?olt_id=.
    """
    olt = get_olt_or_403(db, olt_id, ctx)
    try:
        client = _make_client(olt)
        conn = client._connect()
        try:
            out = conn.send_command_timing("show gpon onu uncfg", read_timeout=20, last_read=0.5)
        finally:
            conn.disconnect()
    except Exception as e:
        raise HTTPException(500, f"Gagal ambil uncfg: {e}")

    items = []
    for line in out.splitlines():
        s = line.strip()
        if not s or "---" in s or s.lower().startswith("onuindex"):
            continue
        parts = s.split()
        if len(parts) < 3:
            continue
        idx, sn, state = parts[0], parts[1], parts[2]

        # ⭐ VALIDASI: idx harus format gpon-onu_X/Y/Z:N
        if not re.match(r"^gpon-onu_\d+/\d+/\d+:\d+$", idx):
            continue

        # ⭐ VALIDASI: SN harus 12-16 karakter alfanumerik uppercase, mix huruf+angka
        # Format ZTE/Huawei: 4 char vendor + 8 hex (mis. YYKC375F0511)
        if not re.match(r"^[A-Z0-9]{8,16}$", sn.upper()):
            continue
        if not any(c.isalpha() for c in sn) or not any(c.isdigit() for c in sn):
            continue

        prefix = sn[:4].upper() if len(sn) >= 4 else ""
        vendor = {"ZTEG":"ZTE","YYKC":"ZTE","HWTC":"Huawei","FHTT":"FiberHome"}.get(prefix, "Unknown")
        m = re.match(r"gpon-onu_(\d+/\d+/\d+):(\d+)", idx)
        items.append({
            "onu_index": idx,
            "serial_number": sn,
            "state": state,
            "vendor": vendor,
            "pon_port": m.group(1) if m else None,
            "onu_id": int(m.group(2)) if m else None,
        })
    return {"total": len(items), "items": items}


# =================== PREFLIGHT + AUTO-ALLOCATE ===================
@router.post("/preflight")
def preflight_check(req: ONUProvisionRequest,
                    db: Session = Depends(get_db),
                    ctx: OwnerContext = Depends(get_owner_ctx),
                    user: User = Depends(get_current_user)):
    """Validasi sebelum provisioning + saran ID kosong."""
    errors, warnings = [], []
    suggestions = {}

    olt = get_olt_or_403(db, req.olt_id, ctx)

    # SN duplikat
    sn_ex = db.query(ONU).filter(ONU.olt_id == req.olt_id,
                                  ONU.serial_number == req.serial_number).first()
    if sn_ex:
        errors.append({"field": "serial_number",
                       "message": f"SN {req.serial_number} sudah dipakai di {sn_ex.pon_port}:{sn_ex.onu_id}"})

    # Slot duplikat
    slot_ex = db.query(ONU).filter(ONU.olt_id == req.olt_id,
                                    ONU.pon_port == req.pon_port,
                                    ONU.onu_id == req.onu_id).first()
    if slot_ex:
        errors.append({"field": "onu_id",
                       "message": f"Slot {req.pon_port}:{req.onu_id} sudah dipakai {slot_ex.serial_number or '-'}"})

    # VLAN exist
    for fname, vlan_id in [("user_vlan", req.user_vlan), ("vlan", req.vlan)]:
        if not db.query(VLAN).filter(VLAN.olt_id == req.olt_id, VLAN.vlan_id == vlan_id).first():
            warnings.append({"field": fname,
                             "message": f"VLAN {vlan_id} belum ada di database aplikasi"})

    # GEMPORT TIDAK dicek bentrok — di ZTE C320 GEMPORT itu per-ONU,
    # jadi ONU berbeda boleh punya GEMPORT ID yang sama.

    # Service-port bentrok
    sp_ex = db.query(ONU).filter(ONU.olt_id == req.olt_id,
                                  ONU.service_port == req.service_port).first()
    if sp_ex:
        errors.append({"field": "service_port",
                       "message": f"Service-port {req.service_port} sudah dipakai {sp_ex.pon_port}:{sp_ex.onu_id}"})

    # Suggestions — cari kosong
    existing_sp = {o.service_port for o in db.query(ONU).filter(ONU.olt_id == req.olt_id).all() if o.service_port}
    for i in range(1, 500):
        if i not in existing_sp:
            suggestions["service_port"] = i
            break

    existing_gem = {o.gemport for o in db.query(ONU).filter(
        ONU.olt_id == req.olt_id, ONU.pon_port == req.pon_port).all() if o.gemport}
    for i in range(1, 32):
        if i not in existing_gem:
            suggestions["gemport_id"] = i
            break

    existing_onu_id = {o.onu_id for o in db.query(ONU).filter(
        ONU.olt_id == req.olt_id, ONU.pon_port == req.pon_port).all()}
    for i in range(1, 128):
        if i not in existing_onu_id:
            suggestions["onu_id"] = i
            break

    return {
        "valid": len(errors) == 0,
        "errors": errors,
        "warnings": warnings,
        "suggestions": suggestions,
    }


LOCK_ACQUIRE_TIMEOUT = 120   # detik — maks tunggu lock OLT


def _acquire_or_fail(lock, timeout: int = LOCK_ACQUIRE_TIMEOUT):
    """Acquire lock OLT dengan timeout. Raise kalau gagal."""
    if not lock.acquire(timeout=timeout):
        raise RuntimeError(
            f"OLT sibuk — gagal acquire lock dalam {timeout}s. "
            f"Coba lagi nanti."
        )


def _run_provision_job(job_id: str, req_dict: dict, username: str):
    """Background worker: jalankan provisioning lengkap + update job store."""
    from schemas import ONUProvisionRequest
    req = ONUProvisionRequest(**req_dict)

    db = SessionLocal()
    try:
        olt = db.query(OLT).get(req.olt_id)
        if not olt:
            olt_manager.finish_job(job_id, "failed", error="OLT tidak ditemukan")
            return

        client = _make_client(olt)

        # Milestone 1: validasi
        olt_manager.update_job(job_id, status="running", progress=5,
                               message="Validasi config")
        commands = generate_onu_config(req)
        val = client.validate_commands(commands)
        if not val["valid"]:
            olt_manager.finish_job(job_id, "failed",
                                   error=f"Validasi gagal: {val['errors']}")
            return

        # Milestone 2: kirim command (PEGANG LOCK)
        olt_manager.update_job(job_id, progress=10,
                               message="Kirim config ke OLT")
        lock = olt_manager.get_thread_lock(str(req.olt_id))
        _acquire_or_fail(lock)
        try:
            try:
                client.run_config(commands, save=True)   # additive → auto-write
            except Exception as e:
                olt_manager.finish_job(job_id, "failed",
                                       error=f"Gagal kirim config: {e}")
                audit(db, username, "provision_onu", req.serial_number, str(e), "failed")
                return
        finally:
            lock.release()   # ⭐ LEPAS LOCK setelah kirim

        # Milestone 3: polling ONU ready (LEPAS LOCK saat sleep)
        olt_manager.update_job(job_id, progress=30,
                               message="Menunggu ONU ready")
        onu_index = f"{req.pon_port}:{req.onu_id}"
        onu_terdaftar = False

        for _ in range(8):   # max 8 × 3s = 24s
            time.sleep(3)
            _acquire_or_fail(lock)
            try:
                conn = client._connect()
                try:
                    state_out = conn.send_command_timing(
                        f"show gpon onu state gpon-olt_{req.pon_port}",
                        read_timeout=20,
                    )
                    if onu_index in state_out:
                        onu_terdaftar = True
                        break
                finally:
                    conn.disconnect()
            except Exception as e:
                print(f"[PROVISION] ONU check error: {e}")
            finally:
                lock.release()

        if not onu_terdaftar:
            # Rollback
            olt_manager.update_job(job_id, progress=40, message="Rollback ONU setengah jadi")
            _acquire_or_fail(lock)
            try:
                try:
                    conn = client._connect()
                    try:
                        conn.send_command_timing("configure terminal", read_timeout=5)
                        conn.send_command_timing(
                            f"interface gpon-onu_{req.pon_port}:{req.onu_id}",
                            read_timeout=5)
                        conn.send_command_timing(
                            f"no service-port {req.service_port}", read_timeout=10)
                        conn.send_command_timing("exit", read_timeout=5)
                        conn.send_command_timing(
                            f"interface gpon-olt_{req.pon_port}", read_timeout=5)
                        conn.send_command_timing(
                            f"no onu {req.onu_id}", read_timeout=10)
                        conn.send_command_timing("exit", read_timeout=5)
                        conn.send_command_timing("end", read_timeout=5)
                        conn.send_command_timing("write", read_timeout=15)
                    finally:
                        conn.disconnect()
                except Exception as e:
                    print(f"[PROVISION] Rollback error: {e}")
            finally:
                lock.release()

            olt_manager.finish_job(job_id, "failed",
                                   error=f"ONU {onu_index} tidak muncul di OLT setelah timeout")
            audit(db, username, "provision_onu", req.serial_number,
                  "ONU tidak terdaftar setelah timeout", "failed")
            return

        # ⭐ Cek mode: routed (ZTE) atau bridge (Huawei/dll)
        from vendor_detect import detect_vendor, get_capability
        detected_vendor_early = detect_vendor(req.serial_number)
        is_bridge_mode = not get_capability(detected_vendor_early)["supports_routed"]

        pppoe_status = "bridge" if is_bridge_mode else "unknown"
        pppoe_dur = 0
        pppoe_user = req.pppoe_user
        pppoe_nat = False

        # Milestone 4: polling PPPoE (SKIP kalau bridge mode)
        if is_bridge_mode:
            olt_manager.update_job(job_id, progress=60,
                                   message=f"Bridge mode ({detected_vendor_early}) — skip PPPoE")
            print(f"[PROVISION] {detected_vendor_early} → bridge mode, skip PPPoE polling")
        else:
            olt_manager.update_job(job_id, progress=60,
                                   message="Menunggu modem dial PPPoE")
            for _ in range(10):   # max 10 × 3s = 30s
                time.sleep(3)
                _acquire_or_fail(lock)
                try:
                    conn = client._connect()
                    try:
                        pppoe_out = conn.send_command_timing(
                            f"show gpon remote-onu pppoe gpon-onu_{onu_index}",
                            read_timeout=20,
                        )
                        m = re.search(r"Status:\s+(\S+)", pppoe_out)
                        if m:
                            pppoe_status = m.group(1).lower()
                        m = re.search(r"Online duration:\s+(\d+)", pppoe_out)
                        if m:
                            pppoe_dur = int(m.group(1))
                        m = re.search(r"Username:\s+(\S+)", pppoe_out)
                        if m:
                            pppoe_user = m.group(1)
                        m = re.search(r"NAT:\s+(\S+)", pppoe_out)
                        if m and m.group(1) == "enable":
                            pppoe_nat = True
                        if pppoe_status == "connected":
                            break
                    finally:
                        conn.disconnect()
                except Exception as e:
                    print(f"[PROVISION] PPPoE check error: {e}")
                finally:
                    lock.release()

        # ⭐ Setelah loop selesai: cek apakah PPPoE berhasil
        warning_msg = None

        # Skip warning check kalau bridge mode (normal)
        if is_bridge_mode:
            pass
        elif req.pppoe_user and pppoe_user and pppoe_user != req.pppoe_user:
            warning_msg = (
                f"⚠️ Mismatch! Kita kirim user='{req.pppoe_user}', "
                f"tapi modem pakai user='{pppoe_user}'. "
                f"Kemungkinan command tidak nempel."
            )
            print(f"[PROVISION] {warning_msg}")
            pppoe_status = "config_mismatch"
        elif pppoe_status == "connected":
            # Match + connected → sempurna
            pass
        elif pppoe_status in ("connecting", "idle", ""):
            # Masih dial setelah 30s → tandai dial_failed
            pppoe_status = "dial_failed"
            warning_msg = (
                f"PPPoE belum connect setelah 30 detik. "
                f"Cek kredensial (user={pppoe_user or '-'}) di Mikrotik atau fisik modem."
            )
            print(f"[PROVISION] ONU {onu_index} — {warning_msg}")
        elif pppoe_status == "disconnected":
            warning_msg = f"PPPoE disconnected — cek modem/kredensial"
        else:
            warning_msg = f"PPPoE status: {pppoe_status}"

        # Milestone 5: optical + distance
        olt_manager.update_job(job_id, progress=85, message="Ambil optical info")
        optical_rx = None
        optical_tx = None
        distance = None

        _acquire_or_fail(lock)
        try:
            conn = client._connect()
            try:
                att_out = conn.send_command_timing(
                    f"show pon power attenuation gpon-onu_{onu_index}",
                    read_timeout=20,
                )
                for line in att_out.splitlines():
                    if line.strip().startswith("down"):
                        m2 = re.search(r"Tx\s*:(-?[\d.]+)\(dbm\)", line)
                        if m2: optical_tx = float(m2.group(1))
                        m3 = re.search(r"Rx\s*:(-?[\d.]+)\(dbm\)", line)
                        if m3: optical_rx = float(m3.group(1))
                det_out = conn.send_command_timing(
                    f"show gpon onu detail-info gpon-onu_{onu_index}",
                    read_timeout=30,
                )
                m = re.search(r"ONU Distance:\s+(\d+)m", det_out)
                if m:
                    distance = int(m.group(1))
            finally:
                conn.disconnect()
        except Exception as e:
            print(f"[PROVISION] Optical error: {e}")
        finally:
            lock.release()

        # Milestone 6: simpan DB
        olt_manager.update_job(job_id, progress=95, message="Simpan ke database")

        # ⭐ DEFENSE-IN-DEPTH: cek lagi sebelum insert (cegah race di background)
        existing = db.query(ONU).filter(
            ONU.olt_id == req.olt_id,
            ONU.pon_port == req.pon_port,
            ONU.onu_id == req.onu_id,
        ).first()
        if existing:
            print(f"[PROVISION] Skip insert: slot {req.pon_port}:{req.onu_id} sudah ada di DB (id={existing.id})")
            olt_manager.finish_job(job_id, "failed",
                                   error=f"Slot {req.pon_port}:{req.onu_id} sudah terpakai")
            audit(db, username, "provision_onu", req.serial_number,
                  "Duplicate slot — skip insert", "failed")
            return

        # ⭐ Resolve vendor untuk disimpan di DB
        from zxan_parser import get_vendor_info
        vinfo = get_vendor_info(req)

        onu = ONU(
            olt_id=req.olt_id, pon_port=req.pon_port, onu_id=req.onu_id,
            interface_name=f"gpon-onu_{onu_index}",
            serial_number=req.serial_number, name=req.name, type=req.onu_type,
            status="online", tcont=req.tcont_profile, gemport=req.gemport_id,
            service_port=req.service_port, user_vlan=req.user_vlan, vlan=req.vlan,
            pppoe_user=pppoe_user, pppoe_nat=pppoe_nat,
            pppoe_status=pppoe_status,
            pppoe_online_duration=pppoe_dur,
            internet_checked_at=datetime.utcnow(),
            optical_rx=optical_rx, optical_tx=optical_tx, distance=distance,
            # ⭐ Multi-vendor fields
            vendor=vinfo["vendor"],
            provisioning_mode=vinfo["provisioning_mode"],
            vendor_source=vinfo["vendor_source"],
        )
        db.add(onu)
        db.commit()
        db.refresh(onu)
        audit(db, username, "provision_onu", req.serial_number)

        # ⚡ Cache invalidate — ONU baru/provision berubah config
        try:
            from routers.sync import invalidate_olt_cache
            invalidate_olt_cache(req.olt_id)
        except Exception:
            pass

        olt_manager.finish_job(job_id, "success", result={
            "onu_id": onu.id,
            "onu_index": onu_index,
            "internet_status": pppoe_status,
            "internet_online_duration": pppoe_dur,
            "optical_rx": optical_rx,
            "optical_tx": optical_tx,
            "distance": distance,
            "warning": warning_msg,
            # ⭐ Multi-vendor info
            "vendor": vinfo["vendor"],
            "provisioning_mode": vinfo["provisioning_mode"],
            "vendor_source": vinfo["vendor_source"],
        })

    except Exception as e:
        print(f"[PROVISION] Fatal: {e}")
        olt_manager.finish_job(job_id, "failed", error=f"Error tidak terduga: {e}")
    finally:
        db.close()


@router.post("/{olt_id}/{onu_id}/reboot")
@olt_locked
def reboot_onu(olt_id: int, onu_id: int, db: Session = Depends(get_db),
               olt: OLT = Depends(require_olt_access),
               user: User = Depends(require_privilege(10))):
    onu = db.query(ONU).filter(ONU.olt_id == olt.id, ONU.onu_id == onu_id).first()
    if not onu:
        raise HTTPException(404, "ONU tidak ditemukan")
    client = _make_client(olt)
    try:
        output = client.run_commands([
            f"pon-onu-mng {onu.interface_name}", "reboot", "exit",
        ])
        audit(db, user.username, "reboot_onu", onu.serial_number)
        return {"ok": True, "output": output}
    except Exception as e:
        raise HTTPException(500, str(e))


def _do_delete_onu(olt_id: int, onu_id: int, db: Session, username: str,
                    job_id: str = None) -> dict:
    """Worker: hapus ONU dari OLT + DB. Dipanggil dari async job."""
    def _p(msg, prog=None):
        if job_id:
            upd = {"message": msg}
            if prog is not None: upd["progress"] = prog
            olt_manager.update_job(job_id, **upd)
        print(f"[DELETE-ONU] {msg}")

    olt = db.query(OLT).get(olt_id)
    onu = db.query(ONU).filter(ONU.olt_id == olt_id, ONU.onu_id == onu_id).first()
    if not onu:
        raise RuntimeError("ONU tidak ditemukan")
    client = _make_client(olt)

    delete_log = []
    _p("Cek ONU di OLT...", 10)

    delete_log = []

    # 1. Cek ONU masih ada di OLT?
    onu_exists_in_olt = False
    real_service_port = onu.service_port   # dari DB
    try:
        conn = client._connect()
        try:
            out = conn.send_command_timing(
                f"show gpon onu state gpon-olt_{onu.pon_port}",
                read_timeout=15,
            )
            delete_log.append(f"[STATE] {out[:200]}")
            target = f"{onu.pon_port}:{onu.onu_id}"
            if target in out:
                onu_exists_in_olt = True
                delete_log.append(f"[DELETE] ONU {target} ada di OLT")

            # Cari service-port ONU ini dari running-config
            rc = conn.send_command_timing("show running-config", read_timeout=60)
            delete_log.append(f"[RC] running-config loaded ({len(rc)} chars)")
            # Cari blok interface gpon-onu_X:Y → service-port N
            pattern = rf"interface gpon-onu_{re.escape(onu.pon_port)}:{onu.onu_id}\b(.*?)(?=^!|\Z)"
            m = re.search(pattern, rc, re.DOTALL | re.MULTILINE)
            if m:
                sp_match = re.search(r"service-port\s+(\d+)", m.group(1))
                if sp_match:
                    real_service_port = int(sp_match.group(1))
                    delete_log.append(f"[DELETE] SP real dari OLT: {real_service_port}")
        finally:
            conn.disconnect()
    except Exception as e:
        delete_log.append(f"[CEK ERROR] {e}")

    # 2. Hapus di OLT kalau masih ada
    olt_success = False
    if onu_exists_in_olt:
        if not real_service_port:
            delete_log.append("[WARN] service_port tidak diketahui — skip no service-port")
            cmds = [
                "configure terminal",
                f"interface gpon-olt_{onu.pon_port}",
                f"no onu {onu.onu_id}",
                "exit",
                "end",
                # tidak ada 'write' — two-phase commit, user commit manual
            ]
        else:
            cmds = [
                "configure terminal",
                f"interface gpon-onu_{onu.pon_port}:{onu.onu_id}",
                f"no service-port {real_service_port}",
                "exit",
                f"interface gpon-olt_{onu.pon_port}",
                f"no onu {onu.onu_id}",
                "exit",
                "end",
                # tidak ada 'write' — two-phase commit
            ]
        try:
            out = client.run_config(cmds, save=False)   # destructive → two-phase
            delete_log.append(f"[RUN_CONFIG] {out[:500]}")
            olt_manager.mark_pending(str(olt_id), f"delete ONU {onu.pon_port}:{onu.onu_id}")
        except Exception as e:
            delete_log.append(f"[RUN_CONFIG ERROR] {e}")

        # 3. VERIFIKASI: pastikan ONU benar-benar hilang dari OLT
        time.sleep(5)
        try:
            conn = client._connect()
            try:
                verify_out = conn.send_command_timing(
                    f"show gpon onu state gpon-olt_{onu.pon_port}",
                    read_timeout=15,
                )
                delete_log.append(f"[VERIFY] {verify_out[:300]}")
                if target not in verify_out:
                    olt_success = True
                    delete_log.append("[VERIFY] ✅ ONU hilang dari OLT")
                else:
                    delete_log.append("[VERIFY] ❌ ONU MASIH ADA di OLT")
            finally:
                conn.disconnect()
        except Exception as e:
            delete_log.append(f"[VERIFY ERROR] {e}")

        # 4. Kalau OLT masih punya ONU → JANGAN hapus DB
        if not olt_success:
            audit(db, username, "delete_onu", onu.serial_number or "",
                  "OLT masih punya ONU setelah delete", "failed")
            raise RuntimeError("Gagal hapus ONU di OLT — ONU masih ada. DB tidak diubah.")

    # 5. Baru hapus DB
    db.delete(onu)
    db.commit()
    audit(db, username, "delete_onu", onu.serial_number or "")
    _p("Selesai", 100)
    return {"ok": True, "onu_id": onu_id, "olt_had_onu": onu_exists_in_olt}


@router.delete("/{olt_id}/{onu_id}")
def delete_onu_endpoint(olt_id: int, onu_id: int,
                        background_tasks: BackgroundTasks,
                        db: Session = Depends(get_db),
                        olt: OLT = Depends(require_olt_access),
                        user: User = Depends(require_privilege(15))):
    """Hapus ONU — async job. Return job_id untuk polling."""
    onu = db.query(ONU).filter(ONU.olt_id == olt.id, ONU.onu_id == onu_id).first()
    if not onu:
        raise HTTPException(404, "ONU tidak ditemukan")

    resource_key = f"delete_onu:{onu_id}"
    job_id, is_new = olt_manager.create_job_atomic(
        olt_id=str(olt_id),
        job_type="delete_onu",
        resource_key=resource_key,
    )
    if not is_new:
        return {"job_id": job_id, "status": "queued", "duplicate": True}

    background_tasks.add_task(
        _run_delete_job,
        job_id=job_id,
        olt_id=olt_id,
        onu_id=onu_id,
        username=user.username,
    )
    return {"job_id": job_id, "status": "queued", "message": "Job hapus ONU dimulai"}


def _run_delete_job(job_id: str, olt_id: int, onu_id: int, username: str):
    """Background runner untuk delete ONU."""
    from database import SessionLocal
    olt_manager.update_job(job_id, status="running", progress=5, message="Mulai...")
    db = SessionLocal()
    try:
        result = _do_delete_onu(olt_id, onu_id, db, username, job_id=job_id)
        olt_manager.finish_job(job_id, "success", result=result)
        # ⚡ Cache invalidate — config berubah
        try:
            from routers.sync import invalidate_olt_cache
            invalidate_olt_cache(olt_id)
        except Exception:
            pass
    except Exception as e:
        import traceback
        traceback.print_exc()
        olt_manager.finish_job(job_id, "failed", error=str(e)[:300])
    finally:
        db.close()


# =================== MARK BRIDGE CONFIGURED ===================
@router.post("/{olt_id}/{onu_id}/mark-connected")
@olt_locked
def mark_connected(olt_id: int, onu_id: int, db: Session = Depends(get_db),
                   olt: OLT = Depends(require_olt_access),
                   user: User = Depends(require_privilege(10))):
    """Tandai bridge mode = configured (user sudah set PPPoE di GUI modem).
    Mengubah status Internet jadi CONNECTED."""
    onu = db.query(ONU).filter(ONU.olt_id == olt.id, ONU.onu_id == onu_id).first()
    if not onu:
        raise HTTPException(404, "ONU tidak ditemukan")

    onu.bridge_configured = True
    onu.pppoe_status = "connected"
    onu.updated_at = datetime.utcnow()
    db.commit()
    db.refresh(onu)
    audit(db, user.username, "mark_connected", onu.serial_number or str(onu_id))
    return {"ok": True, "onu_id": onu_id, "bridge_configured": True}


@router.post("/{olt_id}/{onu_id}/mark-disconnected")
@olt_locked
def mark_disconnected(olt_id: int, onu_id: int, db: Session = Depends(get_db),
                      olt: OLT = Depends(require_olt_access),
                      user: User = Depends(require_privilege(10))):
    """Tandai bridge mode = belum dikonfigurasi (user belum set PPPoE di GUI).
    Mengubah status Internet jadi BELUM SETUP."""
    onu = db.query(ONU).filter(ONU.olt_id == olt.id, ONU.onu_id == onu_id).first()
    if not onu:
        raise HTTPException(404, "ONU tidak ditemukan")

    onu.bridge_configured = False
    onu.pppoe_status = "bridge"
    onu.updated_at = datetime.utcnow()
    db.commit()
    db.refresh(onu)
    audit(db, user.username, "mark_disconnected", onu.serial_number or str(onu_id))
    return {"ok": True, "onu_id": onu_id, "bridge_configured": False}
