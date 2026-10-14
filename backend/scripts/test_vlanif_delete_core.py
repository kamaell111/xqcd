import os
import unittest
import vlanif_delete_core as c

OLT_IP = os.getenv("TEST_OLT_IP", "192.0.2.1")  # RFC 5737 TEST-NET placeholder
V2 = {"vlan": 2, "ip": "10.1.1.200"}
DUMMY = {"vlan": 30, "ip": "192.168.30.2"}


class Preflight(unittest.TestCase):
    def test_dummy_allowed(self):
        self.assertEqual(c.preflight(30, DUMMY, OLT_IP, {99}, {2}), [])

    def test_rule1_ip_equals_olt_ip(self):
        r = c.preflight(30, {"vlan": 30, "ip": OLT_IP + "/24"}, OLT_IP, set(), set())
        self.assertEqual(len(r), 1)

    def test_rule1_misses_nat_topology(self):
        # app connect via NAT_IP_PLACEHOLDER tapi interface OLT punya 136.1.1.200 -> rule 1 TIDAK kena,
        # yang melindungi jalur app adalah rule 2 (default route)
        mng_like = {"vlan": 99, "ip": "136.1.1.200"}
        self.assertEqual(c.preflight(99, mng_like, OLT_IP, set(), set()), [])
        self.assertEqual(len(c.preflight(99, mng_like, OLT_IP, {99}, set())), 1)

    def test_rule2_default_route(self):
        self.assertEqual(len(c.preflight(30, DUMMY, OLT_IP, {30}, set())), 1)

    def test_rule2_failclosed_when_routes_unknown(self):
        r = c.preflight(30, DUMMY, OLT_IP, None, set())
        self.assertEqual(len(r), 1)
        self.assertIn("ditolak", r[0])

    def test_rule3_protected(self):
        self.assertEqual(len(c.preflight(2, V2, OLT_IP, set(), {2})), 1)

    def test_multiple_reasons(self):
        self.assertEqual(len(c.preflight(2, {"vlan": 2, "ip": OLT_IP}, OLT_IP, {2}, {2})), 3)

    def test_missing_and_bad_vlan(self):
        self.assertTrue(c.preflight(30, None, OLT_IP, set(), set()))
        for bad in (0, 4095, "30", True, None):
            self.assertTrue(c.preflight(bad, DUMMY, OLT_IP, set(), set()), bad)

    def test_protected_env(self):
        self.assertEqual(c.protected_vlans("2, 10,,"), {2, 10})
        self.assertEqual(c.protected_vlans(""), set())
        self.assertEqual(c.protected_vlans(None), set())
        for bad in ("2,abc", "0", "4095", "2;10"):
            with self.assertRaises(ValueError, msg=bad):
                c.protected_vlans(bad)


class Confirm(unittest.TestCase):
    def test_full_ip_required(self):
        self.assertTrue(c.confirm_ok("192.168.30.2", 30, DUMMY))
        self.assertTrue(c.confirm_ok(" 192.168.30.2 ", 30, DUMMY))
        self.assertFalse(c.confirm_ok("30", 30, DUMMY))
        self.assertFalse(c.confirm_ok("vlan30", 30, DUMMY))
        self.assertFalse(c.confirm_ok("192.168.30.3", 30, DUMMY))
        self.assertFalse(c.confirm_ok("192.168.30.2/24", 30, DUMMY))
        self.assertFalse(c.confirm_ok("", 30, DUMMY))

    def test_ip_with_mask_in_data(self):
        self.assertTrue(c.confirm_ok("10.1.1.200", 2, {"vlan": 2, "ip": "10.1.1.200/24"}))

    def test_no_ip_uses_vlan_name(self):
        for v in ({"vlan": 31, "ip": None}, {"vlan": 31, "ip": "-"}, {"vlan": 31, "ip": ""}):
            self.assertEqual(c.expected_confirm(31, v), "vlan31")
            self.assertTrue(c.confirm_ok("vlan31", 31, v))
            self.assertFalse(c.confirm_ok("31", 31, v))


class Commands(unittest.TestCase):
    def test_build(self):
        self.assertEqual(c.build_delete(30), ["configure terminal", "no interface vlan 30", "exit"])

    def test_whitelist(self):
        for bad in ("reload", "no interface vlan 30; reload", "no interface vlan 0", "no interface vlan 4095",
                    "no interface vlan 30\nreload", "no interface vlan", "no interface mng1",
                    "no ip route 0.0.0.0 0.0.0.0 1.1.1.1", " exit", "", None):
            with self.assertRaises(c.DeleteBlocked, msg=repr(bad)):
                c.assert_allowed(bad)
        with self.assertRaises(c.DeleteBlocked):
            c.build_delete(4095)


