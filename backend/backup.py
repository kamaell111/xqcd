"""Backup DB SQLite otomatis — pakai sqlite3.Connection.backup().

Aman dipanggil saat DB aktif (SQLite handle locking internal).
Simpan di data/backups/, rotasi N generasi terakhir.
"""
import os
import sqlite3
import glob
from datetime import datetime

BACKUP_DIR = "./data/backups"
KEEP = 7   # simpan 7 backup terakhir


def _resolve_db_path() -> str:
    """Ambil path file DB dari DATABASE_URL."""
    from config import settings
    url = settings.DATABASE_URL
    if url.startswith("sqlite:///"):
        return url.replace("sqlite:///", "", 1)
    raise ValueError(f"URL database tidak didukung: {url}")


def run_backup() -> dict:
    """Buat backup baru + rotasi lama. Return statistik."""
    stats = {"created": None, "removed": 0, "error": None}
    try:
        os.makedirs(BACKUP_DIR, exist_ok=True)
        src_path = _resolve_db_path()
        if not os.path.exists(src_path):
            stats["error"] = f"File DB tidak ada: {src_path}"
            return stats

        stamp = datetime.utcnow().strftime("%Y%m%d_%H%M%S")
        dst_path = os.path.join(BACKUP_DIR, f"zte_olt_manager_{stamp}.db")
        tmp_path = dst_path + ".tmp"

        # sqlite3.backup() — aman saat DB aktif
        src = sqlite3.connect(src_path)
        dst = sqlite3.connect(tmp_path)
        try:
            src.backup(dst)
        finally:
            dst.close()
            src.close()

        # integrity check sebelum rename
        check = sqlite3.connect(tmp_path)
        try:
            result = check.execute("PRAGMA integrity_check").fetchone()[0]
            if result != "ok":
                os.remove(tmp_path)
                stats["error"] = f"Integrity check gagal: {result}"
                return stats
        finally:
            check.close()

        os.rename(tmp_path, dst_path)
        stats["created"] = dst_path

        # Rotasi — hapus yang lama
        existing = sorted(glob.glob(os.path.join(BACKUP_DIR, "zte_olt_manager_*.db")))
        if len(existing) > KEEP:
            for old in existing[:-KEEP]:
                try:
                    os.remove(old)
                    stats["removed"] += 1
                except Exception as e:
                    print(f"[BACKUP] gagal hapus {old}: {e}")

    except Exception as e:
        stats["error"] = str(e)

    return stats


def log_backup(stats: dict):
    """Print ringkas."""
    if stats.get("error"):
        print(f"[BACKUP] ERROR: {stats['error']}")
    else:
        print(f"[BACKUP] OK -> {stats['created']} (dihapus: {stats['removed']})")
