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


# =================== MULTI-VENDOR CONFIG GENERATOR ===================
from vendor_detect import resolve_vendor_and_mode, get_capability


class ONUConfigGenerator:
    """Base class untuk generator config ONU multi-vendor."""

    def __init__(self, req, vendor: str, mode: str):
        self.req = req
        self.vendor = vendor
        self.mode = mode
        self.iface = f"gpon-onu_{req.pon_port}:{req.onu_id}"
        self.olt_iface = f"gpon-olt_{req.pon_port}"

    # --- COMMAND YANG SAMA UNTUK SEMUA VENDOR ---
    def _register_onu(self) -> List[str]:
        return [
            "configure terminal",
            f"interface {self.olt_iface}",
            f"onu {self.req.onu_id} type {self.req.onu_type} sn {self.req.serial_number}",
            "exit",
        ]

    def _base_service_l12(self) -> List[str]:
        """Layer 1/2 dasar — tcont, gemport, service-port, bridge VLAN."""
        r = self.req
        cmds = [
            f"interface {self.iface}",
            f"name {r.name}",
        ]
        desc = getattr(r, "description", None)
        if desc:
            cmds.append(f"description {desc}")
        cmds += [
            "sn-bind enable sn",
            f"tcont 1 name {r.tcont_name} profile {r.tcont_profile}",
            f"gemport {r.gemport_id} tcont 1",
            f"gemport {r.gemport_id} traffic-limit downstream {r.traffic_limit}",
            f"service-port {r.service_port} vport {r.vport} user-vlan {r.user_vlan} vlan {r.vlan}",
            "exit",
        ]
        return cmds

    def _bridge_pon_mng(self) -> List[str]:
        """pon-onu-mng bagian bridge — service + VLAN tag ke port LAN (STANDAR)."""
        r = self.req
        cmds = [
            f"pon-onu-mng {self.iface}",
            f"service {r.tcont_name} gemport {r.gemport_id} iphost 1 vlan {r.vlan}",
            f"vlan port eth_0/1 mode tag vlan {r.vlan}",
            f"vlan port eth_0/2 mode tag vlan {r.vlan}",
        ]
        return cmds

    # --- COMMAND PROPRIETARY ZTE (HANYA UNTUK ZTE) ---
    def _zte_routed_commands(self) -> List[str]:
        """Command proprietary ZTE untuk PPPoE routed mode."""
        r = self.req
        if not r.pppoe_user:
            return []
        nat = "enable" if r.enable_nat else "disable"
        return [
            # STEP 1: Delete instance PPPoE lama (force tear-down)
            "no pppoe 1",
            # STEP 2: Marker delay 3 detik
            "__DELAY_3S__",
            # STEP 3: Push credential baru
            f"pppoe 1 nat {nat} user {r.pppoe_user} password {r.pppoe_password or 'zte'}",
            # Command proprietary tambahan
            "firewall enable level low anti-hack disable",
            "security-mgmt 1 state enable mode forward protocol web",
            "wan 1 service internet host 1",
        ]

    # --- GENERATE (template method) ---
    def generate(self) -> List[str]:
        cmds = self._register_onu()
        cmds += self._base_service_l12()
        cmds += self._bridge_pon_mng()

        # Cuma ZTE yang bisa routed mode (proprietary)
        cap = get_capability(self.vendor)
        if self.mode == "routed" and cap["supports_routed"]:
            cmds += self._zte_routed_commands()
        # else: bridge mode → tidak ada command PPPoE

        cmds += ["exit", "end"]
        return cmds


class ZTEConfigGenerator(ONUConfigGenerator):
    """Generator untuk ONU ZTE — support routed + bridge."""
    pass


class HuaweiConfigGenerator(ONUConfigGenerator):
    """Generator untuk ONU Huawei — bridge only."""
    def _zte_routed_commands(self) -> List[str]:
        # Override: Huawei tidak support command proprietary ZTE
        return []


class FiberHomeConfigGenerator(ONUConfigGenerator):
    """Generator untuk ONU FiberHome — bridge only."""
    def _zte_routed_commands(self) -> List[str]:
        return []


class UnknownConfigGenerator(ONUConfigGenerator):
    """Fallback untuk vendor tidak dikenal — bridge only."""
    def _zte_routed_commands(self) -> List[str]:
        return []


GENERATOR_MAP = {
    "ZTE": ZTEConfigGenerator,
    "Huawei": HuaweiConfigGenerator,
    "FiberHome": FiberHomeConfigGenerator,
    "Unknown": UnknownConfigGenerator,
}


def generate_onu_config(req) -> List[str]:
    """Fungsi utama — auto-detect vendor + generate config sesuai capability."""
    # Resolve vendor & mode (dari SN atau manual)
    resolved = resolve_vendor_and_mode(
        sn=req.serial_number,
        requested_vendor=getattr(req, "vendor", None),
        requested_mode=getattr(req, "provisioning_mode", None),
    )

    vendor = resolved["vendor"]
    mode = resolved["provisioning_mode"]

    # Log warning kalau ada
    if resolved["warning"]:
        print(f"[CONFIG-GEN] {resolved['warning']}")

    GeneratorClass = GENERATOR_MAP.get(vendor, UnknownConfigGenerator)
    generator = GeneratorClass(req, vendor, mode)
    return generator.generate()


def get_vendor_info(req) -> dict:
    """Return info vendor untuk dipakai di endpoint (audit/log)."""
    return resolve_vendor_and_mode(
        sn=req.serial_number,
        requested_vendor=getattr(req, "vendor", None),
        requested_mode=getattr(req, "provisioning_mode", None),
    )