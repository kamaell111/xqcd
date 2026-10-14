"""vlanif_flow.py -- orkestrasi apply + health check + verifikasi + rollback. I/O diinjeksi supaya bisa diuji dengan OLT tiruan.

CATATAN JUJUR tentang rollback:
  Kalau perubahan memutus jalur ke OLT, rollback lewat jaringan TIDAK BISA dijamin. Urutan percobaan:
  (1) sesi yang SUDAH terbuka (kadang masih hidup bila yang putus hanya routing), (2) sesi baru.
  Kalau keduanya gagal -> status rollback_failed + perintah manual; pemulihan akhir = console/serial atau jalur manajemen lain.
  Pagar utama tetap preflight anti-lockout, bukan rollback.
"""
import vlanif_core as core
from olt_strict import OLTCommandError

HEALTH_CMD = "show ip interface brief"


def read_state(client, vlan: int) -> dict:
    return core.parse_vlanif_config(str(client.run_commands([core.SHOW_CFG.format(n=vlan)])), vlan)


def _health(make_client, sleep, wait=3, tries=3, gap=2):
    sleep(wait)
    last = None
    for i in range(tries):
        try:
            c = make_client()
            c.run_commands([HEALTH_CMD])
            return c, None
        except Exception as e:
            last = e
            if i < tries - 1:
                sleep(gap)
    return None, last


def _rollback_cmds(action, vlan, old, now):
    if action == "create":
        return core.build_rollback_create_cmds(vlan) if now["exists"] else None
    target = {k: old[k] for k in ("mode", "ip", "mask", "desc")}
    try:
        return core.build_edit_cmds(vlan, now, target)
    except ValueError:
        return None                                  # sudah sama dengan kondisi awal


def manual_restore_cmds(action, vlan, old, expected):
    """Perintah manual untuk operator (dengan asumsi perubahan sudah penuh terpasang)."""
    if action == "create":
        return core.build_rollback_create_cmds(vlan)
    try:
        return core.build_edit_cmds(vlan, expected, {k: old[k] for k in ("mode", "ip", "mask", "desc")})
    except ValueError:
        return []


def rollback(action, vlan, old, clients, log):
    """Coba tiap klien berurutan. Return (ok, detail)."""
    last = "tidak ada klien yang bisa dipakai"
    for label, getc in clients:
        try:
            c = getc()
            now = read_state(c, vlan)
            cmds = _rollback_cmds(action, vlan, old, now)
            if cmds is None:
                log(f"rollback via {label}: tidak ada yang perlu dikembalikan")
                return True, "kondisi sudah sama dengan semula"
            c.run_config_strict(cmds)
            after = read_state(c, vlan)
            probs = (["interface masih ada"] if after["exists"] else []) if action == "create" else \
                core.verify_state({**old, "exists": True, "desc": core.nd(old["desc"])}, after)
            if not probs:
                return True, f"dikembalikan via {label}"
            last = f"{label}: verifikasi rollback gagal: {'; '.join(probs)}"
        except Exception as e:
            last = f"{label}: {type(e).__name__}: {e}"
        log(f"rollback via {label} gagal: {last}")
    return False, last


def apply_change(*, action, vlan, plan, client_a, make_client, sleep, log=lambda m: None) -> dict:
    """plan = hasil core.build_plan (ok=True). Tidak pernah memanggil write.
    Return {status: applied|rolled_back|rollback_failed, detail, manual_cmds}"""
    old, expected, cmds = plan["old"], plan["expected"], plan["commands"]
    manual = manual_restore_cmds(action, vlan, old, expected)

    def fail(reason, extra_client=None):
        clients = [("sesi lama", lambda: client_a)]
        if extra_client is not None:
            clients.append(("sesi baru", lambda: extra_client))
        clients.append(("sesi baru (reconnect)", make_client))
        ok, detail = rollback(action, vlan, old, clients, log)
        return {"status": "rolled_back" if ok else "rollback_failed",
                "detail": f"{reason}. {detail}", "manual_cmds": [] if ok else manual}

    try:
        client_a.run_config_strict(cmds)
    except OLTCommandError as e:
        return fail(f"apply gagal: {e}")
    except Exception as e:
        return fail(f"apply error: {type(e).__name__}: {e}")

    cb, err = _health(make_client, sleep)
    if cb is None:
        return fail(f"health check gagal, OLT tak terjangkau setelah perubahan ({err})")
    try:
        probs = core.verify_state(expected, read_state(cb, vlan))
    except Exception as e:
        probs = [f"verifikasi tak bisa membaca config: {e}"]
    if probs:
        return fail("verifikasi gagal: " + "; ".join(probs), extra_client=cb)
    return {"status": "applied", "detail": "diterapkan dan terverifikasi (BELUM permanen)", "manual_cmds": []}
