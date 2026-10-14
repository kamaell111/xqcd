"""
profile_executor.py — inti Fase 3a: evaluasi (dry-run) + eksekusi create/delete profile.

TIDAK mengimpor FastAPI, SQLAlchemy, maupun OLTClient. Semua I/O disuntik sebagai
fungsi, jadi seluruh logika keselamatan bisa diuji dengan OLT palsu (pytest).

Prinsip:
  * Hasil akhir ditentukan oleh KONDISI LIVE di OLT (baca ulang setelah menulis),
    bukan oleh ada/tidaknya exception dari wrapper CLI. Wrapper bisa melempar error
    padahal perintah berhasil (mis. 'end' ditolak), atau sebaliknya.
  * Create yang hasilnya tidak sesuai parameter -> rollback (hapus) oleh executor.
  * Delete yang gagal tidak punya rollback (tidak ada yang berubah).
  * Status akhir tidak bisa diverifikasi -> galat 502 eksplisit "cek manual".
"""
from typing import Any, Callable, Dict, List, Optional


class ExecError(Exception):
    """Galat eksekusi. `live` = daftar profile terbaru dari OLT (kalau sempat terbaca),
    dipakai pemanggil untuk menyamakan cache DB."""

    def __init__(self, status: int, message: str, *, live: Optional[list] = None,
                 detail: Optional[str] = None, rolled_back: Optional[bool] = None):
        super().__init__(message)
        self.status = status
        self.message = message
        self.live = live
        self.detail = detail
        self.rolled_back = rolled_back


def find_profile(items: Optional[list], name: Any) -> Optional[Dict[str, Any]]:
    """Cari profile by nama, TIDAK peduli huruf besar/kecil (konservatif)."""
    if not items or not isinstance(name, str):
        return None
    low = name.lower()
    for it in items:
        if it["name"].lower() == low:
            return it
    return None


def _chk(cid: str, label: str, ok: bool, detail: str = "", pending: bool = False) -> Dict[str, Any]:
    ok = bool(ok)
    return {"id": cid, "label": label, "ok": ok, "detail": detail,
            "pending": bool(pending) and not ok}


def _finish(name: Any, commands: List[str], checks: List[dict]) -> Dict[str, Any]:
    return {
        "name": name,
        "commands": commands,
        "checks": checks,
        # allowed: tidak ada penghalang keras (yang pending masih bisa dilengkapi user)
        "allowed": all(c["ok"] or c["pending"] for c in checks),
        # ready: boleh dieksekusi sekarang
        "ready": all(c["ok"] for c in checks),
        "needs": [c["id"] for c in checks if c["pending"]],
    }


# ----------------------------------------------------------------- evaluasi
def evaluate_create(driver, kind: str, params: Dict[str, Any], *,
                    live: Optional[list], live_error: Optional[str],
                    write_enabled: bool) -> Dict[str, Any]:
    name = params.get("name")
    checks: List[dict] = []
    commands: List[str] = []

    try:
        commands = driver.build_create(kind, params)
        checks.append(_chk("params", "Parameter valid", True))
    except ValueError as e:
        checks.append(_chk("params", "Parameter valid", False, str(e)))

    reserved = driver.is_reserved(name)
    checks.append(_chk("reserved", "Nama bukan nama bawaan OLT", not reserved,
                       "Nama 'default' dicadangkan oleh OLT." if reserved else ""))

    if live is None:
        checks.append(_chk("olt_read", "Daftar profile OLT terbaca", False,
                           live_error or "tidak bisa membaca OLT"))
    else:
        hit = find_profile(live, name)
        checks.append(_chk("free", "Nama belum dipakai di OLT", hit is None,
                           f"Sudah ada di OLT sebagai '{hit['name']}'." if hit else ""))

    checks.append(_chk("write", "Penulisan ke OLT diaktifkan", write_enabled,
                       "" if write_enabled else "PROFILE_WRITE_ENABLED belum dinyalakan di server."))
    return _finish(name, commands, checks)


