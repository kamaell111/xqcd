import unittest
import onu_vlan_core as core

IFACE_CFG = """interface gpon-onu_1/1/1:1
  name peo sidorejo
  tcont 1 name PON1 profile 1G
  gemport 1 tcont 1
  gemport 1 traffic-limit downstream 1G 
  service-port 1 vport 1 user-vlan 15 vlan 15 
!
end
"""
MNG_CFG = """pon-onu-mng gpon-onu_1/1/1:1
  service PON1 gemport 1 iphost 1 vlan 15
  pppoe 1 nat enable user router1 password router1
  firewall enable low anti-hack disable
  security-mgmt 1 state enable mode forward protocol web 
  wan 1 service internet host 1
!
"""
IFACE = "gpon-onu_1/1/1:1"


class ParseList(unittest.TestCase):
    def test_ok(self):
        self.assertEqual(core.parse_vlan_list("1-2,15,101"), {1, 2, 15, 101})
        self.assertEqual(core.parse_vlan_list(" 15 "), {15})
        self.assertEqual(core.parse_vlan_list("-"), set())

    def test_bad(self):
        for bad in ["abc", "0", "4095", "5-3", "1,,2", "1;2", "1-2-3", "15 reload"]:
            with self.assertRaises(core.VlanError, msg=bad):
                core.parse_vlan_list(bad)

    def test_format_roundtrip(self):
        self.assertEqual(core.format_vlan_list({1, 2, 15, 101}), "1-2,15,101")
        self.assertEqual(core.parse_vlan_list(core.format_vlan_list({3, 4, 5, 9})), {3, 4, 5, 9})

    def test_extract_from_summary(self):
        self.assertEqual(core.extract_vlan_list("1-2,15,101"), {1, 2, 15, 101})
        self.assertEqual(core.extract_vlan_list("ZXAN#show vlan summary\nVlan list: 1-2,15,101\nTotal: 4\n"), {1, 2, 15, 101})

    def test_extract_failclosed(self):
        with self.assertRaises(core.VlanError):
            core.extract_vlan_list("tidak ada angka di sini")


class ParseCurrent(unittest.TestCase):
    def test_real_onu(self):
        c = core.parse_current(IFACE_CFG, MNG_CFG)
        self.assertEqual((c.sp_id, c.vport, c.vlan, c.svc_name, c.gemport, c.iphost), (1, 1, 15, "PON1", 1, 1))

    def test_refuse_two_service_ports(self):
        two = IFACE_CFG.replace("!\nend", "  service-port 2 vport 1 user-vlan 20 vlan 20\n!\nend")
        with self.assertRaises(core.VlanError):
            core.parse_current(two, MNG_CFG)

    def test_refuse_mismatch(self):
        with self.assertRaises(core.VlanError):
            core.parse_current(IFACE_CFG, MNG_CFG.replace("vlan 15", "vlan 16"))

    def test_refuse_empty(self):
        with self.assertRaises(core.VlanError):
            core.parse_current("", MNG_CFG)


class Plan(unittest.TestCase):
    def setUp(self):
        self.cur = core.parse_current(IFACE_CFG, MNG_CFG)

    def test_apply_and_rollback(self):
        p = core.build_plan(IFACE, self.cur, 101)
        self.assertEqual(p.apply, [
            "configure terminal", "interface gpon-onu_1/1/1:1", "no service-port 1",
            "service-port 1 vport 1 user-vlan 101 vlan 101", "exit",
            "pon-onu-mng gpon-onu_1/1/1:1", "service PON1 gemport 1 iphost 1 vlan 101", "exit", "end"])
        self.assertIn("service-port 1 vport 1 user-vlan 15 vlan 15", p.rollback)
        self.assertIn("service PON1 gemport 1 iphost 1 vlan 15", p.rollback)

    def test_bad_iface(self):
        with self.assertRaises(core.VlanError):
            core.build_plan("gpon-onu_1/1/1:1; reload", self.cur, 101)

    def test_validate_target(self):
        ol, up = {1, 2, 15, 101}, [{1, 2, 15, 101}]
        self.assertEqual(core.validate_target(101, self.cur, ol, up), [])
        self.assertTrue(core.validate_target(15, self.cur, ol, up))        # sama
        self.assertTrue(core.validate_target(200, self.cur, ol, up))       # belum ada di OLT + uplink
        self.assertTrue(core.validate_target(101, self.cur, ol, [{1, 2}])) # tidak di uplink
        self.assertTrue(core.validate_target(1, self.cur, ol, up))         # VLAN 1 ditolak
        self.assertTrue(core.validate_target("101", self.cur, ol, up))
        self.assertTrue(core.validate_target(True, self.cur, ol, up))

    def test_check_after(self):
        ok, _ = core.check_after(IFACE_CFG, MNG_CFG, 15)
        self.assertTrue(ok)
        ok, msg = core.check_after(IFACE_CFG, MNG_CFG, 101)
        self.assertFalse(ok)
        self.assertIn("15", msg)


if __name__ == "__main__":
    unittest.main()
