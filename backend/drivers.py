"""Driver registry untuk multi-vendor OLT support.

Phase 1a-1: hardcoded ZTE saja. Nanti jadi dict yang bisa ditambah driver baru.
Setiap driver punya key unik (dipakai di kolom OLT.driver) dan daftar
hardware_type (dipakai di kolom OLT.hardware_type).
"""
from typing import Dict, List, Optional


# =================== DRIVER DEFINITIONS ===================
DRIVERS: Dict[str, dict] = {
    "zte_zxan": {
        "label": "ZTE ZXAN",
        "vendor": "ZTE",
        "verified": True,
        "description": "ZTE C320/C300/C620 series via Telnet",
        "defaults": {
            "protocol": "telnet",
            "port": 23,
            "snmp_port": 161,
            "snmp_version": "2c",
        },
        "hardware": {
            "zte-c320": {
                "label": "ZTE C320 (2U Modular)",
                "image": None,   # nanti: "/assets/olt/zte-c320.png"
                "defaults": {"port": 23, "snmp_port": 161},
            },
            "zte-c300": {
                "label": "ZTE C300 (1U)",
                "image": None,
                "defaults": {"port": 23, "snmp_port": 161},
            },
            "zte-c620": {
                "label": "ZTE C620 (Large Chassis)",
                "image": None,
                "defaults": {"port": 23, "snmp_port": 161},
            },
        },
    },
}


def list_drivers() -> List[dict]:
    """Return semua driver + hardware types untuk UI dropdown."""
    out = []
    for key, d in DRIVERS.items():
        hw_list = [
            {"key": hw_key, "label": hw["label"], "image": hw.get("image")}
            for hw_key, hw in d["hardware"].items()
        ]
        out.append({
            "key": key,
            "label": d["label"],
            "vendor": d["vendor"],
            "verified": d["verified"],
            "description": d.get("description", ""),
            "defaults": d.get("defaults", {}),
            "hardware": hw_list,
        })
    return out


def get_driver(driver_key: str) -> Optional[dict]:
    """Return driver spec atau None."""
    return DRIVERS.get(driver_key)


def get_hardware(driver_key: str, hardware_key: str) -> Optional[dict]:
    """Return hardware spec atau None."""
    d = DRIVERS.get(driver_key)
    if not d:
        return None
    return d["hardware"].get(hardware_key)


def is_valid(driver_key: str, hardware_key: str) -> bool:
    """Cek apakah (driver, hardware) valid."""
    d = DRIVERS.get(driver_key)
    if not d:
        return False
    return hardware_key in d["hardware"]