def evaluate_delete(driver, kind: str, name: str, *,
                    live: Optional[list], live_error: Optional[str],
                    usage: Dict[str, Any], write_enabled: bool,
                    confirm_name: Optional[str], manual_ack: bool,
                    require_ack: bool = True) -> Dict[str, Any]:
    checks: List[dict] = []
    commands: List[str] = []

    try:
        commands = driver.build_delete(kind, name)
        checks.append(_chk("name_valid", "Nama profile valid", True))
    except ValueError as e:
        checks.append(_chk("name_valid", "Nama profile valid", False, str(e)))

    reserved = driver.is_reserved(name)
    checks.append(_chk("reserved", "Bukan profile bawaan OLT", not reserved,
                       "Profile 'default' tidak boleh dihapus." if reserved else ""))

    if live is None:
        checks.append(_chk("olt_read", "Daftar profile OLT terbaca", False,
                           live_error or "tidak bisa membaca OLT"))
    else:
        hit = find_profile(live, name)
        if hit is None:
            checks.append(_chk("exists", "Profile ada di OLT", False, "Tidak ditemukan di OLT."))
        elif hit["name"] != name:
            checks.append(_chk("exists", "Profile ada di OLT", False,
                               f"Nama di OLT berbeda huruf besar/kecil: '{hit['name']}'."))
        else:
            checks.append(_chk("exists", "Profile ada di OLT", True))

    blocking = int(usage.get("blocking_count") or 0)
    blocked = blocking > 0
    if blocked:
        checks.append(_chk("usage", "Tidak sedang dipakai", False,
                           f"{blocking} pemakai tercatat (ONU/paket). Lepaskan dulu."))
    else:
        checks.append(_chk("usage", "Tidak sedang dipakai (menurut DB aplikasi)", True,
                           usage.get("message") or ""))
        if require_ack:
            checks.append(_chk(
                "manual_ack", "Pemakaian sudah diverifikasi manual di OLT", bool(manual_ack),
                "DB aplikasi hanya mengenal ONU yang diprovision lewat aplikasi.",
                pending=not manual_ack))

    provided = bool(confirm_name)
    checks.append(_chk("confirm_name", "Nama profile diketik ulang", confirm_name == name,
                       "Ketik nama profile persis sama." if not provided else
                       ("" if confirm_name == name else "Nama yang diketik tidak sama."),
                       pending=not provided))

    checks.append(_chk("write", "Penulisan ke OLT diaktifkan", write_enabled,
                       "" if write_enabled else "PROFILE_WRITE_ENABLED belum dinyalakan di server."))
    return _finish(name, commands, checks)


# ---------------------------------------------------------------- eksekusi
def _err_text(e: Exception) -> str:
    return f"{type(e).__name__}: {e}"[:300]


def run_create(driver, kind: str, plan: Dict[str, Any], params: Dict[str, Any], *,
               fetch_live: Callable[[], list], send: Callable[[List[str]], Any]) -> Dict[str, Any]:
    """
    send(list_perintah)  -> kirim ke OLT (lewat wrapper strict). Boleh melempar.
    fetch_live()         -> list profile terbaru dari OLT. Boleh melempar.
    Return: {"ok": True, "live": [...], "warning": str|None}
    Raise ExecError kalau gagal.
    """
    name = plan["name"]
    # Cek ulang tepat sebelum menulis: nama harus masih bebas. Tanpa ini, rollback di bawah
    # bisa menghapus profile milik orang lain yang muncul setelah dry-run.
    try:
        pre = fetch_live()
    except Exception as e:  # noqa: BLE001
        raise ExecError(502, "Daftar profile OLT tidak terbaca sebelum menulis; tidak ada yang diubah.",
                        detail=_err_text(e))
    if find_profile(pre, name) is not None:
        raise ExecError(409, f"Profile '{name}' sudah ada di OLT; tidak ada yang diubah.", live=pre)
    send_err: Optional[str] = None
    try:
        send(plan["commands"])
    except Exception as e:  # noqa: BLE001 - kondisi live yang menentukan
        send_err = _err_text(e)

    try:
        live = fetch_live()
    except Exception as e:  # noqa: BLE001
        raise ExecError(502, "Status akhir di OLT tidak bisa diverifikasi. CEK MANUAL di OLT "
                             f"apakah profile '{name}' terbentuk.", detail=send_err or _err_text(e))

    item = find_profile(live, name)
    if item is not None and driver.matches_expected(kind, params, item):
        return {"ok": True, "live": live, "warning": send_err}

    if item is not None:
        # Terbentuk tapi parameter tidak sesuai -> batalkan.
        try:
            send(driver.build_delete(kind, item["name"]))
        except Exception:  # noqa: BLE001
            pass
        try:
            live2 = fetch_live()
        except Exception:  # noqa: BLE001
            live2 = None
        rolled = live2 is not None and find_profile(live2, name) is None
        raise ExecError(
            500,
            ("Profile terbentuk dengan parameter yang tidak sesuai; sudah dibatalkan (rollback)."
             if rolled else
             f"Profile '{name}' terbentuk dengan parameter tidak sesuai dan ROLLBACK GAGAL. CEK MANUAL."),
            live=live2 if live2 is not None else live, rolled_back=rolled, detail=send_err)

    raise ExecError(502, f"OLT tidak membuat profile: {send_err or 'tidak ada profile baru'}",
                    live=live, detail=send_err)


def run_delete(driver, kind: str, plan: Dict[str, Any], *,
               fetch_live: Callable[[], list], send: Callable[[List[str]], Any]) -> Dict[str, Any]:
    name = plan["name"]
    send_err: Optional[str] = None
    try:
        send(plan["commands"])
    except Exception as e:  # noqa: BLE001
        send_err = _err_text(e)

    try:
        live = fetch_live()
    except Exception as e:  # noqa: BLE001
        raise ExecError(502, "Status akhir di OLT tidak bisa diverifikasi. CEK MANUAL di OLT "
                             f"apakah profile '{name}' sudah terhapus.", detail=send_err or _err_text(e))

    if not live:
        raise ExecError(502, "Daftar profile OLT kosong/tak terbaca setelah menghapus. CEK MANUAL "
                             f"apakah profile '{name}' sudah terhapus.", detail=send_err)
    if find_profile(live, name) is None:
        return {"ok": True, "live": live, "warning": send_err}

    raise ExecError(409, f"OLT tidak menghapus profile (kemungkinan sedang dipakai): "
                         f"{send_err or 'profile masih ada'}", live=live, detail=send_err)
