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
    privilege: int = 5
    role: str = "viewer"


class UserOut(BaseModel):
    id: int
    username: str
    privilege: int
    role: str
    is_active: bool
    last_login: Optional[datetime] = None

    class Config:
        from_attributes = True


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

    class Config:
        from_attributes = True


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
    pppoe_user: Optional[str] = None
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


class ONUProvisionRequest(BaseModel):
    olt_id: int
    pon_port: str
    onu_id: int
    serial_number: str
    name: str
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