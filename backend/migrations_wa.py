"""migrations_wa.py — migrasi idempotent untuk WA-2 master toggle."""
import os


def migrate_wa_engine(engine) -> None:
    """Idempotent. Panggil di startup setelah create_all, sebelum scheduler.start().
    Backfill HANYA di cabang ALTER (jalan sekali): kalau env=1 dan gateway+rule
    aktif, set engine_enabled=1 supaya production tidak mati diam-diam."""
    with engine.begin() as conn:
        cols = {r[1] for r in conn.exec_driver_sql("PRAGMA table_info(wa_settings)")}
        if cols:
            if "engine_locked" not in cols:
                conn.exec_driver_sql(
                    "ALTER TABLE wa_settings ADD COLUMN engine_locked INTEGER NOT NULL DEFAULT 0")
                print("[migrations_wa] OK: wa_settings.engine_locked ditambah")

            if "engine_enabled" not in cols:
                conn.exec_driver_sql(
                    "ALTER TABLE wa_settings ADD COLUMN engine_enabled INTEGER NOT NULL DEFAULT 0")
                print("[migrations_wa] OK: wa_settings.engine_enabled ditambah")

                # BACKFILL (production-safe)
                env_val = os.getenv("WA_ALERT_ENABLED", "0").strip().lower()
                if env_val in ("1", "true", "yes"):
                    result = conn.exec_driver_sql("""
                        UPDATE wa_settings SET engine_enabled = 1
                        WHERE enabled = 1
                          AND EXISTS (SELECT 1 FROM wa_alert_rules r
                                      WHERE r.scope_key = wa_settings.scope_key
                                        AND r.enabled = 1)
                    """)
                    print("[migrations_wa] OK: backfill engine_enabled=1 untuk scope aktif")
                else:
                    print(f"[migrations_wa] NOTE: env WA_ALERT_ENABLED={env_val}, backfill dilewati")

        # WaFlag table
        conn.exec_driver_sql("""
            CREATE TABLE IF NOT EXISTS wa_flag (
                name       TEXT PRIMARY KEY,
                value      TEXT NOT NULL DEFAULT '',
                updated_at DATETIME,
                updated_by TEXT
            )""")
        print("[migrations_wa] OK: wa_flag table siap")

        # Index tambahan
        conn.exec_driver_sql("""CREATE INDEX IF NOT EXISTS ix_wa_log_quota
                                ON wa_alert_log(scope_key, status, sent_at)""")
        conn.exec_driver_sql("""CREATE INDEX IF NOT EXISTS ix_wa_log_scope_created
                                ON wa_alert_log(scope_key, created_at DESC)""")
        print("[migrations_wa] OK: index tambahan")
