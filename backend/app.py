from fastapi import FastAPI
from fastapi.middleware.cors import CORSMiddleware
from fastapi.staticfiles import StaticFiles
from contextlib import asynccontextmanager
from apscheduler.schedulers.asyncio import AsyncIOScheduler
from datetime import datetime
from database import engine, Base, SessionLocal
from config import settings
from models import User, OLT, PONPort, ONU, Interface, VLAN, Alert
from auth import hash_password
import asyncio
import os

from routers import auth as auth_router
from routers import olts, monitoring, config, onu, alerts, users, sync, ports, jobs

scheduler = AsyncIOScheduler()


def seed_data():
    db = SessionLocal()
    try:
        if db.query(User).count() > 0:
            return
        db.add_all([
            User(username=settings.SEED_ADMIN_USERNAME,
                 password_hash=hash_password(settings.SEED_ADMIN_PASSWORD),
                 privilege=15, role="admin"),
            User(username=settings.SEED_OPERATOR_USERNAME,
                 password_hash=hash_password(settings.SEED_OPERATOR_PASSWORD),
                 privilege=10, role="operator"),
            User(username=settings.SEED_VIEWER_USERNAME,
                 password_hash=hash_password(settings.SEED_VIEWER_PASSWORD),
                 privilege=5, role="viewer"),
        ])

        olt = OLT(
            hostname=settings.SEED_OLT_HOSTNAME,
            ip_address=settings.SEED_OLT_IP,
            protocol=settings.SEED_OLT_PROTOCOL,
            port=settings.SEED_OLT_PORT,
            username=settings.SEED_OLT_USERNAME,
            password=settings.SEED_OLT_PASSWORD,
            enable_password=settings.SEED_OLT_ENABLE_PASSWORD or None,
            snmp_community_ro=settings.SEED_OLT_SNMP_RO,
            snmp_community_rw=settings.SEED_OLT_SNMP_RW,
            model="C320", firmware="V2.1.0", location="",
            status="unknown", cpu_usage=None, memory_usage=None,
            uptime_seconds=None, temperature=None,
        )
        db.add(olt)
        db.commit()
        db.refresh(olt)

        for i in range(1, 17):
            db.add(PONPort(
                olt_id=olt.id, port_no=f"1/1/{i}",
                status="up" if i == 1 else "shutdown",
                admin_state="no shutdown" if i == 1 else "shutdown",
                linktrap=False,
                optical_tx=-3.5 if i == 1 else None,
                optical_rx=-12.8 if i == 1 else None,
                onu_count=1 if i == 1 else 0,
            ))

        db.add(ONU(
            olt_id=olt.id, pon_port="1/1/1", onu_id=1,
            interface_name="gpon-onu_1/1/1:1",
            serial_number="YYKC37D4BADA", name="CLIENT-YYKC37D4",
            type="F609", status="online",
            optical_tx=2.5, optical_rx=-18.3, distance=1240,
            tcont="1G", gemport=1, service_port=1,
            user_vlan=15, vlan=15, pppoe_user="zte", pppoe_nat=True,
            last_online=datetime.utcnow(),
        ))

        ifaces = [
            dict(name="gei_1/3/1", type="gei", status="up", admin_state="no shutdown",
                 speed=1000, duplex="full", negotiation="auto", linktrap=True,
                 switchport_mode="trunk", vlans="1-2", hybrid_attribute="fiber"),
            dict(name="xgei_1/3/2", type="xgei", status="up", admin_state="no shutdown",
                 speed=10000, duplex="full", negotiation="manual", linktrap=True,
                 switchport_mode="trunk", vlans="1-2", hybrid_attribute="fiber"),
            dict(name="gei_1/3/3", type="gei", status="up", admin_state="no shutdown",
                 speed=1000, duplex="full", negotiation="auto", linktrap=True,
                 switchport_mode="trunk", vlans="1-2,15,101", hybrid_attribute="copper"),
            dict(name="gei_1/4/1", type="gei", status="up", admin_state="no shutdown",
                 speed=1000, duplex="full", negotiation="auto", linktrap=True,
                 switchport_mode="trunk", vlans="1", hybrid_attribute="fiber"),
            dict(name="xgei_1/4/2", type="xgei", status="up", admin_state="no shutdown",
                 speed=10000, duplex="full", negotiation="manual", linktrap=True,
                 switchport_mode="trunk", vlans="1", hybrid_attribute="fiber"),
            dict(name="gei_1/4/3", type="gei", status="down", admin_state="shutdown",
                 speed=1000, duplex="full", negotiation="auto", linktrap=True,
                 switchport_mode="trunk", vlans="1", hybrid_attribute="copper"),
        ]
        for iface in ifaces:
            db.add(Interface(olt_id=olt.id, **iface))

        for v in [1, 2, 15, 101]:
            db.add(VLAN(olt_id=olt.id, vlan_id=v, name=f"VLAN{v}"))

        db.commit()
        print("Seed data berhasil (tanpa alert dummy)")
    finally:
        db.close()


