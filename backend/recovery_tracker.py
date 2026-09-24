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


DEDUPE_WINDOW = 300   # detik — kalau source sama dalam window ini, skip


def add_recovery(olt_id: int, source: str, title: str):
    """Catat recovery — dipanggil saat alert auto-resolve.
    ⭐ Dedupe: kalau source yang sama sudah ada dalam DEDUPE_WINDOW, skip."""
    now = time.time()
    with _LOCK:
        # Cek duplikat
        for r in _RECENT_RECOVERIES:
            if r["source"] == source and (now - r["ts"]) < DEDUPE_WINDOW:
                # Sudah ada, update timestamp biar tetap fresh
                r["ts"] = now
                r["title"] = title
                return
        _RECENT_RECOVERIES.append({
            "olt_id": olt_id,
            "source": source,
            "title": title,
            "ts": now,
        })
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
