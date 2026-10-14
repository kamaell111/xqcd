import pytest
import vlanif_core as core
from vlanif_flow import apply_change
from olt_strict import OLTCommandError

MASK = "255.255.255.0"


class FakeOLT:
    """OLT tiruan: menjalankan command VLANIF yang kita pakai & merender output seperti OLT asli."""
    def __init__(self):
        self.vlans = {1, 2, 15, 30, 101}
        self.ifs = {2: dict(mode="static", ip="10.1.1.200", mask=MASK, desc="xxx", shutdown=False)}
        self.fail_on = None        # substring command -> %Error
        self.drop_after = None     # substring command -> setelah ini jalur ke OLT putus
        self.ignore_desc = False   # OLT 'diam-diam' mengabaikan description
        self.link_up = True
        self.log = []

    def vsum(self):
        v = sorted(self.vlans)
        return f"All created vlan num: {len(v)}\nDetails are following:\n    {','.join(map(str, v))}\n"

    def cfg(self, n):
        i = self.ifs.get(n)
        if not i:
            return "%Error 1: interface not exist\n"
        s = f"interface vlan {n}\n"
        if i["mode"] == "static": s += f"  ip address {i['ip']} {i['mask']}\n"
        if i["mode"] == "dhcp": s += "  ip address dhcp\n"
        if i["desc"]: s += f"  description {i['desc']}\n"
        if i["shutdown"]: s += "  shutdown\n"
        return s + "!\n"

    def brief(self):
        s = "Interface     IP-Address      Mask            Admin Phy  Prot Description\n"
        for n, i in sorted(self.ifs.items()):
            s += f"vlan{n:<9} {i['ip'] or 'unassigned':<15} {i['mask'] or '--':<15} {'down' if i['shutdown'] else 'up':<5} up   up   {i['desc'] or 'none'}\n"
        return s


class FakeClient:
    def __init__(self, olt, survives=False):
        self.olt, self.survives, self.cur = olt, survives, None

    def _alive(self):
        if not self.olt.link_up and not self.survives:
            raise ConnectionError("koneksi terputus")

    def run_commands(self, cmds):
        self._alive()
        cmd = cmds[0]
        if cmd == "show ip interface brief": return self.olt.brief()
        if cmd == "show vlan summary": return self.olt.vsum()
        if cmd.startswith("show running-config interface vlan "): return self.olt.cfg(int(cmd.split()[-1]))
        raise AssertionError(cmd)

    def run_config_strict(self, cmds):
        self._alive()
        o, done = self.olt, []
        for cmd in cmds:
            if o.fail_on and o.fail_on in cmd:
                raise OLTCommandError(cmd, "%Error 20206: Unrecognized command", done)
            o.log.append(cmd)
            if cmd.startswith("interface vlan "):
                n = int(cmd.split()[-1]); self.cur = n
                assert n in o.vlans, "VLAN belum dibuat"
                o.ifs.setdefault(n, dict(mode="none", ip=None, mask=None, desc="", shutdown=True))
            elif cmd.startswith("no interface vlan "): o.ifs.pop(int(cmd.split()[-1]), None)
            elif cmd == "no ip address": o.ifs[self.cur].update(mode="none", ip=None, mask=None)
            elif cmd == "ip address dhcp": o.ifs[self.cur].update(mode="dhcp", ip=None, mask=None)
            elif cmd.startswith("ip address "):
                _, _, ip, mask = cmd.split(); o.ifs[self.cur].update(mode="static", ip=ip, mask=mask)
            elif cmd.startswith("description "):
                if not o.ignore_desc: o.ifs[self.cur]["desc"] = cmd[len("description "):]
            elif cmd == "no description": o.ifs[self.cur]["desc"] = ""
            elif cmd == "no shutdown": o.ifs[self.cur]["shutdown"] = False
            elif cmd == "shutdown": o.ifs[self.cur]["shutdown"] = True
            if o.drop_after and o.drop_after in cmd: o.link_up = False
            done.append(cmd)
        return ""


def plan_for(olt, action, vlan, raw, host="172.16.0.9"):
    return core.build_plan(olt_id=1, action=action, vlan=vlan, new_raw=raw, connect_host=host,
                           brief_text=olt.brief(), cfg_text=olt.cfg(vlan), vsum_text=olt.vsum(), route_text="",
                           protected=set(), require_route_check=False)


