import os
import logging
from dotenv import load_dotenv
from sqlalchemy import create_engine
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

def get_engine():
    """Get or create the database engine"""
    global _engine
    if _engine is None:
        try:
            db_url = get_database_url()
            _engine = create_engine(
                db_url,
                pool_pre_ping=True,
                pool_recycle=3600,  # Recycle connections hourly so a Cloud SQL-side close is never surfaced to a request.
                # This backend is a persistent systemd service (2 uvicorn workers), each with its
                # own engine and pool -- not the many small serverless invocations this pool was
                # originally sized for (pool_size=5, max_overflow=10, "smaller pool for serverless").
                # That left only 15 connections per worker; a concurrency spike exhausted it on
                # 2026-10-02 and every request hung for the full 30s checkout timeout until the
                # worker was killed. Cloud SQL allows up to 800 connections and ordinarily runs
                # under 50, so 40/worker (80 total) adds headroom without being anywhere close to
                # that ceiling.
                pool_size=20,
                max_overflow=20,
            )
            from app.core.config import settings
            logger.info(f"Database engine created for environment: {settings.ENV}")
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

def get_db():
    """Database dependency for FastAPI"""
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
        logger.error(f"Database error: {str(e)}")
        db.rollback()
        raise
    finally:
        db.close()