SHOW_ROUTE = """ZXAN#show ip route
Total number of routes:       5
IPv4 Routing Table:
Dest            Mask            Gw              Interface    Owner   Pri Metric
0.0.0.0         0.0.0.0         136.1.1.1       mng1         static  1   0
10.1.1.0        255.255.255.0   10.1.1.200      vlan2        direct  0   0
10.1.1.200      255.255.255.255 10.1.1.200      vlan2        address 0   0
136.1.0.0       255.255.0.0     136.1.1.200     mng1         direct  0   0
136.1.1.200     255.255.255.255 136.1.1.200     mng1         address 0   0
ZXAN#"""


class RouteParse(unittest.TestCase):
    def test_real_olt_default_on_mng1(self):
        # default route ada di mng1 (bukan VLANIF) -> tidak ada VLAN yang terblokir oleh aturan 2
        self.assertEqual(c.parse_default_route_vlans(SHOW_ROUTE), set())

    def test_default_via_vlan(self):
        txt = SHOW_ROUTE.replace("136.1.1.1       mng1 ", "10.1.1.1       vlan2")
        self.assertEqual(c.parse_default_route_vlans(txt), {2})

    def test_two_default_routes(self):
        txt = SHOW_ROUTE.replace("Total number of routes:       5", "Total number of routes:       6").replace(
            "10.1.1.0 ", "0.0.0.0         0.0.0.0         10.9.9.1        vlan9        static  5   0\n10.1.1.0 ", 1)
        self.assertEqual(c.parse_default_route_vlans(txt), {9})

    def test_truncated_failclosed(self):
        cut = "\n".join(SHOW_ROUTE.splitlines()[:-3])
        with self.assertRaises(ValueError):
            c.parse_default_route_vlans(cut)

    def test_garbage_failclosed(self):
        for bad in ("", None, "% Error", "Total number of routes: 0"):
            with self.assertRaises(ValueError, msg=repr(bad)):
                c.parse_default_route_vlans(bad)

    def test_bad_row_failclosed(self):
        bad = SHOW_ROUTE.replace("0.0.0.0         0.0.0.0         136.1.1.1       mng1         static  1   0", "0.0.0.0")
        with self.assertRaises(ValueError):
            c.parse_default_route_vlans(bad)


class PickAndValidators(unittest.TestCase):
    ROWS = [
        {"interface": "vlan2", "vlan_id": 2, "ip_address": "10.1.1.200", "mask": "255.255.255.0"},
        {"interface": "mng1", "vlan_id": None, "ip_address": "136.1.1.200", "mask": "255.255.0.0"},
        {"interface": "vlan31", "vlan_id": 31, "ip_address": "0.0.0.0", "mask": None},
        {"interface": "vlan32", "vlan_id": "32", "ip_address": None, "mask": None},
    ]

    def test_pick(self):
        self.assertEqual(c.pick_vlanif(self.ROWS, 2), {"vlan": 2, "ip": "10.1.1.200"})
        self.assertEqual(c.pick_vlanif(self.ROWS, 31), {"vlan": 31, "ip": None})
        self.assertEqual(c.pick_vlanif(self.ROWS, 32), {"vlan": 32, "ip": None})
        self.assertIsNone(c.pick_vlanif(self.ROWS, 30))
        self.assertIsNone(c.pick_vlanif([], 2))

    def test_read_validator(self):
        c.assert_read_allowed("show ip route")
        c.assert_read_allowed("show ip interface brief")
        for bad in ("show running-config", "no interface vlan 30", "show ip route ", "reload"):
            with self.assertRaises(c.DeleteBlocked, msg=bad):
                c.assert_read_allowed(bad)

    def test_any_validator(self):
        for ok in ("show ip route", "show ip interface brief", "configure terminal", "no interface vlan 30", "exit"):
            c.assert_any_allowed(ok)
        for bad in ("reload", "no interface vlan 0", "show running-config", "no interface mng1"):
            with self.assertRaises(c.DeleteBlocked, msg=bad):
                c.assert_any_allowed(bad)


if __name__ == "__main__":
    unittest.main()
