from sqlalchemy import create_engine, event
from sqlalchemy.orm import sessionmaker, declarative_base
from config import settings
import os

os.makedirs("./data", exist_ok=True)

engine = create_engine(
    settings.DATABASE_URL,
    connect_args={"check_same_thread": False, "timeout": 10},
)


# ⭐ WAL mode + pragma tuning — concurrency single-writer multi-reader
@event.listens_for(engine, "connect")
def _set_sqlite_pragma(dbapi_conn, connection_record):
    cursor = dbapi_conn.cursor()
    cursor.execute("PRAGMA journal_mode=WAL")
    cursor.execute("PRAGMA synchronous=NORMAL")
    cursor.execute("PRAGMA busy_timeout=5000")
    cursor.execute("PRAGMA foreign_keys=ON")
    cursor.execute("PRAGMA cache_size=-20000")
    # Checkpoint tiap 1000 halaman WAL (cegah file -wal membengkak)
    cursor.execute("PRAGMA wal_autocheckpoint=1000")
    # Batasi ukuran file -wal maks 64 MB (cegah disk penuh)
    cursor.execute("PRAGMA journal_size_limit=67108864")
    cursor.close()


SessionLocal = sessionmaker(autocommit=False, autoflush=False, bind=engine)
Base = declarative_base()


def get_db():
    db = SessionLocal()
    try:
        yield db
    finally:
        db.close()
