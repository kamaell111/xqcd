from pydantic import BaseModel
from typing import Optional, List
from datetime import datetime


class Token(BaseModel):
    access_token: str
    token_type: str = "bearer"
    user: dict


class LoginRequest(BaseModel):
    username: str
    password: str


class UserCreate(BaseModel):
    username: str
    password: str
    full_name: Optional[str] = None
    role: str = "viewer"
    # Phase B2g: ownership (hanya dihonor kalau requester Multivers)
    owner_user_id: Optional[int] = None
    is_super_admin: int = 0


class UserOut(BaseModel):
    id: int
    username: str
    full_name: Optional[str] = None
    privilege: int
    role: str
    is_active: bool
    # Phase B2g
    is_super_admin: int = 0
    owner_user_id: Optional[int] = None
    last_login: Optional[datetime] = None
    created_at: Optional[datetime] = None
    updated_at: Optional[datetime] = None

    class Config:
        from_attributes = True


class UserUpdate(BaseModel):
    full_name: Optional[str] = None
    role: Optional[str] = None
    is_active: Optional[bool] = None
    # Phase B2g (hanya dihonor kalau requester Multivers)
    owner_user_id: Optional[int] = None
    is_super_admin: Optional[int] = None


class PasswordReset(BaseModel):
    new_password: str


class PasswordSelfChange(BaseModel):
    old_password: str
    new_password: str


class OLTCreate(BaseModel):
    hostname: str = "ZXAN"
    ip_address: str
    protocol: str = "telnet"
    port: int = 23
    username: str
    password: str
    enable_password: Optional[str] = None
    snmp_version: str = "2c"
    snmp_community_ro: str = "public"
    snmp_community_rw: str = "private"
    location: Optional[str] = None
    # Phase B3: hanya dihonor kalau requester Multivers
    owner_user_id: Optional[int] = None


class OLTOut(BaseModel):
    id: int
    hostname: str
    ip_address: str
    model: str
    firmware: str
    status: str
    cpu_usage: Optional[float] = None
    memory_usage: Optional[float] = None
    uptime_seconds: Optional[int] = None
    temperature: Optional[float] = None
    location: Optional[str] = None
    last_polled: Optional[datetime] = None
    # 1a-1 multi-vendor
    driver: Optional[str] = None
    hardware_type: Optional[str] = None
    enabled: Optional[int] = 1
    # Phase B3: ownership
    owner_user_id: Optional[int] = None

    class Config:
        from_attributes = True


class OLTUpdate(BaseModel):
    """Update OLT. Semua field opsional. Password kosong = tidak diubah."""
    hostname: Optional[str] = None
    ip_address: Optional[str] = None
    protocol: Optional[str] = None
    port: Optional[int] = None
    username: Optional[str] = None
    password: Optional[str] = None
    enable_password: Optional[str] = None
    snmp_version: Optional[str] = None
    snmp_community_ro: Optional[str] = None
    snmp_community_rw: Optional[str] = None
    snmp_port: Optional[int] = None
    location: Optional[str] = None
    driver: Optional[str] = None
    hardware_type: Optional[str] = None
    enabled: Optional[int] = None
    owner_user_id: Optional[int] = None


class OLTTestRequest(BaseModel):
    """Body untuk POST /olts/test-connection (tanpa simpan DB)."""
    ip_address: str
    port: int = 23
    username: str
    password: str
    enable_password: Optional[str] = None
    protocol: str = "telnet"


class PONPortOut(BaseModel):
    id: int
    port_no: str
    status: str
    admin_state: str
    optical_tx: Optional[float] = None
    optical_rx: Optional[float] = None
    onu_count: int

    class Config:
        from_attributes = True


class ONUOut(BaseModel):
    id: int
    pon_port: str
    onu_id: int
    interface_name: Optional[str] = None
    serial_number: Optional[str] = None
    name: Optional[str] = None
    type: Optional[str] = None
    status: str
    optical_tx: Optional[float] = None
    optical_rx: Optional[float] = None
    distance: Optional[int] = None
    user_vlan: Optional[int] = None
    vlan: Optional[int] = None
    tcont: Optional[str] = None
    gemport: Optional[int] = None
    service_port: Optional[int] = None
    description: Optional[str] = None
    pppoe_user: Optional[str] = None
    pppoe_nat: Optional[bool] = False
    pppoe_status: Optional[str] = "unknown"
    pppoe_online_duration: Optional[int] = 0
    internet_checked_at: Optional[datetime] = None
    # ⭐ Multi-vendor
    vendor: Optional[str] = "ZTE"
    provisioning_mode: Optional[str] = "routed"
    vendor_source: Optional[str] = "sn_prefix"
    bridge_configured: Optional[bool] = False
    last_online: Optional[datetime] = None
    last_offline: Optional[datetime] = None

    class Config:
        from_attributes = True


class ONUWithOLTOut(ONUOut):
    """ONU + info OLT untuk tabel lintas-OLT."""
    olt_id: int
    olt_hostname: Optional[str] = None


class ONUProvisionRequest(BaseModel):
    olt_id: int
    pon_port: str
    onu_id: int
    serial_number: str
    name: str
    description: Optional[str] = None    # Site Location / ODP
    onu_type: str = "F609"
    tcont_profile: str = "1G"
    tcont_name: str = "PON1"
    gemport_id: int = 1
    traffic_limit: str = "1G"
    service_port: int = 1
    vport: int = 1
    user_vlan: int = 15
    vlan: int = 15
    pppoe_user: Optional[str] = None
    pppoe_password: Optional[str] = None
    enable_nat: bool = True
    # ⭐ Multi-vendor
    vendor: Optional[str] = None              # None = auto-detect
    provisioning_mode: Optional[str] = None   # None = auto based on vendor


class VLANCreate(BaseModel):
    vlan_id: int
    name: Optional[str] = None
    description: Optional[str] = None


class AlertOut(BaseModel):
    id: int
    olt_id: Optional[int] = None
    severity: str
    category: str
    title: str
    message: str
    source: Optional[str] = None
    acknowledged: bool
    resolved: bool
    created_at: datetime

    class Config:
        from_attributes = True


class ConfigPreviewRequest(BaseModel):
    commands: List[str]


class ConfigApplyRequest(BaseModel):
    commands: List[str]
    comment: Optional[str] = None


class PortToggleRequest(BaseModel):
    port_type: str      # "gpon" | "uplink"
    port_name: str      # "1/1/1" atau "gei_1/3/1"
    action: str         # "enable" | "disable"

class PortVLANEditRequest(BaseModel):
    port_name: str              # "gei_1/3/3"
    mode: str                   # "access" | "trunk" | "hybrid"
    native_vlan: int = 1        # PVID
    tag_vlans: str = ""         # "1-2,15,101"
