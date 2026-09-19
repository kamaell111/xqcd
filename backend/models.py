from sqlalchemy import (
    Column, Integer, String, Boolean, DateTime, Float,
    ForeignKey, Text, JSON,
)
from sqlalchemy.orm import relationship
from datetime import datetime
from database import Base


class User(Base):
    __tablename__ = "users"
    id = Column(Integer, primary_key=True)
    username = Column(String(64), unique=True, index=True, nullable=False)
    password_hash = Column(String(255), nullable=False)
    privilege = Column(Integer, default=5)
    role = Column(String(32), default="viewer")
    is_active = Column(Boolean, default=True)
    created_at = Column(DateTime, default=datetime.utcnow)
    last_login = Column(DateTime, nullable=True)


class OLT(Base):
    __tablename__ = "olts"
    id = Column(Integer, primary_key=True)
    hostname = Column(String(64), default="ZXAN")
    ip_address = Column(String(64), unique=True, nullable=False)
    protocol = Column(String(8), default="telnet")
    port = Column(Integer, default=23)
    username = Column(String(64))
    password = Column(String(255))
    enable_password = Column(String(255), nullable=True)
    snmp_version = Column(String(8), default="2c")
    snmp_community_ro = Column(String(64), default="public")
    snmp_community_rw = Column(String(64), default="private")
    snmp_v3_user = Column(String(64), nullable=True)
    snmp_v3_auth = Column(String(64), nullable=True)
    snmp_v3_priv = Column(String(64), nullable=True)
    snmp_port = Column(Integer, default=161)
    model = Column(String(32), default="C320")
    firmware = Column(String(32), default="V4.8.35")
    location = Column(String(255), nullable=True)
    status = Column(String(16), default="unknown")
    cpu_usage = Column(Float, nullable=True)
    memory_usage = Column(Float, nullable=True)
    uptime_seconds = Column(Integer, nullable=True)
    temperature = Column(Float, nullable=True)
    last_polled = Column(DateTime, nullable=True)
    created_at = Column(DateTime, default=datetime.utcnow)

    pons = relationship("PONPort", back_populates="olt", cascade="all, delete-orphan")
    onus = relationship("ONU", back_populates="olt", cascade="all, delete-orphan")
    interfaces = relationship("Interface", back_populates="olt", cascade="all, delete-orphan")
    vlans = relationship("VLAN", back_populates="olt", cascade="all, delete-orphan")
    alerts = relationship("Alert", back_populates="olt", cascade="all, delete-orphan")
    config_versions = relationship("ConfigVersion", back_populates="olt", cascade="all, delete-orphan")


class PONPort(Base):
    __tablename__ = "pon_ports"
    id = Column(Integer, primary_key=True)
    olt_id = Column(Integer, ForeignKey("olts.id"), nullable=False)
    port_no = Column(String(16), nullable=False)
    status = Column(String(16), default="down")
    admin_state = Column(String(16), default="shutdown")
    linktrap = Column(Boolean, default=False)
    optical_tx = Column(Float, nullable=True)
    optical_rx = Column(Float, nullable=True)
    onu_count = Column(Integer, default=0)
    last_updated = Column(DateTime, default=datetime.utcnow)
    olt = relationship("OLT", back_populates="pons")


class ONU(Base):
    __tablename__ = "onus"
    id = Column(Integer, primary_key=True)
    olt_id = Column(Integer, ForeignKey("olts.id"), nullable=False)
    pon_port = Column(String(16), nullable=False)
    onu_id = Column(Integer, nullable=False)
    interface_name = Column(String(64))
    serial_number = Column(String(32), index=True)
    name = Column(String(64))
    type = Column(String(32), default="F609")
    status = Column(String(16), default="offline")
    optical_tx = Column(Float, nullable=True)
    optical_rx = Column(Float, nullable=True)
    distance = Column(Integer, nullable=True)
    tcont = Column(String(32), nullable=True)
    gemport = Column(Integer, nullable=True)
    service_port = Column(Integer, nullable=True)
    user_vlan = Column(Integer, nullable=True)
    vlan = Column(Integer, nullable=True)
    pppoe_user = Column(String(64), nullable=True)
    pppoe_nat = Column(Boolean, default=False)
    # ⭐ Multi-vendor support
    vendor = Column(String(16), default="ZTE")             # ZTE|Huawei|FiberHome|Unknown
    provisioning_mode = Column(String(16), default="routed")   # routed|bridge
    vendor_source = Column(String(16), default="sn_prefix")    # sn_prefix|manual|conflict
    bridge_configured = Column(Boolean, default=False)     # ⭐ manual confirm untuk bridge mode
    # Status internet real (dari remote-onu pppoe)
    pppoe_status = Column(String(16), default="unknown")   # connected|disconnected|connecting|unknown
    pppoe_online_duration = Column(Integer, default=0)     # detik
    internet_checked_at = Column(DateTime, nullable=True)
    last_online = Column(DateTime, nullable=True)
    last_offline = Column(DateTime, nullable=True)
    updated_at = Column(DateTime, default=datetime.utcnow)
    olt = relationship("OLT", back_populates="onus")


