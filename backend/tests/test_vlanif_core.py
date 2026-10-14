import pytest
import vlanif_core as c

CFG2 = """Building configuration...
interface vlan 2
  ip address 10.1.1.200 255.255.255.0
  description xxx
!
end
"""
BRIEF = """Interface     IP-Address      Mask            Admin Phy  Prot Description
vlan2         10.1.1.200      255.255.255.0   up    up   up   xxx
mng           192.168.0.1     255.255.255.0   up    up   up   none
gei_1/3/1     unassigned      --              up    up   up   none
"""
VSUM = "All created vlan num: 4\nDetails are following:\n    1-2,15,101\n"


def test_parse_real_config():
    st = c.parse_vlanif_config(CFG2, 2)
    assert st == {"exists": True, "mode": "static", "ip": "10.1.1.200", "mask": "255.255.255.0", "desc": "xxx", "shutdown": False}
    assert c.parse_vlanif_config(CFG2.replace("vlan 2", "vlan2"), 2)["exists"]          # tanpa spasi pun terbaca
    assert c.parse_vlanif_config(CFG2, 30) == c.empty_state()                           # interface lain: tidak ada
    assert c.parse_vlanif_config("%Error 1: not exist", 30)["exists"] is False


@pytest.mark.parametrize("extra", ["ip address 10.9.9.1 255.255.255.0 secondary", "ip ospf cost 10", "ip helper-address 1.1.1.1"])
def test_unknown_lines_fail_closed(extra):
    with pytest.raises(c.Unsupported):
        c.parse_vlanif_config(CFG2.replace("description xxx", "description xxx\n  " + extra), 2)


def test_dhcp_and_shutdown_parse():
    st = c.parse_vlanif_config("interface vlan 5\n  ip address dhcp\n  shutdown\n!\n", 5)
    assert st["mode"] == "dhcp" and st["shutdown"] is True


@pytest.mark.parametrize("kw", [
    dict(vlan=0, mode="static", ip="10.0.0.5", mask="255.255.255.0"),
    dict(vlan=30, mode="static", ip="10.0.0.0", mask="255.255.255.0"),        # network
    dict(vlan=30, mode="static", ip="10.0.0.255", mask="255.255.255.0"),      # broadcast
    dict(vlan=30, mode="static", ip="127.0.0.1", mask="255.0.0.0"),
    dict(vlan=30, mode="static", ip="224.0.0.5", mask="255.255.255.0"),
    dict(vlan=30, mode="static", ip="10.0.0.5", mask="255.0.255.0"),          # mask tak kontigu
    dict(vlan=30, mode="static", ip="10.0.0.5", mask="255.255.255.255"),      # /32
    dict(vlan=30, mode="static", ip="10.0.0.5; reload", mask="255.255.255.0"),
    dict(vlan=30, mode="static", ip="10.0.0.5", mask="255.255.255.0", desc="a" * 101),
    dict(vlan=30, mode="static", ip="10.0.0.5", mask="255.255.255.0", desc="x\nwrite"),
    dict(vlan=30, mode="static", ip="10.0.0.5", mask="255.255.255.0", desc="bahaya?"),
    dict(vlan=30, mode="dhcp"),                                                # DHCP default mati
    dict(vlan=30, mode="static", ip=None, mask=None),
    dict(vlan="30", mode="static", ip="10.0.0.5", mask="255.255.255.0"),
])
def test_validate_input_rejects(kw):
    with pytest.raises(ValueError):
        c.validate_input(**kw)


def test_validate_input_ok():
    assert c.validate_input(30, "static", "192.168.30.2", "255.255.255.0", "  REMOTE-OL1 ") == \
        {"mode": "static", "ip": "192.168.30.2", "mask": "255.255.255.0", "desc": "REMOTE-OL1"}
    assert c.validate_input(30, "dhcp", allow_dhcp=True)["mode"] == "dhcp"


def test_create_cmds_match_spec():
    new = c.validate_input(30, "static", "192.168.30.2", "255.255.255.0", "REMOTE-OL1")
    assert c.build_create_cmds(30, new) == [
        "configure terminal", "interface vlan 30", "ip address 192.168.30.2 255.255.255.0",
        "description REMOTE-OL1", "no shutdown", "exit", "exit"]


def test_edit_sends_only_changes():
    old = c.parse_vlanif_config(CFG2, 2)
    only_desc = c.validate_input(2, "static", "10.1.1.200", "255.255.255.0", "baru")
    assert c.build_edit_cmds(2, old, only_desc) == ["configure terminal", "interface vlan 2", "description baru", "exit", "exit"]
    new_ip = c.validate_input(2, "static", "10.1.1.201", "255.255.255.0", "xxx")
    assert c.build_edit_cmds(2, old, new_ip) == [
        "configure terminal", "interface vlan 2", "no ip address", "ip address 10.1.1.201 255.255.255.0", "exit", "exit"]
    with pytest.raises(ValueError):
        c.build_edit_cmds(2, old, c.validate_input(2, "static", "10.1.1.200", "255.255.255.0", "xxx"))


