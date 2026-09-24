from pydantic_settings import BaseSettings


class Settings(BaseSettings):
    APP_NAME: str = "ZTE OLT Manager"
    SECRET_KEY: str = "change-me-in-production-please-use-strong-key"
    ALGORITHM: str = "HS256"
    ACCESS_TOKEN_EXPIRE_MINUTES: int = 60
    DATABASE_URL: str = "sqlite:///./data/zte_olt_manager.db"
    SNMP_POLL_INTERVAL: int = 60
    SNMP_TIMEOUT: int = 3
    SNMP_RETRIES: int = 2
    ENABLE_SNMP_TRAP: bool = False   # default OFF — clone-and-run tanpa setup

    # Alert thresholds
    ALERT_CPU_WARNING: float = 85.0
    ALERT_MEM_WARNING: float = 90.0
    ALERT_OPTICAL_RX_WARNING: float = -25.0     # dBm (makin negatif = makin lemah)
    ALERT_OPTICAL_RX_CRITICAL: float = -28.0    # dBm
    ALERT_AUTO_RESOLVE: bool = True

    # VLAN yang dipakai untuk manajemen OLT — tidak boleh dihapus dari UI
    # Kosongkan kalau tidak ada
    MANAGEMENT_VLAN: int = 0

    # DyingGasp — sinyal ONU sebelum mati total (kemungkinan pelanggan cabut adaptor).
    # false (default) = tidak alert untuk dying_gasp (kurangi noise)
    # true = alert "info" saat mendeteksi dying_gasp
    ALERT_DYING_GASP: bool = False

    # =================== Seed data ===================
    # User default aplikasi
    SEED_ADMIN_USERNAME: str = "admin"
    SEED_ADMIN_PASSWORD: str = "change-me-in-env"
    SEED_OPERATOR_USERNAME: str = "zte"
    SEED_OPERATOR_PASSWORD: str = "change-me-in-env"
    SEED_VIEWER_USERNAME: str = "viewer"
    SEED_VIEWER_PASSWORD: str = "change-me-in-env"

    # OLT yang didaftarkan otomatis saat seed
    SEED_OLT_HOSTNAME: str = "ZXAN"
    SEED_OLT_IP: str = "10.0.0.1"
    SEED_OLT_PROTOCOL: str = "telnet"
    SEED_OLT_PORT: int = 23
    SEED_OLT_USERNAME: str = "admin"
    SEED_OLT_PASSWORD: str = "admin"
    SEED_OLT_ENABLE_PASSWORD: str = ""
    SEED_OLT_SNMP_RO: str = "public"
    SEED_OLT_SNMP_RW: str = "private"

    class Config:
        env_file = ".env"


settings = Settings()