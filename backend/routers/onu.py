import time
import re
from datetime import datetime
from fastapi import APIRouter, Depends, HTTPException, BackgroundTasks
from sqlalchemy.orm import Session
from database import get_db, SessionLocal
from models import OLT, ONU, User
from schemas import ONUProvisionRequest
from auth import get_current_user, require_privilege, audit
from zxan_parser import generate_onu_config
from olt_client import OLTClient
from olt_manager import olt_locked, olt_manager

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
                        user: User = Depends(require_privilege(10))):
    """Bikin job provisioning ONU — return instan (<1 detik).
    Proses aktual jalan di background. Poll GET /api/v1/jobs/{job_id}."""
    olt = db.query(OLT).get(req.olt_id)
    if not olt:
        raise HTTPException(404, "OLT tidak ditemukan")

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
        lock.acquire()
        try:
            try:
                client.run_config(commands)
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
            lock.acquire()
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
            lock.acquire()
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

        # Milestone 4: polling PPPoE (LEPAS LOCK saat sleep)
        olt_manager.update_job(job_id, progress=60,
                               message="Menunggu modem dial PPPoE")
        pppoe_status = "unknown"
        pppoe_dur = 0
        pppoe_user = req.pppoe_user
        pppoe_nat = False

        for _ in range(10):   # max 10 × 3s = 30s
            time.sleep(3)
            lock.acquire()
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
        if pppoe_status == "connected":
            pass   # OK, tidak ada warning
        elif pppoe_status in ("connecting", "idle", ""):
            # Masih dial setelah 30s → tandai dial_failed
            pppoe_status = "dial_failed"
            warning_msg = (
                f"PPPoE belum connect setelah 30 detik. "
                f"Cek kredensial modem (user={pppoe_user or '-'}) atau fisik modem."
            )
            print(f"[PROVISION] ONU {onu_index} — PPPoE {warning_msg}")
        elif pppoe_status == "disconnected":
            warning_msg = f"PPPoE disconnected — cek modem/kredensial"
        else:
            warning_msg = f"PPPoE status: {pppoe_status}"

        # Milestone 5: optical + distance
        olt_manager.update_job(job_id, progress=85, message="Ambil optical info")
        optical_rx = None
        optical_tx = None
        distance = None

        lock.acquire()
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
        )
        db.add(onu)
        db.commit()
        db.refresh(onu)
        audit(db, username, "provision_onu", req.serial_number)

        olt_manager.finish_job(job_id, "success", result={
            "onu_id": onu.id,
            "onu_index": onu_index,
            "internet_status": pppoe_status,
            "internet_online_duration": pppoe_dur,
            "optical_rx": optical_rx,
            "optical_tx": optical_tx,
            "distance": distance,
            "warning": warning_msg,
        })

    except Exception as e:
        print(f"[PROVISION] Fatal: {e}")
        olt_manager.finish_job(job_id, "failed", error=f"Error tidak terduga: {e}")
    finally:
        db.close()


@router.post("/{olt_id}/{onu_id}/reboot")
@olt_locked
def reboot_onu(olt_id: int, onu_id: int, db: Session = Depends(get_db),
               user: User = Depends(require_privilege(10))):
    olt = db.query(OLT).get(olt_id)
    onu = db.query(ONU).filter(ONU.olt_id == olt_id, ONU.onu_id == onu_id).first()
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


@router.delete("/{olt_id}/{onu_id}")
@olt_locked
def delete_onu(olt_id: int, onu_id: int, db: Session = Depends(get_db),
               user: User = Depends(require_privilege(15))):
    olt = db.query(OLT).get(olt_id)
    onu = db.query(ONU).filter(ONU.olt_id == olt_id, ONU.onu_id == onu_id).first()
    if not onu:
        raise HTTPException(404, "ONU tidak ditemukan")
    client = _make_client(olt)

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
                "write",
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
                "write",
            ]
        try:
            out = client.run_config(cmds)
            delete_log.append(f"[RUN_CONFIG] {out[:500]}")
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
            audit(db, user.username, "delete_onu", onu.serial_number or "",
                  "OLT masih punya ONU setelah delete", "failed")
            raise HTTPException(500, {
                "message": "Gagal hapus ONU di OLT — ONU masih ada. DB tidak diubah.",
                "delete_log": delete_log,
            })

    # 5. Baru hapus DB
    db.delete(onu)
    db.commit()
    audit(db, user.username, "delete_onu", onu.serial_number or "")
    return {"ok": True, "olt_had_onu": onu_exists_in_olt, "delete_log": delete_log}