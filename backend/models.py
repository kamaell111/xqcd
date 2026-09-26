from sqlalchemy import (
    Column, Integer, BigInteger, String, Boolean, DateTime, Float,
    ForeignKey, Text, JSON, UniqueConstraint,
)
from sqlalchemy.orm import relationship
from datetime import datetime
from database import Base


class User(Base):
    __tablename__ = "users"
    id = Column(Integer, primary_key=True)
    username = Column(String(64), unique=True, index=True, nullable=False)
    password_hash = Column(String(255), nullable=False)
    full_name = Column(String(128), nullable=True)
    privilege = Column(Integer, default=5)
    role = Column(String(32), default="viewer")
    is_active = Column(Boolean, default=True)
    token_version = Column(Integer, default=0)
    # Phase A: ownership
    is_super_admin = Column(Integer, default=0)                             # 1=Multivers, 0=admin biasa
    owner_user_id = Column(Integer, ForeignKey("users.id"), nullable=True)  # untuk operator/viewer: admin atasannya
    created_at = Column(DateTime, default=datetime.utcnow)
    updated_at = Column(DateTime, default=datetime.utcnow)
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
    # 1a-1: multi-vendor prep
    driver = Column(String(32), default="zte_zxan")
    hardware_type = Column(String(32), default="zte-c320")
    enabled = Column(Integer, default=1)
    # Phase A: ownership — NULL = milik Multivers/JSN pusat
    owner_user_id = Column(Integer, ForeignKey("users.id"), nullable=True, index=True)
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
    # Fix #3: ONUEvent & MetricHistory tanpa back_populates (tidak dipakai reverse)
    onu_events = relationship("ONUEvent", cascade="all, delete-orphan",
                              foreign_keys="ONUEvent.olt_id")
    metric_history = relationship("MetricHistory", cascade="all, delete-orphan",
                                  foreign_keys="MetricHistory.olt_id")


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
    last_dying_gasp = Column(DateTime, nullable=True)   # untuk suppress offline alert setelah dying_gasp
    updated_at = Column(DateTime, default=datetime.utcnow)
    olt = relationship("OLT", back_populates="onus")


class ONUEvent(Base):
    """Event log ONU — hanya catat saat status BERUBAH.
    Tujuan: hindari DB bengkak karena polling tiap 15s."""
    __tablename__ = "onu_events"
    id = Column(Integer, primary_key=True)
    olt_id = Column(Integer, ForeignKey("olts.id"), nullable=False, index=True)
    onu_id = Column(Integer, nullable=False, index=True)
    pon_port = Column(String(16), nullable=False)
    serial_number = Column(String(32), index=True)
    event_type = Column(String(32), nullable=False)   # status_change|pppoe_change
    old_value = Column(String(32), nullable=True)
    new_value = Column(String(32), nullable=True)
    detail = Column(Text, nullable=True)
    created_at = Column(DateTime, default=datetime.utcnow, index=True)


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
    __table_args__ = (
        UniqueConstraint('olt_id', 'vlan_id', name='uq_vlan_olt_vlan'),
    )
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

class TrafficSample(Base):
    """Sample traffic per interface (PON, Uplink, ONU).

    rate disimpan hasil hitung (bps) supaya query chart cepat.
    raw_octets disimpan untuk hitung delta poll berikutnya.
    """
    __tablename__ = "traffic_sample"
    id = Column(Integer, primary_key=True)
    olt_id = Column(Integer, ForeignKey("olts.id"), nullable=False, index=True)
    scope = Column(String(16), nullable=False)          # 'pon' | 'uplink' | 'onu'
    entity_key = Column(String(64), nullable=False)     # 'gpon_1/1/1', 'gei_1/3/3', 'gpon-onu_1/1/1:1'
    rx_bps = Column(Float, nullable=True)               # rate hitung
    tx_bps = Column(Float, nullable=True)
    raw_rx_octets = Column(BigInteger, nullable=True)   # counter raw saat sample
    raw_tx_octets = Column(BigInteger, nullable=True)
    ts = Column(DateTime, default=datetime.utcnow, index=True)

    __table_args__ = (
        # Index untuk query chart
        __import__("sqlalchemy").Index("idx_traffic_chart", "olt_id", "scope", "entity_key", "ts"),
    )