class Interface(Base):
    __tablename__ = "interfaces"
    id = Column(Integer, primary_key=True)
    olt_id = Column(Integer, ForeignKey("olts.id"), nullable=False)
    name = Column(String(32), nullable=False)
    type = Column(String(16))
    status = Column(String(16), default="down")
    admin_state = Column(String(16), default="shutdown")
    speed = Column(Integer, nullable=True)
    duplex = Column(String(8), nullable=True)
    negotiation = Column(String(8), nullable=True)
    flowcontrol = Column(Boolean, default=False)
    linktrap = Column(Boolean, default=False)
    switchport_mode = Column(String(16), nullable=True)
    vlans = Column(String(255), nullable=True)
    hybrid_attribute = Column(String(16), nullable=True)
    port_protect = Column(Boolean, default=False)
    uplink_isolate = Column(Boolean, default=False)
    rx_bytes = Column(Integer, default=0)
    tx_bytes = Column(Integer, default=0)
    rx_errors = Column(Integer, default=0)
    tx_errors = Column(Integer, default=0)
    last_updated = Column(DateTime, default=datetime.utcnow)
    olt = relationship("OLT", back_populates="interfaces")


class VLAN(Base):
    __tablename__ = "vlans"
    id = Column(Integer, primary_key=True)
    olt_id = Column(Integer, ForeignKey("olts.id"), nullable=False)
    vlan_id = Column(Integer, nullable=False)
    name = Column(String(64), nullable=True)
    description = Column(String(255), nullable=True)
    olt = relationship("OLT", back_populates="vlans")


class Alert(Base):
    __tablename__ = "alerts"
    id = Column(Integer, primary_key=True)
    olt_id = Column(Integer, ForeignKey("olts.id"), nullable=True)
    severity = Column(String(16), default="warning")
    category = Column(String(32))
    title = Column(String(255))
    message = Column(Text)
    source = Column(String(64))
    acknowledged = Column(Boolean, default=False)
    ack_by = Column(String(64), nullable=True)
    ack_at = Column(DateTime, nullable=True)
    resolved = Column(Boolean, default=False)
    resolved_at = Column(DateTime, nullable=True)
    created_at = Column(DateTime, default=datetime.utcnow, index=True)
    olt = relationship("OLT", back_populates="alerts")


class ConfigVersion(Base):
    __tablename__ = "config_versions"
    id = Column(Integer, primary_key=True)
    olt_id = Column(Integer, ForeignKey("olts.id"), nullable=False)
    version = Column(String(64))
    content = Column(Text)
    author = Column(String(64))
    comment = Column(String(255), nullable=True)
    created_at = Column(DateTime, default=datetime.utcnow, index=True)
    olt = relationship("OLT", back_populates="config_versions")


class AuditLog(Base):
    __tablename__ = "audit_logs"
    id = Column(Integer, primary_key=True)
    username = Column(String(64))
    action = Column(String(64))
    target = Column(String(128))
    detail = Column(Text, nullable=True)
    result = Column(String(16), default="success")
    ip_address = Column(String(64), nullable=True)
    created_at = Column(DateTime, default=datetime.utcnow, index=True)


class MetricHistory(Base):
    __tablename__ = "metric_history"
    id = Column(Integer, primary_key=True)
    olt_id = Column(Integer, ForeignKey("olts.id"), nullable=False, index=True)
    metric = Column(String(16), nullable=False, index=True)   # "cpu" | "memory"
    value = Column(Float, nullable=False)
    ts = Column(DateTime, default=datetime.utcnow, index=True)