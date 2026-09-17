"""Endpoint untuk cek status async job."""
from fastapi import APIRouter, Depends, HTTPException

from models import User
from auth import get_current_user
from olt_manager import olt_manager

router = APIRouter(prefix="/api/v1/jobs", tags=["jobs"])


@router.get("/{job_id}")
def get_job_status(job_id: str, user: User = Depends(get_current_user)):
    """Cek status job berdasarkan ID.
    
    Status yang mungkin:
    - queued: belum mulai
    - running: sedang berjalan (lihat field progress 0-100)
    - success: selesai sukses (lihat field result)
    - failed: gagal (lihat field error)
    """
    job = olt_manager.get_job(job_id)
    if not job:
        raise HTTPException(404, "Job tidak ditemukan atau sudah expired")
    return job


@router.get("")
def list_active_jobs(user: User = Depends(get_current_user)):
    """List semua job yang sedang berjalan (untuk job tray di UI)."""
    with olt_manager.jobs_lock:
        active = [
            dict(j) for j in olt_manager.jobs.values()
            if j["status"] in ("queued", "running")
        ]
    active.sort(key=lambda x: x["created_at"], reverse=True)
    return active
