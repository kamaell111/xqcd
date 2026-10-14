from pydantic import field_validator
from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    # WA_ALERT_* dibaca langsung via os.getenv di wa_alerting.py; biarkan Pydantic
    # mengabaikan field yang tidak dikenal (mis. WA_ALERT_INTERVAL, WA_ALLOW_PRIVATE_HOSTS).
    model_config = SettingsConfigDict(extra="ignore", env_file=".env", env_file_encoding="utf-8")
    APP_NAME: str = "ZTE OLT Manager"
    SECRET_KEY: str = "change-me-in-production-please-use-strong-key"
    ALGORITHM: str = "HS256"
    ACCESS_TOKEN_EXPIRE_MINUTES: int = 60
    DATABASE_URL: str = "sqlite:///./data/zte_olt_manager.db"
    SNMP_POLL_INTERVAL: int = 60
    SNMP_TIMEOUT: int = 3
    SNMP_RETRIES: int = 2
    ENABLE_SNMP_TRAP: bool = False   # default OFF — clone-and-run tanpa setup

    # ── SNMP Status Priority (Fase 1c) ────────────────────────────────────────
    # true  = status SNMP (2,3,4) prioritas, fallback Telnet
    # false = 100% Telnet (rollback cepat tanpa rebuild)
    SNMP_STATUS_PRIORITY: bool = True
    SNMP_BOARD_PRIORITY: bool = True   # Fase 2a: CPU/Mem/Firmware via SNMP

    # ── SNMP Traffic Migration Mode ───────────────────────────────────────────
    # telnet : semua traffic via Telnet (default, zero risk)
    # shadow : SNMP jalan paralel, log diff, TIDAK ubah DB
    # snmp   : SNMP primary, Telnet fallback per entity
    SNMP_TRAFFIC_MODE: str = "snmp"

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

    # =================== Cache TTL ===================
    # Berapa lama data OLT di-cache sebelum di-query ulang via Telnet.
    # Kecilkan = data lebih fresh, tapi beban OLT lebih besar.
    # Perbesar = hemat beban, tapi data bisa stale.
    PPPOE_CACHE_TTL_S: int = 60         # status PPPoE (connected/disconnected)
    DETAIL_CACHE_TTL_S: int = 600       # detail ONU (SN, distance, online duration)
    OPTICAL_CACHE_TTL_S: int = 120      # optical RX/TX per-PON

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


    @field_validator("SNMP_TRAFFIC_MODE", mode="before")
    @classmethod
    def _validate_snmp_traffic_mode(cls, v):
        allowed = ("telnet", "shadow", "snmp")
        if v not in allowed:
            raise ValueError(
                f"SNMP_TRAFFIC_MODE='{v}' tidak valid. Pilih: {', '.join(allowed)}"
            )
        return v

    # =================== AI Agent ===================
    # Kosongkan kalau tidak mau pakai fitur AI
    GEMINI_API_KEY: str = ""
    GROQ_API_KEY: str = ""
    GEMINI_MODEL: str = "gemini-3.5-flash-lite"
    GROQ_MODEL: str = "openai/gpt-oss-120b"
    AI_ENABLED: bool = True              # auto-disabled kalau key kosong
    AI_TIMEOUT_S: int = 15               # timeout per provider call
    AI_RATE_LIMIT_PER_USER_DAY: int = 50 # batas pertanyaan per user per hari




settings = Settings()