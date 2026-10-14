import re
import unittest
from olt_strict import StrictConfigMixin, StrictConfigError

# salinan _ALLOWED asli dari olt_client.py (versi yang ditempel user)
REAL_ALLOWED = re.compile(
    r"^\s*("
    r"configure|config|interface|exit|end|no\s|undo\s|"
    r"onu|gpon|pon|epon|gpon-olt|gpon-onu|pon-onu-mng|"
    r"tcont|gemport|service-port|vport|sn-bind|name|"
    r"profile|onu-type|add-card|add-rack|add-shelf|add-subcard|"
    r"uncfg-onu-display-info|onu-pnp|"
    r"pppoe|wan|iphost|firewall|security-mgmt|"
    r"ip\s|iphost|dhcp|"
    r"switchport|hybrid-attribute|negotiation|speed|duplex|"
    r"flowcontrol|linktrap|shutdown|port-protect|uplink-isolate|"
    r"description|mtu|tag-mode|phy-attribute|"
    r"vlan|vlan-reserve|"
    r"hostname|username|snmp-server|logging|clock|"
    r"service\s|line\s|banner|message-of-day|"
    r"config-filename|set-pnp|operator-mode|"
    r"ip\s+route|static-route|"
    r"write|save"
    r")\b", re.IGNORECASE)


class FakeConn:
    def __init__(self, reply):
        self.reply, self.sent, self.closed = reply, [], False

    def send_command_timing(self, cmd, **kw):
        self.sent.append(cmd)
        return self.reply(cmd)

    def disconnect(self):
        self.closed = True


class FakeClient(StrictConfigMixin):
    def __init__(self, reply=lambda c: "", allowed=REAL_ALLOWED):
        self.conn, self.connected = FakeConn(reply), False
        if allowed is not None:
            self._ALLOWED = allowed

    def _connect(self):
        self.connected = True
        return self.conn


VLANIF = ["interface vlan 30", "ip address 192.168.30.2 255.255.255.0", "description lab", "exit"]


class RunConfigStrict(unittest.TestCase):
    def test_success_returns_str_and_prepends_configure(self):
        c = FakeClient(lambda cmd: "OK " + cmd)
        out = c.run_config_strict(VLANIF)
        self.assertIsInstance(out, str)
        self.assertEqual(c.conn.sent[:3], ["terminal length 0", "configure terminal", "interface vlan 30"])
        self.assertEqual(c.conn.sent[-1], "end")
        self.assertTrue(c.conn.closed)

    def test_no_duplicate_configure(self):
        c = FakeClient()
        c.run_config_strict(["configure terminal"] + VLANIF)
        self.assertEqual(c.conn.sent.count("configure terminal"), 1)

    def test_strips_whitespace(self):
        c = FakeClient()
        c.run_config_strict(["  interface vlan 30  ", "exit "])
        self.assertIn("interface vlan 30", c.conn.sent)

    def test_rejects_not_allowed_before_connect(self):
        c = FakeClient()
        with self.assertRaises(ValueError):
            c.run_config_strict(["interface vlan 30", "reload"])
        self.assertFalse(c.connected)

    def test_rejects_newline_injection(self):
        c = FakeClient()
        with self.assertRaises(ValueError):
            c.run_config_strict(["interface vlan 30\nreload"])
        self.assertFalse(c.connected)

    def test_no_allowed_attr_failclosed(self):
        c = FakeClient(allowed=None)
        with self.assertRaises(StrictConfigError):
            c.run_config_strict(VLANIF)
        self.assertFalse(c.connected)

    def test_empty(self):
        with self.assertRaises(StrictConfigError):
            FakeClient().run_config_strict([])

    def test_stops_at_first_error(self):
        c = FakeClient(lambda cmd: "% Invalid input detected" if cmd.startswith("ip address") else "")
        with self.assertRaises(StrictConfigError):
            c.run_config_strict(VLANIF)
        self.assertNotIn("description lab", c.conn.sent)
        self.assertTrue(c.conn.closed)

    def test_echoed_description_with_fail_is_not_error(self):
        # OLT meng-echo perintah; kata "fail" di description tidak boleh dianggap error
        c = FakeClient(lambda cmd: cmd)
        c.run_config_strict(["interface vlan 30", "description failover link invalid-test", "exit"])
        self.assertIn("description failover link invalid-test", c.conn.sent)

    def test_real_error_after_echo_still_caught(self):
        c = FakeClient(lambda cmd: cmd + "\n%Error 20202: Invalid input" if cmd.startswith("description") else "")
        with self.assertRaises(StrictConfigError):
            c.run_config_strict(["interface vlan 30", "description lab", "exit"])


if __name__ == "__main__":
    unittest.main()
