import os
import logging
from pathlib import Path
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker, declarative_base

# Configuration directory - persisted via Docker volume
CONFIG_DIR = Path(os.environ.get('CONFIG_DIR', str(Path(__file__).parent.parent / 'data')))
DB_PATH = CONFIG_DIR / 'streamflow.db'

# Single declarative base for the entire application
Base = declarative_base()

_engine = None
logger = logging.getLogger(__name__)


def _reconcile_sqlite_schema(engine) -> None:
    """Apply idempotent startup schema fixes for existing SQLite databases.

    ``Base.metadata.create_all`` only creates missing tables. It does not add
    new columns to already-existing tables, so older DB files can drift behind
    model changes and trigger runtime ``OperationalError`` failures.
    """
    if engine.dialect.name != 'sqlite':
        return

    # Keep this map explicit so startup upgrades are predictable and safe.
    required_columns = {
        'automation_profiles': {
            'enable_loop_detection': 'BOOLEAN NOT NULL DEFAULT 0',
        },
        'automation_periods': {
            'enable_loop_detection': 'BOOLEAN NOT NULL DEFAULT 0',
        },
        'monitoring_sessions': {
            'enable_loop_detection': 'BOOLEAN NOT NULL DEFAULT 0',
        },
    }

    with engine.begin() as conn:
        existing_tables = {
            row[0]
            for row in conn.exec_driver_sql(
                "SELECT name FROM sqlite_master WHERE type='table'"
            ).fetchall()
        }

        for table_name, columns in required_columns.items():
            if table_name not in existing_tables:
                continue

            existing_column_names = {
                row[1]
                for row in conn.exec_driver_sql(
                    f'PRAGMA table_info("{table_name}")'
                ).fetchall()
            }

            for column_name, column_def in columns.items():
                if column_name in existing_column_names:
                    continue

                conn.exec_driver_sql(
                    f'ALTER TABLE "{table_name}" '
                    f'ADD COLUMN "{column_name}" {column_def}'
                )
                logger.warning(
                    "Database schema upgraded on startup: added %s.%s",
                    table_name,
                    column_name,
                )


def _as_positive_int(name: str, default: int) -> int:
    raw_value = os.getenv(name)
    if raw_value is None:
        return default
    try:
        parsed = int(raw_value)
        return parsed if parsed > 0 else default
    except ValueError:
        return default

def get_engine():
    """Returns the SQLAlchemy engine, creating the directory if it doesn't exist."""
    global _engine
    if _engine is None:
        CONFIG_DIR.mkdir(parents=True, exist_ok=True)
        from sqlalchemy import create_engine, event

        pool_size = _as_positive_int('DB_POOL_SIZE', 10)
        max_overflow = _as_positive_int('DB_MAX_OVERFLOW', 20)
        pool_timeout = _as_positive_int('DB_POOL_TIMEOUT_SECONDS', 30)
        pool_recycle = _as_positive_int('DB_POOL_RECYCLE_SECONDS', 1800)
        
        # For SQLite in multi-threaded environment, check_same_thread=False is mandatory
        _engine = create_engine(
            f'sqlite:///{DB_PATH}', 
            echo=False,
            connect_args={'check_same_thread': False, 'timeout': 30},
            pool_size=pool_size,
            max_overflow=max_overflow,
            pool_timeout=pool_timeout,
            pool_recycle=pool_recycle,
            pool_pre_ping=True,
            future=True,
        )
        
        # Enable WAL (Write-Ahead Logging) mode to allow concurrent readers during writes
        @event.listens_for(_engine, "connect")
        def set_sqlite_pragma(dbapi_connection, connection_record):
            cursor = dbapi_connection.cursor()
            try:
                cursor.execute("PRAGMA journal_mode=WAL")
                cursor.execute("PRAGMA synchronous=NORMAL")
                # Validate applied PRAGMA values and fallback if necessary.
                journal_mode = cursor.execute("PRAGMA journal_mode").fetchone()
                synchronous = cursor.execute("PRAGMA synchronous").fetchone()

                if not journal_mode or str(journal_mode[0]).lower() != 'wal':
                    cursor.execute("PRAGMA journal_mode=DELETE")
                if not synchronous:
                    cursor.execute("PRAGMA synchronous=FULL")
            except Exception:
                # Keep defaults when PRAGMAs cannot be applied.
                pass
            finally:
                cursor.close()
                
    return _engine

def get_session():
    """Returns a new SQLAlchemy session."""
    engine = get_engine()
    Session = sessionmaker(bind=engine, autoflush=False, autocommit=False, future=True)
    return Session()

def init_db():
    """Create all tables if they don't exist."""
    engine = get_engine()
    
    # Import models here to ensure they are registered with Base
    from apps.database.models import (
        Channel, Stream, ChannelGroup, Logo, 
        M3UAccount, M3UAccountProfile,
        channel_streams, group_accounts,
        MatchProfile, MatchProfileStep,
        AutomationProfile, AutomationPeriod,
        MonitoringSession, DeadStream,
        ChannelRegexConfig, ChannelRegexPattern,
    )
    
    Base.metadata.create_all(engine)
    _reconcile_sqlite_schema(engine)
    return True
