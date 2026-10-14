"""Eksekutor config STRICT: berhenti di error pertama, verifikasi, rollback.
Dipakai lewat mixin: class OLTClient(StrictConfigMixin). Hanya butuh self._connect()
yang mengembalikan koneksi ber-API send_command_timing() / disconnect()."""
import re

ERROR_MARKERS = re.compile(
    r"%\s*error|error\s*\d+|invalid|incomplete|unrecognized|ambiguous|already exist|not exist|fail",
    re.I,
)


class StrictConfigError(RuntimeError):
    def __init__(self, msg, log=None):
        super().__init__(msg)
        self.log = log if log is not None else []
        self.rollback_sent = False   # True = semua perintah rollback TERKIRIM (bukan jaminan berhasil)


class StrictConfigMixin:
    READ_TIMEOUT = 20

    def _send(self, conn, cmd, log, last_read=0.8):
        out = conn.send_command_timing(cmd, read_timeout=self.READ_TIMEOUT, last_read=last_read)
        log.append(f">>> {cmd}\n{out}")
        return out or ""

    def read_strict(self, commands, validator):
        """Baca-saja. Semua perintah divalidasi DULU sebelum koneksi dibuka."""
        for c in commands:
            validator(c)
        log = []
        conn = self._connect()
        try:
            self._send(conn, "terminal length 0", log, 0.3)
            return {c: self._send(conn, c, log, 1.0) for c in commands}
        finally:
            try:
                conn.disconnect()
            except Exception:
                pass

    def apply_strict(self, commands, rollback, verify_cmds, verify_fn, validator, save_cmd=None):
        for c in [*commands, *rollback, *verify_cmds, *([save_cmd] if save_cmd else [])]:
            validator(c)
        log = []
        conn = self._connect()
        try:
            self._send(conn, "terminal length 0", log, 0.3)
            try:
                for c in commands:
                    out = self._send(conn, c, log)
                    if c != "end" and ERROR_MARKERS.search(out.replace(c, "")):
                        raise StrictConfigError(f"OLT menolak perintah: {c}", log)
                self._send(conn, "end", log)
                outs = {v: self._send(conn, v, log, 1.0) for v in verify_cmds}
                ok, msg = verify_fn(outs)
                if not ok:
                    raise StrictConfigError(f"verifikasi gagal: {msg}", log)
            except Exception as exc:
                err = exc if isinstance(exc, StrictConfigError) else StrictConfigError(f"gangguan koneksi/OLT: {exc}", log)
                err.rollback_sent = self._rollback(conn, rollback, log)
                if err is not exc:
                    raise err from exc
                raise err
            if save_cmd:                       # simpan permanen HANYA setelah verifikasi lolos
                self._send(conn, save_cmd, log)
            return log
        finally:
            try:
                conn.disconnect()
            except Exception:
                pass

    _CONF_TERM = ("configure terminal", "config terminal", "conf t")

    def run_config_strict(self, commands):
        """Dipakai vlanif_flow. Kirim urutan config, BERHENTI di error pertama.
        - tiap perintah harus lolos self._ALLOWED (whitelist OLTClient); tidak ada _ALLOWED -> ditolak
        - 'configure terminal' ditambahkan di depan kalau belum ada
        - return: output gabungan (str); error -> StrictConfigError
        Tidak ada rollback di sini: rollback urusan vlanif_flow."""
        allowed = getattr(self, "_ALLOWED", None)
        if allowed is None:
            raise StrictConfigError("whitelist _ALLOWED tidak ditemukan di OLTClient; perintah ditolak")
        cmds = [c.strip() if isinstance(c, str) else c for c in (commands or [])]
        if not cmds:
            raise StrictConfigError("daftar perintah kosong")
        if cmds[0].lower() not in self._CONF_TERM:
            cmds.insert(0, "configure terminal")

        def validator(c):
            if not (isinstance(c, str) and c.isprintable() and allowed.match(c)):
                raise ValueError(f"perintah tidak diizinkan: {c!r}")

        log = self.apply_strict(cmds, rollback=[], verify_cmds=[],
                                verify_fn=lambda outs: (True, "ok"), validator=validator)
        return "\n".join(log)

    def _rollback(self, conn, rollback, log):
        sent_all = True
        try:
            self._send(conn, "end", log)       # keluar dari mode config mana pun
        except Exception:
            pass
        for c in rollback:                     # best-effort: error tidak menghentikan rollback
            try:
                self._send(conn, c, log)
            except Exception:
                sent_all = False
                log.append(f"!!! rollback gagal terkirim: {c}")
        return sent_all


# ==================== Compatibility Shim ====================
# Alias untuk vlanif_flow.py (nama berbeda, fungsi sama)
OLTCommandError = StrictConfigError
