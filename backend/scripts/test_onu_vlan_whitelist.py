import unittest
import onu_vlan_core as core
from olt_strict import StrictConfigMixin, StrictConfigError
from test_onu_vlan_core import IFACE_CFG, MNG_CFG, IFACE


class Whitelist(unittest.TestCase):
    def setUp(self):
        self.plan = core.build_plan(IFACE, core.parse_current(IFACE_CFG, MNG_CFG), 101)

    def test_builder_output_allowed(self):
        for c in self.plan.apply + self.plan.rollback + self.plan.verify + ["write"]:
            self.assertTrue(core.is_config_allowed(c) or core.is_show_allowed(c), c)

    def test_dangerous_rejected(self):
        bad = [
            "reload", "write erase", "delete flash:/x", "no interface gpon-onu_1/1/1:1",
            "exit\nreload", "exit\r", " exit", "exit ",
            "service-port 1 vport 1 user-vlan 101 vlan 101; reload",
            "service-port 1 vport 1 user-vlan 0 vlan 0",
            "service-port 1 vport 1 user-vlan 4095 vlan 4095",
            "service PON1 gemport 1 iphost 1 vlan 9999",
            "service PON1; reload gemport 1 iphost 1 vlan 20",
            "show running-config", "show running-config interface gpon-onu_1/1/1:1 | include x",
            "interface gpon-olt_1/1/1", "pon-onu-mng gpon-onu_1/1/1:1 reboot", "", None,
        ]
        for c in bad:
            with self.assertRaises(core.VlanError, msg=repr(c)):
                core.assert_allowed(c)


class FakeConn:
    def __init__(self, reply):
        self.reply, self.sent, self.closed = reply, [], False

    def send_command_timing(self, cmd, **kw):
        self.sent.append(cmd)
        return self.reply(cmd)

    def disconnect(self):
        self.closed = True


class FakeClient(StrictConfigMixin):
    def __init__(self, reply):
        self.conn, self.connected = FakeConn(reply), False

    def _connect(self):
        self.connected = True
        return self.conn


class Strict(unittest.TestCase):
    def setUp(self):
        self.plan = core.build_plan(IFACE, core.parse_current(IFACE_CFG, MNG_CFG), 101)
        self.after = (IFACE_CFG.replace("vlan 15", "vlan 101").replace("user-vlan 15", "user-vlan 101"),
                      MNG_CFG.replace("vlan 15", "vlan 101"))

    def verify_fn(self, outs):
        return core.check_after(outs[self.plan.verify[0]], outs[self.plan.verify[1]], 101)

    def run_apply(self, client, save=None):
        return client.apply_strict(self.plan.apply, self.plan.rollback, self.plan.verify,
                                   self.verify_fn, core.assert_allowed, save_cmd=save)

    def good_reply(self, cmd):
        if cmd == self.plan.verify[0]:
            return self.after[0]
        if cmd == self.plan.verify[1]:
            return self.after[1]
        return ""

    def test_validator_blocks_before_connect(self):
        c = FakeClient(self.good_reply)
        with self.assertRaises(core.VlanError):
            c.apply_strict(self.plan.apply + ["reload"], self.plan.rollback, self.plan.verify,
                           self.verify_fn, core.assert_allowed)
        self.assertFalse(c.connected)

    def test_read_blocks_before_connect(self):
        c = FakeClient(lambda x: "")
        with self.assertRaises(core.VlanError):
            c.read_strict(["show running-config"], core.assert_allowed)
        self.assertFalse(c.connected)

    def test_success_no_save(self):
        c = FakeClient(self.good_reply)
        self.run_apply(c)
        self.assertNotIn("write", c.conn.sent)
        self.assertTrue(c.conn.closed)
        self.assertEqual(c.conn.sent.count("no service-port 1"), 1)   # tidak ada rollback

    def test_success_with_save(self):
        c = FakeClient(self.good_reply)
        self.run_apply(c, save="write")
        self.assertEqual(c.conn.sent[-1], "write")

    def test_error_stops_and_rolls_back(self):
        def reply(cmd):
            return "% Invalid input detected" if cmd.startswith("service-port 1 vport 1 user-vlan 101") else ""
        c = FakeClient(reply)
        with self.assertRaises(StrictConfigError) as cm:
            self.run_apply(c, save="write")
        s = c.conn.sent
        self.assertTrue(cm.exception.rollback_sent)
        self.assertNotIn("service PON1 gemport 1 iphost 1 vlan 101", s)      # berhenti di error pertama
        self.assertIn("service-port 1 vport 1 user-vlan 15 vlan 15", s)      # rollback jalan
        self.assertNotIn("write", s)                                         # tidak disimpan
        self.assertTrue(c.conn.closed)

    def test_verify_fail_rolls_back(self):
        c = FakeClient(lambda cmd: IFACE_CFG if cmd == self.plan.verify[0] else (MNG_CFG if cmd == self.plan.verify[1] else ""))
        with self.assertRaises(StrictConfigError) as cm:
            self.run_apply(c, save="write")
        self.assertIn("verifikasi gagal", str(cm.exception))
        self.assertIn("service PON1 gemport 1 iphost 1 vlan 15", c.conn.sent)
        self.assertNotIn("write", c.conn.sent)

    def test_connection_drop_reported(self):
        class Dead(FakeConn):
            def send_command_timing(self, cmd, **kw):
                if cmd.startswith("interface"):
                    raise OSError("socket closed")
                return super().send_command_timing(cmd, **kw)
        c = FakeClient(self.good_reply)
        c.conn = Dead(self.good_reply)
        with self.assertRaises(StrictConfigError) as cm:
            self.run_apply(c)
        self.assertIn("gangguan koneksi", str(cm.exception))


if __name__ == "__main__":
    unittest.main()