async def poll_olts():
    """Auto-sync via Telnet (CPU/Mem/Uptime) tiap X detik."""
    import asyncio
    from olt_client import OLTClient
    from olt_manager import olt_manager
    from models import MetricHistory
    db = SessionLocal()
    try:
        olts = db.query(OLT).all()
        for olt in olts:
            # ⭐ LOCK per OLT — skip kalau sedang dipakai endpoint manual
            lock = olt_manager.get_thread_lock(str(olt.id))
            if not lock.acquire(blocking=False):
                print(f"[AUTO-SYNC] {olt.ip_address} — OLT sibuk (dipakai operasi lain), skip siklus ini")
                continue

            try:
                await _do_olt_sync(db, olt)
            finally:
                lock.release()

            db.commit()
    finally:
        db.close()

    # Cleanup job lama (>30 menit)
    try:
        from olt_manager import olt_manager
        stats = olt_manager.cleanup_old_jobs(ttl=1800, orphan_timeout=300)
        if stats["deleted"] > 0 or stats["orphaned"] > 0:
            print(f"[CLEANUP] Jobs: deleted={stats['deleted']}, orphaned={stats['orphaned']}")
    except Exception as e:
        print(f"[CLEANUP] Error: {e}")


async def _do_olt_sync(db, olt):
    """Sync CPU/Mem satu OLT. Lock sudah di-acquire oleh caller."""
    import asyncio
    from olt_client import OLTClient
    from models import MetricHistory

    client = OLTClient(
        host=olt.ip_address,
        username=olt.username,
        password=olt.password,
        enable_password=olt.enable_password,
        port=olt.port,
        protocol=olt.protocol,
    )
    try:
                # Jalankan di thread pool biar tidak block event loop
                def _do_sync():
                    conn = client._connect()
                    try:
                        proc = conn.send_command_timing("show processor", read_timeout=20)
                        sysinfo = conn.send_command_timing("show system-group", read_timeout=20)
                        return proc, sysinfo
                    finally:
                        conn.disconnect()

                proc_out, sys_out = await asyncio.to_thread(_do_sync)

                # Parse (import dari routers/sync)
                from routers.sync import _parse_processor, _parse_system_group
                proc = _parse_processor(proc_out)
                sysinfo = _parse_system_group(sys_out)

                now = datetime.utcnow()
                if proc:
                    olt.cpu_usage = proc["cpu_usage"]
                    olt.memory_usage = proc["memory_usage"]
                    db.add(MetricHistory(olt_id=olt.id, metric="cpu",
                                         value=proc["cpu_usage"], ts=now))
                    db.add(MetricHistory(olt_id=olt.id, metric="memory",
                                         value=proc["memory_usage"], ts=now))
                if sysinfo:
                    if sysinfo["uptime_seconds"] is not None:
                        olt.uptime_seconds = sysinfo["uptime_seconds"]
                    if sysinfo["hostname"]:
                        olt.hostname = sysinfo["hostname"]
                    if sysinfo["model"]:
                        olt.model = sysinfo["model"]
                    if sysinfo["firmware"]:
                        olt.firmware = sysinfo["firmware"]

                olt.status = "online"
                olt.last_polled = now

                # 🔔 AUTO-ALERT resource
                try:
                    from alerting import check_olt_resource_alerts
                    check_olt_resource_alerts(db, olt)
                except Exception as e:
                    print(f"[AUTO-ALERT] {e}")

                print(f"[AUTO-SYNC] {olt.ip_address} OK · CPU {olt.cpu_usage}% · Mem {olt.memory_usage}%")
    except Exception as e:
        print(f"[AUTO-SYNC] {olt.ip_address} error: {e}")
        olt.status = "offline"


@asynccontextmanager
async def lifespan(app: FastAPI):
    Base.metadata.create_all(bind=engine)
    seed_data()
    scheduler.add_job(poll_olts, "interval", seconds=settings.SNMP_POLL_INTERVAL)
    scheduler.start()

    # ⭐ Start SNMP Trap receiver
    from trap_receiver import start_trap_receiver
    from trap_sync import trigger_light_sync_from_trap

    trap_transport = await start_trap_receiver(trigger_light_sync_from_trap)
    if trap_transport:
        print("[APP] SNMP Trap receiver aktif")
    else:
        print("[APP] SNMP Trap receiver TIDAK aktif (port 1620 tidak bisa dibind)")

    yield

    # Shutdown
    if trap_transport:
        trap_transport.close()
        print("[APP] SNMP Trap receiver dimatikan")
    scheduler.shutdown()


app = FastAPI(title=settings.APP_NAME, version="1.0.0", lifespan=lifespan)

app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)

app.include_router(auth_router.router)
app.include_router(olts.router)
app.include_router(monitoring.router)
app.include_router(config.router)
app.include_router(onu.router)
app.include_router(alerts.router)
app.include_router(users.router)
app.include_router(sync.router)
app.include_router(ports.router)
app.include_router(jobs.router)


@app.get("/api/health")
def health():
    return {"status": "ok", "app": settings.APP_NAME}


FRONTEND_DIR = os.path.abspath(os.path.join(os.path.dirname(__file__), "..", "frontend"))
if os.path.exists(FRONTEND_DIR):
    app.mount("/", StaticFiles(directory=FRONTEND_DIR, html=True), name="frontend")
else:
    @app.get("/")
    def root():
        return {"message": "Frontend belum dibuat. Letakkan index.html di folder ../frontend/"}