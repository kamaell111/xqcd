import pytest
from vlanif import parse_ip_interface_brief as parse, VlanifParseError

REAL = """Interface     IP-Address      Mask            Admin Phy  Prot Description
vlan2         10.1.1.200      255.255.255.0   up    up   up   none
"""


def test_real_output():
    r = parse(REAL)
    assert r == [{"interface": "vlan2", "vlan_id": 2, "ip_address": "10.1.1.200", "mask": "255.255.255.0",
                  "prefix": 24, "admin_status": "up", "phy_status": "up", "protocol_status": "up",
                  "status": "up", "description": ""}]


def test_filters_non_vlan_and_sorts():
    out = REAL + """gei_1/3/1      unassigned      --              up    up   up   none
mng           192.168.0.1     255.255.255.0   up    up   up   none
vlan101       10.2.2.1        255.255.255.0   up    down down uplink ke BRAS
vlan15        unassigned      --              down  down down none
ZXAN#"""
    r = parse(out)
    assert [x["vlan_id"] for x in r] == [2, 15, 101]
    assert r[2]["status"] == "down" and r[2]["description"] == "uplink ke BRAS"
    assert r[1]["status"] == "admin_down" and r[1]["ip_address"] is None and r[1]["prefix"] is None


def test_crlf_and_more_prompt():
    out = (REAL.replace("\n", "\r\n") + "--More--\x08\x08\x08        \x08\x08\x08"
           + "vlan200       10.9.9.1        255.255.0.0     up    up   up   none\r\n")
    assert [x["vlan_id"] for x in parse(out)] == [2, 200]
    assert parse(out)[1]["prefix"] == 16


def test_no_vlanif_is_empty_not_error():
    assert parse("Interface     IP-Address      Mask            Admin Phy  Prot Description\n") == []


def test_bad_mask_and_ip_become_none():
    r = parse(REAL.replace("255.255.255.0", "255.0.255.0"))
    assert r[0]["mask"] == "255.0.255.0" and r[0]["prefix"] is None      # mask tak kontigu: tampil apa adanya, tanpa prefix
    r = parse(REAL.replace("10.1.1.200", "10.1.1.999"))
    assert r[0]["ip_address"] is None


@pytest.mark.parametrize("bad", [
    "%Error 20206: Unrecognized command", "", "ZXAN#", "random text\nvlan2 1 2 3 4 5",
])
def test_fail_loud_when_format_unknown(bad):
    with pytest.raises(VlanifParseError):
        parse(bad)


def test_duplicate_vlan_ignored():
    assert len(parse(REAL + REAL.split("\n")[1] + "\n")) == 1


def test_all_interfaces_for_overlap_check():
    from vlanif import parse_ip_interface_brief_all as allrows
    out = REAL + "mng           192.168.0.1     255.255.255.0   up    up   up   none\n" \
                 "gei_1/3/1     unassigned      --              up    up   up   none\nZXAN#"
    r = {x["interface"]: x for x in allrows(out)}
    assert set(r) == {"vlan2", "mng", "gei_1/3/1"} and r["mng"]["vlan_id"] is None
    assert r["mng"]["ip_address"] == "192.168.0.1"
