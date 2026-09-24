"""
OLT Manager — Lock per OLT + Async Job Store.

Fungsi:
- Lock per OLT: mencegah race condition antar request & scheduler
- Job store in-memory: untuk async job pattern (provisioning, dll)
- Cleanup job otomatis via scheduler
"""
import asyncio
import time
import uuid
import threading
from functools import wraps
from typing import Dict, Any, Optional


class OLTManager:
    def __init__(self):
        # Lock per OLT (asyncio.Lock, karena dipakai di async context)
        self.locks: Dict[str, asyncio.Lock] = {}
        # Thread lock per OLT (untuk endpoint sync yang jalan di threadpool)
        self.thread_locks: Dict[str, threading.Lock] = {}
        self.thread_locks_guard = threading.Lock()
        # Job store in-memory
        self.jobs: Dict[str, Dict[str, Any]] = {}
        # Thread lock untuk job store (karena bisa diakses dari thread berbeda)
        self.jobs_lock = threading.Lock()
        # Idempotency map: {(olt_id, resource_key): job_id}
        self.active_resources: Dict[tuple, str] = {}
        self.resources_lock = threading.Lock()
        # Circuit breaker per OLT: {olt_id: {failures, open_until, last_error, open_count}}
        self._circuit: Dict[str, Dict[str, Any]] = {}
        self._circuit_guard = threading.Lock()
        # Pending config changes (two-phase commit): {olt_id: {pending, last_op, last_op_time, last_commit_time}}
        self._pending_config: Dict[str, Dict[str, Any]] = {}
        self._pending_guard = threading.Lock()

    # =================== CLEANUP ===================
    def clear_olt_state(self, olt_id) -> Dict[str, int]:
        """Bersihkan state in-memory untuk OLT yang dihapus.

        Aman dipanggil sebelum db.delete(olt). Lock yang masih dipegang
        thread lain tidak akan di-force-release — cukup di-pop dari dict,
        thread pemegang akan release sendiri, lock baru dibuat kalau ada
        request baru.

        Returns: dict jumlah yang dibersihkan per kategori.
        """
        key = str(olt_id)
        cleared = {"locks": 0, "thread_locks": 0, "circuit": 0, "pending": 0, "jobs": 0}

        # 1. Locks (asyncio + thread) — pop tanpa acquire
        self.locks.pop(key, None); cleared["locks"] = 1
        with self.thread_locks_guard:
            if self.thread_locks.pop(key, None) is not None:
                cleared["thread_locks"] = 1

        # 2. Circuit breaker state
        with self._circuit_guard:
            if self._circuit.pop(key, None) is not None:
                cleared["circuit"] = 1

        # 3. Pending config (two-phase commit)
        with self._pending_guard:
            if self._pending_config.pop(key, None) is not None:
                cleared["pending"] = 1

        # 4. Job store — HANYA job yang sudah selesai (success/failed/cancelled).
        #    Job yang masih running jangan disentuh, biar tidak nyangkut.
        terminal_status = {"success", "failed", "error", "cancelled", "timeout"}
        with self.jobs_lock:
            to_remove = [
                jid for jid, j in self.jobs.items()
                if str(j.get("olt_id")) == key
                and j.get("status") in terminal_status
            ]
            for jid in to_remove:
                self.jobs.pop(jid, None)
            cleared["jobs"] = len(to_remove)

        # 5. Idempotency map — buang yang olt_id-nya sama dan job-nya sudah tidak ada
        with self.resources_lock:
            stale = [
                rk for rk in list(self.active_resources.keys())
                if str(rk[0]) == key
                and self.active_resources[rk] not in self.jobs
            ]
            for rk in stale:
                self.active_resources.pop(rk, None)

        return cleared

    # =================== LOCK ===================
    def get_lock(self, olt_id: str) -> asyncio.Lock:
        """Ambil atau buat lock untuk OLT tertentu."""
        if olt_id not in self.locks:
            self.locks[olt_id] = asyncio.Lock()
        return self.locks[olt_id]

    def get_thread_lock(self, olt_id: str) -> threading.Lock:
        """Ambil atau buat thread lock per OLT.
        Untuk endpoint sync (def) yang jalan di threadpool FastAPI."""
        with self.thread_locks_guard:
            if olt_id not in self.thread_locks:
                self.thread_locks[olt_id] = threading.Lock()
            return self.thread_locks[olt_id]

    # =================== CIRCUIT BREAKER ===================
    def record_olt_result(self, olt_id: str, success: bool, error: str = None):
        """Catat hasil operasi OLT. Kalau gagal berturut-turut → buka circuit."""
        with self._circuit_guard:
            state = self._circuit.setdefault(str(olt_id), {
                "failures": 0, "open_until": 0.0,
                "last_error": None, "open_count": 0,
            })
            if success:
                if state["failures"] > 0:
                    print(f"[CIRCUIT] OLT {olt_id} pulih — reset setelah {state['failures']} gagal")
                state["failures"] = 0
                state["open_until"] = 0.0
                state["last_error"] = None
                state["open_count"] = 0
            else:
                state["failures"] += 1
                state["last_error"] = (error or "")[:200]
                if state["failures"] >= CIRCUIT_FAIL_THRESHOLD:
                    idx = min(state["open_count"], len(CIRCUIT_BACKOFF_STEPS) - 1)
                    backoff = CIRCUIT_BACKOFF_STEPS[idx]
                    state["open_until"] = time.time() + backoff
                    state["open_count"] += 1
                    print(f"[CIRCUIT] OLT {olt_id} OPEN {backoff}s (gagal {state['failures']}x: {state['last_error']})")

    def is_olt_circuit_open(self, olt_id: str):
        """Return alasan kalau circuit OPEN, None kalau aman."""
        with self._circuit_guard:
            state = self._circuit.get(str(olt_id))
            if not state:
                return None
            now = time.time()
            if state["open_until"] > now:
                remain = int(state["open_until"] - now)
                return f"circuit OPEN — tunggu {remain}s (gagal {state['failures']}x terakhir)"
            return None

    def get_olt_circuit_state(self, olt_id: str) -> dict:
        """Ambil status circuit untuk ditampilkan di UI."""
        with self._circuit_guard:
            state = self._circuit.get(str(olt_id), {
                "failures": 0, "open_until": 0.0,
                "last_error": None, "open_count": 0,
            })
            now = time.time()
            is_open = state["open_until"] > now
            return {
                "is_open": is_open,
                "failures": state["failures"],
                "remain_sec": max(0, int(state["open_until"] - now)) if is_open else 0,
                "last_error": state["last_error"],
                "total_opens": state["open_count"],
            }

    # =================== TWO-PHASE COMMIT STATE ===================
    def mark_pending(self, olt_id: str, op: str = ""):
        """Tandai ada perubahan belum di-commit ke flash."""
        with self._pending_guard:
            state = self._pending_config.setdefault(str(olt_id), {
                "pending": False,
                "last_op": None,
                "last_op_time": 0.0,
                "last_commit_time": 0.0,
                "ops_count": 0,
            })
            state["pending"] = True
            state["last_op"] = op
            state["last_op_time"] = time.time()
            state["ops_count"] = state.get("ops_count", 0) + 1

    def mark_committed(self, olt_id: str):
        """Tandai config sudah di-commit (write sukses)."""
        with self._pending_guard:
            state = self._pending_config.setdefault(str(olt_id), {
                "pending": False,
                "last_op": None,
                "last_op_time": 0.0,
                "last_commit_time": 0.0,
                "ops_count": 0,
            })
            state["pending"] = False
            state["last_commit_time"] = time.time()
            state["ops_count"] = 0

    def get_pending_state(self, olt_id: str) -> dict:
        """Ambil status pending untuk UI."""
        with self._pending_guard:
            state = self._pending_config.get(str(olt_id), {
                "pending": False,
                "last_op": None,
                "last_op_time": 0.0,
                "last_commit_time": 0.0,
                "ops_count": 0,
            })
            now = time.time()
            return {
                "pending": state["pending"],
                "last_op": state["last_op"],
                "age_sec": max(0, int(now - state["last_op_time"])) if state["last_op_time"] else 0,
                "last_commit_sec_ago": max(0, int(now - state["last_commit_time"])) if state["last_commit_time"] else None,
                "ops_count": state.get("ops_count", 0),
            }

    # =================== JOB STORE ===================
    def create_job(self, olt_id: str, job_type: str, resource_key: str = None) -> str:
        """Buat job baru. Return job_id."""
        job_id = str(uuid.uuid4())
        now = time.time()
        with self.jobs_lock:
            self.jobs[job_id] = {
                "id": job_id,
                "olt_id": olt_id,
                "type": job_type,
                "status": "queued",
                "progress": 0,
                "message": "Menunggu antrean",
                "result": None,
                "error": None,
                "created_at": now,
                "updated_at": now,
            }
        # Register resource key untuk idempotency
        if resource_key:
            with self.resources_lock:
                self.active_resources[(olt_id, resource_key)] = job_id
        return job_id

    def update_job(self, job_id: str, **kwargs):
        """Update field job."""
        with self.jobs_lock:
            if job_id in self.jobs:
                self.jobs[job_id].update(kwargs)
                self.jobs[job_id]["updated_at"] = time.time()

    def get_job(self, job_id: str) -> Optional[Dict[str, Any]]:
        """Ambil job by ID."""
        with self.jobs_lock:
            job = self.jobs.get(job_id)
            return dict(job) if job else None

    def finish_job(self, job_id: str, status: str, result=None, error=None):
        """Tandai job selesai & release resource key."""
        with self.jobs_lock:
            if job_id in self.jobs:
                self.jobs[job_id].update({
                    "status": status,
                    "result": result,
                    "error": error,
                    "updated_at": time.time(),
                    "progress": 100 if status == "success" else self.jobs[job_id].get("progress", 0),
                })
                olt_id = self.jobs[job_id]["olt_id"]
        # Release resource keys milik job ini
        with self.resources_lock:
            keys_to_remove = [k for k, v in self.active_resources.items() if v == job_id]
            for k in keys_to_remove:
                del self.active_resources[k]

    def create_job_atomic(self, olt_id: str, job_type: str,
                          resource_key: str = None) -> tuple:
        """Bikin job baru ATAU return existing job_id — atomik.
        
        Return: (job_id, is_new) — is_new=True kalau baru dibuat.
        """
        with self.resources_lock:
            # Cek dulu apakah ada job aktif untuk resource ini
            if resource_key:
                existing = self.active_resources.get((olt_id, resource_key))
                if existing:
                    with self.jobs_lock:
                        job = self.jobs.get(existing)
                        if job and job["status"] not in ("success", "failed"):
                            return (existing, False)

            # Bikin job baru
            job_id = str(uuid.uuid4())
            now = time.time()
            with self.jobs_lock:
                self.jobs[job_id] = {
                    "id": job_id,
                    "olt_id": olt_id,
                    "type": job_type,
                    "status": "queued",
                    "progress": 0,
                    "message": "Menunggu antrean",
                    "result": None,
                    "error": None,
                    "created_at": now,
                    "updated_at": now,
                }
            if resource_key:
                self.active_resources[(olt_id, resource_key)] = job_id

            return (job_id, True)

    def find_active_job(self, olt_id: str, resource_key: str) -> Optional[str]:
        """Cek apakah ada job aktif untuk resource tertentu (idempotency)."""
        with self.resources_lock:
            job_id = self.active_resources.get((olt_id, resource_key))
            if not job_id:
                return None
            # Verifikasi job masih aktif
            with self.jobs_lock:
                job = self.jobs.get(job_id)
                if job and job["status"] not in ("success", "failed"):
                    return job_id
            return None

    def cleanup_old_jobs(self, ttl: int = 3600, orphan_timeout: int = 300) -> Dict[str, int]:
        """Bersihkan job lama. Return statistik."""
        now = time.time()
        deleted = 0
        orphaned = 0
        with self.jobs_lock:
            to_delete = []
            for jid, job in self.jobs.items():
                age = now - job["updated_at"]
                # Job selesai & sudah lama → hapus
                if job["status"] in ("success", "failed") and age > ttl:
                    to_delete.append(jid)
                # Job stuck IN_PROGRESS terlalu lama → mark failed
                elif job["status"] not in ("success", "failed") and age > orphan_timeout:
                    job["status"] = "failed"
                    job["error"] = f"Job stuck >{orphan_timeout}s — ditandai gagal oleh cleanup"
                    job["updated_at"] = now
                    orphaned += 1
            for jid in to_delete:
                del self.jobs[jid]
                deleted += 1
        return {"deleted": deleted, "orphaned": orphaned, "total_remaining": len(self.jobs)}


# =================== DECORATOR ===================
def olt_locked(fn):
    """Decorator: serialize request per OLT pakai thread lock.
    
    Cari olt_id dari:
    - kwargs['olt_id'] (int)
    - args[0] kalau int (langsung olt_id)
    - args[0].olt_id kalau objek (misal req.olt_id)
    """
    @wraps(fn)
    def wrapper(*args, **kwargs):
        olt_id = None

        # Cek kwargs
        if 'olt_id' in kwargs:
            olt_id = kwargs['olt_id']

        # Cek args[0]
        if olt_id is None and args:
            first = args[0]
            if isinstance(first, int):
                olt_id = first
            elif hasattr(first, 'olt_id'):
                olt_id = first.olt_id

        # Fallback: cek kwargs['req']
        if olt_id is None and 'req' in kwargs:
            req = kwargs['req']
            if hasattr(req, 'olt_id'):
                olt_id = req.olt_id

        if olt_id is None:
            # Tidak dapat olt_id — panggil tanpa lock
            return fn(*args, **kwargs)

        with olt_manager.get_thread_lock(str(olt_id)):
            return fn(*args, **kwargs)

    return wrapper


# =================== SINGLETON ===================
olt_manager = OLTManager()