def run(olt, action, vlan, raw, survives=False, host="172.16.0.9"):
    p = plan_for(olt, action, vlan, raw, host)
    assert p["ok"], p["checks"]
    def make():
        if not olt.link_up: raise ConnectionError("reconnect gagal")
        return FakeClient(olt)
    return apply_change(action=action, vlan=vlan, plan=p, client_a=FakeClient(olt, survives), make_client=make,
                        sleep=lambda s: None)


NEW30 = {"mode": "static", "ip": "192.168.30.2", "mask": MASK, "description": "REMOTE-OL1"}


def test_create_ok():
    o = FakeOLT(); r = run(o, "create", 30, NEW30)
    assert r["status"] == "applied" and o.ifs[30]["ip"] == "192.168.30.2" and o.ifs[30]["shutdown"] is False


def test_create_error_midway_removes_half_created_interface():
    o = FakeOLT(); o.fail_on = "description"
    r = run(o, "create", 30, NEW30)
    assert r["status"] == "rolled_back" and 30 not in o.ifs
    assert 2 in o.ifs                                   # interface lain tak tersentuh


def test_create_error_on_first_command_nothing_to_undo():
    o = FakeOLT(); o.fail_on = "interface vlan 30"
    r = run(o, "create", 30, NEW30)
    assert r["status"] == "rolled_back" and 30 not in o.ifs


def test_edit_ok_and_untouched_ip_when_only_description():
    o = FakeOLT(); run(o, "create", 30, NEW30); o.log.clear()
    r = run(o, "edit", 30, {**NEW30, "description": "baru"})
    assert r["status"] == "applied" and "no ip address" not in o.log and o.ifs[30]["desc"] == "baru"


def test_edit_error_after_no_ip_address_restores_old_ip():
    o = FakeOLT(); run(o, "create", 30, NEW30)
    o.fail_on = "ip address 192.168.31.2"               # 'no ip address' sukses, set IP baru ditolak
    r = run(o, "edit", 30, {**NEW30, "ip": "192.168.31.2"})
    o.fail_on = None
    assert r["status"] == "rolled_back"
    assert o.ifs[30]["ip"] == "192.168.30.2" and o.ifs[30]["mode"] == "static"


def test_lockout_session_survives_rollback_via_old_session():
    o = FakeOLT(); run(o, "create", 30, NEW30)
    o.drop_after = "ip address 192.168.31.2"            # jalur baru putus, sesi lama masih hidup
    r = run(o, "edit", 30, {**NEW30, "ip": "192.168.31.2"}, survives=True)
    assert r["status"] == "rolled_back" and "sesi lama" in r["detail"] and o.ifs[30]["ip"] == "192.168.30.2"


def test_lockout_everything_dead_reports_manual_commands():
    o = FakeOLT(); run(o, "create", 30, NEW30)
    o.drop_after = "ip address 192.168.31.2"
    r = run(o, "edit", 30, {**NEW30, "ip": "192.168.31.2"}, survives=False)
    assert r["status"] == "rollback_failed" and r["manual_cmds"]
    assert "ip address 192.168.30.2 255.255.255.0" in r["manual_cmds"]    # perintah pemulihan manual untuk operator


def test_silent_ignore_detected_by_verification_and_rolled_back():
    o = FakeOLT(); run(o, "create", 30, NEW30)
    o.ignore_desc = True
    r = run(o, "edit", 30, {**NEW30, "ip": "192.168.32.2", "description": "baru"})
    o.ignore_desc = False
    assert r["status"] == "rolled_back" and o.ifs[30]["ip"] == "192.168.30.2"


def test_management_interface_never_reaches_apply():
    o = FakeOLT()
    p = plan_for(o, "edit", 2, {"mode": "static", "ip": "10.1.1.201", "mask": MASK, "description": "xxx"}, host="10.1.1.200")
    assert not p["ok"] and p["commands"] == []


def test_never_writes_unsaved_marker():
    o = FakeOLT(); run(o, "create", 30, NEW30)
    assert "write" not in o.log
