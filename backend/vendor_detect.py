"""Deteksi vendor ONU dari SN prefix (4 karakter pertama GPON SN).

SN GPON 8-byte = 4 byte vendor ID (ASCII) + 4 byte serial.
Vendor ID sesuai ITU-T G.984.3:
- ZTEG = ZTE
- YYKC = ZTE OEM/rebrand
- HWTC = Huawei
- FHTT = FiberHome
- ALCL = Alcatel-Lucent
"""
import re


# Mapping SN prefix → vendor
VENDOR_PREFIX_MAP = {
    "ZTEG": "ZTE",
    "YYKC": "ZTE",       # OEM ZTE
    "HWTC": "Huawei",
    "FHTT": "FiberHome",
    "ALCL": "Alcatel-Lucent",
    "ZTEC": "ZTE",
    "ZTEZ": "ZTE",
}

# Capability per vendor (untuk validasi & UI)
VENDOR_CAPABILITY = {
    "ZTE": {
        "supports_routed": True,
        "supports_bridge": True,
        "proprietary_commands_ok": True,
        "default_mode": "routed",
    },
    "Huawei": {
        "supports_routed": False,        # OMCI proprietary ZTE ditolak
        "supports_bridge": True,
        "proprietary_commands_ok": False,
        "default_mode": "bridge",
    },
    "FiberHome": {
        "supports_routed": False,
        "supports_bridge": True,
        "proprietary_commands_ok": False,
        "default_mode": "bridge",
    },
    "Unknown": {
        "supports_routed": False,
        "supports_bridge": True,
        "proprietary_commands_ok": False,
        "default_mode": "bridge",
    },
}


def detect_vendor(serial_number: str) -> str:
    """Return vendor dari SN prefix. Default 'Unknown' kalau tidak dikenali."""
    if not serial_number:
        return "Unknown"
    prefix = serial_number.strip().upper()[:4]
    return VENDOR_PREFIX_MAP.get(prefix, "Unknown")


def get_capability(vendor: str) -> dict:
    """Return capability dict untuk vendor."""
    return VENDOR_CAPABILITY.get(vendor, VENDOR_CAPABILITY["Unknown"])


def resolve_vendor_and_mode(sn: str, requested_vendor: str = None,
                             requested_mode: str = None) -> dict:
    """Tentukan vendor + provisioning_mode final.

    Args:
        sn: Serial number ONU
        requested_vendor: vendor dari user (None = auto-detect)
        requested_mode: mode dari user (None = pakai default vendor)

    Returns:
        {"vendor": str, "provisioning_mode": str, "vendor_source": str, "warning": str|None}
    """
    auto_vendor = detect_vendor(sn)
    vendor_source = "sn_prefix"
    warning = None

    # Kalau user pilih vendor manual dan beda dengan auto-detect
    if requested_vendor and requested_vendor != auto_vendor and auto_vendor != "Unknown":
        vendor = requested_vendor
        vendor_source = "manual"
        warning = (
            f"⚠️ SN prefix menunjukkan {auto_vendor}, tapi Anda pilih {requested_vendor}. "
            f"Pastikan ini disengaja."
        )
    elif requested_vendor:
        vendor = requested_vendor
        vendor_source = "manual"
    else:
        vendor = auto_vendor

    # Tentukan mode
    cap = get_capability(vendor)
    if requested_mode:
        mode = requested_mode
        # Validasi mode vs capability
        if mode == "routed" and not cap["supports_routed"]:
            mode = cap["default_mode"]
            warning = (
                f"⚠️ Mode 'routed' tidak didukung {vendor}. "
                f"Otomatis pakai mode '{mode}'."
            )
    else:
        mode = cap["default_mode"]

    return {
        "vendor": vendor,
        "provisioning_mode": mode,
        "vendor_source": vendor_source,
        "warning": warning,
    }
