"""Generic OLT job runner — bungkus operasi sync jadi async job.

Pola:
- Endpoint terima request → create job → return 202 + job_id
- Background task jalankan fungsi aktual
- Frontend poll GET /api/v1/jobs/{job_id}
"""
import functools
from datetime import datetime
from olt_manager import olt_manager
from database import SessionLocal


def run_olt_job(job_type: str, resource_key: str = None):
    """Decorator: bungkus fungsi endpoint jadi async job.

    Fungsi yang di-decorate harus return dict (result).
    Raise Exception kalau gagal.
    """
    def decorator(fn):
        @functools.wraps(fn)
        def wrapper(olt_id: int, *args, **kwargs):
            # Create job atomik
            job_id, is_new = olt_manager.create_job_atomic(
                olt_id=str(olt_id),
                job_type=job_type,
                resource_key=resource_key,
            )
            if not is_new:
                return {
                    "job_id": job_id,
                    "status": "queued",
                    "duplicate": True,
                    "message": "Job sudah berjalan untuk operasi ini",
                }

            # Cari BackgroundTasks dari kwargs
            from fastapi import BackgroundTasks
            bg = kwargs.get("background_tasks") or kwargs.get("bg")
            if bg is None:
                # Cari di kwargs
                for k, v in kwargs.items():
                    if isinstance(v, BackgroundTasks):
                        bg = v
                        break

            if bg is None:
                # Fallback: jalankan sync langsung
                _execute(job_id, fn, olt_id, args, kwargs)
            else:
                bg.add_task(_execute, job_id, fn, olt_id, args, kwargs)

            return {
                "job_id": job_id,
                "status": "queued",
                "message": "Job dimulai. Poll GET /api/v1/jobs/{job_id}",
            }
        return wrapper
    return decorator


def _execute(job_id: str, fn, olt_id: int, args: tuple, kwargs: dict):
    """Background worker: jalankan fungsi di session baru."""
    olt_manager.update_job(job_id, status="running", progress=10,
                           message="Mulai...")
    db = SessionLocal()
    try:
        # Buang BackgroundTasks dari kwargs
        from fastapi import BackgroundTasks
        clean_kwargs = {k: v for k, v in kwargs.items()
                        if not isinstance(v, BackgroundTasks)}
        clean_kwargs["db"] = db
        clean_kwargs["_job_id"] = job_id   # opsional, untuk update progress

        result = fn(olt_id, *args, **clean_kwargs)
        olt_manager.finish_job(job_id, "success", result=result)
    except Exception as e:
        import traceback
        traceback.print_exc()
        olt_manager.finish_job(job_id, "failed", error=str(e)[:300])
    finally:
        db.close()
