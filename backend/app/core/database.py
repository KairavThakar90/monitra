import os
import logging
import time
from typing import Optional
from dotenv import load_dotenv
from sqlalchemy import create_engine, event
from sqlalchemy.orm import sessionmaker, DeclarativeBase

logger = logging.getLogger(__name__)

# Which of these were set in the REAL environment, before .env was consulted.
# This distinction is the whole point: an explicitly exported DATABASE_URL (a
# Vercel environment variable, or one set for a one-off script or migration)
# must win over whatever .env happens to contain. The previous
# load_dotenv(override=True) did the opposite -- it silently overwrote an
# explicitly set DATABASE_URL with the .env value, so a command aimed at one
# database quietly ran against another.
_EXPLICIT_ENV = {
    key: os.environ.get(key)
    for key in ("DATABASE_URL", "DATABASE_URL_DEV", "ENV")
}

# override=False so real environment variables win over the .env file.
load_dotenv(override=False)

# Create engine lazily to avoid import-time failures in serverless
_engine = None
_SessionLocal = None

def describe_url(url: str) -> str:
    """Host/database of a connection string, with credentials stripped.

    Safe to log. Every caller that resolves a database should say which one it
    picked -- silent resolution is what let a local command write to production.
    """
    try:
        from sqlalchemy.engine import make_url

        parsed = make_url(url)
        return f"{parsed.host}/{parsed.database}"
    except Exception:  # noqa: BLE001 - never let logging break startup
        return "<unparseable url>"


def get_database_url():
    """Resolve the database URL, in a defined and explicit order.

    Order, highest priority first:

    1. DATABASE_URL exported in the real environment. This is how Vercel
       supplies production, and how a deliberate one-off (a migration, a load
       test) targets a specific branch. Nothing may override it.
    2. ENV == "production"  -> settings.DATABASE_URL.
    3. Anything else        -> settings.DATABASE_URL_DEV, the development
       database. This is the case that used to be wrong: the old code preferred
       the production URL regardless of ENV, so simply running the app or a
       script locally connected to production.
    4. If no dev URL is configured, fall back to the production URL but log a
       warning, so a misconfigured ENV still starts rather than crashing --
       the concern the original comment was trying to address.
    """
    from app.core.config import settings

    explicit = _EXPLICIT_ENV.get("DATABASE_URL")
    if explicit:
        logger.info("Database target: %s (explicit DATABASE_URL)", describe_url(explicit))
        return explicit

    if settings.ENV == "production":
        if not settings.DATABASE_URL:
            raise ValueError("ENV=production but DATABASE_URL is not set.")
        logger.info("Database target: %s (production)", describe_url(settings.DATABASE_URL))
        return settings.DATABASE_URL

    if settings.DATABASE_URL_DEV:
        logger.info("Database target: %s (development)",
                    describe_url(settings.DATABASE_URL_DEV))
        return settings.DATABASE_URL_DEV

    if settings.DATABASE_URL:
        logger.warning(
            "ENV=%s but DATABASE_URL_DEV is not set -- falling back to the "
            "production database at %s. Set DATABASE_URL_DEV to avoid this.",
            settings.ENV, describe_url(settings.DATABASE_URL),
        )
        return settings.DATABASE_URL

    error_msg = ("Database URL not configured. Set DATABASE_URL (prod) or "
                 "DATABASE_URL_DEV (dev) environment variable.")
    logger.error(error_msg)
    raise ValueError(error_msg)

def _connect_args(db_url: str) -> dict:
    """PostgreSQL session settings applied to every pooled connection."""
    from sqlalchemy.engine import make_url

    from app.core.config import settings

    if make_url(db_url).get_backend_name() != "postgresql":
        return {}
    args: dict = {
        # Names the session in pg_stat_activity, so this application's
        # connections can be told apart from psql, migrations and other services.
        "application_name": "monitra-api",
    }
    if settings.DB_IDLE_IN_TRANSACTION_TIMEOUT_MS > 0:
        args["options"] = (
            f"-c idle_in_transaction_session_timeout={int(settings.DB_IDLE_IN_TRANSACTION_TIMEOUT_MS)}"
        )
    return args


def _install_pool_monitoring(engine) -> None:
    """Log the pool events that matter when diagnosing a leak, and nothing else.

    Not every checkout and checkin: at 150 users that would be the loudest thing
    in the log and tell nobody anything. A connection held longer than
    `DB_CHECKOUT_WARN_SECONDS` is the signal -- it is a transaction left open
    across slow work -- and is reported once, when it comes back, with the path
    of the request that held it. An invalidated connection (the server dropped
    it, or killed it for sitting idle in a transaction) is reported too.
    """
    from app.core.config import settings
    from app.core.request_context import current_request_context

    warn_after = float(settings.DB_CHECKOUT_WARN_SECONDS)

    @event.listens_for(engine, "checkout")
    def _on_checkout(dbapi_connection, connection_record, connection_proxy):
        connection_record.info["monitra_checked_out_at"] = time.monotonic()
        context = current_request_context()
        connection_record.info["monitra_path"] = context.path if context and context.path else "-"

    @event.listens_for(engine, "checkin")
    def _on_checkin(dbapi_connection, connection_record):
        started = connection_record.info.pop("monitra_checked_out_at", None)
        path = connection_record.info.pop("monitra_path", "-")
        if started is None or warn_after <= 0:
            return
        held = time.monotonic() - started
        if held >= warn_after:
            logger.warning(
                "DB_CONNECTION_HELD_LONG: held=%.1fs path=%s pool=%s",
                held, path, engine.pool.status(),
            )

    @event.listens_for(engine, "invalidate")
    def _on_invalidate(dbapi_connection, connection_record, exception):
        logger.warning(
            "DB_CONNECTION_INVALIDATED: %s",
            type(exception).__name__ if exception is not None else "soft invalidate",
        )


