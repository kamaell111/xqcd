"""Parser untuk file `show running-config` ZTE ZXAN."""
import re
from typing import Dict, List


def parse_running_config(text: str) -> Dict:
    result = {
        "hostname": None, "version": None, "config_version": None,
        "cards": [], "subcards": [], "vlans": [], "interfaces": [],
        "pon_ports": [], "onus": [], "users": [], "snmp": {}, "routes": [],
        "profiles": {"tcont": [], "traffic": []}, "raw": text,
    }

    for line in text.splitlines():
        line = line.strip()
        m = re.match(r"^hostname\s+(\S+)", line)
        if m: result["hostname"] = m.group(1)
        m = re.match(r"^version\s+(\S+)", line)
        if m: result["version"] = m.group(1)
        m = re.match(r"^config-version\s+(\S+)", line)
        if m: result["config_version"] = m.group(1)
        m = re.match(r"^add-card rackno (\d+) shelfno (\d+) slotno (\d+) (\S+)", line)
        if m:
            result["cards"].append({"rack": int(m.group(1)), "shelf": int(m.group(2)),
                                    "slot": int(m.group(3)), "type": m.group(4)})
        m = re.match(r"^add-subcard rackno (\d+) shelfno (\d+) slotno (\d+) subcardno (\d+) (\S+)", line)
        if m:
            result["subcards"].append({"rack": int(m.group(1)), "shelf": int(m.group(2)),
                                       "slot": int(m.group(3)), "subcard": int(m.group(4)),
                                       "type": m.group(5)})
        m = re.match(r"^onu-type\s+(\S+)\s+(\S+)", line)
        if m:
            result.setdefault("onu_types", []).append({"type": m.group(1), "mode": m.group(2)})
        m = re.match(r"^profile tcont (\S+) type (\d+) maximum (\d+)", line)
        if m:
            result["profiles"]["tcont"].append({"name": m.group(1), "type": int(m.group(2)),
                                                "maximum": int(m.group(3))})
        m = re.match(r"^profile traffic (\S+) sir (\d+) pir (\d+)", line)
        if m:
            result["profiles"]["traffic"].append({"name": m.group(1), "sir": int(m.group(2)),
                                                  "pir": int(m.group(3))})
        m = re.match(r"^username (\S+) password\s+7\s+(\S+)(?:\s+privilege (\d+))?", line)
        if m:
            result["users"].append({"username": m.group(1), "hash": m.group(2),
                                    "privilege": int(m.group(3)) if m.group(3) else 5})
        m = re.match(r"^snmp-server community (\S+) view (\S+) (\S+)", line)
        if m:
            result["snmp"].setdefault("communities", []).append(
                {"name": m.group(1), "view": m.group(2), "access": m.group(3)})
        m = re.match(r"^snmp-server host (\S+)\s+version (\S+) (\S+)", line)
        if m:
            result["snmp"].setdefault("hosts", []).append(
                {"host": m.group(1), "version": m.group(2), "community": m.group(3)})
        m = re.match(r"^ip route (\S+) (\S+) (\S+)", line)
        if m:
            result["routes"].append({"dest": m.group(1), "mask": m.group(2), "next_hop": m.group(3)})

    return result


def generate_onu_config(req) -> List[str]:
    iface = f"gpon-onu_{req.pon_port}:{req.onu_id}"
    olt_iface = f"gpon-olt_{req.pon_port}"
    commands = [
        "configure terminal",
        f"interface {olt_iface}",
        f"onu {req.onu_id} type {req.onu_type} sn {req.serial_number}",
        "exit",
        f"interface {iface}",
        f"name {req.name}",
        "sn-bind enable sn",
        f"tcont 1 name {req.tcont_name} profile {req.tcont_profile}",
        f"gemport {req.gemport_id} tcont 1",
        f"gemport {req.gemport_id} traffic-limit downstream {req.traffic_limit}",
        f"service-port {req.service_port} vport {req.vport} user-vlan {req.user_vlan} vlan {req.vlan}",
        "exit",
        f"pon-onu-mng {iface}",
        f"service {req.tcont_name} gemport {req.gemport_id} iphost 1 vlan {req.vlan}",
    ]
    if req.pppoe_user:
        nat = "enable" if req.enable_nat else "disable"
        commands.append(f"pppoe 1 nat {nat} user {req.pppoe_user} password {req.pppoe_password or 'zte'}")
    commands += [
        "firewall enable level low anti-hack disable",
        "security-mgmt 1 state enable mode forward protocol web",
        "wan 1 service internet host 1",
        "exit",
        "end",
    ]
    return commands