def test_overlap_and_lockout():
    from vlanif import parse_ip_interface_brief_all as allrows
    rows = allrows(BRIEF)
    assert c.check_overlap(30, "10.1.1.50", "255.255.255.0", rows) == ["vlan2 10.1.1.200/24"]
    assert c.check_overlap(30, "10.5.5.5", "255.255.255.0", rows) == []
    assert c.check_overlap(2, "10.1.1.201", "255.255.255.0", rows) == []            # dirinya sendiri diabaikan
    assert c.check_overlap(30, "192.168.0.50", "255.255.0.0", rows)                 # tumpang tindih dengan mng
    old = {"ip": "10.1.1.200", "mask": "255.255.255.0"}
    assert c.lockout_reasons("10.1.1.200", 2, "edit", old, None, set())             # IP = host koneksi
    assert c.lockout_reasons("10.1.1.77", 2, "edit", old, None, set())              # host di subnet interface
    assert c.lockout_reasons("172.16.0.9", 2, "edit", old, None, set()) == []
    assert c.lockout_reasons("172.16.0.9", 2, "edit", old, None, {2})               # daftar terlindungi
    assert c.lockout_reasons("olt.example.net", 2, "edit", old, None, set())        # hostname -> tak bisa memastikan
    assert c.lockout_reasons("olt.example.net", 30, "create", None, None, set()) == []


def _plan(action, vlan, raw, host="172.16.0.9", vsum=VSUM, cfg=None, **kw):
    return c.build_plan(olt_id=1, action=action, vlan=vlan, new_raw=raw, connect_host=host, brief_text=BRIEF,
                        cfg_text=cfg if cfg is not None else (CFG2 if vlan == 2 else ""), vsum_text=vsum, route_text="",
                        protected=set(), **kw)


def test_plan_create_vlan30_blocked_because_vlan_missing():
    p = _plan("create", 30, {"mode": "static", "ip": "192.168.30.2", "mask": "255.255.255.0", "description": "REMOTE-OL1"})
    assert p["ok"] is False and p["commands"] == []
    assert [x["name"] for x in p["checks"] if not x["ok"]] == ["VLAN sudah dibuat di OLT"]    # sesuai kondisi nyata (1,2,15,101)


def test_plan_create_ok_when_vlan_exists():
    vsum = "All created vlan num: 5\nDetails are following:\n    1-2,15,30,101\n"
    p = _plan("create", 30, {"mode": "static", "ip": "192.168.30.2", "mask": "255.255.255.0"}, vsum=vsum)
    assert p["ok"] and p["commands"][1] == "interface vlan 30" and p["plan_token"]


def test_plan_create_blocked_when_overlapping_or_exists():
    vsum = "All created vlan num: 5\nDetails are following:\n    1-2,15,30,101\n"
    p = _plan("create", 30, {"mode": "static", "ip": "10.1.1.50", "mask": "255.255.255.0"}, vsum=vsum)
    assert not p["ok"] and any("tumpang" in x["name"] for x in p["checks"] if not x["ok"])
    p = _plan("create", 2, {"mode": "static", "ip": "10.7.7.7", "mask": "255.255.255.0"})
    assert not p["ok"] and any(x["name"] == "VLANIF belum ada" and not x["ok"] for x in p["checks"])


def test_plan_edit_management_interface_is_blocked():
    p = _plan("edit", 2, {"mode": "static", "ip": "10.1.1.201", "mask": "255.255.255.0", "description": "xxx"},
              host="10.1.1.200", require_route_check=False)
    assert not p["ok"] and p["commands"] == []
    assert any("anti-lockout" in x["name"] and not x["ok"] for x in p["checks"])


def test_plan_edit_blocked_until_route_check_exists():
    p = _plan("edit", 2, {"mode": "static", "ip": "10.1.1.200", "mask": "255.255.255.0", "description": "baru"}, host="172.16.0.9")
    assert not p["ok"] and any("route" in x["name"] and not x["ok"] for x in p["checks"])
    p = _plan("edit", 2, {"mode": "static", "ip": "10.1.1.200", "mask": "255.255.255.0", "description": "baru"},
              host="172.16.0.9", require_route_check=False)
    assert p["ok"] and p["commands"][-3] == "description baru"


def test_plan_token_depends_on_state_and_input():
    raw = {"mode": "static", "ip": "10.1.1.200", "mask": "255.255.255.0", "description": "baru"}
    a = _plan("edit", 2, raw, require_route_check=False)["plan_token"]
    b = _plan("edit", 2, {**raw, "description": "lain"}, require_route_check=False)["plan_token"]
    d = _plan("edit", 2, raw, cfg=CFG2.replace("xxx", "yyy"), require_route_check=False)["plan_token"]
    assert len({a, b, d}) == 3


def test_verify_state():
    old = c.parse_vlanif_config(CFG2, 2)
    exp = c.expected_state("edit", old, c.validate_input(2, "static", "10.1.1.200", "255.255.255.0", "baru"))
    assert c.verify_state(exp, {**old, "desc": "baru"}) == []
    assert c.verify_state(exp, old)
    assert c.verify_state(exp, c.empty_state())
