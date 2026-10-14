#!/usr/bin/env python3
"""
probe_profiles.py — PROBE READ-ONLY syntax profile GPON ZTE C320 V2.1.0

Tujuan: cari tahu parameter create/delete/edit profile T-CONT & Traffic
TANPA mengubah apa pun di OLT.

Pengaman:
  1. Hanya perintah berakhiran '?' atau diawali 'show ' (allowlist) yang boleh jalan.
  2. '?' dikirim TANPA Enter, lalu baris dibersihkan dengan Ctrl-U,
     supaya perintah setengah jadi tidak pernah dieksekusi.
  3. Snapshot 'show gpon profile tcont/traffic' sebelum & sesudah -> di-diff.
  4. Selalu keluar dari config mode (finally).

!! ADAPTER (bagian paling atas) harus disesuaikan dengan OLTClient aslimu.
   Aku belum lihat olt_client.py, jadi 3 fungsi itu masih kerangka.

Pakai:
  python probe_profiles.py --olt-id 1
  python probe_profiles.py --olt-id 1 --onu 1/1/1:1
  python probe_profiles.py --olt-id 1 --extra "profile tcont ?"
Hasil disimpan di probe_profiles_output.txt (paste isinya ke Claude).
"""
import argparse
import datetime
import difflib
import sys

DUMMY = "__PROBE_TMP__"   # nama yang tidak ada di OLT; dipakai hanya untuk '?'

# ======================= ADAPTER (PATCHED) =======================
import sys as _sys
import time as _time

# Pastikan backend ada di sys.path
_BACKEND = "/home/kamaell/Downloads/project-manager-olt-main/backend"
if _BACKEND not in _sys.path:
    _sys.path.insert(0, _BACKEND)

_client = None       # OLTClient
_conn = None         # netmiko connection (via _connect)


def connect(olt_id):
    """Buka koneksi lewat OLTClient."""
    global _client, _conn
    from database import SessionLocal
    from models import OLT
    from olt_client import OLTClient

    db = SessionLocal()
    olt = db.query(OLT).filter(OLT.id == int(olt_id)).first()
    if not olt:
        raise RuntimeError(f"OLT id={olt_id} tidak ditemukan")

    _client = OLTClient(
        host=olt.ip_address,
        username=olt.username,
        password=olt.password,
        enable_password=olt.enable_password,
        port=olt.port,
        protocol=olt.protocol,
    )
    _conn = _client._connect()._conn   # akses netmiko di bawah wrapper
    # Set terminal length 0
    _conn.send_command_timing("terminal length 0", read_timeout=10, last_read=0.3)


def send_line(cmd):
    """Kirim cmd + Enter, baca output."""
    return _conn.send_command_timing(cmd, read_timeout=15, last_read=1.0,
                                     strip_prompt=False, strip_command=False)


def send_help(cmd):
    """Kirim cmd TANPA Enter, baca help, clear dengan Ctrl-U."""
    # Kirim tanpa newline — cmd sudah berakhir '?'
    _conn.write_channel(cmd)
    _time.sleep(2.5)
    out = _conn.read_channel()
    _conn.write_channel("\x15")   # Ctrl-U: clear line
    _time.sleep(0.5)
    _conn.read_channel()
    return out


def disconnect():
    try:
        if _client:
            _client._connect().disconnect()
    except Exception:
        pass
# ===================================================================


def is_safe(cmd):
    c = cmd.strip()
    if c.endswith("?"):
        return True
    if c.startswith("show "):
        return True
    return c in {"configure terminal", "gpon", "end", "exit"}


def run(cmd, log):
    if not is_safe(cmd):
        log(f"[DITOLAK — tidak aman] {cmd}")
        return ""
    out = send_help(cmd) if cmd.strip().endswith("?") else send_line(cmd)
    log(f"\n$ {cmd}\n{out}")
    return out


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--olt-id", required=True)
    ap.add_argument("--onu", help="contoh: 1/1/1:1 (opsional, untuk cek pemakaian profile)")
    ap.add_argument("--extra", action="append", default=[],
                    help="perintah '?' tambahan (di config-gpon)")
    args = ap.parse_args()

    lines = []

    def log(s):
        print(s)
        lines.append(s)

    log(f"# probe {datetime.datetime.now().isoformat(timespec='seconds')}")
    connect(args.olt_id)
    try:
        # --- snapshot awal ---
        before_t = run("show gpon profile tcont", log)
        before_g = run("show gpon profile traffic", log)

        # --- help di mode exec ---
        for c in ["show gpon profile ?", "show gpon profile tcont ?",
                  "show gpon profile traffic ?"]:
            run(c, log)

        # --- cek pemakaian profile oleh ONU (opsional) ---
        if args.onu:
            run(f"show running-config interface gpon-onu_{args.onu}", log)
            run(f"show gpon onu detail-info gpon-onu_{args.onu}", log)

        # --- masuk config-gpon ---
        run("configure terminal", log)
        run("gpon", log)

        cmds = [
            "?",                                    # semua command di config-gpon
            "profile ?",
            "profile tcont ?",
            f"profile tcont {DUMMY} ?",
            f"profile tcont {DUMMY} type ?",
            f"profile tcont {DUMMY} type 4 ?",
            f"profile tcont {DUMMY} type 5 ?",
            "profile traffic ?",
            f"profile traffic {DUMMY} ?",
            f"profile traffic {DUMMY} sir ?",
            f"profile traffic {DUMMY} sir 1000 ?",
            "no profile ?",
            "no profile tcont ?",
            f"no profile tcont {DUMMY} ?",
            "no profile traffic ?",
            f"no profile traffic {DUMMY} ?",
        ] + args.extra
        for c in cmds:
            run(c, log)
    finally:
        try:
            run("end", log)
        except Exception:
            pass

        # --- snapshot akhir + diff: pastikan TIDAK ada yang berubah ---
        try:
            after_t = run("show gpon profile tcont", log)
            after_g = run("show gpon profile traffic", log)
            d = list(difflib.unified_diff(
                (before_t + before_g).splitlines(),
                (after_t + after_g).splitlines(), lineterm=""))
            log("\n# DIFF sebelum/sesudah: " + ("TIDAK ADA PERUBAHAN ✔" if not d
                                                 else "\n" + "\n".join(d)))
        finally:
            disconnect()

    with open("probe_profiles_output.txt", "w", encoding="utf-8") as f:
        f.write("\n".join(lines))
    print("\nTersimpan: probe_profiles_output.txt")


if __name__ == "__main__":
    sys.exit(main())
