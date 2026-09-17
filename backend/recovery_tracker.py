"""Track alert yang baru saja resolved — untuk notifikasi ke user.

In-memory, TTL 60 detik. Frontend polling endpoint ini untuk tahu
"apakah ada ONU yang baru recovery?".
"""
import time
import threading
from typing import List, Dict

_RECENT_RECOVERIES: List[Dict] = []
_LOCK = threading.Lock()
TTL = 60   # detik — berapa lama disimpan untuk polling frontend


def add_recovery(olt_id: int, source: str, title: str):
    """Catat recovery — dipanggil saat alert auto-resolve."""
    with _LOCK:
        _RECENT_RECOVERIES.append({
            "olt_id": olt_id,
            "source": source,
            "title": title,
            "ts": time.time(),
        })
        # Cleanup yang lama
        _cleanup_locked()


def get_recoveries(since_ts: float = 0) -> List[Dict]:
    """Ambil recovery setelah timestamp tertentu. Auto-cleanup yang lama."""
    with _LOCK:
        _cleanup_locked()
        return [r for r in _RECENT_RECOVERIES if r["ts"] > since_ts]


def _cleanup_locked():
    """Bersihkan recovery lama (>TTL). Dipanggil dari context lock."""
    now = time.time()
    while _RECENT_RECOVERIES and (now - _RECENT_RECOVERIES[0]["ts"]) > TTL:
        _RECENT_RECOVERIES.pop(0)
