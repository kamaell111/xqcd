"""Jalankan DI REPO-MU (butuh olt_client asli). Memastikan whitelist validate_commands mengizinkan SEMUA command
Add/Edit VLANIF -- dan hanya itu. Whitelist jangan dilonggarkan ke 'ip ...' umum (ip route bisa memutus akses)."""
import pytest
olt_client = pytest.importorskip("olt_client")
import vlanif_core as c


def _client():
    # ADAPT: kalau konstruktor langsung connect, ganti dengan mock/fixture milikmu
    return olt_client.OLTClient(host="127.0.0.1", username="u", password="p", enable_password="e", port=23, protocol="telnet")


def _ok(cmds):
    r = _client().validate_commands(cmds)
    assert r["valid"], r["errors"]


def test_create_and_edit_and_rollback_cmds_pass():
    new = c.validate_input(30, "static", "192.168.30.2", "255.255.255.0", "REMOTE-OL1")
    old = c.parse_vlanif_config("interface vlan 2\n ip address 10.1.1.200 255.255.255.0\n description xxx\n!\n", 2)
    _ok(c.build_create_cmds(30, new))
    _ok(c.build_edit_cmds(2, old, c.validate_input(2, "static", "10.1.1.201", "255.255.255.0", "")))   # no ip address, ip address, no description
    _ok(c.build_rollback_create_cmds(30))                                                              # no interface vlan 30 (khusus rollback)


@pytest.mark.parametrize("cmd", ["ip route 0.0.0.0 0.0.0.0 10.1.1.1", "no ip route 0.0.0.0 0.0.0.0 10.1.1.1",
                                 "interface mng", "no interface mng", "no interface gpon-onu_1/1/1:1"])
def test_dangerous_commands_stay_blocked(cmd):
    assert not _client().validate_commands(["configure terminal", cmd])["valid"]