def pool_snapshot() -> Optional[dict]:
    """How busy this process's pool is right now, or None before it has been used.

    Never creates the engine: a health check must not be what opens the first
    database connection.
    """
    if _engine is None:
        return None
    pool = _engine.pool
    try:
        return {
            "size": pool.size(),
            "checked_out": pool.checkedout(),
            "overflow": max(pool.overflow(), 0),
            "max_connections": pool.size() + pool._max_overflow,
        }
    except Exception:  # noqa: BLE001 - a pool without these counters (a test double)
        return None


def get_engine():
    """Get or create the database engine"""
    global _engine
    if _engine is None:
        try:
            from app.core.config import settings

            db_url = get_database_url()
            _engine = create_engine(
                db_url,
                pool_pre_ping=True,
                # Newest connection first: under light load the extra ones go idle and
                # are recycled instead of every one being kept warm.
                pool_use_lifo=True,
                pool_recycle=settings.DB_POOL_RECYCLE_SECONDS,
                pool_size=settings.DB_POOL_SIZE,
                max_overflow=settings.DB_MAX_OVERFLOW,
                pool_timeout=settings.DB_POOL_TIMEOUT_SECONDS,
                connect_args=_connect_args(db_url),
            )
            _install_pool_monitoring(_engine)
            logger.info(
                "Database engine created for environment: %s (pool_size=%s max_overflow=%s "
                "pool_timeout=%ss -> at most %s connections per process)",
                settings.ENV, settings.DB_POOL_SIZE, settings.DB_MAX_OVERFLOW,
                settings.DB_POOL_TIMEOUT_SECONDS, settings.DB_POOL_SIZE + settings.DB_MAX_OVERFLOW,
            )
        except Exception as e:
            logger.error(f"Failed to create database engine: {str(e)}")
            raise
    return _engine

def get_session_local():
    """Get or create the sessionmaker"""
    global _SessionLocal
    if _SessionLocal is None:
        engine = get_engine()
        _SessionLocal = sessionmaker(autocommit=False, autoflush=False, bind=engine)
    return _SessionLocal

class Base(DeclarativeBase):
    pass

def end_transaction(db) -> None:
    """Finish the session's open transaction so its connection returns to the pool.

    A Session holds one connection from its first query until a commit or
    rollback. Call this immediately before work that does not need the database
    and can take a while -- an SMTP send, a WFPM or Google Drive call -- so the
    wait is not spent holding a connection (and, on PostgreSQL, sitting
    "idle in transaction"). The next query simply checks out a fresh one.

    Commit, not rollback, so a caller that did have unflushed work loses nothing.
    Every instance the session loaded is expired by it: read what you still need
    into local variables *first*, or the next attribute access re-queries.
    """
    db.commit()


def get_db():
    """Database dependency for FastAPI.

    Always declare it as ``Depends(get_db, scope="function")``. FastAPI's default
    scope keeps a ``yield`` dependency open until the response has been sent *and*
    every background task has run, so the request's connection -- with whatever
    transaction its last query left open -- stayed checked out through WFPM and
    SMTP deliveries. ``scope="function"`` runs the clean-up below as soon as the
    route function (including response serialisation) returns.

    Every dependency in one request must use the same scope: FastAPI keys its
    per-request cache on it, so mixing scopes would open two sessions.
    tests/test_db_lifecycle.py fails if any site is left unscoped.
    """
    try:
        SessionLocal = get_session_local()
        db = SessionLocal()
    except Exception as e:
        logger.error(f"Failed to initialize database session: {str(e)}")
        from fastapi import HTTPException
        raise HTTPException(status_code=500, detail=f"Database connection error: {str(e)}")
    try:
        yield db
    except Exception as e:
        # A refused request (an HTTPException, or a body that failed validation)
        # is not a database error. Logging it as one was wrong, and for a
        # validation failure it was worse: its text carries the values the
        # caller submitted, so a malformed request body reached the log verbatim.
        from fastapi import HTTPException
        from fastapi.exceptions import RequestValidationError

        if not isinstance(e, (HTTPException, RequestValidationError)):
            logger.error(f"Database error: {str(e)}")
        db.rollback()
        raise
    finally:
        db.close()
