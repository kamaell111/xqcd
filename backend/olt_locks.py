"""olt_locks.py -- satu lock per OLT yang DIPAKAI BERSAMA (Edit VLAN ONU, VLANIF, ...), agar dua operasi tulis
tidak berjalan bersamaan di OLT yang sama. Ganti pemakaian _olt_lock/_LOCKS di onu_vlan_router.py dengan ini.
(Kalau olt_manager sudah punya lock per-OLT, pakai itu dan buang file ini.)"""
import threading

_LOCKS: dict = {}
_GUARD = threading.Lock()


def olt_lock(olt_id) -> threading.Lock:
    with _GUARD:
        return _LOCKS.setdefault(olt_id, threading.Lock())
